import flet as ft

from print_label import listar_impressoras
from remote_access import gerar_chave, urls_acesso
from settings import AppSettings, impressora_disponivel, salvar_config
from ui_base import (
    BORDA,
    TEXTO,
    TEXTO_SUAVE,
    VERDE,
    VERDE_SUAVE,
    abrir_dialogo,
    estilo_campo,
    fechar_dialogo,
    mostrar_snack,
    titulo_secao,
)


class ConfigMixin:
    page: ft.Page
    config: AppSettings

    def _impressora_ok(self) -> bool:
        impressora = self.config.printer
        if not impressora:
            mostrar_snack(self.page, "Nenhuma impressora configurada. Escolha uma em Impressora.")
            return False
        if not impressora_disponivel(impressora):
            mostrar_snack(
                self.page,
                f"A impressora \"{impressora}\" não foi encontrada neste computador. "
                "Verifique se está instalada ou escolha outra em Impressora.",
            )
            return False
        return True

    def abrir_dialogo_configuracao(self) -> None:
        impressoras = listar_impressoras()
        ausente = bool(self.config.printer) and self.config.printer not in impressoras
        if ausente:
            impressoras = [self.config.printer, *impressoras]

        dropdown_printer = ft.Dropdown(
            label="Impressora",
            width=320,
            filled=True,
            fill_color=VERDE_SUAVE,
            border_radius=14,
            border_color=BORDA,
            focused_border_color=VERDE,
            options=[
                ft.DropdownOption(
                    key=nome,
                    text=f"{nome} (não encontrada)"
                    if ausente and nome == self.config.printer
                    else nome,
                )
                for nome in impressoras
            ],
            value=self.config.printer or (impressoras[0] if impressoras else None),
        )
        font_size_slider = ft.Slider(
            min=10,
            max=50,
            divisions=40,
            value=self.config.font_size,
            label="{value}",
            active_color=VERDE,
        )
        font_input = ft.TextField(
            label="Fonte (ex: arial.ttf)", value=self.config.font_name, **estilo_campo(320)
        )
        remoto_switch = ft.Switch(
            label="Permitir consulta e exportação pela rede",
            value=self.config.remote_enabled,
        )
        porta_input = ft.TextField(
            label="Porta",
            value=str(self.config.remote_port or 8765),
            **estilo_campo(160),
        )
        chave_atual = self.config.remote_token or gerar_chave()
        chave_input = ft.TextField(
            label="Chave de acesso",
            value=chave_atual,
            password=True,
            can_reveal_password=True,
            **estilo_campo(320),
        )
        url_texto = ft.Text(
            "\n".join(urls_acesso(int(self.config.remote_port or 8765), chave_atual)),
            size=12,
            selectable=True,
            color=TEXTO,
        )

        def atualizar_url(_=None) -> None:
            try:
                porta = int((porta_input.value or "8765").strip())
            except ValueError:
                porta = 8765
            chave = (chave_input.value or "").strip() or chave_atual
            url_texto.value = "\n".join(urls_acesso(porta, chave))
            url_texto.update()

        def nova_chave(_):
            chave_input.value = gerar_chave()
            chave_input.update()
            atualizar_url()

        def copiar_url(_):
            texto = url_texto.value or ""

            async def copiar() -> None:
                await self._clipboard.set(texto)
                mostrar_snack(self.page, "Endereço copiado.")

            self.page.run_task(copiar)

        porta_input.on_change = atualizar_url
        chave_input.on_change = atualizar_url

        def on_salvar(_):
            if not dropdown_printer.value:
                mostrar_snack(self.page, "Selecione uma impressora.")
                return
            try:
                porta = int((porta_input.value or "").strip())
            except ValueError:
                mostrar_snack(self.page, "Informe uma porta numérica.")
                return
            if porta < 1024 or porta > 65535:
                mostrar_snack(self.page, "A porta deve estar entre 1024 e 65535.")
                return
            self.config.printer = dropdown_printer.value
            self.config.font_size = int(font_size_slider.value or 20)
            self.config.font_name = (font_input.value or "arial.ttf").strip()
            self.config.remote_enabled = bool(remoto_switch.value)
            self.config.remote_port = porta
            self.config.remote_token = (chave_input.value or "").strip() or gerar_chave()
            salvar_config(self.config)
            fechar_dialogo(dialogo)
            self._aplicar_acesso_remoto(avisar=True)

        dialogo = abrir_dialogo(
            self.page,
            "Configurações",
            ft.Column(
                [
                    titulo_secao("Etiqueta"),
                    dropdown_printer,
                    ft.Text("Tamanho da fonte da etiqueta", size=12, color=TEXTO_SUAVE),
                    font_size_slider,
                    font_input,
                    ft.Divider(color=BORDA),
                    titulo_secao("Acesso remoto"),
                    ft.Text(
                        "Outros computadores da loja consultam e exportam pelo navegador. "
                        "Este programa precisa permanecer aberto. Libere a porta no Firewall do Windows se pedir.",
                        size=12,
                        color=TEXTO_SUAVE,
                    ),
                    remoto_switch,
                    porta_input,
                    chave_input,
                    ft.Row(
                        [
                            ft.TextButton("Nova chave", on_click=nova_chave),
                            ft.TextButton("Copiar endereço", on_click=copiar_url),
                        ],
                        wrap=True,
                    ),
                    url_texto,
                ],
                tight=True,
                spacing=12,
                width=420,
                height=460,
                scroll=ft.ScrollMode.AUTO,
            ),
            confirmar="Salvar",
            ao_confirmar=on_salvar,
        )
