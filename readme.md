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

# Structure
- `src/` - kod aplikacji
- `logs/` - logi runtime i PID
