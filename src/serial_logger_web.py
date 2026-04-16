#!/usr/bin/env python3
import argparse
import json
import os
import threading
import uuid
from collections import deque
from datetime import datetime

from flask import Flask, Response, jsonify, request, stream_with_context
import serial
from serial.tools import list_ports

app = Flask(__name__)

RING_MAX = 20000

sessions_lock = threading.Lock()
sessions = {}
next_session_number = 1
session_meta_path = None
session_logs_dir = None
legacy_log_path = None


def now_ts():
    return datetime.now().isoformat(timespec="seconds")


def list_serial_ports():
    ports = []
    for port_info in list_ports.comports():
        ports.append(port_info.device)
    return ports


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


def format_signal_state(value: bool) -> str:
    return "ON" if value else "OFF"


def format_serial_chunk(raw: bytes, fmt: str) -> str:
    if fmt == "hex":
        return " ".join(f"{byte:02x}" for byte in raw) + (" " if raw else "")
    return raw.decode("utf-8", errors="replace")


def format_text_with_timestamps(text: str, ts: str | None, state: dict) -> str:
    if not text:
        return ""

    prefix = f"{ts} " if ts else ""
    out = []
    idx = 0
    while idx < len(text):
        if state["at_line_start"] and prefix:
            out.append(prefix)
            state["at_line_start"] = False

        ch = text[idx]
        if ch == "\r":
            out.append("\r")
            if idx + 1 < len(text) and text[idx + 1] == "\n":
                out.append("\n")
                idx += 1
            state["at_line_start"] = True
        elif ch == "\n":
            out.append("\n")
            state["at_line_start"] = True
        else:
            out.append(ch)
        idx += 1

    return "".join(out)


def infer_plain_log_line_start(path: str) -> bool:
    try:
        if not os.path.exists(path) or os.path.getsize(path) == 0:
            return True
        with open(path, "rb") as f:
            f.seek(-1, os.SEEK_END)
            return f.read(1) in (b"\n", b"\r")
    except OSError:
        return True


def atomic_write_json(path: str, payload):
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8", errors="replace") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp_path, path)


