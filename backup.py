"""Backup e restauração do banco de dados.

- Cópia CONSISTENTE mesmo com o programa em uso (API de backup online do SQLite);
- conferência `integrity_check` da cópia antes de guardá-la;
- compressão (zlib) + criptografia AES-256-GCM: o arquivo `.ehb` também
  detecta adulteração (tag de autenticação) e não expõe documentos de
  visitantes se for copiado para pendrive/nuvem;
- a chave é gerada uma vez e guardada com o DPAPI do Windows (`segredos.py`).
  Para recuperar em OUTRO computador existe o "código de recuperação":
  guarde-o fora do computador (cofre, gerente);
- rotação: mantém só os N backups mais recentes de cada tipo;
- agendador em segundo plano (padrão: 1 backup a cada 24 h).

Formato do arquivo: b"EHB1" | nonce(12) | AES-GCM(zlib(banco.sqlite)), AAD = b"EHB1".
"""

from __future__ import annotations

import base64
import hmac
import os
import re
import secrets
import sqlite3
import threading
import zlib
from datetime import datetime
from pathlib import Path

import db
import migracoes
import paths
import segredos
from limitador import BloqueioPorFalhas
from logs import log

MAGIC = b"EHB1"
EXTENSAO = ".ehb"
NOME_CHAVE = "backup"
MANTER_PADRAO = 14
MANTER_OUTROS = 5
TAMANHO_MAXIMO = 1_500_000_000  # 1,5 GB: acima disso não cabe na memória com folga

_trava = threading.Lock()
_NOME_RE = re.compile(r"^etiquetas-(\d{8}-\d{6})-([a-z0-9-]+)(?:_(\d+))?\.ehb$")


class ErroBackup(Exception):
    pass


def _aesgcm(chave: bytes):
    try:
        from cryptography.hazmat.primitives.ciphers.aead import AESGCM
    except ImportError as exc:  # pragma: no cover
        raise ErroBackup(
            "A biblioteca 'cryptography' não está instalada; não é possível cifrar o backup."
        ) from exc
    return AESGCM(chave)


# ------------------------------------------------------------------ chave

def obter_chave() -> tuple[bytes, bool]:
    """(chave, criada_agora)."""
    return segredos.obter_segredo(NOME_CHAVE, 32)


def formatar_codigo(chave: bytes) -> str:
    bruto = base64.b32encode(chave).decode("ascii").rstrip("=")
    return "-".join(bruto[i:i + 4] for i in range(0, len(bruto), 4))


def codigo_recuperacao() -> str:
    return formatar_codigo(obter_chave()[0])


_tentativas_codigo = BloqueioPorFalhas(5, 300.0)


def codigo_confere(texto: str) -> bool:
    """Confere um código digitado com o da máquina (tempo constante, com limite de tentativas)."""
    espera = _tentativas_codigo.segundos_bloqueado("codigo")
    if espera:
        raise ErroBackup(f"Muitas tentativas. Aguarde {espera} s e tente de novo.")
    try:
        ok = hmac.compare_digest(interpretar_codigo(texto), obter_chave()[0])
    except ErroBackup:
        ok = False
    if ok:
        _tentativas_codigo.sucesso("codigo")
    else:
        _tentativas_codigo.falha("codigo")
    return ok


def interpretar_codigo(texto: str) -> bytes:
    limpo = re.sub(r"[\s-]", "", texto or "").upper()
    try:
        chave = base64.b32decode(limpo + "=" * (-len(limpo) % 8))
    except (ValueError, TypeError):
        raise ErroBackup("Código de recuperação inválido.") from None
    if len(chave) != 32:
        raise ErroBackup("Código de recuperação inválido.")
    return chave


# ----------------------------------------------------------------- criar

def _motivo_seguro(motivo: str) -> str:
    return re.sub(r"[^a-z0-9-]", "", (motivo or "automatico").lower().replace("_", "-")) or "automatico"


def _copia_consistente(destino: Path) -> None:
    origem = db._abrir()
    try:
        copia = sqlite3.connect(str(destino))
        try:
            origem.backup(copia)
            resultado = copia.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            copia.close()
    finally:
        origem.close()
    if resultado != "ok":
        raise ErroBackup(f"A cópia do banco não passou na verificação de integridade: {resultado}")


