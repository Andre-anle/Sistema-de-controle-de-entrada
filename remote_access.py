from __future__ import annotations

import secrets
import socket
import threading
import time
from datetime import datetime
from html import escape
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, quote, unquote, urlencode, urlparse

import db
from export_excel import COLUNAS_MERCADORIAS, COLUNAS_VISITANTES, gerar_planilha_entradas
from filtros import resumo_mercadorias, resumo_visitantes
from paths import ICONE_PATH, LOGO_PATH

LIMITE_TELA = 300
COOKIE_NOME = "acesso"
VALIDADE_ENDERECOS = 60.0
_servidor: ThreadingHTTPServer | None = None
_thread: threading.Thread | None = None
_lock = threading.Lock()
_enderecos_cache: tuple[float, list[str]] | None = None


class _Servidor(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True
    chave = ""


def gerar_chave() -> str:
    return secrets.token_urlsafe(9)


def enderecos_rede() -> list[str]:
    global _enderecos_cache
    agora = time.monotonic()
    if _enderecos_cache and agora - _enderecos_cache[0] < VALIDADE_ENDERECOS:
        return list(_enderecos_cache[1])
    encontrados = _descobrir_enderecos()
    _enderecos_cache = (agora, encontrados)
    return list(encontrados)


def _descobrir_enderecos() -> list[str]:
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


_ARQUIVOS_ESTATICOS = {
    "/logo.png": (LOGO_PATH, "image/png"),
    "/favicon.ico": (ICONE_PATH, "image/x-icon"),
}

_ICONES = {
    "etiqueta": "M21.41 11.58l-9-9C12.05 2.22 11.55 2 11 2H4c-1.1 0-2 .9-2 2v7c0 .55.22 1.05.59 1.42l9 9c.36.36.86.58 1.41.58s1.05-.22 1.41-.59l7-7c.37-.36.59-.86.59-1.41s-.23-1.06-.59-1.42zM13 20.01L4 11V4h7v-.01l9 9-7 7.02zM6.5 5C5.67 5 5 5.67 5 6.5S5.67 8 6.5 8 8 7.33 8 6.5 7.33 5 6.5 5z",
    "pessoas": "M16 11c1.66 0 2.99-1.34 2.99-3S17.66 5 16 5c-1.66 0-3 1.34-3 3s1.34 3 3 3zm-8 0c1.66 0 2.99-1.34 2.99-3S9.66 5 8 5C6.34 5 5 6.34 5 8s1.34 3 3 3zm0 2c-2.33 0-7 1.17-7 3.5V19h14v-2.5c0-2.33-4.67-3.5-7-3.5zm8 0c-.29 0-.62.02-.97.05 1.16.84 1.97 1.97 1.97 3.45V19h6v-2.5c0-2.33-4.67-3.5-7-3.5z",
    "filtro": "M3 17v2h6v-2H3zM3 5v2h10V5H3zm10 16v-2h8v-2h-8v-2h-2v6h2zM7 9v2H3v2h4v2h2V9H7zm14 4v-2H11v2h10zm-6-4h2V7h4V5h-4V3h-2v6z",
    "busca": "M15.5 14h-.79l-.28-.27C15.41 12.59 16 11.11 16 9.5 16 5.91 13.09 3 9.5 3S3 5.91 3 9.5 5.91 16 9.5 16c1.61 0 3.09-.59 4.23-1.57l.27.28v.79l5 4.99L20.49 19l-4.99-5zm-6 0C7.01 14 5 11.99 5 9.5S7.01 5 9.5 5 14 7.01 14 9.5 11.99 14 9.5 14z",
    "baixar": "M19 9h-4V3H9v6H5l7 7 7-7zM5 18v2h14v-2H5z",
    "cadeado": "M18 8h-1V6c0-2.76-2.24-5-5-5S7 3.24 7 6v2H6c-1.1 0-2 .9-2 2v10c0 1.1.9 2 2 2h12c1.1 0 2-.9 2-2V10c0-1.1-.9-2-2-2zM9 6c0-1.66 1.34-3 3-3s3 1.34 3 3v2H9V6zm9 14H6V10h12v10zm-6-3c1.1 0 2-.9 2-2s-.9-2-2-2-2 .9-2 2 .9 2 2 2z",
    "vazio": "M19 3H4.99C3.88 3 3.01 3.89 3.01 5L3 19c0 1.1.88 2 1.99 2H19c1.1 0 2-.9 2-2V5c0-1.11-.9-2-2-2zm0 12h-4c0 1.66-1.35 3-3 3s-3-1.34-3-3H4.99V5H19v10z",
    "limpar": "M19 6.41L17.59 5 12 10.59 6.41 5 5 6.41 10.59 12 5 17.59 6.41 19 12 13.41 17.59 19 19 17.59 13.41 12z",
    "alerta": "M1 21h22L12 2 1 21zm12-3h-2v-2h2v2zm0-4h-2v-4h2v4z",
}

_CSS = """
:root {
  --verde:#1F6B3A; --verde-folha:#2F8A4A; --verde-suave:#E7F3EA; --creme:#F3F0E6;
  --cartao:#FFFDF8; --laranja:#E07A2F; --laranja-suave:#FBEBDD; --texto:#243028;
  --texto-suave:#5B6F60; --borda:#C9D6C3; --sombra:0 6px 22px rgba(31,107,58,.13);
}
* { box-sizing:border-box; }
body { margin:0; font-family:"Segoe UI",Roboto,Calibri,sans-serif; background:var(--creme); color:var(--texto); font-size:14px; }
svg.ic { width:1em; height:1em; fill:currentColor; flex:none; }
.topo { background:var(--cartao); box-shadow:0 2px 12px rgba(31,107,58,.09); padding:12px 28px; display:flex; align-items:center; gap:20px; position:sticky; top:0; z-index:5; }
.topo img { height:64px; max-width:200px; object-fit:contain; }
.topo .sep { width:1px; height:44px; background:var(--borda); }
.topo .titulos { flex:1; min-width:0; }
.topo h1 { margin:0; font-size:22px; color:var(--verde); font-weight:700; }
.topo p { margin:0; font-size:13px; color:var(--texto-suave); }
.chip { display:inline-flex; align-items:center; gap:8px; background:var(--verde-suave); color:var(--verde); border-radius:20px; padding:8px 14px; font-size:13px; font-weight:500; white-space:nowrap; }
.chip .ponto { width:8px; height:8px; border-radius:50%; background:var(--verde-folha); }
main { max-width:1280px; margin:0 auto; padding:24px 28px 8px; display:flex; flex-direction:column; gap:20px; }
.cartao { background:var(--cartao); border-radius:22px; padding:28px; box-shadow:var(--sombra); }
.cab { display:flex; align-items:center; gap:14px; }
.cab .textos { flex:1; min-width:0; }
.cab h2 { margin:0; font-size:17px; font-weight:700; color:var(--texto); display:flex; align-items:center; gap:8px; }
.cab p { margin:2px 0 0; font-size:13px; color:var(--texto-suave); }
.selo-icone { width:44px; height:44px; border-radius:13px; background:var(--verde-suave); color:var(--verde); display:grid; place-items:center; font-size:22px; flex:none; }
.selo-icone.laranja { background:var(--laranja-suave); color:var(--laranja); }
.contador { background:var(--verde); color:#fff; border-radius:10px; padding:2px 8px; font-size:12px; font-weight:700; }
hr { border:0; border-top:1px solid var(--borda); margin:18px 0; }
.grupos { display:grid; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)); gap:24px; }
.grupo h3 { margin:0 0 10px; font-size:13px; font-weight:600; color:var(--verde); text-transform:uppercase; letter-spacing:.04em; display:flex; align-items:center; gap:6px; }
.campos { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:12px; }
.campos .largo { grid-column:1 / -1; }
label { display:block; font-size:12px; font-weight:500; color:var(--texto-suave); margin:0 0 4px 2px; }
input { width:100%; height:46px; padding:0 16px; border:1px solid var(--borda); border-radius:14px; background:var(--verde-suave); color:var(--texto); font:inherit; font-size:15px; outline:none; transition:border-color .15s, background .15s; }
input:focus { background:#fff; border-color:var(--verde); box-shadow:0 0 0 1px var(--verde); }
input::placeholder { color:#95A597; }
.acoes { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-top:22px; }
.acoes .espaco { flex:1; }
.botao { display:inline-flex; align-items:center; gap:8px; height:44px; padding:0 22px; border-radius:14px; border:1px solid transparent; font:inherit; font-size:14px; font-weight:600; text-decoration:none; cursor:pointer; transition:filter .15s, background .15s; }
.botao.primario { background:var(--verde); color:#fff; }
.botao.primario:hover { filter:brightness(1.1); }
.botao.contorno { background:transparent; color:var(--verde); border-color:var(--borda); }
.botao.contorno:hover { background:var(--verde-suave); }
.botao.texto { background:transparent; color:var(--texto-suave); padding:0 12px; }
.botao.texto:hover { color:var(--verde); }
.botao .ic { font-size:18px; }
.tabela { margin-top:18px; border:1px solid var(--borda); border-radius:16px; overflow:auto; max-height:520px; }
table { width:100%; border-collapse:collapse; }
th { position:sticky; top:0; background:var(--verde-suave); color:var(--verde); text-align:left; font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:.03em; padding:12px 14px; white-space:nowrap; }
td { padding:11px 14px; border-top:1px solid #E3EBDF; white-space:nowrap; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
tbody tr:hover td { background:#F4F9F2; }
.tag { display:inline-block; border-radius:10px; padding:2px 10px; font-size:12px; font-weight:600; background:var(--laranja-suave); color:var(--laranja); }
.vazio { text-align:center; padding:36px 12px 16px; color:var(--texto-suave); }
.vazio .ic { font-size:52px; color:var(--borda); }
.vazio strong { display:block; color:var(--texto); font-size:15px; margin:8px 0 2px; }
.aviso { display:flex; align-items:center; gap:10px; background:#FDECEC; color:#9B2C2C; border-radius:14px; padding:12px 16px; font-weight:500; }
.aviso .ic { font-size:20px; }
.login { max-width:420px; margin:48px auto 0; width:100%; }
.login form { display:flex; flex-direction:column; gap:14px; margin-top:18px; }
.login .botao { justify-content:center; }
footer { max-width:1280px; margin:0 auto; padding:8px 28px 16px; text-align:right; font-size:12px; font-style:italic; color:var(--texto-suave); }
@media (max-width:640px) {
  .topo { padding:10px 16px; gap:12px; flex-wrap:wrap; }
  .topo img, .topo .sep { display:none; }
  main { padding:16px; }
  .cartao { padding:20px; }
  .campos { grid-template-columns:1fr; }
}
"""


def _icone(nome: str) -> str:
    return f'<svg class="ic" viewBox="0 0 24 24" aria-hidden="true"><path d="{_ICONES[nome]}"/></svg>'


def _cabecalho(icone: str, titulo: str, subtitulo: str, *, extra_titulo: str = "", acao: str = "", cor: str = "") -> str:
    return (
        f'<div class="cab"><div class="selo-icone {cor}">{_icone(icone)}</div>'
        f'<div class="textos"><h2>{escape(titulo)}{extra_titulo}</h2><p>{escape(subtitulo)}</p></div>'
        f"{acao}</div>"
    )


def _aviso(texto: str) -> str:
    return f'<div class="aviso">{_icone("alerta")}{escape(texto)}</div>' if texto else ""


def _formatar_celula(valor, tipo: str) -> str:
    if valor is None or valor == "":
        return "—"
    if tipo == "data":
        try:
            return datetime.strptime(str(valor), "%Y-%m-%d %H:%M:%S").strftime("%d/%m/%Y %H:%M")
        except ValueError:
            return str(valor)
    return str(valor)


class _Handler(BaseHTTPRequestHandler):

    def log_message(self, formato: str, *args) -> None:
        return

    def do_GET(self) -> None:
        destino = urlparse(self.path)
        params = parse_qs(destino.query)
        if destino.path in _ARQUIVOS_ESTATICOS:
            self._arquivo_estatico(destino.path)
            return
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

    def _arquivo_estatico(self, caminho: str) -> None:
        arquivo, tipo = _ARQUIVOS_ESTATICOS[caminho]
        try:
            dados = arquivo.read_bytes()
        except OSError:
            self._responder(404, "text/plain; charset=utf-8", b"Nao encontrado")
            return
        self._responder(200, tipo, dados, {"Cache-Control": "max-age=86400"})

    def _html(self, titulo: str, corpo: str) -> None:
        logo = (
            '<img src="/logo.png" alt="Hortifruti Natural da Terra"><div class="sep"></div>'
            if LOGO_PATH.is_file()
            else ""
        )
        pagina = f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(titulo)} · Hortifruti Natural da Terra</title>
  <link rel="icon" href="/favicon.ico">
  <style>{_CSS}</style>
