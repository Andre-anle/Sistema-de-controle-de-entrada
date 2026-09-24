import os
import sys
from pathlib import Path


def executando_empacotado() -> bool:
    return bool(getattr(sys, "frozen", False)) or "__compiled__" in globals()


def diretorio_executavel() -> Path:
    """Pasta do .exe: config e banco ficam aqui (persistem no modo onefile)."""
    if executando_empacotado():
        return Path(sys.argv[0]).resolve().parent
    return Path(__file__).resolve().parent


def diretorio_recursos() -> Path:
    """Pasta dos arquivos embutidos (logo e cliente Flet)."""
    if executando_empacotado():
        return Path(__file__).resolve().parent
    return Path(__file__).resolve().parent


APP_DIR = diretorio_executavel()
CONFIG_PATH = APP_DIR / "config.json"
LOGO_PATH = diretorio_recursos() / "logo.png"
if not LOGO_PATH.is_file():
    LOGO_PATH = APP_DIR / "logo.png"
ICONE_PATH = diretorio_recursos() / "logo.ico"
if not ICONE_PATH.is_file():
    ICONE_PATH = APP_DIR / "logo.ico"
FONTES_WINDOWS = Path(r"C:\Windows\Fonts")


def bancos_antigos() -> list[Path]:
    return [
        Path.home() / "Desktop" / "etiquetas.db",
        Path.home() / "Área de Trabalho" / "etiquetas.db",
    ]


def preparar_flet_view() -> None:
    """Aponta o Flet para o cliente empacotado junto do Nuitka."""
    if os.environ.get("FLET_VIEW_PATH"):
        pasta = Path(os.environ["FLET_VIEW_PATH"])
        if (pasta / "flet.exe").is_file():
            return

    candidatos = [
        diretorio_recursos() / "flet_client" / "flet",
        diretorio_recursos() / "flet_client",
        APP_DIR / "flet_client" / "flet",
        APP_DIR / "flet_client",
        diretorio_recursos() / "flet",
        APP_DIR / "flet",
    ]
    try:
        import flet_desktop

        candidatos.append(Path(flet_desktop.__file__).resolve().parent / "app" / "flet")
    except ImportError:
        pass

    for pasta in candidatos:
        if (pasta / "flet.exe").is_file():
            os.environ["FLET_VIEW_PATH"] = str(pasta)
            return


def preparar_runtime() -> None:
    os.chdir(APP_DIR)
    preparar_flet_view()
    if executando_empacotado() and not (
        Path(os.environ.get("FLET_VIEW_PATH", "")) / "flet.exe"
    ).is_file():
        raise FileNotFoundError(
            "O cliente visual do Flet (flet.exe) não está na pasta do programa. "
            "Copie a pasta inteira dist\\EtiquetasHortifruti, não apenas o .exe."
        )
