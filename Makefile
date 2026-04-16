-include .env

LOG_DIR := logs
PID_FILE := $(LOG_DIR)/serial_logger.pid
LEGACY_PID_FILE := serial_logger.pid
LOG_FILE := $(LOG_DIR)/serial_web.out
PROC_MATCH := serial_logger_web.py
PORT ?= 7488

export PORT

all: start

start:
	@mkdir -p "$(LOG_DIR)"
	@chmod +x run.sh
	@PID_FILE_TO_USE=""; \
	if [ -f "$(PID_FILE)" ]; then \
		PID_FILE_TO_USE="$(PID_FILE)"; \
	elif [ -f "$(LEGACY_PID_FILE)" ]; then \
		PID_FILE_TO_USE="$(LEGACY_PID_FILE)"; \
	fi; \
	if [ -n "$$PID_FILE_TO_USE" ]; then \
		PID=$$(cat "$$PID_FILE_TO_USE"); \
		CMD=$$(ps -p $$PID -o args= 2>/dev/null); \
		if printf '%s\n' "$$CMD" | grep -q "$(PROC_MATCH)"; then \
			PORT_FROM_CMD=$$(printf '%s\n' "$$CMD" | sed -n 's/.*--port \([0-9][0-9]*\).*/\1/p'); \
			if [ -z "$$PORT_FROM_CMD" ]; then PORT_FROM_CMD="$(PORT)"; fi; \
			echo "Already running (pid=$$PID, port=$$PORT_FROM_CMD)"; \
			exit 0; \
		else \
			rm -f "$$PID_FILE_TO_USE"; \
		fi; \
	fi; \
	if python3 -c "import socket,sys; s=socket.socket(); s.settimeout(0.2); sys.exit(0 if s.connect_ex(('127.0.0.1', int(sys.argv[1])))==0 else 1)" "$(PORT)" > /dev/null 2>&1; then \
		PID=$$(lsof -tiTCP:"$(PORT)" -sTCP:LISTEN 2>/dev/null | head -n 1); \
		if [ -n "$$PID" ]; then \
			echo "Already running (pid=$$PID, port=$(PORT))"; \
		else \
			echo "Already running (pid=unknown, port=$(PORT))"; \
		fi; \
	else \
		PORT="$(PORT)" nohup ./run.sh > "$(LOG_FILE)" 2>&1 & echo $$! > "$(PID_FILE)"; \
		echo "Started (pid=$$(cat "$(PID_FILE)"), port=$(PORT))"; \
	fi

stop:
	@PID_FILE_TO_USE=""; \
	if [ -f "$(PID_FILE)" ]; then \
		PID_FILE_TO_USE="$(PID_FILE)"; \
	elif [ -f "$(LEGACY_PID_FILE)" ]; then \
		PID_FILE_TO_USE="$(LEGACY_PID_FILE)"; \
	fi; \
	if [ -n "$$PID_FILE_TO_USE" ]; then \
		PID=$$(cat "$$PID_FILE_TO_USE"); \
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
		rm -f "$$PID_FILE_TO_USE"; \
	else \
		echo "Not running (no pid file)"; \
	fi

status:
	@PID_FILE_TO_USE=""; \
	if [ -f "$(PID_FILE)" ]; then \
		PID_FILE_TO_USE="$(PID_FILE)"; \
	elif [ -f "$(LEGACY_PID_FILE)" ]; then \
		PID_FILE_TO_USE="$(LEGACY_PID_FILE)"; \
	fi; \
	if [ -n "$$PID_FILE_TO_USE" ]; then \
		PID=$$(cat "$$PID_FILE_TO_USE"); \
		CMD=$$(ps -p $$PID -o args= 2>/dev/null); \
		if printf '%s\n' "$$CMD" | grep -q "$(PROC_MATCH)"; then \
			PORT_FROM_CMD=$$(printf '%s\n' "$$CMD" | sed -n 's/.*--port \([0-9][0-9]*\).*/\1/p'); \
			if [ -z "$$PORT_FROM_CMD" ]; then PORT_FROM_CMD="$(PORT)"; fi; \
			echo "Running (pid=$$PID, port=$$PORT_FROM_CMD)"; \
		else \
			echo "Not running (stale pid=$$PID)"; \
		fi; \
	elif python3 -c "import socket,sys; s=socket.socket(); s.settimeout(0.2); sys.exit(0 if s.connect_ex(('127.0.0.1', int(sys.argv[1])))==0 else 1)" "$(PORT)" > /dev/null 2>&1; then \
		PID=$$(lsof -tiTCP:"$(PORT)" -sTCP:LISTEN 2>/dev/null | head -n 1); \
		if [ -n "$$PID" ]; then \
			echo "Running (pid=$$PID, port=$(PORT))"; \
		else \
			echo "Running (pid=unknown, port=$(PORT))"; \
		fi; \
	else \
		echo "Not running"; \
	fi
