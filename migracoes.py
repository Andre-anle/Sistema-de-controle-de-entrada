"""Migrations versionadas do banco (SQLite, `PRAGMA user_version`).

Regras:
- cada migration tem um número crescente e roda em UMA transação (ou tudo ou nada);
- o número aplicado fica em `PRAGMA user_version`, dentro da mesma transação;
- antes de alterar um banco que já tem dados, é feito um backup automático;
- um banco mais novo que o programa NÃO é aberto (evita corromper ao voltar de versão);
- bancos criados por versões antigas (user_version = 0) passam pela migration 1,
  que é idempotente e leva o esquema ao formato atual.

Para mudar o esquema: acrescente uma função `_mNNN` e registre-a em MIGRACOES.
Nunca edite uma migration já publicada.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime
from typing import Callable

from logs import log


class ErroMigracao(Exception):
    pass


def _colunas(conn: sqlite3.Connection, tabela: str) -> set[str]:
    return {linha[1] for linha in conn.execute(f"PRAGMA table_info({tabela})")}  # nosec B608 (constante)


# ------------------------------------------------------------------ 001
def _m001_esquema_base(conn: sqlite3.Connection) -> None:
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
        conn.execute(f"DROP INDEX IF EXISTS {indice}")  # nosec B608 (constantes)
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

    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS usuarios (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            usuario TEXT NOT NULL UNIQUE COLLATE NOCASE,
            senha_hash TEXT NOT NULL,
            perfil TEXT NOT NULL
                CHECK (perfil IN ('admin', 'gerente', 'visualizacao')),
            criado_em TEXT NOT NULL,
            trocar_senha INTEGER NOT NULL DEFAULT 0
        )
        """
    )
    if "trocar_senha" not in _colunas(conn, "usuarios"):
        conn.execute(
            "ALTER TABLE usuarios ADD COLUMN trocar_senha INTEGER NOT NULL DEFAULT 0"
        )
    # O log só aceita inclusão: os gatilhos abaixo bloqueiam qualquer
    # UPDATE/DELETE. Cada linha ainda carrega o hash da anterior, o que
    # permite detectar adulteração feita direto no arquivo do banco.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS log_alteracoes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            data_hora TEXT NOT NULL,
            usuario TEXT NOT NULL,
            perfil TEXT NOT NULL,
            acao TEXT NOT NULL,
            detalhes TEXT NOT NULL,
            ip TEXT NOT NULL DEFAULT '',
            hash_anterior TEXT NOT NULL,
            hash TEXT NOT NULL
        )
        """
    )
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_log_data_hora ON log_alteracoes(data_hora)"
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS log_alteracoes_sem_update
        BEFORE UPDATE ON log_alteracoes
        BEGIN
            SELECT RAISE(ABORT, 'O log de alterações não pode ser modificado.');
        END
        """
    )
    conn.execute(
        """
        CREATE TRIGGER IF NOT EXISTS log_alteracoes_sem_delete
        BEFORE DELETE ON log_alteracoes
        BEGIN
            SELECT RAISE(ABORT, 'O log de alterações não pode ser apagado.');
        END
        """
    )


# ------------------------------------------------------------------ 002
def _m002_meta(conn: sqlite3.Connection) -> None:
    """Tabela chave/valor para estado de manutenção (última limpeza, último VACUUM...)."""
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS meta (
            chave TEXT PRIMARY KEY,
            valor TEXT NOT NULL,
            atualizado_em TEXT NOT NULL
        )
        """
    )
    # Consultas por usuário no log de auditoria (tela /log).
    conn.execute("CREATE INDEX IF NOT EXISTS idx_log_usuario ON log_alteracoes(usuario)")


MIGRACOES: tuple[tuple[int, str, Callable[[sqlite3.Connection], None]], ...] = (
    (1, "esquema base", _m001_esquema_base),
    (2, "tabela meta e índice do log", _m002_meta),
)

VERSAO_ESQUEMA = MIGRACOES[-1][0]


def versao_atual(conn: sqlite3.Connection) -> int:
    return int(conn.execute("PRAGMA user_version").fetchone()[0])


def _tabela_existe(conn: sqlite3.Connection, nome: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (nome,)
    ).fetchone() is not None


def _banco_tem_dados(conn: sqlite3.Connection) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' LIMIT 1"
    ).fetchone() is not None


def pendentes(conn: sqlite3.Connection) -> list[int]:
    atual = versao_atual(conn)
    return [n for n, _, _ in MIGRACOES if n > atual]


def migrar(conn: sqlite3.Connection, *, antes_de_alterar: Callable[[], None] | None = None) -> list[int]:
    """Aplica as migrations pendentes e devolve os números aplicados."""
    atual = versao_atual(conn)
    if atual > VERSAO_ESQUEMA:
        raise ErroMigracao(
            f"O banco de dados é de uma versão mais nova do programa (esquema {atual}; "
            f"este programa entende até {VERSAO_ESQUEMA}). Atualize o programa."
        )
    aplicadas: list[int] = []
    primeira = True
    for numero, nome, funcao in MIGRACOES:
        if numero <= atual:
            continue
        if primeira:
            primeira = False
            if antes_de_alterar is not None and _banco_tem_dados(conn):
                antes_de_alterar()
        log("migracoes").info("Aplicando migration %03d (%s)...", numero, nome)
        try:
            conn.execute("BEGIN IMMEDIATE")
            funcao(conn)
            if _tabela_existe(conn, "meta"):
                conn.execute(
                    "INSERT OR REPLACE INTO meta (chave, valor, atualizado_em) VALUES (?, ?, ?)",
                    (f"migracao_{numero}", nome, datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
                )
            conn.execute(f"PRAGMA user_version = {int(numero)}")  # nosec B608 (int)
            conn.commit()
        except Exception:
            conn.rollback()
            log("migracoes").exception("Migration %03d falhou; nada foi alterado.", numero)
            raise
        aplicadas.append(numero)
    return aplicadas
