from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import flet as ft

import db
from export_excel import COLUNAS_MERCADORIAS, COLUNAS_VISITANTES, gerar_planilha_entradas
from filtros import resumo_mercadorias, resumo_visitantes
from ui_base import (
    TEXTO,
    TEXTO_SUAVE,
    VERDE,
    VERDE_SUAVE,
    CampoData,
    CampoTexto,
    abrir_dialogo,
    fechar_dialogo,
    mostrar_snack,
)


def _tabela_resultados(
    destino: ft.Column,
    colunas: tuple[tuple[str, str, str], ...],
    registros: list,
    vazio: str,
) -> None:
    destino.controls.clear()
    if not registros:
        destino.controls.append(ft.Text(vazio, color=TEXTO))
    else:
        destino.controls.append(
            ft.Text(
                f"{len(registros)} registro(s) encontrado(s)",
                color=VERDE,
                weight=ft.FontWeight.W_600,
            )
        )
        destino.controls.append(
            ft.Row(
                [
                    ft.DataTable(
                        columns=[
                            ft.DataColumn(label=ft.Text(rotulo, weight=ft.FontWeight.W_600))
                            for rotulo, _, _ in colunas
                        ],
                        heading_row_color=VERDE_SUAVE,
                        column_spacing=28,
                        rows=[
                            ft.DataRow(
                                cells=[
                                    ft.DataCell(ft.Text(str(item[chave])))
                                    for _, chave, _ in colunas
                                ]
                            )
                            for item in registros
                        ],
                    )
                ],
                scroll=ft.ScrollMode.AUTO,
            )
        )
    destino.update()


def _area_resultados() -> ft.Column:
    return ft.Column(
        [ft.Text("Informe os filtros e clique em Pesquisar.", color=TEXTO_SUAVE)],
        scroll=ft.ScrollMode.AUTO,
        expand=True,
    )


def _linha_campos(controles: list[ft.Control]) -> ft.Row:
    return ft.Row(controles, wrap=True, spacing=10, run_spacing=10)


