"""Contas, perfis de acesso, sessões e log de alterações do acesso remoto."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime

import db
import validacao
from limitador import BloqueioPorFalhas
from logs import seguranca, seguro
from validacao import MENSAGEM_EAN_INVALIDO, ean_valido

PERFIL_ADMIN = "admin"
PERFIL_GERENTE = "gerente"
PERFIL_VISUALIZACAO = "visualizacao"

PERFIS = {
    PERFIL_ADMIN: "Administrador",
    PERFIL_GERENTE: "Gerente",
    PERFIL_VISUALIZACAO: "Visualização",
}

# Todos os perfis enxergam consultas, exportação e o log ("ver").
# "configurar" libera, no aplicativo do computador, os menus de impressora/rede
# e de segurança/backup.
PERMISSOES = {
    PERFIL_ADMIN: frozenset({"ver", "editar", "usuarios", "configurar"}),
    PERFIL_GERENTE: frozenset({"ver", "editar", "configurar"}),
    PERFIL_VISUALIZACAO: frozenset({"ver"}),
}

ROTULOS_ACAO = {
    "login": "Entrada",
    "login_falhou": "Falha de login",
    "logout": "Saída",
    "bloqueio_login": "Bloqueio por excesso de tentativas",
    "exportou": "Exportação de planilha",
    "produto_editado": "Mercadoria editada",
    "usuario_criado": "Conta criada",
    "usuario_alterado": "Conta alterada",
    "usuario_excluido": "Conta excluída",
    "senha_alterada": "Senha alterada",
    "admin_padrao": "Administrador padrão criado",
    "admin_redefinido": "Senha do administrador redefinida",
    "retencao": "Limpeza de dados antigos",
    "backup": "Backup",
    "restauracao": "Restauração de backup",
}

USUARIO_PADRAO = "admin"
# Senha das versões antigas. Não é mais usada; segue aqui só para ser recusada.
SENHA_PADRAO = "admin123"

USUARIO_RE = re.compile(r"[a-z0-9._-]{3,32}")
SENHA_MIN = 8
SENHA_MAX = 128
DESCRICAO_MAX = validacao.DESCRICAO_MAX

# OWASP: PBKDF2-HMAC-SHA256 com pelo menos 600.000 iterações.
ITERACOES_SENHA = 600_000
ITERACOES_MAX_ACEITAS = 5_000_000  # proteção contra valor absurdo gravado no banco

DURACAO_SESSAO = 8 * 3600.0       # vida máxima absoluta
OCIOSIDADE_SESSAO = 30 * 60.0     # some após 30 min sem uso
MAX_SESSOES_USUARIO = 5

MAX_FALHAS = 5                    # por IP
JANELA_FALHAS = 300.0
MAX_FALHAS_USUARIO = 15           # por conta (qualquer IP)
JANELA_FALHAS_USUARIO = 900.0

_GENESIS = "0" * 64
_ALFABETO_SENHA = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnpqrstuvwxyz23456789"

SENHAS_FRACAS = frozenset(
    {
        "admin123", "administrador", "12345678", "123456789", "1234567890",
        "123123123", "11111111", "00000000", "87654321", "qwertyui", "qwerty123",
        "password", "password1", "senha123", "senha1234", "mudar123", "trocar123",
        "abcd1234", "abc12345", "hortifruti", "hortifruti123", "natural123",
    }
)


@dataclass(frozen=True)
class Usuario:
    id: int
    usuario: str
    perfil: str
    trocar_senha: bool = False

    def pode(self, permissao: str) -> bool:
        return permissao in PERMISSOES.get(self.perfil, frozenset())

    @property
    def rotulo_perfil(self) -> str:
        return PERFIS.get(self.perfil, self.perfil)


class LoginBloqueado(Exception):
    def __init__(self, segundos: int) -> None:
        super().__init__("Muitas tentativas. Aguarde alguns minutos e tente de novo.")
        self.segundos = segundos


def exigir(usuario: Usuario, permissao: str) -> None:
    if not usuario.pode(permissao):
        raise PermissionError("Seu perfil não tem permissão para esta ação.")


# ----------------------------------------------------------------- senhas

def hash_senha(senha: str, iteracoes: int = ITERACOES_SENHA) -> str:
    sal = secrets.token_bytes(16)
    derivada = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), sal, iteracoes)
    return f"pbkdf2_sha256${iteracoes}${sal.hex()}${derivada.hex()}"


def _partes_hash(armazenado: str) -> tuple[int, bytes, str] | None:
    try:
        algoritmo, iteracoes, sal, esperado = armazenado.split("$")
        numero = int(iteracoes)
        if algoritmo != "pbkdf2_sha256" or not 1 <= numero <= ITERACOES_MAX_ACEITAS:
            return None
        return numero, bytes.fromhex(sal), esperado
    except (ValueError, TypeError, AttributeError):
        return None


def verificar_senha(senha: str, armazenado: str) -> bool:
    partes = _partes_hash(armazenado)
    if partes is None:
        return False
    iteracoes, sal, esperado = partes
    derivada = hashlib.pbkdf2_hmac("sha256", senha.encode("utf-8"), sal, iteracoes)
    return hmac.compare_digest(derivada.hex(), esperado)


def precisa_rehash(armazenado: str) -> bool:
    """True se o hash foi gerado com menos iterações que o padrão atual."""
    partes = _partes_hash(armazenado)
    return partes is not None and partes[0] < ITERACOES_SENHA


_senha_falsa: str | None = None
_trava_falsa = threading.Lock()


def _hash_falso() -> str:
    # Mantém o tempo de resposta igual para usuário inexistente.
    global _senha_falsa
    with _trava_falsa:
        if _senha_falsa is None:
            _senha_falsa = hash_senha(secrets.token_urlsafe(8))
        return _senha_falsa


def validar_senha(senha: str, usuario: str = "") -> None:
    if len(senha) < SENHA_MIN:
        raise ValueError(f"A senha deve ter pelo menos {SENHA_MIN} caracteres.")
    if len(senha) > SENHA_MAX:
        raise ValueError(f"A senha deve ter no máximo {SENHA_MAX} caracteres.")
    if senha.lower() in SENHAS_FRACAS or len(set(senha)) < 4:
        raise ValueError("Senha muito previsível. Escolha outra, menos comum.")
    if usuario and len(usuario) >= 3 and usuario.lower() in senha.lower():
        raise ValueError("A senha não pode conter o nome de usuário.")


def gerar_senha_inicial() -> str:
    """Senha aleatória legível (sem 0/O/1/l/I), no formato XXXX-XXXX-XXXX."""
    grupos = ["".join(secrets.choice(_ALFABETO_SENHA) for _ in range(4)) for _ in range(3)]
    return "-".join(grupos)


def normalizar_usuario(nome: str) -> str:
    nome = (nome or "").strip().lower()
    if not USUARIO_RE.fullmatch(nome):
        raise ValueError(
            "Usuário: use de 3 a 32 caracteres (letras, números, ponto, hífen ou sublinhado)."
        )
    return nome


def _validar_perfil(perfil: str) -> None:
    if perfil not in PERFIS:
        raise ValueError("Perfil inválido.")


# -------------------------------------------------------------- log (hash)

_trava_escrita = threading.RLock()


@contextmanager
def _escrita():
    """Transação de escrita exclusiva: mantém o encadeamento do log consistente."""
    with _trava_escrita:
        with db.conexao() as conn:
            conn.execute("BEGIN IMMEDIATE")
            yield conn


def _agora() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def _limpar(texto: str, limite: int) -> str:
    limpo = "".join(c if c.isprintable() else " " for c in str(texto or ""))
    limpo = " ".join(limpo.split())
    return limpo if len(limpo) <= limite else limpo[: limite - 1] + "…"


def _calcular_hash(anterior: str, data_hora: str, usuario: str, perfil: str,
                   acao: str, detalhes: str, ip: str) -> str:
    carga = json.dumps(
        [anterior, data_hora, usuario, perfil, acao, detalhes, ip],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(carga.encode("utf-8")).hexdigest()


def _registrar(conn: sqlite3.Connection, usuario: str, perfil: str, acao: str,
               detalhes: str, ip: str) -> None:
    ultimo = conn.execute(
        "SELECT hash FROM log_alteracoes ORDER BY id DESC LIMIT 1"
    ).fetchone()
    anterior = ultimo["hash"] if ultimo else _GENESIS
    data_hora = _agora()
    usuario = _limpar(usuario, 64) or "(vazio)"
    perfil = _limpar(perfil, 32) or "-"
    detalhes = _limpar(detalhes, 600)
    ip = _limpar(ip, 45)
    assinatura = _calcular_hash(anterior, data_hora, usuario, perfil, acao, detalhes, ip)
    conn.execute(
        """
        INSERT INTO log_alteracoes
            (data_hora, usuario, perfil, acao, detalhes, ip, hash_anterior, hash)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (data_hora, usuario, perfil, acao, detalhes, ip, anterior, assinatura),
    )


