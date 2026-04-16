#!/bin/bash

set -euo pipefail

BASE_DIR="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
VENV_DIR="$BASE_DIR/venv"
LOG_DIR="$BASE_DIR/logs"
APP_PATH="$BASE_DIR/src/serial_logger_web.py"

mkdir -p "$LOG_DIR"
cd "$BASE_DIR"

# 1. Utworz wirtualne srodowisko jesli nie istnieje
if [ ! -d "$VENV_DIR" ]; then
    echo "Tworze srodowisko wirtualne..."
    python3 -m venv "$VENV_DIR"
fi

# 2. Zainstaluj wymagane paczki jesli trzeba
"$VENV_DIR/bin/python3" -m pip install --upgrade pip >/dev/null
"$VENV_DIR/bin/python3" -m pip install -r "$BASE_DIR/requirements.txt" >/dev/null

# 3. Uruchom skrypt pythonowy (exec, aby PID byl procesem serwera)
PORT="${PORT:-7488}"
exec "$VENV_DIR/bin/python3" "$APP_PATH" --host 0.0.0.0 --port "$PORT" --logfile "$LOG_DIR/serial.log"
