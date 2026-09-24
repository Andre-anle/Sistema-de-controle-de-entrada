@echo off
setlocal
cd /d "%~dp0"

echo Compilando Etiquetas Hortifruti para Windows 10 e 11 (64 bits)...
echo.

if exist "%~dp0venv\Scripts\python.exe" (
    "%~dp0venv\Scripts\python.exe" "%~dp0build_nuitka.py" %*
) else (
    python "%~dp0build_nuitka.py" %*
)

if errorlevel 1 (
    echo.
    echo A compilacao falhou.
    pause
    exit /b 1
)

echo.
echo Pasta pronta: dist\EtiquetasHortifruti

set "ISCC="
for %%P in (
    "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
    "%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
    "%ProgramFiles%\Inno Setup 6\ISCC.exe"
) do if not defined ISCC if exist %%P set "ISCC=%%~P"

if not defined ISCC (
    echo.
    echo Inno Setup 6 nao encontrado. Instale para gerar o instalador:
    echo   winget install JRSoftware.InnoSetup
    pause
    exit /b 0
)

echo.
echo Gerando instalador...
"%ISCC%" /Q "%~dp0instalador.iss"
if errorlevel 1 (
    echo.
    echo Falha ao gerar o instalador.
    pause
    exit /b 1
)
echo Instalador pronto em dist\instalador
pause
endlocal