def registrar_evento(acao: str, detalhes: str, *, usuario: str = "sistema",
                     perfil: str = "-", ip: str = "local") -> None:
    """Grava um evento no log de auditoria (retenção, backup, exportação...)."""
    with _escrita() as conn:
        _registrar(conn, usuario, perfil, acao, detalhes, ip)


def registrar_exportacao(ator: Usuario, tipo: str, filtros: str, total: int, ip: str) -> None:
    registrar_evento(
        "exportou",
        f"Exportou {total} registro(s) ({tipo}). Filtros: {filtros}",
        usuario=ator.usuario, perfil=ator.perfil, ip=ip,
    )


def listar_log(usuario: str = "", acao: str = "", data_inicio: str = "",
               data_fim: str = "", limite: int = 500) -> list[sqlite3.Row]:
    inicio = db.normalizar_filtro_data(data_inicio)
    fim = db.normalizar_filtro_data(data_fim, fim_do_dia=True)
    usuario = validacao.limpar_filtro(usuario, "filtro de usuário")
    acao = validacao.limpar_filtro(acao, "filtro de ação")
    padrao = f"%{validacao.escapar_like(usuario)}%"
    with db.conexao() as conn:
        return conn.execute(
            """
            SELECT id, data_hora, usuario, perfil, acao, detalhes, ip
            FROM log_alteracoes
            WHERE (? = '' OR usuario LIKE ? ESCAPE '\\')
              AND (? = '' OR acao = ?)
              AND (? = '' OR data_hora >= ?)
              AND (? = '' OR data_hora <= ?)
            ORDER BY id DESC
            LIMIT ?
            """,
            (usuario, padrao, acao, acao, inicio, inicio, fim, fim, int(limite)),
        ).fetchall()


