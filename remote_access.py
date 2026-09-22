from __future__ import annotations

import secrets
import socket
import threading
from html import escape
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

import db
from export_excel import gerar_planilha_entradas

LIMITE_TELA = 300
COOKIE_NOME = "acesso"
_servidor: ThreadingHTTPServer | None = None
_thread: threading.Thread | None = None
_lock = threading.Lock()


class _Servidor(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    chave = ""


def gerar_chave() -> str:
    return secrets.token_urlsafe(9)


def enderecos_rede() -> list[str]:
    encontrados: list[str] = []
    try:
        teste = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        teste.connect(("8.8.8.8", 80))
        ip = teste.getsockname()[0]
        teste.close()
        if ip and not ip.startswith("127."):
            encontrados.append(ip)
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip not in encontrados and not ip.startswith("127."):
                encontrados.append(ip)
    except OSError:
        pass
    return encontrados or ["127.0.0.1"]


def urls_acesso(porta: int, chave: str) -> list[str]:
    sufixo = f":{porta}/?chave={quote(chave)}"
    return [f"http://{ip}{sufixo}" for ip in enderecos_rede()]


def servidor_ativo() -> bool:
    return _servidor is not None


def parar_servidor() -> None:
    global _servidor, _thread
    with _lock:
        httpd = _servidor
        _servidor = None
        _thread = None
    if httpd is not None:
        httpd.shutdown()
        httpd.server_close()


def iniciar_servidor(porta: int, chave: str) -> None:
    global _servidor, _thread
    if porta < 1024 or porta > 65535:
        raise ValueError("A porta deve estar entre 1024 e 65535.")
    if not chave.strip():
        raise ValueError("Defina uma chave de acesso remoto.")
    parar_servidor()
    httpd = _Servidor(("0.0.0.0", porta), _Handler)
    httpd.chave = chave.strip()
    worker = threading.Thread(target=httpd.serve_forever, name="acesso-remoto", daemon=True)
    with _lock:
        _servidor = httpd
        _thread = worker
    worker.start()


def aplicar_acesso_remoto(ativo: bool, porta: int, chave: str) -> str | None:
    if not ativo:
        parar_servidor()
        return None
    try:
        iniciar_servidor(porta, chave)
    except OSError as exc:
        return f"Não foi possível abrir a porta {porta}: {exc}"
    except ValueError as exc:
        return str(exc)
    return None


def _resumo(itens: list[tuple[str, str]]) -> str:
    ativos = [f"{nome}: {valor.strip()}" for nome, valor in itens if (valor or "").strip()]
    return " | ".join(ativos) if ativos else "Nenhum (todos os registros)"


class _Handler(BaseHTTPRequestHandler):

    def log_message(self, formato: str, *args) -> None:
        return

    def do_GET(self) -> None:
        destino = urlparse(self.path)
        params = parse_qs(destino.query)
        if destino.path == "/exportar":
            if not self._autorizado(params):
                self._pagina_login("Informe a chave de acesso para exportar.")
                return
            self._exportar(params)
            return
        if destino.path not in {"", "/"}:
            self._responder(404, "text/plain; charset=utf-8", b"Nao encontrado")
            return
        chave_url = (params.get("chave") or [""])[0].strip()
        if chave_url and self._chave_valida(chave_url):
            self._redirecionar_com_cookie("/")
            return
        if not self._autorizado(params):
            self._pagina_login()
            return
        self._pagina_consulta(params)

    def do_POST(self) -> None:
        destino = urlparse(self.path)
        if destino.path != "/entrar":
            self._responder(404, "text/plain; charset=utf-8", b"Nao encontrado")
            return
        tamanho = int(self.headers.get("Content-Length", "0") or 0)
        corpo = self.rfile.read(min(tamanho, 4096)).decode("utf-8", "ignore")
        chave = (parse_qs(corpo).get("chave") or [""])[0].strip()
        if self._chave_valida(chave):
            self._redirecionar_com_cookie("/")
            return
        self._pagina_login("Chave inválida.")

    def _chave_esperada(self) -> str:
        return str(getattr(self.server, "chave", "") or "")

    def _chave_valida(self, recebida: str) -> bool:
        esperada = self._chave_esperada()
        if not esperada or not recebida:
            return False
        try:
            return secrets.compare_digest(recebida, esperada)
        except ValueError:
            return False

    def _cookie_chave(self) -> str:
        bruto = self.headers.get("Cookie", "")
        if not bruto:
            return ""
        cookie = SimpleCookie()
        try:
            cookie.load(bruto)
        except Exception:
            return ""
        if COOKIE_NOME not in cookie:
            return ""
        return unquote(cookie[COOKIE_NOME].value or "")

    def _autorizado(self, params: dict[str, list[str]]) -> bool:
        chave_url = (params.get("chave") or [""])[0].strip()
        return self._chave_valida(chave_url) or self._chave_valida(self._cookie_chave())

    def _redirecionar_com_cookie(self, caminho: str) -> None:
        self.send_response(302)
        self.send_header("Location", caminho)
        self.send_header(
            "Set-Cookie",
            f"{COOKIE_NOME}={quote(self._chave_esperada())}; Path=/; HttpOnly; SameSite=Lax; Max-Age=43200",
        )
        self.end_headers()

    def _responder(self, status: int, tipo: str, corpo: bytes, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        if extra:
            for nome, valor in extra.items():
                self.send_header(nome, valor)
        self.end_headers()
        self.wfile.write(corpo)

    def _html(self, titulo: str, corpo: str) -> None:
        pagina = f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(titulo)}</title>
  <style>
    body {{ margin:0; font-family:Calibri,Segoe UI,sans-serif; background:#F3F0E6; color:#243028; }}
    header {{ background:#1F6B3A; color:#fff; padding:18px 22px; }}
    header span {{ color:#E07A2F; font-weight:700; letter-spacing:.04em; font-size:12px; }}
    main {{ max-width:1100px; margin:0 auto; padding:22px; }}
    .card {{ background:#FFFDF8; border:1px solid #C9D6C3; border-radius:16px; padding:18px; margin-bottom:16px; }}
    label {{ display:block; font-size:13px; color:#5B6F60; margin:8px 0 4px; }}
    input, select {{ width:100%; box-sizing:border-box; padding:9px 10px; border:1px solid #C9D6C3; border-radius:10px; background:#E7F3EA; }}
    .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:10px; }}
    .acoes {{ display:flex; gap:10px; flex-wrap:wrap; margin-top:14px; }}
    button, .botao {{ background:#1F6B3A; color:#fff; border:0; border-radius:10px; padding:10px 14px; text-decoration:none; cursor:pointer; font-weight:600; }}
    .secundario {{ background:#fff; color:#1F6B3A; border:1px solid #1F6B3A; }}
    table {{ width:100%; border-collapse:collapse; font-size:14px; }}
    th {{ background:#1F6B3A; color:#fff; text-align:left; padding:8px; }}
    td {{ padding:8px; border-bottom:1px solid #C9D6C3; }}
    tr:nth-child(even) td {{ background:#E7F3EA; }}
    .erro {{ color:#9b2c2c; margin-bottom:10px; }}
    .ok {{ color:#1F6B3A; font-weight:700; }}
    h2 {{ margin:0 0 12px; color:#1F6B3A; }}
  </style>
</head>
<body>
  <header>
    <span>HORTIFRUTI NATURAL DA TERRA</span>
    <h1 style="margin:6px 0 0; font-size:22px;">Controle de entradas</h1>
    <p style="margin:6px 0 0; opacity:.9;">Consulta e exportação remota (somente leitura)</p>
  </header>
  <main>{corpo}</main>
</body>
</html>"""
        self._responder(200, "text/html; charset=utf-8", pagina.encode("utf-8"))

    def _pagina_login(self, erro: str = "") -> None:
        aviso = f'<p class="erro">{escape(erro)}</p>' if erro else ""
        self._html(
            "Acesso remoto",
            f"""
            <div class="card" style="max-width:420px;">
              <h2>Entrar</h2>
              {aviso}
              <p>Use a chave exibida no computador da portaria, em Impressora / acesso remoto.</p>
              <form method="post" action="/entrar">
                <label>Chave de acesso</label>
                <input name="chave" type="password" autocomplete="current-password" required>
                <div class="acoes"><button type="submit">Entrar</button></div>
              </form>
            </div>
            """,
        )

    def _param(self, params: dict[str, list[str]], nome: str) -> str:
        return (params.get(nome) or [""])[0].strip()

    def _pagina_consulta(self, params: dict[str, list[str]]) -> None:
        filtros_merc = {
            "ean": self._param(params, "ean"),
            "descricao": self._param(params, "descricao"),
            "colaborador": self._param(params, "colaborador"),
            "data_inicio": self._param(params, "data_inicio"),
            "data_fim": self._param(params, "data_fim"),
        }
        filtros_vis = {
            "nome": self._param(params, "nome"),
            "empresa": self._param(params, "empresa"),
            "data_inicio": self._param(params, "vis_inicio"),
            "data_fim": self._param(params, "vis_fim"),
        }
        erro = ""
        mercadorias: list = []
        visitantes: list = []
        try:
            mercadorias = db.consultar_entradas_produto(limite=LIMITE_TELA, **filtros_merc)
            visitantes = db.consultar_entradas_visitante(limite=LIMITE_TELA, **filtros_vis)
        except ValueError as exc:
            erro = str(exc)

        query_export = urlencode({k: v for k, v in {**filtros_merc, **{
            "nome": filtros_vis["nome"],
            "empresa": filtros_vis["empresa"],
            "vis_inicio": filtros_vis["data_inicio"],
            "vis_fim": filtros_vis["data_fim"],
        }}.items() if v})

        def campo(nome: str, rotulo: str, valor: str) -> str:
            return (
                f'<div><label>{escape(rotulo)}</label>'
                f'<input name="{escape(nome)}" value="{escape(valor)}"></div>'
            )

        def tabela(colunas: list[tuple[str, str]], registros: list, vazio: str) -> str:
            if not registros:
                return f"<p>{escape(vazio)}</p>"
            aviso = (
                f'<p class="ok">{len(registros)} registro(s) na tela'
                f"{' (mais recentes; exporte para ver todos)' if len(registros) >= LIMITE_TELA else ''}.</p>"
            )
            cab = "".join(f"<th>{escape(titulo)}</th>" for titulo, _ in colunas)
            linhas = []
            for item in registros:
                celulas = "".join(f"<td>{escape(str(item[chave]))}</td>" for _, chave in colunas)
                linhas.append(f"<tr>{celulas}</tr>")
            return aviso + f"<table><tr>{cab}</tr>{''.join(linhas)}</table>"

        aviso = f'<p class="erro">{escape(erro)}</p>' if erro else ""
        self._html(
            "Controle de entradas",
            f"""
            {aviso}
            <form class="card" method="get" action="/">
              <h2>Filtros</h2>
              <div class="grid">
                {campo("data_inicio", "Mercadoria · data inicial", filtros_merc["data_inicio"])}
                {campo("data_fim", "Mercadoria · data final", filtros_merc["data_fim"])}
                {campo("ean", "EAN", filtros_merc["ean"])}
                {campo("descricao", "Nome da mercadoria", filtros_merc["descricao"])}
                {campo("colaborador", "Colaborador", filtros_merc["colaborador"])}
                {campo("vis_inicio", "Visitante · data inicial", filtros_vis["data_inicio"])}
                {campo("vis_fim", "Visitante · data final", filtros_vis["data_fim"])}
                {campo("nome", "Visitante", filtros_vis["nome"])}
                {campo("empresa", "Empresa", filtros_vis["empresa"])}
              </div>
              <div class="acoes">
                <button type="submit">Pesquisar</button>
                <a class="botao secundario" href="/exportar?tipo=mercadorias&{query_export}">Exportar mercadorias</a>
                <a class="botao secundario" href="/exportar?tipo=visitantes&{query_export}">Exportar visitantes</a>
                <a class="botao secundario" href="/exportar?tipo=ambos&{query_export}">Exportar ambos</a>
              </div>
            </form>
            <div class="card">
              <h2>Mercadorias</h2>
              {tabela(
                    [("EAN", "ean"), ("Mercadoria", "descricao"), ("Colaborador", "colaborador"), ("Data e hora", "data_hora")],
                    mercadorias,
                    "Nenhuma entrada de mercadoria encontrada.",
                )}
            </div>
            <div class="card">
              <h2>Visitantes</h2>
              {tabela(
                    [("Visitante", "nome"), ("Função", "funcao"), ("Empresa", "empresa"), ("Data e hora", "data_hora")],
                    visitantes,
                    "Nenhuma entrada de visitante encontrada.",
                )}
            </div>
            """,
        )

    def _exportar(self, params: dict[str, list[str]]) -> None:
        tipo = self._param(params, "tipo") or "ambos"
        filtros_merc = {
            "ean": self._param(params, "ean"),
            "descricao": self._param(params, "descricao"),
            "colaborador": self._param(params, "colaborador"),
            "data_inicio": self._param(params, "data_inicio"),
            "data_fim": self._param(params, "data_fim"),
        }
        filtros_vis = {
            "nome": self._param(params, "nome"),
            "empresa": self._param(params, "empresa"),
            "data_inicio": self._param(params, "vis_inicio"),
            "data_fim": self._param(params, "vis_fim"),
        }
        try:
            mercadorias = None
            visitantes = None
            if tipo in {"mercadorias", "ambos"}:
                mercadorias = db.consultar_entradas_produto(limite=None, **filtros_merc)
            if tipo in {"visitantes", "ambos"}:
                visitantes = db.consultar_entradas_visitante(limite=None, **filtros_vis)
            dados = gerar_planilha_entradas(
                mercadorias,
                visitantes,
                _resumo(
                    [
                        ("Data inicial", filtros_merc["data_inicio"]),
                        ("Data final", filtros_merc["data_fim"]),
                        ("Mercadoria", filtros_merc["ean"]),
                        ("Nome", filtros_merc["descricao"]),
                        ("Colaborador", filtros_merc["colaborador"]),
                    ]
                )
                if mercadorias is not None
                else "",
                _resumo(
                    [
                        ("Data inicial", filtros_vis["data_inicio"]),
                        ("Data final", filtros_vis["data_fim"]),
                        ("Empresa", filtros_vis["empresa"]),
                        ("Visitante", filtros_vis["nome"]),
                    ]
                )
                if visitantes is not None
                else "",
            )
        except ValueError as exc:
            self._html("Exportar", f'<div class="card"><p class="erro">{escape(str(exc))}</p></div>')
            return
        except Exception as exc:
            self._html("Exportar", f'<div class="card"><p class="erro">Falha ao montar a planilha: {escape(str(exc))}</p></div>')
            return
        nome = "entradas.xlsx"
        self._responder(
            200,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            dados,
            {"Content-Disposition": f'attachment; filename="{nome}"'},
        )
