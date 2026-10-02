from __future__ import annotations

import hashlib
import ipaddress
import json
import secrets
import socket
import socketserver
import ssl
import sys
import threading
import time
from datetime import datetime
from html import escape
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable
from urllib.parse import parse_qs, parse_qsl, quote, unquote, urlencode, urlsplit

import acesso
import backup
import db
import monitor
import tls
from acesso import PERFIS, ROTULOS_ACAO, LoginBloqueado, Usuario
from export_excel import COLUNAS_MERCADORIAS, COLUNAS_VISITANTES, gerar_planilha_entradas
from filtros import resumo_mercadorias, resumo_visitantes
from limitador import JanelaDeslizante
from logs import log, seguranca, seguro
from paths import ICONE_PATH, LOGO_PATH

LIMITE_TELA = 300             # ao abrir o site (sem filtros): só os mais recentes
LIMITE_TELA_FILTRADA = 5000   # com filtro: procura em tudo; o teto só protege o navegador
LIMITE_LOG = 500
LIMITE_CORPO = 8192
LIMITE_COOKIE = 4096
MAX_CAMPOS = 60
VALIDADE_ENDERECOS = 60.0

# --- proteção contra DoS / abuso -------------------------------------------
TIMEOUT_CONEXAO = 15.0          # segundos por leitura/escrita (mata conexões lentas, "slowloris")
MAX_CONEXOES = 64               # conexões simultâneas no servidor
MAX_CONEXOES_IP = 16            # simultâneas por endereço
LIMITE_REQUISICOES = (300, 60.0)  # por IP: 300 requisições por minuto
LIMITE_ENTRAR = (20, 60.0)        # por IP: 20 tentativas de login por minuto (além do bloqueio por falhas)
LIMITE_EXPORTAR = (6, 60.0)       # por IP: 6 exportações por minuto
REDES_PADRAO = (
    "127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "169.254.0.0/16",
)

_limite_geral = JanelaDeslizante(*LIMITE_REQUISICOES)
_limite_entrar = JanelaDeslizante(*LIMITE_ENTRAR)
_limite_exportar = JanelaDeslizante(*LIMITE_EXPORTAR)
_aviso_rede = JanelaDeslizante(1, 600.0)  # registra no log no máximo 1 recusa por IP a cada 10 min
_aviso_limite = JanelaDeslizante(1, 60.0)

_servidor: "_Servidor | None" = None
_thread: threading.Thread | None = None
_lock = threading.Lock()
_enderecos_cache: tuple[float, list[str]] | None = None

MENSAGENS = {
    "editado": "Mercadoria atualizada.",
    "sem_mudanca": "Nada foi alterado.",
    "criado": "Conta criada.",
    "alterado": "Conta atualizada.",
    "excluido": "Conta excluída.",
    "senha_alterada": "Senha alterada com sucesso.",
}


def _interpretar_redes(extras: tuple[str, ...] | list[str]) -> tuple:
    redes = []
    for texto in (*REDES_PADRAO, *extras):
        try:
            redes.append(ipaddress.ip_network(str(texto).strip(), strict=False))
        except ValueError:
            log("remoto").warning("Rede ignorada na configuração: %s", seguro(texto, 64))
    return tuple(redes)


def ip_permitido(ip: str, redes: tuple) -> bool:
    try:
        endereco = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(endereco in rede for rede in redes if rede.version == endereco.version)


class _Servidor(ThreadingHTTPServer):
    """Servidor com número de conexões limitado, TLS opcional e rede de origem restrita."""

    allow_reuse_address = False  # no Windows, SO_REUSEADDR deixaria outro programa "roubar" a porta
    daemon_threads = True
    request_queue_size = 64

    def __init__(self, endereco, handler, contexto_tls: ssl.SSLContext | None = None,
                 redes: tuple = ()) -> None:
        self.contexto_tls = contexto_tls
        self.redes = redes or _interpretar_redes(())
        self._vagas = threading.BoundedSemaphore(MAX_CONEXOES)
        self._trava_conexoes = threading.Lock()
        self._por_ip: dict[str, int] = {}
        self._ativas: dict[int, str] = {}
        super().__init__(endereco, handler)

    @property
    def usa_https(self) -> bool:
        return self.contexto_tls is not None

    @property
    def conexoes_ativas(self) -> int:
        with self._trava_conexoes:
            return len(self._ativas)

    def server_bind(self) -> None:
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()
        if self.contexto_tls is not None:
            # O handshake TLS fica para a thread da conexão (com timeout): um cliente
            # lento no handshake não trava o servidor inteiro.
            self.socket = self.contexto_tls.wrap_socket(
                self.socket, server_side=True, do_handshake_on_connect=False
            )

    def verify_request(self, request, client_address) -> bool:
        ip = str(client_address[0])
        if not ip_permitido(ip, self.redes):
            monitor.contar("conexoes_recusadas_rede")
            if _aviso_rede.permitir(ip)[0]:
                seguranca().warning("Conexão recusada (fora da rede permitida): ip=%s", seguro(ip))
            return False
        return True

    def process_request(self, request, client_address) -> None:
        ip = str(client_address[0])
        with self._trava_conexoes:
            excedeu = self._por_ip.get(ip, 0) >= MAX_CONEXOES_IP
            if not excedeu:
                excedeu = not self._vagas.acquire(blocking=False)
                if not excedeu:
                    self._por_ip[ip] = self._por_ip.get(ip, 0) + 1
                    self._ativas[id(request)] = ip
        if excedeu:
            monitor.contar("conexoes_recusadas_limite")
            socketserver.TCPServer.shutdown_request(self, request)  # sem liberar vaga
            return
        super().process_request(request, client_address)

    def shutdown_request(self, request) -> None:
        try:
            super().shutdown_request(request)
        finally:
            with self._trava_conexoes:
                ip = self._ativas.pop(id(request), None)
                if ip is not None:
                    restante = self._por_ip.get(ip, 1) - 1
                    if restante > 0:
                        self._por_ip[ip] = restante
                    else:
                        self._por_ip.pop(ip, None)
                    self._vagas.release()

    def handle_error(self, request, client_address) -> None:
        erro = sys.exc_info()[1]
        if isinstance(erro, (OSError, ssl.SSLError, TimeoutError)):
            monitor.contar("erros_conexao")  # cliente desistiu, TLS inválido, timeout: rotina
            return
        monitor.contar("erros_internos")
        log("remoto").exception("Erro inesperado ao atender %s", seguro(client_address[0]))


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
        teste.connect(("8.8.8.8", 80))  # UDP: só descobre a interface, não envia nada
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


def urls_acesso(porta: int, https: bool = True) -> list[str]:
    esquema = "https" if https else "http"
    return [f"{esquema}://{ip}:{porta}/" for ip in enderecos_rede()]


def servidor_ativo() -> bool:
    return _servidor is not None


def estado_servidor() -> dict:
    httpd = _servidor
    return {
        "ativo": httpd is not None,
        "https": bool(httpd and httpd.usa_https),
        "conexoes": httpd.conexoes_ativas if httpd else 0,
        "porta": httpd.server_address[1] if httpd else 0,
    }


def parar_servidor() -> None:
    global _servidor, _thread
    with _lock:
        httpd = _servidor
        _servidor = None
        _thread = None
    if httpd is not None:
        httpd.shutdown()
        httpd.server_close()
        log("remoto").info("Acesso remoto desligado.")


