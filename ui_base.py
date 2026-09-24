import asyncio
from collections.abc import Callable
from datetime import datetime

import flet as ft

VERDE = "#1F6B3A"
VERDE_FOLHA = "#2F8A4A"
VERDE_SUAVE = "#E7F3EA"
CREME = "#F3F0E6"
CARTAO = "#FFFDF8"
LARANJA = "#E07A2F"
TEXTO = "#243028"
TEXTO_SUAVE = "#5B6F60"
BORDA = "#C9D6C3"

ESPACO_CARTAO = 24


def agora_texto() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def formatar_data_hora_exibicao(valor: str) -> str:
    try:
        return datetime.strptime(valor, "%Y-%m-%d %H:%M:%S").strftime("%d/%m/%Y %H:%M")
    except (TypeError, ValueError):
        return valor or ""


def estilo_cartao(padding=ESPACO_CARTAO) -> dict:
    return {
        "bgcolor": CARTAO,
        "border_radius": 22,
        "padding": padding,
        "shadow": ft.BoxShadow(
            blur_radius=22,
            spread_radius=0,
            color="#1F6B3A22",
            offset=ft.Offset(0, 6),
        ),
    }


def titulo_secao(texto: str) -> ft.Text:
    return ft.Text(texto, size=16, weight=ft.FontWeight.W_600, color=VERDE)


def aplicar_tema(page: ft.Page) -> None:
    page.theme_mode = ft.ThemeMode.LIGHT
    page.bgcolor = CREME
    page.theme = ft.Theme(
        use_material3=True,
        color_scheme_seed=VERDE,
        color_scheme=ft.ColorScheme(
            primary=VERDE,
            on_primary="#FFFFFF",
            primary_container="#CDEAD4",
            on_primary_container="#0F3B22",
            secondary=LARANJA,
            on_secondary="#FFFFFF",
            surface=CARTAO,
            on_surface=TEXTO,
            on_surface_variant=TEXTO_SUAVE,
            outline=BORDA,
        ),
    )


def mostrar_snack(page: ft.Page, mensagem: str) -> None:
    page.show_dialog(
        ft.SnackBar(
            content=ft.Text(mensagem, color="#FFFFFF"),
            bgcolor=VERDE,
        )
    )


def fechar_dialogo(dialogo: ft.AlertDialog) -> None:
    dialogo.open = False
    dialogo.update()


def abrir_dialogo(
    page: ft.Page,
    titulo: str,
    conteudo: ft.Control,
    *,
    confirmar: str | None = None,
    ao_confirmar: Callable | None = None,
    cancelar: str = "Cancelar",
    **opcoes,
) -> ft.AlertDialog:
    dialogo = ft.AlertDialog(
        modal=True,
        bgcolor=CARTAO,
        title=ft.Text(titulo, color=VERDE, weight=ft.FontWeight.W_600),
        content=conteudo,
        **opcoes,
    )
    acoes: list[ft.Control] = []
    if confirmar:
        acoes.append(ft.FilledButton(confirmar, bgcolor=VERDE, on_click=ao_confirmar))
    acoes.append(ft.TextButton(cancelar, on_click=lambda _: fechar_dialogo(dialogo)))
    dialogo.actions = acoes
    page.show_dialog(dialogo)
    return dialogo


def estilo_campo(largura: int = 420, icone=None) -> dict:
    estilo = {
        "filled": True,
        "fill_color": VERDE_SUAVE,
        "bgcolor": VERDE_SUAVE,
        "color": TEXTO,
        "cursor_color": VERDE,
        "border_radius": 14,
        "border_color": BORDA,
        "focused_border_color": VERDE,
        "focused_bgcolor": "#FFFFFF",
        "content_padding": ft.Padding.symmetric(horizontal=16, vertical=14),
        "label_style": ft.TextStyle(color=TEXTO_SUAVE, size=14, weight=ft.FontWeight.W_500),
        "text_size": 15,
        "width": largura,
    }
    if icone is not None:
        estilo["prefix_icon"] = icone
    return estilo


