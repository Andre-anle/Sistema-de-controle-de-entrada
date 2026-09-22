"""Empacota a aplicação com Nuitka para Windows 10 e 11 (64 bits)."""

from __future__ import annotations

import argparse
import os
import shutil
import site
import subprocess
import sys
from pathlib import Path

VERSAO = "1.0.0"
NOME_EXE = "EtiquetasHortifruti.exe"
PASTA_DIST_FINAL = "EtiquetasHortifruti"


def _tem_flet_exe(pasta: Path) -> bool:
    return (pasta / "flet" / "flet.exe").is_file() or (pasta / "flet.exe").is_file()


def _normalizar_cliente(origem: Path, destino: Path) -> Path:
    if destino.exists():
        shutil.rmtree(destino)
    destino.mkdir(parents=True, exist_ok=True)

    if (origem / "flet" / "flet.exe").is_file():
        shutil.copytree(origem / "flet", destino / "flet")
    elif (origem / "flet.exe").is_file():
        shutil.copytree(origem, destino / "flet", dirs_exist_ok=True)
    else:
        raise FileNotFoundError(f"flet.exe não encontrado em {origem}")
    return destino


def pasta_flet_client() -> Path:
    import flet_desktop

    raiz = Path(__file__).resolve().parent
    local = raiz / ".flet_runtime"
    if _tem_flet_exe(local):
        return local

    candidatos = [
        Path(flet_desktop.__file__).resolve().parent / "app",
        Path.home() / ".flet" / "client" / f"flet-desktop-full-{flet_desktop.version.version}",
        Path.home() / ".flet" / "client" / f"flet-desktop-light-{flet_desktop.version.version}",
    ]
    for pasta in candidatos:
        if _tem_flet_exe(pasta):
            print(f"Usando cliente Flet em {pasta}")
            return _normalizar_cliente(pasta, local)

    print("Cliente Flet não está na venv. Baixando o runtime desktop (uma vez)...")
    cache = Path(flet_desktop.ensure_client_cached())
    if _tem_flet_exe(cache):
        return _normalizar_cliente(cache, local)

    raise FileNotFoundError(
        f"Não foi possível obter o cliente Flet. Conferir {cache} após o download."
    )


