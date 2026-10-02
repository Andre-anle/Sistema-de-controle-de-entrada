
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import urlparse

from paths import APP_DIR, PASTAS_DO_USUARIO, executando_empacotado
from versao import REPOSITORIO_GITHUB, VERSAO

NOME_EXE = "EtiquetasHortifruti.exe"
PREFIXO_PACOTE = "EtiquetasHortifruti"
NOME_MANIFESTO = "manifest.json"
PASTA_TRABALHO = APP_DIR / "_atualizacao"
API_GITHUB = "https://api.github.com"
TIMEOUT = 25
BLOCO = 256 * 1024
BLOCO_REMOTO = 4 * 1024 * 1024
LIMITE_PRECARGA = 8 * 1024 * 1024
LIMITE_MANIFESTO = 20 * 1024 * 1024
ESPERA_SCRIPT = 20.0
# Proteção contra "zip bomb" / pacote absurdo.
LIMITE_DESCOMPACTADO = 3 * 1024 ** 3
LIMITE_ARQUIVOS = 50_000

# Arquivos criados pelo uso do programa: nunca são sobrescritos nem removidos.
PRESERVAR = (
    "etiquetas.db", "etiquetas.db-wal", "etiquetas.db-shm", "etiquetas.db.bak",
    "etiquetas_dados.db", "etiquetas_dados.db-wal", "etiquetas_dados.db-shm",
    "config.json", "erro.log", "atualizacao.log", "atualizacao_ok.flag",
)

_HOSTS_GITHUB = ("github.com", "githubusercontent.com")
_REPO_RE = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")

Progresso = Callable[[float, str], None]


class ErroAtualizacao(Exception):
    pass


@dataclass(frozen=True)
class Atualizacao:
    versao: str
    tag: str
    notas: str
    nome_pacote: str
    url_pacote: str
    tamanho: int
    sha256: str
    url_sha256: str
    url_manifesto: str = ""
    sha256_manifesto: str = ""
    nome_manifesto: str = ""
    # True quando as notas da Release trazem a marca [auto]: o programa se atualiza
    # sozinho (após uma contagem regressiva), sem esperar o clique.
    automatica: bool = False


# ------------------------------------------------------------------ versões

def _numeros(texto: str) -> tuple[int, ...]:
    achados = re.findall(r"\d+", texto or "")[:4]
    if not achados:
        raise ErroAtualizacao(f"Versão inválida: {texto!r}.")
    numeros = tuple(int(n) for n in achados)
    return numeros + (0,) * (4 - len(numeros))


def versao_mais_nova(remota: str, atual: str = VERSAO) -> bool:
    return _numeros(remota) > _numeros(atual)


def versao_em_uso(aplicada: str = "") -> str:
    """Versão deste programa: a embutida no .exe ou, se maior, a última que o atualizador instalou."""
    try:
        if aplicada and _numeros(aplicada) > _numeros(VERSAO):
            return aplicada.strip()
    except ErroAtualizacao:
        pass
    return VERSAO


_MARCA_AUTO_RE = re.compile(r"\[\s*auto(?:m[aá]tic[ao])?\s*\]", re.IGNORECASE)


def notas_tem_marca_auto(notas: str) -> bool:
    return bool(_MARCA_AUTO_RE.search(notas or ""))


def limpar_marca_auto(notas: str) -> str:
    return _MARCA_AUTO_RE.sub("", notas or "").strip()


def pode_atualizar() -> bool:
    """A troca de arquivos só faz sentido no programa compilado."""
    return executando_empacotado()


# --------------------------------------------------------------- repositório

def normalizar_repositorio(texto: str) -> str:
    """Aceita 'usuario/repositorio' ou a URL do repositório no GitHub."""
    bruto = (texto or "").strip()
    if not bruto:
        raise ErroAtualizacao(
            "Informe o repositório do GitHub (usuario/repositorio) em Configurações."
        )
    if "://" in bruto:
        partes = urlparse(bruto)
        if (partes.hostname or "").lower() not in {"github.com", "www.github.com"}:
            raise ErroAtualizacao("O repositório precisa ser do github.com.")
        bruto = partes.path.strip("/")
    bruto = bruto.removesuffix(".git").strip("/")
    bruto = "/".join(bruto.split("/")[:2])
    if not _REPO_RE.fullmatch(bruto) or any(p in {".", ".."} for p in bruto.split("/")):
        raise ErroAtualizacao("Repositório inválido. Use o formato usuario/repositorio.")
    return bruto


# --------------------------------------------------------------------- rede

def _url_permitida(url: str) -> bool:
    partes = urlparse(url)
    host = (partes.hostname or "").lower()
    return partes.scheme == "https" and any(
        host == h or host.endswith("." + h) for h in _HOSTS_GITHUB
    )


