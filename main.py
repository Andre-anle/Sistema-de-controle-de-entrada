import asyncio
import time
from datetime import datetime
from collections.abc import Callable
from pathlib import Path

import flet as ft

import db
from export_excel import gerar_planilha_entradas
from paths import LOGO_PATH, ICONE_PATH
from print_label import (
    imprimir_etiqueta_visitante,
    imprimir_etiquetas_produto,
    impressora_padrao,
    listar_impressoras,
)
from remote_access import (
    aplicar_acesso_remoto,
    gerar_chave,
    parar_servidor,
    urls_acesso,
)
from settings import AppSettings, carregar_config, salvar_config

QUANTIDADE_MAXIMA = 50

VERDE = "#1F6B3A"
VERDE_FOLHA = "#2F8A4A"
VERDE_SUAVE = "#E7F3EA"
CREME = "#F3F0E6"
CARTAO = "#FFFDF8"
LARANJA = "#E07A2F"
TEXTO = "#243028"
TEXTO_SUAVE = "#5B6F60"
BORDA = "#C9D6C3"


def resumo_filtros(itens: list[tuple[str, str]]) -> str:
    ativos = [f"{nome}: {valor.strip()}" for nome, valor in itens if (valor or "").strip()]
    return " | ".join(ativos) if ativos else "Nenhum (todos os registros)"


def aplicar_tema(page: ft.Page) -> None:
    page.theme_mode = ft.ThemeMode.LIGHT
    page.bgcolor = CREME
    page.theme = ft.Theme(
        use_material3=True,
        color_scheme_seed=VERDE,
        color_scheme=ft.ColorScheme(
            primary=VERDE,
            on_primary="#FFFFFF",
            primary_container="#CDEAD4",
            on_primary_container="#0F3B22",
            secondary=LARANJA,
            on_secondary="#FFFFFF",
            surface=CARTAO,
            on_surface=TEXTO,
            on_surface_variant=TEXTO_SUAVE,
            outline=BORDA,
        ),
    )


def mostrar_snack(page: ft.Page, mensagem: str) -> None:
    page.show_dialog(
        ft.SnackBar(
            content=ft.Text(mensagem, color="#FFFFFF"),
            bgcolor=VERDE,
        )
    )


def fechar_dialogo(page: ft.Page) -> None:
    page.pop_dialog()


def abrir_dialogo(page: ft.Page, dialogo: ft.AlertDialog) -> None:
    dialogo.modal = True
    page.show_dialog(dialogo)


def ean_valido(ean: str) -> bool:
    limpo = ean.strip()
    if not limpo:
        return False
    if limpo.isdigit():
        return 8 <= len(limpo) <= 14
    return 3 <= len(limpo) <= 32


def estilo_campo(largura: int = 420, icone=None) -> dict:
    estilo = {
        "filled": True,
        "fill_color": VERDE_SUAVE,
        "bgcolor": VERDE_SUAVE,
        "color": TEXTO,
        "cursor_color": VERDE,
        "border_radius": 14,
        "border_color": BORDA,
        "focused_border_color": VERDE,
        "focused_bgcolor": "#FFFFFF",
        "content_padding": ft.Padding.symmetric(horizontal=16, vertical=14),
        "label_style": ft.TextStyle(color=TEXTO_SUAVE, size=14, weight=ft.FontWeight.W_500),
        "text_size": 15,
        "width": largura,
    }
    if icone is not None:
        estilo["prefix_icon"] = icone
    return estilo


def _formatar_hora(digitos: str) -> str:
    hora = digitos[:4]
    if len(hora) <= 2:
        return hora
    return f"{hora[:2]}:{hora[2:]}"


def _formatar_so_data(digitos: str) -> str:
    data = "".join(c for c in digitos if c.isdigit())[:8]
    if not data:
        return ""
    if len(data) <= 2:
        return data
    if len(data) <= 4:
        return f"{data[:2]}/{data[2:]}"
    if len(data) <= 6:
        return f"{data[:2]}/{data[2:4]}/{data[4:]}"
    return f"{data[:2]}/{data[2:4]}/{data[4:]}"


