PID_FILE := serial_logger.pid
LOG_FILE := serial_web.out
PROC_MATCH := serial_logger_web.py
PORT := 7488

all: start

start:
	@chmod +x run.sh
	@if [ -f "$(PID_FILE)" ]; then \
		PID=$$(cat "$(PID_FILE)"); \
		if ps -p $$PID -o args= 2>/dev/null | grep -q "$(PROC_MATCH)"; then \
			echo "Already running (pid=$$PID)"; \
			exit 0; \
		else \
			rm -f "$(PID_FILE)"; \
		fi; \
	fi; \
	if python3 -c "import socket,sys; s=socket.socket(); s.settimeout(0.2); sys.exit(0 if s.connect_ex(('127.0.0.1', int(sys.argv[1])))==0 else 1)" "$(PORT)" > /dev/null 2>&1; then \
		echo "Already running (port $(PORT) open)"; \
	else \
		PORT="$(PORT)" nohup ./run.sh > "$(LOG_FILE)" 2>&1 & echo $$! > "$(PID_FILE)"; \
		echo "Started (pid=$$(cat "$(PID_FILE)"))"; \
	fi

stop:
	@if [ -f "$(PID_FILE)" ]; then \
		PID=$$(cat "$(PID_FILE)"); \
		if ps -p $$PID -o args= 2>/dev/null | grep -q "$(PROC_MATCH)"; then \
			CHILD=$$(ps -o pid= --ppid $$PID | tr -d ' '); \
			if [ -n "$$CHILD" ]; then \
				kill $$CHILD; \
			fi; \
			kill $$PID; \
			echo "Stopped (pid=$$PID)"; \
		else \
			echo "Not running (stale pid=$$PID)"; \
		fi; \
		rm -f "$(PID_FILE)"; \
	else \
		echo "Not running (no pid file)"; \
	fi

status:
	@if [ -f "$(PID_FILE)" ]; then \
		PID=$$(cat "$(PID_FILE)"); \
		if ps -p $$PID -o args= 2>/dev/null | grep -q "$(PROC_MATCH)"; then \
			echo "Running (pid=$$PID)"; \
		else \
			echo "Not running (stale pid=$$PID)"; \
		fi; \
	elif python3 -c "import socket,sys; s=socket.socket(); s.settimeout(0.2); sys.exit(0 if s.connect_ex(('127.0.0.1', int(sys.argv[1])))==0 else 1)" "$(PORT)" > /dev/null 2>&1; then \
		echo "Running (port $(PORT) open)"; \
	else \
		echo "Not running"; \
	fi
