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
- ustaw `Format` na `string`
- kliknij w panel logu w przeglądarce
- wpisywane klawisze są wysyłane na UART
- `Pokaż czasy` włącza/wyłącza timestampy tylko w widoku WWW
- log można pobrać z przeglądarki z czasami albo bez

# Structure
- `src/` - kod aplikacji
- `logs/` - logi runtime i PID