def iniciar_servidor(porta: int, *, https: bool = True, redes_extras=()) -> None:
    global _servidor, _thread
    if porta < 1024 or porta > 65535:
        raise ValueError("A porta deve estar entre 1024 e 65535.")
    parar_servidor()
    contexto = tls.criar_contexto(enderecos_rede()) if https else None
    redes = _interpretar_redes(tuple(redes_extras))
    httpd = _Servidor(("0.0.0.0", porta), _Handler, contexto, redes)  # nosec B104 (acesso pela rede da loja; filtrado por rede de origem)
    worker = threading.Thread(target=httpd.serve_forever, name="acesso-remoto", daemon=True)
    with _lock:
        _servidor = httpd
        _thread = worker
    worker.start()
    log("remoto").info("Acesso remoto ligado na porta %d (%s).", porta, "HTTPS" if https else "HTTP")
    if not https:
        seguranca().warning("Acesso remoto ligado SEM HTTPS: senhas trafegam em texto puro na rede.")


def aplicar_acesso_remoto(ativo: bool, porta: int, https: bool = True, redes_extras=()) -> str | None:
    if not ativo:
        parar_servidor()
        return None
    try:
        iniciar_servidor(porta, https=https, redes_extras=redes_extras)
    except OSError as exc:
        return f"Não foi possível abrir a porta {porta}: {exc}"
    except (ValueError, tls.ErroTLS) as exc:
        return str(exc)
    return None


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
    "editar": "M3 17.25V21h3.75L17.81 9.94l-3.75-3.75L3 17.25zM20.71 7.04c.39-.39.39-1.02 0-1.41l-2.34-2.34c-.39-.39-1.02-.39-1.41 0l-1.83 1.83 3.75 3.75 1.83-1.83z",
    "historico": "M13 3c-4.97 0-9 4.03-9 9H1l3.89 3.89.07.14L9 12H6c0-3.87 3.13-7 7-7s7 3.13 7 7-3.13 7-7 7c-1.93 0-3.68-.79-4.94-2.06l-1.42 1.42C8.27 19.99 10.51 21 13 21c4.97 0 9-4.03 9-9s-4.03-9-9-9zm-1 5v5l4.28 2.54.72-1.21-3.5-2.08V8H12z",
    "usuario": "M12 12c2.21 0 4-1.79 4-4s-1.79-4-4-4-4 1.79-4 4 1.79 4 4 4zm0 2c-2.67 0-8 1.34-8 4v2h16v-2c0-2.66-5.33-4-8-4z",
    "salvar": "M9 16.17L4.83 12l-1.42 1.41L9 19 21 7l-1.41-1.41z",
    "lixeira": "M6 19c0 1.1.9 2 2 2h8c1.1 0 2-.9 2-2V7H6v12zM19 4h-3.5l-1-1h-5l-1 1H5v2h14V4z",
    "sair": "M17 7l-1.41 1.41L18.17 11H8v2h10.17l-2.58 2.58L17 17l5-5zM4 5h8V3H4c-1.1 0-2 .9-2 2v14c0 1.1.9 2 2 2h8v-2H4V5z",
    "mais": "M19 13h-6v6h-2v-6H5v-2h6V5h2v6h6v2z",
    "ok": "M12 2C6.48 2 2 6.48 2 12s4.48 10 10 10 10-4.48 10-10S17.52 2 12 2zm-2 15l-5-5 1.41-1.41L10 14.17l7.59-7.59L19 8l-9 9z",
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
.topo form { margin:0; }
.chip { display:inline-flex; align-items:center; gap:8px; background:var(--verde-suave); color:var(--verde); border-radius:20px; padding:8px 14px; font-size:13px; font-weight:500; white-space:nowrap; }
.chip .ponto { width:8px; height:8px; border-radius:50%; background:var(--verde-folha); }
.menu { max-width:1280px; margin:0 auto; padding:16px 28px 0; display:flex; gap:8px; flex-wrap:wrap; }
.menu a { display:inline-flex; align-items:center; gap:8px; padding:9px 16px; border-radius:12px; text-decoration:none; color:var(--texto-suave); font-weight:600; border:1px solid transparent; }
.menu a:hover { background:var(--verde-suave); color:var(--verde); }
.menu a.ativo { background:var(--verde); color:#fff; }
.menu a .ic { font-size:18px; }
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
input, select { width:100%; height:46px; padding:0 16px; border:1px solid var(--borda); border-radius:14px; background:var(--verde-suave); color:var(--texto); font:inherit; font-size:15px; outline:none; transition:border-color .15s, background .15s; }
input:focus, select:focus { background:#fff; border-color:var(--verde); box-shadow:0 0 0 1px var(--verde); }
input::placeholder { color:#95A597; }
.acoes { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin-top:22px; }
.acoes .espaco { flex:1; }
.botao { display:inline-flex; align-items:center; gap:8px; height:44px; padding:0 22px; border-radius:14px; border:1px solid transparent; font:inherit; font-size:14px; font-weight:600; text-decoration:none; cursor:pointer; transition:filter .15s, background .15s; }
.botao.primario { background:var(--verde); color:#fff; }
.botao.primario:hover { filter:brightness(1.1); }
.botao.contorno { background:transparent; color:var(--verde); border-color:var(--borda); }
.botao.contorno:hover { background:var(--verde-suave); }
.botao.perigo { background:transparent; color:#9B2C2C; border-color:#E5B8B8; }
.botao.perigo:hover { background:#FDECEC; }
.botao.texto { background:transparent; color:var(--texto-suave); padding:0 12px; }
.botao.texto:hover { color:var(--verde); }
.botao.peq { height:34px; padding:0 14px; font-size:13px; border-radius:11px; }
.botao .ic { font-size:18px; }
.tabela { margin-top:18px; border:1px solid var(--borda); border-radius:16px; overflow:auto; max-height:520px; }
table { width:100%; border-collapse:collapse; }
th { position:sticky; top:0; background:var(--verde-suave); color:var(--verde); text-align:left; font-size:12px; font-weight:700; text-transform:uppercase; letter-spacing:.03em; padding:12px 14px; white-space:nowrap; }
td { padding:11px 14px; border-top:1px solid #E3EBDF; white-space:nowrap; }
td.livre { white-space:normal; min-width:320px; }
td.num { text-align:right; font-variant-numeric:tabular-nums; }
tbody tr:hover td { background:#F4F9F2; }
.tag { display:inline-block; border-radius:10px; padding:2px 10px; font-size:12px; font-weight:600; background:var(--laranja-suave); color:var(--laranja); }
.tag.verde { background:var(--verde-suave); color:var(--verde); }
.vazio { text-align:center; padding:36px 12px 16px; color:var(--texto-suave); }
.vazio .ic { font-size:52px; color:var(--borda); }
.vazio strong { display:block; color:var(--texto); font-size:15px; margin:8px 0 2px; }
.aviso { display:flex; align-items:center; gap:10px; background:#FDECEC; color:#9B2C2C; border-radius:14px; padding:12px 16px; font-weight:500; }
.aviso .ic { font-size:20px; }
.sucesso { display:flex; align-items:center; gap:10px; background:var(--verde-suave); color:var(--verde); border-radius:14px; padding:12px 16px; font-weight:500; }
.sucesso .ic { font-size:20px; }
.integridade { display:flex; align-items:center; gap:10px; margin-top:16px; }
.login { max-width:420px; margin:48px auto 0; width:100%; }
.login form, form.coluna { display:flex; flex-direction:column; gap:14px; margin-top:18px; }
.login .botao { justify-content:center; }
.nota { margin:0; font-size:13px; color:var(--texto-suave); }
.leitura { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; margin-top:18px; }
.leitura div { background:var(--creme); border-radius:12px; padding:10px 14px; }
.leitura span { display:block; font-size:12px; color:var(--texto-suave); }
.leitura strong { font-size:14px; }
.conta { display:flex; align-items:center; gap:16px; flex-wrap:wrap; padding:14px 0; border-top:1px solid #E3EBDF; }
.conta .quem { flex:1 1 200px; min-width:160px; display:flex; align-items:center; gap:8px; flex-wrap:wrap; }
.conta form { display:flex; align-items:center; gap:10px; flex-wrap:wrap; margin:0; }
.conta select { width:170px; }
.conta input { width:220px; }
.largo-640 { max-width:640px; }
.campos.auto { grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); }
.acoes.sem-topo { margin-top:0; }
hr.sem-base { margin-bottom:0; }
footer { max-width:1280px; margin:0 auto; padding:8px 28px 16px; text-align:right; font-size:12px; font-style:italic; color:var(--texto-suave); }
@media (max-width:640px) {
  .topo { padding:10px 16px; gap:12px; flex-wrap:wrap; }
  .topo img, .topo .sep { display:none; }
  .menu { padding:12px 16px 0; }
  main { padding:16px; }
  .cartao { padding:20px; }
  .campos { grid-template-columns:1fr; }
  .conta select, .conta input { width:100%; }
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


def _sucesso(texto: str) -> str:
    return f'<div class="sucesso">{_icone("ok")}{escape(texto)}</div>' if texto else ""


def _campo(nome: str, rotulo: str, valor: str = "", *, dica: str = "", largo: bool = False,
           tipo: str = "text", extra: str = "") -> str:
    classe = ' class="largo"' if largo else ""
    return (
        f'<div{classe}><label for="{escape(nome)}">{escape(rotulo)}</label>'
        f'<input id="{escape(nome)}" name="{escape(nome)}" type="{tipo}" value="{escape(valor)}" '
        f'placeholder="{escape(dica)}" autocomplete="off" {extra}></div>'
    )


# Campos numéricos (EAN): o navegador nem deixa digitar ou colar outra coisa.
_EXTRA_NUMERICO = 'data-numerico inputmode="numeric" pattern="[0-9]*" maxlength="14"'
# Todo JavaScript fica neste arquivo estático (/app.js): a CSP não permite script inline.
_JS = r"""
(function () {
  var campos = document.querySelectorAll('input[data-numerico]');
  Array.prototype.forEach.call(campos, function (campo) {
    campo.addEventListener('beforeinput', function (e) {
      if (e.data && /\D/.test(e.data)) { e.preventDefault(); }
    });
    campo.addEventListener('input', function () {
      var limpo = campo.value.replace(/\D/g, '');
      if (limpo !== campo.value) { campo.value = limpo; }
    });
  });
  Array.prototype.forEach.call(document.querySelectorAll('form[data-confirmar]'), function (f) {
    f.addEventListener('submit', function (e) {
      if (!window.confirm(f.getAttribute('data-confirmar'))) { e.preventDefault(); }
    });
  });
  Array.prototype.forEach.call(document.querySelectorAll('[data-voltar]'), function (a) {
    a.addEventListener('click', function (e) { e.preventDefault(); window.history.back(); });
  });
})();
"""

_VERSAO_ASSETS = hashlib.sha256((_CSS + _JS).encode("utf-8")).hexdigest()[:10]
_ESTATICOS_FIXOS = {
    "/estilo.css": (_CSS.encode("utf-8"), "text/css; charset=utf-8"),
    "/app.js": (_JS.encode("utf-8"), "application/javascript; charset=utf-8"),
}
_ARQUIVOS_ESTATICOS = {
    "/logo.png": (LOGO_PATH, "image/png"),
    "/favicon.ico": (ICONE_PATH, "image/x-icon"),
}
_cache_arquivos: dict[str, tuple[bytes, str]] = {}

CSP = (
    "default-src 'none'; img-src 'self'; style-src 'self'; script-src 'self'; "
    "form-action 'self'; frame-ancestors 'none'; base-uri 'none'; connect-src 'none'"
)


def _csrf(token: str) -> str:
    return f'<input type="hidden" name="csrf" value="{escape(token)}">'


def _select_perfil(nome: str, atual: str, rotulo: str = "", id_html: str = "") -> str:
    opcoes = "".join(
        f'<option value="{escape(chave)}"{" selected" if chave == atual else ""}>{escape(texto)}</option>'
        for chave, texto in PERFIS.items()
    )
    id_html = id_html or nome
    cabecalho = f'<label for="{escape(id_html)}">{escape(rotulo)}</label>' if rotulo else ""
    return f'<div>{cabecalho}<select id="{escape(id_html)}" name="{escape(nome)}">{opcoes}</select></div>'


def _formatar_celula(valor, tipo: str) -> str:
    if valor is None or valor == "":
        return "—"
    if tipo in {"data", "data_seg"}:
        saida = "%d/%m/%Y %H:%M:%S" if tipo == "data_seg" else "%d/%m/%Y %H:%M"
        try:
            return datetime.strptime(str(valor), "%Y-%m-%d %H:%M:%S").strftime(saida)
        except ValueError:
            return str(valor)
    return str(valor)


def _voltar_seguro(texto: str) -> str:
    """Reconstrói a query string para evitar injeção de cabeçalhos/redirecionamento externo."""
    pares = parse_qsl((texto or "")[:2000], keep_blank_values=False)
    consulta = urlencode([(k, v) for k, v in pares if k != "msg"])
    return f"/?{consulta}" if consulta else "/"


def _com_mensagem(caminho: str, codigo: str) -> str:
    separador = "&" if "?" in caminho else "?"
    return f"{caminho}{separador}msg={codigo}"


_nomes_host: set[str] | None = None


def _nomes_host_permitidos() -> set[str]:
    """Nomes aceitos no cabeçalho Host (além de endereços IP). Barra DNS rebinding."""
    global _nomes_host
    if _nomes_host is None:
        nomes = {"localhost"}
        try:
            nomes.add(socket.gethostname().lower())
            nomes.add(socket.getfqdn().lower())
        except OSError:
            pass
        _nomes_host = nomes
    return _nomes_host


def host_permitido(cabecalho: str) -> bool:
    if not cabecalho:
        return True  # HTTP/1.0 sem Host; navegadores sempre enviam
    try:
        nome = urlsplit("//" + cabecalho).hostname
    except ValueError:
        return False
    if not nome:
        return False
    try:
        ipaddress.ip_address(nome)
        return True  # por IP: não há DNS para sequestrar
    except ValueError:
        return nome.lower() in _nomes_host_permitidos()


def texto_metricas() -> str:
    """Métricas em texto simples (chave valor), sem dados pessoais."""
    linhas = [f"uptime_segundos {monitor.segundos_ativo()}"]
    for nome, valor in sorted(monitor.instantaneo().items()):
        linhas.append(f"{nome} {valor}")
    estado = estado_servidor()
    linhas.append(f"conexoes_ativas {estado['conexoes']}")
    linhas.append(f"sessoes_ativas {acesso.contar_sessoes()}")
    try:
        for nome, valor in sorted(db.estatisticas().items()):
            linhas.append(f"banco_{nome} {valor}")
    except Exception:
        linhas.append("banco_erro 1")
    idade = backup.idade_ultimo_horas()
    linhas.append(f"backup_idade_horas {-1 if idade is None else round(idade, 1)}")
    integro, _ = acesso.verificar_log()
    linhas.append(f"log_auditoria_integro {1 if integro else 0}")
    return "\n".join(linhas) + "\n"


class _Handler(BaseHTTPRequestHandler):
    timeout = TIMEOUT_CONEXAO
    server_version = "Etiquetas"
    sys_version = ""
    _usuario_log = "-"

    def version_string(self) -> str:
        return "Etiquetas"  # não revela versões de Python/servidor

    def setup(self) -> None:
        super().setup()
        if isinstance(self.connection, ssl.SSLSocket):
            self.connection.do_handshake()  # com timeout; falha cai em handle_error

    def log_message(self, formato: str, *args) -> None:
        return

    # ------------------------------------------------------------ utilidades

    @property
    def _https(self) -> bool:
        return bool(getattr(self.server, "usa_https", False))

    @property
    def _nome_cookie(self) -> str:
        # O prefixo __Host- obriga Secure + Path=/ + sem Domain: o navegador recusa variações.
        return "__Host-sessao" if self._https else "sessao"

    def _ip(self) -> str:
        return str(self.client_address[0])

    def _token_cookie(self) -> str:
        bruto = self.headers.get("Cookie", "")
        if not bruto or len(bruto) > LIMITE_COOKIE:
            return ""
        cookie = SimpleCookie()
        try:
            cookie.load(bruto)
        except Exception:
            return ""
        if self._nome_cookie not in cookie:
            return ""
        return unquote(cookie[self._nome_cookie].value or "")

    def _sessao(self) -> tuple[Usuario, str] | None:
        sessao = acesso.obter_sessao(self._token_cookie(), self._ip())
        if sessao is not None:
            self._usuario_log = sessao[0].usuario
        return sessao

    def _cabecalhos_seguranca(self) -> dict[str, str]:
        cabecalhos = {
            "Cache-Control": "no-store",
            "Content-Security-Policy": CSP,
            "X-Frame-Options": "DENY",
            "X-Content-Type-Options": "nosniff",
            # same-origin: nada vaza para outros sites, e o navegador continua mandando
            # o cabeçalho Origin correto nos formulários (com "no-referrer" ele manda "null").
            "Referrer-Policy": "same-origin",
            "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=(), usb=()",
            # Sem CORS: nenhum site de fora pode ler respostas, e o navegador não
            # deve embutir estes recursos em outras origens.
            "Cross-Origin-Opener-Policy": "same-origin",
            "Cross-Origin-Resource-Policy": "same-origin",
        }
        if self._https:
            cabecalhos["Strict-Transport-Security"] = "max-age=2592000"
        return cabecalhos

    def _registrar_acesso(self, status: int) -> None:
        monitor.contar("requisicoes")
        monitor.contar(f"respostas_{status // 100}xx")
        estatico = self.command == "GET" and urlsplit(self.path).path in (
            *_ESTATICOS_FIXOS, *_ARQUIVOS_ESTATICOS
        )
        if estatico and status < 400:
            return
        nivel = "warning" if status >= 500 else "info"
        getattr(log("acesso"), nivel)(
            "%s %s %s -> %d (%s)",
            seguro(self._ip()), self.command, seguro(urlsplit(self.path).path, 120),
            status, seguro(self._usuario_log, 64),
        )

    def _responder(self, status: int, tipo: str, corpo: bytes, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        cabecalhos = {
            "Content-Type": tipo,
            "Content-Length": str(len(corpo)),
            **self._cabecalhos_seguranca(),
            **(extra or {}),
        }
        for nome, valor in cabecalhos.items():
            self.send_header(nome, valor)
        self.end_headers()
        self.wfile.write(corpo)
        self._registrar_acesso(status)

    def _redirecionar(self, destino: str, cookie: str = "") -> None:
        self.send_response(303)
        self.send_header("Location", destino)
        self.send_header("Content-Length", "0")
        for nome, valor in self._cabecalhos_seguranca().items():
            self.send_header(nome, valor)
        if cookie:
            self.send_header("Set-Cookie", cookie)
        self.end_headers()
        self._registrar_acesso(303)

    def _arquivo_estatico(self, caminho: str) -> None:
        if caminho in _ESTATICOS_FIXOS:
            dados, tipo = _ESTATICOS_FIXOS[caminho]
        else:
            if caminho not in _cache_arquivos:
                arquivo, tipo = _ARQUIVOS_ESTATICOS[caminho]
                try:
                    _cache_arquivos[caminho] = (arquivo.read_bytes(), tipo)
                except OSError:
                    self._responder(404, "text/plain; charset=utf-8", b"Nao encontrado")
                    return
            dados, tipo = _cache_arquivos[caminho]
        self._responder(200, tipo, dados, {"Cache-Control": "public, max-age=86400"})

    def _param(self, params: dict[str, list[str]], nome: str) -> str:
        return (params.get(nome) or [""])[0].strip()

    def _corpo_form(self) -> dict[str, list[str]] | None:
        if self.headers.get("Transfer-Encoding"):
            return None  # corpo "chunked" não é aceito (contrabando de requisições)
        try:
            tamanho = int(self.headers.get("Content-Length", "0") or 0)
        except ValueError:
            return None
        if tamanho < 0 or tamanho > LIMITE_CORPO:
            return None
        bruto = self.rfile.read(tamanho).decode("utf-8", "ignore")
        try:
            return parse_qs(bruto, keep_blank_values=True, max_num_fields=MAX_CAMPOS)
        except ValueError:
            return None

    @staticmethod
    def _valor(dados: dict[str, list[str]], nome: str, *, cortar: bool = True) -> str:
        valor = (dados.get(nome) or [""])[0]
        return valor.strip() if cortar else valor

    @staticmethod
    def _inteiro(texto: str) -> int:
        try:
            numero = int(texto)
        except ValueError:
            return -1
        return numero if 0 <= numero < 2**62 else -1  # cabe no INTEGER do SQLite

    # ----------------------------------------------------------- estrutura

    def _html(
        self,
        titulo: str,
        corpo: str,
        sessao: tuple[Usuario, str] | None = None,
        menu: str = "",
        status: int = 200,
        extra: dict[str, str] | None = None,
    ) -> None:
        logo = (
            '<img src="/logo.png" alt="Hortifruti Natural da Terra"><div class="sep"></div>'
            if LOGO_PATH.is_file()
            else ""
        )
        area_usuario = ""
        navegacao = ""
        subtitulo = "Consulta, edição e exportação pela rede"
        if sessao is not None:
            usuario, csrf = sessao
            area_usuario = (
                f'<span class="chip"><span class="ponto"></span>'
                f"{escape(usuario.usuario)} · {escape(usuario.rotulo_perfil)}</span>"
                f'<form method="post" action="/sair">{_csrf(csrf)}'
                f'<button class="botao texto" type="submit">{_icone("sair")}Sair</button></form>'
            )
            itens = [("consulta", "/", "etiqueta", "Consulta"),
                     ("log", "/log", "historico", "Log de alterações")]
            if usuario.pode("usuarios"):
                itens.append(("usuarios", "/usuarios", "usuario", "Usuários"))
            itens.append(("senha", "/senha", "cadeado", "Alterar senha"))
            if usuario.trocar_senha:
                itens = []  # troca obrigatória: nada além da própria tela de senha
            links = "".join(
                f'<a href="{href}"{" class=ativo" if chave == menu else ""}>{_icone(icone)}{escape(texto)}</a>'
                for chave, href, icone, texto in itens
            )
            navegacao = f'<nav class="menu">{links}</nav>' if links else ""
        pagina = f"""<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{escape(titulo)} · Hortifruti Natural da Terra</title>
  <link rel="icon" href="/favicon.ico">
  <link rel="stylesheet" href="/estilo.css?v={_VERSAO_ASSETS}">
</head>
<body>
  <header class="topo">
    {logo}
    <div class="titulos">
      <h1>Controle de etiquetas</h1>
      <p>{escape(subtitulo)}</p>
    </div>
    {area_usuario}
  </header>
  {navegacao}
  <main>{corpo}</main>
  <footer>Feito por: André luiz</footer>
  <script src="/app.js?v={_VERSAO_ASSETS}"></script>
</body>
</html>"""
        self._responder(status, "text/html; charset=utf-8", pagina.encode("utf-8"), extra)

    def _pagina_negada(self, sessao: tuple[Usuario, str], texto: str = "Seu perfil não tem permissão para esta página.") -> None:
        self._html("Acesso negado", _aviso(texto), sessao, status=403)

    # ------------------------------------------------------------- roteamento

    def _nao_permitido(self) -> None:
        self._responder(405, "text/plain; charset=utf-8", b"Metodo nao permitido", {"Allow": "GET, POST"})

    # Só GET e POST existem. Sem OPTIONS: não há CORS, nenhum site de fora fala com este servidor.
    do_HEAD = do_OPTIONS = do_PUT = do_DELETE = do_PATCH = _nao_permitido

    def _limitado(self, espera: int) -> None:
        if _aviso_limite.permitir(self._ip())[0]:
            seguranca().warning("Limite de requisições excedido: ip=%s", seguro(self._ip()))
        self._responder(
            429, "text/plain; charset=utf-8", "Muitas requisições. Aguarde um instante.".encode("utf-8"),
            {"Retry-After": str(espera)},
        )

    def _verificacoes_iniciais(self) -> bool:
        """Limite de taxa, tamanho da URL e cabeçalho Host. False se já respondeu."""
        permitido, espera = _limite_geral.permitir(self._ip())
        if not permitido:
            monitor.contar("limite_taxa_excedido")
            self._limitado(espera)
            return False
        if len(self.path) > 4096:
            self._responder(414, "text/plain; charset=utf-8", b"URL longa demais")
            return False
        if not host_permitido(self.headers.get("Host", "")):
            monitor.contar("host_invalido")
            seguranca().warning("Cabeçalho Host recusado: host=%s ip=%s",
                                seguro(self.headers.get("Host", ""), 100), seguro(self._ip()))
            self._responder(421, "text/plain; charset=utf-8", b"Host nao reconhecido")
            return False
        return True

    def _origem_ok(self) -> bool:
        """Em POST, exige que a requisição venha desta mesma origem (defesa extra além do CSRF)."""
        origem = self.headers.get("Origin")
        relacao = self.headers.get("Sec-Fetch-Site")
        if origem == "null":
            # Navegadores enviam "null" quando a política de referrer é restrita ou a página
            # veio de redirecionamento. Só vale se o próprio navegador afirma "mesma origem";
            # sem essa afirmação (HTTP em rede local não envia Sec-Fetch-*), o token CSRF
            # e o cookie SameSite=Strict continuam protegendo.
            return relacao in (None, "same-origin")
        if origem:
            partes = urlsplit(origem)
            esquema = "https" if self._https else "http"
            return partes.scheme == esquema and partes.netloc.lower() == self.headers.get("Host", "").lower()
        return relacao in (None, "same-origin", "none")

    def _executar(self, tratar: Callable[[], None]) -> None:
        try:
            if self._verificacoes_iniciais():
                tratar()
        except OSError:
            monitor.contar("erros_conexao")  # cliente foi embora / timeout / TLS: rotina
        except Exception:
            monitor.contar("erros_internos")
            log("remoto").exception("Erro ao atender %s %s", self.command, seguro(urlsplit(self.path).path))
            try:
                self._responder(500, "text/plain; charset=utf-8", b"Erro interno")
            except OSError:
                pass

    def do_GET(self) -> None:
        self._executar(self._tratar_get)

    def do_POST(self) -> None:
        self._executar(self._tratar_post)

    def _saude(self) -> None:
        """Verificação de funcionamento para monitoramento. Não revela dados."""
        try:
            with db.conexao() as conn:
                conn.execute("SELECT 1").fetchone()
            status, corpo = 200, {"status": "ok"}
        except Exception:
            log("remoto").exception("Verificação de saúde falhou.")
            status, corpo = 503, {"status": "erro"}
        self._responder(status, "application/json; charset=utf-8", json.dumps(corpo).encode("ascii"))

    def _metricas(self, sessao: tuple[Usuario, str]) -> None:
        if not sessao[0].pode("usuarios"):
            self._pagina_negada(sessao)
            return
        self._responder(200, "text/plain; charset=utf-8", texto_metricas().encode("utf-8"))

    def _tratar_get(self) -> None:
        destino = urlsplit(self.path)
        try:
            params = parse_qs(destino.query, max_num_fields=MAX_CAMPOS)
        except ValueError:
            self._responder(400, "text/plain; charset=utf-8", b"Requisicao invalida")
            return
        caminho = destino.path or "/"
        if caminho in _ESTATICOS_FIXOS or caminho in _ARQUIVOS_ESTATICOS:
            self._arquivo_estatico(caminho)
            return
        if caminho == "/saude":
            self._saude()
            return
        if caminho not in {"/", "/exportar", "/log", "/usuarios", "/editar", "/senha", "/metricas"}:
            self._responder(404, "text/plain; charset=utf-8", b"Nao encontrado")
            return
        sessao = self._sessao()
        if sessao is None:
            if caminho == "/":
                self._pagina_login()
            else:
                self._redirecionar("/")
            return
        usuario, _ = sessao
        if usuario.trocar_senha and caminho != "/senha":
            self._redirecionar("/senha")
            return
        if caminho == "/senha":
            self._pagina_senha(sessao)
        elif caminho == "/":
            self._pagina_consulta(params, sessao, destino.query)
        elif caminho == "/exportar":
            self._exportar(params, sessao)
        elif caminho == "/metricas":
            self._metricas(sessao)
        elif caminho == "/log":
            self._pagina_log(params, sessao)
        elif caminho == "/usuarios":
            if not usuario.pode("usuarios"):
                self._pagina_negada(sessao)
                return
            self._pagina_usuarios(sessao, ok=MENSAGENS.get(self._param(params, "msg"), ""))
        elif caminho == "/editar":
            if not usuario.pode("editar"):
                self._pagina_negada(sessao)
                return
            self._pagina_editar_get(params, sessao)

    def _tratar_post(self) -> None:
        caminho = urlsplit(self.path).path
        if caminho not in {"/entrar", "/sair", "/senha", "/editar", "/usuarios/criar",
                           "/usuarios/alterar", "/usuarios/excluir"}:
            self._responder(404, "text/plain; charset=utf-8", b"Nao encontrado")
            return
        if not self._origem_ok():
            monitor.contar("origem_invalida")
            seguranca().warning("POST de origem diferente recusado: origin=%s ip=%s",
                                seguro(self.headers.get("Origin", "-"), 100), seguro(self._ip()))
            self._responder(403, "text/plain; charset=utf-8", b"Origem nao permitida")
            return
        dados = self._corpo_form()
        if dados is None:
            self._responder(413, "text/plain; charset=utf-8", b"Requisicao invalida")
            return
        if caminho == "/entrar":
            permitido, espera = _limite_entrar.permitir(self._ip())
            if not permitido:
                monitor.contar("limite_taxa_excedido")
                self._limitado(espera)
                return
            self._entrar(dados)
            return
        sessao = self._sessao()
        if sessao is None:
            self._redirecionar("/")
            return
        usuario, csrf = sessao
        recebido = self._valor(dados, "csrf")
        if not recebido or not secrets.compare_digest(recebido, csrf):
            monitor.contar("csrf_invalido")
            seguranca().warning("Token CSRF inválido: usuario=%s ip=%s caminho=%s",
                                seguro(usuario.usuario), seguro(self._ip()), caminho)
            self._pagina_negada(sessao, "Sessão expirada ou formulário inválido. Volte e tente novamente.")
            return
        if usuario.trocar_senha and caminho not in {"/senha", "/sair"}:
            self._redirecionar("/senha")
            return
        if caminho == "/sair":
            self._sair(usuario)
        elif caminho == "/senha":
            self._senha_post(dados, sessao)
        elif caminho == "/editar":
            self._editar_post(dados, sessao)
        else:
            if not usuario.pode("usuarios"):
                self._pagina_negada(sessao)
                return
            if caminho == "/usuarios/criar":
                self._usuario_criar(dados, sessao)
            elif caminho == "/usuarios/alterar":
                self._usuario_alterar(dados, sessao)
            else:
                self._usuario_excluir(dados, sessao)

    # ------------------------------------------------------------ login/sair

    def _cookie_sessao(self, token: str, *, apagar: bool = False) -> str:
        atributos = "Path=/; HttpOnly; SameSite=Strict" + ("; Secure" if self._https else "")
        if apagar:
            return f"{self._nome_cookie}=; {atributos}; Max-Age=0"
        return f"{self._nome_cookie}={quote(token)}; {atributos}"

    def _pagina_login(self, erro: str = "", usuario: str = "", status: int = 200) -> None:
        aviso_contas = "Use o usuário e a senha fornecidos pelo administrador."
        self._html(
            "Entrar",
            f"""
            <div class="cartao login">
              {_cabecalho("cadeado", "Entrar", "Informe seu usuário e senha para continuar.")}
              <form method="post" action="/entrar">
                {_aviso(erro)}
                <div>
                  <label for="usuario">Usuário</label>
                  <input id="usuario" name="usuario" type="text" value="{escape(usuario)}" autocomplete="username" required autofocus>
                </div>
                <div>
                  <label for="senha">Senha</label>
                  <input id="senha" name="senha" type="password" autocomplete="current-password" required>
                </div>
                <p class="nota">{aviso_contas}</p>
                <button class="botao primario" type="submit">Entrar</button>
              </form>
            </div>
            """,
            status=status,
        )

    def _entrar(self, dados: dict[str, list[str]]) -> None:
        nome = self._valor(dados, "usuario")
        senha = self._valor(dados, "senha", cortar=False)
        try:
            usuario = acesso.autenticar(nome, senha, self._ip())
        except LoginBloqueado as exc:
            monitor.contar("login_bloqueado")
            self._pagina_login(str(exc), nome, status=429)
            return
        if usuario is None:
            monitor.contar("login_falhou")
            self._pagina_login("Usuário ou senha incorretos.", nome, status=401)
            return
        monitor.contar("login_ok")
        acesso.encerrar_sessao(self._token_cookie())
        token, _ = acesso.iniciar_sessao(usuario, self._ip())
        self._redirecionar("/", self._cookie_sessao(token))

    def _pagina_senha(self, sessao: tuple[Usuario, str], erro: str = "") -> None:
        usuario, csrf = sessao
        if usuario.trocar_senha:
            subtitulo = "Por segurança, defina uma nova senha antes de continuar."
        else:
            subtitulo = "Informe a senha atual e escolha uma nova."
        self._html(
            "Alterar senha",
            f"""
            <div class="cartao login">
              {_cabecalho("cadeado", "Alterar senha", subtitulo)}
              <form method="post" action="/senha">
                {_aviso(erro)}
                {_csrf(csrf)}
                {_campo("senha_atual", "Senha atual", tipo="password", extra='maxlength="128" required autofocus autocomplete="current-password"')}
                {_campo("nova_senha", "Nova senha (mínimo 8 caracteres)", tipo="password", extra='maxlength="128" required autocomplete="new-password"')}
                {_campo("confirmar_senha", "Confirmar nova senha", tipo="password", extra='maxlength="128" required autocomplete="new-password"')}
                <button class="botao primario" type="submit">{_icone("salvar")}Salvar nova senha</button>
              </form>
            </div>
            """,
            sessao,
            menu="senha",
        )

    def _senha_post(self, dados: dict[str, list[str]], sessao: tuple[Usuario, str]) -> None:
        usuario, _ = sessao
        try:
            acesso.alterar_propria_senha(
                usuario,
                self._valor(dados, "senha_atual", cortar=False),
                self._valor(dados, "nova_senha", cortar=False),
                self._valor(dados, "confirmar_senha", cortar=False),
                self._ip(),
            )
        except ValueError as exc:
            self._pagina_senha(sessao, str(exc))
            return
        acesso.encerrar_sessoes_usuario(usuario.id, manter=self._token_cookie())
        self._redirecionar("/?msg=senha_alterada")

    def _sair(self, usuario: Usuario) -> None:
        acesso.registrar_saida(usuario, self._ip())
        acesso.encerrar_sessao(self._token_cookie())
        self._redirecionar("/", self._cookie_sessao("", apagar=True))

    # -------------------------------------------------------------- consulta

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

    def _pagina_consulta(self, params: dict[str, list[str]], sessao: tuple[Usuario, str], query_atual: str) -> None:
        usuario, _ = sessao
        filtros_merc, filtros_vis = self._filtros(params)
        erro = ""
        mercadorias: list = []
        visitantes: list = []
        # Sem filtro: últimos 300. Com filtro: a busca percorre todos os registros.
        limite_merc = LIMITE_TELA_FILTRADA if any(filtros_merc.values()) else LIMITE_TELA
        limite_vis = LIMITE_TELA_FILTRADA if any(filtros_vis.values()) else LIMITE_TELA
        try:
            mercadorias = db.consultar_entradas_produto(limite=limite_merc, **filtros_merc)
            visitantes = db.consultar_entradas_visitante(limite=limite_vis, **filtros_vis)
        except ValueError as exc:
            erro = str(exc)

        query_export = urlencode({k: v for k, v in {**filtros_merc, **{
            "nome": filtros_vis["nome"],
            "empresa": filtros_vis["empresa"],
            "vis_inicio": filtros_vis["data_inicio"],
            "vis_fim": filtros_vis["data_fim"],
        }}.items() if v})
        voltar = urlencode([(k, v) for k, v in parse_qsl(query_atual) if k != "msg"])
        pode_editar = usuario.pode("editar")

        def botao_exportar(tipo: str, texto: str, classe: str = "contorno") -> str:
            return (
                f'<a class="botao {classe}" href="/exportar?tipo={tipo}&{query_export}">'
                f'{_icone("baixar")}{escape(texto)}</a>'
            )

        def link_editar(item) -> str:
            alvo = urlencode({"id": item["id"], "voltar": voltar})
            return (
                f'<td><a class="botao contorno peq" href="/editar?{alvo}">'
                f'{_icone("editar")}Editar</a></td>'
            )

        def secao(
            icone: str,
            titulo: str,
            tipo: str,
            colunas: tuple[tuple[str, str, str], ...],
            registros: list,
            vazio: str,
            cor: str = "",
            acao_linha: Callable | None = None,
            limite: int = LIMITE_TELA,
        ) -> str:
            if registros:
                if len(registros) >= limite:
                    subtitulo = (
                        f"Mostrando os {len(registros)} registros mais recentes. "
                        "Use os filtros para procurar os demais."
                        if limite == LIMITE_TELA
                        else f"Mostrando os {len(registros)} registros mais recentes deste filtro. "
                        "Refine o filtro ou exporte para ver todos."
                    )
                else:
                    subtitulo = f"{len(registros)} registro(s) encontrados."
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
                    "Ajuste os filtros ou limpe-os para ver os mais recentes.</div>"
                )
            else:
                cab = "".join(f"<th>{escape(nome)}</th>" for nome, _, _ in colunas)
                if acao_linha:
                    cab += "<th>Ações</th>"
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
                    if acao_linha:
                        celulas.append(acao_linha(item))
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
            {_sucesso(MENSAGENS.get(self._param(params, "msg"), ""))}
            {_aviso(erro)}
            <form class="cartao" method="get" action="/">
              {_cabecalho("filtro", "Filtros", "Sem filtros, mostra os 300 mais recentes; ao filtrar, procura em todos os registros. Datas no formato dd/mm/aaaa.")}
              <hr>
              <div class="grupos">
                <div class="grupo">
                  <h3>{_icone("etiqueta")}Mercadorias</h3>
                  <div class="campos">
                    {_campo("data_inicio", "Data inicial", filtros_merc["data_inicio"], dica=dica_data)}
                    {_campo("data_fim", "Data final", filtros_merc["data_fim"], dica=dica_data)}
                    {_campo("ean", "EAN", filtros_merc["ean"], extra=_EXTRA_NUMERICO)}
                    {_campo("colaborador", "Colaborador", filtros_merc["colaborador"])}
                    {_campo("descricao", "Nome da mercadoria", filtros_merc["descricao"], largo=True)}
                  </div>
                </div>
                <div class="grupo">
                  <h3>{_icone("pessoas")}Visitantes</h3>
                  <div class="campos">
                    {_campo("vis_inicio", "Data inicial", filtros_vis["data_inicio"], dica=dica_data)}
                    {_campo("vis_fim", "Data final", filtros_vis["data_fim"], dica=dica_data)}
                    {_campo("nome", "Visitante", filtros_vis["nome"])}
                    {_campo("empresa", "Empresa", filtros_vis["empresa"])}
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
                   "Nenhuma entrada de mercadoria encontrada",
                   acao_linha=link_editar if pode_editar else None, limite=limite_merc)}
            {secao("pessoas", "Visitantes", "visitantes", COLUNAS_VISITANTES, visitantes,
                   "Nenhuma entrada de visitante encontrada", cor="laranja", limite=limite_vis)}
            """,
            sessao,
            menu="consulta",
        )

    def _pagina_erro_exportacao(self, mensagem: str, status: int = 400) -> None:
        self._html(
            "Exportar",
            f'{_aviso(mensagem)}<div><a class="botao contorno" href="/" data-voltar>Voltar</a></div>',
            status=status,
        )

    def _exportar(self, params: dict[str, list[str]], sessao: tuple[Usuario, str]) -> None:
        usuario, _ = sessao
        permitido, espera = _limite_exportar.permitir(self._ip())
        if not permitido:
            monitor.contar("limite_taxa_excedido")
            self._pagina_erro_exportacao(
                f"Muitas exportações seguidas. Aguarde {espera} segundo(s).", status=429
            )
            return
        tipo = self._param(params, "tipo") or "ambos"
        if tipo not in {"mercadorias", "visitantes", "ambos"}:
            self._pagina_erro_exportacao("Tipo de exportação inválido.")
            return
        filtros_merc, filtros_vis = self._filtros(params)
        resumo_m = resumo_v = ""
        try:
            mercadorias = None
            visitantes = None
            if tipo in {"mercadorias", "ambos"}:
                mercadorias = db.consultar_entradas_produto(limite=None, **filtros_merc)
                resumo_m = resumo_mercadorias(filtros_merc)
            if tipo in {"visitantes", "ambos"}:
                visitantes = db.consultar_entradas_visitante(limite=None, **filtros_vis)
                resumo_v = resumo_visitantes(filtros_vis)
            dados = gerar_planilha_entradas(mercadorias, visitantes, resumo_m, resumo_v)
        except ValueError as exc:
            self._pagina_erro_exportacao(str(exc))
            return
        except Exception:
            monitor.contar("erros_internos")
            log("remoto").exception("Falha ao montar a planilha.")
            self._pagina_erro_exportacao("Não foi possível montar a planilha. Tente novamente.", status=500)
            return
        total = len(mercadorias or []) + len(visitantes or [])
        acesso.registrar_exportacao(
            usuario, tipo, " / ".join(t for t in (resumo_m, resumo_v) if t), total, self._ip()
        )
        monitor.contar("exportacoes")
        nome = "entradas.xlsx"
        self._responder(
            200,
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            dados,
            {"Content-Disposition": f'attachment; filename="{nome}"'},
        )

    # ---------------------------------------------------------------- edição

    def _pagina_editar(
        self,
        sessao: tuple[Usuario, str],
        produto,
        *,
        ean: str,
        descricao: str,
        voltar: str,
        erro: str = "",
    ) -> None:
        _, csrf = sessao
        cancelar = _voltar_seguro(voltar)
        self._html(
            "Editar mercadoria",
            f"""
            <div class="cartao login largo-640">
              {_cabecalho("editar", "Editar mercadoria", "Somente o EAN e o nome podem ser alterados. A mudança fica registrada no log.")}
              <div class="leitura">
                <div><span>Registro</span><strong>nº {produto["id"]}</strong></div>
                <div><span>Colaborador</span><strong>{escape(str(produto["colaborador"]))}</strong></div>
                <div><span>Quantidade</span><strong>{escape(str(produto["quantidade"]))}</strong></div>
                <div><span>Data e hora</span><strong>{escape(_formatar_celula(produto["data_hora"], "data"))}</strong></div>
              </div>
              <form method="post" action="/editar">
                {_aviso(erro)}
                {_csrf(csrf)}
                <input type="hidden" name="id" value="{produto["id"]}">
                <input type="hidden" name="voltar" value="{escape(voltar)}">
                {_campo("ean", "EAN", ean, extra=_EXTRA_NUMERICO)}
                {_campo("descricao", "Nome da mercadoria", descricao, extra='maxlength="120" required autofocus')}
                <div class="acoes">
                  <a class="botao texto" href="{escape(cancelar)}">Cancelar</a>
                  <span class="espaco"></span>
                  <button class="botao primario" type="submit">{_icone("salvar")}Salvar</button>
                </div>
              </form>
            </div>
            """,
            sessao,
            menu="consulta",
        )

    def _pagina_editar_get(self, params: dict[str, list[str]], sessao: tuple[Usuario, str]) -> None:
        produto = acesso.obter_produto(self._inteiro(self._param(params, "id")))
        if produto is None:
            self._html("Editar mercadoria", _aviso("Registro não encontrado."), sessao, menu="consulta", status=404)
            return
        self._pagina_editar(
            sessao, produto,
            ean=str(produto["ean"]), descricao=str(produto["descricao"]),
            voltar=self._param(params, "voltar"),
        )

    def _editar_post(self, dados: dict[str, list[str]], sessao: tuple[Usuario, str]) -> None:
        usuario, _ = sessao
        registro_id = self._inteiro(self._valor(dados, "id"))
        ean = self._valor(dados, "ean")
        descricao = self._valor(dados, "descricao")
        voltar = self._valor(dados, "voltar")
        try:
            alterou = acesso.editar_produto(usuario, registro_id, ean, descricao, self._ip())
        except PermissionError:
            self._pagina_negada(sessao)
            return
        except ValueError as exc:
            produto = acesso.obter_produto(registro_id)
            if produto is None:
                self._html("Editar mercadoria", _aviso(str(exc)), sessao, menu="consulta", status=404)
                return
            self._pagina_editar(sessao, produto, ean=ean, descricao=descricao, voltar=voltar, erro=str(exc))
            return
        codigo = "editado" if alterou else "sem_mudanca"
        self._redirecionar(_com_mensagem(_voltar_seguro(voltar), codigo))

    # ------------------------------------------------------------------- log

    def _pagina_log(self, params: dict[str, list[str]], sessao: tuple[Usuario, str]) -> None:
        filtros = {
            "usuario": self._param(params, "usuario"),
            "acao": self._param(params, "acao"),
            "data_inicio": self._param(params, "data_inicio"),
            "data_fim": self._param(params, "data_fim"),
        }
        erro = ""
        registros: list = []
        try:
            registros = acesso.listar_log(limite=LIMITE_LOG, **filtros)
        except ValueError as exc:
            erro = str(exc)
        integro, resumo_integridade = acesso.verificar_log()

        opcoes_acao = '<option value="">Todas</option>' + "".join(
            f'<option value="{escape(chave)}"{" selected" if chave == filtros["acao"] else ""}>{escape(texto)}</option>'
            for chave, texto in ROTULOS_ACAO.items()
        )
        if integro:
            selo = f'<div class="sucesso integridade">{_icone("ok")}Integridade: {escape(resumo_integridade)}</div>'
        else:
            selo = (
                f'<div class="aviso integridade">{_icone("alerta")}'
                f"ATENÇÃO — o log parece ter sido adulterado: {escape(resumo_integridade)}</div>"
            )

        if registros:
            linhas = []
            for item in registros:
                perfil = PERFIS.get(item["perfil"], "Computador local" if item["perfil"] == "local" else "—")
                acao = ROTULOS_ACAO.get(item["acao"], item["acao"])
                marca = "tag" if item["acao"] == "login_falhou" else "tag verde"
                linhas.append(
                    "<tr>"
                    f"<td>{escape(_formatar_celula(item['data_hora'], 'data_seg'))}</td>"
                    f"<td>{escape(item['usuario'])}</td>"
                    f"<td>{escape(perfil)}</td>"
                    f'<td><span class="{marca}">{escape(acao)}</span></td>'
                    f'<td class="livre">{escape(item["detalhes"])}</td>'
                    f"<td>{escape(item['ip'] or '—')}</td>"
                    "</tr>"
                )
            cab = "".join(f"<th>{t}</th>" for t in ("Data e hora", "Usuário", "Perfil", "Ação", "Detalhes", "Origem"))
            tabela = (
                f'<div class="tabela"><table><thead><tr>{cab}</tr></thead>'
                f"<tbody>{''.join(linhas)}</tbody></table></div>"
            )
            subtitulo = (
                f"Mostrando os {len(registros)} registros mais recentes."
                if len(registros) >= LIMITE_LOG
                else f"{len(registros)} registro(s) encontrados."
            )
        else:
            tabela = (
                f'<div class="vazio">{_icone("vazio")}<strong>Nenhum registro encontrado</strong>'
                "Ajuste os filtros ou limpe-os para ver tudo.</div>"
            )
            subtitulo = "Nenhum registro com os filtros atuais."

        dica_data = "dd/mm/aaaa"
        self._html(
            "Log de alterações",
            f"""
            {_aviso(erro)}
            <form class="cartao" method="get" action="/log">
              {_cabecalho("filtro", "Filtros do log", "Somente leitura: os registros não podem ser editados nem apagados.")}
              <hr>
              <div class="campos auto">
                {_campo("data_inicio", "Data inicial", filtros["data_inicio"], dica=dica_data)}
                {_campo("data_fim", "Data final", filtros["data_fim"], dica=dica_data)}
                {_campo("usuario", "Usuário", filtros["usuario"])}
                <div><label for="acao">Ação</label><select id="acao" name="acao">{opcoes_acao}</select></div>
              </div>
              <div class="acoes">
                <a class="botao texto" href="/log">{_icone("limpar")}Limpar filtros</a>
                <span class="espaco"></span>
                <button class="botao primario" type="submit">{_icone("busca")}Pesquisar</button>
              </div>
            </form>
            <section class="cartao">
              {_cabecalho("historico", "Log de alterações", subtitulo,
                          extra_titulo=f'<span class="contador">{len(registros)}</span>', cor="laranja")}
              {selo}
              {tabela}
            </section>
            """,
            sessao,
            menu="log",
        )

    # -------------------------------------------------------------- usuários

    def _pagina_usuarios(
        self,
        sessao: tuple[Usuario, str],
        *,
        erro: str = "",
        ok: str = "",
        novo_usuario: str = "",
        novo_perfil: str = acesso.PERFIL_VISUALIZACAO,
    ) -> None:
        atual, csrf = sessao
        contas = []
        for conta in acesso.listar_usuarios():
            eu = conta["id"] == atual.id
            nome = escape(conta["usuario"])
            excluir = (
                ""
                if eu
                else (
                    f'<form method="post" action="/usuarios/excluir" '
                    f'data-confirmar="Excluir a conta {nome}?">'
                    f'{_csrf(csrf)}<input type="hidden" name="id" value="{conta["id"]}">'
                    f'<button class="botao perigo peq" type="submit">{_icone("lixeira")}Excluir</button></form>'
                )
            )
            contas.append(
                f'<div class="conta"><div class="quem"><strong>{nome}</strong>'
                f'<span class="tag verde">{escape(PERFIS.get(conta["perfil"], conta["perfil"]))}</span>'
                f'{"<span class=nota>(você)</span>" if eu else ""}</div>'
                f'<form method="post" action="/usuarios/alterar">{_csrf(csrf)}'
                f'<input type="hidden" name="id" value="{conta["id"]}">'
                f'{_select_perfil("perfil", conta["perfil"], id_html="perfil_" + str(conta["id"]))}'
                f'<input name="nova_senha" type="password" placeholder="Nova senha (opcional)" '
                f'autocomplete="new-password" maxlength="128">'
                f'<button class="botao contorno peq" type="submit">{_icone("salvar")}Salvar</button></form>'
                f"{excluir}</div>"
            )
        lista = "".join(contas) or '<p class="nota">Nenhuma conta cadastrada.</p>'
        self._html(
            "Usuários",
            f"""
            {_sucesso(ok)}
            {_aviso(erro)}
            <section class="cartao">
              {_cabecalho("mais", "Nova conta", "Somente administradores criam, alteram e excluem contas.")}
              <form method="post" action="/usuarios/criar" class="coluna">
                {_csrf(csrf)}
                <div class="campos">
                  {_campo("novo_usuario", "Usuário", novo_usuario, dica="ex.: maria", extra='maxlength="32" required')}
                  {_campo("nova_senha", "Senha (mínimo 8 caracteres)", "", tipo="password", extra='maxlength="128" required autocomplete="new-password"')}
                  {_select_perfil("novo_perfil", novo_perfil, "Perfil")}
                </div>
                <p class="nota"><b>Administrador:</b> tudo, inclusive criar/excluir contas.
                <b>Gerente:</b> consulta, exporta e edita EAN/nome, sem gerenciar contas.
                <b>Visualização:</b> apenas consulta e exporta. Todos enxergam o log.</p>
                <div class="acoes sem-topo">
                  <button class="botao primario" type="submit">{_icone("mais")}Criar conta</button>
                </div>
              </form>
            </section>
            <section class="cartao">
              {_cabecalho("usuario", "Contas", "Altere o perfil, redefina a senha ou exclua.",
                          extra_titulo=f'<span class="contador">{len(contas)}</span>')}
              <hr class="sem-base">
              {lista}
            </section>
            """,
            sessao,
            menu="usuarios",
        )

    def _usuario_criar(self, dados: dict[str, list[str]], sessao: tuple[Usuario, str]) -> None:
        usuario, _ = sessao
        nome = self._valor(dados, "novo_usuario")
        senha = self._valor(dados, "nova_senha", cortar=False)
        perfil = self._valor(dados, "novo_perfil")
        try:
            acesso.criar_usuario(usuario, nome, senha, perfil, self._ip())
        except PermissionError:
            self._pagina_negada(sessao)
            return
        except ValueError as exc:
            self._pagina_usuarios(sessao, erro=str(exc), novo_usuario=nome,
                                  novo_perfil=perfil if perfil in PERFIS else acesso.PERFIL_VISUALIZACAO)
            return
        self._redirecionar("/usuarios?msg=criado")

    def _usuario_alterar(self, dados: dict[str, list[str]], sessao: tuple[Usuario, str]) -> None:
        usuario, _ = sessao
        alvo = self._inteiro(self._valor(dados, "id"))
        perfil = self._valor(dados, "perfil")
        nova_senha = self._valor(dados, "nova_senha", cortar=False)
        try:
            senha_trocada = acesso.alterar_usuario(usuario, alvo, perfil, nova_senha, self._ip())
        except PermissionError:
            self._pagina_negada(sessao)
            return
        except ValueError as exc:
            self._pagina_usuarios(sessao, erro=str(exc))
            return
        if senha_trocada:
            acesso.encerrar_sessoes_usuario(alvo, manter=self._token_cookie())
        self._redirecionar("/usuarios?msg=alterado")

    def _usuario_excluir(self, dados: dict[str, list[str]], sessao: tuple[Usuario, str]) -> None:
        usuario, _ = sessao
        alvo = self._inteiro(self._valor(dados, "id"))
        try:
            acesso.excluir_usuario(usuario, alvo, self._ip())
        except PermissionError:
            self._pagina_negada(sessao)
            return
        except ValueError as exc:
            self._pagina_usuarios(sessao, erro=str(exc))
            return
        self._redirecionar("/usuarios?msg=excluido")
