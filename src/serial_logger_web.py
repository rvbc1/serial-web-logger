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
desired_dtr = False
desired_rts = False

# Przechowujemy ostatnie N fragmentów strumienia do podglądu w WWW
RING_MAX = 20000
ring = deque(maxlen=RING_MAX)  # elementy: (id, chunk)
next_id = 1


def now_ts():
    return datetime.now().isoformat(timespec="seconds")


def list_serial_ports():
    # Zwraca listę stringów /dev/tty...
    ports = []
    for p in list_ports.comports():
        ports.append(p.device)
    return ports


def append_chunk(text: str):
    global next_id
    if not text:
        return
    with state_lock:
        _id = next_id
        next_id += 1
        ring.append((_id, text))


def write_to_file(text: str):
    # dopis do pliku (UTF-8) bez modyfikowania strumienia
    with open(log_path, "a", encoding="utf-8", errors="replace") as f:
        f.write(text)
        f.flush()


def log_file_event(level: str, message: str):
    write_to_file(f"{now_ts()} [{level}] {message}\n")


def log_terminal_event(level: str, message: str):
    text = f"\r\n{now_ts()} [{level}] {message}\r\n"
    append_chunk(text)
    write_to_file(text)


def format_serial_chunk(raw: bytes, fmt: str) -> str:
    if fmt == "hex":
        return " ".join(f"{b:02x}" for b in raw) + (" " if raw else "")
    return raw.decode("utf-8", errors="replace")


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


def write_serial_input(text: str):
    with state_lock:
        serial_port = ser

    if serial_port is None or not serial_port.is_open:
        raise RuntimeError("Port szeregowy nie jest otwarty.")

    payload = text.encode("utf-8", errors="replace")
    serial_port.write(payload)
    serial_port.flush()


def read_loop():
    global ser
    log_file_event("INFO", f"Start czytania z {current_port} @ {current_baud}")

    try:
        while not stop_event.is_set():
            try:
                waiting = ser.in_waiting
                data = ser.read(waiting or 1)
                if data:
                    raw = data.rstrip(b"\r\n")
                    with state_lock:
                        fmt = current_format
                    text = format_serial_chunk(raw if fmt == "hex" else data, fmt)
                    append_chunk(text)
                    write_to_file(text)
            except serial.SerialException as e:
                log_terminal_event("ERROR", f"SerialException: {e}")
                break
    finally:
        try:
            if ser is not None:
                ser.close()
        except Exception:
            pass
        ser = None
        log_file_event("INFO", "Stop czytania")


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
        new_ser.timeout = 0.1  # szybsza reakcja terminala i stop_event
        new_ser.rtscts = False
        new_ser.dsrdtr = False
        new_ser.dtr = dtr_state
        new_ser.rts = rts_state
        new_ser.open()
        ser = new_ser
    except Exception as e:
        return False, f"Nie mogę otworzyć {port}: {e}"

    log_file_event(
        "INFO",
        f"Port otwarty: {current_port} @ {current_baud} "
        f"DTR={format_signal_state(dtr_state)} RTS={format_signal_state(rts_state)}",
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
      --term-accent: #38bdf8;
    }
    body { font-family: system-ui, sans-serif; margin: 16px; color: #0f172a; background: #f8fafc; }
    .row { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; }
    select, input, button { padding: 8px; font-size: 14px; }
    #log {
      width: 100%;
      height: 70vh;
      overflow: auto;
      box-sizing: border-box;
      padding: 14px;
      border: 1px solid var(--panel-border);
      border-radius: 10px;
      background: radial-gradient(circle at top, #172554 0%, var(--term-bg) 28%, #020617 100%);
      color: var(--term-fg);
      font: 13px/1.5 SFMono-Regular, Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
      box-shadow: inset 0 1px 0 rgba(255,255,255,0.05);
      cursor: text;
      outline: none;
    }
    #log.capture-active,
    #log:focus {
      border-color: var(--term-accent);
      box-shadow:
        0 0 0 1px rgba(56, 189, 248, 0.28),
        inset 0 1px 0 rgba(255,255,255,0.05);
    }
    .log-line {
      white-space: pre-wrap;
      word-break: break-word;
      min-height: 1.5em;
    }
    .log-empty {
      color: var(--term-muted);
      font-style: italic;
    }
    #terminalStatus {
      margin: 8px 0 12px;
      font-size: 13px;
      color: var(--term-muted);
    }
    #terminalStatus.active {
      color: #0369a1;
      font-weight: 600;
    }
    #terminalStatus.error {
      color: #b91c1c;
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

    <label><input id="dtrCheckbox" type="checkbox" /> DTR</label>
    <label><input id="rtsCheckbox" type="checkbox" /> RTS</label>

    <button id="startBtn">Start</button>
    <button id="stopBtn">Stop</button>
    <button id="clearBtn">Wyczyść podgląd</button>
  </div>

  <p style="margin-top: 8px; color:#666;">
    Podgląd pokazuje ostatnie wpisy (bufor w pamięci). Pełny log jest w pliku na serwerze.
  </p>

  <p id="terminalStatus">Kliknij panel logu, aby przejąć klawiaturę i wysyłać znaki na UART.</p>
  <div id="log" tabindex="0" role="textbox" aria-label="Log UART"></div>

