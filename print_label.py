from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont, ImageWin

try:
    import pywintypes  # noqa: F401
    import pythoncom  # noqa: F401
    import win32api  # noqa: F401
    import win32con  # noqa: F401
    import win32print
    import win32ui
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "Não foi possível importar win32print/win32ui. "
        "Instale o pywin32 neste Python com: python -m pip install pywin32==312"
    ) from exc

from paths import FONTES_WINDOWS, LOGO_PATH
from settings import AppSettings

DPI = 203
MM_PARA_INCH = 25.4
LARGURA_ETIQUETA = int(55 * DPI / MM_PARA_INCH)
ALTURA_ETIQUETA = int(40 * DPI / MM_PARA_INCH)
MARGEM = 10
COR_TEXTO = (0, 0, 0)
# 3 px ≈ 0,37 mm em 203 dpi: módulo estável para leitores USB.
MODULO_IDEAL_PX = 3
QUIET_MODULES = 10
# Faixa vertical à direita (~13 mm): um pouco maior, sem ocupar o bloco de texto.
LARGURA_FAIXA_MM = 13.0
LOGO_LARGURA = 250
LOGO_ALTURA = 120
LOGO_LARGURA_COM_BARRAS = 200
LOGO_ALTURA_COM_BARRAS = 96


def listar_impressoras() -> list[str]:
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    try:
        return [impressora[2] for impressora in win32print.EnumPrinters(flags)]
    except Exception:
        return []


def impressora_padrao() -> str:
    try:
        return win32print.GetDefaultPrinter()
    except Exception:
        impressoras = listar_impressoras()
        return impressoras[0] if impressoras else ""