# Verificação incremental: o log só cresce, então depois de conferir tudo uma vez
# basta conferir as linhas novas. Uma conferência completa roda de tempos em tempos.
REVERIFICACAO_COMPLETA = 600.0
_trava_verificacao = threading.Lock()
_estado_verificacao: dict | None = None


def _conferir_trecho(conn: sqlite3.Connection, anterior: str, depois_de: int) -> tuple[bool, str, int, int, str]:
    """Confere as linhas com id > depois_de. Devolve (ok, mensagem, contadas, ultimo_id, ultimo_hash)."""
    ultimo_id, contadas = depois_de, 0
    for linha in conn.execute("SELECT * FROM log_alteracoes WHERE id > ? ORDER BY id", (depois_de,)):
        if linha["hash_anterior"] != anterior:
            return False, f"Sequência quebrada no registro nº {linha['id']}.", contadas, ultimo_id, anterior
        esperado = _calcular_hash(
            linha["hash_anterior"], linha["data_hora"], linha["usuario"],
            linha["perfil"], linha["acao"], linha["detalhes"], linha["ip"],
        )
        if not hmac.compare_digest(esperado, linha["hash"]):
            return False, f"Registro nº {linha['id']} foi alterado.", contadas, ultimo_id, anterior
        anterior = linha["hash"]
        ultimo_id = linha["id"]
        contadas += 1
    return True, "", contadas, ultimo_id, anterior


