import flet as ft

import tls
from print_label import listar_impressoras
from remote_access import urls_acesso
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
        if self._gerente_ativo() is None:  # defesa extra: nunca abre sem identificação
            self._exigir_gerente(self.abrir_dialogo_configuracao)
            return
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
        https_switch = ft.Switch(
            label="Usar HTTPS (conexão criptografada - recomendado)",
            value=self.config.remote_https,
        )
        url_texto = ft.Text(
            "\n".join(urls_acesso(int(self.config.remote_port or 8765), self.config.remote_https)),
            size=12,
            selectable=True,
            color=TEXTO,
        )
        impressao = ""
        try:
            impressao = tls.impressao_digital()
        except Exception:
            impressao = ""
        aviso_certificado = ft.Text(
            "Na primeira vez, o navegador avisa que o certificado não é de uma autoridade "
            "conhecida (ele é gerado neste computador). Confira a impressão digital abaixo "
            "antes de continuar:\n" + impressao
            if impressao
            else "O certificado de segurança é criado na primeira vez que o acesso remoto é ligado.",
            size=11,
            selectable=True,
            color=TEXTO_SUAVE,
        )

        def atualizar_url(_=None) -> None:
            try:
                porta = int((porta_input.value or "8765").strip())
            except ValueError:
                porta = 8765
            url_texto.value = "\n".join(urls_acesso(porta, bool(https_switch.value)))
            url_texto.update()

        https_switch.on_change = atualizar_url

        def copiar_url(_):
            texto = url_texto.value or ""

            async def copiar() -> None:
                await self._clipboard.set(texto)
                mostrar_snack(self.page, "Endereço copiado.")

            self.page.run_task(copiar)

        porta_input.on_change = atualizar_url

        auto_switch = ft.Switch(
            label="Procurar atualização ao abrir o programa e a cada hora",
            value=self.config.update_auto,
        )

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
            self.config.update_auto = bool(auto_switch.value)
            self.config.printer = dropdown_printer.value
            self.config.font_size = int(font_size_slider.value or 20)
            self.config.font_name = (font_input.value or "arial.ttf").strip()
            self.config.remote_enabled = bool(remoto_switch.value)
            self.config.remote_port = porta
            self.config.remote_https = bool(https_switch.value)
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
                        "Outros computadores da loja entram pelo navegador com usuário e senha. "
                        "Este programa precisa permanecer aberto. Libere a porta no Firewall do Windows se pedir.",
                        size=12,
                        color=TEXTO_SUAVE,
                    ),
                    remoto_switch,
                    porta_input,
                    https_switch,
                    aviso_certificado,
                    ft.Row(
                        [ft.TextButton("Copiar endereço", on_click=copiar_url)],
                        wrap=True,
                    ),
                    url_texto,
                    ft.Divider(color=BORDA),
                    titulo_secao("Atualizações"),
                    auto_switch,
                ],
                tight=True,
                spacing=12,
                width=420,
                height=600,
                scroll=ft.ScrollMode.AUTO,
            ),
            confirmar="Salvar",
            ao_confirmar=on_salvar,
        )
