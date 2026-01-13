#!/bin/bash

# 1. Utworz wirtualne srodowisko jesli nie istnieje
if [ ! -d "venv" ]; then
    echo "Tworze srodowisko wirtualne..."
    python3 -m venv venv
fi

# 2. Aktywuj srodowisko
source venv/bin/activate

# 3. Zainstaluj wymagane paczki jesli trzeba
pip install --upgrade pip >/dev/null
pip install -r requirements.txt >/dev/null

# 4. Uruchom skrypt pythonowy (exec, aby PID byl procesem serwera)
PORT="${PORT:-7489}"
exec python3 serial_logger_web.py --host 0.0.0.0 --port "$PORT" --logfile serial.log