def verificar_log(forcar: bool = False) -> tuple[bool, str]:
    """Confere o encadeamento de hashes do log (completo ou incremental)."""
    global _estado_verificacao
    with _trava_verificacao:
        with db.conexao() as conn:
            estado = _estado_verificacao
            agora = time.monotonic()
            incremental = (
                not forcar
                and estado is not None
                and agora - estado["quando"] < REVERIFICACAO_COMPLETA
            )
            if incremental and estado["ultimo_id"]:
                # a linha já conferida continua igual? (barato; senão refaz tudo)
                ancora = conn.execute(
                    "SELECT hash FROM log_alteracoes WHERE id = ?", (estado["ultimo_id"],)
                ).fetchone()
                incremental = ancora is not None and ancora["hash"] == estado["hash"]
            if incremental:
                ok, msg, novas, ultimo_id, ultimo_hash = _conferir_trecho(
                    conn, estado["hash"], estado["ultimo_id"]
                )
                total = estado["total"] + novas
                quando = estado["quando"]
            else:
                ok, msg, total, ultimo_id, ultimo_hash = _conferir_trecho(conn, _GENESIS, 0)
                quando = agora
            if not ok:
                _estado_verificacao = None
                seguranca().critical("Log de auditoria adulterado: %s", msg)
                return False, msg
            sequencia = conn.execute(
                "SELECT seq FROM sqlite_sequence WHERE name = 'log_alteracoes'"
            ).fetchone()
            if sequencia and sequencia["seq"] > ultimo_id:
                _estado_verificacao = None
                seguranca().critical("Log de auditoria: registros do final foram removidos.")
                return False, "Registros do final do log foram removidos."
            _estado_verificacao = {
                "ultimo_id": ultimo_id, "hash": ultimo_hash, "total": total, "quando": quando,
            }
    return True, f"{total} registro(s) conferido(s), sem sinais de alteração."


def reiniciar_verificacao() -> None:
    global _estado_verificacao
    with _trava_verificacao:
        _estado_verificacao = None


# ---------------------------------------------------------------- usuários

def _linha_para_usuario(linha: sqlite3.Row | None) -> Usuario | None:
    if linha is None:
        return None
    return Usuario(
        int(linha["id"]),
        str(linha["usuario"]),
        str(linha["perfil"]),
        bool(linha["trocar_senha"]),
    )


def obter_usuario(usuario_id: int) -> Usuario | None:
    with db.conexao() as conn:
        return _linha_para_usuario(
            conn.execute(
                "SELECT id, usuario, perfil, trocar_senha FROM usuarios WHERE id = ?",
                (usuario_id,),
            ).fetchone()
        )


def listar_usuarios() -> list[sqlite3.Row]:
    with db.conexao() as conn:
        return conn.execute(
            "SELECT id, usuario, perfil, criado_em FROM usuarios ORDER BY usuario"
        ).fetchall()


def contar_usuarios() -> int:
    with db.conexao() as conn:
        return int(conn.execute("SELECT COUNT(*) FROM usuarios").fetchone()[0])


def _contar_admins(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute(
            "SELECT COUNT(*) FROM usuarios WHERE perfil = ?", (PERFIL_ADMIN,)
        ).fetchone()[0]
    )


def criar_usuario(ator: Usuario, nome: str, senha: str, perfil: str, ip: str) -> None:
    exigir(ator, "usuarios")
    nome = normalizar_usuario(nome)
    validar_senha(senha, nome)
    _validar_perfil(perfil)
    senha_hash = hash_senha(senha)
    with _escrita() as conn:
        if conn.execute("SELECT 1 FROM usuarios WHERE usuario = ?", (nome,)).fetchone():
            raise ValueError("Já existe uma conta com esse nome de usuário.")
        conn.execute(
            "INSERT INTO usuarios (usuario, senha_hash, perfil, criado_em) VALUES (?, ?, ?, ?)",
            (nome, senha_hash, perfil, _agora()),
        )
        _registrar(conn, ator.usuario, ator.perfil, "usuario_criado",
                   f"Conta '{nome}' criada com perfil {PERFIS[perfil]}.", ip)


