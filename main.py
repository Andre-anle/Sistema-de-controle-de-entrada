import sqlite3
import sys
import ssl
from dataclasses import replace
ssl._create_default_https_context = ssl._create_unverified_context

import flet as ft

import db
from paths import LOGO_PATH, ICONE_PATH
from print_label import imprimir_etiquetas_produto, impressora_padrao
from remote_access import aplicar_acesso_remoto, gerar_chave, parar_servidor
from settings import carregar_config, salvar_config
from ui_base import (
    LARANJA,
    TEXTO_SUAVE,
    VERDE,
    CampoTexto,
    abrir_dialogo,
    agora_texto,
    aplicar_tema,
    estilo_campo,
    estilo_cartao,
    fechar_dialogo,
    mostrar_snack,
    titulo_secao,
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
        )
        self.descricao = CampoTexto(
            "Descrição do produto",
            ft.Icons.INVENTORY_2_OUTLINED,
            on_enter=lambda: self.colaborador.campo.focus(),
        )
        self.colaborador = CampoTexto(
            "Nome do colaborador",
            ft.Icons.PERSON_OUTLINE,
            on_enter=self.gerar_etiqueta,
        )

        formulario = ft.Container(
            **estilo_cartao(),
            content=ft.Column(
                [
                    titulo_secao("Nova etiqueta"),
                    self.ean.view,
                    self.descricao.view,
                    self.colaborador.view,
                    ft.Container(height=8),
                    ft.Row(
                        controls=[
                            ft.FilledButton(
                                "Gerar etiqueta",
                                icon=ft.Icons.PRINT,
                                bgcolor=VERDE,
                                on_click=lambda _: self.gerar_etiqueta(),
                            ),
                            ft.OutlinedButton(
                                "Consultar entradas",
                                icon=ft.Icons.SEARCH,
                                on_click=lambda _: self.consulta_entradas_dialog(),
                            ),
                            ft.OutlinedButton(
                                "Visitante",
                                icon=ft.Icons.PERSON_ADD_ALT,
                                on_click=lambda _: self.adicionar_visitante_dialog(),
                            ),
                            ft.OutlinedButton(
                                "Impressora",
                                icon=ft.Icons.SETTINGS_OUTLINED,
                                on_click=lambda _: self.abrir_dialogo_configuracao(),
                            ),
                        ],
                        wrap=True,
                        spacing=10,
                        run_spacing=10,
                    ),
                ],
                spacing=14,
            ),
        )

        conteudo = ft.Column(
            expand=True,
            scroll=ft.ScrollMode.AUTO,
            spacing=18,
            controls=[
                ft.Row(
                    [
                        ft.Column(
                            [
                                ft.Text(
                                    "HORTIFRUTI NATURAL DA TERRA",
                                    size=13,
                                    weight=ft.FontWeight.W_600,
                                    color=LARANJA,
                                ),
                                ft.Text(
                                    "Controle de etiquetas",
                                    size=32,
                                    weight=ft.FontWeight.BOLD,
                                    color=VERDE,
                                ),
                                ft.Text(
                                    "Controle de entrada de mercadorias e visitantes.",
                                    size=15,
                                    color=TEXTO_SUAVE,
                                ),
                                self._texto_remoto(),
                            ],
                            spacing=6,
                            expand=True,
                        ),
                        self._logo(),
                    ],
                    alignment=ft.MainAxisAlignment.SPACE_BETWEEN,
                    vertical_alignment=ft.CrossAxisAlignment.CENTER,
                ),
                formulario,
                ft.Container(height=28),
            ],
        )

        credito = ft.Text(
            "Feito por: André luiz",
            size=12,
            italic=True,
            color=TEXTO_SUAVE,
            right=16,
            bottom=10,
        )

        painel_visitantes = ft.Container(
            padding=ft.Padding.only(top=28, right=28, bottom=28),
            content=ft.Column(
                [self._painel_visitantes()],
                alignment=ft.MainAxisAlignment.CENTER,
            ),
        )

        page.add(
            ft.Stack(
                expand=True,
                controls=[
                    ft.Row(
                        expand=True,
                        spacing=0,
                        vertical_alignment=ft.CrossAxisAlignment.STRETCH,
                        controls=[
                            ft.Container(expand=True, padding=28, content=conteudo),
                            painel_visitantes,
                        ],
                    ),
                    credito,
                ],
            )
        )
        self._aplicar_acesso_remoto(avisar=False)

    def _texto_remoto(self) -> ft.Text:
        self.rotulo_remoto = ft.Text(
            self._mensagem_remoto(),
            size=13,
            color=VERDE if self.config.remote_enabled else TEXTO_SUAVE,
            selectable=True,
        )
        return self.rotulo_remoto

    def _mensagem_remoto(self) -> str:
        if not self.config.remote_enabled:
            return "Acesso remoto desligado. Ative em Impressora para consultar pela rede."
        return "Acesso remoto ativo"

    def _atualizar_rotulo_remoto(self) -> None:
        if hasattr(self, "rotulo_remoto"):
            self.rotulo_remoto.value = self._mensagem_remoto()
            self.rotulo_remoto.color = VERDE if self.config.remote_enabled else TEXTO_SUAVE
            self.rotulo_remoto.update()

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
                width=420,
                height=210,
                fit=ft.BoxFit.CONTAIN,
            )
        return ft.Container(width=420, height=210)

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