@lru_cache(maxsize=16)
def _resolver_fonte(nome: str, tamanho: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    candidatos = [
        Path(nome),
        FONTES_WINDOWS / nome,
        FONTES_WINDOWS / Path(nome).name,
        FONTES_WINDOWS / "arial.ttf",
    ]
    for caminho in candidatos:
        try:
            if caminho.is_file():
                return ImageFont.truetype(str(caminho), tamanho)
        except OSError:
            continue
    return ImageFont.load_default()


@lru_cache(maxsize=4)
def _carregar_logo(largura: int = LOGO_LARGURA, altura: int = LOGO_ALTURA) -> Image.Image | None:
    if not LOGO_PATH.is_file():
        return None
    try:
        logo = Image.open(LOGO_PATH).convert("RGBA")
        return logo.resize((largura, altura), Image.Resampling.LANCZOS)
    except OSError:
        return None


def _quebrar_linha(draw: ImageDraw.ImageDraw, texto: str, font, max_width: int) -> list[str]:
    if not texto:
        return [""]
    palavras = texto.split()
    if not palavras:
        return [texto]
    linhas: list[str] = []
    atual = palavras[0]
    for palavra in palavras[1:]:
        teste = f"{atual} {palavra}"
        largura = draw.textbbox((0, 0), teste, font=font)[2]
        if largura <= max_width:
            atual = teste
        else:
            linhas.append(atual)
            atual = palavra
    linhas.append(atual)
    return linhas


def _bits_code128(conteudo: str) -> str | None:
    try:
        import barcode
    except ImportError:
        return None
    try:
        codigo = barcode.get_barcode_class("code128")(conteudo)
        padrao = codigo.build()
    except Exception:
        return None
    if not padrao or not padrao[0]:
        return None
    return padrao[0]


def _gerar_barcode_leitor_usb(conteudo: str, comprimento_max: int, largura_faixa: int) -> Image.Image | None:
    """Code 128 em pixels inteiros, sem suavização. `comprimento_max` é o eixo das barras."""
    texto = (conteudo or "").strip()
    if not texto or comprimento_max < 40 or largura_faixa < 24:
        return None
    bits = _bits_code128(texto)
    if not bits:
        return None

    melhor: tuple[int, int] | None = None
    for quiet in range(QUIET_MODULES, 5, -1):
        total = len(bits) + 2 * quiet
        if total <= 0:
            continue
        modulo = min(MODULO_IDEAL_PX, comprimento_max // total)
        if modulo < 1:
            continue
        if melhor is None or modulo > melhor[0] or (modulo == melhor[0] and quiet > melhor[1]):
            melhor = (modulo, quiet)
    if melhor is None:
        return None
    modulo, quiet = melhor

    comprimento = (len(bits) + 2 * quiet) * modulo
    imagem = Image.new("1", (comprimento, largura_faixa), 1)
    draw = ImageDraw.Draw(imagem)
    x = quiet * modulo
    for bit in bits:
        if bit == "1":
            draw.rectangle((x, 0, x + modulo - 1, largura_faixa - 1), fill=0)
        x += modulo
    return imagem


def _gerar_barcode_vertical_direita(conteudo: str, comprimento_max: int, largura_faixa: int) -> Image.Image | None:
    """Code 128 na vertical para a lateral direita da etiqueta."""
    horizontal = _gerar_barcode_leitor_usb(conteudo, comprimento_max, largura_faixa)
    if horizontal is None:
        return None
    return horizontal.rotate(90, expand=True, fillcolor=1).convert("RGB")


def montar_imagem(linhas: list[str], config: AppSettings, barcode_dados: str | None = None) -> Image.Image:
    com_barras = bool(barcode_dados)
    tamanho_fonte = max(12, config.font_size - 2) if com_barras else config.font_size
    font = _resolver_fonte(config.font_name, tamanho_fonte)
    logo = (
        _carregar_logo(LOGO_LARGURA_COM_BARRAS, LOGO_ALTURA_COM_BARRAS)
        if com_barras
        else _carregar_logo()
    )
    largura_faixa = int(LARGURA_FAIXA_MM * DPI / MM_PARA_INCH)
    comprimento_cb = ALTURA_ETIQUETA - 8
    cb = (
        _gerar_barcode_vertical_direita(barcode_dados, comprimento_cb, largura_faixa)
        if barcode_dados
        else None
    )
    if cb:
        max_texto = LARGURA_ETIQUETA - (MARGEM * 2) - 6 - cb.width
    else:
        max_texto = LARGURA_ETIQUETA - (MARGEM * 2)

    if logo and logo.width > max_texto:
        nova_altura = max(1, int(logo.height * max_texto / logo.width))
        logo = logo.resize((max_texto, nova_altura), Image.Resampling.LANCZOS)

    img = Image.new("RGB", (LARGURA_ETIQUETA, ALTURA_ETIQUETA), (255, 255, 255))
    draw = ImageDraw.Draw(img)

    linhas_quebradas: list[str] = []
    for linha in linhas:
        linhas_quebradas.extend(_quebrar_linha(draw, linha, font, max_texto))

    altura_linha = tamanho_fonte + 4
    altura_logo = logo.height if logo else 0

    if logo:
        x_logo = MARGEM + max(0, (max_texto - logo.width) // 2)
        img.paste(logo, (x_logo, MARGEM), logo)

    y = MARGEM + altura_logo + 6
    limite_texto = ALTURA_ETIQUETA - MARGEM
    for linha in linhas_quebradas:
        if y + altura_linha > limite_texto:
            break
        draw.text((MARGEM, y), linha, font=font, fill=COR_TEXTO)
        y += altura_linha

    if cb:
        x_cb = LARGURA_ETIQUETA - 4 - cb.width
        y_cb = max(0, (ALTURA_ETIQUETA - cb.height) // 2)
        img.paste(cb, (x_cb, y_cb))

    return img


def imprimir_imagens(imagens: list[Image.Image], impressora: str, titulo: str) -> None:
    if not imagens:
        return
    if not impressora:
        raise RuntimeError("Nenhuma impressora configurada.")

    hdc = None
    doc_iniciado = False
    try:
        hdc = win32ui.CreateDC()
        hdc.CreatePrinterDC(impressora)
        hdc.StartDoc(titulo)
        doc_iniciado = True
        dibs: dict[int, ImageWin.Dib] = {}
        for imagem in imagens:
            dib = dibs.get(id(imagem))
            if dib is None:
                dib = dibs[id(imagem)] = ImageWin.Dib(imagem.convert("RGB"))
            hdc.StartPage()
            dib.draw(
                hdc.GetHandleOutput(),
                (0, 0, imagem.width, imagem.height),
            )
            hdc.EndPage()
        hdc.EndDoc()
        doc_iniciado = False
    except Exception:
        if hdc is not None and doc_iniciado:
            try:
                hdc.AbortDoc()
            except Exception:
                pass
        raise
    finally:
        if hdc is not None:
            hdc.DeleteDC()


def imprimir_etiquetas_produto(
    ean: str,
    descricao: str,
    colaborador: str,
    data_hora: str,
    quantidade: int,
    config: AppSettings,
) -> None:
    linhas = [
        "CONTROLE ENTRADA CD",
        *([f"EAN: {ean}"] if ean else []),
        f"Descrição: {descricao}",
        f"Colaborador: {colaborador}",
        f"Data e Hora: {data_hora}",
        "Proibido remover",
    ]
    imagem = montar_imagem(linhas, config, barcode_dados=ean or None)
    imprimir_imagens([imagem] * quantidade, config.printer, "Etiqueta")


def imprimir_etiqueta_visitante(
    nome: str,
    funcao: str,
    empresa: str,
    data_hora: str,
    config: AppSettings,
) -> None:
    linhas = [
        "VISITANTE",
        f"NOME: {nome}",
        f"FUNÇÃO: {funcao}",
        f"EMPRESA: {empresa}",
        f"Data e Hora: {data_hora}",
        "Proibido remover",
    ]
    imagem = montar_imagem(linhas, config)
    imprimir_imagens([imagem], config.printer, "Etiqueta Visitante")