def alterar_usuario(ator: Usuario, usuario_id: int, perfil: str, nova_senha: str, ip: str) -> bool:
    """Altera perfil e/ou senha. Retorna True se a senha foi trocada."""
    exigir(ator, "usuarios")
    _validar_perfil(perfil)
    with _escrita() as conn:
        alvo = conn.execute(
            "SELECT id, usuario, perfil FROM usuarios WHERE id = ?", (usuario_id,)
        ).fetchone()
        if alvo is None:
            raise ValueError("Conta não encontrada.")
        if nova_senha:
            validar_senha(nova_senha, alvo["usuario"])
        senha_hash = hash_senha(nova_senha) if nova_senha else ""
        mudancas = []
        if perfil != alvo["perfil"]:
            if alvo["perfil"] == PERFIL_ADMIN and _contar_admins(conn) <= 1:
                raise ValueError("Precisa existir pelo menos um administrador.")
            conn.execute("UPDATE usuarios SET perfil = ? WHERE id = ?", (perfil, usuario_id))
            mudancas.append(
                f"perfil {PERFIS.get(alvo['perfil'], alvo['perfil'])} → {PERFIS[perfil]}"
            )
        if senha_hash:
            conn.execute("UPDATE usuarios SET senha_hash = ? WHERE id = ?", (senha_hash, usuario_id))
            mudancas.append("senha redefinida")
        if mudancas:
            _registrar(conn, ator.usuario, ator.perfil, "usuario_alterado",
                       f"Conta '{alvo['usuario']}': " + "; ".join(mudancas) + ".", ip)
    return bool(senha_hash)


def excluir_usuario(ator: Usuario, usuario_id: int, ip: str) -> None:
    exigir(ator, "usuarios")
    if usuario_id == ator.id:
        raise ValueError("Você não pode excluir a própria conta.")
    with _escrita() as conn:
        alvo = conn.execute(
            "SELECT id, usuario, perfil FROM usuarios WHERE id = ?", (usuario_id,)
        ).fetchone()
        if alvo is None:
            raise ValueError("Conta não encontrada.")
        if alvo["perfil"] == PERFIL_ADMIN and _contar_admins(conn) <= 1:
            raise ValueError("Precisa existir pelo menos um administrador.")
        conn.execute("DELETE FROM usuarios WHERE id = ?", (usuario_id,))
        _registrar(conn, ator.usuario, ator.perfil, "usuario_excluido",
                   f"Conta '{alvo['usuario']}' ({PERFIS.get(alvo['perfil'], alvo['perfil'])}) excluída.", ip)
    encerrar_sessoes_usuario(usuario_id)


def _exigir_troca_da_senha_antiga() -> int:
    """Contas de versões antigas que ainda usam a senha padrão conhecida ("admin123")
    passam a exigir a troca no próximo acesso. Devolve quantas foram marcadas."""
    with db.conexao() as conn:
        candidatas = conn.execute(
            "SELECT id, usuario, senha_hash FROM usuarios WHERE trocar_senha = 0"
        ).fetchall()
    marcadas = 0
    for conta in candidatas:
        if not verificar_senha(SENHA_PADRAO, conta["senha_hash"]):
            continue
        with _escrita() as conn:
            conn.execute("UPDATE usuarios SET trocar_senha = 1 WHERE id = ?", (conta["id"],))
            _registrar(conn, "sistema", "-", "admin_padrao",
                       f"Conta '{conta['usuario']}' ainda usava a senha padrão antiga; troca exigida.",
                       "local")
        seguranca().warning("Conta %s usa a senha padrão antiga; troca exigida.", seguro(conta["usuario"]))
        marcadas += 1
    return marcadas