def formatar_data_hora_digitada(texto: str) -> str:
    bruto = texto or ""
    limpo = bruto.strip()
    if not limpo:
        return ""
    for formato, saida in (
        ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M"),
        ("%Y-%m-%d %H:%M", "%d/%m/%Y %H:%M"),
        ("%Y-%m-%d", "%d/%m/%Y"),
        ("%d-%m-%Y %H:%M", "%d/%m/%Y %H:%M"),
        ("%d-%m-%Y", "%d/%m/%Y"),
        ("%d-%m-%y", "%d/%m/%y"),
    ):
        try:
            return datetime.strptime(limpo, formato).strftime(saida)
        except ValueError:
            continue

    if " " in limpo:
        data_bruta, _, hora_bruta = limpo.partition(" ")
        data = _formatar_so_data(data_bruta)
        hora_digitos = "".join(c for c in hora_bruta if c.isdigit())[:4]
        if not hora_digitos:
            return f"{data} "
        return f"{data} {_formatar_hora(hora_digitos)}"

    digitos = "".join(c for c in limpo if c.isdigit())[:12]
    if len(digitos) <= 6:
        return _formatar_so_data(digitos)
    if len(digitos) <= 8:
        return _formatar_so_data(digitos)
    return f"{_formatar_so_data(digitos[:8])} {_formatar_hora(digitos[8:])}"


def melhor_sugestao(texto: str, opcoes: list[str]) -> str | None:
    termo = texto.strip().lower()
    if not termo:
        return None
    prefixos = [item for item in opcoes if item.lower().startswith(termo)]
    if prefixos:
        return sorted(prefixos, key=lambda item: (len(item), item.lower()))[0]
    parciais = [item for item in opcoes if termo in item.lower()]
    return sorted(parciais, key=lambda item: (len(item), item.lower()))[0] if parciais else None


