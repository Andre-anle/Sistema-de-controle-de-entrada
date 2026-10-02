import calendar
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

import migracoes
import validacao
from logs import log
from paths import APP_DIR, bancos_antigos

_banco: Path | None = None
_schema_pronto = False
_lock = threading.RLock()

# Só roda VACUUM (caro) quando muita coisa foi apagada ou faz tempo que não roda.
VACUUM_MIN_REGISTROS = 200
VACUUM_INTERVALO_DIAS = 30


def _sqlite_valido(caminho: Path) -> bool:
    if not caminho.exists() or caminho.stat().st_size < 100:
        return False
    try:
        conn = sqlite3.connect(str(caminho))
        try:
            conn.execute("SELECT 1 FROM sqlite_master LIMIT 1")
        finally:
            conn.close()
        return True
    except sqlite3.Error:
        return False


def _mover_banco(origem: Path, destino: Path) -> Path:
    if destino.exists():
        return origem
    try:
        conn = sqlite3.connect(str(origem))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        shutil.copy2(origem, destino)
        if not _sqlite_valido(destino):
            destino.unlink(missing_ok=True)
            return origem
    except (OSError, sqlite3.Error):
        return origem
    try:
        origem.replace(origem.with_name(f"{origem.name}.bak"))
    except OSError:
        pass
    return destino


def _localizar_banco() -> Path:
    destino = APP_DIR / "etiquetas.db"
    if _sqlite_valido(destino):
        return destino
    for antigo in bancos_antigos():
        if _sqlite_valido(antigo):
            return _mover_banco(antigo, destino)
    if destino.exists():
        return APP_DIR / "etiquetas_dados.db"
    return destino


def arquivo_banco() -> Path:
    global _banco
    if _banco is None:
        with _lock:
            if _banco is None:
                _banco = _localizar_banco()
    return _banco


def usar_banco(caminho: Path | None) -> None:
    """Troca o arquivo do banco (testes e restauração). None volta à detecção automática."""
    global _banco, _schema_pronto
    with _lock:
        _banco = caminho
        _schema_pronto = False


def _abrir() -> sqlite3.Connection:
    conn = sqlite3.connect(arquivo_banco(), timeout=15)
    conn.row_factory = sqlite3.Row
    # Segurança
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA trusted_schema = OFF")
    # Apagar de verdade: registros removidos pela retenção não ficam nas páginas livres.
    conn.execute("PRAGMA secure_delete = ON")
    # Desempenho (seguro com WAL)
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA cache_size = -16000")
    return conn


def _backup_antes_de_migrar() -> None:
    try:
        import backup

        backup.criar_backup("pre-migracao")
    except Exception:
        log("db").exception("Não foi possível fazer o backup antes da migração; seguindo.")


def _garantir_schema() -> None:
    global _schema_pronto
    if _schema_pronto:
        return
    with _lock:
        if _schema_pronto:
            return
        conn = _abrir()
        try:
            conn.execute("PRAGMA journal_mode = WAL")
            aplicadas = migracoes.migrar(conn, antes_de_alterar=_backup_antes_de_migrar)
            if aplicadas:
                log("db").info("Migrations aplicadas: %s", aplicadas)
        finally:
            conn.close()
        _schema_pronto = True


@contextmanager
def conexao() -> Iterator[sqlite3.Connection]:
    _garantir_schema()
    conn = _abrir()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def finalizar_uso() -> None:
    """Grava o que está pendente no WAL para o banco ficar consistente ao encerrar."""
    conn = _abrir()
    try:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        conn.execute("PRAGMA optimize")
    finally:
        conn.close()


def verificar_integridade(completa: bool = False) -> tuple[bool, str]:
    """`quick_check` (rápido) ou `integrity_check` (completo). Nunca levanta exceção."""
    try:
        _garantir_schema()
        conn = _abrir()
        try:
            comando = "PRAGMA integrity_check" if completa else "PRAGMA quick_check"
            resultado = [linha[0] for linha in conn.execute(comando).fetchall()]
        finally:
            conn.close()
    except (sqlite3.Error, migracoes.ErroMigracao) as exc:
        return False, f"Falha ao abrir o banco: {exc}"
    if resultado == ["ok"]:
        return True, "Banco íntegro."
    return False, "; ".join(str(r) for r in resultado[:5])


def estatisticas() -> dict[str, int]:
    """Números simples para monitoramento (sem dados pessoais)."""
    with conexao() as conn:
        dados = {
            "etiquetas": conn.execute("SELECT COUNT(*) FROM etiquetas").fetchone()[0],
            "visitantes": conn.execute("SELECT COUNT(*) FROM visitantes").fetchone()[0],
            "visitantes_na_loja": conn.execute(
                "SELECT COUNT(*) FROM visitantes WHERE saida IS NULL"
            ).fetchone()[0],
            "usuarios": conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0],
            "log_registros": conn.execute("SELECT COUNT(*) FROM log_alteracoes").fetchone()[0],
            "esquema": migracoes.versao_atual(conn),
        }
    try:
        dados["banco_bytes"] = arquivo_banco().stat().st_size
    except OSError:
        dados["banco_bytes"] = 0
    return {k: int(v) for k, v in dados.items()}


