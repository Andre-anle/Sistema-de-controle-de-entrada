import calendar
import shutil
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from paths import APP_DIR, bancos_antigos

_banco: Path | None = None
_schema_pronto = False
_lock = threading.RLock()


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


def _abrir() -> sqlite3.Connection:
    conn = sqlite3.connect(arquivo_banco(), timeout=15)
    conn.row_factory = sqlite3.Row
    return conn


def _garantir_schema() -> None:
    global _schema_pronto
    if _schema_pronto:
        return
    with _lock:
        if _schema_pronto:
            return
        conn = _abrir()
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            inicializar_schema(conn)
            conn.commit()
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


def _colunas(conn: sqlite3.Connection, tabela: str) -> set[str]:
    return {linha[1] for linha in conn.execute(f"PRAGMA table_info({tabela})")}


def inicializar_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS etiquetas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ean TEXT NOT NULL,
            descricao TEXT NOT NULL,
            colaborador TEXT NOT NULL,
            data_hora TEXT NOT NULL,
            quantidade INTEGER NOT NULL DEFAULT 1
        )
        """
    )
    colunas = _colunas(conn, "etiquetas")
    if "quantidade" not in colunas:
        conn.execute(
            "ALTER TABLE etiquetas ADD COLUMN quantidade INTEGER NOT NULL DEFAULT 1"
        )
    if "tipo" in colunas:
        outro_tipo = conn.execute(
            "SELECT 1 FROM etiquetas WHERE tipo != 'produto' LIMIT 1"
        ).fetchone()
        if outro_tipo is None:
            conn.execute("DROP INDEX IF EXISTS idx_etiquetas_tipo")
            try:
                conn.execute("ALTER TABLE etiquetas DROP COLUMN tipo")
            except sqlite3.OperationalError:
                pass

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS visitantes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            documento TEXT NOT NULL DEFAULT '',
            funcao TEXT NOT NULL,
            empresa TEXT NOT NULL,
            autorizado_por TEXT NOT NULL DEFAULT '',
            entrada TEXT NOT NULL,
            saida TEXT
        )
        """
    )
    colunas_vis = _colunas(conn, "visitantes")
    if "data_hora" in colunas_vis and "entrada" not in colunas_vis:
        conn.execute("ALTER TABLE visitantes RENAME COLUMN data_hora TO entrada")
    if "documento" not in colunas_vis:
        conn.execute(
            "ALTER TABLE visitantes ADD COLUMN documento TEXT NOT NULL DEFAULT ''"
        )
    if "autorizado_por" not in colunas_vis:
        conn.execute(
            "ALTER TABLE visitantes ADD COLUMN autorizado_por TEXT NOT NULL DEFAULT ''"
        )
    if "saida" not in colunas_vis:
        conn.execute("ALTER TABLE visitantes ADD COLUMN saida TEXT")

    for indice in (
        "idx_etiquetas_colaborador",
        "idx_etiquetas_descricao",
        "idx_visitantes_nome",
        "idx_visitantes_empresa",
    ):
        conn.execute(f"DROP INDEX IF EXISTS {indice}")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_etiquetas_ean ON etiquetas(ean)")
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_etiquetas_data_hora ON etiquetas(data_hora)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_visitantes_entrada ON visitantes(entrada)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_visitantes_ativos ON visitantes(entrada) "
        "WHERE saida IS NULL"
    )


def adicionar_etiquetas(
    ean: str,
    descricao: str,
    colaborador: str,
    data_hora: str,
    quantidade: int,
) -> None:
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
    return f"%{termo.strip()}%"


def _consultar_com_limite(sql: str, params: tuple, limite: int | None) -> list[sqlite3.Row]:
    if limite is not None:
        sql = f"{sql} LIMIT ?"
        params = (*params, int(limite))
    with conexao() as conn:
        return conn.execute(sql, params).fetchall()


def consultar_entradas_produto(
    ean: str = "",
    descricao: str = "",
    colaborador: str = "",
    data_inicio: str = "",
    data_fim: str = "",
    limite: int | None = 300,
) -> list[sqlite3.Row]:
    inicio = normalizar_filtro_data(data_inicio)
    fim = normalizar_filtro_data(data_fim, fim_do_dia=True)
    return _consultar_com_limite(
        """
        SELECT ean, descricao, colaborador, quantidade, data_hora
        FROM etiquetas
        WHERE (? = '' OR ean LIKE ? COLLATE NOCASE)
          AND (? = '' OR descricao LIKE ? COLLATE NOCASE)
          AND (? = '' OR colaborador LIKE ? COLLATE NOCASE)
          AND (? = '' OR data_hora >= ?)
          AND (? = '' OR data_hora <= ?)
        ORDER BY id DESC
        """,
        (
            ean.strip(),
            _like(ean),
            descricao.strip(),
            _like(descricao),
            colaborador.strip(),
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
    inicio = normalizar_filtro_data(data_inicio)
    fim = normalizar_filtro_data(data_fim, fim_do_dia=True)
    return _consultar_com_limite(
        """
        SELECT nome, documento, funcao, empresa, autorizado_por, entrada,
               COALESCE(saida, '') AS saida
        FROM visitantes
        WHERE (? = '' OR nome LIKE ? COLLATE NOCASE)
          AND (? = '' OR empresa LIKE ? COLLATE NOCASE)
          AND (? = '' OR entrada >= ?)
          AND (? = '' OR entrada <= ?)
        ORDER BY id DESC
        """,
        (
            nome.strip(),
            _like(nome),
            empresa.strip(),
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
    with conexao() as conn:
        cursor = conn.execute(
            "UPDATE visitantes SET saida = ? WHERE id = ? AND saida IS NULL",
            (saida, visitante_id),
        )
        return cursor.rowcount > 0


def _subtrair_meses(dt: datetime, meses: int) -> datetime:
    total = dt.year * 12 + (dt.month - 1) - meses
    ano, mes = divmod(total, 12)
    mes += 1
    dia = min(dt.day, calendar.monthrange(ano, mes)[1])
    return dt.replace(year=ano, month=mes, day=dia)


def excluir_entradas_antigas(meses: int = 6) -> dict[str, int]:
    corte = _subtrair_meses(datetime.now(), meses).strftime("%Y-%m-%d %H:%M:%S")
    with conexao() as conn:
        etiquetas = conn.execute(
            "DELETE FROM etiquetas WHERE data_hora < ?", (corte,)
        ).rowcount
        visitantes = conn.execute(
            "DELETE FROM visitantes WHERE entrada < ?", (corte,)
        ).rowcount
    if etiquetas or visitantes:
        conn = _abrir()
        try:
            conn.execute("VACUUM")
        finally:
            conn.close()
    return {"etiquetas": etiquetas, "visitantes": visitantes}