class ConsultaMixin:
    page: ft.Page

    def consulta_entradas_dialog(self) -> None:
        data_ini_merc = CampoData("Data/hora inicial")
        data_fim_merc = CampoData("Data/hora final")
        ean_filtro = CampoTexto("Mercadoria (EAN)", ft.Icons.QR_CODE_2, largura=220, somente_numeros=True)
        nome_merc = CampoTexto("Nome da mercadoria", ft.Icons.INVENTORY_2_OUTLINED, largura=220)
        colaborador_filtro = CampoTexto("Nome do colaborador", ft.Icons.PERSON_OUTLINE, largura=220)

        data_ini_vis = CampoData("Data/hora inicial")
        data_fim_vis = CampoData("Data/hora final")
        empresa_filtro = CampoTexto("Empresa", ft.Icons.APARTMENT, largura=220)
        nome_vis = CampoTexto("Nome do visitante", ft.Icons.BADGE_OUTLINED, largura=220)

        resultado_merc = _area_resultados()
        resultado_vis = _area_resultados()

        def filtros_mercadoria() -> dict[str, str]:
            return {
                "ean": ean_filtro.aceitar(),
                "descricao": nome_merc.aceitar(),
                "colaborador": colaborador_filtro.aceitar(),
                "data_inicio": data_ini_merc.value,
                "data_fim": data_fim_merc.value,
            }

        def filtros_visitante() -> dict[str, str]:
            return {
                "nome": nome_vis.aceitar(),
                "empresa": empresa_filtro.aceitar(),
                "data_inicio": data_ini_vis.value,
                "data_fim": data_fim_vis.value,
            }

        def pesquisar_mercadoria(_=None) -> None:
            try:
                resultados = db.consultar_entradas_produto(**filtros_mercadoria())
            except ValueError as exc:
                mostrar_snack(self.page, str(exc))
                return
            _tabela_resultados(
                resultado_merc,
                COLUNAS_MERCADORIAS,
                resultados,
                "Nenhuma entrada de mercadoria encontrada.",
            )

        def pesquisar_visitante(_=None) -> None:
            try:
                resultados = db.consultar_entradas_visitante(**filtros_visitante())
            except ValueError as exc:
                mostrar_snack(self.page, str(exc))
                return
            _tabela_resultados(
                resultado_vis,
                COLUNAS_VISITANTES,
                resultados,
                "Nenhuma entrada de visitante encontrada.",
            )

        colaborador_filtro.on_enter = pesquisar_mercadoria
        nome_vis.on_enter = pesquisar_visitante

        abas = ft.Tabs(
            length=2,
            expand=True,
            content=ft.Column(expand=True, spacing=8, controls=[]),
        )

        def abrir_exportacao(_=None) -> None:
            padrao = "visitantes" if abas.selected_index == 1 else "mercadorias"
            self._dialogo_exportar_planilha(padrao, filtros_mercadoria, filtros_visitante)

        def botoes(pesquisar: Callable) -> ft.Row:
            return ft.Row(
                [
                    ft.FilledButton(
                        "Pesquisar",
                        icon=ft.Icons.SEARCH,
                        bgcolor=VERDE,
                        on_click=pesquisar,
                    ),
                    ft.OutlinedButton(
                        "Exportar planilha",
                        icon=ft.Icons.TABLE_VIEW_OUTLINED,
                        on_click=abrir_exportacao,
                    ),
                ],
                spacing=10,
                wrap=True,
            )

        aba_mercadoria = ft.Column(
            [
                _linha_campos([data_ini_merc.view, data_fim_merc.view]),
                _linha_campos([ean_filtro.view, nome_merc.view, colaborador_filtro.view]),
                botoes(pesquisar_mercadoria),
                ft.Container(content=resultado_merc, expand=True),
            ],
            spacing=12,
            expand=True,
        )
        aba_visitante = ft.Column(
            [
                _linha_campos([data_ini_vis.view, data_fim_vis.view]),
                _linha_campos([empresa_filtro.view, nome_vis.view]),
                botoes(pesquisar_visitante),
                ft.Container(content=resultado_vis, expand=True),
            ],
            spacing=12,
            expand=True,
        )
        abas.content.controls = [
            ft.TabBar(
                tabs=[
                    ft.Tab(label="Entrada de mercadoria", icon=ft.Icons.INVENTORY_2_OUTLINED),
                    ft.Tab(label="Entrada de visitantes", icon=ft.Icons.BADGE_OUTLINED),
                ]
            ),
            ft.TabBarView(expand=True, controls=[aba_mercadoria, aba_visitante]),
        ]

        abrir_dialogo(
            self.page,
            "Consultar entradas",
            ft.Container(width=860, height=520, content=abas),
            cancelar="Fechar",
            scrollable=True,
        )

    def _dialogo_exportar_planilha(
        self,
        padrao: str,
        obter_filtros_merc: Callable[[], dict[str, str]],
        obter_filtros_vis: Callable[[], dict[str, str]],
    ) -> None:
        tipo = ft.RadioGroup(
            value=padrao if padrao in {"mercadorias", "visitantes"} else "ambos",
            content=ft.Column(
                [
                    ft.Radio(value="mercadorias", label="Mercadorias"),
                    ft.Radio(value="visitantes", label="Visitantes"),
                    ft.Radio(
                        value="ambos",
                        label="Ambos (mercadorias e visitantes em planilhas diferentes)",
                    ),
                ],
                spacing=4,
            ),
        )
        usar_filtros = ft.RadioGroup(
            value="filtros",
            content=ft.Column(
                [
                    ft.Radio(value="filtros", label="Usar os filtros preenchidos na consulta"),
                    ft.Radio(value="todos", label="Exportar todos os registros, sem filtro"),
                ],
                spacing=4,
            ),
        )

        def on_exportar(_):
            conteudo = tipo.value or "ambos"
            aplicar_filtros = (usar_filtros.value or "filtros") == "filtros"
            fechar_dialogo(dialogo)
            self.page.run_task(
                self._exportar_planilha,
                conteudo,
                obter_filtros_merc() if aplicar_filtros else {},
                obter_filtros_vis() if aplicar_filtros else {},
            )

        dialogo = abrir_dialogo(
            self.page,
            "Exportar planilha",
            ft.Column(
                [
                    ft.Text("O que deseja exportar?", weight=ft.FontWeight.W_600, color=TEXTO),
                    tipo,
                    ft.Container(height=8),
                    ft.Text("Filtros da exportação", weight=ft.FontWeight.W_600, color=TEXTO),
                    usar_filtros,
                ],
                tight=True,
                spacing=8,
                width=460,
                height=280,
            ),
            confirmar="Exportar",
            ao_confirmar=on_exportar,
        )

    async def _exportar_planilha(
        self,
        conteudo: str,
        filtros_merc: dict[str, str],
        filtros_vis: dict[str, str],
    ) -> None:
        try:
            mercadorias = None
            visitantes = None
            if conteudo in {"mercadorias", "ambos"}:
                mercadorias = db.consultar_entradas_produto(limite=None, **filtros_merc)
            if conteudo in {"visitantes", "ambos"}:
                visitantes = db.consultar_entradas_visitante(limite=None, **filtros_vis)
            dados = gerar_planilha_entradas(
                mercadorias,
                visitantes,
                resumo_mercadorias(filtros_merc) if mercadorias is not None else "",
                resumo_visitantes(filtros_vis) if visitantes is not None else "",
            )
        except ValueError as exc:
            mostrar_snack(self.page, str(exc))
            return
        except Exception as exc:
            mostrar_snack(self.page, f"Falha ao montar a planilha: {exc}")
            return

        nome = f"entradas_{datetime.now().strftime('%Y-%m-%d_%H%M')}.xlsx"
        area_trabalho = Path.home() / "Desktop"
        if not area_trabalho.is_dir():
            area_trabalho = Path.home() / "Área de Trabalho"
        pasta_inicial = str(area_trabalho if area_trabalho.is_dir() else Path.home())
        caminho = await self._seletor_arquivo.save_file(
            dialog_title="Salvar planilha Excel",
            file_name=nome,
            initial_directory=pasta_inicial,
            file_type=ft.FilePickerFileType.CUSTOM,
            allowed_extensions=["xlsx"],
        )
        if not caminho:
            mostrar_snack(self.page, "Exportação cancelada.")
            return
        destino = Path(caminho)
        if destino.suffix.lower() != ".xlsx":
            destino = destino.with_suffix(".xlsx")
        try:
            destino.write_bytes(dados)
        except OSError as exc:
            mostrar_snack(self.page, f"Não foi possível salvar o arquivo: {exc}")
            return
        mostrar_snack(self.page, f"Planilha salva em {destino}")
