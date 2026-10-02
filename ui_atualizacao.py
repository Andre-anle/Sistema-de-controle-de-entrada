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
from logs import log

ATRASO_VERIFICACAO_INICIAL = 4.0
INTERVALO_VERIFICACAO = 3600.0   # com o programa aberto, procura de hora em hora
CONTAGEM_AUTOMATICA = 60         # segundos de aviso antes de uma atualização [auto]
LIMITE_NOTAS = 1200


class AtualizacaoMixin:
    page: ft.Page
    config: AppSettings

    _dialogo_atualizacao_ref: ft.AlertDialog | None = None

    def repositorio_atualizacao(self) -> str:
        return atualizador.REPOSITORIO_GITHUB

    def versao_atual(self) -> str:
        """Versão em uso: a do .exe ou, se maior, a última que o atualizador instalou."""
        return atualizador.versao_em_uso(self.config.update_aplicada)

    def _dialogo_atualizacao_aberto(self) -> bool:
        dialogo = self._dialogo_atualizacao_ref
        return dialogo is not None and bool(dialogo.open)

    def iniciar_atualizacao_automatica(self) -> None:
        """Limpa restos de atualização anterior e, se configurado, procura versão nova
        ao abrir e depois de hora em hora, enquanto o programa estiver aberto."""
        registro = log("aplicacao")
        registro.info(
            "Versão do programa: %s (última atualização aplicada: %s)",
            atualizador.VERSAO, self.config.update_aplicada or "nenhuma",
        )
        if not atualizador.pode_atualizar():
            return
        atualizador.limpar_residuos()
        if not self.config.update_auto:
            return
        if not self.repositorio_atualizacao():
            return

        def verificar_sempre() -> None:
            time.sleep(ATRASO_VERIFICACAO_INICIAL)
            while True:
                # A opção pode ser desligada com o programa aberto.
                if self.config.update_auto and not self._dialogo_atualizacao_aberto():
                    try:
                        self.verificar_atualizacao(manual=False)
                    except Exception:
                        registro.exception("Falha ao verificar atualizações")
                time.sleep(INTERVALO_VERIFICACAO)

        threading.Thread(target=verificar_sempre, name="verifica-atualizacao", daemon=True).start()

    def verificar_atualizacao(self, manual: bool) -> None:
        try:
            repo = atualizador.normalizar_repositorio(self.repositorio_atualizacao())
        except ErroAtualizacao as exc:
            if manual:
                mostrar_snack(self.page, str(exc))
            return
        if manual:
            if self._dialogo_atualizacao_aberto():
                return
            mostrar_snack(self.page, "Verificando atualizações...")

        def trabalho() -> None:
            info: Atualizacao | None = None
            erro = ""
            atual = self.versao_atual()
            try:
                info = atualizador.buscar_atualizacao(repo, atual)
            except ErroAtualizacao as exc:
                erro = str(exc)
            except Exception as exc:
                erro = f"Falha ao verificar atualizações: {exc}"
            if erro:
                log("aplicacao").warning("Verificação de atualização falhou: %s", erro)

            async def concluir() -> None:
                if self._dialogo_atualizacao_aberto():
                    return
                if erro:
                    if manual:
                        mostrar_snack(self.page, erro)
                elif info is None:
                    if manual:
                        mostrar_snack(self.page, f"Você já está na versão mais recente ({atual}).")
                elif manual or info.automatica or info.versao != self.config.update_ignorada:
                    self._dialogo_atualizacao(info, automatica=info.automatica and not manual)

            self.page.run_task(concluir)

        self.page.run_thread(trabalho)

    def _dialogo_atualizacao(self, info: Atualizacao, automatica: bool = False) -> None:
        notas = info.notas.strip()
        if len(notas) > LIMITE_NOTAS:
            notas = notas[:LIMITE_NOTAS].rstrip() + "…"
        estado = ft.Text("", size=12, color=TEXTO_SUAVE)
        barra = ft.ProgressBar(value=0, color=VERDE, visible=False)
        controles: list[ft.Control] = [
            ft.Text(
                f"Versão instalada: {self.versao_atual()}   →   Nova versão: {info.versao}",
                size=14,
                color=TEXTO,
            ),
        ]
        contagem = ft.Text("", size=13, weight=ft.FontWeight.W_600, color=VERDE, visible=automatica)
        controles.append(contagem)
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
                atualizador.aplicar_e_reiniciar(pasta, info.versao)  # encerra o processo
            except ErroAtualizacao as exc:
                mensagem = str(exc)
            except Exception as exc:
                mensagem = f"Falha ao atualizar: {exc}"

            async def falha() -> None:
                mostrar_falha(mensagem)

            self.page.run_task(falha)

        iniciado = threading.Event()

        def confirmar(_) -> None:
            if iniciado.is_set():
                return
            iniciado.set()
            contagem.visible = False
            dialogo.actions = []
            barra.visible = True
            estado.value = "Preparando o download..."
            dialogo.update()
            self.page.run_thread(baixar_e_aplicar)

        def contar_e_atualizar() -> None:
            for restante in range(CONTAGEM_AUTOMATICA, 0, -1):
                if iniciado.is_set() or not dialogo.open:
                    return  # clicou em "Atualizar agora" ou adiou
                contagem.value = (
                    f"Atualização automática em {restante} s. "
                    "Salve o que estiver fazendo ou clique em \"Depois\" para adiar."
                )
                try:
                    contagem.update()
                except Exception:
                    return
                time.sleep(1)
            if not iniciado.is_set() and dialogo.open:
                log("aplicacao").info("Atualização automática para %s iniciada.", info.versao)
                confirmar(None)

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
        if not automatica:
            dialogo.actions.insert(1, ft.TextButton("Ignorar esta versão", on_click=ignorar))
        dialogo.update()
        self._dialogo_atualizacao_ref = dialogo
        if automatica:
            threading.Thread(target=contar_e_atualizar, name="atualizacao-automatica", daemon=True).start()
