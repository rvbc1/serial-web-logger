#!/usr/bin/env python3
import argparse
import json
import threading
import time
from collections import deque
from datetime import datetime

from flask import Flask, request, jsonify, Response
import serial
from serial.tools import list_ports

app = Flask(__name__)

# --- Stan globalny ---
state_lock = threading.Lock()
reader_thread = None
stop_event = threading.Event()
ser = None

current_port = None
current_baud = None
log_path = None
current_format = "string"

# Przechowujemy ostatnie N wpisów do podglądu w WWW (plik rośnie normalnie na dysku)
RING_MAX = 5000
ring = deque(maxlen=RING_MAX)  # elementy: (id, line)
next_id = 1


def now_ts():
    return datetime.now().isoformat(timespec="seconds")


def list_serial_ports():
    # Zwraca listę stringów /dev/tty...
    ports = []
    for p in list_ports.comports():
        ports.append(p.device)
    return ports


def append_line(line: str):
    global next_id
    with state_lock:
        _id = next_id
        next_id += 1
        ring.append((_id, line))


def write_to_file(line: str):
    # dopis do pliku (UTF-8)
    with open(log_path, "a", encoding="utf-8", errors="replace") as f:
        f.write(line + "\n")
        f.flush()


def read_loop():
    global ser
    append_line(f"{now_ts()} [INFO] Start czytania z {current_port} @ {current_baud}")
    write_to_file(f"{now_ts()} [INFO] Start czytania z {current_port} @ {current_baud}")

    try:
        while not stop_event.is_set():
            try:
                data = ser.readline()  # do \n lub timeout
                if data:
                    raw = data.rstrip(b"\r\n")
                    with state_lock:
                        fmt = current_format
                    if fmt == "hex":
                        text = " ".join(f"{b:02x}" for b in raw)
                    else:
                        text = raw.decode("utf-8", errors="replace")
                    line = f"{now_ts()} {text}"
                    append_line(line)
                    write_to_file(line)
            except serial.SerialException as e:
                line = f"{now_ts()} [ERROR] SerialException: {e}"
                append_line(line)
                write_to_file(line)
                break
    finally:
        try:
            if ser is not None:
                ser.close()
        except Exception:
            pass
        ser = None
        append_line(f"{now_ts()} [INFO] Stop czytania")
        write_to_file(f"{now_ts()} [INFO] Stop czytania")


def start_listening(port: str, baud: int):
    global reader_thread, ser, current_port, current_baud

    with state_lock:
        running = reader_thread is not None and reader_thread.is_alive()
    if running:
        stop_listening()

    stop_event.clear()
    current_port = port
    current_baud = int(baud)

    try:
        ser = serial.Serial(
            port=current_port,
            baudrate=current_baud,
            timeout=0.5,   # żeby wątek mógł reagować na stop_event
        )
    except Exception as e:
        return False, f"Nie mogę otworzyć {port}: {e}"

    reader_thread = threading.Thread(target=read_loop, daemon=True)
    reader_thread.start()
    return True, "OK"


def stop_listening():
    global reader_thread, ser
    stop_event.set()

    t = None
    with state_lock:
        t = reader_thread

    if t and t.is_alive():
        t.join(timeout=2.0)

    # Gdyby join nie domknął, spróbuj zamknąć port (czasem to odblokuje readline)
    try:
        if ser is not None:
            ser.close()
    except Exception:
        pass

    with state_lock:
        reader_thread = None
    return True


def is_running():
    with state_lock:
        return reader_thread is not None and reader_thread.is_alive()


