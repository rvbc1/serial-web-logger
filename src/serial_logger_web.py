#!/usr/bin/env python3
import argparse
import os
import threading
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
desired_dtr = True
desired_rts = True

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


def format_signal_state(value: bool) -> str:
    return "ON" if value else "OFF"


def parse_bool_field(value, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in ("1", "true", "on", "yes"):
            return True
        if normalized in ("0", "false", "off", "no"):
            return False
    raise ValueError(f"Pole {field_name} musi być typu bool.")


def apply_control_lines(serial_port, dtr_state: bool, rts_state: bool):
    serial_port.dtr = dtr_state
    serial_port.rts = rts_state


def update_control_lines(dtr_state: bool, rts_state: bool):
    global desired_dtr, desired_rts

    with state_lock:
        serial_port = ser

    if serial_port is not None:
        apply_control_lines(serial_port, dtr_state, rts_state)

    with state_lock:
        desired_dtr = dtr_state
        desired_rts = rts_state


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


def start_listening(port: str, baud: int, dtr_state: bool, rts_state: bool):
    global reader_thread, ser, current_port, current_baud

    with state_lock:
        running = reader_thread is not None and reader_thread.is_alive()
    if running:
        stop_listening()

    stop_event.clear()
    current_port = port
    current_baud = int(baud)

    try:
        new_ser = serial.Serial()
        new_ser.port = current_port
        new_ser.baudrate = current_baud
        new_ser.timeout = 0.5  # żeby wątek mógł reagować na stop_event
        new_ser.rtscts = False
        new_ser.dsrdtr = False
        new_ser.dtr = dtr_state
        new_ser.rts = rts_state
        new_ser.open()
        ser = new_ser
    except Exception as e:
        return False, f"Nie mogę otworzyć {port}: {e}"

    append_line(
        f"{now_ts()} [INFO] Port otwarty: {current_port} @ {current_baud} "
        f"DTR={format_signal_state(dtr_state)} RTS={format_signal_state(rts_state)}"
    )
    write_to_file(
        f"{now_ts()} [INFO] Port otwarty: {current_port} @ {current_baud} "
        f"DTR={format_signal_state(dtr_state)} RTS={format_signal_state(rts_state)}"
    )

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
    :root {
      color-scheme: light;
      --panel-border: #cbd5e1;
      --term-bg: #0f172a;
      --term-fg: #e5eefc;
      --term-muted: #94a3b8;
    }
    body { font-family: system-ui, sans-serif; margin: 16px; color: #0f172a; background: #f8fafc; }
    .row { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
    select, input, button { padding: 8px; font-size: 14px; }
    #log {
      width: 100%;
      height: 70vh;
      overflow: auto;
      padding: 14px;
      border: 1px solid var(--panel-border);
      border-radius: 10px;
      background: radial-gradient(circle at top, #172554 0%, var(--term-bg) 28%, #020617 100%);
      color: var(--term-fg);
      font: 13px/1.5 SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.05);
    }
    .log-line {
      white-space: pre-wrap;
      word-break: break-word;
    }
    .log-empty {
      color: var(--term-muted);
      font-style: italic;
    }
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

    <label><input id="dtrCheckbox" type="checkbox" checked /> DTR</label>
    <label><input id="rtsCheckbox" type="checkbox" checked /> RTS</label>

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
const ANSI_RE = /\x1b\[([0-9;]*)m/g;
const ANSI_COLORS = [
  "#1f2937", "#ef4444", "#22c55e", "#eab308",
  "#60a5fa", "#c084fc", "#2dd4bf", "#e5e7eb",
];
const ANSI_BRIGHT_COLORS = [
  "#94a3b8", "#f87171", "#4ade80", "#facc15",
  "#93c5fd", "#d8b4fe", "#5eead4", "#ffffff",
];

function defaultAnsiState() {
  return {
    bold: false,
    dim: false,
    italic: false,
    underline: false,
    fg: null,
    bg: null,
  };
}

function ansi256ToCss(code) {
  if (code < 0 || code > 255) return null;
  if (code < 16) {
    const palette = code < 8 ? ANSI_COLORS : ANSI_BRIGHT_COLORS;
    return palette[code % 8];
  }
  if (code <= 231) {
    const index = code - 16;
    const levels = [0, 95, 135, 175, 215, 255];
    const r = levels[Math.floor(index / 36) % 6];
    const g = levels[Math.floor(index / 6) % 6];
    const b = levels[index % 6];
    return `rgb(${r}, ${g}, ${b})`;
  }
  const gray = 8 + (code - 232) * 10;
  return `rgb(${gray}, ${gray}, ${gray})`;
}

function applyAnsiCodes(state, codes) {
  if (codes.length === 0) codes = [0];
  for (let i = 0; i < codes.length; i++) {
    const code = codes[i];
    if (Number.isNaN(code)) continue;
    if (code === 0) {
      Object.assign(state, defaultAnsiState());
    } else if (code === 1) {
      state.bold = true;
    } else if (code === 2) {
      state.dim = true;
    } else if (code === 3) {
      state.italic = true;
    } else if (code === 4) {
      state.underline = true;
    } else if (code === 22) {
      state.bold = false;
      state.dim = false;
    } else if (code === 23) {
      state.italic = false;
    } else if (code === 24) {
      state.underline = false;
    } else if (code >= 30 && code <= 37) {
      state.fg = ANSI_COLORS[code - 30];
    } else if (code === 39) {
      state.fg = null;
    } else if (code >= 40 && code <= 47) {
      state.bg = ANSI_COLORS[code - 40];
    } else if (code === 49) {
      state.bg = null;
    } else if (code >= 90 && code <= 97) {
      state.fg = ANSI_BRIGHT_COLORS[code - 90];
    } else if (code >= 100 && code <= 107) {
      state.bg = ANSI_BRIGHT_COLORS[code - 100];
    } else if (code === 38 || code === 48) {
      const next = codes[i + 1];
      if (next === 5 && Number.isInteger(codes[i + 2])) {
        const color = ansi256ToCss(codes[i + 2]);
        if (code === 38) state.fg = color;
        else state.bg = color;
        i += 2;
      } else if (
        next === 2 &&
        Number.isInteger(codes[i + 2]) &&
        Number.isInteger(codes[i + 3]) &&
        Number.isInteger(codes[i + 4])
      ) {
        const r = codes[i + 2];
        const g = codes[i + 3];
        const b = codes[i + 4];
        const color = `rgb(${r}, ${g}, ${b})`;
        if (code === 38) state.fg = color;
        else state.bg = color;
        i += 4;
      }
    }
  }
}

function buildStyledSpan(text, state) {
  if (!text) return null;
  const span = document.createElement("span");
  span.textContent = text;
  if (state.fg) span.style.color = state.fg;
  if (state.bg) span.style.backgroundColor = state.bg;
  if (state.bold) span.style.fontWeight = "700";
  if (state.dim) span.style.opacity = "0.75";
  if (state.italic) span.style.fontStyle = "italic";
  if (state.underline) span.style.textDecoration = "underline";
  return span;
}

function renderAnsiLine(line) {
  const row = document.createElement("div");
  row.className = "log-line";
  const state = defaultAnsiState();
  let lastIndex = 0;
  let match = null;

  while ((match = ANSI_RE.exec(line)) !== null) {
    const text = line.slice(lastIndex, match.index);
    const span = buildStyledSpan(text, state);
    if (span) row.appendChild(span);

    const codes = match[1]
      ? match[1].split(";").map(value => Number.parseInt(value, 10))
      : [];
    applyAnsiCodes(state, codes);
    lastIndex = match.index + match[0].length;
  }

  const tail = line.slice(lastIndex);
  const tailSpan = buildStyledSpan(tail, state);
  if (tailSpan) row.appendChild(tailSpan);
  if (!row.childNodes.length) row.appendChild(document.createTextNode(""));
  ANSI_RE.lastIndex = 0;
  return row;
}

function setLogEmptyState() {
  const el = document.getElementById("log");
  if (el.childNodes.length) return;
  const empty = document.createElement("div");
  empty.className = "log-line log-empty";
  empty.textContent = "Brak danych w podglądzie.";
  el.appendChild(empty);
}

function clearLogEmptyState() {
  const el = document.getElementById("log");
  const first = el.firstElementChild;
  if (first && first.classList.contains("log-empty")) {
    first.remove();
  }
}

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
  document.getElementById("dtrCheckbox").checked = !!data.dtr;
  document.getElementById("rtsCheckbox").checked = !!data.rts;
}

function appendToLog(lines) {
  const el = document.getElementById("log");
  const atBottom = (el.scrollTop + el.clientHeight) >= (el.scrollHeight - 8);

  for (const l of lines) {
    clearLogEmptyState();
    el.appendChild(renderAnsiLine(l));
  }
  if (atBottom) el.scrollTop = el.scrollHeight;
  setLogEmptyState();
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

async function pushControlLines() {
  const dtr = document.getElementById("dtrCheckbox").checked;
  const rts = document.getElementById("rtsCheckbox").checked;
  const res = await fetch("/api/control-lines", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({dtr, rts})
  });
  const data = await res.json();
  if (!res.ok || !data.ok) {
    await fetchStatus();
    alert(data.error || "Błąd ustawiania DTR/RTS");
    return;
  }
  setStatus(data.running);
}

document.getElementById("refreshPorts").onclick = fetchPorts;
document.getElementById("dtrCheckbox").onchange = pushControlLines;
document.getElementById("rtsCheckbox").onchange = pushControlLines;

document.getElementById("startBtn").onclick = async () => {
  const port = document.getElementById("portSelect").value;
  const baud = parseInt(document.getElementById("baudInput").value, 10);
  const format = document.getElementById("formatSelect").value;
  const dtr = document.getElementById("dtrCheckbox").checked;
  const rts = document.getElementById("rtsCheckbox").checked;
  const res = await fetch("/api/start", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({port, baud, format, dtr, rts})
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
  document.getElementById("log").replaceChildren();
  setLogEmptyState();
  lastId = 0; // ponownie pobierze od początku bufora (jeśli chcesz inaczej, zmień)
};

(async function init(){
  setLogEmptyState();
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
    with state_lock:
        running = reader_thread is not None and reader_thread.is_alive()
        port = current_port
        baud = current_baud
        logfile = log_path
        fmt = current_format
        dtr = desired_dtr
        rts = desired_rts

    return jsonify({
        "running": running,
        "port": port,
        "baud": baud,
        "logfile": logfile,
        "format": fmt,
        "dtr": dtr,
        "rts": rts,
    })


@app.post("/api/start")
def api_start():
    global current_format, desired_dtr, desired_rts
    data = request.get_json(force=True, silent=True) or {}
    port = (data.get("port") or "").strip()
    baud = data.get("baud", 115200)
    fmt = (data.get("format") or "string").strip().lower()

    with state_lock:
        current_dtr = desired_dtr
        current_rts = desired_rts

    if fmt not in ("string", "hex"):
        return jsonify({"ok": False, "error": "Nieprawidłowy format (string/hex)."}), 400

    if not port:
        return jsonify({"ok": False, "error": "Nie wybrano portu."}), 400

    try:
        dtr_state = parse_bool_field(data.get("dtr", current_dtr), "dtr")
        rts_state = parse_bool_field(data.get("rts", current_rts), "rts")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    with state_lock:
        current_format = fmt
        desired_dtr = dtr_state
        desired_rts = rts_state

    ok, msg = start_listening(port, int(baud), dtr_state, rts_state)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 500
    return jsonify({"ok": True})


@app.post("/api/stop")
def api_stop():
    stop_listening()
    return jsonify({"ok": True})


@app.post("/api/control-lines")
def api_control_lines():
    data = request.get_json(force=True, silent=True) or {}

    try:
        dtr_state = parse_bool_field(data.get("dtr"), "dtr")
        rts_state = parse_bool_field(data.get("rts"), "rts")
    except ValueError as e:
        return jsonify({"ok": False, "error": str(e)}), 400

    try:
        update_control_lines(dtr_state, rts_state)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Nie mogę ustawić DTR/RTS: {e}"}), 500

    target = "na otwartym porcie" if is_running() else "dla następnego otwarcia portu"
    append_line(
        f"{now_ts()} [INFO] Ustawiono DTR={format_signal_state(dtr_state)} "
        f"RTS={format_signal_state(rts_state)} {target}"
    )
    write_to_file(
        f"{now_ts()} [INFO] Ustawiono DTR={format_signal_state(dtr_state)} "
        f"RTS={format_signal_state(rts_state)} {target}"
    )

    return jsonify({
        "ok": True,
        "running": is_running(),
        "dtr": dtr_state,
        "rts": rts_state,
    })


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
    parser.add_argument("--logfile", default="logs/serial.log", help="plik logu")
    args = parser.parse_args()

    log_path = args.logfile
    log_dir = os.path.dirname(log_path)
    if log_dir:
        os.makedirs(log_dir, exist_ok=True)
    # dopisz informację startową
    append_line(f"{now_ts()} [INFO] Serwer WWW start: http://{args.host}:{args.port}  logfile={log_path}")
    write_to_file(f"{now_ts()} [INFO] Serwer WWW start: http://{args.host}:{args.port}  logfile={log_path}")

    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
