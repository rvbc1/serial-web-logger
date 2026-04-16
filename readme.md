# First Time
sudo usermod -a -G dialout $USER

# Run
make start

# Port override
echo 'PORT=7499' > .env
# albo jednorazowo:
PORT=7499 make start

# Stop
make stop

# Status
make status

# Logs
tail -f logs/serial_web.out

# UART terminal
- jedna karta = jedna niezależna sesja UART
- można dodawać i usuwać karty z poziomu WWW
- każda karta ma własny port, baud, format, DTR/RTS, podgląd i plik logu
- ustaw `Format` na `string`
- kliknij w panel logu w przeglądarce
- wpisywane klawisze są wysyłane na UART
- `Pokaż czasy` włącza/wyłącza timestampy tylko w widoku WWW
- log można pobrać z przeglądarki z czasami albo bez

# Structure
- `src/` - kod aplikacji
- `logs/` - logi runtime i PID
- `logs/session_store.json` - zapis konfiguracji kart/sesji
- `logs/sessions/` - osobne logi dla dodatkowych sesji
