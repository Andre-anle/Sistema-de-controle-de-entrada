import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from paths import APP_DIR, candidatos_banco, caminho_banco


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


def arquivo_banco() -> Path:
    for caminho in candidatos_banco():
        if _sqlite_valido(caminho):
            return caminho
    destino = caminho_banco()
    if destino.exists() and not _sqlite_valido(destino):
        return APP_DIR / "etiquetas_dados.db"
    return destino


@contextmanager
def conexao() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(arquivo_banco(), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=8000")
    conn.execute("PRAGMA journal_mode=WAL")
    try:
        inicializar_schema(conn)
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def inicializar_schema(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS etiquetas (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ean TEXT NOT NULL,
            descricao TEXT NOT NULL,
            colaborador TEXT NOT NULL,
            data_hora TEXT NOT NULL
        )
        """
    )
    colunas = {linha[1] for linha in conn.execute("PRAGMA table_info(etiquetas)")}
    if "tipo" not in colunas:
        conn.execute(
            "ALTER TABLE etiquetas ADD COLUMN tipo TEXT NOT NULL DEFAULT 'produto'"
        )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS visitantes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            nome TEXT NOT NULL,
            funcao TEXT NOT NULL,
            empresa TEXT NOT NULL,
            data_hora TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_etiquetas_ean ON etiquetas(ean)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_etiquetas_tipo ON etiquetas(tipo)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_etiquetas_colaborador ON etiquetas(colaborador)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_etiquetas_descricao ON etiquetas(descricao)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_visitantes_nome ON visitantes(nome)"
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_visitantes_empresa ON visitantes(empresa)"
    )


def adicionar_etiquetas(
    ean: str,
    descricao: str,
    colaborador: str,
    data_hora: str,
    quantidade: int,
) -> None:
    registros = [(ean, descricao, colaborador, data_hora, "produto")] * quantidade
    with conexao() as conn:
        conn.executemany(
            """
            INSERT INTO etiquetas (ean, descricao, colaborador, data_hora, tipo)
            VALUES (?, ?, ?, ?, ?)
            """,
            registros,
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
        SELECT ean, descricao, colaborador, data_hora
        FROM etiquetas
        WHERE tipo = 'produto'
          AND (? = '' OR ean LIKE ? COLLATE NOCASE)
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
        SELECT nome, funcao, empresa, data_hora
        FROM visitantes
        WHERE (? = '' OR nome LIKE ? COLLATE NOCASE)
          AND (? = '' OR empresa LIKE ? COLLATE NOCASE)
          AND (? = '' OR data_hora >= ?)
          AND (? = '' OR data_hora <= ?)
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


def _listar_distintos(coluna: str, tabela: str = "etiquetas") -> list[str]:
    colunas_ok = {
        ("etiquetas", "ean"),
        ("etiquetas", "descricao"),
        ("etiquetas", "colaborador"),
        ("visitantes", "nome"),
        ("visitantes", "empresa"),
        ("visitantes", "funcao"),
    }
    if (tabela, coluna) not in colunas_ok:
        raise ValueError("Coluna inválida para autocomplete.")
    if tabela == "etiquetas":
        sql = (
            f"SELECT DISTINCT {coluna} FROM etiquetas "
            f"WHERE tipo = 'produto' AND TRIM({coluna}) != '' "
            f"ORDER BY {coluna} COLLATE NOCASE LIMIT 500"
        )
    else:
        sql = (
            f"SELECT DISTINCT {coluna} FROM {tabela} "
            f"WHERE TRIM({coluna}) != '' "
            f"ORDER BY {coluna} COLLATE NOCASE LIMIT 500"
        )
    with conexao() as conn:
        return [str(linha[0]) for linha in conn.execute(sql) if linha[0]]


def listar_eans() -> list[str]:
    return _listar_distintos("ean")


def listar_descricoes() -> list[str]:
    return _listar_distintos("descricao")


def listar_colaboradores() -> list[str]:
    return _listar_distintos("colaborador")


def listar_nomes_visitantes() -> list[str]:
    return _listar_distintos("nome", "visitantes")


def listar_empresas_visitantes() -> list[str]:
    return _listar_distintos("empresa", "visitantes")


def ultimo_por_ean(ean: str) -> sqlite3.Row | None:
    with conexao() as conn:
        cursor = conn.execute(
            """
            SELECT ean, descricao, colaborador, data_hora
            FROM etiquetas
            WHERE ean = ? AND tipo = 'produto'
            ORDER BY id DESC
            LIMIT 1
            """,
            (ean,),
        )
        return cursor.fetchone()


def adicionar_visitante(nome: str, funcao: str, empresa: str, data_hora: str) -> None:
    with conexao() as conn:
        conn.execute(
            """
            INSERT INTO visitantes (nome, funcao, empresa, data_hora)
            VALUES (?, ?, ?, ?)
            """,
            (nome, funcao, empresa, data_hora),
        )
