@echo off
setlocal
cd /d "%~dp0"
set "PYTHON="

if not exist ".env" (
    echo Arquivo .env nao encontrado.
    echo Copie .env.example para .env e configure RIOT_API_KEY.
    pause
    exit /b 1
)

if exist ".venv\Scripts\python.exe" (
    set "PYTHON=.venv\Scripts\python.exe"
) else (
    py -3 -c "import sys" >nul 2>&1
    if not errorlevel 1 set "PYTHON=py -3"
    if not defined PYTHON (
        python -c "import sys" >nul 2>&1
        if not errorlevel 1 set "PYTHON=python"
    )
)

if not defined PYTHON (
    echo Python 3 nao encontrado. Instale o Python e execute novamente.
    pause
    exit /b 1
)

%PYTHON% -c "import requests, dotenv" >nul 2>&1
if errorlevel 1 (
    echo Instalando dependencias...
    %PYTHON% -m pip install -r requirements.txt
    if errorlevel 1 (
        echo Falha ao instalar as dependencias.
        pause
        exit /b 1
    )
)

echo Iniciando Rift Coach em http://127.0.0.1:8000
start "" /min powershell -NoProfile -Command "Start-Sleep -Seconds 2; Start-Process 'http://127.0.0.1:8000'"
%PYTHON% web.py

echo.
echo O servidor foi encerrado.
pause