class _RedirecionamentoSeguro(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _url_permitida(newurl):
            raise ErroAtualizacao("Download bloqueado: redirecionamento para fora do GitHub.")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _abrir(url: str, aceitar: str = "application/vnd.github+json",
           cabecalhos: dict[str, str] | None = None):
    if not _url_permitida(url):
        raise ErroAtualizacao("Endereço de atualização inválido (somente HTTPS do GitHub).")
    pedido = urllib.request.Request(
        url,
        headers={
            "Accept": aceitar,
            "User-Agent": f"EtiquetasHortifruti/{VERSAO}",
            "X-GitHub-Api-Version": "2022-11-28",
            **(cabecalhos or {}),
        },
    )
    opener = urllib.request.build_opener(_RedirecionamentoSeguro)
    try:
        return opener.open(pedido, timeout=TIMEOUT)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            raise ErroAtualizacao(
                "Nenhuma versão publicada foi encontrada (ou o repositório é privado)."
            ) from exc
        if exc.code in (403, 429):
            raise ErroAtualizacao(
                "O GitHub limitou as consultas por enquanto. Tente novamente mais tarde."
            ) from exc
        raise ErroAtualizacao(f"O GitHub respondeu com erro {exc.code}.") from exc
    except urllib.error.URLError as exc:
        raise ErroAtualizacao(
            "Não foi possível conectar ao GitHub. Verifique a internet."
        ) from exc
    except (TimeoutError, OSError) as exc:
        raise ErroAtualizacao("Não foi possível conectar ao GitHub (tempo esgotado).") from exc


def _json(url: str) -> dict:
    with _abrir(url) as resposta:
        try:
            return json.loads(resposta.read(2_000_000).decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ErroAtualizacao("Resposta inesperada do GitHub.") from exc


def _baixar_bytes(url: str, limite: int) -> bytes:
    with _abrir(url, "application/octet-stream") as resposta:
        dados = resposta.read(limite + 1)
    if len(dados) > limite:
        raise ErroAtualizacao("Arquivo maior que o esperado.")
    return dados


def _sha256_do_texto(texto: str, nome: str | None = None) -> str:
    """Lê um .sha256 no formato '<hash>  <arquivo>' (uma ou mais linhas).

    Com 'nome', procura a linha daquele arquivo; sem 'nome', aceita a primeira.
    """
    primeira = ""
    for linha in (texto or "").splitlines():
        achado = re.match(r"\s*([0-9a-fA-F]{64})\b\s*\*?(.*)$", linha)
        if not achado:
            continue
        hash_, arquivo = achado.group(1).lower(), achado.group(2).strip()
        primeira = primeira or hash_
        if nome and arquivo.lower() == nome.lower():
            return hash_
    return "" if nome else primeira


def buscar_atualizacao(repositorio: str, atual: str = VERSAO) -> Atualizacao | None:
    """Devolve a Release mais nova que a instalada, ou None se já está atualizado."""
    repo = normalizar_repositorio(repositorio)
    dados = _json(f"{API_GITHUB}/repos/{repo}/releases/latest")
    tag = str(dados.get("tag_name") or "")
    if not versao_mais_nova(tag, atual):
        return None
    versao = ".".join(str(n) for n in _numeros(tag)[:3])

    ativos = [a for a in dados.get("assets") or [] if isinstance(a, dict)]

    def achar(predicado) -> dict | None:
        return next((a for a in ativos if predicado(str(a.get("name", "")).lower())), None)

    prefixo = PREFIXO_PACOTE.lower()
    pacote = achar(lambda n: n.startswith(prefixo) and n.endswith(".zip"))
    if pacote is None:
        raise ErroAtualizacao(
            f"A versão {tag} não tem o pacote {PREFIXO_PACOTE}-<versão>.zip anexado."
        )
    nome = str(pacote["name"])
    arquivo_sha = achar(lambda n: n == nome.lower() + ".sha256")
    url_sha = str((arquivo_sha or {}).get("browser_download_url") or "")

    def digest(ativo: dict | None) -> str:
        texto = str((ativo or {}).get("digest") or "")
        return _sha256_do_texto(texto.split(":", 1)[1]) if texto.lower().startswith("sha256:") else ""

    sha = digest(pacote)
    manifesto = achar(lambda n: n.startswith(prefixo) and n.endswith(".manifest.json"))
    sha_manifesto = digest(manifesto)
    nome_manifesto = str((manifesto or {}).get("name") or "")
    if manifesto is not None and not sha_manifesto and url_sha:
        try:
            texto = _baixar_bytes(url_sha, 8192).decode("utf-8", "ignore")
            sha_manifesto = _sha256_do_texto(texto, nome_manifesto)
        except ErroAtualizacao:
            sha_manifesto = ""

    if not sha and not url_sha:
        raise ErroAtualizacao(
            f"A versão {tag} não tem o checksum (.sha256) do pacote; a atualização foi recusada."
        )
    return Atualizacao(
        versao=versao,
        tag=tag,
        notas=limpar_marca_auto(str(dados.get("body") or "")),
        nome_pacote=nome,
        url_pacote=str(pacote.get("browser_download_url") or ""),
        tamanho=int(pacote.get("size") or 0),
        sha256=sha,
        url_sha256=url_sha,
        url_manifesto=str((manifesto or {}).get("browser_download_url") or "") if sha_manifesto else "",
        sha256_manifesto=sha_manifesto,
        nome_manifesto=nome_manifesto,
        automatica=notas_tem_marca_auto(str(dados.get("body") or "")),
    )


# ------------------------------------------------------------- utilidades

def _hash_arquivo(caminho: Path) -> str:
    resumo = hashlib.sha256()
    with caminho.open("rb") as f:
        for bloco in iter(lambda: f.read(1024 * 1024), b""):
            resumo.update(bloco)
    return resumo.hexdigest()


def _caminho_seguro(rel: str) -> str | None:
    """Normaliza um caminho relativo do manifesto; None se for suspeito."""
    if not rel or "\\" in rel or ":" in rel:
        return None
    p = PurePosixPath(rel)
    if p.is_absolute() or any(parte in {"", ".", ".."} for parte in p.parts):
        return None
    return p.as_posix()


_PASTAS_PRESERVADAS = {p.lower() for p in PASTAS_DO_USUARIO}


def _preservado(rel: str) -> bool:
    """Dados do usuário (banco, config, backups, logs, segredos, certificados) nunca são tocados."""
    partes = rel.lower().split("/")
    if len(partes) > 1:
        return partes[0] in _PASTAS_PRESERVADAS
    return partes[0] in {n.lower() for n in PRESERVAR}


def _validar_manifesto(dados: object) -> dict[str, dict]:
    if not isinstance(dados, dict) or not isinstance(dados.get("arquivos"), dict):
        raise ErroAtualizacao("Manifesto da atualização inválido.")
    limpo: dict[str, dict] = {}
    for rel, meta in dados["arquivos"].items():
        seguro = _caminho_seguro(str(rel))
        sha = str((meta or {}).get("sha256", "")).lower() if isinstance(meta, dict) else ""
        tamanho = (meta or {}).get("tamanho") if isinstance(meta, dict) else None
        if seguro is None or not re.fullmatch(r"[0-9a-f]{64}", sha) or not isinstance(tamanho, int):
            raise ErroAtualizacao("Manifesto da atualização contém entradas inválidas.")
        if not _preservado(seguro):
            limpo[seguro] = {"sha256": sha, "tamanho": tamanho}
    return limpo


def _manifesto_local() -> dict[str, dict] | None:
    try:
        return _validar_manifesto(json.loads((APP_DIR / NOME_MANIFESTO).read_text(encoding="utf-8")))
    except (OSError, ValueError, ErroAtualizacao):
        return None


def _testar_escrita(pasta: Path) -> None:
    try:
        pasta.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=pasta, delete=True):
            pass
    except OSError as exc:
        raise ErroAtualizacao(
            f"Sem permissão para gravar em {pasta}. Instale o programa em uma pasta do seu "
            f"usuário (o instalador já faz isso). Detalhe: {exc}"
        ) from exc


def _mb(valor: float) -> str:
    return f"{valor / 1_048_576:.1f} MB"


# ----------------------------------------------------- download completo

def _baixar_arquivo(url: str, destino: Path, tamanho_esperado: int,
                    progresso: Progresso | None) -> str:
    """Baixa para 'destino' e devolve o SHA-256 calculado durante a transferência."""
    resumo = hashlib.sha256()
    total = tamanho_esperado
    baixado = 0
    ultimo_pct = -1
    with _abrir(url, "application/octet-stream") as resposta, destino.open("wb") as saida:
        cabecalho = resposta.headers.get("Content-Length")
        if not total and cabecalho and cabecalho.isdigit():
            total = int(cabecalho)
        while True:
            bloco = resposta.read(BLOCO)
            if not bloco:
                break
            saida.write(bloco)
            resumo.update(bloco)
            baixado += len(bloco)
            if progresso and total:
                pct = int(baixado * 100 / total)
                if pct != ultimo_pct:
                    ultimo_pct = pct
                    progresso(min(baixado / total, 1.0),
                              f"Baixando o pacote completo... {_mb(baixado)} de {_mb(total)}")
    if total and baixado != total:
        raise ErroAtualizacao("O download veio incompleto. Tente novamente.")
    return resumo.hexdigest()


def _extrair(zip_path: Path, destino: Path) -> None:
    raiz = destino.resolve()
    try:
        with zipfile.ZipFile(zip_path) as pacote:
            membros = pacote.infolist()
            if len(membros) > LIMITE_ARQUIVOS:
                raise ErroAtualizacao("Pacote inválido (arquivos demais).")
            if sum(m.file_size for m in membros) > LIMITE_DESCOMPACTADO:
                raise ErroAtualizacao("Pacote inválido (tamanho descompactado excessivo).")
            for membro in membros:
                alvo = (destino / membro.filename).resolve()
                if alvo != raiz and raiz not in alvo.parents:
                    raise ErroAtualizacao("Pacote inválido (caminho fora da pasta).")
            pacote.extractall(destino)
    except zipfile.BadZipFile as exc:
        raise ErroAtualizacao("O pacote baixado está corrompido.") from exc


def _raiz_do_pacote(pasta: Path) -> Path:
    if (pasta / NOME_EXE).is_file():
        return pasta
    subpastas = [p for p in pasta.iterdir() if p.is_dir()]
    if len(subpastas) == 1 and (subpastas[0] / NOME_EXE).is_file():
        return subpastas[0]
    raise ErroAtualizacao(f"O pacote não contém o {NOME_EXE}.")


def _preparar_completo(info: Atualizacao, novo: Path, progresso: Progresso | None) -> Path:
    esperado = info.sha256
    if not esperado:
        texto = _baixar_bytes(info.url_sha256, 8192).decode("utf-8", "ignore")
        esperado = _sha256_do_texto(texto, info.nome_pacote) or _sha256_do_texto(texto)
    if not esperado:
        raise ErroAtualizacao("Não foi possível ler o checksum da atualização.")

    zip_path = PASTA_TRABALHO / "pacote.zip"
    calculado = _baixar_arquivo(info.url_pacote, zip_path, info.tamanho, progresso)
    if calculado != esperado:
        raise ErroAtualizacao(
            "A verificação de integridade (SHA-256) falhou. A atualização foi descartada."
        )
    if progresso:
        progresso(1.0, "Extraindo arquivos...")
    novo.mkdir(parents=True, exist_ok=True)
    _extrair(zip_path, novo)
    zip_path.unlink(missing_ok=True)
    return _raiz_do_pacote(novo)


# ------------------------------------------------- download incremental

class _ArquivoRemoto:
    """Arquivo somente-leitura sobre um .zip remoto, lido por HTTP Range."""

    def __init__(self, url: str, tamanho: int) -> None:
        self.url = url
        self.tamanho = tamanho
        self._pos = 0
        self._ini = 0
        self._bloco = b""
        self.bytes_baixados = 0

    def _requisitar(self, ini: int, fim: int) -> bytes:
        fim = min(fim, self.tamanho - 1)
        esperado = fim - ini + 1
        ultimo: Exception | None = None
        for tentativa in range(3):
            try:
                with _abrir(self.url, "application/octet-stream",
                            {"Range": f"bytes={ini}-{fim}"}) as resposta:
                    if resposta.status != 206:
                        raise ErroAtualizacao("O servidor não aceita download parcial.")
                    dados = resposta.read(esperado + 1)
                if len(dados) != esperado:
                    raise ErroAtualizacao("Trecho baixado incompleto.")
                self.bytes_baixados += len(dados)
                return dados
            except ErroAtualizacao as exc:
                if "não aceita" in str(exc):
                    raise
                ultimo = exc
            except OSError as exc:
                ultimo = exc
            time.sleep(1 + tentativa)
        raise ErroAtualizacao(f"Falha ao baixar parte do pacote: {ultimo}")

    def precarregar(self, ini: int, fim: int) -> None:
        """Busca de uma vez o trecho [ini, fim] (limitado) para evitar muitas requisições."""
        fim = min(fim, ini + LIMITE_PRECARGA - 1, self.tamanho - 1)
        if ini < self.tamanho:
            self._bloco = self._requisitar(ini, fim)
            self._ini = ini

    def seekable(self) -> bool:
        return True

    def tell(self) -> int:
        return self._pos

    def seek(self, deslocamento: int, origem: int = 0) -> int:
        if origem == 0:
            self._pos = deslocamento
        elif origem == 1:
            self._pos += deslocamento
        else:
            self._pos = self.tamanho + deslocamento
        self._pos = max(0, self._pos)
        return self._pos

    def read(self, n: int = -1) -> bytes:
        restante = self.tamanho - self._pos
        n = restante if n is None or n < 0 else min(n, restante)
        saida = bytearray()
        while n > 0:
            if not (self._ini <= self._pos < self._ini + len(self._bloco)):
                ini = self._pos
                self._bloco = self._requisitar(ini, ini + BLOCO_REMOTO - 1)
                self._ini = ini
            deslocamento = self._pos - self._ini
            parte = self._bloco[deslocamento:deslocamento + n]
            saida += parte
            self._pos += len(parte)
            n -= len(parte)
        return bytes(saida)

    def close(self) -> None:
        self._bloco = b""


def _arquivos_a_baixar(manifesto: dict[str, dict], progresso: Progresso | None) -> list[str]:
    """Compara o manifesto novo com os arquivos locais e lista o que precisa vir."""
    precisa: list[str] = []
    total = len(manifesto)
    for i, (rel, meta) in enumerate(sorted(manifesto.items()), start=1):
        local = APP_DIR / rel
        try:
            igual = local.is_file() and local.stat().st_size == meta["tamanho"] and (
                _hash_arquivo(local) == meta["sha256"]
            )
        except OSError:
            igual = False
        if not igual:
            precisa.append(rel)
        if progresso and (i % 25 == 0 or i == total):
            progresso(i / total, f"Comparando arquivos... {i} de {total}")
    return precisa


def _preparar_incremental(info: Atualizacao, novo: Path, progresso: Progresso | None) -> dict[str, dict]:
    """Baixa só os arquivos alterados. Devolve o manifesto novo; levanta erro se não der."""
    if not (info.url_manifesto and info.sha256_manifesto and info.tamanho):
        raise ErroAtualizacao("A Release não tem manifesto para atualização parcial.")
    bruto = _baixar_bytes(info.url_manifesto, LIMITE_MANIFESTO)
    if hashlib.sha256(bruto).hexdigest() != info.sha256_manifesto:
        raise ErroAtualizacao("O manifesto da atualização não confere (SHA-256).")
    try:
        manifesto = _validar_manifesto(json.loads(bruto.decode("utf-8")))
    except (ValueError, UnicodeDecodeError) as exc:
        raise ErroAtualizacao("Manifesto da atualização inválido.") from exc
    if NOME_EXE not in manifesto:
        raise ErroAtualizacao(f"O manifesto não lista o {NOME_EXE}.")

    precisa = _arquivos_a_baixar(manifesto, progresso)
    novo.mkdir(parents=True, exist_ok=True)
    total_bytes = sum(manifesto[r]["tamanho"] for r in precisa) or 1
    feito = 0
    remoto = _ArquivoRemoto(info.url_pacote, info.tamanho)
    try:
        with zipfile.ZipFile(remoto) as pacote:  # type: ignore[arg-type]
            membros = {}
            for m in pacote.infolist():
                seguro = _caminho_seguro(m.filename)
                if seguro:
                    membros[seguro] = m
            for rel in sorted(precisa, key=lambda r: membros[r].header_offset if r in membros else 0):
                membro = membros.get(rel)
                if membro is None:
                    raise ErroAtualizacao(f"{rel} não está no pacote.")
                destino = (novo / rel).resolve()
                if novo.resolve() not in destino.parents:
                    raise ErroAtualizacao("Pacote inválido (caminho fora da pasta).")
                esperado = manifesto[rel]["tamanho"]
                if membro.file_size != esperado or esperado > LIMITE_DESCOMPACTADO:
                    raise ErroAtualizacao(f"Tamanho inesperado para {rel} no pacote.")
                destino.parent.mkdir(parents=True, exist_ok=True)
                remoto.precarregar(
                    membro.header_offset,
                    membro.header_offset + 30 + len(membro.filename.encode("utf-8"))
                    + membro.compress_size + 1024,
                )
                resumo = hashlib.sha256()
                escrito = 0
                with pacote.open(membro) as origem, destino.open("wb") as saida:
                    while True:
                        bloco = origem.read(BLOCO)
                        if not bloco:
                            break
                        escrito += len(bloco)
                        if escrito > esperado:
                            raise ErroAtualizacao(f"{rel} excede o tamanho declarado.")
                        saida.write(bloco)
                        resumo.update(bloco)
                        feito += len(bloco)
                        if progresso:
                            progresso(
                                min(feito / total_bytes, 1.0),
                                f"Baixando somente o que mudou... {_mb(feito)} de {_mb(total_bytes)}",
                            )
                if resumo.hexdigest() != manifesto[rel]["sha256"]:
                    raise ErroAtualizacao(f"Verificação de integridade falhou em {rel}.")
    except zipfile.BadZipFile as exc:
        raise ErroAtualizacao("Não foi possível ler o índice do pacote remoto.") from exc
    finally:
        remoto.close()
    # O manifesto novo passa a valer na pasta instalada.
    (novo / NOME_MANIFESTO).write_bytes(bruto)
    return manifesto


# ------------------------------------------------------------------ plano

def _montar_plano(raiz: Path, manifesto_novo: dict[str, dict] | None) -> None:
    copiar = []
    for arquivo in sorted(raiz.rglob("*")):
        if not arquivo.is_file():
            continue
        rel = arquivo.relative_to(raiz).as_posix()
        if _preservado(rel):
            continue
        copiar.append({"r": rel, "h": _hash_arquivo(arquivo)})
    if not any(item["r"] == NOME_EXE for item in copiar) and not (APP_DIR / NOME_EXE).is_file():
        raise ErroAtualizacao(f"A atualização não contém o {NOME_EXE}.")

    remover: list[str] = []
    antigo = _manifesto_local()
    if antigo is not None and manifesto_novo is not None:
        remover = sorted(r for r in antigo if r not in manifesto_novo and (APP_DIR / r).is_file())
    (PASTA_TRABALHO / "plano.json").write_text(
        json.dumps({"origem": str(raiz), "copiar": copiar, "remover": remover}, ensure_ascii=False),
        encoding="utf-8",
    )


def baixar_e_preparar(info: Atualizacao, progresso: Progresso | None = None) -> Path:
    """Baixa e confere a atualização (incremental, ou completa se preciso).

    Devolve a pasta com os arquivos novos; o plano de troca fica em _atualizacao\\plano.json.
    """
    if not pode_atualizar():
        raise ErroAtualizacao(
            "A atualização automática só funciona no programa compilado (EtiquetasHortifruti.exe)."
        )
    _testar_escrita(APP_DIR)
    shutil.rmtree(PASTA_TRABALHO, ignore_errors=True)
    PASTA_TRABALHO.mkdir(parents=True, exist_ok=True)
    novo = PASTA_TRABALHO / "novo"
    try:
        manifesto: dict[str, dict] | None = None
        raiz: Path | None = None
        try:
            manifesto = _preparar_incremental(info, novo, progresso)
            raiz = novo
        except (ErroAtualizacao, OSError, zipfile.BadZipFile, ValueError, KeyError):
            shutil.rmtree(novo, ignore_errors=True)
            if progresso:
                progresso(0.0, "Atualização parcial indisponível; baixando o pacote completo...")
        if raiz is None:
            raiz = _preparar_completo(info, novo, progresso)
            try:
                manifesto = _validar_manifesto(
                    json.loads((raiz / NOME_MANIFESTO).read_text(encoding="utf-8"))
                )
            except (OSError, ValueError, ErroAtualizacao):
                manifesto = None
        if progresso:
            progresso(1.0, "Conferindo os arquivos...")
        _montar_plano(raiz, manifesto)
        return raiz
    except ErroAtualizacao:
        shutil.rmtree(PASTA_TRABALHO, ignore_errors=True)
        raise
    except OSError as exc:
        shutil.rmtree(PASTA_TRABALHO, ignore_errors=True)
        raise ErroAtualizacao(f"Falha durante o download ou a extração: {exc}") from exc
    except BaseException:
        shutil.rmtree(PASTA_TRABALHO, ignore_errors=True)
        raise


# ---------------------------------------------------------------- aplicação

_SCRIPT = r'''$ErrorActionPreference = 'Stop'
$base = $env:ATU_BASE
$destino = $env:ATU_DESTINO
$log = Join-Path $destino 'atualizacao.log'
function Log([string]$m) {
    try { Add-Content -LiteralPath $log -Value ('[{0}] {1}' -f (Get-Date -Format 'dd/MM/yyyy HH:mm:ss'), $m) -Encoding UTF8 } catch {}
}
try { Set-Content -LiteralPath $log -Value '' -Encoding UTF8 } catch {}
New-Item -ItemType File -Force -Path (Join-Path $base 'script_ok') | Out-Null
Log 'Aguardando o programa fechar...'
try { Wait-Process -Id ([int]$env:ATU_PID) -Timeout 90 -ErrorAction Stop } catch {}
Start-Sleep -Seconds 2

$maximo = 40
if ($env:ATU_TENTATIVAS) { $maximo = [int]$env:ATU_TENTATIVAS }
function Tentar([scriptblock]$acao) {
    $erro = $null
    for ($i = 0; $i -lt $maximo; $i++) {
        try { & $acao; return } catch { $erro = $_; Start-Sleep -Milliseconds 750 }
    }
    throw $erro
}

$plano = Get-Content -LiteralPath (Join-Path $base 'plano.json') -Raw -Encoding UTF8 | ConvertFrom-Json
$origem = $plano.origem
$backup = Join-Path $base 'backup'
$feitos = New-Object System.Collections.Generic.List[object]

function Reverter {
    Log 'Voltando para a versão anterior...'
    for ($i = $feitos.Count - 1; $i -ge 0; $i--) {
        $f = $feitos[$i]
        $dst = Join-Path $destino $f.rel
        try {
            if ($f.tinha) {
                Tentar {
                    New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dst) | Out-Null
                    Copy-Item -LiteralPath (Join-Path $backup $f.rel) -Destination $dst -Force
                }
            } elseif (Test-Path -LiteralPath $dst) {
                Tentar { Remove-Item -LiteralPath $dst -Force }
            }
        } catch { Log ('Não foi possível restaurar {0}: {1}' -f $f.rel, $_.Exception.Message) }
    }
}

function Guardar([string]$rel) {
    $dst = Join-Path $destino $rel
    $bak = Join-Path $backup $rel
    $tinha = Test-Path -LiteralPath $dst
    if ($tinha) {
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $bak) | Out-Null
        Tentar { Copy-Item -LiteralPath $dst -Destination $bak -Force }
    }
    $feitos.Add([pscustomobject]@{ rel = $rel; tinha = $tinha })
}

$aplicado = $false
try {
    Log ('Copiando {0} arquivo(s)...' -f @($plano.copiar).Count)
    foreach ($item in @($plano.copiar)) {
        $rel = $item.r
        Guardar $rel
        $dst = Join-Path $destino $rel
        New-Item -ItemType Directory -Force -Path (Split-Path -Parent $dst) | Out-Null
        Tentar { Copy-Item -LiteralPath (Join-Path $origem $rel) -Destination $dst -Force }
    }
    foreach ($rel in @($plano.remover)) {
        if (-not $rel) { continue }
        Guardar $rel
        $dst = Join-Path $destino $rel
        Tentar { Remove-Item -LiteralPath $dst -Force }
    }
    Log 'Conferindo os arquivos instalados...'
    foreach ($item in @($plano.copiar)) {
        $atual = (Get-FileHash -LiteralPath (Join-Path $destino $item.r) -Algorithm SHA256).Hash.ToLower()
        if ($atual -ne $item.h) { throw ('Arquivo diferente do esperado após a cópia: ' + $item.r) }
    }
    $aplicado = $true
} catch {
    Log ('Falha ao aplicar: ' + $_.Exception.Message)
    Reverter
}

$exe = Join-Path $destino $env:ATU_EXE
$flag = Join-Path $destino 'atualizacao_ok.flag'
Remove-Item -LiteralPath $flag -Force -ErrorAction SilentlyContinue
if ($aplicado) {
    $iniciou = $false
    try {
        $env:ATU_CONFIRMAR = $flag
        $p = Start-Process -FilePath $exe -WorkingDirectory $destino -PassThru
        for ($i = 0; $i -lt 60; $i++) {
            if (Test-Path -LiteralPath $flag) { $iniciou = $true; break }
            if ($p.HasExited) { break }
            Start-Sleep -Seconds 1
        }
        if (-not $iniciou -and -not $p.HasExited) { $iniciou = $true }
    } catch { $iniciou = $false }
    Remove-Item Env:ATU_CONFIRMAR -ErrorAction SilentlyContinue
    if ($iniciou) {
        Log 'Atualização concluída.'
    } else {
        Log 'A nova versão não abriu.'
        Reverter
        $aplicado = $false
    }
}
if (-not $aplicado) {
    Log 'Reabrindo a versão anterior.'
    try { Start-Process -FilePath $exe -WorkingDirectory $destino } catch { Log ('Não foi possível reabrir: ' + $_.Exception.Message) }
}
Remove-Item -LiteralPath $flag -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $base -Recurse -Force -ErrorAction SilentlyContinue
Remove-Item -LiteralPath $PSCommandPath -Force -ErrorAction SilentlyContinue
'''


def _powershell() -> str:
    raiz = os.environ.get("SystemRoot", r"C:\Windows")
    caminho = Path(raiz) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    return str(caminho) if caminho.is_file() else "powershell.exe"


def _descendentes(pai: int) -> list[int]:
    """PIDs de todos os processos filhos (recursivo) de 'pai', via Toolhelp32."""
    import ctypes
    from ctypes import wintypes

    class ENTRADA(ctypes.Structure):
        _fields_ = [
            ("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
            ("th32ProcessID", wintypes.DWORD), ("th32DefaultHeapID", ctypes.c_size_t),
            ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
            ("th32ParentProcessID", wintypes.DWORD), ("pcPriClassBase", wintypes.LONG),
            ("dwFlags", wintypes.DWORD), ("szExeFile", wintypes.WCHAR * 260),
        ]

    k32 = ctypes.windll.kernel32
    k32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ENTRADA)]
    k32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ENTRADA)]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]

    filhos_de: dict[int, list[int]] = {}
    snapshot = k32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if snapshot in (None, wintypes.HANDLE(-1).value):
        return []
    try:
        entrada = ENTRADA()
        entrada.dwSize = ctypes.sizeof(ENTRADA)
        ok = k32.Process32FirstW(snapshot, ctypes.byref(entrada))
        while ok:
            filhos_de.setdefault(int(entrada.th32ParentProcessID), []).append(
                int(entrada.th32ProcessID)
            )
            ok = k32.Process32NextW(snapshot, ctypes.byref(entrada))
    finally:
        k32.CloseHandle(snapshot)

    achados: list[int] = []
    fila = [pai]
    while fila:
        for filho in filhos_de.get(fila.pop(), []):
            if filho not in achados and filho != pai:
                achados.append(filho)
                fila.append(filho)
    return achados


