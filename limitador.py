from __future__ import annotations

import threading
import time
from collections import OrderedDict, deque
from typing import Callable

Relogio = Callable[[], float]


class JanelaDeslizante:
    """Permite até `limite` eventos por chave a cada `janela` segundos."""

    def __init__(self, limite: int, janela: float, *, max_chaves: int = 5000,
                 relogio: Relogio = time.monotonic) -> None:
        self.limite = limite
        self.janela = janela
        self.max_chaves = max_chaves
        self._relogio = relogio
        self._eventos: OrderedDict[str, deque[float]] = OrderedDict()
        self._trava = threading.Lock()

    def permitir(self, chave: str) -> tuple[bool, int]:
        """Registra um evento. Devolve (permitido, segundos até liberar)."""
        agora = self._relogio()
        corte = agora - self.janela
        with self._trava:
            fila = self._eventos.get(chave)
            if fila is None:
                self._abrir_espaco()
                fila = self._eventos[chave] = deque()
            else:
                self._eventos.move_to_end(chave)
            while fila and fila[0] <= corte:
                fila.popleft()
            if len(fila) >= self.limite:
                return False, max(1, int(fila[0] + self.janela - agora) + 1)
            fila.append(agora)
            return True, 0

    def _abrir_espaco(self) -> None:
        while len(self._eventos) >= self.max_chaves:
            self._eventos.popitem(last=False)


class BloqueioPorFalhas:
    """Bloqueia a chave depois de `limite` falhas dentro de `janela` segundos."""

    def __init__(self, limite: int, janela: float, *, max_chaves: int = 5000,
                 relogio: Relogio = time.monotonic) -> None:
        self.limite = limite
        self.janela = janela
        self.max_chaves = max_chaves
        self._relogio = relogio
        self._falhas: OrderedDict[str, list[float]] = OrderedDict()
        self._trava = threading.Lock()

    def _recentes(self, chave: str) -> list[float]:
        corte = self._relogio() - self.janela
        recentes = [t for t in self._falhas.get(chave, []) if t > corte]
        if recentes:
            self._falhas[chave] = recentes
        else:
            self._falhas.pop(chave, None)
        return recentes

    def segundos_bloqueado(self, chave: str) -> int:
        """0 se liberado; senão quantos segundos faltam."""
        with self._trava:
            recentes = self._recentes(chave)
            if len(recentes) < self.limite:
                return 0
            return max(1, int(recentes[0] + self.janela - self._relogio()) + 1)

    def falha(self, chave: str) -> bool:
        """Registra uma falha. True se ESTA falha acabou de bloquear a chave."""
        with self._trava:
            recentes = self._recentes(chave)
            while len(self._falhas) >= self.max_chaves and chave not in self._falhas:
                self._falhas.popitem(last=False)
            recentes.append(self._relogio())
            self._falhas[chave] = recentes
            self._falhas.move_to_end(chave)
            return len(recentes) == self.limite

    def sucesso(self, chave: str) -> None:
        with self._trava:
            self._falhas.pop(chave, None)

    def limpar(self) -> None:
        with self._trava:
            self._falhas.clear()