def garantir_admin_padrao() -> str | None:
    """Cria o administrador inicial quando ainda não existe nenhuma conta.

    A senha é ALEATÓRIA (não existe mais senha padrão conhecida) e a troca é
    obrigatória no primeiro acesso. Devolve a senha para ser mostrada UMA vez
    ao dono do computador, ou None se já havia contas.
    """
    if contar_usuarios() > 0:
        _exigir_troca_da_senha_antiga()
        return None
    senha = gerar_senha_inicial()
    senha_hash = hash_senha(senha)  # lento: feito antes de travar o banco
    with _escrita() as conn:
        if conn.execute("SELECT 1 FROM usuarios LIMIT 1").fetchone():
            return None
        conn.execute(
            """
            INSERT INTO usuarios (usuario, senha_hash, perfil, criado_em, trocar_senha)
            VALUES (?, ?, ?, ?, 1)
            """,
            (USUARIO_PADRAO, senha_hash, PERFIL_ADMIN, _agora()),
        )
        _registrar(conn, "sistema", "-", "admin_padrao",
                   f"Conta '{USUARIO_PADRAO}' criada com senha aleatória; troca exigida no primeiro acesso.",
                   "local")
    return senha


def redefinir_senha_admin() -> tuple[str, str]:
    """Gera nova senha aleatória para o administrador (troca obrigatória no próximo acesso).

    Quem chama deve ter verificado que o usuário do computador é o dono (código de recuperação).
    Devolve (usuario, nova_senha).
    """
    senha = gerar_senha_inicial()
    senha_hash = hash_senha(senha)
    with _escrita() as conn:
        alvo = conn.execute(
            "SELECT id, usuario FROM usuarios WHERE perfil = ? ORDER BY id LIMIT 1", (PERFIL_ADMIN,)
        ).fetchone()
        if alvo is None:
            conn.execute(
                "INSERT INTO usuarios (usuario, senha_hash, perfil, criado_em, trocar_senha) "
                "VALUES (?, ?, ?, ?, 1)",
                (USUARIO_PADRAO, senha_hash, PERFIL_ADMIN, _agora()),
            )
            nome, alvo_id = USUARIO_PADRAO, None
        else:
            conn.execute(
                "UPDATE usuarios SET senha_hash = ?, trocar_senha = 1 WHERE id = ?",
                (senha_hash, alvo["id"]),
            )
            nome, alvo_id = alvo["usuario"], alvo["id"]
        _registrar(conn, "sistema", "-", "admin_redefinido",
                   f"Senha do administrador '{nome}' redefinida pelo computador local.", "local")
    if alvo_id is not None:
        encerrar_sessoes_usuario(alvo_id)
    seguranca().warning("Senha do administrador redefinida pelo computador local.")
    return nome, senha


def alterar_propria_senha(usuario: Usuario, atual: str, nova: str, confirmacao: str, ip: str) -> None:
    if nova != confirmacao:
        raise ValueError("A confirmação não confere com a nova senha.")
    validar_senha(nova, usuario.usuario)
    if nova == atual:
        raise ValueError("A nova senha deve ser diferente da atual.")
    if nova == SENHA_PADRAO:
        raise ValueError("Escolha uma senha diferente da senha padrão.")
    espera = _falhas_usuario.segundos_bloqueado(f"senha:{usuario.id}")
    if espera:
        raise LoginBloqueado(espera)
    # O hash (lento) é conferido fora da transação para não travar o banco.
    with db.conexao() as conn:
        linha = conn.execute(
            "SELECT senha_hash FROM usuarios WHERE id = ?", (usuario.id,)
        ).fetchone()
    if linha is None:
        raise ValueError("Conta não encontrada.")
    if len(atual) > SENHA_MAX or not verificar_senha(atual, linha["senha_hash"]):
        if _falhas_usuario.falha(f"senha:{usuario.id}"):
            seguranca().warning("Bloqueio de troca de senha: usuario=%s ip=%s",
                                seguro(usuario.usuario), seguro(ip))
        raise ValueError("Senha atual incorreta.")
    _falhas_usuario.sucesso(f"senha:{usuario.id}")
    novo_hash = hash_senha(nova)
    with _escrita() as conn:
        conn.execute(
            "UPDATE usuarios SET senha_hash = ?, trocar_senha = 0 WHERE id = ?",
            (novo_hash, usuario.id),
        )
        _registrar(conn, usuario.usuario, usuario.perfil, "senha_alterada",
                   "Alterou a própria senha.", ip)


# ------------------------------------------------------------ autenticação

