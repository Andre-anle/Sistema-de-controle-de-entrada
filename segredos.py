from __future__ import annotations

import os
import secrets
import sys
from pathlib import Path

import paths
from logs import log

_ENTROPIA = b"EtiquetasHortifruti/segredos/v1"
_cache: dict[str, bytes] = {}


class ErroSegredo(Exception):
    pass


def _proteger(dados: bytes) -> bytes:
    if sys.platform != "win32":
        return dados
    import win32crypt

    return bytes(win32crypt.CryptProtectData(dados, "Etiquetas Hortifruti", _ENTROPIA, None, None, 0))


def _desproteger(blob: bytes) -> bytes:
    if sys.platform != "win32":
        return blob
    import win32crypt

    try:
        return bytes(win32crypt.CryptUnprotectData(blob, _ENTROPIA, None, None, 0)[1])
    except Exception as exc:  # pywintypes.error
        raise ErroSegredo(
            "Não foi possível abrir o segredo (arquivo de outro usuário ou computador?)."
        ) from exc


def _gravar_atomico(caminho: Path, dados: bytes) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    temporario = caminho.with_name(caminho.name + ".tmp")
    temporario.write_bytes(dados)
    os.replace(temporario, caminho)


def obter_segredo(nome: str, tamanho: int = 32) -> tuple[bytes, bool]:
    """Devolve (segredo, criado_agora). Cria um segredo aleatório na primeira chamada."""
    if nome in _cache:
        return _cache[nome], False
    caminho = paths.SEGREDOS_DIR / f"{nome}.bin"
    criado = False
    if caminho.is_file():
        segredo = _desproteger(caminho.read_bytes())
    else:
        segredo = secrets.token_bytes(tamanho)
        _gravar_atomico(caminho, _proteger(segredo))
        criado = True
        log("segredos").info("Segredo '%s' criado.", nome)
    _cache[nome] = segredo
    return segredo, criado


def substituir_segredo(nome: str, segredo: bytes) -> None:
    """Usado na restauração: adota uma chave informada pelo usuário (código de recuperação)."""
    _gravar_atomico(paths.SEGREDOS_DIR / f"{nome}.bin", _proteger(segredo))
    _cache[nome] = segredo


def esquecer_cache() -> None:
    _cache.clear()
