"""Validação e sanitização de entradas (teclado, formulários web e filtros).

Toda entrada de texto livre passa por aqui antes de chegar ao banco, à
impressora ou à planilha. As regras são propositalmente restritivas:
texto normalizado (NFC), sem caracteres de controle/invisíveis e com
tamanho máximo. Os limites também protegem as bibliotecas nativas
(Pillow/FreeType, win32) contra entradas absurdas.
"""

from __future__ import annotations

import re
import unicodedata

MENSAGEM_EAN_INVALIDO = "EAN inválido. Use somente números, de 8 a 14 dígitos."

DESCRICAO_MAX = 120
COLABORADOR_MAX = 80
NOME_MAX = 80
DOCUMENTO_MIN = 5
DOCUMENTO_MAX = 20
FUNCAO_MAX = 60
EMPRESA_MAX = 80
AUTORIZADO_MAX = 80
FILTRO_MAX = 100
QUANTIDADE_MINIMA = 1
QUANTIDADE_MAXIMA = 50

_DOCUMENTO_RE = re.compile(r"[0-9A-Za-z][0-9A-Za-z.\-/ ]*")


def ean_valido(ean: str) -> bool:
    limpo = (ean or "").strip()
    return limpo.isascii() and limpo.isdigit() and 8 <= len(limpo) <= 14


def normalizar_texto(valor: object) -> str:
    """NFC + espaços colapsados. Não remove nada além de espaços repetidos."""
    texto = unicodedata.normalize("NFC", str(valor if valor is not None else ""))
    return " ".join(texto.split())


def limpar_texto(
    valor: object,
    *,
    campo: str,
    maximo: int,
    obrigatorio: bool = True,
) -> str:
    """Devolve o texto normalizado ou levanta ValueError com mensagem para o usuário."""
    texto = normalizar_texto(valor)
    if not texto:
        if obrigatorio:
            raise ValueError(f"Informe {campo}.")
        return ""
    if len(texto) > maximo:
        raise ValueError(f"{campo[0].upper()}{campo[1:]} deve ter no máximo {maximo} caracteres.")
    # isprintable() recusa controles, separadores de linha, marcas invisíveis
    # e os caracteres de inversão de texto (U+202E) usados para enganar a leitura.
    if any(not c.isprintable() for c in texto):
        raise ValueError(f"{campo[0].upper()}{campo[1:]} contém caracteres inválidos.")
    return texto


def validar_ean_opcional(ean: object) -> str:
    texto = str(ean or "").strip()
    if texto and not ean_valido(texto):
        raise ValueError(MENSAGEM_EAN_INVALIDO)
    return texto


def validar_descricao(valor: object) -> str:
    return limpar_texto(valor, campo="o nome da mercadoria", maximo=DESCRICAO_MAX)


def validar_colaborador(valor: object) -> str:
    return limpar_texto(valor, campo="o nome do colaborador", maximo=COLABORADOR_MAX)


def validar_quantidade(valor: object) -> int:
    try:
        quantidade = int(str(valor).strip())
    except (TypeError, ValueError):
        raise ValueError("Digite um número válido para a quantidade.") from None
    if not QUANTIDADE_MINIMA <= quantidade <= QUANTIDADE_MAXIMA:
        raise ValueError(
            f"A quantidade deve ser entre {QUANTIDADE_MINIMA} e {QUANTIDADE_MAXIMA}."
        )
    return quantidade


def validar_documento(valor: object) -> str:
    texto = limpar_texto(valor, campo="o documento", maximo=DOCUMENTO_MAX)
    if len(texto) < DOCUMENTO_MIN or not _DOCUMENTO_RE.fullmatch(texto):
        raise ValueError("Documento inválido. Use apenas letras, números, ponto, hífen e barra.")
    return texto


def validar_visitante(
    nome: object,
    documento: object,
    funcao: object,
    empresa: object,
    autorizado_por: object,
) -> tuple[str, str, str, str, str]:
    return (
        limpar_texto(nome, campo="o nome do visitante", maximo=NOME_MAX),
        validar_documento(documento),
        limpar_texto(funcao, campo="a função", maximo=FUNCAO_MAX),
        limpar_texto(empresa, campo="a empresa", maximo=EMPRESA_MAX),
        limpar_texto(autorizado_por, campo="quem autorizou", maximo=AUTORIZADO_MAX),
    )


def limpar_filtro(valor: object, campo: str = "filtro") -> str:
    """Filtro de busca: opcional, curto e sem caracteres de controle."""
    texto = normalizar_texto(valor)
    if len(texto) > FILTRO_MAX:
        raise ValueError(f"O {campo} deve ter no máximo {FILTRO_MAX} caracteres.")
    if any(not c.isprintable() for c in texto):
        raise ValueError(f"O {campo} contém caracteres inválidos.")
    return texto


def escapar_like(termo: str) -> str:
    """Escapa %, _ e \\ para uso com `LIKE ? ESCAPE '\\'` (o termo vira literal)."""
    return termo.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