</head>
<body>
  <header class="topo">
    {logo}
    <div class="titulos">
      <h1>Controle de etiquetas</h1>
      <p>Consulta e exportação pela rede</p>
    </div>
    <span class="chip"><span class="ponto"></span>Somente leitura</span>
  </header>
  <main>{corpo}</main>
  <footer>Feito por: André luiz</footer>
</body>
</html>"""
        self._responder(200, "text/html; charset=utf-8", pagina.encode("utf-8"))

    def _pagina_login(self, erro: str = "") -> None:
        self._html(
            "Entrar",
            f"""
            <div class="cartao login">
              {_cabecalho("cadeado", "Entrar", "Informe a chave de acesso para consultar.")}
              <form method="post" action="/entrar">
                {_aviso(erro)}
                <div>
                  <label for="chave">Chave de acesso</label>
                  <input id="chave" name="chave" type="password" autocomplete="current-password" required autofocus>
                </div>
                <p style="margin:0; font-size:13px; color:var(--texto-suave);">
                  A chave aparece no computador da portaria, em <b>Impressora e rede</b>.
                </p>
                <button class="botao primario" type="submit">Entrar</button>
              </form>
            </div>
            """,
        )

    def _param(self, params: dict[str, list[str]], nome: str) -> str:
        return (params.get(nome) or [""])[0].strip()

    def _filtros(self, params: dict[str, list[str]]) -> tuple[dict[str, str], dict[str, str]]:
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
        return filtros_merc, filtros_vis

    def _pagina_consulta(self, params: dict[str, list[str]]) -> None:
        filtros_merc, filtros_vis = self._filtros(params)
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

        def campo(nome: str, rotulo: str, valor: str, *, dica: str = "", largo: bool = False) -> str:
            classe = ' class="largo"' if largo else ""
            return (
                f'<div{classe}><label for="{escape(nome)}">{escape(rotulo)}</label>'
                f'<input id="{escape(nome)}" name="{escape(nome)}" value="{escape(valor)}" '
                f'placeholder="{escape(dica)}" autocomplete="off"></div>'
            )

        def botao_exportar(tipo: str, texto: str, classe: str = "contorno") -> str:
            return (
                f'<a class="botao {classe}" href="/exportar?tipo={tipo}&{query_export}">'
                f'{_icone("baixar")}{escape(texto)}</a>'
            )

        def secao(
            icone: str,
            titulo: str,
            tipo: str,
            colunas: tuple[tuple[str, str, str], ...],
            registros: list,
            vazio: str,
            cor: str = "",
        ) -> str:
            if registros:
                subtitulo = (
                    f"Mostrando os {len(registros)} registros mais recentes. Exporte para ver todos."
                    if len(registros) >= LIMITE_TELA
                    else f"{len(registros)} registro(s) encontrados."
                )
            else:
                subtitulo = "Nenhum registro com os filtros atuais."
            cabecalho = _cabecalho(
                icone,
                titulo,
                subtitulo,
                extra_titulo=f'<span class="contador">{len(registros)}</span>',
                acao=botao_exportar(tipo, "Exportar") if registros else "",
                cor=cor,
            )
            if not registros:
                corpo = (
                    f'<div class="vazio">{_icone("vazio")}<strong>{escape(vazio)}</strong>'
                    "Ajuste os filtros ou limpe-os para ver tudo.</div>"
                )
            else:
                cab = "".join(f"<th>{escape(nome)}</th>" for nome, _, _ in colunas)
                linhas = []
                for item in registros:
                    celulas = []
                    for _, chave, tipo_coluna in colunas:
                        valor = item[chave]
                        if chave == "saida" and not valor:
                            celulas.append('<td><span class="tag">Na loja</span></td>')
                            continue
                        classe = ' class="num"' if tipo_coluna == "numero" else ""
                        celulas.append(f"<td{classe}>{escape(_formatar_celula(valor, tipo_coluna))}</td>")
                    linhas.append(f"<tr>{''.join(celulas)}</tr>")
                corpo = (
                    f'<div class="tabela"><table><thead><tr>{cab}</tr></thead>'
                    f"<tbody>{''.join(linhas)}</tbody></table></div>"
                )
            return f'<section class="cartao">{cabecalho}{corpo}</section>'

        dica_data = "dd/mm/aaaa"
        self._html(
            "Controle de entradas",
            f"""
            {_aviso(erro)}
            <form class="cartao" method="get" action="/">
              {_cabecalho("filtro", "Filtros", "Deixe em branco para trazer tudo. Datas no formato dd/mm/aaaa.")}
              <hr>
              <div class="grupos">
                <div class="grupo">
                  <h3>{_icone("etiqueta")}Mercadorias</h3>
                  <div class="campos">
                    {campo("data_inicio", "Data inicial", filtros_merc["data_inicio"], dica=dica_data)}
                    {campo("data_fim", "Data final", filtros_merc["data_fim"], dica=dica_data)}
                    {campo("ean", "EAN", filtros_merc["ean"])}
                    {campo("colaborador", "Colaborador", filtros_merc["colaborador"])}
                    {campo("descricao", "Nome da mercadoria", filtros_merc["descricao"], largo=True)}
                  </div>
                </div>
                <div class="grupo">
                  <h3>{_icone("pessoas")}Visitantes</h3>
                  <div class="campos">
                    {campo("vis_inicio", "Data inicial", filtros_vis["data_inicio"], dica=dica_data)}
                    {campo("vis_fim", "Data final", filtros_vis["data_fim"], dica=dica_data)}
                    {campo("nome", "Visitante", filtros_vis["nome"])}
                    {campo("empresa", "Empresa", filtros_vis["empresa"])}
                  </div>
                </div>
              </div>
              <div class="acoes">
                <a class="botao texto" href="/">{_icone("limpar")}Limpar filtros</a>
                <span class="espaco"></span>
                {botao_exportar("ambos", "Exportar tudo")}
                <button class="botao primario" type="submit">{_icone("busca")}Pesquisar</button>
              </div>
            </form>
            {secao("etiqueta", "Mercadorias", "mercadorias", COLUNAS_MERCADORIAS, mercadorias,
                   "Nenhuma entrada de mercadoria encontrada")}
            {secao("pessoas", "Visitantes", "visitantes", COLUNAS_VISITANTES, visitantes,
                   "Nenhuma entrada de visitante encontrada", cor="laranja")}
            """,
        )

    def _pagina_erro_exportacao(self, mensagem: str) -> None:
        self._html(
            "Exportar",
            f'{_aviso(mensagem)}<div><a class="botao contorno" href="javascript:history.back()">Voltar</a></div>',
        )

    def _exportar(self, params: dict[str, list[str]]) -> None:
        tipo = self._param(params, "tipo") or "ambos"
        filtros_merc, filtros_vis = self._filtros(params)
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
                resumo_mercadorias(filtros_merc) if mercadorias is not None else "",
                resumo_visitantes(filtros_vis) if visitantes is not None else "",
            )
        except ValueError as exc:
            self._pagina_erro_exportacao(str(exc))
            return
        except Exception as exc:
            self._pagina_erro_exportacao(f"Falha ao montar a planilha: {exc}")
            return
        nome = "entradas.xlsx"
        self._responder(
            200,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            dados,
            {"Content-Disposition": f'attachment; filename="{nome}"'},
        )
