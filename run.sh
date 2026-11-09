#!/usr/bin/env bash
set -euo pipefail

cd -- "$(dirname -- "${BASH_SOURCE[0]}")"

if [[ ! -f .env ]]; then
    echo "Arquivo .env não encontrado. Copie .env.example para .env e configure RIOT_API_KEY."
    exit 1
fi

if [[ -x .venv/bin/python ]]; then
    python_bin=.venv/bin/python
elif command -v python3 >/dev/null 2>&1; then
    python_bin=$(command -v python3)
else
    echo "Python 3 não encontrado. Instale o Python 3 e execute novamente."
    exit 1
fi

if ! "$python_bin" -c 'import requests, dotenv' >/dev/null 2>&1; then
    if [[ "$python_bin" != .venv/bin/python ]]; then
        python3 -m venv .venv
        python_bin=.venv/bin/python
    fi
    echo "Instalando dependências..."
    "$python_bin" -m pip install -r requirements.txt
fi

url="http://127.0.0.1:${PORT:-8000}"
echo "Iniciando Rift Coach em $url"
if command -v xdg-open >/dev/null 2>&1; then
    (sleep 2; xdg-open "$url" >/dev/null 2>&1 || true) &
fi
exec "$python_bin" web.py
