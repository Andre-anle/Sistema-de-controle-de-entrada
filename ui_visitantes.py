import sqlite3
from dataclasses import replace

import flet as ft

import db
from print_label import imprimir_etiqueta_visitante
from settings import AppSettings
from ui_base import (
    BORDA,
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
    icone_destaque,
    mostrar_snack,
)


class VisitantesMixin:
    page: ft.Page
    config: AppSettings

    def _painel_visitantes(self) -> ft.Control:
        self.contador_ativos = ft.Text("", size=12, weight=ft.FontWeight.W_700, color="#FFFFFF")
        self.lista_ativos = ft.Column(
            spacing=10,
            scroll=ft.ScrollMode.AUTO,
            expand=True,
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
                                icone_destaque(ft.Icons.GROUPS_OUTLINED),
                                ft.Row(
                                    [
                                        ft.Text(
                                            "Visitantes",
                                            size=17,
                                            weight=ft.FontWeight.W_700,
                                            color=TEXTO,
                                        ),
                                        ft.Container(
                                            bgcolor=VERDE,
                                            border_radius=10,
                                            padding=ft.Padding.symmetric(horizontal=8, vertical=2),
                                            content=self.contador_ativos,
                                            tooltip="Visitantes na loja agora",
                                        ),
                                    ],
                                    spacing=8,
                                    expand=True,
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.REFRESH,
                                    icon_color=TEXTO_SUAVE,
                                    tooltip="Atualizar",
                                    on_click=lambda _: self._atualizar_visitantes_ativos(),
                                ),
                                ft.IconButton(
                                    icon=ft.Icons.PERSON_ADD_ALT,
                                    icon_color=VERDE,
                                    tooltip="Registrar visitante",
                                    on_click=lambda _: self.adicionar_visitante_dialog(),
                                ),
                            ],
                            spacing=10,
                            vertical_alignment=ft.CrossAxisAlignment.CENTER,
                        ),
                    ),
                    ft.Divider(height=1, color=BORDA),
                    self.lista_ativos,
                ],
                spacing=14,
            ),
        )

    def _preencher_visitantes_ativos(self) -> None:
        ativos = db.listar_visitantes_ativos()
        self.contador_ativos.value = str(len(ativos))
        if ativos:
            self.lista_ativos.alignment = ft.MainAxisAlignment.START
            self.lista_ativos.controls = [
                ft.Container(
                    padding=ft.Padding.symmetric(horizontal=ESPACO_CARTAO),
                    content=self._cartao_visitante(visitante),
                )
                for visitante in ativos
            ]
        else:
            self.lista_ativos.alignment = ft.MainAxisAlignment.CENTER
            self.lista_ativos.controls = [self._sem_visitantes()]

    def _sem_visitantes(self) -> ft.Control:
        return ft.Column(
            [
                ft.Icon(ft.Icons.EMOJI_PEOPLE, size=56, color=BORDA),
                ft.Text(
                    "Nenhum visitante na loja",
                    size=15,
                    weight=ft.FontWeight.W_600,
                    color=TEXTO,
                ),
                ft.Text(
                    "Quem entrar aparece aqui até\nregistrar a saída.",
                    size=13,
                    color=TEXTO_SUAVE,
                    text_align=ft.TextAlign.CENTER,
                ),
                ft.Container(height=4),
                ft.OutlinedButton(
                    "Registrar visitante",
                    icon=ft.Icons.PERSON_ADD_ALT,
                    on_click=lambda _: self.adicionar_visitante_dialog(),
                ),
            ],
            spacing=6,
            horizontal_alignment=ft.CrossAxisAlignment.CENTER,
        )

    def _atualizar_visitantes_ativos(self) -> None:
        self._preencher_visitantes_ativos()
        self.contador_ativos.update()
        self.lista_ativos.update()

    def _cartao_visitante(self, visitante) -> ft.Control:
        detalhes = [
            (ft.Icons.APARTMENT, f"{visitante['empresa']} · {visitante['funcao']}"),
        ]
        if visitante["documento"]:
            detalhes.append((ft.Icons.BADGE_OUTLINED, visitante["documento"]))
        if visitante["autorizado_por"]:
            detalhes.append((ft.Icons.VERIFIED_USER_OUTLINED, f"Autorizado por {visitante['autorizado_por']}"))
        detalhes.append((ft.Icons.LOGIN, f"Entrada {formatar_data_hora_exibicao(visitante['entrada'])}"))
        nome = str(visitante["nome"])
        return ft.Container(
            bgcolor=VERDE_SUAVE,
            border_radius=16,
            padding=14,
            content=ft.Column(
                [
                    ft.Row(
                        [
                            ft.CircleAvatar(
                                content=ft.Text(
                                    nome[:1].upper(),
                                    weight=ft.FontWeight.W_700,
                                    color="#FFFFFF",
                                ),
                                bgcolor=VERDE,
                                radius=18,
                            ),
                            ft.Text(
                                nome,
                                size=15,
                                weight=ft.FontWeight.W_600,
                                color=TEXTO,
                                expand=True,
                                max_lines=2,
                            ),
                        ],
                        spacing=10,
                    ),
                    *[
                        ft.Row(
                            [
                                ft.Icon(icone, size=14, color=TEXTO_SUAVE),
                                ft.Text(texto, size=12, color=TEXTO_SUAVE, expand=True),
                            ],
                            spacing=6,
                        )
                        for icone, texto in detalhes
                    ],
                    ft.Container(height=2),
                    ft.OutlinedButton(
                        "Dar saída",
                        icon=ft.Icons.LOGOUT,
                        style=ft.ButtonStyle(
                            color=LARANJA,
                            side=ft.BorderSide(1, LARANJA),
                            shape=ft.RoundedRectangleBorder(radius=12),
                        ),
                        on_click=lambda _, v=visitante: self._confirmar_saida(v),
                    ),
                ],
                spacing=6,
                horizontal_alignment=ft.CrossAxisAlignment.STRETCH,
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
