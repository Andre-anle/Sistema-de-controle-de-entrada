"""Contadores simples para monitoramento (expostos em /metricas e gravados no log)."""

from __future__ import annotations

import threading
import time
from collections import Counter

_inicio = time.time()
_contadores: Counter[str] = Counter()
_trava = threading.Lock()


def contar(nome: str, quantidade: int = 1) -> None:
    with _trava:
        _contadores[nome] += quantidade


def instantaneo() -> dict[str, int]:
    with _trava:
        return dict(_contadores)


def segundos_ativo() -> int:
    return int(time.time() - _inicio)


def reiniciar() -> None:
    with _trava:
        _contadores.clear()
