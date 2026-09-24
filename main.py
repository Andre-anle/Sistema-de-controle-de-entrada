import sqlite3
import sys
import ssl
from dataclasses import replace

import flet as ft

import db
from paths import LOGO_PATH, ICONE_PATH
from print_label import imprimir_etiquetas_produto, impressora_padrao
from remote_access import aplicar_acesso_remoto, gerar_chave, parar_servidor
from settings import carregar_config, salvar_config
from ui_base import (
    BORDA,
    CARTAO,
    CREME,
    LARANJA,
    TEXTO,
    TEXTO_SUAVE,
    VERDE,
    VERDE_FOLHA,
    VERDE_SUAVE,
    CampoTexto,
    abrir_dialogo,
    agora_texto,
    aplicar_tema,
    cabecalho_cartao,
    estilo_campo,
    estilo_cartao,
    fechar_dialogo,
    icone_destaque,
    mostrar_snack,
)
from ui_config import ConfigMixin
from ui_consulta import ConsultaMixin
from ui_visitantes import VisitantesMixin

QUANTIDADE_MAXIMA = 50


def ean_valido(ean: str) -> bool:
    limpo = ean.strip()
    if not limpo:
        return False
    if limpo.isdigit():
        return 8 <= len(limpo) <= 14
    return 3 <= len(limpo) <= 32