# ------------------------------------------------------------------- meta

def meta_ler(chave: str, padrao: str = "") -> str:
    with conexao() as conn:
        linha = conn.execute("SELECT valor FROM meta WHERE chave = ?", (chave,)).fetchone()
    return str(linha["valor"]) if linha else padrao


def meta_gravar(chave: str, valor: str) -> None:
    with conexao() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO meta (chave, valor, atualizado_em) VALUES (?, ?, ?)",
            (chave, valor, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        )


# ---------------------------------------------------------------- inserção

def adicionar_etiquetas(
    ean: str,
    descricao: str,
    colaborador: str,
    data_hora: str,
    quantidade: int,
) -> None:
    # Validação também aqui: o banco não confia em quem chama.
    ean = validacao.validar_ean_opcional(ean)
    descricao = validacao.validar_descricao(descricao)
    colaborador = validacao.validar_colaborador(colaborador)
    quantidade = validacao.validar_quantidade(quantidade)
    data_hora = normalizar_filtro_data(data_hora)
    with conexao() as conn:
        conn.execute(
            """
            INSERT INTO etiquetas (ean, descricao, colaborador, data_hora, quantidade)
            VALUES (?, ?, ?, ?, ?)
            """,
            (ean, descricao, colaborador, data_hora, quantidade),
        )


def normalizar_filtro_data(texto: str, fim_do_dia: bool = False) -> str:
    bruto = (texto or "").strip()
    if not bruto:
        return ""
    if len(bruto) > 19:
        raise ValueError("Data/hora inválida. Use dd/mm/aa, dd/mm/aaaa ou dd/mm/aaaa hh:mm.")
    formatos = (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%d/%m/%Y %H:%M:%S",
        "%d/%m/%Y %H:%M",
        "%d/%m/%Y",
        "%d/%m/%y %H:%M:%S",
        "%d/%m/%y %H:%M",
        "%d/%m/%y",
    )
    for formato in formatos:
        try:
            dt = datetime.strptime(bruto, formato)
        except ValueError:
            continue
        so_data = formato in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y")
        so_minuto = formato in (
            "%Y-%m-%d %H:%M",
            "%d/%m/%Y %H:%M",
            "%d/%m/%y %H:%M",
        )
        if so_data and fim_do_dia:
            dt = dt.replace(hour=23, minute=59, second=59)
        elif so_minuto and fim_do_dia:
            dt = dt.replace(second=59)
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    raise ValueError("Data/hora inválida. Use dd/mm/aa, dd/mm/aaaa ou dd/mm/aaaa hh:mm.")


def _like(termo: str) -> str:
    """Padrão LIKE 'contém', com %, _ e \\ do usuário tratados como texto comum."""
    return f"%{validacao.escapar_like(termo.strip())}%"


# Limite de linhas de uma consulta "sem limite" (exportação). Protege a memória.
LIMITE_EXPORTACAO = 100_000


def _consultar_com_limite(sql: str, params: tuple, limite: int | None) -> list[sqlite3.Row]:
    if limite is None:
        limite = LIMITE_EXPORTACAO + 1
    sql = f"{sql} LIMIT ?"  # nosec B608 (sql é sempre literal do módulo)
    params = (*params, int(limite))
    with conexao() as conn:
        linhas = conn.execute(sql, params).fetchall()
    if limite == LIMITE_EXPORTACAO + 1 and len(linhas) > LIMITE_EXPORTACAO:
        maximo = f"{LIMITE_EXPORTACAO:,}".replace(",", ".")
        raise ValueError(
            f"Há mais de {maximo} registros neste filtro. Use um período menor para exportar."
        )
    return linhas


def consultar_entradas_produto(
    ean: str = "",
    descricao: str = "",
    colaborador: str = "",
    data_inicio: str = "",
    data_fim: str = "",
    limite: int | None = 300,
) -> list[sqlite3.Row]:
    ean = validacao.limpar_filtro(ean, "filtro de EAN")
    descricao = validacao.limpar_filtro(descricao, "filtro de nome")
    colaborador = validacao.limpar_filtro(colaborador, "filtro de colaborador")
    inicio = normalizar_filtro_data(data_inicio)
    fim = normalizar_filtro_data(data_fim, fim_do_dia=True)
    return _consultar_com_limite(
        """
        SELECT id, ean, descricao, colaborador, quantidade, data_hora
        FROM etiquetas
        WHERE (? = '' OR ean LIKE ? ESCAPE '\\')
          AND (? = '' OR descricao LIKE ? ESCAPE '\\')
          AND (? = '' OR colaborador LIKE ? ESCAPE '\\')
          AND (? = '' OR data_hora >= ?)
          AND (? = '' OR data_hora <= ?)
        ORDER BY id DESC
        """,
        (
            ean,
            _like(ean),
            descricao,
            _like(descricao),
            colaborador,
            _like(colaborador),
            inicio,
            inicio,
            fim,
            fim,
        ),
        limite,
    )