class CampoComSugestao:
    def __init__(
        self,
        rotulo: str,
        icone,
        obter_opcoes: Callable[[], list[str]],
        on_escolha: Callable[[str], None] | None = None,
        on_enter: Callable[[], None] | None = None,
        largura: int = 480,
    ) -> None:
        self.obter_opcoes = obter_opcoes
        self.on_escolha = on_escolha
        self.on_enter = on_enter
        self.focado = False
        self._atualizando = False
        self._bloquear_completar = False
        self._digitado = ""
        self._valor_exibido = ""
        self._sugestao: str | None = None
        self._geracao = 0
        self._ultimo_input = 0.0
        self._entrada_rapida = False
        self.campo = ft.TextField(
            label=rotulo,
            on_change=self._ao_digitar,
            on_submit=self._ao_enter,
            on_focus=self._ao_focar,
            on_blur=self._ao_sair,
            **estilo_campo(largura, icone),
        )
        self.view = self.campo

    @property
    def value(self) -> str:
        return self.campo.value or ""

    @value.setter
    def value(self, valor: str) -> None:
        self.campo.value = valor
        self._digitado = valor
        self._valor_exibido = valor
        self._sugestao = None
        self._bloquear_completar = False
        self._geracao += 1
        self._entrada_rapida = False

    def _cancelar_sugestao_pendente(self) -> None:
        self._geracao += 1

    def _ao_focar(self, _=None) -> None:
        self.focado = True

    def _ao_sair(self, _=None) -> None:
        self.focado = False
        self._cancelar_sugestao_pendente()
        self._rejeitar_sugestao()

    def _rejeitar_sugestao(self) -> None:
        if self._sugestao is None:
            return
        restaurar = self._digitado
        self._sugestao = None
        atual = self.campo.value or ""
        if atual == restaurar:
            self._valor_exibido = restaurar
            return
        self._atualizando = True
        self.campo.value = restaurar
        self._valor_exibido = restaurar
        self.campo.selection = ft.TextSelection(
            base_offset=len(restaurar),
            extent_offset=len(restaurar),
        )
        self.campo.update()
        self._atualizando = False

    def marcar_exclusao(self) -> None:
        self._bloquear_completar = True

    def _ao_digitar(self, _=None) -> None:
        if self._atualizando:
            return
        atual = self.campo.value or ""
        apagou = self._bloquear_completar or len(atual) < len(self._digitado)
        self._bloquear_completar = False
        agora = time.monotonic()
        if self._ultimo_input and 0 < (agora - self._ultimo_input) < 0.08:
            self._entrada_rapida = True
        self._ultimo_input = agora
        self._digitado = atual
        self._valor_exibido = atual
        self._sugestao = None
        self._cancelar_sugestao_pendente()
        if apagou or not atual.strip():
            return
        geracao = self._geracao
        pagina = self.campo.page
        if pagina is None:
            return

        async def aplicar_depois() -> None:
            await asyncio.sleep(0.45)
            if geracao != self._geracao or not self.focado:
                return
            if self._entrada_rapida:
                self._entrada_rapida = False
                return
            self._aplicar_sugestao()

        pagina.run_task(aplicar_depois)

    def _aplicar_sugestao(self) -> None:
        if self._atualizando or not self.focado:
            return
        atual = self.campo.value or ""
        if atual != self._digitado:
            return
        sugestao = melhor_sugestao(atual, self.obter_opcoes())
        self._sugestao = sugestao
        if not sugestao or sugestao.lower() == atual.lower():
            self._valor_exibido = atual
            return
        self._atualizando = True
        self.campo.value = sugestao
        self.campo.selection = ft.TextSelection(
            base_offset=len(atual),
            extent_offset=len(sugestao),
        )
        self._valor_exibido = sugestao
        self.campo.update()
        self._atualizando = False

    def _sugestao_esta_selecionada(self) -> bool:
        if not self._sugestao:
            return False
        selecao = self.campo.selection
        if selecao is None:
            return False
        return selecao.base_offset != selecao.extent_offset

    def aceitar(self) -> str:
        self._cancelar_sugestao_pendente()
        if self._sugestao_esta_selecionada():
            escolhido = self._sugestao or self.value.strip()
        else:
            escolhido = self.value.strip()
            self._sugestao = None
        if escolhido:
            self._atualizando = True
            self.campo.value = escolhido
            self._digitado = escolhido
            self._valor_exibido = escolhido
            self.campo.selection = ft.TextSelection(
                base_offset=len(escolhido),
                extent_offset=len(escolhido),
            )
            self.campo.update()
            self._atualizando = False
            if self.on_escolha:
                self.on_escolha(escolhido)
        self._sugestao = None
        return escolhido

    def _ao_enter(self, _=None) -> None:
        self.aceitar()
        if self.on_enter:
            self.on_enter()