_falhas_ip = BloqueioPorFalhas(MAX_FALHAS, JANELA_FALHAS)
_falhas_usuario = BloqueioPorFalhas(MAX_FALHAS_USUARIO, JANELA_FALHAS_USUARIO)


def reiniciar_bloqueios() -> None:
    _falhas_ip.limpar()
    _falhas_usuario.limpar()


def autenticar(nome: str, senha: str, ip: str) -> Usuario | None:
    espera = _falhas_ip.segundos_bloqueado(ip)
    if espera:
        raise LoginBloqueado(espera)
    nome = (nome or "").strip().lower()[:64]
    if nome:
        espera = _falhas_usuario.segundos_bloqueado(nome)
        if espera:
            seguranca().warning("Login recusado (conta bloqueada): usuario=%s ip=%s", seguro(nome), seguro(ip))
            raise LoginBloqueado(espera)

    # 1) leitura rápida; 2) hash lento SEM segurar o banco; 3) escrita rápida.
    with db.conexao() as conn:
        linha = conn.execute(
            "SELECT id, usuario, perfil, senha_hash, trocar_senha FROM usuarios WHERE usuario = ?",
            (nome,),
        ).fetchone()
    armazenado = linha["senha_hash"] if linha else _hash_falso()
    senha_ok = len(senha) <= SENHA_MAX and verificar_senha(senha, armazenado)

    if linha is not None and senha_ok:
        novo_hash = hash_senha(senha) if precisa_rehash(armazenado) else ""
        with _escrita() as conn:
            if novo_hash:
                # só troca se ninguém alterou a senha nesse meio tempo
                conn.execute(
                    "UPDATE usuarios SET senha_hash = ? WHERE id = ? AND senha_hash = ?",
                    (novo_hash, linha["id"], armazenado),
                )
            _registrar(conn, linha["usuario"], linha["perfil"], "login",
                       "Confirmou a identidade no computador do aplicativo."
                       if ip == "local" else "Entrou no acesso remoto.", ip)
        _falhas_ip.sucesso(ip)
        _falhas_usuario.sucesso(nome)
        return _linha_para_usuario(linha)

    with _escrita() as conn:
        _registrar(conn, nome, "-", "login_falhou", "Usuário ou senha incorretos.", ip)
    seguranca().warning("Falha de login: usuario=%s ip=%s", seguro(nome), seguro(ip))
    bloqueou_ip = _falhas_ip.falha(ip)
    bloqueou_usuario = _falhas_usuario.falha(nome) if nome else False
    if bloqueou_ip or bloqueou_usuario:
        alvo = "IP" if bloqueou_ip else "conta"
        seguranca().error("Bloqueio por excesso de tentativas (%s): usuario=%s ip=%s",
                          alvo, seguro(nome), seguro(ip))
        registrar_evento("bloqueio_login",
                         f"Bloqueio temporário ({alvo}) por excesso de falhas de login.",
                         usuario=nome or "(vazio)", ip=ip)
    return None


def registrar_saida(usuario: Usuario, ip: str) -> None:
    with _escrita() as conn:
        _registrar(conn, usuario.usuario, usuario.perfil, "logout",
                   "Saiu do acesso remoto.", ip)


# ------------------------------------------------------------------ sessões

@dataclass
class _Sessao:
    usuario_id: int
    csrf: str
    ip: str
    criada: float
    ultimo_uso: float


_sessoes: dict[str, _Sessao] = {}
_trava_sessoes = threading.Lock()


def _expirada(sessao: _Sessao, agora: float) -> bool:
    return agora - sessao.criada > DURACAO_SESSAO or agora - sessao.ultimo_uso > OCIOSIDADE_SESSAO


def iniciar_sessao(usuario: Usuario, ip: str = "") -> tuple[str, str]:
    token = secrets.token_urlsafe(32)
    csrf = secrets.token_urlsafe(24)
    agora = time.monotonic()
    with _trava_sessoes:
        for chave in [k for k, s in _sessoes.items() if _expirada(s, agora)]:
            del _sessoes[chave]
        do_usuario = sorted(
            (k for k, s in _sessoes.items() if s.usuario_id == usuario.id),
            key=lambda k: _sessoes[k].ultimo_uso,
        )
        for velha in do_usuario[: max(0, len(do_usuario) - MAX_SESSOES_USUARIO + 1)]:
            del _sessoes[velha]
        _sessoes[token] = _Sessao(usuario.id, csrf, ip, agora, agora)
    return token, csrf