<script>
let lastId = 0;
let polling = null;
let pollInFlight = false;
let terminalFocused = false;
let terminalRunning = false;
let inputBuffer = "";
let inputInFlight = false;
let inputFlushTimer = null;
let statusTimer = null;
let statusFlash = null;
let pendingEscape = "";
let currentLineEl = null;
let ansiState = null;

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

function initLogPanel() {
  ansiState = defaultAnsiState();
  const log = document.getElementById("log");
  log.addEventListener("click", () => log.focus());
  log.addEventListener("focus", () => {
    terminalFocused = true;
    renderTerminalStatus();
  });
  log.addEventListener("blur", () => {
    terminalFocused = false;
    renderTerminalStatus();
  });
  log.addEventListener("keydown", handleLogKeydown);
  log.addEventListener("paste", handleLogPaste);
  setLogEmptyState();
  renderTerminalStatus();
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
        const color = `rgb(${codes[i + 2]}, ${codes[i + 3]}, ${codes[i + 4]})`;
        if (code === 38) state.fg = color;
        else state.bg = color;
        i += 4;
      }
    }
  }
}

function flashTerminalStatus(message, variant = "error") {
  statusFlash = {message, variant};
  renderTerminalStatus();
  if (statusTimer) clearTimeout(statusTimer);
  statusTimer = setTimeout(() => {
    statusFlash = null;
    renderTerminalStatus();
  }, 2200);
}

function renderTerminalStatus() {
  const el = document.getElementById("terminalStatus");
  const log = document.getElementById("log");
  log.classList.toggle("capture-active", terminalFocused);

  if (statusFlash) {
    el.textContent = statusFlash.message;
    el.className = statusFlash.variant;
    return;
  }

  if (!terminalRunning) {
    el.textContent = "Port zamknięty. Kliknij Start, a potem panel logu, żeby wysyłać klawisze na UART.";
    el.className = "";
    return;
  }

  if (terminalFocused) {
    el.textContent = "Klawiatura aktywna: wpisy trafiają bezpośrednio na UART.";
    el.className = "active";
    return;
  }

  el.textContent = "Kliknij panel logu, aby przejąć klawiaturę i wysyłać znaki na UART.";
  el.className = "";
}