class App:
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
        page.on_keyboard_event = self._ao_teclado
        page.window.on_event = self._ao_janela
        self._campos_dialogo: list[CampoComSugestao] = []
        self._seletor_arquivo = ft.FilePicker()
        self._clipboard = ft.Clipboard()

        self.ean = CampoComSugestao(
            "EAN do produto",
            ft.Icons.QR_CODE_2,
            db.listar_eans,
            on_escolha=self._ao_selecionar_ean,
            on_enter=lambda: self.descricao.campo.focus(),
        )
        self.descricao = CampoComSugestao(
            "Descrição do produto",
            ft.Icons.INVENTORY_2_OUTLINED,
            db.listar_descricoes,
            on_enter=lambda: self.colaborador.campo.focus(),
        )
        self.colaborador = CampoComSugestao(
            "Nome do colaborador",
            ft.Icons.PERSON_OUTLINE,
            db.listar_colaboradores,
            on_enter=self.gerar_etiqueta,
        )

        formulario = ft.Container(
            bgcolor=CARTAO,
            border_radius=22,
            padding=28,
            shadow=ft.BoxShadow(
                blur_radius=22,
                spread_radius=0,
                color="#1F6B3A22",
                offset=ft.Offset(0, 6),
            ),
            content=ft.Column(
                [
                    ft.Text(
                        "Nova etiqueta",
                        size=16,
                        weight=ft.FontWeight.W_600,
                        color=VERDE,
                    ),
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
                                color="#FFFFFF",
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

        page.add(
            ft.Stack(
                expand=True,
                controls=[
                    ft.Container(expand=True, padding=28, content=conteudo),
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
        urls = urls_acesso(self.config.remote_port, self.config.remote_token)
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

    def _ao_teclado(self, evento: ft.KeyboardEvent) -> None:
        tecla = (evento.key or "").lower()
        campos = [self.ean, self.descricao, self.colaborador, *self._campos_dialogo]
        if tecla in ("backspace", "delete"):
            for campo in campos:
                if campo.focado:
                    campo.marcar_exclusao()
                    break

    def _ao_selecionar_ean(self, ean: str) -> None:
        registro = db.ultimo_por_ean(ean.strip())
        if not registro:
            return
        self.descricao.value = str(registro["descricao"])
        self.descricao.campo.update()

    def gerar_etiqueta(self) -> None:
        self.ean.aceitar()
        self.descricao.aceitar()
        self.colaborador.aceitar()
        ean = self.ean.value.strip()
        descricao = self.descricao.value.strip()
        colaborador = self.colaborador.value.strip()

        if not ean or not descricao or not colaborador:
            mostrar_snack(self.page, "Preencha todos os campos.")
            return
        if not ean_valido(ean):
            mostrar_snack(self.page, "EAN inválido. Use 8 a 14 dígitos ou um código de 3 a 32 caracteres.")
            return

        qtd_input = ft.TextField(label="Quantidade de etiquetas", value="1", **estilo_campo(300))

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
            fechar_dialogo(self.page)
            self._imprimir_lote_produto(ean, descricao, colaborador, quantidade)

        abrir_dialogo(
            self.page,
            ft.AlertDialog(
                bgcolor=CARTAO,
                title=ft.Text("Quantidade de etiquetas", color=VERDE, weight=ft.FontWeight.W_600),
                content=qtd_input,
                actions=[
                    ft.FilledButton("Gerar", bgcolor=VERDE, on_click=on_confirmar),
                    ft.TextButton("Cancelar", on_click=lambda _: fechar_dialogo(self.page)),
                ],
            ),
        )

    def _imprimir_lote_produto(
        self,
        ean: str,
        descricao: str,
        colaborador: str,
        quantidade: int,
    ) -> None:
        mostrar_snack(self.page, f"Imprimindo {quantidade} etiqueta(s)...")
        config = AppSettings(
            printer=self.config.printer,
            font_name=self.config.font_name,
            font_size=self.config.font_size,
        )
        data_hora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

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

    def consulta_entradas_dialog(self) -> None:
        def campo_data(rotulo: str) -> ft.TextField:
            estado = {"geracao": 0, "ultimo": 0.0, "rapida": False, "atualizando": False}

            def aplicar_formato(campo: ft.TextField, *, imediato: bool) -> None:
                bruto = campo.value or ""
                formatado = formatar_data_hora_digitada(bruto)
                if imediato:
                    formatado = formatado.strip()
                if formatado == bruto:
                    return
                estado["atualizando"] = True
                campo.value = formatado
                campo.update()
                estado["atualizando"] = False

            def ao_digitar(evento):
                campo = evento.control
                if estado["atualizando"]:
                    return
                agora = time.monotonic()
                if estado["ultimo"] and 0 < (agora - estado["ultimo"]) < 0.08:
                    estado["rapida"] = True
                estado["ultimo"] = agora
                estado["geracao"] += 1
                geracao = estado["geracao"]
                pagina = campo.page
                if pagina is None:
                    return

                async def depois() -> None:
                    await asyncio.sleep(0.45)
                    if geracao != estado["geracao"]:
                        return
                    estado["rapida"] = False
                    aplicar_formato(campo, imediato=False)

                pagina.run_task(depois)

            def ao_sair(evento):
                estado["geracao"] += 1
                estado["rapida"] = False
                aplicar_formato(evento.control, imediato=True)

            campo = ft.TextField(
                label=rotulo,
                hint_text="dd/mm/aa ou dd/mm/aaaa",
                on_change=ao_digitar,
                on_blur=ao_sair,
                **estilo_campo(220, ft.Icons.CALENDAR_MONTH_OUTLINED),
            )
            campo.width = 220
            return campo

        data_ini_merc = campo_data("Data/hora inicial")
        data_fim_merc = campo_data("Data/hora final")
        ean_filtro = CampoComSugestao(
            "Mercadoria (EAN)",
            ft.Icons.QR_CODE_2,
            db.listar_eans,
            largura=220,
        )
        nome_merc = CampoComSugestao(
            "Nome da mercadoria",
            ft.Icons.INVENTORY_2_OUTLINED,
            db.listar_descricoes,
            largura=220,
        )
        colaborador_filtro = CampoComSugestao(
            "Nome do colaborador",
            ft.Icons.PERSON_OUTLINE,
            db.listar_colaboradores,
            largura=220,
        )

        data_ini_vis = campo_data("Data/hora inicial")
        data_fim_vis = campo_data("Data/hora final")
        empresa_filtro = CampoComSugestao(
            "Empresa",
            ft.Icons.APARTMENT,
            db.listar_empresas_visitantes,
            largura=220,
        )
        nome_vis = CampoComSugestao(
            "Nome do visitante",
            ft.Icons.BADGE_OUTLINED,
            db.listar_nomes_visitantes,
            largura=220,
        )

        self._campos_dialogo = [
            ean_filtro,
            nome_merc,
            colaborador_filtro,
            empresa_filtro,
            nome_vis,
        ]

        resultado_merc = ft.Column(
            [ft.Text("Informe os filtros e clique em Pesquisar.", color=TEXTO_SUAVE)],
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )
        resultado_vis = ft.Column(
            [ft.Text("Informe os filtros e clique em Pesquisar.", color=TEXTO_SUAVE)],
            scroll=ft.ScrollMode.AUTO,
            expand=True,
        )

        def preencher_tabela(
            destino: ft.Column,
            colunas: list[str],
            registros: list,
            chaves: list[str],
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
                    ft.DataTable(
                        columns=[
                            ft.DataColumn(label=ft.Text(nome, weight=ft.FontWeight.W_600))
                            for nome in colunas
                        ],
                        heading_row_color=VERDE_SUAVE,
                        rows=[
                            ft.DataRow(
                                cells=[
                                    ft.DataCell(ft.Text(str(item[chave])))
                                    for chave in chaves
                                ]
                            )
                            for item in registros
                        ],
                    )
                )
            destino.update()

        def pesquisar_mercadoria(_=None) -> None:
            ean_filtro.aceitar()
            nome_merc.aceitar()
            colaborador_filtro.aceitar()
            try:
                resultados = db.consultar_entradas_produto(
                    ean=ean_filtro.value,
                    descricao=nome_merc.value,
                    colaborador=colaborador_filtro.value,
                    data_inicio=data_ini_merc.value or "",
                    data_fim=data_fim_merc.value or "",
                )
            except ValueError as exc:
                mostrar_snack(self.page, str(exc))
                return
            preencher_tabela(
                resultado_merc,
                ["EAN", "Mercadoria", "Colaborador", "Data e hora"],
                resultados,
                ["ean", "descricao", "colaborador", "data_hora"],
                "Nenhuma entrada de mercadoria encontrada.",
            )

        def pesquisar_visitante(_=None) -> None:
            empresa_filtro.aceitar()
            nome_vis.aceitar()
            try:
                resultados = db.consultar_entradas_visitante(
                    nome=nome_vis.value,
                    empresa=empresa_filtro.value,
                    data_inicio=data_ini_vis.value or "",
                    data_fim=data_fim_vis.value or "",
                )
            except ValueError as exc:
                mostrar_snack(self.page, str(exc))
                return
            preencher_tabela(
                resultado_vis,
                ["Visitante", "Função", "Empresa", "Data e hora"],
                resultados,
                ["nome", "funcao", "empresa", "data_hora"],
                "Nenhuma entrada de visitante encontrada.",
            )

        colaborador_filtro.on_enter = pesquisar_mercadoria
        nome_vis.on_enter = pesquisar_visitante

        abas = ft.Tabs(
            length=2,
            expand=True,
            content=ft.Column(
                expand=True,
                spacing=8,
                controls=[],
            ),
        )

        def filtros_mercadoria() -> dict[str, str]:
            ean_filtro.aceitar()
            nome_merc.aceitar()
            colaborador_filtro.aceitar()
            return {
                "ean": ean_filtro.value,
                "descricao": nome_merc.value,
                "colaborador": colaborador_filtro.value,
                "data_inicio": data_ini_merc.value or "",
                "data_fim": data_fim_merc.value or "",
            }

        def filtros_visitante() -> dict[str, str]:
            empresa_filtro.aceitar()
            nome_vis.aceitar()
            return {
                "nome": nome_vis.value,
                "empresa": empresa_filtro.value,
                "data_inicio": data_ini_vis.value or "",
                "data_fim": data_fim_vis.value or "",
            }

        def abrir_exportacao(_=None) -> None:
            padrao = "visitantes" if abas.selected_index == 1 else "mercadorias"
            self._dialogo_exportar_planilha(padrao, filtros_mercadoria, filtros_visitante)

        botoes_merc = ft.Row(
            [
                ft.FilledButton(
                    "Pesquisar",
                    icon=ft.Icons.SEARCH,
                    bgcolor=VERDE,
                    on_click=pesquisar_mercadoria,
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
        botoes_vis = ft.Row(
            [
                ft.FilledButton(
                    "Pesquisar",
                    icon=ft.Icons.SEARCH,
                    bgcolor=VERDE,
                    on_click=pesquisar_visitante,
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
                ft.Row(
                    [data_ini_merc, data_fim_merc],
                    wrap=True,
                    spacing=10,
                    run_spacing=10,
                ),
                ft.Row(
                    [ean_filtro.view, nome_merc.view, colaborador_filtro.view],
                    wrap=True,
                    spacing=10,
                    run_spacing=10,
                ),
                botoes_merc,
                ft.Container(content=resultado_merc, expand=True),
            ],
            spacing=12,
            expand=True,
        )
        aba_visitante = ft.Column(
            [
                ft.Row(
                    [data_ini_vis, data_fim_vis],
                    wrap=True,
                    spacing=10,
                    run_spacing=10,
                ),
                ft.Row(
                    [empresa_filtro.view, nome_vis.view],
                    wrap=True,
                    spacing=10,
                    run_spacing=10,
                ),
                botoes_vis,
                ft.Container(content=resultado_vis, expand=True),
            ],
            spacing=12,
            expand=True,
        )
        abas.content.controls = [
            ft.TabBar(
                tabs=[
                    ft.Tab(
                        label="Entrada de mercadoria",
                        icon=ft.Icons.INVENTORY_2_OUTLINED,
                    ),
                    ft.Tab(
                        label="Entrada de visitantes",
                        icon=ft.Icons.BADGE_OUTLINED,
                    ),
                ]
            ),
            ft.TabBarView(
                expand=True,
                controls=[aba_mercadoria, aba_visitante],
            ),
        ]

        def ao_fechar(_=None) -> None:
            self._campos_dialogo = []
            fechar_dialogo(self.page)

        abrir_dialogo(
            self.page,
            ft.AlertDialog(
                bgcolor=CARTAO,
                title=ft.Text("Consultar entradas", color=VERDE, weight=ft.FontWeight.W_600),
                content=ft.Container(
                    width=860,
                    height=520,
                    content=abas,
                ),
                scrollable=True,
                actions=[ft.TextButton("Fechar", on_click=ao_fechar)],
            ),
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
                    ft.Radio(
                        value="filtros",
                        label="Usar os filtros preenchidos na consulta",
                    ),
                    ft.Radio(
                        value="todos",
                        label="Exportar todos os registros, sem filtro",
                    ),
                ],
                spacing=4,
            ),
        )

        def on_exportar(_):
            conteudo = tipo.value or "ambos"
            aplicar_filtros = (usar_filtros.value or "filtros") == "filtros"
            fechar_dialogo(self.page)
            self.page.run_task(
                self._exportar_planilha,
                conteudo,
                aplicar_filtros,
                obter_filtros_merc(),
                obter_filtros_vis(),
            )

        abrir_dialogo(
            self.page,
            ft.AlertDialog(
                bgcolor=CARTAO,
                title=ft.Text("Exportar planilha", color=VERDE, weight=ft.FontWeight.W_600),
                content=ft.Column(
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
                actions=[
                    ft.FilledButton("Exportar", bgcolor=VERDE, on_click=on_exportar),
                    ft.TextButton("Cancelar", on_click=lambda _: fechar_dialogo(self.page)),
                ],
            ),
        )

    async def _exportar_planilha(
        self,
        conteudo: str,
        aplicar_filtros: bool,
        filtros_merc: dict[str, str],
        filtros_vis: dict[str, str],
    ) -> None:
        args_merc = filtros_merc if aplicar_filtros else {}
        args_vis = filtros_vis if aplicar_filtros else {}
        try:
            mercadorias = None
            visitantes = None
            if conteudo in {"mercadorias", "ambos"}:
                mercadorias = db.consultar_entradas_produto(limite=None, **args_merc)
            if conteudo in {"visitantes", "ambos"}:
                visitantes = db.consultar_entradas_visitante(limite=None, **args_vis)
            dados = gerar_planilha_entradas(
                mercadorias,
                visitantes,
                resumo_filtros(
                    [
                        ("Data inicial", args_merc.get("data_inicio", "")),
                        ("Data final", args_merc.get("data_fim", "")),
                        ("Mercadoria", args_merc.get("ean", "")),
                        ("Nome", args_merc.get("descricao", "")),
                        ("Colaborador", args_merc.get("colaborador", "")),
                    ]
                )
                if mercadorias is not None
                else "",
                resumo_filtros(
                    [
                        ("Data inicial", args_vis.get("data_inicio", "")),
                        ("Data final", args_vis.get("data_fim", "")),
                        ("Empresa", args_vis.get("empresa", "")),
                        ("Visitante", args_vis.get("nome", "")),
                    ]
                )
                if visitantes is not None
                else "",
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

    def adicionar_visitante_dialog(self) -> None:
        nome_input = CampoComSugestao(
            "Nome do visitante",
            ft.Icons.PERSON_OUTLINE,
            db.listar_nomes_visitantes,
            largura=320,
        )
        funcao_input = ft.TextField(label="Função", **estilo_campo(320, ft.Icons.WORK_OUTLINE))
        empresa_input = ft.TextField(label="Empresa", **estilo_campo(320, ft.Icons.APARTMENT))
        nome_input.on_enter = lambda: funcao_input.focus()
        self._campos_dialogo = [nome_input]

        def on_adicionar(_):
            nome_input.aceitar()
            nome = nome_input.value.strip()
            funcao = (funcao_input.value or "").strip()
            empresa = (empresa_input.value or "").strip()
            if not nome or not funcao or not empresa:
                mostrar_snack(self.page, "Preencha nome, função e empresa.")
                return
            self._campos_dialogo = []
            fechar_dialogo(self.page)
            self._imprimir_visitante(nome, funcao, empresa)

        def ao_fechar(_):
            self._campos_dialogo = []
            fechar_dialogo(self.page)

        abrir_dialogo(
            self.page,
            ft.AlertDialog(
                bgcolor=CARTAO,
                title=ft.Text("Adicionar visitante", color=VERDE, weight=ft.FontWeight.W_600),
                content=ft.Column(
                    [nome_input.view, funcao_input, empresa_input],
                    tight=True,
                    height=220,
                    spacing=14,
                ),
                actions=[
                    ft.FilledButton("Adicionar", bgcolor=VERDE, on_click=on_adicionar),
                    ft.TextButton("Cancelar", on_click=ao_fechar),
                ],
            ),
        )

    def _imprimir_visitante(self, nome: str, funcao: str, empresa: str) -> None:
        mostrar_snack(self.page, "Imprimindo etiqueta de visitante...")
        config = AppSettings(
            printer=self.config.printer,
            font_name=self.config.font_name,
            font_size=self.config.font_size,
        )
        data_hora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        def trabalho() -> None:
            erro = None
            try:
                imprimir_etiqueta_visitante(nome, funcao, empresa, data_hora, config)
                db.adicionar_visitante(nome, funcao, empresa, data_hora)
            except Exception as exc:
                erro = exc

            async def concluir() -> None:
                if erro:
                    mostrar_snack(self.page, f"Falha ao imprimir visitante: {erro}")
                    return
                mostrar_snack(self.page, "Etiqueta de visitante criada com sucesso!")

            self.page.run_task(concluir)

        self.page.run_thread(trabalho)

    def abrir_dialogo_configuracao(self) -> None:
        impressoras = listar_impressoras()
        if self.config.printer and self.config.printer not in impressoras:
            impressoras = [self.config.printer, *impressoras]

        dropdown_printer = ft.Dropdown(
            label="Impressora",
            width=320,
            filled=True,
            fill_color=VERDE_SUAVE,
            border_radius=14,
            border_color=BORDA,
            focused_border_color=VERDE,
            options=[ft.DropdownOption(key=nome, text=nome) for nome in impressoras],
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
        font_input = ft.TextField(label="Fonte (ex: arial.ttf)", value=self.config.font_name, **estilo_campo(320))
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
        urls = urls_acesso(int(self.config.remote_port or 8765), chave_atual)
        url_texto = ft.Text(
            "\n".join(urls),
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
            chave = (chave_input.value or "").strip() or gerar_chave()
            self.config.printer = dropdown_printer.value
            self.config.font_size = int(font_size_slider.value or 20)
            self.config.font_name = (font_input.value or "arial.ttf").strip()
            self.config.remote_enabled = bool(remoto_switch.value)
            self.config.remote_port = porta
            self.config.remote_token = chave
            salvar_config(self.config)
            fechar_dialogo(self.page)
            self._aplicar_acesso_remoto(avisar=True)

        abrir_dialogo(
            self.page,
            ft.AlertDialog(
                bgcolor=CARTAO,
                title=ft.Text("Configurações", color=VERDE, weight=ft.FontWeight.W_600),
                content=ft.Column(
                    [
                        ft.Text("Impressora", color=TEXTO_SUAVE),
                        dropdown_printer,
                        ft.Text("Tamanho da fonte da etiqueta:", color=TEXTO_SUAVE),
                        font_size_slider,
                        font_input,
                        ft.Divider(),
                        ft.Text("Acesso remoto", weight=ft.FontWeight.W_600, color=VERDE),
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
                    width=420,
                    height=460,
                    scroll=ft.ScrollMode.AUTO,
                ),
                actions=[
                    ft.FilledButton("Salvar", bgcolor=VERDE, on_click=on_salvar),
                    ft.TextButton("Cancelar", on_click=lambda _: fechar_dialogo(self.page)),
                ],
            ),
        )


def main(page: ft.Page) -> None:
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