class App(ConsultaMixin, VisitantesMixin, ConfigMixin):
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.config = carregar_config()
        if not self.config.printer:
            self.config.printer = impressora_padrao()
        if self.config.remote_enabled and not self.config.remote_token:
            self.config.remote_token = gerar_chave()
            salvar_config(self.config)

        aplicar_tema(page)
        page.title = "Hortifruti Natural da Terra · Etiquetas"
        page.padding = 0
        page.scroll = None
        if ICONE_PATH.is_file():
            page.window.icon = str(ICONE_PATH)
        page.window.on_event = self._ao_janela
        self._seletor_arquivo = ft.FilePicker()
        self._clipboard = ft.Clipboard()

        self.ean = CampoTexto(
            "EAN do produto (opcional)",
            ft.Icons.QR_CODE_2,
            on_escolha=self._ao_selecionar_ean,
            on_enter=lambda: self.descricao.campo.focus(),
            largura=None,
        )
        self.ean.campo.autofocus = True
        self.descricao = CampoTexto(
            "Descrição do produto",
            ft.Icons.INVENTORY_2_OUTLINED,
            on_enter=lambda: self.colaborador.campo.focus(),
            largura=None,
        )
        self.colaborador = CampoTexto(
            "Nome do colaborador",
            ft.Icons.PERSON_OUTLINE,
            on_enter=self.gerar_etiqueta,
            largura=None,
        )

        formulario = ft.Container(
            **estilo_cartao(28),
            content=ft.Column(
                [
                    cabecalho_cartao(
                        ft.Icons.LOCAL_OFFER_OUTLINED,
                        "Nova etiqueta",
                        "Bipe o código de barras ou preencha os dados do produto.",
                    ),
                    ft.Divider(height=1, color=BORDA),
                    self.ean.view,
                    self.descricao.view,
                    self.colaborador.view,
                    ft.Row(
                        [
                            ft.Row(
                                [
                                    ft.Icon(ft.Icons.KEYBOARD_RETURN, size=16, color=TEXTO_SUAVE),
                                    ft.Text(
                                        "Enter avança para o próximo campo",
                                        size=12,
                                        color=TEXTO_SUAVE,
                                    ),
                                ],
                                spacing=6,
                                expand=True,
                            ),
                            ft.FilledButton(
                                "Gerar etiqueta",
                                icon=ft.Icons.PRINT,
                                bgcolor=VERDE,
                                height=48,
                                style=ft.ButtonStyle(
                                    padding=ft.Padding.symmetric(horizontal=28),
                                    shape=ft.RoundedRectangleBorder(radius=14),
                                    text_style=ft.TextStyle(size=15, weight=ft.FontWeight.W_600),
                                ),
                                on_click=lambda _: self.gerar_etiqueta(),
                            ),
                        ],
                        vertical_alignment=ft.CrossAxisAlignment.CENTER,
                    ),
                ],
                spacing=16,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
            ),
        )

        atalhos = ft.Row(
            [
                self._atalho(
                    ft.Icons.PERSON_ADD_ALT,
                    "Registrar visitante",
                    "Imprime o crachá e marca a entrada",
                    self.adicionar_visitante_dialog,
                    cor=LARANJA,
                    fundo="#FBEBDD",
                ),
                self._atalho(
                    ft.Icons.MANAGE_SEARCH,
                    "Consultar entradas",
                    "Pesquise e exporte para Excel",
                    self.consulta_entradas_dialog,
                ),
                self._atalho(
                    ft.Icons.SETTINGS_OUTLINED,
                    "Impressora e rede",
                    "Configure impressora e acesso remoto",
                    self.abrir_dialogo_configuracao,
                ),
            ],
            spacing=16,
        )

        conteudo = ft.Column(
            expand=True,
            scroll=ft.ScrollMode.AUTO,
            spacing=20,
            controls=[formulario, atalhos],
        )

        corpo = ft.Container(
            expand=True,
            padding=ft.Padding.only(left=28, top=24, right=28, bottom=8),
            content=ft.Row(
                expand=True,
                spacing=24,
                vertical_alignment=ft.CrossAxisAlignment.STRETCH,
                controls=[conteudo, self._painel_visitantes()],
            ),
        )

        rodape = ft.Container(
            padding=ft.Padding.only(right=28, bottom=10),
            content=ft.Row(
                [ft.Text("Feito por: André luiz", size=12, italic=True, color=TEXTO_SUAVE)],
                alignment=ft.MainAxisAlignment.END,
            ),
        )

        page.add(
            ft.Column(
                expand=True,
                spacing=0,
                controls=[self._barra_superior(), corpo, rodape],
            )
        )
        self._aplicar_acesso_remoto(avisar=False)

    def _barra_superior(self) -> ft.Control:
        return ft.Container(
            bgcolor=CARTAO,
            padding=ft.Padding.symmetric(horizontal=28, vertical=12),
            shadow=ft.BoxShadow(blur_radius=12, color="#1F6B3A18", offset=ft.Offset(0, 2)),
            content=ft.Row(
                [
                    self._logo(),
                    ft.Container(width=1, height=44, bgcolor=BORDA),
                    ft.Column(
                        [
                            ft.Text(
                                "Controle de etiquetas",
                                size=22,
                                weight=ft.FontWeight.BOLD,
                                color=VERDE,
                            ),
                            ft.Text(
                                "Entrada de mercadorias e visitantes",
                                size=13,
                                color=TEXTO_SUAVE,
                            ),
                        ],
                        spacing=0,
                        expand=True,
                    ),
                    self._chip_remoto(),
                ],
                spacing=20,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _atalho(self, icone, titulo: str, descricao: str, acao, *, cor=VERDE, fundo=VERDE_SUAVE) -> ft.Control:
        return ft.Container(
            **estilo_cartao(20),
            expand=True,
            ink=True,
            on_click=lambda _: acao(),
            content=ft.Row(
                [
                    icone_destaque(icone, cor, fundo),
                    ft.Column(
                        [
                            ft.Text(titulo, size=15, weight=ft.FontWeight.W_600, color=TEXTO),
                            ft.Text(descricao, size=12, color=TEXTO_SUAVE),
                        ],
                        spacing=2,
                        expand=True,
                    ),
                    ft.Icon(ft.Icons.CHEVRON_RIGHT, color=TEXTO_SUAVE),
                ],
                spacing=14,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )

    def _chip_remoto(self) -> ft.Control:
        self.rotulo_remoto = ft.Text(size=13, weight=ft.FontWeight.W_500)
        self.ponto_remoto = ft.Container(width=8, height=8, border_radius=4)
        self.chip_remoto = ft.Container(
            border_radius=20,
            padding=ft.Padding.symmetric(horizontal=14, vertical=8),
            ink=True,
            on_click=lambda _: self.abrir_dialogo_configuracao(),
            content=ft.Row([self.ponto_remoto, self.rotulo_remoto], spacing=8, tight=True),
        )
        self._estilizar_chip_remoto()
        return self.chip_remoto

    def _estilizar_chip_remoto(self) -> None:
        ativo = self.config.remote_enabled
        self.rotulo_remoto.value = "Acesso remoto ativo" if ativo else "Acesso remoto desligado"
        self.rotulo_remoto.color = VERDE if ativo else TEXTO_SUAVE
        self.ponto_remoto.bgcolor = VERDE_FOLHA if ativo else "#A7B3A9"
        self.chip_remoto.bgcolor = VERDE_SUAVE if ativo else CREME
        self.chip_remoto.tooltip = (
            "Outros computadores da loja podem consultar pela rede"
            if ativo
            else "Clique para ativar a consulta pela rede"
        )

    def _atualizar_rotulo_remoto(self) -> None:
        if hasattr(self, "chip_remoto"):
            self._estilizar_chip_remoto()
            self.chip_remoto.update()

    def _aplicar_acesso_remoto(self, *, avisar: bool) -> None:
        erro = aplicar_acesso_remoto(
            self.config.remote_enabled,
            self.config.remote_port,
            self.config.remote_token,
        )
        if erro:
            self.config.remote_enabled = False
            if avisar:
                mostrar_snack(self.page, erro)
        elif avisar:
            if self.config.remote_enabled:
                mostrar_snack(self.page, "Acesso remoto disponível na rede local.")
            else:
                mostrar_snack(self.page, "Acesso remoto desligado.")
        self._atualizar_rotulo_remoto()

    def _ao_janela(self, evento) -> None:
        if getattr(evento, "type", None) == ft.WindowEventType.CLOSE:
            parar_servidor()

    def _logo(self) -> ft.Control:
        if LOGO_PATH.is_file():
            return ft.Image(
                src=str(LOGO_PATH),
                width=180,
                height=64,
                fit=ft.BoxFit.CONTAIN,
            )
        return ft.Container(width=180, height=64)

    def _ao_selecionar_ean(self, ean: str) -> None:
        registro = db.ultimo_por_ean(ean)
        if not registro:
            return
        self.descricao.value = str(registro["descricao"])
        self.descricao.campo.update()

    def gerar_etiqueta(self) -> None:
        ean = self.ean.aceitar()
        descricao = self.descricao.aceitar()
        colaborador = self.colaborador.aceitar()

        if not descricao or not colaborador:
            mostrar_snack(self.page, "Preencha a descrição e o colaborador.")
            return
        if ean and not ean_valido(ean):
            mostrar_snack(self.page, "EAN inválido. Use 8 a 14 dígitos ou um código de 3 a 32 caracteres.")
            return
        if not self._impressora_ok():
            return

        qtd_input = ft.TextField(label="Quantidade de etiquetas", value="1", **estilo_campo(320))

        def on_confirmar(_):
            try:
                quantidade = int((qtd_input.value or "").strip())
            except ValueError:
                mostrar_snack(self.page, "Digite um número válido para quantidade.")
                return
            if quantidade < 1 or quantidade > QUANTIDADE_MAXIMA:
                mostrar_snack(
                    self.page,
                    f"A quantidade deve ser entre 1 e {QUANTIDADE_MAXIMA}.",
                )
                return
            fechar_dialogo(dialogo)
            self._imprimir_lote_produto(ean, descricao, colaborador, quantidade)

        dialogo = abrir_dialogo(
            self.page,
            "Quantidade de etiquetas",
            qtd_input,
            confirmar="Gerar",
            ao_confirmar=on_confirmar,
        )

    def _imprimir_lote_produto(
        self,
        ean: str,
        descricao: str,
        colaborador: str,
        quantidade: int,
    ) -> None:
        mostrar_snack(self.page, f"Imprimindo {quantidade} etiqueta(s)...")
        config = replace(self.config)
        data_hora = agora_texto()

        def trabalho() -> None:
            erro = None
            try:
                imprimir_etiquetas_produto(
                    ean, descricao, colaborador, data_hora, quantidade, config
                )
                db.adicionar_etiquetas(ean, descricao, colaborador, data_hora, quantidade)
            except Exception as exc:
                erro = exc

            async def concluir() -> None:
                if erro:
                    mostrar_snack(self.page, f"Falha ao imprimir: {erro}")
                    return
                self.ean.value = ""
                self.descricao.value = ""
                self.colaborador.value = ""
                self.page.update()
                mostrar_snack(self.page, f"{quantidade} etiqueta(s) criadas com sucesso!")
                await self.ean.campo.focus()

            self.page.run_task(concluir)

        self.page.run_thread(trabalho)


def main(page: ft.Page) -> None:
    try:
        db.excluir_entradas_antigas()
    except sqlite3.Error:
        pass
    App(page)


if __name__ == "__main__":
    from pathlib import Path as _Path
    import traceback

    try:
        from paths import preparar_runtime

        preparar_runtime()
        ft.run(main)
    except Exception:
        texto = traceback.format_exc()
        try:
            pasta = _Path(sys.argv[0]).resolve().parent
            (pasta / "erro.log").write_text(texto, encoding="utf-8")
        except OSError:
            pasta = None
        try:
            import ctypes

            ctypes.windll.user32.MessageBoxW(
                0,
                "Não foi possível abrir o programa.\n\n"
                "Copie a pasta inteira do aplicativo (não só o .exe).\n"
                "Detalhes em erro.log, na mesma pasta.",
                "Etiquetas Hortifruti",
                0x10,
            )
        except Exception:
            pass
        raise
