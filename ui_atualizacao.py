import threading
import time

import flet as ft

import atualizador
from atualizador import Atualizacao, ErroAtualizacao
from remote_access import parar_servidor
from settings import AppSettings, salvar_config
from ui_base import (
    TEXTO,
    TEXTO_SUAVE,
    VERDE,
    abrir_dialogo,
    fechar_dialogo,
    mostrar_snack,
)
from versao import VERSAO

ATRASO_VERIFICACAO_INICIAL = 4.0
LIMITE_NOTAS = 1200


class AtualizacaoMixin:
    page: ft.Page
    config: AppSettings

    def repositorio_atualizacao(self) -> str:
        return atualizador.REPOSITORIO_GITHUB

    def iniciar_atualizacao_automatica(self) -> None:
        """Limpa restos de atualização anterior e, se configurado, procura versão nova."""
        if not atualizador.pode_atualizar():
            return
        atualizador.limpar_residuos()
        if not self.config.update_auto:
            return
        if not self.repositorio_atualizacao():
            return

        def esperar_e_verificar() -> None:
            time.sleep(ATRASO_VERIFICACAO_INICIAL)
            self.verificar_atualizacao(manual=False)

        threading.Thread(target=esperar_e_verificar, name="verifica-atualizacao", daemon=True).start()

    def verificar_atualizacao(self, manual: bool) -> None:
        try:
            repo = atualizador.normalizar_repositorio(self.repositorio_atualizacao())
        except ErroAtualizacao as exc:
            if manual:
                mostrar_snack(self.page, str(exc))
            return
        if manual:
            mostrar_snack(self.page, "Verificando atualizações...")

        def trabalho() -> None:
            info: Atualizacao | None = None
            erro = ""
            try:
                info = atualizador.buscar_atualizacao(repo)
            except ErroAtualizacao as exc:
                erro = str(exc)
            except Exception as exc:
                erro = f"Falha ao verificar atualizações: {exc}"

            async def concluir() -> None:
                if erro:
                    if manual:
                        mostrar_snack(self.page, erro)
                elif info is None:
                    if manual:
                        mostrar_snack(self.page, f"Você já está na versão mais recente ({VERSAO}).")
                elif manual or info.versao != self.config.update_ignorada:
                    self._dialogo_atualizacao(info)

            self.page.run_task(concluir)

        self.page.run_thread(trabalho)

    def _dialogo_atualizacao(self, info: Atualizacao) -> None:
        notas = info.notas.strip()
        if len(notas) > LIMITE_NOTAS:
            notas = notas[:LIMITE_NOTAS].rstrip() + "…"
        estado = ft.Text("", size=12, color=TEXTO_SUAVE)
        barra = ft.ProgressBar(value=0, color=VERDE, visible=False)
        controles: list[ft.Control] = [
            ft.Text(f"Versão instalada: {VERSAO}   →   Nova versão: {info.versao}", size=14, color=TEXTO),
        ]
        if notas:
            controles.append(ft.Text("Novidades", size=12, weight=ft.FontWeight.W_600, color=VERDE))
            controles.append(ft.Text(notas, size=12, color=TEXTO_SUAVE, selectable=True))
        controles.append(
            ft.Text(
                "Só os arquivos que mudaram são baixados. O programa fecha e reabre sozinho; "
                "o banco de dados e as configurações são mantidos. Se a nova versão não abrir, "
                "a anterior é restaurada automaticamente.",
                size=12,
                color=TEXTO_SUAVE,
            )
        )
        controles.extend([barra, estado])

        def mostrar_falha(mensagem: str) -> None:
            barra.visible = False
            estado.value = mensagem
            estado.color = "#9B2C2C"
            dialogo.actions = [ft.TextButton("Fechar", on_click=lambda _: fechar_dialogo(dialogo))]
            dialogo.update()

        def progresso(fracao: float, texto: str) -> None:
            barra.value = fracao
            estado.value = texto
            try:
                barra.update()
                estado.update()
            except Exception:
                pass

        def baixar_e_aplicar() -> None:
            mensagem = "Falha ao atualizar."
            try:
                pasta = atualizador.baixar_e_preparar(info, progresso)
                progresso(1.0, "Reiniciando o programa...")
                parar_servidor()
                atualizador.aplicar_e_reiniciar(pasta)  # encerra o processo
            except ErroAtualizacao as exc:
                mensagem = str(exc)
            except Exception as exc:
                mensagem = f"Falha ao atualizar: {exc}"

            async def falha() -> None:
                mostrar_falha(mensagem)

            self.page.run_task(falha)

        def confirmar(_) -> None:
            dialogo.actions = []
            barra.visible = True
            estado.value = "Preparando o download..."
            dialogo.update()
            self.page.run_thread(baixar_e_aplicar)

        def ignorar(_) -> None:
            self.config.update_ignorada = info.versao
            try:
                salvar_config(self.config)
            except OSError:
                pass
            fechar_dialogo(dialogo)
            mostrar_snack(self.page, f"A versão {info.versao} não será mais oferecida automaticamente.")

        dialogo = abrir_dialogo(
            self.page,
            "Nova versão disponível",
            ft.Column(controles, tight=True, spacing=12, width=440, scroll=ft.ScrollMode.AUTO),
            confirmar="Atualizar agora",
            ao_confirmar=confirmar,
            cancelar="Depois",
        )
        dialogo.actions.insert(1, ft.TextButton("Ignorar esta versão", on_click=ignorar))
        dialogo.update()