def criar_backup(motivo: str = "automatico", manter: int | None = None) -> Path:
    """Cria um backup cifrado em backups/ e devolve o caminho."""
    motivo = _motivo_seguro(motivo)
    with _trava:
        paths.BACKUP_DIR.mkdir(parents=True, exist_ok=True)
        temporario = paths.BACKUP_DIR / f".tmp-{secrets.token_hex(6)}.db"
        try:
            _copia_consistente(temporario)
            tamanho = temporario.stat().st_size
            if tamanho > TAMANHO_MAXIMO:
                raise ErroBackup("O banco é grande demais para o backup em memória.")
            chave, _ = obter_chave()
            nonce = secrets.token_bytes(12)
            cifrado = _aesgcm(chave).encrypt(nonce, zlib.compress(temporario.read_bytes(), 6), MAGIC)
        finally:
            temporario.unlink(missing_ok=True)
        carimbo = datetime.now().strftime("%Y%m%d-%H%M%S")
        # Mesmo segundo: usa um número maior que todos os já existentes (mesmo que algum
        # tenha sido apagado), para o backup novo nunca parecer mais antigo que os outros.
        usados = [
            int(achado.group(3) or 1)
            for achado in (_NOME_RE.match(a.name) for a in paths.BACKUP_DIR.iterdir())
            if achado and achado.group(1) == carimbo and achado.group(2) == motivo
        ]
        indice = max(usados, default=0) + 1
        sufixo = "" if indice == 1 else f"_{indice}"
        final = paths.BACKUP_DIR / f"etiquetas-{carimbo}-{motivo}{sufixo}{EXTENSAO}"
        parcial = final.with_name(final.name + ".parcial")
        parcial.write_bytes(MAGIC + nonce + cifrado)
        os.replace(parcial, final)
        _rotacionar(manter, proteger=final)
    log("backup").info("Backup criado: %s (%d bytes)", final.name, final.stat().st_size)
    return final


def _listar() -> list[tuple[Path, datetime, str]]:
    """(arquivo, quando, motivo) em ordem cronológica."""
    itens = []
    if not paths.BACKUP_DIR.is_dir():
        return itens
    for arquivo in paths.BACKUP_DIR.iterdir():
        achado = _NOME_RE.match(arquivo.name)
        if achado:
            quando = datetime.strptime(achado.group(1), "%Y%m%d-%H%M%S")
            indice = int(achado.group(3) or 1)  # desempate numérico (_2, _10...) no mesmo segundo
            itens.append((arquivo, quando, achado.group(2), indice))
    itens.sort(key=lambda i: (i[1], i[3]))
    return [(arquivo, quando, motivo) for arquivo, quando, motivo, _ in itens]


def listar_backups() -> list[Path]:
    return [i[0] for i in _listar()]


def _rotacionar(manter: int | None, proteger: Path | None = None) -> None:
    manter = max(1, int(manter or MANTER_PADRAO))
    grupos: dict[str, list[Path]] = {}
    for arquivo, _, motivo in _listar():
        grupos.setdefault(motivo, []).append(arquivo)
    for motivo, arquivos in grupos.items():
        limite = manter if motivo == "automatico" else MANTER_OUTROS
        for velho in arquivos[:-limite]:
            if velho == proteger:
                continue  # o backup recém-criado nunca é apagado
            try:
                velho.unlink()
                log("backup").info("Backup antigo removido: %s", velho.name)
            except OSError:
                log("backup").warning("Não foi possível remover o backup antigo %s", velho.name)
    for resto in paths.BACKUP_DIR.glob(".tmp-*"):
        resto.unlink(missing_ok=True)


def estado() -> dict:
    itens = _listar()
    ultimo = itens[-1] if itens else None
    return {
        "quantidade": len(itens),
        "ultimo": ultimo[1] if ultimo else None,
        "ultimo_arquivo": ultimo[0] if ultimo else None,
        "pasta": paths.BACKUP_DIR,
    }


def idade_ultimo_horas() -> float | None:
    ultimo = estado()["ultimo"]
    return None if ultimo is None else (datetime.now() - ultimo).total_seconds() / 3600


# -------------------------------------------------------------- restaurar