def _encerrar_processos(pids: list[int]) -> None:
    import ctypes
    from ctypes import wintypes

    k32 = ctypes.windll.kernel32
    k32.OpenProcess.restype = wintypes.HANDLE
    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    for pid in pids:
        handle = k32.OpenProcess(0x0001, False, pid)  # PROCESS_TERMINATE
        if handle:
            k32.TerminateProcess(handle, 1)
            k32.CloseHandle(handle)


def iniciar_script(pasta_nova: Path, ambiente_extra: dict[str, str] | None = None) -> subprocess.Popen:
    """Grava e dispara o script de troca. Só devolve depois de ele confirmar que iniciou."""
    if not (PASTA_TRABALHO / "plano.json").is_file():
        raise ErroAtualizacao("O plano da atualização não foi encontrado. Baixe novamente.")
    script = Path(tempfile.gettempdir()) / f"atualizar_hortifruti_{os.getpid()}.ps1"
    try:
        script.write_text(_SCRIPT, encoding="utf-8-sig", newline="\r\n")
    except OSError as exc:
        raise ErroAtualizacao(f"Não foi possível preparar a atualização: {exc}") from exc

    ambiente = dict(os.environ)
    ambiente.pop("FLET_VIEW_PATH", None)
    ambiente.pop("ATU_CONFIRMAR", None)
    ambiente.update(
        ATU_PID=str(os.getpid()),
        ATU_DESTINO=str(APP_DIR),
        ATU_BASE=str(PASTA_TRABALHO),
        ATU_EXE=NOME_EXE,
    )
    ambiente.update(ambiente_extra or {})
    try:
        processo = subprocess.Popen(
            [_powershell(), "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
             "-File", str(script)],
            env=ambiente,
            cwd=tempfile.gettempdir(),
            creationflags=subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
    except OSError as exc:
        raise ErroAtualizacao(f"Não foi possível iniciar a atualização: {exc}") from exc

    # Só fecha o programa se o script realmente começou (PowerShell pode estar bloqueado).
    marca = PASTA_TRABALHO / "script_ok"
    limite = time.monotonic() + ESPERA_SCRIPT
    while time.monotonic() < limite:
        if marca.exists():
            return processo
        if processo.poll() is not None:
            break
        time.sleep(0.2)
    try:
        processo.kill()
    except OSError:
        pass
    raise ErroAtualizacao(
        "O Windows não permitiu executar o script de atualização (PowerShell bloqueado?). "
        "O programa continua na versão atual."
    )


def aplicar_e_reiniciar(pasta_nova: Path, versao: str = "") -> None:
    """Dispara o script de troca e encerra o programa (não retorna se der certo).

    `versao` é repassada ao programa recém-instalado (ATU_VERSAO), que a grava como
    "última versão aplicada". Se a troca for desfeita, a versão antiga não recebe isso.
    """
    try:
        import db

        db.finalizar_uso()
    except Exception:
        pass
    processo = iniciar_script(pasta_nova, {"ATU_VERSAO": versao} if versao else None)
    # O cliente visual (flet.exe) é filho deste processo e trava arquivos da pasta:
    # encerra-o (menos o script) antes de sair.
    try:
        _encerrar_processos([p for p in _descendentes(os.getpid()) if p != processo.pid])
    except Exception:
        pass
    os._exit(0)


def versao_recem_instalada() -> str:
    """Versão que acabou de ser instalada pelo atualizador ("" se não foi esta abertura)."""
    if not os.environ.get("ATU_CONFIRMAR"):
        return ""
    return os.environ.get("ATU_VERSAO", "").strip()[:40]


def confirmar_inicializacao() -> None:
    """Chamado quando a interface abriu: avisa o script que a nova versão está viva."""
    alvo = os.environ.pop("ATU_CONFIRMAR", "")
    os.environ.pop("ATU_VERSAO", None)
    if alvo:
        try:
            Path(alvo).write_text("ok", encoding="utf-8")
        except OSError:
            pass
    limpar_residuos()


def limpar_residuos() -> None:
    """Remove restos de uma atualização anterior (baixados ou interrompidos)."""
    if os.environ.get("ATU_CONFIRMAR"):
        return  # o script ainda pode precisar do backup
    shutil.rmtree(PASTA_TRABALHO, ignore_errors=True)
