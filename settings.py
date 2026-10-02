import json
import os
from dataclasses import asdict, dataclass, field

from paths import CONFIG_PATH

FONTE_TAMANHO_MIN = 10
FONTE_TAMANHO_MAX = 50
# Campos que versões antigas gravavam e que são segredos: removidos ao carregar.
CAMPOS_LEGADOS_SENSIVEIS = ("remote_token",)


@dataclass
class AppSettings:
    printer: str = ""
    font_name: str = "arial.ttf"
    font_size: int = 20
    remote_enabled: bool = False
    remote_port: int = 8765
    # HTTPS ligado por padrão: senhas nunca trafegam em texto puro pela rede.
    remote_https: bool = True
    # Redes extras (CIDR) autorizadas a acessar, além das redes privadas da loja.
    remote_redes: list[str] = field(default_factory=list)
    backup_auto: bool = True
    backup_manter: int = 14
    update_auto: bool = True
    update_ignorada: str = ""
    # Última versão que o atualizador instalou neste computador (guardada antes de reiniciar).
    update_aplicada: str = ""


def _limitar(valor: int, minimo: int, maximo: int) -> int:
    return max(minimo, min(maximo, valor))


def carregar_config() -> AppSettings:
    if not CONFIG_PATH.exists():
        return AppSettings()
    try:
        dados = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if not isinstance(dados, dict):
            return AppSettings()
        porta = int(dados.get("remote_port", 8765))
        if porta < 1024 or porta > 65535:
            porta = 8765
        redes = dados.get("remote_redes", [])
        config = AppSettings(
            printer=str(dados.get("printer", ""))[:200],
            font_name=str(dados.get("font_name", "arial.ttf"))[:100],
            font_size=_limitar(int(dados.get("font_size", 20)), FONTE_TAMANHO_MIN, FONTE_TAMANHO_MAX),
            remote_enabled=bool(dados.get("remote_enabled", False)),
            remote_port=porta,
            remote_https=bool(dados.get("remote_https", True)),
            remote_redes=[str(r)[:64] for r in redes[:20]] if isinstance(redes, list) else [],
            backup_auto=bool(dados.get("backup_auto", True)),
            backup_manter=_limitar(int(dados.get("backup_manter", 14)), 1, 120),
            update_auto=bool(dados.get("update_auto", True)),
            update_ignorada=str(dados.get("update_ignorada", "")).strip()[:40],
            update_aplicada=str(dados.get("update_aplicada", "")).strip()[:40],
        )
        if any(chave in dados for chave in CAMPOS_LEGADOS_SENSIVEIS):
            try:
                salvar_config(config)  # regrava sem o segredo antigo
            except OSError:
                pass
        return config
    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        return AppSettings()


def impressora_disponivel(nome: str) -> bool:
    nome = (nome or "").strip().lower()
    if not nome:
        return False
    try:
        import win32print
    except ImportError:
        return False
    flags = win32print.PRINTER_ENUM_LOCAL | win32print.PRINTER_ENUM_CONNECTIONS
    try:
        instaladas = win32print.EnumPrinters(flags)
    except Exception:
        return False
    return any(impressora[2].lower() == nome for impressora in instaladas)


def salvar_config(config: AppSettings) -> None:
    """Grava por arquivo temporário + troca: queda de energia não deixa config pela metade."""
    temporario = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
    temporario.write_text(
        json.dumps(asdict(config), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    os.replace(temporario, CONFIG_PATH)
