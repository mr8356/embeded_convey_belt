#!/usr/bin/env python3
"""
Conveyor event web dashboard — pure Python stdlib.
Real-time updates via Server-Sent Events (SSE).
No external dependencies beyond mosquitto_sub CLI.

Usage:
    python3 conveyor_web.py [--broker localhost] [--topic iot/conveyor/#] [--port 8080]
"""

import argparse
import json
import queue
import subprocess
import sys
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, HTTPServer

MAX_HISTORY = 200
HEARTBEAT_SEC = 25

# ── shared state ───────────────────────────────────────────────────────────────

_state_lock = threading.Lock()
_current: dict = {}
_history: deque = deque(maxlen=MAX_HISTORY)

_clients_lock = threading.Lock()
_clients: list[queue.Queue] = []


def _broadcast(ev: dict) -> None:
    msg = ("data: " + json.dumps(ev, separators=(",", ":")) + "\n\n").encode()
    with _clients_lock:
        dead = []
        for q in _clients:
            try:
                q.put_nowait(msg)
            except queue.Full:
                dead.append(q)
        for q in dead:
            _clients.remove(q)


# ── MQTT reader thread ─────────────────────────────────────────────────────────

def _mqtt_reader(broker: str, topic: str) -> None:
    cmd = ["mosquitto_sub", "-h", broker, "-t", topic, "-v"]
    while True:
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, text=True)
            for raw in proc.stdout:
                parts = raw.strip().split(" ", 1)
                if len(parts) != 2:
                    continue
                try:
                    ev = json.loads(parts[1])
                except json.JSONDecodeError:
                    continue
                ev["_wall"] = time.strftime("%H:%M:%S", time.localtime(ev.get("created_at", time.time())))
                with _state_lock:
                    _current.clear()
                    _current.update(ev)
                    _history.appendleft(ev)
                _broadcast(ev)
            proc.wait()
        except FileNotFoundError:
            print("ERROR: mosquitto_sub not found — install mosquitto-clients", file=sys.stderr)
            return
        except Exception as exc:
            print(f"mqtt reader error: {exc}", file=sys.stderr)
        time.sleep(5)


# ── HTTP handler ───────────────────────────────────────────────────────────────

class _Handler(BaseHTTPRequestHandler):

    def do_GET(self) -> None:
        if self.path in ("/", "/index.html"):
            self._serve_html()
        elif self.path == "/events":
            self._serve_sse()
        elif self.path == "/api/state":
            self._serve_json()
        else:
            self.send_error(404)

    # ── routes ──

    def _serve_html(self) -> None:
        body = _HTML.encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_json(self) -> None:
        with _state_lock:
            data = {"current": dict(_current), "history": list(_history)}
        body = json.dumps(data, separators=(",", ":")).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _serve_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        # send current snapshot immediately so page loads with data
        with _state_lock:
            if _current:
                snap = ("data: " + json.dumps(dict(_current), separators=(",", ":")) + "\n\n").encode()
                try:
                    self.wfile.write(snap)
                    self.wfile.flush()
                except OSError:
                    return

        q: queue.Queue = queue.Queue(maxsize=64)
        with _clients_lock:
            _clients.append(q)

        try:
            while True:
                try:
                    msg = q.get(timeout=HEARTBEAT_SEC)
                    self.wfile.write(msg)
                except queue.Empty:
                    self.wfile.write(b": heartbeat\n\n")
                self.wfile.flush()
        except OSError:
            pass
        finally:
            with _clients_lock:
                try:
                    _clients.remove(q)
                except ValueError:
                    pass

    def log_message(self, *_):  # suppress per-request stdout noise
        pass


# ── embedded HTML dashboard ────────────────────────────────────────────────────