# --- WWW ---
INDEX_HTML = r"""
<!doctype html>
<html lang="pl">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Serial Logger</title>
  <style>
    body { font-family: system-ui, sans-serif; margin: 16px; }
    .row { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
    select, input, button { padding: 8px; font-size: 14px; }
    #log { width: 100%; height: 70vh; white-space: pre; overflow: auto; padding: 12px; border: 1px solid #ccc; border-radius: 8px; background: #fafafa; }
    .badge { padding: 4px 8px; border-radius: 999px; font-size: 12px; }
    .on { background: #d1fae5; }
    .off { background: #fee2e2; }
  </style>
</head>
<body>
  <h2>Serial Logger</h2>

  <div class="row">
    <span>Status: <span id="statusBadge" class="badge off">STOP</span></span>
    <button id="refreshPorts">Odśwież porty</button>

    <label>Port:
      <select id="portSelect"></select>
    </label>

    <label>Baud:
      <input id="baudInput" type="number" value="115200" min="1" step="1" />
    </label>

    <label>Format:
      <select id="formatSelect">
        <option value="string">string</option>
        <option value="hex">hex</option>
      </select>
    </label>

    <button id="startBtn">Start</button>
    <button id="stopBtn">Stop</button>
    <button id="clearBtn">Wyczyść podgląd</button>
  </div>

  <p style="margin-top: 8px; color:#666;">
    Podgląd pokazuje ostatnie wpisy (bufor w pamięci). Pełny log jest w pliku na serwerze.
  </p>

  <div id="log"></div>

<script>
let lastId = 0;
let polling = null;

function setStatus(running) {
  const b = document.getElementById("statusBadge");
  if (running) {
    b.textContent = "RUN";
    b.classList.remove("off"); b.classList.add("on");
  } else {
    b.textContent = "STOP";
    b.classList.remove("on"); b.classList.add("off");
  }
}

async function fetchPorts() {
  const res = await fetch("/api/ports");
  const data = await res.json();
  const sel = document.getElementById("portSelect");
  sel.innerHTML = "";
  data.ports.forEach(p => {
    const opt = document.createElement("option");
    opt.value = p; opt.textContent = p;
    sel.appendChild(opt);
  });
  if (data.ports.length === 0) {
    const opt = document.createElement("option");
    opt.value = ""; opt.textContent = "(brak portów)";
    sel.appendChild(opt);
  }
}

async function fetchStatus() {
  const res = await fetch("/api/status");
  const data = await res.json();
  setStatus(data.running);
  if (data.port) document.getElementById("portSelect").value = data.port;
  if (data.baud) document.getElementById("baudInput").value = data.baud;
  if (data.format) document.getElementById("formatSelect").value = data.format;
}

function appendToLog(lines) {
  const el = document.getElementById("log");
  const atBottom = (el.scrollTop + el.clientHeight) >= (el.scrollHeight - 8);

  for (const l of lines) {
    el.textContent += l + "\n";
  }
  if (atBottom) el.scrollTop = el.scrollHeight;
}

async function pollLog() {
  const res = await fetch(`/api/log?since=${lastId}`);
  const data = await res.json();
  if (data.lines && data.lines.length) {
    appendToLog(data.lines.map(x => x.line));
    lastId = data.to_id;
  }
}

function startPolling() {
  if (polling) return;
  polling = setInterval(pollLog, 700);
}

function stopPolling() {
  if (polling) { clearInterval(polling); polling = null; }
}

document.getElementById("refreshPorts").onclick = fetchPorts;

document.getElementById("startBtn").onclick = async () => {
  const port = document.getElementById("portSelect").value;
  const baud = parseInt(document.getElementById("baudInput").value, 10);
  const format = document.getElementById("formatSelect").value;
  const res = await fetch("/api/start", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({port, baud, format})
  });
  const data = await res.json();
  if (!data.ok) alert(data.error || "Błąd startu");
  await fetchStatus();
};

document.getElementById("stopBtn").onclick = async () => {
  await fetch("/api/stop", {method: "POST"});
  await fetchStatus();
};

document.getElementById("clearBtn").onclick = () => {
  document.getElementById("log").textContent = "";
  lastId = 0; // ponownie pobierze od początku bufora (jeśli chcesz inaczej, zmień)
};

(async function init(){
  await fetchPorts();
  await fetchStatus();
  startPolling();
  await pollLog();
})();
</script>

</body>
</html>
"""


@app.get("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.get("/api/ports")
def api_ports():
    return jsonify({"ports": list_serial_ports()})


@app.get("/api/status")
def api_status():
    return jsonify({
        "running": is_running(),
        "port": current_port,
        "baud": current_baud,
        "logfile": log_path,
        "format": current_format,
    })


@app.post("/api/start")
def api_start():
    global current_format
    data = request.get_json(force=True, silent=True) or {}
    port = (data.get("port") or "").strip()
    baud = data.get("baud", 115200)
    fmt = (data.get("format") or "string").strip().lower()

    if fmt not in ("string", "hex"):
        return jsonify({"ok": False, "error": "Nieprawidłowy format (string/hex)."}), 400

    if not port:
        return jsonify({"ok": False, "error": "Nie wybrano portu."}), 400

    with state_lock:
        current_format = fmt

    ok, msg = start_listening(port, int(baud))
    if not ok:
        return jsonify({"ok": False, "error": msg}), 500
    return jsonify({"ok": True})


@app.post("/api/stop")
def api_stop():
    stop_listening()
    return jsonify({"ok": True})


@app.get("/api/log")
def api_log():
    # Zwraca linie o id > since (inkrementalne odświeżanie)
    try:
        since = int(request.args.get("since", "0"))
    except ValueError:
        since = 0

    with state_lock:
        items = [(i, l) for (i, l) in ring if i > since]
        to_id = ring[-1][0] if ring else since

    return jsonify({
        "from_id": since,
        "to_id": to_id,
        "lines": [{"id": i, "line": l} for (i, l) in items],
    })


def main():
    global log_path
    parser = argparse.ArgumentParser(description="Serial logger + prosta strona WWW")
    parser.add_argument("--host", default="127.0.0.1", help="adres nasłuchu (np. 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="port HTTP")
    parser.add_argument("--logfile", default="serial.log", help="plik logu")
    args = parser.parse_args()

    log_path = args.logfile
    # dopisz informację startową
    append_line(f"{now_ts()} [INFO] Serwer WWW start: http://{args.host}:{args.port}  logfile={log_path}")
    write_to_file(f"{now_ts()} [INFO] Serwer WWW start: http://{args.host}:{args.port}  logfile={log_path}")

    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