function setStatus(running) {
  terminalRunning = !!running;
  const b = document.getElementById("statusBadge");
  if (running) {
    b.textContent = "RUN";
    b.classList.remove("off"); b.classList.add("on");
  } else {
    b.textContent = "STOP";
    b.classList.remove("on"); b.classList.add("off");
  }
  renderTerminalStatus();
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

function setLogEmptyState() {
  const log = document.getElementById("log");
  if (log.childNodes.length) return;
  const empty = document.createElement("div");
  empty.className = "log-line log-empty";
  empty.textContent = "Brak danych w podglądzie.";
  log.appendChild(empty);
}

function clearLogEmptyState() {
  const log = document.getElementById("log");
  const first = log.firstElementChild;
  if (first && first.classList.contains("log-empty")) {
    first.remove();
  }
}

function ensureCurrentLine() {
  if (currentLineEl) return currentLineEl;
  const line = document.createElement("div");
  line.className = "log-line";
  document.getElementById("log").appendChild(line);
  currentLineEl = line;
  return line;
}

function buildStyledSpan(text) {
  if (!text) return null;
  const span = document.createElement("span");
  span.textContent = text;
  if (ansiState.fg) span.style.color = ansiState.fg;
  if (ansiState.bg) span.style.backgroundColor = ansiState.bg;
  if (ansiState.bold) span.style.fontWeight = "700";
  if (ansiState.dim) span.style.opacity = "0.75";
  if (ansiState.italic) span.style.fontStyle = "italic";
  if (ansiState.underline) span.style.textDecoration = "underline";
  return span;
}

function appendStyledText(text) {
  if (!text) return;
  clearLogEmptyState();
  const span = buildStyledSpan(text);
  if (span) ensureCurrentLine().appendChild(span);
}

function appendNewLine() {
  clearLogEmptyState();
  if (!currentLineEl) {
    ensureCurrentLine();
  }
  currentLineEl = null;
}

function removeLastRenderedChar() {
  if (!currentLineEl) return;
  while (currentLineEl.lastChild) {
    const node = currentLineEl.lastChild;
    if (node.nodeType === Node.TEXT_NODE) {
      if (node.textContent.length > 1) {
        node.textContent = node.textContent.slice(0, -1);
        return;
      }
      currentLineEl.removeChild(node);
      continue;
    }
    if (node.textContent.length > 1) {
      node.textContent = node.textContent.slice(0, -1);
      return;
    }
    currentLineEl.removeChild(node);
  }
}

function consumeEscapeSequence(buffer, startIndex) {
  if (startIndex + 1 >= buffer.length) {
    return {complete: false};
  }

  const next = buffer[startIndex + 1];
  if (next === "[") {
    for (let i = startIndex + 2; i < buffer.length; i++) {
      const ch = buffer[i];
      if (ch >= "@" && ch <= "~") {
        if (ch === "m") {
          const payload = buffer.slice(startIndex + 2, i);
          const codes = payload ? payload.split(";").map(value => Number.parseInt(value, 10)) : [];
          return {complete: true, nextIndex: i + 1, kind: "sgr", codes};
        }
        return {complete: true, nextIndex: i + 1, kind: "ignore"};
      }
    }
    return {complete: false};
  }

  if (next === "]") {
    for (let i = startIndex + 2; i < buffer.length; i++) {
      if (buffer[i] === "\x07") {
        return {complete: true, nextIndex: i + 1, kind: "ignore"};
      }
      if (buffer[i] === "\x1b" && buffer[i + 1] === "\\") {
        return {complete: true, nextIndex: i + 2, kind: "ignore"};
      }
    }
    return {complete: false};
  }

  return {complete: true, nextIndex: startIndex + 2, kind: "ignore"};
}

function appendChunkToLog(chunk) {
  let input = pendingEscape + chunk;
  pendingEscape = "";
  let textStart = 0;

  for (let i = 0; i < input.length; i++) {
    const ch = input[i];

    if (ch === "\x1b") {
      appendStyledText(input.slice(textStart, i));
      const seq = consumeEscapeSequence(input, i);
      if (!seq.complete) {
        pendingEscape = input.slice(i);
        return;
      }
      if (seq.kind === "sgr") {
        applyAnsiCodes(ansiState, seq.codes);
      }
      i = seq.nextIndex - 1;
      textStart = seq.nextIndex;
      continue;
    }

    if (ch === "\r") {
      appendStyledText(input.slice(textStart, i));
      if (input[i + 1] === "\n") {
        i += 1;
      }
      appendNewLine();
      textStart = i + 1;
      continue;
    }

    if (ch === "\n") {
      appendStyledText(input.slice(textStart, i));
      appendNewLine();
      textStart = i + 1;
      continue;
    }

    if (ch === "\b") {
      appendStyledText(input.slice(textStart, i));
      removeLastRenderedChar();
      textStart = i + 1;
    }
  }

  appendStyledText(input.slice(textStart));
}

function appendToLog(chunks) {
  const log = document.getElementById("log");
  const atBottom = (log.scrollTop + log.clientHeight) >= (log.scrollHeight - 8);
  for (const chunk of chunks) {
    appendChunkToLog(chunk);
  }
  if (atBottom) {
    log.scrollTop = log.scrollHeight;
  }
}

async function pollLog() {
  if (pollInFlight) return;
  pollInFlight = true;
  try {
    const res = await fetch(`/api/log?since=${lastId}`);
    const data = await res.json();
    if (data.chunks && data.chunks.length) {
      appendToLog(data.chunks.map(x => x.text));
      lastId = data.to_id;
    } else if (typeof data.to_id === "number") {
      lastId = data.to_id;
    }
  } finally {
    pollInFlight = false;
  }
}

function startPolling() {
  if (polling) return;
  polling = setInterval(pollLog, 100);
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

function queueTerminalInput(data) {
  if (!terminalRunning) {
    flashTerminalStatus("Port nie jest otwarty.");
    return;
  }
  inputBuffer += data;
  if (inputFlushTimer) return;
  inputFlushTimer = setTimeout(() => {
    inputFlushTimer = null;
    flushTerminalInput();
  }, 10);
}

function encodeCtrlKey(key) {
  if (key === " ") return "\x00";
  if (key.length !== 1) return null;
  const code = key.toUpperCase().charCodeAt(0);
  if (code >= 64 && code <= 95) {
    return String.fromCharCode(code - 64);
  }
  return null;
}

function encodeKeyEvent(event) {
  if (event.metaKey) return null;

  const special = {
    Enter: "\r",
    Backspace: "\x7f",
    Tab: "\t",
    Escape: "\x1b",
    ArrowUp: "\x1b[A",
    ArrowDown: "\x1b[B",
    ArrowRight: "\x1b[C",
    ArrowLeft: "\x1b[D",
    Home: "\x1b[H",
    End: "\x1b[F",
    Delete: "\x1b[3~",
    PageUp: "\x1b[5~",
    PageDown: "\x1b[6~",
    Insert: "\x1b[2~",
  };

  if (event.ctrlKey) {
    const encoded = encodeCtrlKey(event.key);
    if (encoded !== null) return encoded;
    if (special[event.key]) return special[event.key];
    return null;
  }

  if (event.altKey) {
    if (special[event.key]) return "\x1b" + special[event.key];
    if (event.key.length === 1) return "\x1b" + event.key;
    return null;
  }

  if (special[event.key]) return special[event.key];
  if (event.key.length === 1) return event.key;
  return null;
}

function handleLogKeydown(event) {
  const text = encodeKeyEvent(event);
  if (text === null) return;
  event.preventDefault();
  queueTerminalInput(text);
}

function handleLogPaste(event) {
  const text = event.clipboardData ? event.clipboardData.getData("text/plain") : "";
  if (!text) return;
  event.preventDefault();
  queueTerminalInput(text.replace(/\n/g, "\r"));
}

async function flushTerminalInput() {
  if (inputInFlight || !inputBuffer) return;
  inputInFlight = true;
  const payload = inputBuffer;
  inputBuffer = "";

  try {
    const res = await fetch("/api/input", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({text: payload})
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || "Błąd wysyłania na UART");
    }
    if (typeof data.running === "boolean") {
      setStatus(data.running);
    }
  } catch (err) {
    inputBuffer = payload + inputBuffer;
    flashTerminalStatus(err.message || "Błąd wysyłania na UART");
    await fetchStatus();
  } finally {
    inputInFlight = false;
    if (inputBuffer) {
      flushTerminalInput();
    }
  }
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
  lastId = 0;
  pendingEscape = "";
  currentLineEl = null;
  ansiState = defaultAnsiState();
  setLogEmptyState();
};

(async function init(){
  initLogPanel();
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
    log_file_event(
        "INFO",
        f"Ustawiono DTR={format_signal_state(dtr_state)} "
        f"RTS={format_signal_state(rts_state)} {target}",
    )

    return jsonify({
        "ok": True,
        "running": is_running(),
        "dtr": dtr_state,
        "rts": rts_state,
    })


@app.post("/api/input")
def api_input():
    data = request.get_json(force=True, silent=True) or {}
    text = data.get("text")

    if not isinstance(text, str):
        return jsonify({"ok": False, "error": "Pole text musi być typu string."}), 400
    if not text:
        return jsonify({"ok": True, "running": is_running()})

    try:
        write_serial_input(text)
    except Exception as e:
        return jsonify({"ok": False, "error": f"Nie mogę wysłać danych na UART: {e}"}), 500

    return jsonify({"ok": True, "running": is_running()})


@app.get("/api/log")
def api_log():
    # Zwraca fragmenty tekstu o id > since (inkrementalne odświeżanie)
    try:
        since = int(request.args.get("since", "0"))
    except ValueError:
        since = 0

    with state_lock:
        items = [(i, chunk) for (i, chunk) in ring if i > since]
        to_id = ring[-1][0] if ring else since

    return jsonify({
        "from_id": since,
        "to_id": to_id,
        "chunks": [{"id": i, "text": text} for (i, text) in items],
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
    log_file_event("INFO", f"Serwer WWW start: http://{args.host}:{args.port}  logfile={log_path}")

    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