_HTML = r"""<!DOCTYPE html>
<html lang="ko">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Conveyor Node Monitor</title>
<style>
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0f172a;color:#e2e8f0;font-family:'Courier New',monospace;min-height:100vh}

/* header */
header{background:#1e293b;padding:16px 24px;display:flex;align-items:center;gap:16px;border-bottom:1px solid #334155}
header h1{font-size:1.1rem;color:#94a3b8;letter-spacing:.05em;flex:1}
#conn-dot{width:10px;height:10px;border-radius:50%;background:#64748b;transition:background .3s}
#conn-dot.live{background:#22c55e;box-shadow:0 0 6px #22c55e}
#last-update{font-size:.75rem;color:#64748b}

/* state badge */
#state-banner{padding:12px 24px;font-size:1.5rem;font-weight:bold;letter-spacing:.1em;text-align:center;transition:background .4s,color .4s}
.s-ok      {background:#14532d;color:#86efac}
.s-blockage{background:#78350f;color:#fde68a}
.s-structural{background:#7c2d12;color:#fed7aa}
.s-critical{background:#7f1d1d;color:#fca5a5;animation:blink .6s step-start infinite}
.s-unknown {background:#1e293b;color:#64748b}
@keyframes blink{50%{opacity:.25}}

/* cards */
.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px;padding:16px 24px}
.card{background:#1e293b;border:1px solid #334155;border-radius:8px;padding:14px 16px}
.card-label{font-size:.7rem;color:#64748b;text-transform:uppercase;letter-spacing:.08em;margin-bottom:6px}
.card-value{font-size:1.4rem;font-weight:bold}
.cv-ok{color:#22c55e} .cv-warn{color:#f59e0b} .cv-err{color:#ef4444} .cv-neutral{color:#94a3b8}

/* stats row */
.stats-row{padding:0 24px 12px;display:flex;gap:24px;font-size:.75rem;color:#64748b}
.stats-row span b{color:#94a3b8}

/* table */
.table-wrap{padding:0 24px 24px;overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:.8rem}
thead th{background:#1e293b;color:#64748b;text-align:left;padding:8px 10px;border-bottom:2px solid #334155;white-space:nowrap}
tbody tr{border-bottom:1px solid #1e293b;transition:background .15s}
tbody tr:hover{background:#1e293b}
tbody td{padding:7px 10px;white-space:nowrap}
.row-ok{color:#86efac} .row-blockage{color:#fde68a} .row-structural{color:#fed7aa} .row-critical{color:#fca5a5}

/* reason badges */
.badge{display:inline-block;font-size:.65rem;background:#334155;color:#94a3b8;border-radius:4px;padding:1px 5px;margin:1px}
</style>
</head>
<body>

<header>
  <h1>&#x2699; CONVEYOR NODE MONITOR</h1>
  <div id="last-update">—</div>
  <div id="conn-dot" title="SSE connection"></div>
</header>

<div id="state-banner" class="s-unknown">— CONNECTING —</div>

<div class="cards">
  <div class="card">
    <div class="card-label">Tilt</div>
    <div class="card-value cv-neutral" id="cv-tilt">—</div>
  </div>
  <div class="card">
    <div class="card-label">Blockage</div>
    <div class="card-value cv-neutral" id="cv-blockage">—</div>
  </div>
  <div class="card">
    <div class="card-label">Distance</div>
    <div class="card-value cv-neutral" id="cv-dist">— <span style="font-size:.8rem">cm</span></div>
  </div>
  <div class="card">
    <div class="card-label">Light ADC</div>
    <div class="card-value cv-neutral" id="cv-light">—</div>
  </div>
  <div class="card">
    <div class="card-label">Node</div>
    <div class="card-value cv-neutral" style="font-size:1rem" id="cv-node">—</div>
  </div>
  <div class="card">
    <div class="card-label">Events (session)</div>
    <div class="card-value cv-neutral" id="cv-count">0</div>
  </div>
</div>

<div class="stats-row">
  <span>total received: <b id="st-total">0</b></span>
  <span>last reason: <span id="st-reason">—</span></span>
</div>

<div class="table-wrap">
<table>
  <thead>
    <tr>
      <th>Time</th>
      <th>State</th>
      <th>Tilt</th>
      <th>Blockage</th>
      <th>Dist cm</th>
      <th>Light</th>
      <th>Reason</th>
      <th>Node</th>
    </tr>
  </thead>
  <tbody id="tbody"></tbody>
</table>
</div>

<script>
const MAX_ROWS = 100;
let total = 0;

const STATE_CLASS = {
  RUNNING_OK:       'ok',
  BLOCKAGE_ALERT:   'blockage',
  STRUCTURAL_FAULT: 'structural',
  CRITICAL_FAULT:   'critical',
};
const REASON_BITS = [
  [0x01, 'TILT'],
  [0x02, 'BLOK'],
  [0x04, 'LITE'],
  [0x08, 'DIST'],
];

function decodeReason(r) {
  if (!r) return '<span class="badge">—</span>';
  return REASON_BITS
    .filter(([bit]) => r & bit)
    .map(([, label]) => `<span class="badge">${label}</span>`)
    .join('');
}

function stateClass(s) { return STATE_CLASS[s] || 'unknown'; }

function updateBanner(ev) {
  const el = document.getElementById('state-banner');
  const cls = stateClass(ev.state);
  el.className = `s-${cls}`;
  el.textContent = ev.state || '—';
}

function updateCards(ev) {
  const tiltOk = ev.tilt === 'LEVEL';
  const blkOk  = ev.blockage === 'CLEAR';

  const set = (id, val, cls) => {
    const el = document.getElementById(id);
    el.textContent = val;
    el.className = 'card-value ' + cls;
  };

  set('cv-tilt',    ev.tilt    || '—', tiltOk ? 'cv-ok' : 'cv-err');
  set('cv-blockage',ev.blockage|| '—', blkOk  ? 'cv-ok' : 'cv-warn');

  const dist = ev.distance_cm;
  const distCls = (dist > 0 && dist < 50) ? 'cv-warn' : 'cv-neutral';
  document.getElementById('cv-dist').innerHTML =
    `${dist ?? '—'} <span style="font-size:.8rem">cm</span>`;
  document.getElementById('cv-dist').className = 'card-value ' + distCls;

  document.getElementById('cv-light').textContent = ev.light ?? '—';
  document.getElementById('cv-light').className   = 'card-value cv-neutral';
  document.getElementById('cv-node').textContent  = ev.node_id || '—';
  document.getElementById('cv-count').textContent = total;

  document.getElementById('st-total').textContent  = total;
  document.getElementById('st-reason').innerHTML   = decodeReason(ev.reason);
  document.getElementById('last-update').textContent =
    ev._wall || new Date().toLocaleTimeString();
}

function addRow(ev) {
  const tbody = document.getElementById('tbody');
  const cls   = 'row-' + stateClass(ev.state).replace('ok','ok');
  const tr    = document.createElement('tr');
  tr.className = cls;
  tr.innerHTML = `
    <td>${ev._wall || '—'}</td>
    <td>${ev.state || '—'}</td>
    <td>${ev.tilt || '—'}</td>
    <td>${ev.blockage || '—'}</td>
    <td>${ev.distance_cm ?? '—'}</td>
    <td>${ev.light ?? '—'}</td>
    <td>${decodeReason(ev.reason)}</td>
    <td>${ev.node_id || '—'}</td>
  `;
  tbody.insertBefore(tr, tbody.firstChild);
  while (tbody.rows.length > MAX_ROWS) tbody.deleteRow(tbody.rows.length - 1);
}

function onEvent(ev) {
  total++;
  updateBanner(ev);
  updateCards(ev);
  addRow(ev);
}

// ── SSE ──
const src = new EventSource('/events');
src.onopen    = () => document.getElementById('conn-dot').classList.add('live');
src.onerror   = () => document.getElementById('conn-dot').classList.remove('live');
src.onmessage = e => { try { onEvent(JSON.parse(e.data)); } catch(_) {} };
</script>
</body>
</html>
"""


# ── entry point ────────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="Conveyor node web dashboard")
    parser.add_argument("--broker", default="localhost", help="MQTT broker host")
    parser.add_argument("--topic",  default="iot/conveyor/#")
    parser.add_argument("--port",   default=8080, type=int)
    args = parser.parse_args()

    t = threading.Thread(target=_mqtt_reader, args=(args.broker, args.topic), daemon=True)
    t.start()

    server = HTTPServer(("0.0.0.0", args.port), _Handler)
    print(f"conveyor web UI → http://0.0.0.0:{args.port}  (broker={args.broker})")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