class SerialSession:
    def __init__(self, session_id: str, name: str, config: dict | None = None):
        config = config or {}

        self.id = session_id
        self.name = name

        self.state_lock = threading.Lock()
        self.log_io_lock = threading.Lock()
        self.reader_thread = None
        self.stop_event = threading.Event()
        self.ser = None

        self.current_port = config.get("port")
        self.current_baud = config.get("baud")
        self.current_format = config.get("format", "string")
        self.desired_dtr = bool(config.get("dtr", False))
        self.desired_rts = bool(config.get("rts", False))

        default_log_path = os.path.join(session_logs_dir, f"{self.id}.log")
        self.log_path = config.get("log_path") or default_log_path
        self.structured_log_path = self.log_path + ".jsonl"

        os.makedirs(os.path.dirname(self.log_path) or ".", exist_ok=True)
        self.ring = deque(maxlen=RING_MAX)  # elementy: (id, ts, source, text)
        self.next_id = 1
        self.plain_log_state = {"at_line_start": infer_plain_log_line_start(self.log_path)}
        self.bootstrap_structured_log()

    def export_config(self) -> dict:
        with self.state_lock:
            return {
                "id": self.id,
                "name": self.name,
                "port": self.current_port,
                "baud": self.current_baud,
                "format": self.current_format,
                "dtr": self.desired_dtr,
                "rts": self.desired_rts,
                "log_path": self.log_path,
            }

    def is_running(self) -> bool:
        with self.state_lock:
            return self.reader_thread is not None and self.reader_thread.is_alive()

    def status_payload(self) -> dict:
        with self.state_lock:
            running = self.reader_thread is not None and self.reader_thread.is_alive()
            return {
                "id": self.id,
                "name": self.name,
                "running": running,
                "port": self.current_port,
                "baud": self.current_baud,
                "logfile": os.path.abspath(self.log_path),
                "format": self.current_format,
                "dtr": self.desired_dtr,
                "rts": self.desired_rts,
            }

    def append_buffer_record(self, ts: str | None, source: str, text: str):
        if not text:
            return
        with self.state_lock:
            record_id = self.next_id
            self.next_id += 1
            self.ring.append((record_id, ts, source, text))

    def write_log_files(self, ts: str | None, source: str, text: str):
        if not text:
            return

        record = {"ts": ts, "source": source, "text": text}
        plain_text = format_text_with_timestamps(text, ts, self.plain_log_state)

        with self.log_io_lock:
            with open(self.log_path, "a", encoding="utf-8", errors="replace") as plain_file:
                plain_file.write(plain_text)
                plain_file.flush()

            with open(self.structured_log_path, "a", encoding="utf-8", errors="replace") as jsonl_file:
                jsonl_file.write(json.dumps(record, ensure_ascii=False) + "\n")
                jsonl_file.flush()

    def append_record(
        self,
        text: str,
        *,
        ts: str | None = None,
        source: str = "serial",
        include_in_buffer: bool = True,
    ):
        if not text:
            return

        ts = ts or now_ts()
        if include_in_buffer:
            self.append_buffer_record(ts, source, text)
        self.write_log_files(ts, source, text)

    def log_file_event(self, level: str, message: str):
        self.append_record(
            f"[{level}] {message}\n",
            ts=now_ts(),
            source="event",
            include_in_buffer=False,
        )

    def log_terminal_event(self, level: str, message: str):
        self.append_record(
            f"[{level}] {message}\n",
            ts=now_ts(),
            source="event",
            include_in_buffer=True,
        )

    def bootstrap_structured_log(self):
        if os.path.exists(self.structured_log_path):
            return

        if not os.path.exists(self.log_path) or os.path.getsize(self.log_path) == 0:
            open(self.structured_log_path, "a", encoding="utf-8").close()
            return

        with open(self.log_path, "r", encoding="utf-8", errors="replace") as src, open(
            self.structured_log_path, "w", encoding="utf-8", errors="replace"
        ) as dst:
            legacy_text = src.read()
            if legacy_text:
                dst.write(
                    json.dumps(
                        {"ts": None, "source": "legacy", "text": legacy_text},
                        ensure_ascii=False,
                    )
                    + "\n"
                )

    def iter_structured_records(self):
        if not os.path.exists(self.structured_log_path):
            return

        with open(self.structured_log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict):
                    continue
                text = record.get("text")
                if not isinstance(text, str):
                    continue
                ts = record.get("ts")
                if ts is not None and not isinstance(ts, str):
                    ts = None
                source = record.get("source")
                if not isinstance(source, str):
                    source = "serial"
                yield {"ts": ts, "source": source, "text": text}

    def iter_download_text(self, include_timestamps: bool):
        state = {"at_line_start": True}
        for record in self.iter_structured_records() or ():
            text = record["text"]
            if include_timestamps:
                yield format_text_with_timestamps(text, record["ts"], state)
            else:
                yield text

    def update_control_lines(self, dtr_state: bool, rts_state: bool):
        with self.state_lock:
            serial_port = self.ser

        if serial_port is not None:
            serial_port.dtr = dtr_state
            serial_port.rts = rts_state

        with self.state_lock:
            self.desired_dtr = dtr_state
            self.desired_rts = rts_state

    def update_config(self, port: str | None = None, baud: int | None = None, fmt: str | None = None):
        with self.state_lock:
            if port is not None:
                self.current_port = port
            if baud is not None:
                self.current_baud = int(baud)
            if fmt is not None:
                self.current_format = fmt

    def write_serial_input(self, text: str):
        with self.state_lock:
            serial_port = self.ser

        if serial_port is None or not serial_port.is_open:
            raise RuntimeError("Port szeregowy nie jest otwarty.")

        payload = text.encode("utf-8", errors="replace")
        serial_port.write(payload)
        serial_port.flush()

    def read_loop(self):
        self.log_file_event("INFO", f"Start czytania z {self.current_port} @ {self.current_baud}")

        try:
            while not self.stop_event.is_set():
                with self.state_lock:
                    serial_port = self.ser
                    fmt = self.current_format

                if serial_port is None:
                    break

                try:
                    waiting = serial_port.in_waiting
                    data = serial_port.read(waiting or 1)
                    if data:
                        raw = data.rstrip(b"\r\n")
                        text = format_serial_chunk(raw if fmt == "hex" else data, fmt)
                        self.append_record(text, ts=now_ts(), source="serial", include_in_buffer=True)
                except (serial.SerialException, OSError) as exc:
                    self.log_terminal_event("ERROR", f"SerialException: {exc}")
                    break
        finally:
            with self.state_lock:
                serial_port = self.ser
                self.ser = None
                self.reader_thread = None

            try:
                if serial_port is not None:
                    serial_port.close()
            except Exception:
                pass

            self.log_file_event("INFO", "Stop czytania")

    def start_listening(self, port: str, baud: int, fmt: str, dtr_state: bool, rts_state: bool):
        if self.is_running():
            self.stop_listening()

        self.stop_event.clear()

        try:
            new_ser = serial.Serial()
            new_ser.port = port
            new_ser.baudrate = int(baud)
            new_ser.timeout = 0.1
            new_ser.rtscts = False
            new_ser.dsrdtr = False
            new_ser.dtr = dtr_state
            new_ser.rts = rts_state
            new_ser.open()
        except Exception as exc:
            return False, f"Nie mogę otworzyć {port}: {exc}"

        with self.state_lock:
            self.current_port = port
            self.current_baud = int(baud)
            self.current_format = fmt
            self.desired_dtr = dtr_state
            self.desired_rts = rts_state
            self.ser = new_ser

        self.log_file_event(
            "INFO",
            f"Port otwarty: {port} @ {baud} "
            f"DTR={format_signal_state(dtr_state)} RTS={format_signal_state(rts_state)}",
        )

        thread = threading.Thread(target=self.read_loop, daemon=True, name=f"serial-session-{self.id}")
        with self.state_lock:
            self.reader_thread = thread
        thread.start()
        return True, "OK"

    def stop_listening(self):
        self.stop_event.set()

        with self.state_lock:
            thread = self.reader_thread
            serial_port = self.ser

        if thread and thread.is_alive():
            thread.join(timeout=2.0)

        try:
            if serial_port is not None:
                serial_port.close()
        except Exception:
            pass

        with self.state_lock:
            self.ser = None
            self.reader_thread = None
        return True


def get_session(session_id: str) -> SerialSession | None:
    with sessions_lock:
        return sessions.get(session_id)


def get_default_session() -> SerialSession | None:
    with sessions_lock:
        if not sessions:
            return None
        first_key = next(iter(sessions))
        return sessions[first_key]


def list_session_summaries() -> list[dict]:
    with sessions_lock:
        current_sessions = list(sessions.values())
    return [session.status_payload() for session in current_sessions]


def persist_sessions_metadata():
    with sessions_lock:
        payload = {
            "next_session_number": next_session_number,
            "sessions": [session.export_config() for session in sessions.values()],
        }
    atomic_write_json(session_meta_path, payload)


def create_session(config: dict | None = None, persist: bool = True) -> SerialSession:
    global next_session_number
    config = dict(config or {})

    session_id = config.get("id")
    name = config.get("name")

    with sessions_lock:
        if not session_id:
            session_id = uuid.uuid4().hex[:8]
            while session_id in sessions:
                session_id = uuid.uuid4().hex[:8]
        if not name:
            name = f"Sesja {next_session_number}"
            next_session_number += 1
        session = SerialSession(session_id, name, config)
        sessions[session_id] = session

    if persist:
        persist_sessions_metadata()
    return session