def obter_sessao(token: str, ip: str = "") -> tuple[Usuario, str] | None:
    """Devolve (usuário atual, token CSRF) ou None. Perfil é relido do banco a cada acesso."""
    if not token:
        return None
    agora = time.monotonic()
    with _trava_sessoes:
        sessao = _sessoes.get(token)
        if sessao is None:
            return None
        if _expirada(sessao, agora):
            del _sessoes[token]
            return None
        if ip and sessao.ip and ip != sessao.ip:
            # cookie usado de outro endereço: trata como roubo e derruba a sessão
            del _sessoes[token]
            seguranca().warning("Sessão derrubada: cookie usado de outro IP (%s != %s).",
                                seguro(sessao.ip), seguro(ip))
            return None
        sessao.ultimo_uso = agora
        usuario_id, csrf = sessao.usuario_id, sessao.csrf
    usuario = obter_usuario(usuario_id)
    if usuario is None:
        encerrar_sessao(token)
        return None
    return usuario, csrf


def encerrar_sessao(token: str) -> None:
    with _trava_sessoes:
        _sessoes.pop(token, None)


def encerrar_sessoes_usuario(usuario_id: int, manter: str = "") -> None:
    with _trava_sessoes:
        for chave in [k for k, s in _sessoes.items() if s.usuario_id == usuario_id and k != manter]:
            del _sessoes[chave]


def contar_sessoes() -> int:
    agora = time.monotonic()
    with _trava_sessoes:
        return sum(1 for s in _sessoes.values() if not _expirada(s, agora))


def limpar_sessoes() -> None:
    with _trava_sessoes:
        _sessoes.clear()


# ------------------------------------------------------------------ produtos

def obter_produto(registro_id: int) -> sqlite3.Row | None:
    with db.conexao() as conn:
        return conn.execute(
            """
            SELECT id, ean, descricao, colaborador, quantidade, data_hora
            FROM etiquetas WHERE id = ?
            """,
            (registro_id,),
        ).fetchone()


def validar_produto(ean: str, descricao: str, ean_atual: str | None = None) -> tuple[str, str]:
    ean = (ean or "").strip()
    # Um EAN antigo e fora do padrão pode ser mantido ao corrigir só o nome.
    if ean and ean != ean_atual and not ean_valido(ean):
        raise ValueError(MENSAGEM_EAN_INVALIDO)
    if any(not c.isprintable() for c in ean) or len(ean) > 64:
        raise ValueError("EAN contém caracteres inválidos.")
    descricao = validacao.validar_descricao(descricao)
    return ean, descricao


def editar_produto(ator: Usuario, registro_id: int, ean: str, descricao: str, ip: str) -> bool:
    """Edita somente EAN e nome. Retorna False se nada mudou."""
    exigir(ator, "editar")
    with _escrita() as conn:
        atual = conn.execute(
            "SELECT id, ean, descricao, data_hora FROM etiquetas WHERE id = ?", (registro_id,)
        ).fetchone()
        if atual is None:
            raise ValueError("Registro não encontrado (pode ter sido removido).")
        ean, descricao = validar_produto(ean, descricao, atual["ean"])
        mudancas = []
        if atual["ean"] != ean:
            mudancas.append(f"EAN '{atual['ean']}' → '{ean}'")
        if atual["descricao"] != descricao:
            mudancas.append(f"Nome '{atual['descricao']}' → '{descricao}'")
        if not mudancas:
            return False
        conn.execute(
            "UPDATE etiquetas SET ean = ?, descricao = ? WHERE id = ?",
            (ean, descricao, registro_id),
        )
        _registrar(conn, ator.usuario, ator.perfil, "produto_editado",
                   f"Mercadoria nº {registro_id} ({atual['data_hora']}): " + "; ".join(mudancas) + ".", ip)
    return True
