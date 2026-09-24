import sqlite3
from dataclasses import replace

import flet as ft

import db
from print_label import imprimir_etiqueta_visitante
from settings import AppSettings
from ui_base import (
    ESPACO_CARTAO,
    LARANJA,
    TEXTO,
    TEXTO_SUAVE,
    VERDE,
    VERDE_SUAVE,
    CampoTexto,
    abrir_dialogo,
    agora_texto,
    estilo_campo,
    estilo_cartao,
    fechar_dialogo,
    formatar_data_hora_exibicao,
    mostrar_snack,
    titulo_secao,
)


class VisitantesMixin:
    page: ft.Page
    config: AppSettings

    def _painel_visitantes(self) -> ft.Control:
        self.contador_ativos = ft.Text("", size=13, color=TEXTO_SUAVE)
        self.lista_ativos = ft.Column(
            spacing=10,
            scroll=ft.ScrollMode.AUTO,
            height=420,
            horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
        )
        self._preencher_visitantes_ativos()
        return ft.Container(
            width=340,
            **estilo_cartao(ft.Padding.symmetric(vertical=ESPACO_CARTAO)),
            content=ft.Column(
                [
                    ft.Container(
                        padding=ft.Padding.symmetric(horizontal=ESPACO_CARTAO),
                        content=ft.Row(
                            [
                                ft.Column(
                                    [
                                        titulo_secao("Visitantes ativos"),
                                        self.contador_ativos,
                                    ],
                                    spacing=2,
                                    expand=True,
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.REFRESH,
                                    icon_color=VERDE,
                                    tooltip="Atualizar",
                                    on_click=lambda _: self._atualizar_visitantes_ativos(),
                                ),
                            ],
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    ),
                    self.lista_ativos,
                ],
                spacing=14,
                tight=True,
            ),
        )

    def _preencher_visitantes_ativos(self) -> None:
        ativos = db.listar_visitantes_ativos()
        self.contador_ativos.value = f"{len(ativos)} no momento"
        itens = (
            [self._cartao_visitante(visitante) for visitante in ativos]
            if ativos
            else [ft.Text("Nenhum visitante no momento.", color=TEXTO_SUAVE)]
        )
        self.lista_ativos.controls = [
            ft.Container(padding=ft.Padding.symmetric(horizontal=ESPACO_CARTAO), content=item)
            for item in itens
        ]

    def _atualizar_visitantes_ativos(self) -> None:
        self._preencher_visitantes_ativos()
        self.contador_ativos.update()
        self.lista_ativos.update()

    def _cartao_visitante(self, visitante) -> ft.Control:
        detalhes = [f"{visitante['empresa']} · {visitante['funcao']}"]
        if visitante["documento"]:
            detalhes.append(f"Documento: {visitante['documento']}")
        if visitante["autorizado_por"]:
            detalhes.append(f"Autorizado por: {visitante['autorizado_por']}")
        detalhes.append(f"Entrada: {formatar_data_hora_exibicao(visitante['entrada'])}")
        return ft.Container(
            bgcolor=VERDE_SUAVE,
            border_radius=14,
            padding=12,
            content=ft.Column(
                [
                    ft.Text(
                        visitante["nome"],
                        size=15,
                        weight=ft.FontWeight.W_600,
                        color=TEXTO,
                    ),
                    *[ft.Text(linha, size=12, color=TEXTO_SUAVE) for linha in detalhes],
                    ft.FilledButton(
                        "Dar saída",
                        icon=ft.Icons.LOGOUT,
                        bgcolor=LARANJA,
                        color="#FFFFFF",
                        on_click=lambda _, v=visitante: self._confirmar_saida(v),
                    ),
                ],
                spacing=4,
            ),
        )

    def _confirmar_saida(self, visitante) -> None:
        nome = visitante["nome"]

        def on_confirmar(_):
            fechar_dialogo(dialogo)
            try:
                db.registrar_saida_visitante(visitante["id"], agora_texto())
            except sqlite3.Error as exc:
                mostrar_snack(self.page, f"Falha ao registrar saída: {exc}")
                return
            self._atualizar_visitantes_ativos()
            mostrar_snack(self.page, f"Saída de {nome} registrada.")

        dialogo = abrir_dialogo(
            self.page,
            "Dar saída",
            ft.Text(f"Registrar a saída de {nome} agora?", color=TEXTO),
            confirmar="Confirmar",
            ao_confirmar=on_confirmar,
        )

    def adicionar_visitante_dialog(self) -> None:
        nome_input = CampoTexto("Nome do visitante", ft.Icons.PERSON_OUTLINE, largura=320)
        documento_input = ft.TextField(
            label="Documento (RG ou CPF)", **estilo_campo(320, ft.Icons.BADGE_OUTLINED)
        )
        funcao_input = ft.TextField(label="Função", **estilo_campo(320, ft.Icons.WORK_OUTLINE))
        empresa_input = ft.TextField(label="Empresa", **estilo_campo(320, ft.Icons.APARTMENT))
        autorizado_input = CampoTexto(
            "Autorizado por", ft.Icons.VERIFIED_USER_OUTLINED, largura=320
        )
        nome_input.on_enter = lambda: documento_input.focus()

        def on_adicionar(_):
            nome = nome_input.aceitar()
            documento = (documento_input.value or "").strip()
            funcao = (funcao_input.value or "").strip()
            empresa = (empresa_input.value or "").strip()
            autorizado_por = autorizado_input.aceitar()
            if not all((nome, documento, funcao, empresa, autorizado_por)):
                mostrar_snack(
                    self.page,
                    "Preencha nome, documento, função, empresa e quem autorizou.",
                )
                return
            if not self._impressora_ok():
                return
            fechar_dialogo(dialogo)
            self._imprimir_visitante(nome, documento, funcao, empresa, autorizado_por)

        autorizado_input.on_enter = lambda: on_adicionar(None)

        dialogo = abrir_dialogo(
            self.page,
            "Adicionar visitante",
            ft.Column(
                [
                    nome_input.view,
                    documento_input,
                    funcao_input,
                    empresa_input,
                    autorizado_input.view,
                ],
                tight=True,
                height=360,
                spacing=14,
            ),
            confirmar="Adicionar",
            ao_confirmar=on_adicionar,
        )

    def _imprimir_visitante(
        self,
        nome: str,
        documento: str,
        funcao: str,
        empresa: str,
        autorizado_por: str,
    ) -> None:
        mostrar_snack(self.page, "Imprimindo etiqueta de visitante...")
        config = replace(self.config)
        entrada = agora_texto()

        def trabalho() -> None:
            erro = None
            try:
                imprimir_etiqueta_visitante(nome, funcao, empresa, entrada, config)
                db.adicionar_visitante(
                    nome, documento, funcao, empresa, autorizado_por, entrada
                )
            except Exception as exc:
                erro = exc

            async def concluir() -> None:
                if erro:
                    mostrar_snack(self.page, f"Falha ao imprimir visitante: {erro}")
                    return
                self._atualizar_visitantes_ativos()
                mostrar_snack(self.page, "Etiqueta de visitante criada com sucesso!")

            self.page.run_task(concluir)

        self.page.run_thread(trabalho)
