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
pause
endlocal
