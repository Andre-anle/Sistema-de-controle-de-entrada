import flet as ft
from datetime import datetime
import sqlite3
from PIL import Image, ImageDraw, ImageFont, ImageWin
import win32print
import win32ui
import os

# Sistema de controle de etiquetas para produtos do CD Pavuna

# Configurações iniciais
selected_printer = win32print.GetDefaultPrinter()
selected_font_size = 20

selected_font = "arial.ttf"

def configurar_banco_dados():
    caminho_area_de_trabalho = os.path.join(os.path.expanduser("~"), "Desktop")
    caminho_banco = os.path.join(caminho_area_de_trabalho, "etiquetas.db")
    
    if not os.path.exists(caminho_banco):
        conexao = sqlite3.connect(caminho_banco)
        cursor = conexao.cursor()
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS etiquetas (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ean TEXT NOT NULL,
                descricao TEXT NOT NULL,
                colaborador TEXT NOT NULL,
                data_hora TEXT NOT NULL
            )
        ''')
        conexao.commit()
        conexao.close()
    
    return caminho_banco

# Função para gerar a etiqueta de produto
def imprimir_etiqueta(ean, descricao, colaborador, data_hora):
    global selected_printer, selected_font, selected_font_size
    hdc = win32ui.CreateDC()
    hdc.CreatePrinterDC(selected_printer)

    hdc.StartDoc("Etiqueta")
    hdc.StartPage()

    logo_path = 'logo.jpg'
    logo = Image.open(logo_path).convert("RGBA")
    logo = logo.resize((250, 120), Image.Resampling.LANCZOS)
    logo_width, logo_height = logo.size

    img = Image.new('RGB', (400, 500 + logo_height), color=(255, 255, 255))
    d = ImageDraw.Draw(img)

    img.paste(logo, (int((img.width - logo_width) / 2), 8), logo)

    try:
        font = ImageFont.truetype(selected_font, selected_font_size)
    except:
        font = ImageFont.load_default()

    text = (f"CONTROLE ENTRADA CD\nEAN: {ean}\nDescrição: {descricao}\n"
            f"Colaborador: {colaborador}\nData e Hora: {data_hora}\nProibido remover")

    current_height = logo_height + 30

    for line in text.split('\n'):
        d.text((10, current_height), line, font=font, fill=(0, 0, 1)) 
        current_height += selected_font_size + 10

    dib = ImageWin.Dib(img)
    dib.draw(hdc.GetHandleOutput(), (0, 0, img.width, img.height))

    hdc.EndPage()
    hdc.EndDoc()
    hdc.DeleteDC()

# Lista as Impressoras disponíveis
def listar_impressoras():
    return [printer[2] for printer in win32print.EnumPrinters(win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS)]

# Definir impressora padrão
def definir_impressora_padrao(impressora):
    global selected_printer
    selected_printer = impressora

# Abre o diálogo de configuração de impressão (padrão: ZDesigner ZD220-203dpi)
def abrir_dialogo_configuracao(page) -> None:
    impressoras = listar_impressoras()
    dropdown_printer = ft.Dropdown(width=300, options=[ft.dropdown.Option(impressora) for impressora in impressoras], value=selected_printer)
    font_size_slider = ft.Slider(min=10, max=50, value=selected_font_size, label="Tamanho da Fonte: {value}")
    font_input = ft.TextField(label="Fonte (ex: arial.ttf)", value=selected_font, width=300)

    def on_salvar(e):
        definir_impressora_padrao(dropdown_printer.value)
        global selected_font_size, selected_font
        selected_font_size = int(font_size_slider.value)
        selected_font = font_input.value
        page.dialog.open = False
        page.snack_bar = ft.SnackBar(content=ft.Text(f"Configurações salvas. Impressora: {dropdown_printer.value}, Fonte: {selected_font}, Tamanho: {selected_font_size}"))
        page.snack_bar.open = True
        page.update()

    page.dialog = ft.AlertDialog(
        title=ft.Text("Configuração de Impressora"),
        content=ft.Column([
            ft.Text("Selecione a impressora:"),
            dropdown_printer,
            ft.Text("Ajuste o tamanho da fonte:"),
            font_size_slider,
            ft.Text("Escolha a fonte:"),
            font_input
        ]),
        actions=[
            ft.TextButton(text="Salvar", on_click=on_salvar),
            ft.TextButton(text="Cancelar", on_click=lambda e: fechar_dialog(page))
        ]
    )
    page.dialog.open = True
    page.update()

# Salva a etiqueta no banco de dados
def adicionar_etiqueta(ean, descricao, colaborador, data_hora):
    caminho_banco = configurar_banco_dados()
    conexao = sqlite3.connect(caminho_banco)
    cursor = conexao.cursor()
    cursor.execute('''
        INSERT INTO etiquetas (ean, descricao, colaborador, data_hora) VALUES (?, ?, ?, ?)
    ''', (ean, descricao, colaborador, data_hora))
    conexao.commit()
    conexao.close()

# Busca a etiqueta cadastrada no banco de dados
def consultar_ean(ean):
    caminho_banco = configurar_banco_dados()
    conexao = sqlite3.connect(caminho_banco)
    cursor = conexao.cursor()
    cursor.execute('SELECT ean, descricao, colaborador, data_hora FROM etiquetas WHERE ean = ?', (ean,))
    resultado = cursor.fetchone()
    conexao.close()
    return resultado

# Função principal de controle do sistema
def main(page: ft.Page):
    page.title = "Sistema de Controle de Etiquetas"

    titulo = ft.Text("Controle de Etiquetas de Mercadorias", size=30, weight="bold")
    subtitulo = ft.Text("Gerencie e consulte as etiquetas de produtos", size=20)

    ean = ft.TextField(label="Digite o EAN", width=300, autofocus=True)
    descricao = ft.TextField(label="Descrição do Produto", width=300)
    colaborador = ft.TextField(label="Nome do Colaborador", width=300)

    botao_gerar = ft.ElevatedButton(
        text="Gerar Etiqueta",
        on_click=lambda e: gerar_etiqueta(page, ean, descricao, colaborador)
    )

    botao_consultar = ft.ElevatedButton(
        text="Consultar EAN",
        on_click=lambda e: consulta_ean_dialog(page)
    )

    botao_visitante = ft.ElevatedButton(
        text="Adicionar Visitante",
        on_click=lambda e: adicionar_visitante_dialog(page)
    )

    botao_configurar_impressora = ft.ElevatedButton(
        text="Configurar Impressora",
        on_click=lambda e: abrir_dialogo_configuracao(page)
    )

    page.add(
        titulo,
        subtitulo,
        ean,
        descricao,
        colaborador,
        ft.Row([botao_gerar, botao_consultar, botao_visitante, botao_configurar_impressora], alignment="center")
    )

# Função ajustada para gerar etiquetas com quantidade informada
def gerar_etiqueta(page, ean, descricao, colaborador):
    # Verifica se os campos obrigatórios foram preenchidos
    if not ean.value or not descricao.value or not colaborador.value:
        page.snack_bar = ft.SnackBar(content=ft.Text("Preencha todos os campos."))
        page.snack_bar.open = True
        page.update()
        return

    # Cria um diálogo para solicitar a quantidade de etiquetas
    qtd_input = ft.TextField(label="Quantidade de Etiquetas", width=300, value="1")
    
    def on_confirmar(e):
        try:
            quantidade = int(qtd_input.value)
        except ValueError:
            page.snack_bar = ft.SnackBar(content=ft.Text("Digite um número válido para quantidade."))
            page.snack_bar.open = True
            page.update()
            return
        if quantidade <= 0:
            page.snack_bar = ft.SnackBar(content=ft.Text("A quantidade deve ser maior que zero."))
            page.snack_bar.open = True
            page.update()
            return

        # Gera as etiquetas conforme a quantidade informada
        for _ in range(quantidade):
            data_hora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            imprimir_etiqueta(ean.value, descricao.value, colaborador.value, data_hora)
            adicionar_etiqueta(ean.value, descricao.value, colaborador.value, data_hora)
        page.dialog.open = False
        
        # Opcional: Limpa os campos após gerar as etiquetas
        ean.value = ""
        descricao.value = ""
        colaborador.value = ""
        page.update()
        
        page.snack_bar = ft.SnackBar(content=ft.Text(f"{quantidade} etiqueta(s) criadas com sucesso!"))
        page.snack_bar.open = True
        page.update()

    page.dialog = ft.AlertDialog(
        title=ft.Text("Quantidade de Etiquetas"),
        content=qtd_input,
        actions=[
            ft.TextButton(text="Gerar", on_click=on_confirmar),
            ft.TextButton(text="Cancelar", on_click=lambda e: fechar_dialog(page))
        ]
    )
    page.dialog.open = True
    page.update()

def adicionar_visitante_dialog(page):
    nome_input = ft.TextField(label="Nome do Visitante", width=300)
    funcao_input = ft.TextField(label="Função", width=300)
    empresa_input = ft.TextField(label="Empresa", width=300)

    def on_adicionar(e):
        data_hora = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        imprimir_etiqueta_visitante(nome_input.value, funcao_input.value, empresa_input.value, data_hora)
        page.dialog.open = False
        page.snack_bar = ft.SnackBar(content=ft.Text("Etiqueta de Visitante criada com sucesso!"))
        page.snack_bar.open = True
        page.update()

    page.dialog = ft.AlertDialog(
        title=ft.Text("Adicionar Visitante"),
        content=ft.Column([nome_input, funcao_input, empresa_input]),
        actions=[
            ft.TextButton(text="Adicionar", on_click=on_adicionar),
            ft.TextButton(text="Cancelar", on_click=lambda e: fechar_dialog(page))
        ]
    )
    page.dialog.open = True
    page.update()

def fechar_dialog(page):
    page.dialog.open = False
    page.update()

# Função para imprimir etiqueta de visitante
def imprimir_etiqueta_visitante(nome, funcao, empresa, data_hora):
    global selected_printer, selected_font, selected_font_size
    hdc = win32ui.CreateDC()
    hdc.CreatePrinterDC(selected_printer)

    hdc.StartDoc("Etiqueta Visitante")
    hdc.StartPage()

    logo_path = 'logo.jpg'
    logo = Image.open(logo_path).convert("RGBA")
    logo = logo.resize((250, 120), Image.Resampling.LANCZOS)
    logo_width, logo_height = logo.size
    
    

    img = Image.new('RGB', (400, 450 + logo_height), color=(255, 255, 255))
    d = ImageDraw.Draw(img)

    img.paste(logo, (int((img.width - logo_width) / 2), 8), logo)

    try:
        font = ImageFont.truetype(selected_font, selected_font_size)
    except:
        font = ImageFont.load_default()

    text = (f"NOME: {nome}\nFUNÇÃO: {funcao}\nEMPRESA: {empresa}\nData e Hora: {data_hora}\nVisitante")

    current_height = logo_height + 30

    for line in text.split('\n'):
        d.text((10, current_height), line, font=font, fill=(0, 0, 0))
        current_height += selected_font_size + 10

    dib = ImageWin.Dib(img)
    dib.draw(hdc.GetHandleOutput(), (0, 0, img.width, img.height))

    hdc.EndPage()
    hdc.EndDoc()
    hdc.DeleteDC()

# Função para consultar EAN cadastrado
def consulta_ean_dialog(page):
    ean_input = ft.TextField(label="Digite o EAN para consulta", width=300)

    def on_consulta(e):
        resultado = consultar_ean(ean_input.value)
        if resultado:
            resultado_text = (f"EAN: {resultado[0]}\nDescrição: {resultado[1]}\n"
                              f"Colaborador: {resultado[2]}\nData e Hora: {resultado[3]}")
        else:
            resultado_text = "EAN não encontrado no banco de dados."

        page.dialog.open = False
        page.snack_bar = ft.SnackBar(content=ft.Text(resultado_text))
        page.snack_bar.open = True
        page.update()

    page.dialog = ft.AlertDialog(
        title=ft.Text("Consulta de EAN"),
        content=ean_input,
        actions=[
            ft.TextButton(text="Consultar", on_click=on_consulta),
            ft.TextButton(text="Cancelar", on_click=lambda e: fechar_dialog(page))
        ]
    )
    page.dialog.open = True
    page.update()

if __name__ == "__main__":
    ft.app(target=main)