def ler_backup(arquivo: Path, chave: bytes) -> bytes:
    """Decifra e descomprime. Levanta ErroBackup se a chave estiver errada ou o arquivo alterado."""
    try:
        bruto = Path(arquivo).read_bytes()
    except OSError as exc:
        raise ErroBackup(f"Não foi possível ler o arquivo: {exc}") from exc
    if len(bruto) < 4 + 12 + 16 or bruto[:4] != MAGIC:
        raise ErroBackup("Este arquivo não é um backup válido do programa.")
    nonce, cifrado = bruto[4:16], bruto[16:]
    try:
        comprimido = _aesgcm(chave).decrypt(nonce, cifrado, MAGIC)
    except ErroBackup:
        raise
    except Exception:
        raise ErroBackup(
            "Não foi possível abrir o backup: código de recuperação incorreto ou arquivo alterado."
        ) from None
    try:
        # Limite de expansão: protege a memória contra "bomba" de compressão.
        descompressor = zlib.decompressobj()
        dados = descompressor.decompress(comprimido, TAMANHO_MAXIMO + 1)
    except zlib.error:
        raise ErroBackup("O backup está corrompido.") from None
    if len(dados) > TAMANHO_MAXIMO or descompressor.unconsumed_tail:
        raise ErroBackup("O backup é grande demais.")
    return dados


def _validar_sqlite(caminho: Path) -> int:
    """Confere integridade e versão do esquema. Devolve o user_version."""
    try:
        conn = sqlite3.connect(f"file:{caminho.as_posix()}?mode=ro", uri=True)
        try:
            resultado = conn.execute("PRAGMA integrity_check").fetchone()[0]
            versao = int(conn.execute("PRAGMA user_version").fetchone()[0])
            tabelas = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            conn.close()
    except sqlite3.Error as exc:
        raise ErroBackup(f"O conteúdo do backup não é um banco válido: {exc}") from exc
    if resultado != "ok":
        raise ErroBackup(f"O backup não passou na verificação de integridade: {resultado}")
    if not {"etiquetas", "visitantes", "usuarios", "log_alteracoes"} <= tabelas:
        raise ErroBackup("O backup não contém as tabelas esperadas.")
    if versao > migracoes.VERSAO_ESQUEMA:
        raise ErroBackup("Este backup é de uma versão mais nova do programa.")
    return versao


def restaurar(arquivo: Path, codigo: str | None = None) -> Path:
    """Substitui o banco atual pelo conteúdo do backup. FECHE o programa antes.

    O banco atual não é apagado: vira `etiquetas.db.antes-restauracao-<data>`.
    Devolve o caminho dessa cópia de segurança.
    """
    with _trava:
        if codigo:
            chave = interpretar_codigo(codigo)
        else:
            chave = obter_chave()[0]
        dados = ler_backup(Path(arquivo), chave)
        destino = db.arquivo_banco()
        temporario = destino.with_name(destino.name + ".restaurando")
        try:
            temporario.write_bytes(dados)
            _validar_sqlite(temporario)
            # grava o que estiver pendente no WAL antes de guardar o banco atual
            if destino.exists():
                try:
                    conn = sqlite3.connect(str(destino))
                    try:
                        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                    finally:
                        conn.close()
                except sqlite3.Error:
                    pass
            seguranca = destino.with_name(
                f"{destino.name}.antes-restauracao-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            )
            if destino.exists():
                os.replace(destino, seguranca)
            for sufixo in ("-wal", "-shm"):
                destino.with_name(destino.name + sufixo).unlink(missing_ok=True)
            os.replace(temporario, destino)
        finally:
            temporario.unlink(missing_ok=True)
        if codigo:
            # A chave informada passa a ser a do programa: os próximos backups usam a mesma.
            segredos.substituir_segredo(NOME_CHAVE, chave)
        db.usar_banco(destino)
    log("backup").warning("Banco restaurado a partir de %s", Path(arquivo).name)
    return seguranca


# -------------------------------------------------------------- agendador

class Agendador(threading.Thread):
    """Faz um backup ao abrir (se o último tiver mais de `intervalo_h`) e depois a cada intervalo."""

    def __init__(self, intervalo_h: float = 24.0, manter: int = MANTER_PADRAO) -> None:
        super().__init__(name="backup-agendado", daemon=True)
        self.intervalo_h = intervalo_h
        self.manter = manter
        self._parar = threading.Event()

    def run(self) -> None:
        while not self._parar.is_set():
            try:
                idade = idade_ultimo_horas()
                if idade is None or idade >= self.intervalo_h:
                    criar_backup("automatico", self.manter)
            except Exception:
                log("backup").exception("Falha no backup automático.")
            self._parar.wait(900)  # confere a cada 15 min

    def parar(self) -> None:
        self._parar.set()


_agendador: Agendador | None = None


def iniciar_agendador(intervalo_h: float = 24.0, manter: int = MANTER_PADRAO) -> None:
    global _agendador
    parar_agendador()
    _agendador = Agendador(intervalo_h, manter)
    _agendador.start()


def parar_agendador() -> None:
    global _agendador
    if _agendador is not None:
        _agendador.parar()
        _agendador = None