def delete_session(session_id: str):
    with sessions_lock:
        session = sessions.pop(session_id, None)
        remaining_ids = list(sessions.keys())

    if session is None:
        return False, None

    session.stop_listening()

    replacement_id = remaining_ids[0] if remaining_ids else None
    if replacement_id is None:
        replacement = create_session(persist=False)
        replacement_id = replacement.id

    persist_sessions_metadata()
    return True, replacement_id


def load_sessions_metadata():
    global next_session_number

    loaded = False
    if session_meta_path and os.path.exists(session_meta_path):
        try:
            with open(session_meta_path, "r", encoding="utf-8", errors="replace") as f:
                data = json.load(f)
            raw_sessions = data.get("sessions")
            raw_next_number = data.get("next_session_number", 1)
            if isinstance(raw_sessions, list):
                next_session_number = int(raw_next_number) if int(raw_next_number) > 0 else 1
                for item in raw_sessions:
                    if isinstance(item, dict):
                        create_session(item, persist=False)
                loaded = True
        except Exception:
            loaded = False

    if not loaded or not sessions:
        default_config = {}
        if legacy_log_path:
            default_config["log_path"] = legacy_log_path
        create_session(default_config, persist=False)
        persist_sessions_metadata()


def session_not_found(session_id: str):
    return jsonify({"ok": False, "error": f"Nie ma sesji o id={session_id}."}), 404