def _formatar_hora(digitos: str) -> str:
    hora = digitos[:4]
    if len(hora) <= 2:
        return hora
    return f"{hora[:2]}:{hora[2:]}"


def _formatar_so_data(digitos: str) -> str:
    data = "".join(c for c in digitos if c.isdigit())[:8]
    if len(data) <= 2:
        return data
    if len(data) <= 4:
        return f"{data[:2]}/{data[2:]}"
    return f"{data[:2]}/{data[2:4]}/{data[4:]}"


def formatar_data_hora_digitada(texto: str) -> str:
    limpo = (texto or "").strip()
    if not limpo:
        return ""
    for formato, saida in (
        ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M"),
        ("%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M"),
        ("%Y-%m-%d", "%d/%m/%Y"),
        ("%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M"),
        ("%d-%m-%Y", "%d/%m/%Y"),
        ("%d-%m-%y", "%d/%m/%y"),
    ):
        try:
            return datetime.strptime(limpo, formato).strftime(saida)
        except ValueError:
            continue

    if " " in limpo:
        data_bruta, _, hora_bruta = limpo.partition(" ")
        data = _formatar_so_data(data_bruta)
        hora_digitos = "".join(c for c in hora_bruta if c.isdigit())[:4]
        if not hora_digitos:
            return f"{data} "
        return f"{data} {_formatar_hora(hora_digitos)}"

    digitos = "".join(c for c in limpo if c.isdigit())[:12]
    if len(digitos) <= 8:
        return _formatar_so_data(digitos)
    return f"{_formatar_so_data(digitos[:8])} {_formatar_hora(digitos[8:])}"


class CampoTexto:
    def __init__(
        self,
        rotulo: str,
        icone,
        on_escolha: Callable[[str], None] | None = None,
        on_enter: Callable[[], None] | None = None,
        largura: int = 480,
    ) -> None:
        self.on_escolha = on_escolha
        self.on_enter = on_enter
        self.campo = ft.TextField(
            label=rotulo,
            on_submit=self._ao_enter,
            **estilo_campo(largura, icone),
        )
        self.view = self.campo

    @property
    def value(self) -> str:
        return self.campo.value or ""

    @value.setter
    def value(self, valor: str) -> None:
        self.campo.value = valor

    def aceitar(self) -> str:
        escolhido = self.value.strip()
        if escolhido and self.on_escolha:
            self.on_escolha(escolhido)
        return escolhido

    def _ao_enter(self, _=None) -> None:
        self.aceitar()
        if self.on_enter:
            self.on_enter()


class CampoData:
    ATRASO_FORMATACAO = 0.45

    def __init__(self, rotulo: str) -> None:
        self._geracao = 0
        self._atualizando = False
        self.campo = ft.TextField(
            label=rotulo,
            hint_text="dd/mm/aa ou dd/mm/aaaa",
            on_change=self._ao_digitar,
            on_blur=self._ao_sair,
            **estilo_campo(220, ft.Icons.CALENDAR_MONTH_OUTLINED),
        )
        self.view = self.campo

    @property
    def value(self) -> str:
        return self.campo.value or ""

    def _formatar(self, *, imediato: bool) -> None:
        bruto = self.value
        formatado = formatar_data_hora_digitada(bruto)
        if imediato:
            formatado = formatado.strip()
        if formatado == bruto:
            return
        self._atualizando = True
        self.campo.value = formatado
        self.campo.update()
        self._atualizando = False

    def _ao_digitar(self, _=None) -> None:
        if self._atualizando:
            return
        self._geracao += 1
        geracao = self._geracao
        pagina = self.campo.page
        if pagina is None:
            return

        async def depois() -> None:
            await asyncio.sleep(self.ATRASO_FORMATACAO)
            if geracao == self._geracao:
                self._formatar(imediato=False)

        pagina.run_task(depois)

    def _ao_sair(self, _=None) -> None:
        self._geracao += 1
        self._formatar(imediato=True)