def consultar_entradas_visitante(
    nome: str = "",
    empresa: str = "",
    data_inicio: str = "",
    data_fim: str = "",
    limite: int | None = 300,
) -> list[sqlite3.Row]:
    nome = validacao.limpar_filtro(nome, "filtro de visitante")
    empresa = validacao.limpar_filtro(empresa, "filtro de empresa")
    inicio = normalizar_filtro_data(data_inicio)
    fim = normalizar_filtro_data(data_fim, fim_do_dia=True)
    return _consultar_com_limite(
        """
        SELECT nome, documento, funcao, empresa, autorizado_por, entrada,
               COALESCE(saida, '') AS saida
        FROM visitantes
        WHERE (? = '' OR nome LIKE ? ESCAPE '\\')
          AND (? = '' OR empresa LIKE ? ESCAPE '\\')
          AND (? = '' OR entrada >= ?)
          AND (? = '' OR entrada <= ?)
        ORDER BY id DESC
        """,
        (
            nome,
            _like(nome),
            empresa,
            _like(empresa),
            inicio,
            inicio,
            fim,
            fim,
        ),
        limite,
    )


def ultimo_por_ean(ean: str) -> sqlite3.Row | None:
    with conexao() as conn:
        cursor = conn.execute(
            """
            SELECT ean, descricao, colaborador, data_hora
            FROM etiquetas
            WHERE ean = ?
            ORDER BY id DESC
            LIMIT 1
            """,
            (ean,),
        )
        return cursor.fetchone()


def adicionar_visitante(
    nome: str,
    documento: str,
    funcao: str,
    empresa: str,
    autorizado_por: str,
    entrada: str,
) -> None:
    nome, documento, funcao, empresa, autorizado_por = validacao.validar_visitante(
        nome, documento, funcao, empresa, autorizado_por
    )
    entrada = normalizar_filtro_data(entrada)
    with conexao() as conn:
        conn.execute(
            """
            INSERT INTO visitantes (nome, documento, funcao, empresa, autorizado_por, entrada)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (nome, documento, funcao, empresa, autorizado_por, entrada),
        )


def listar_visitantes_ativos() -> list[sqlite3.Row]:
    with conexao() as conn:
        return conn.execute(
            """
            SELECT id, nome, documento, funcao, empresa, autorizado_por, entrada
            FROM visitantes
            WHERE saida IS NULL
            ORDER BY entrada
            """
        ).fetchall()


def registrar_saida_visitante(visitante_id: int, saida: str) -> bool:
    saida = normalizar_filtro_data(saida)
    with conexao() as conn:
        cursor = conn.execute(
            "UPDATE visitantes SET saida = ? WHERE id = ? AND saida IS NULL",
            (saida, int(visitante_id)),
        )
        return cursor.rowcount > 0


def _subtrair_meses(dt: datetime, meses: int) -> datetime:
    total = dt.year * 12 + (dt.month - 1) - meses
    ano, mes = divmod(total, 12)
    mes += 1
    dia = min(dt.day, calendar.monthrange(ano, mes)[1])
    return dt.replace(year=ano, month=mes, day=dia)


def excluir_entradas_antigas(meses: int = 6) -> dict[str, int]:
    """Retenção: apaga entradas com mais de `meses` meses.

    Pode demorar (VACUUM); chame em segundo plano. O VACUUM só roda quando
    foi apagado bastante coisa ou quando faz mais de 30 dias do último.
    """
    agora = datetime.now()
    corte = _subtrair_meses(agora, meses).strftime("%Y-%m-%d %H:%M:%S")
    with conexao() as conn:
        etiquetas = conn.execute(
            "DELETE FROM etiquetas WHERE data_hora < ?", (corte,)
        ).rowcount
        visitantes = conn.execute(
            "DELETE FROM visitantes WHERE entrada < ?", (corte,)
        ).rowcount
    total = etiquetas + visitantes
    if total:
        ultimo = meta_ler("ultimo_vacuum")
        vencido = True
        if ultimo:
            try:
                vencido = (agora - datetime.strptime(ultimo, "%Y-%m-%d %H:%M:%S")).days >= VACUUM_INTERVALO_DIAS
            except ValueError:
                vencido = True
        if total >= VACUUM_MIN_REGISTROS or vencido:
            conn = _abrir()
            try:
                conn.execute("VACUUM")
            finally:
                conn.close()
            meta_gravar("ultimo_vacuum", agora.strftime("%Y-%m-%d %H:%M:%S"))
    return {"etiquetas": etiquetas, "visitantes": visitantes}