def session_from_request(session_id: str):
    session = get_session(session_id)
    if session is None:
        return None, session_not_found(session_id)
    return session, None


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
      --tab-bg: #ffffff;
      --tab-active-bg: #e0f2fe;
      --tab-active-border: #38bdf8;
    }
    body {
      font-family: system-ui, sans-serif;
      margin: 16px;
      color: #0f172a;
      background: #f8fafc;
    }
    .row {
      display: flex;
      gap: 12px;
      flex-wrap: wrap;
      align-items: center;
    }
    .sessions-row {
      display: flex;
      gap: 12px;
      align-items: center;
      margin-bottom: 16px;
    }
    #sessionTabs {
      display: flex;
      gap: 10px;
      flex: 1;
      overflow-x: auto;
      padding-bottom: 4px;
    }
    .session-tab {
      min-width: 180px;
      display: flex;
      flex-direction: column;
      align-items: flex-start;
      gap: 3px;
      padding: 10px 12px;
      border: 1px solid var(--panel-border);
      border-radius: 10px;
      background: var(--tab-bg);
      color: inherit;
      cursor: pointer;
      text-align: left;
      transition: border-color 120ms ease, background 120ms ease;
    }
    .session-tab.active {
      background: var(--tab-active-bg);
      border-color: var(--tab-active-border);
      box-shadow: 0 0 0 1px rgba(56, 189, 248, 0.2);
    }
    .session-tab-name {
      font-weight: 700;
    }
    .session-tab-meta {
      font-size: 12px;
      color: #475569;
    }
    .session-tab-state {
      font-size: 11px;
      font-weight: 700;
      letter-spacing: 0.04em;
    }
    .session-tab-state.run {
      color: #15803d;
    }
    .session-tab-state.stop {
      color: #b91c1c;
    }
    select, input, button {
      padding: 8px;
      font-size: 14px;
    }
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
    .log-ts {
      color: #93c5fd;
      opacity: 0.85;
      user-select: none;
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
    .badge {
      padding: 4px 8px;
      border-radius: 999px;
      font-size: 12px;
    }
    .on {
      background: #d1fae5;
    }
    .off {
      background: #fee2e2;
    }
  </style>
</head>
<body>
  <h2>Serial Logger</h2>

  <div class="sessions-row">
    <div id="sessionTabs"></div>
    <button id="addSessionBtn">Nowa sesja</button>
    <button id="removeSessionBtn">Usuń sesję</button>
  </div>

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
    <label><input id="showTimestampsCheckbox" type="checkbox" checked /> Pokaż czasy</label>

    <button id="startBtn">Start</button>
    <button id="stopBtn">Stop</button>
    <button id="clearBtn">Wyczyść podgląd</button>
    <button id="downloadLogBtn">Pobierz log</button>
    <button id="downloadLogWithTsBtn">Pobierz log + czasy</button>
  </div>

  <p style="margin-top: 8px; color:#666;">
    Jedna zakładka to jedna sesja UART z własnym stanem, logiem i ustawieniami.
  </p>

  <p id="terminalStatus">Kliknij panel logu, aby przejąć klawiaturę i wysyłać znaki na UART.</p>
  <div id="log" tabindex="0" role="textbox" aria-label="Log UART"></div>

<script>
let sessionSummaries = [];
let activeSessionId = null;
let sessionCaches = new Map();
let ports = [];
let logPolling = null;
let sessionsPolling = null;
let pollInFlight = false;
let sessionsRefreshInFlight = false;
let terminalFocused = false;
let terminalRunning = false;
let inputBuffer = "";
let inputTargetSessionId = null;
let inputInFlight = false;
let inputFlushTimer = null;
let statusTimer = null;
let statusFlash = null;
let showTimestamps = true;
let pendingEscape = "";
let currentLineEl = null;
let ansiState = null;
let configPushTimer = null;
let configPushInFlight = false;

const LOG_BUFFER_MAX = 20000;
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

function getActiveSummary() {
  return sessionSummaries.find(session => session.id === activeSessionId) || null;
}

function ensureSessionCache(sessionId) {
  if (!sessionCaches.has(sessionId)) {
    sessionCaches.set(sessionId, {lastId: 0, entries: [], draft: null, draftDirty: false});
  }
  return sessionCaches.get(sessionId);
}

function getActiveCache() {
  if (!activeSessionId) return null;
  return ensureSessionCache(activeSessionId);
}

function buildDraftFromStatus(status) {
  return {
    port: status && status.port ? status.port : "",
    baud: status && status.baud ? status.baud : 115200,
    format: status && status.format ? status.format : "string",
    dtr: !!(status && status.dtr),
    rts: !!(status && status.rts),
  };
}

function getSessionDraft(sessionId) {
  if (!sessionId) return null;
  const cache = ensureSessionCache(sessionId);
  if (!cache.draft) {
    const summary = sessionSummaries.find(session => session.id === sessionId) || null;
    cache.draft = buildDraftFromStatus(summary);
  }
  return cache.draft;
}

function syncDraftFromStatus(status) {
  const cache = ensureSessionCache(status.id);
  if (!cache.draft) {
    cache.draft = buildDraftFromStatus(status);
  } else {
    if (!cache.draftDirty || status.running) {
      cache.draft.port = status.port || "";
      cache.draft.baud = status.baud || 115200;
      cache.draft.format = status.format || "string";
      cache.draftDirty = false;
    }
    cache.draft.dtr = !!status.dtr;
    cache.draft.rts = !!status.rts;
  }
  return cache.draft;
}

function applyDraftToControls(draft) {
  const normalized = draft || buildDraftFromStatus(null);
  ensurePortOption(normalized.port);
  document.getElementById("portSelect").value = normalized.port || "";
  document.getElementById("baudInput").value = normalized.baud || 115200;
  document.getElementById("formatSelect").value = normalized.format || "string";
  document.getElementById("dtrCheckbox").checked = !!normalized.dtr;
  document.getElementById("rtsCheckbox").checked = !!normalized.rts;
}

function updateActiveDraft(patch, options = {}) {
  if (!activeSessionId) return;
  const cache = ensureSessionCache(activeSessionId);
  const draft = cache.draft || buildDraftFromStatus(getActiveSummary());
  Object.assign(draft, patch);
  cache.draft = draft;
  if (options.markDirty !== false) {
    cache.draftDirty = true;
  }
}

function scheduleConfigPush() {
  if (!activeSessionId) return;
  if (configPushTimer) {
    clearTimeout(configPushTimer);
  }
  configPushTimer = setTimeout(() => {
    configPushTimer = null;
    pushDraftConfig();
  }, 120);
}

function pruneSessionCaches() {
  const ids = new Set(sessionSummaries.map(session => session.id));
  for (const key of Array.from(sessionCaches.keys())) {
    if (!ids.has(key)) {
      sessionCaches.delete(key);
    }
  }
}

function renderSessionTabs() {
  const tabs = document.getElementById("sessionTabs");
  tabs.innerHTML = "";

  for (const session of sessionSummaries) {
    const tab = document.createElement("button");
    tab.className = "session-tab" + (session.id === activeSessionId ? " active" : "");
    tab.type = "button";
    tab.onclick = () => activateSession(session.id);

    const name = document.createElement("div");
    name.className = "session-tab-name";
    name.textContent = session.name;

    const meta = document.createElement("div");
    meta.className = "session-tab-meta";
    meta.textContent = session.port || "(brak portu)";

    const state = document.createElement("div");
    state.className = "session-tab-state " + (session.running ? "run" : "stop");
    state.textContent = session.running ? "RUN" : "STOP";

    tab.appendChild(name);
    tab.appendChild(meta);
    tab.appendChild(state);
    tabs.appendChild(tab);
  }
}

function ensurePortOption(port) {
  if (!port) return;
  const select = document.getElementById("portSelect");
  for (const option of select.options) {
    if (option.value === port) return;
  }
  const option = document.createElement("option");
  option.value = port;
  option.textContent = port;
  select.appendChild(option);
}

function setControlsEnabled(enabled) {
  document.getElementById("startBtn").disabled = !enabled;
  document.getElementById("stopBtn").disabled = !enabled;
  document.getElementById("clearBtn").disabled = !enabled;
  document.getElementById("downloadLogBtn").disabled = !enabled;
  document.getElementById("downloadLogWithTsBtn").disabled = !enabled;
  document.getElementById("removeSessionBtn").disabled = !enabled;
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

  if (!activeSessionId) {
    el.textContent = "Dodaj sesję, aby rozpocząć pracę.";
    el.className = "";
    return;
  }

  if (!terminalRunning) {
    el.textContent = "Port zamknięty. Kliknij Start, a potem panel logu, żeby wysyłać klawisze na UART.";
    el.className = "";
    return;
  }

  if (terminalFocused) {
    el.textContent = "Klawiatura aktywna: wpisy trafiają bezpośrednio do aktywnej sesji UART.";
    el.className = "active";
    return;
  }

  el.textContent = "Kliknij panel logu, aby przejąć klawiaturę i wysyłać znaki na UART.";
  el.className = "";
}

function setStatus(running) {
  terminalRunning = !!running;
  const badge = document.getElementById("statusBadge");
  if (running) {
    badge.textContent = "RUN";
    badge.classList.remove("off");
    badge.classList.add("on");
  } else {
    badge.textContent = "STOP";
    badge.classList.remove("on");
    badge.classList.add("off");
  }
  renderTerminalStatus();
}

async function fetchPorts() {
  const res = await fetch("/api/ports");
  const data = await res.json();
  ports = Array.isArray(data.ports) ? data.ports : [];

  const select = document.getElementById("portSelect");
  select.innerHTML = "";
  for (const port of ports) {
    const option = document.createElement("option");
    option.value = port;
    option.textContent = port;
    select.appendChild(option);
  }

  if (ports.length === 0) {
    const option = document.createElement("option");
    option.value = "";
    option.textContent = "(brak portów)";
    select.appendChild(option);
  }

  const draft = getSessionDraft(activeSessionId);
  if (draft && draft.port) {
    ensurePortOption(draft.port);
    select.value = draft.port;
  } else {
    const active = getActiveSummary();
    if (active && active.port) {
      ensurePortOption(active.port);
      select.value = active.port;
    }
  }
}

function mergeSessionSummary(summary) {
  const idx = sessionSummaries.findIndex(session => session.id === summary.id);
  if (idx >= 0) {
    sessionSummaries[idx] = {...sessionSummaries[idx], ...summary};
  }
}

function applyActiveStatus(status) {
  mergeSessionSummary(status);
  renderSessionTabs();
  setStatus(status.running);
  const draft = syncDraftFromStatus(status);
  applyDraftToControls(draft);
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

function buildTimestampSpan(ts) {
  const span = document.createElement("span");
  span.className = "log-ts";
  span.dataset.role = "timestamp";
  span.textContent = `${ts} `;
  return span;
}

function ensureCurrentLine(ts = null) {
  if (currentLineEl) return currentLineEl;
  const line = document.createElement("div");
  line.className = "log-line";
  if (showTimestamps && ts) {
    line.appendChild(buildTimestampSpan(ts));
  }
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

function appendStyledText(text, ts = null) {
  if (!text) return;
  clearLogEmptyState();
  const span = buildStyledSpan(text);
  if (span) ensureCurrentLine(ts).appendChild(span);
}

function appendNewLine(ts = null) {
  clearLogEmptyState();
  if (!currentLineEl) {
    ensureCurrentLine(ts);
  }
  currentLineEl = null;
}

function removeLastRenderedChar() {
  if (!currentLineEl) return;
  while (currentLineEl.lastChild) {
    const node = currentLineEl.lastChild;
    if (node.dataset && node.dataset.role === "timestamp") {
      return;
    }
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

function appendChunkToLog(entry) {
  let input = pendingEscape + entry.text;
  pendingEscape = "";
  let textStart = 0;
  const entryTs = entry.ts || null;

  for (let i = 0; i < input.length; i++) {
    const ch = input[i];

    if (ch === "\x1b") {
      appendStyledText(input.slice(textStart, i), entryTs);
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
      appendStyledText(input.slice(textStart, i), entryTs);
      if (input[i + 1] === "\n") {
        i += 1;
      }
      appendNewLine(entryTs);
      textStart = i + 1;
      continue;
    }

    if (ch === "\n") {
      appendStyledText(input.slice(textStart, i), entryTs);
      appendNewLine(entryTs);
      textStart = i + 1;
      continue;
    }

    if (ch === "\b") {
      appendStyledText(input.slice(textStart, i), entryTs);
      removeLastRenderedChar();
      textStart = i + 1;
    }
  }

  appendStyledText(input.slice(textStart), entryTs);
}

function appendToLog(entries) {
  const log = document.getElementById("log");
  const atBottom = (log.scrollTop + log.clientHeight) >= (log.scrollHeight - 8);
  for (const entry of entries) {
    appendChunkToLog(entry);
  }
  if (atBottom) {
    log.scrollTop = log.scrollHeight;
  }
}

function resetLogRenderState() {
  pendingEscape = "";
  currentLineEl = null;
  ansiState = defaultAnsiState();
}

function rebuildLogView() {
  const log = document.getElementById("log");
  log.replaceChildren();
  resetLogRenderState();
  const cache = getActiveCache();
  if (!cache || cache.entries.length === 0) {
    setLogEmptyState();
    return;
  }
  appendToLog(cache.entries);
}

async function fetchActiveStatus() {
  if (!activeSessionId) {
    setControlsEnabled(false);
    return;
  }

  const sessionId = activeSessionId;
  const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/status`);
  if (!res.ok) {
    return;
  }
  const data = await res.json();
  if (sessionId !== activeSessionId) return;

  setControlsEnabled(true);
  applyActiveStatus(data);
}

async function activateSession(sessionId) {
  if (!sessionId) return;
  activeSessionId = sessionId;
  window.localStorage.setItem("active_session_id", sessionId);
  ensureSessionCache(sessionId);
  renderSessionTabs();
  rebuildLogView();
  await fetchActiveStatus();
  await pollActiveLog();
}

async function refreshSessionsList(preferredActiveId = null) {
  if (sessionsRefreshInFlight) return;
  sessionsRefreshInFlight = true;

  try {
    const res = await fetch("/api/sessions");
    const data = await res.json();
    sessionSummaries = Array.isArray(data.sessions) ? data.sessions : [];
    pruneSessionCaches();
    renderSessionTabs();

    if (sessionSummaries.length === 0) {
      activeSessionId = null;
      setControlsEnabled(false);
      setStatus(false);
      rebuildLogView();
      return;
    }

    const availableIds = new Set(sessionSummaries.map(session => session.id));
    let nextActive = preferredActiveId;

    if (!nextActive || !availableIds.has(nextActive)) {
      const storedActive = window.localStorage.getItem("active_session_id");
      if (storedActive && availableIds.has(storedActive)) {
        nextActive = storedActive;
      }
    }

    if (!nextActive || !availableIds.has(nextActive)) {
      nextActive = sessionSummaries[0].id;
    }

    if (nextActive !== activeSessionId) {
      await activateSession(nextActive);
    } else {
      const summary = getActiveSummary();
      if (summary) {
        setControlsEnabled(true);
        applyActiveStatus(summary);
      }
      renderSessionTabs();
    }
  } finally {
    sessionsRefreshInFlight = false;
  }
}

async function pollActiveLog() {
  if (pollInFlight || !activeSessionId) return;

  const sessionId = activeSessionId;
  const cache = ensureSessionCache(sessionId);
  pollInFlight = true;

  try {
    const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/log?since=${cache.lastId}`);
    if (!res.ok) return;
    const data = await res.json();
    const entries = Array.isArray(data.chunks)
      ? data.chunks.map(item => ({
          text: item.text,
          ts: item.ts || null,
          source: item.source || "serial",
        }))
      : [];

    if (entries.length > 0) {
      cache.entries.push(...entries);
      if (cache.entries.length > LOG_BUFFER_MAX) {
        cache.entries.splice(0, cache.entries.length - LOG_BUFFER_MAX);
      }
      if (sessionId === activeSessionId) {
        appendToLog(entries);
      }
    }

    if (typeof data.to_id === "number") {
      cache.lastId = data.to_id;
    }
  } finally {
    pollInFlight = false;
  }
}

function startPolling() {
  if (!logPolling) {
    logPolling = setInterval(pollActiveLog, 100);
  }
  if (!sessionsPolling) {
    sessionsPolling = setInterval(() => { refreshSessionsList(); }, 1500);
  }
}

function initLogPanel() {
  ansiState = defaultAnsiState();
  const log = document.getElementById("log");
  const showTsCheckbox = document.getElementById("showTimestampsCheckbox");
  const portSelect = document.getElementById("portSelect");
  const baudInput = document.getElementById("baudInput");
  const formatSelect = document.getElementById("formatSelect");
  const storedPreference = window.localStorage.getItem("show_timestamps");
  showTimestamps = storedPreference !== "0";
  showTsCheckbox.checked = showTimestamps;

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
  showTsCheckbox.addEventListener("change", () => {
    showTimestamps = showTsCheckbox.checked;
    window.localStorage.setItem("show_timestamps", showTimestamps ? "1" : "0");
    rebuildLogView();
  });
  portSelect.addEventListener("change", () => {
    updateActiveDraft({port: portSelect.value});
    scheduleConfigPush();
  });
  baudInput.addEventListener("change", () => {
    const baud = Number.parseInt(baudInput.value, 10);
    if (Number.isFinite(baud) && baud > 0) {
      updateActiveDraft({baud});
      scheduleConfigPush();
    }
  });
  formatSelect.addEventListener("change", () => {
    updateActiveDraft({format: formatSelect.value});
    scheduleConfigPush();
  });

  setLogEmptyState();
  renderTerminalStatus();
}

function queueTerminalInput(data) {
  if (!terminalRunning || !activeSessionId) {
    flashTerminalStatus("Port nie jest otwarty.");
    return;
  }

  if (inputTargetSessionId === null) {
    inputTargetSessionId = activeSessionId;
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
  if (inputInFlight || !inputBuffer || !inputTargetSessionId) return;
  inputInFlight = true;

  const payload = inputBuffer;
  const sessionId = inputTargetSessionId;
  inputBuffer = "";

  try {
    const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/input`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({text: payload})
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || "Błąd wysyłania na UART");
    }

    if (sessionId === activeSessionId && typeof data.running === "boolean") {
      setStatus(data.running);
    }
  } catch (err) {
    inputBuffer = payload + inputBuffer;
    inputTargetSessionId = sessionId;
    if (sessionId === activeSessionId) {
      flashTerminalStatus(err.message || "Błąd wysyłania na UART");
      await fetchActiveStatus();
    }
  } finally {
    inputInFlight = false;
    if (!inputBuffer) {
      inputTargetSessionId = null;
    }
    if (inputBuffer) {
      flushTerminalInput();
    }
  }
}

async function createSession() {
  const res = await fetch("/api/sessions", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({})
  });
  const data = await res.json();
  if (!res.ok || !data.ok) {
    alert(data.error || "Nie mogę dodać sesji.");
    return;
  }
  await refreshSessionsList(data.session ? data.session.id : null);
}

async function removeActiveSession() {
  const summary = getActiveSummary();
  if (!summary) return;

  const ok = window.confirm(`Usunąć sesję "${summary.name}"?`);
  if (!ok) return;

  const res = await fetch(`/api/sessions/${encodeURIComponent(summary.id)}`, {
    method: "DELETE"
  });
  const data = await res.json();
  if (!res.ok || !data.ok) {
    alert(data.error || "Nie mogę usunąć sesji.");
    return;
  }

  sessionCaches.delete(summary.id);
  await refreshSessionsList(data.active_session_id || null);
}

async function pushDraftConfig() {
  if (configPushInFlight || !activeSessionId) return;
  const sessionId = activeSessionId;
  const draft = getSessionDraft(sessionId);
  if (!draft) return;
  const payload = {
    port: draft.port || "",
    baud: draft.baud || 115200,
    format: draft.format || "string",
  };

  configPushInFlight = true;
  try {
    const res = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/config`, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(payload)
    });
    const data = await res.json();
    if (!res.ok || !data.ok) {
      throw new Error(data.error || "Nie mogę zapisać ustawień sesji.");
    }
    const currentDraft = getSessionDraft(sessionId);
    const currentMatchesPayload = !!currentDraft
      && currentDraft.port === payload.port
      && Number(currentDraft.baud || 115200) === Number(payload.baud)
      && (currentDraft.format || "string") === payload.format;
    if (currentMatchesPayload) {
      ensureSessionCache(sessionId).draftDirty = false;
    }
    if (sessionId !== activeSessionId) return;
    if (data.status) {
      applyActiveStatus(data.status);
    }
    if (!currentMatchesPayload) {
      scheduleConfigPush();
    }
  } catch (err) {
    if (sessionId === activeSessionId) {
      flashTerminalStatus(err.message || "Nie mogę zapisać ustawień sesji.");
    }
  } finally {
    configPushInFlight = false;
  }
}

async function pushControlLines() {
  if (!activeSessionId) return;
  const dtr = document.getElementById("dtrCheckbox").checked;
  const rts = document.getElementById("rtsCheckbox").checked;
  updateActiveDraft({dtr, rts}, {markDirty: false});
  const res = await fetch(`/api/sessions/${encodeURIComponent(activeSessionId)}/control-lines`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({dtr, rts})
  });
  const data = await res.json();
  if (!res.ok || !data.ok) {
    await fetchActiveStatus();
    alert(data.error || "Błąd ustawiania DTR/RTS");
    return;
  }
  await refreshSessionsList(activeSessionId);
}

async function startActiveSession() {
  if (!activeSessionId) return;

  const port = document.getElementById("portSelect").value;
  const baud = parseInt(document.getElementById("baudInput").value, 10);
  const format = document.getElementById("formatSelect").value;
  const dtr = document.getElementById("dtrCheckbox").checked;
  const rts = document.getElementById("rtsCheckbox").checked;
  updateActiveDraft({port, baud, format, dtr, rts});

  const res = await fetch(`/api/sessions/${encodeURIComponent(activeSessionId)}/start`, {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({port, baud, format, dtr, rts})
  });
  const data = await res.json();
  if (!res.ok || !data.ok) {
    alert(data.error || "Błąd startu");
    return;
  }

  const cache = ensureSessionCache(activeSessionId);
  cache.draftDirty = false;
  await refreshSessionsList(activeSessionId);
  await pollActiveLog();
}

async function stopActiveSession() {
  if (!activeSessionId) return;
  await fetch(`/api/sessions/${encodeURIComponent(activeSessionId)}/stop`, {method: "POST"});
  await refreshSessionsList(activeSessionId);
}

function clearActivePreview() {
  const cache = getActiveCache();
  if (!cache) return;
  cache.entries = [];
  rebuildLogView();
}

function downloadActiveLog(includeTimestamps) {
  if (!activeSessionId) return;
  const flag = includeTimestamps ? "1" : "0";
  window.location.href = `/api/sessions/${encodeURIComponent(activeSessionId)}/download?timestamps=${flag}`;
}

document.getElementById("refreshPorts").onclick = fetchPorts;
document.getElementById("dtrCheckbox").onchange = pushControlLines;
document.getElementById("rtsCheckbox").onchange = pushControlLines;
document.getElementById("addSessionBtn").onclick = createSession;
document.getElementById("removeSessionBtn").onclick = removeActiveSession;
document.getElementById("startBtn").onclick = startActiveSession;
document.getElementById("stopBtn").onclick = stopActiveSession;
document.getElementById("clearBtn").onclick = clearActivePreview;
document.getElementById("downloadLogBtn").onclick = () => downloadActiveLog(false);
document.getElementById("downloadLogWithTsBtn").onclick = () => downloadActiveLog(true);

(async function init() {
  initLogPanel();
  setControlsEnabled(false);
  await fetchPorts();
  await refreshSessionsList();
  startPolling();
  await pollActiveLog();
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


@app.get("/api/sessions")
def api_sessions():
    return jsonify({"sessions": list_session_summaries()})


@app.post("/api/sessions")
def api_sessions_create():
    data = request.get_json(force=True, silent=True) or {}
    name = data.get("name")
    if not isinstance(name, str) or not name.strip():
        name = None
    session = create_session({"name": name} if name else {}, persist=True)
    return jsonify({"ok": True, "session": session.status_payload()})


@app.delete("/api/sessions/<session_id>")
def api_sessions_delete(session_id: str):
    ok, next_active_id = delete_session(session_id)
    if not ok:
        return session_not_found(session_id)
    return jsonify({"ok": True, "active_session_id": next_active_id})


@app.get("/api/sessions/<session_id>/status")
def api_session_status(session_id: str):
    session, error = session_from_request(session_id)
    if error:
        return error
    return jsonify(session.status_payload())


@app.post("/api/sessions/<session_id>/config")
def api_session_config(session_id: str):
    session, error = session_from_request(session_id)
    if error:
        return error

    data = request.get_json(force=True, silent=True) or {}

    port = data.get("port")
    if port is not None:
        if not isinstance(port, str):
            return jsonify({"ok": False, "error": "Pole port musi być typu string."}), 400
        port = port.strip()

    baud = data.get("baud")
    if baud is not None:
        try:
            baud = int(baud)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "error": "Pole baud musi być liczbą całkowitą."}), 400
        if baud <= 0:
            return jsonify({"ok": False, "error": "Pole baud musi być > 0."}), 400

    fmt = data.get("format")
    if fmt is not None:
        if not isinstance(fmt, str):
            return jsonify({"ok": False, "error": "Pole format musi być typu string."}), 400
        fmt = fmt.strip().lower()
        if fmt not in ("string", "hex"):
            return jsonify({"ok": False, "error": "Nieprawidłowy format (string/hex)."}), 400

    session.update_config(port=port, baud=baud, fmt=fmt)
    persist_sessions_metadata()
    return jsonify({"ok": True, "status": session.status_payload()})


@app.post("/api/sessions/<session_id>/start")
def api_session_start(session_id: str):
    session, error = session_from_request(session_id)
    if error:
        return error

    data = request.get_json(force=True, silent=True) or {}
    port = (data.get("port") or "").strip()
    baud = data.get("baud", 115200)
    fmt = (data.get("format") or "string").strip().lower()

    if fmt not in ("string", "hex"):
        return jsonify({"ok": False, "error": "Nieprawidłowy format (string/hex)."}), 400
    if not port:
        return jsonify({"ok": False, "error": "Nie wybrano portu."}), 400

    try:
        dtr_state = parse_bool_field(data.get("dtr", session.desired_dtr), "dtr")
        rts_state = parse_bool_field(data.get("rts", session.desired_rts), "rts")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    ok, msg = session.start_listening(port, int(baud), fmt, dtr_state, rts_state)
    if not ok:
        return jsonify({"ok": False, "error": msg}), 500

    persist_sessions_metadata()
    return jsonify({"ok": True, "status": session.status_payload()})


@app.post("/api/sessions/<session_id>/stop")
def api_session_stop(session_id: str):
    session, error = session_from_request(session_id)
    if error:
        return error
    session.stop_listening()
    return jsonify({"ok": True, "status": session.status_payload()})


@app.post("/api/sessions/<session_id>/control-lines")
def api_session_control_lines(session_id: str):
    session, error = session_from_request(session_id)
    if error:
        return error

    data = request.get_json(force=True, silent=True) or {}
    try:
        dtr_state = parse_bool_field(data.get("dtr"), "dtr")
        rts_state = parse_bool_field(data.get("rts"), "rts")
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400

    try:
        session.update_control_lines(dtr_state, rts_state)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Nie mogę ustawić DTR/RTS: {exc}"}), 500

    target = "na otwartym porcie" if session.is_running() else "dla następnego otwarcia portu"
    session.log_file_event(
        "INFO",
        f"Ustawiono DTR={format_signal_state(dtr_state)} "
        f"RTS={format_signal_state(rts_state)} {target}",
    )
    persist_sessions_metadata()

    return jsonify({"ok": True, "status": session.status_payload()})


@app.post("/api/sessions/<session_id>/input")
def api_session_input(session_id: str):
    session, error = session_from_request(session_id)
    if error:
        return error

    data = request.get_json(force=True, silent=True) or {}
    text = data.get("text")
    if not isinstance(text, str):
        return jsonify({"ok": False, "error": "Pole text musi być typu string."}), 400
    if not text:
        return jsonify({"ok": True, "running": session.is_running()})

    try:
        session.write_serial_input(text)
    except Exception as exc:
        return jsonify({"ok": False, "error": f"Nie mogę wysłać danych na UART: {exc}"}), 500

    return jsonify({"ok": True, "running": session.is_running()})


@app.get("/api/sessions/<session_id>/log")
def api_session_log(session_id: str):
    session, error = session_from_request(session_id)
    if error:
        return error

    try:
        since = int(request.args.get("since", "0"))
    except ValueError:
        since = 0

    with session.state_lock:
        items = [(i, ts, source, text) for (i, ts, source, text) in session.ring if i > since]
        to_id = session.ring[-1][0] if session.ring else since

    return jsonify({
        "from_id": since,
        "to_id": to_id,
        "chunks": [
            {"id": i, "ts": ts, "source": source, "text": text}
            for (i, ts, source, text) in items
        ],
    })


@app.get("/api/sessions/<session_id>/download")
def api_session_download(session_id: str):
    session, error = session_from_request(session_id)
    if error:
        return error

    raw_value = request.args.get("timestamps", "1")
    try:
        include_timestamps = parse_bool_field(raw_value, "timestamps")
    except ValueError:
        include_timestamps = True

    suffix = "z-czasami" if include_timestamps else "bez-czasow"
    filename = f"{session.name.lower().replace(' ', '-')}-{suffix}.log"
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    return Response(
        stream_with_context(session.iter_download_text(include_timestamps)),
        content_type="text/plain; charset=utf-8",
        headers=headers,
    )


# Zachowanie kompatybilne: stare endpointy operują na pierwszej sesji.
@app.get("/api/status")
def api_status_legacy():
    session = get_default_session()
    if session is None:
        return jsonify({"ok": False, "error": "Brak sesji."}), 404
    return jsonify(session.status_payload())


@app.post("/api/start")
def api_start_legacy():
    session = get_default_session()
    if session is None:
        return jsonify({"ok": False, "error": "Brak sesji."}), 404
    return api_session_start(session.id)


@app.post("/api/stop")
def api_stop_legacy():
    session = get_default_session()
    if session is None:
        return jsonify({"ok": False, "error": "Brak sesji."}), 404
    return api_session_stop(session.id)


@app.post("/api/control-lines")
def api_control_lines_legacy():
    session = get_default_session()
    if session is None:
        return jsonify({"ok": False, "error": "Brak sesji."}), 404
    return api_session_control_lines(session.id)


@app.post("/api/input")
def api_input_legacy():
    session = get_default_session()
    if session is None:
        return jsonify({"ok": False, "error": "Brak sesji."}), 404
    return api_session_input(session.id)


@app.get("/api/log")
def api_log_legacy():
    session = get_default_session()
    if session is None:
        return jsonify({"ok": False, "error": "Brak sesji."}), 404
    return api_session_log(session.id)


@app.get("/api/download")
def api_download_legacy():
    session = get_default_session()
    if session is None:
        return jsonify({"ok": False, "error": "Brak sesji."}), 404
    return api_session_download(session.id)


def main():
    global session_meta_path, session_logs_dir, legacy_log_path

    parser = argparse.ArgumentParser(description="Serial logger + prosta strona WWW")
    parser.add_argument("--host", default="127.0.0.1", help="adres nasłuchu (np. 0.0.0.0)")
    parser.add_argument("--port", type=int, default=8080, help="port HTTP")
    parser.add_argument("--logfile", default="logs/serial.log", help="plik legacy logu dla pierwszej sesji")
    args = parser.parse_args()

    legacy_log_path = os.path.abspath(args.logfile)
    log_dir = os.path.dirname(legacy_log_path) or "."
    os.makedirs(log_dir, exist_ok=True)

    session_meta_path = os.path.join(log_dir, "session_store.json")
    session_logs_dir = os.path.join(log_dir, "sessions")
    os.makedirs(session_logs_dir, exist_ok=True)

    load_sessions_metadata()
    app.run(host=args.host, port=args.port, debug=False, threaded=True)


if __name__ == "__main__":
    main()
