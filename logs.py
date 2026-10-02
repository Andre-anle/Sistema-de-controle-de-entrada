"""Logs operacionais e de segurança em arquivo, com rotação.

- logs/aplicacao.log : tudo (erros, acessos HTTP, backup, manutenção);
- logs/seguranca.log : só eventos de segurança (bloqueios, CSRF, hosts
  inválidos, falhas de login). É o arquivo a observar no monitoramento.

Regras: nunca registrar senhas, tokens, cookies nem query strings; textos
vindos do usuário passam por `seguro()` para impedir forjar linhas de log
(quebras de linha / caracteres de controle).
"""

from __future__ import annotations

import logging
import logging.handlers

from paths import LOG_DIR

NOME = "hortifruti"
NOME_SEGURANCA = "hortifruti.seguranca"
_FORMATO = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
_configurado = False


def seguro(valor: object, limite: int = 200) -> str:
    """Texto de uma linha só, sem caracteres de controle e com tamanho limitado."""
    texto = "".join(c if c.isprintable() else "?" for c in str(valor))
    return texto if len(texto) <= limite else texto[: limite - 1] + "…"


class _FormatadorSeguro(logging.Formatter):
    def formatMessage(self, record: logging.LogRecord) -> str:  # noqa: N802
        linha = super().formatMessage(record)
        return linha.replace("\r", " ").replace("\n", " | ")


def configurar(nivel: int = logging.INFO) -> None:
    """Idempotente. Se a pasta não for gravável, o programa segue sem arquivo de log."""
    global _configurado
    if _configurado:
        return
    _configurado = True
    raiz = logging.getLogger(NOME)
    raiz.setLevel(nivel)
    raiz.propagate = False
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        geral = logging.handlers.RotatingFileHandler(
            LOG_DIR / "aplicacao.log", maxBytes=1_000_000, backupCount=5, encoding="utf-8"
        )
        geral.setFormatter(_FormatadorSeguro(_FORMATO))
        raiz.addHandler(geral)
        seg = logging.handlers.RotatingFileHandler(
            LOG_DIR / "seguranca.log", maxBytes=1_000_000, backupCount=10, encoding="utf-8"
        )
        seg.setFormatter(_FormatadorSeguro(_FORMATO))
        seg.addFilter(lambda r: r.name.startswith(NOME_SEGURANCA))
        raiz.addHandler(seg)
    except OSError:
        raiz.addHandler(logging.NullHandler())


def log(sufixo: str = "") -> logging.Logger:
    return logging.getLogger(f"{NOME}.{sufixo}" if sufixo else NOME)


def seguranca() -> logging.Logger:
    return logging.getLogger(NOME_SEGURANCA)