def _vswhere() -> Path | None:
    caminhos = [
        Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
        / "Microsoft Visual Studio"
        / "Installer"
        / "vswhere.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "Microsoft Visual Studio"
        / "Installer"
        / "vswhere.exe",
        Path(r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe"),
    ]
    for caminho in caminhos:
        if caminho.is_file():
            return caminho
    return None


def localizar_vcvars64() -> Path | None:
    vswhere = _vswhere()
    if vswhere is not None:
        try:
            saida = subprocess.check_output(
                [
                    str(vswhere),
                    "-latest",
                    "-products",
                    "*",
                    "-find",
                    r"VC\Auxiliary\Build\vcvars64.bat",
                ],
                text=True,
                stderr=subprocess.DEVNULL,
            )
        except (OSError, subprocess.CalledProcessError):
            saida = ""
        for linha in saida.splitlines():
            caminho = Path(linha.strip())
            if caminho.is_file():
                return caminho
    for raiz in (
        Path(r"C:\Program Files (x86)\Microsoft Visual Studio"),
        Path(r"C:\Program Files\Microsoft Visual Studio"),
    ):
        if not raiz.is_dir():
            continue
        encontrados = list(raiz.glob(r"**\VC\Auxiliary\Build\vcvars64.bat"))
        if encontrados:
            return encontrados[0]
    return None


def windows_sdk_instalado() -> bool:
    if os.environ.get("WindowsSDKVersion") and os.environ.get("WindowsSdkDir"):
        sdk_dir = Path(os.environ["WindowsSdkDir"])
        if (sdk_dir / "Include").is_dir():
            return True
    pastas = [
        Path(r"C:\Program Files (x86)\Windows Kits\10\Include"),
        Path(r"C:\Program Files\Windows Kits\10\Include"),
    ]
    for chave in ("ProgramFiles(x86)", "ProgramFiles", "ProgramW6432"):
        raiz = os.environ.get(chave)
        if raiz:
            pastas.append(Path(raiz) / "Windows Kits" / "10" / "Include")
    vistos: set[Path] = set()
    for include in pastas:
        if include in vistos or not include.is_dir():
            continue
        vistos.add(include)
        for versao in include.iterdir():
            if (versao / "um" / "Windows.h").is_file():
                return True
    return False


def msvc_completo() -> bool:
    return localizar_vcvars64() is not None and windows_sdk_instalado()


def limpar_cache_scons_msvc() -> None:
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return
    raiz = Path(local) / "Nuitka"
    if not raiz.is_dir():
        return
    for pasta in raiz.rglob("scons-msvc-config"):
        if pasta.is_dir():
            shutil.rmtree(pasta, ignore_errors=True)
            print(f"Cache antigo do Scons/MSVC apagado: {pasta}")


def carregar_ambiente_msvc() -> bool:
    vcvars = localizar_vcvars64()
    if vcvars is None:
        return False
    script = f'call "{vcvars}" >nul && set'
    try:
        saida = subprocess.check_output(
            script,
            shell=True,
            text=True,
            stderr=subprocess.STDOUT,
            encoding="mbcs",
            errors="replace",
        )
    except subprocess.CalledProcessError as exc:
        print("Não foi possível carregar o ambiente do Visual Studio.")
        print((exc.output or "")[-1500:])
        return False
    for linha in saida.splitlines():
        if "=" not in linha:
            continue
        chave, _, valor = linha.partition("=")
        chave = chave.strip()
        if chave:
            os.environ[chave] = valor
    sdk = (os.environ.get("WindowsSDKVersion") or "").strip().strip("\\")
    cl = shutil.which("cl")
    if not sdk or cl is None:
        print(
            "O vcvars64.bat rodou, mas o Windows SDK ou o cl.exe ainda não apareceu no ambiente."
        )
        return False
    os.environ["CC"] = cl
    print(f"Ambiente MSVC carregado. SDK {sdk}. Compilador: {cl}")
    return True


def flags_compilador() -> list[str]:
    """Carrega o MSVC de verdade (vcvars + SDK). Se falhar, usa MinGW64."""
    if msvc_completo() and carregar_ambiente_msvc():
        limpar_cache_scons_msvc()
        print("Compilador: Microsoft Visual C++ com Windows SDK.")
        return ["--msvc=latest"]
    if localizar_vcvars64() is not None and not windows_sdk_instalado():
        print(
            "O Visual Studio Build Tools está instalado, mas o Windows 10/11 SDK não foi encontrado no disco."
        )
    print("Compilador: MinGW64 (baixado pelo Nuitka, compatível com Windows 10 e 11).")
    if sys.version_info >= (3, 13):
        print(
            "Python 3.13+ pede MSVC. Como o ambiente do Visual Studio não ficou utilizável, "
            "o empacotamento tenta MinGW64."
        )
        return ["--mingw64", "--experimental=force-mingw64"]
    return ["--mingw64"]


def validar_ambiente() -> None:
    if os.name != "nt":
        raise SystemExit("Compile no Windows 10 ou 11 (64 bits).")
    if sys.maxsize <= 2**32:
        raise SystemExit("Use Python 64 bits. Este Python é 32 bits e não serve para o empacotamento.")
    if sys.version_info < (3, 10):
        raise SystemExit("Use Python 3.10 ou superior.")
    if sys.version_info >= (3, 15):
        raise SystemExit(f"Python {sys.version.split()[0]} ainda não é suportado por este empacotamento.")
    print(f"Python {sys.version.split()[0]} 64 bits em {sys.executable}")
    print("Alvo: Windows 10 e Windows 11 (x64).")


def pastas_pywin32() -> list[Path]:
    pastas: list[Path] = []
    for base in [Path(sys.prefix), *map(Path, site.getsitepackages())]:
        for nome in ("pywin32_system32", "win32"):
            pasta = base / nome
            if pasta.is_dir() and pasta not in pastas:
                pastas.append(pasta)
    return pastas


def copiar_dlls_pywin32(pasta_dist: Path) -> None:
    copiados = 0
    for pasta in pastas_pywin32():
        for dll in pasta.glob("*.dll"):
            destino = pasta_dist / dll.name
            if not destino.exists():
                shutil.copy2(dll, destino)
                copiados += 1
    if copiados:
        print(f"Copiadas {copiados} DLL(s) do pywin32 para a pasta do programa.")


def escrever_leia_me(pasta_dist: Path) -> None:
    texto = (
        "Etiquetas Hortifruti — Windows 10 e 11 (64 bits)\n"
        "\n"
        "Copie a pasta inteira. Não mova só o .exe.\n"
        "Dentro dela devem existir EtiquetasHortifruti.exe e a pasta flet_client "
        "(com flet.exe).\n"
        "Requisitos: Windows 10 (versão 1909 ou superior) ou Windows 11, 64 bits.\n"
        "O Visual C++ Runtime já vai junto nesta pasta.\n"
        "Na primeira execução, o Windows pode pedir permissão de firewall "
        "se o acesso remoto estiver ligado.\n"
    )
    (pasta_dist / "LEIA-ME.txt").write_text(texto, encoding="utf-8")


def localizar_pasta_dist(raiz: Path) -> Path:
    dist = raiz / "dist"
    candidatos = [
        dist / PASTA_DIST_FINAL,
        dist / "main.dist",
        dist / "EtiquetasHortifruti.dist",
    ]
    for pasta in candidatos:
        if (pasta / NOME_EXE).is_file():
            return pasta
    if dist.is_dir():
        for pasta in dist.iterdir():
            if pasta.is_dir() and (pasta / NOME_EXE).is_file():
                return pasta
    raise FileNotFoundError(f"Não encontrei {NOME_EXE} em {dist}")


def copiar_cliente_flet(destino: Path) -> None:
    """O Nuitka ignora .exe/.dll em --include-data-dir; copia o cliente Flet completo."""
    origem = pasta_flet_client()
    if not (origem / "flet" / "flet.exe").is_file() and not (origem / "flet.exe").is_file():
        raise FileNotFoundError(f"flet.exe não encontrado em {origem}")
    alvo = destino / "flet_client"
    shutil.copytree(origem, alvo, dirs_exist_ok=True)
    flet_exe = alvo / "flet" / "flet.exe"
    if not flet_exe.is_file():
        flet_exe = alvo / "flet.exe"
    print(f"Cliente Flet copiado: {flet_exe}")
    msvcp = (alvo / "flet" / "msvcp140.dll") if (alvo / "flet" / "msvcp140.dll").is_file() else alvo / "msvcp140.dll"
    if msvcp.is_file():
        shutil.copy2(msvcp, destino / "msvcp140.dll")


def finalizar_dist(raiz: Path) -> Path:
    origem = localizar_pasta_dist(raiz)
    destino = raiz / "dist" / PASTA_DIST_FINAL
    if origem.resolve() != destino.resolve():
        if destino.exists():
            shutil.rmtree(destino)
        shutil.move(str(origem), str(destino))
    copiar_dlls_pywin32(destino)
    copiar_cliente_flet(destino)
    icone = raiz / "logo.ico"
    if icone.is_file():
        shutil.copy2(icone, destino / "logo.ico")
    escrever_leia_me(destino)
    return destino


def garantir_icone_windows(caminho: Path) -> Path:
    """Garante um .ico real. Arquivo JPEG/PNG só com extensão .ico quebra o Nuitka."""
    if not caminho.is_file():
        return caminho
    cabecalho = caminho.read_bytes()[:6]
    if cabecalho[:4] == b"\x00\x00\x01\x00":
        return caminho
    from PIL import Image

    imagem = Image.open(caminho).convert("RGBA")
    lado = max(imagem.size)
    quadro = Image.new("RGBA", (lado, lado), (0, 0, 0, 0))
    quadro.paste(imagem, ((lado - imagem.width) // 2, (lado - imagem.height) // 2), imagem)
    quadro.save(
        caminho,
        format="ICO",
        sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)],
    )
    print(f"Ícone convertido para formato Windows ICO: {caminho}")
    return caminho


def montar_comando(onefile: bool, console: bool, jobs: int) -> list[str]:
    raiz = Path(__file__).resolve().parent
    logo = raiz / "logo.png"
    icone = garantir_icone_windows(raiz / "logo.ico")
    cliente = pasta_flet_client()
    cmd = [
        sys.executable,
        "-m",
        "nuitka",
        "--standalone",
        *flags_compilador(),
        "--assume-yes-for-downloads",
        "--show-progress",
        f"--jobs={jobs}",
        "--lto=no",
        "--remove-output",
        "--include-windows-runtime-dlls=yes",
        f"--output-dir={raiz / 'dist'}",
        f"--output-filename={NOME_EXE}",
        "--windows-company-name=Hortifruti Natural da Terra",
        "--windows-product-name=Etiquetas Hortifruti",
        "--windows-file-description=Controle de entrada de mercadorias e visitantes",
        f"--windows-file-version={VERSAO}",
        f"--windows-product-version={VERSAO}",
        "--copyright=Hortifruti Natural da Terra",
        "--include-package=flet",
        "--include-package=flet_desktop",
        "--include-package-data=flet",
        "--include-package-data=flet_desktop",
        "--include-package=PIL",
        "--include-package=qrcode",
        "--include-package=barcode",
        "--include-package=openpyxl",
        "--include-package=et_xmlfile",
        "--include-module=remote_access",
        "--include-module=export_excel",
        "--include-module=db",
        "--include-module=settings",
        "--include-module=print_label",
        "--include-module=paths",
        "--include-module=win32print",
        "--include-module=win32ui",
        "--include-module=win32api",
        "--include-module=win32con",
        "--include-module=pywintypes",
        "--include-module=pythoncom",
        "--include-module=PIL.ImageWin",
        "--follow-imports",
        "--nofollow-import-to=pytest",
        "--nofollow-import-to=setuptools",
        "--nofollow-import-to=tkinter",
        "--nofollow-import-to=unittest",
        "--nofollow-import-to=nuitka",
        "--nofollow-import-to=pip",
        f"--include-data-dir={cliente}=flet_client",
    ]
    if logo.is_file():
        cmd.append(f"--include-data-files={logo}=logo.png")
    if icone.is_file():
        cmd.append(f"--windows-icon-from-ico={icone}")
        cmd.append(f"--include-data-files={icone}=logo.ico")
    try:
        import pywin32_system32  # noqa: F401

        cmd.append("--include-package=pywin32_system32")
    except ImportError:
        pass
    cmd.append("--windows-console-mode=" + ("force" if console else "disable"))
    if onefile:
        cmd.append("--onefile")
        cmd.append("--onefile-tempdir-spec={CACHE_DIR}/EtiquetasHortifruti")
    cmd.append(str(raiz / "main.py"))
    return cmd


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Empacota a aplicação com Nuitka para Windows 10 e 11 64 bits."
    )
    parser.add_argument(
        "--onefile",
        action="store_true",
        help="Gera um único .exe (abre mais lento; prefira a pasta standalone).",
    )
    parser.add_argument(
        "--console",
        action="store_true",
        help="Mantém o console para ver erros no primeiro empacotamento.",
    )
    parser.add_argument(
        "--jobs",
        type=int,
        default=max(1, (os.cpu_count() or 2) - 1),
        help="Compilações C em paralelo. Padrão: núcleos da CPU menos 1.",
    )
    args = parser.parse_args()
    validar_ambiente()
    comando = montar_comando(onefile=args.onefile, console=args.console, jobs=max(1, args.jobs))
    print(" ".join(comando))
    codigo = subprocess.call(comando)
    if codigo != 0:
        return codigo
    if args.onefile:
        print("Empacotamento onefile concluído em dist\\")
        return 0
    pasta = finalizar_dist(Path(__file__).resolve().parent)
    exe = pasta / NOME_EXE
    print(f"Pronto para Windows 10 e 11 (64 bits): {exe}")
    print("Distribua a pasta inteira dist\\EtiquetasHortifruti")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
