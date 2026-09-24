import json
from dataclasses import asdict, dataclass

from paths import CONFIG_PATH


@dataclass
class AppSettings:
    printer: str = ""
    font_name: str = "arial.ttf"
    font_size: int = 20
    remote_enabled: bool = False
    remote_port: int = 8765
    remote_token: str = ""


def carregar_config() -> AppSettings:
    if not CONFIG_PATH.exists():
        return AppSettings()
    try:
        dados = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        porta = int(dados.get("remote_port", 8765))
        if porta < 1024 or porta > 65535:
            porta = 8765
        return AppSettings(
            printer=str(dados.get("printer", "")),
            font_name=str(dados.get("font_name", "arial.ttf")),
            font_size=int(dados.get("font_size", 20)),
            remote_enabled=bool(dados.get("remote_enabled", False)),
            remote_port=porta,
            remote_token=str(dados.get("remote_token", "")).strip(),
        )
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
    CONFIG_PATH.write_text(
        json.dumps(asdict(config), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
