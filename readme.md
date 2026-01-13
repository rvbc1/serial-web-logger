# First Time
sudo usermod -a -G dialout $USER

# Run
make start

# Stop
make stop

# Status
make status

# Logs
tail -f serial_web.out
