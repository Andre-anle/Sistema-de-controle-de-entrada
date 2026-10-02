import sqlite3
import sys
import threading
from dataclasses import replace

import flet as ft

import acesso
import backup
import db
import logs
import migracoes
import validacao
from atualizador import confirmar_inicializacao, versao_recem_instalada
from paths import LOGO_PATH, ICONE_PATH
from print_label import imprimir_etiquetas_produto, impressora_padrao
from remote_access import aplicar_acesso_remoto, parar_servidor
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
from ui_atualizacao import AtualizacaoMixin
from ui_config import ConfigMixin
from ui_consulta import ConsultaMixin
from ui_seguranca import SegurancaMixin
from ui_visitantes import VisitantesMixin

QUANTIDADE_MAXIMA = validacao.QUANTIDADE_MAXIMA


class App(ConsultaMixin, VisitantesMixin, ConfigMixin, AtualizacaoMixin, SegurancaMixin):
    def __init__(self, page: ft.Page) -> None:
        self.page = page
        self.config = carregar_config()
        # Aberto pelo atualizador logo após a troca: registra a versão instalada.
        instalada = versao_recem_instalada()
        if instalada and instalada != self.config.update_aplicada:
            self.config.update_aplicada = instalada
            try:
                salvar_config(self.config)
            except OSError:
                logs.log("aplicacao").exception("Não foi possível gravar a versão instalada")
        if not self.config.printer:
            self.config.printer = impressora_padrao()

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
            somente_numeros=True,
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
                [
                    ft.TextButton(
                        f"Versão {self.versao_atual()} · Verificar atualização",
                        icon=ft.Icons.SYSTEM_UPDATE_ALT,
                        on_click=lambda _: self.verificar_atualizacao(manual=True),
                    ),
                    ft.Text("Feito por: André luiz", size=12, italic=True, color=TEXTO_SUAVE),
                ],
                alignment=ft.MainAxisAlignment.END,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
                spacing=16,
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
        self.iniciar_atualizacao_automatica()

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
            padding=ft.Padding.only(left=14, top=8, right=8, bottom=8),
            content=ft.Row(
                [
                    self.ponto_remoto,
                    self.rotulo_remoto,
                    ft.Icon(ft.Icons.ARROW_DROP_DOWN, size=20, color=TEXTO_SUAVE),
                ],
                spacing=8,
                tight=True,
                vertical_alignment=ft.CrossAxisAlignment.CENTER,
            ),
        )
        self._estilizar_chip_remoto()

        # Menu recolhido: abre ao clicar no indicador "Acesso remoto".
        # Os itens dependem de haver um gerente/administrador identificado.
        self.menu_remoto = ft.PopupMenuButton(
            content=self.chip_remoto,
            tooltip="Menu de gerência",
            menu_position=ft.PopupMenuPosition.UNDER,
            items=self._itens_menu_remoto(),
        )
        return self.menu_remoto

    def _itens_menu_remoto(self) -> list[ft.PopupMenuItem]:
        def item(icone, texto: str, acao) -> ft.PopupMenuItem:
            return ft.PopupMenuItem(
                icon=icone,
                content=ft.Text(texto, size=14, color=TEXTO),
                on_click=lambda _: acao(),
            )

        gerente = self._gerente_ativo()
        if gerente is None:
            return [
                item(
                    ft.Icons.LOCK_OUTLINE,
                    "Entrar como gerente ou administrador",
                    self.entrar_modo_gerente,
                ),
            ]
        return [
            item(ft.Icons.SETTINGS_OUTLINED, "Impressora e rede", self.abrir_configuracao_protegida),
            item(ft.Icons.SHIELD_OUTLINED, "Segurança e backup", self.abrir_seguranca_protegida),
            item(
                ft.Icons.LOGOUT,
                f"Encerrar acesso de gerência ({gerente.usuario})",
                self.sair_modo_gerente,
            ),
        ]

    def _atualizar_menu_remoto(self) -> None:
        menu = getattr(self, "menu_remoto", None)
        if menu is None:
            return
        menu.items = self._itens_menu_remoto()
        menu.update()

    def _estilizar_chip_remoto(self) -> None:
        ativo = self.config.remote_enabled
        self.rotulo_remoto.value = "Acesso remoto ativo" if ativo else "Acesso remoto desligado"
        self.rotulo_remoto.color = VERDE if ativo else TEXTO_SUAVE
        self.ponto_remoto.bgcolor = VERDE_FOLHA if ativo else "#A7B3A9"
        self.chip_remoto.bgcolor = VERDE_SUAVE if ativo else CREME

    def _atualizar_rotulo_remoto(self) -> None:
        if hasattr(self, "chip_remoto"):
            self._estilizar_chip_remoto()
            self.chip_remoto.update()

    def _aplicar_acesso_remoto(self, *, avisar: bool) -> None:
        erro = aplicar_acesso_remoto(
            self.config.remote_enabled,
            self.config.remote_port,
            https=self.config.remote_https,
            redes_extras=tuple(self.config.remote_redes),
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
            backup.parar_agendador()
            try:
                db.finalizar_uso()
            except Exception:
                logs.log("aplicacao").exception("Falha ao finalizar o banco")

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
        try:
            ean = validacao.validar_ean_opcional(ean)
            descricao = validacao.validar_descricao(descricao)
            colaborador = validacao.validar_colaborador(colaborador)
        except ValueError as exc:
            mostrar_snack(self.page, str(exc))
            return
        if not self._impressora_ok():
            return

        qtd_input = ft.TextField(label="Quantidade de etiquetas", value="1", **estilo_campo(320))

        def on_confirmar(_):
            try:
                quantidade = validacao.validar_quantidade(qtd_input.value)
            except ValueError as exc:
                mostrar_snack(self.page, str(exc))
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


def _erro_fatal(page: ft.Page, mensagem: str) -> None:
    page.add(
        ft.Container(
            padding=32,
            content=ft.Column(
                [
                    ft.Text("Não foi possível iniciar", size=22, weight=ft.FontWeight.BOLD, color=VERDE),
                    ft.Text(mensagem, size=15, color=TEXTO, selectable=True),
                ],
                spacing=12,
            ),
        )
    )


def _manutencao(config, avisar) -> None:
    """Tarefas pesadas fora da thread da interface: integridade, backup, retenção."""
    registro = logs.log("manutencao")
    try:
        integro, detalhe = db.verificar_integridade()
        if not integro:
            registro.critical("Banco de dados com problema: %s", detalhe)
            avisar(
                "O banco de dados apresentou problemas de integridade. "
                "Consulte 'Segurança e backup' e restaure um backup, se necessário."
            )
        if config.backup_auto:
            idade = backup.idade_ultimo_horas()
            if idade is None or idade >= 24:
                # Backup ANTES da limpeza por retenção.
                backup.criar_backup("automatico", config.backup_manter)
        removidos = db.excluir_entradas_antigas()
        if removidos and any(removidos.values()):
            acesso.registrar_evento(
                "retencao", "Registros antigos removidos: " + ", ".join(f"{k}={v}" for k, v in removidos.items())
            )
        if config.backup_auto:
            backup.iniciar_agendador(24, config.backup_manter)
    except Exception:
        registro.exception("Falha na manutenção em segundo plano")


def main(page: ft.Page) -> None:
    logs.configurar()
    registro = logs.log("aplicacao")
    registro.info("Aplicativo iniciado")
    senha_inicial = None
    try:
        senha_inicial = acesso.garantir_admin_padrao()
    except migracoes.ErroMigracao as exc:
        registro.critical("Banco incompatível: %s", exc)
        _erro_fatal(page, str(exc))
        return
    except sqlite3.Error:
        registro.exception("Falha ao preparar o banco de dados")

    codigo_backup = None
    try:
        _, chave_nova = backup.obter_chave()
        if chave_nova:
            codigo_backup = backup.codigo_recuperacao()
    except Exception:
        registro.exception("Não foi possível preparar a chave de backup")

    app = App(page)

    def avisar(mensagem: str) -> None:
        async def mostrar() -> None:
            mostrar_snack(page, mensagem)

        page.run_task(mostrar)

    threading.Thread(
        target=_manutencao, args=(app.config, avisar), name="manutencao", daemon=True
    ).start()
    confirmar_inicializacao()
    if senha_inicial or codigo_backup:
        app.mostrar_primeiros_passos(senha_inicial, codigo_backup)


def _restaurar_pela_linha_de_comando(argumentos: list[str]) -> int:
    """`Etiquetas.exe --restaurar <arquivo.ehb> [codigo]` (app fechado)."""
    import ctypes
    from pathlib import Path

    def aviso(texto: str, icone: int) -> None:
        ctypes.windll.user32.MessageBoxW(0, texto, "Etiquetas Hortifruti", icone)

    if not argumentos:
        aviso("Uso: --restaurar <arquivo.ehb> [codigo-de-recuperacao]", 0x10)
        return 2
    try:
        antigo = backup.restaurar(Path(argumentos[0]), argumentos[1] if len(argumentos) > 1 else None)
    except Exception as exc:
        aviso(f"Falha ao restaurar:\n\n{exc}", 0x10)
        return 1
    aviso(f"Backup restaurado com sucesso.\n\nO banco anterior foi guardado em:\n{antigo}", 0x40)
    return 0


if __name__ == "__main__":
    from pathlib import Path as _Path
    import traceback

    try:
        from paths import preparar_runtime

        preparar_runtime()
        if len(sys.argv) > 1 and sys.argv[1] == "--restaurar":
            logs.configurar()
            sys.exit(_restaurar_pela_linha_de_comando(sys.argv[2:]))
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
