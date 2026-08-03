"""
Zephr - dashboard_server.py
Panel de control local para el enriquecimiento por IA. Reemplaza el archivo
HTML estatico (watch_progress.py --html) por un servidor con botones reales:

  - Encender/apagar el enrich de una autoridad especifica con un click.
  - Un solo trabajo a la vez (la GPU/Ollama es un recurso compartido — correr
    dos en paralelo solo los hace mas lentos a ambos, no los acelera).
  - Estado en tiempo real via polling cada 2s (sin recargar la pagina).
  - Notificaciones (toast en pantalla + notificacion del navegador opcional)
    cuando una autoridad se enciende o termina.
  - Cola de historial de las ultimas corridas (completado / detenido / error).
  - Cola de texto en vivo (ultimas lineas del log) de la autoridad activa.

Uso:
    python dashboard_server.py
    -> abre http://127.0.0.1:8787
"""

import os
import re
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

from flask import Flask, Response, jsonify, request

import enrich_ai
import watch_progress as wp

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(BASE_DIR, "outputs")
os.makedirs(OUT_DIR, exist_ok=True)

SINCE_MIN = 60  # ventana para el contador "procesados recientemente" (informativo)
SNAPSHOT_INTERVAL = 5  # segundos entre refrescos de la DB (desacoplado del polling del navegador)

app = Flask(__name__)

# ---------------------------------------------------------------------------
# Estado del trabajo activo (un solo proceso enrich_ai.py a la vez)
# ---------------------------------------------------------------------------
JOB_LOCK = threading.Lock()
JOB = {"proc": None, "label": None, "mode": None, "started_at": None,
       "log_path": None, "log_file": None, "total_flagged": None}
HISTORY = []  # [{label, mode, started_at, ended_at, ok, stopped}]
HISTORY_MAX = 12


def _slug(name: str) -> str:
    return name.lower().replace(" ", "_")


def start_job(source: str):
    with JOB_LOCK:
        if JOB["proc"] is not None and JOB["proc"].poll() is None:
            return False, f"Ya hay un trabajo activo: {JOB['label']}"
        log_path = os.path.join(OUT_DIR, f"dashboard_{_slug(source)}.log")
        log_file = open(log_path, "a", encoding="utf-8")
        cmd = [sys.executable, "-u", "enrich_ai.py", "--model", "qwen-ad", "--source", source]
        proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT, cwd=BASE_DIR)
        JOB.update(proc=proc, label=source, mode="source", started_at=datetime.now(timezone.utc),
                   log_path=log_path, log_file=log_file, total_flagged=None)
        return True, "ok"


def _count_truncated_rows(sb):
    """Cuenta filas (no marcas por campo) con al menos un texto cortado —
    unidad real de trabajo del modo --fix-truncated, que reprocesa por fila."""
    fields = ("unsafe_condition", "corrective_action", "compliance_time")
    total_bad, offset, page = 0, 0, 1000
    while True:
        rows = sb.table("ads").select(",".join(fields)).range(offset, offset + page - 1).execute().data or []
        if not rows:
            break
        for r in rows:
            if any(enrich_ai._looks_truncated(r.get(f)) for f in fields):
                total_bad += 1
        if len(rows) < page:
            break
        offset += page
    return total_bad


def start_repair(sb):
    with JOB_LOCK:
        if JOB["proc"] is not None and JOB["proc"].poll() is None:
            return False, f"Ya hay un trabajo activo: {JOB['label']}"
        total_flagged = None
    # Fuera del lock: el conteo recorre toda la tabla (~15s) y no debe
    # bloquear otras consultas de estado mientras tanto.
    try:
        total_flagged = _count_truncated_rows(sb)
    except Exception:
        total_flagged = None
    with JOB_LOCK:
        if JOB["proc"] is not None and JOB["proc"].poll() is None:
            return False, f"Ya hay un trabajo activo: {JOB['label']}"
        log_path = os.path.join(OUT_DIR, "dashboard_repair.log")
        # modo "w": arranca el log limpio para que el conteo de reparados de
        # esta sesion coincida con total_flagged (recien calculado).
        log_file = open(log_path, "w", encoding="utf-8")
        cmd = [sys.executable, "-u", "enrich_ai.py", "--model", "qwen-ad", "--fix-truncated"]
        proc = subprocess.Popen(cmd, stdout=log_file, stderr=subprocess.STDOUT, cwd=BASE_DIR)
        JOB.update(proc=proc, label="Reparacion de textos cortados", mode="repair",
                   started_at=datetime.now(timezone.utc), log_path=log_path,
                   log_file=log_file, total_flagged=total_flagged)
        return True, "ok"


def _finish_job_locked(ok: bool, stopped: bool = False):
    """Debe llamarse con JOB_LOCK ya tomado."""
    if JOB["proc"] is None:
        return
    HISTORY.append({
        "label": JOB["label"],
        "mode": JOB["mode"],
        "started_at": JOB["started_at"].isoformat() if JOB["started_at"] else None,
        "ended_at": datetime.now(timezone.utc).isoformat(),
        "ok": ok,
        "stopped": stopped,
    })
    del HISTORY[:-HISTORY_MAX]
    try:
        JOB["log_file"].close()
    except Exception:
        pass
    JOB.update(proc=None, label=None, mode=None, started_at=None,
               log_path=None, log_file=None, total_flagged=None)


def stop_job():
    with JOB_LOCK:
        if JOB["proc"] is None or JOB["proc"].poll() is not None:
            return False, "No hay trabajo activo"
        JOB["proc"].terminate()
        try:
            JOB["proc"].wait(timeout=5)
        except subprocess.TimeoutExpired:
            JOB["proc"].kill()
        _finish_job_locked(ok=False, stopped=True)
        return True, "detenido"


def _watcher():
    """Detecta cuando el proceso activo termina solo (no via boton stop)."""
    while True:
        time.sleep(2)
        with JOB_LOCK:
            proc = JOB.get("proc")
            if proc is not None and proc.poll() is not None:
                _finish_job_locked(ok=(proc.returncode == 0))


# ---------------------------------------------------------------------------
# Parseo del log de reparacion -> eventos estructurados por AD (para la lista
# interactiva "que hizo") + heartbeat de actividad (para avisar si se colgo).
# ---------------------------------------------------------------------------
_REPAIR_FIXED_RE = re.compile(r'^\d\d:\d\d:\d\d \[INFO\] ([^:]+): reparado \[(.*?)\]$')
_REPAIR_SKIP_RE = re.compile(
    r'^\d\d:\d\d:\d\d \[INFO\] ([^:]+): (cortado pero sin texto de PDF disponible -> skip'
    r'|seguia cortado tras reintentar -> skip)$'
)
_REPAIR_FAIL_RE = re.compile(r'^\d\d:\d\d:\d\d \[WARNING\] ([^:]+): update fallo: (.+)$')


def parse_repair_log(path, max_events=40):
    """Lee el log completo del modo --fix-truncated y devuelve eventos
    estructurados (mas reciente primero) + conteos totales de la sesion."""
    events = []
    counts = {"fixed": 0, "skip": 0, "fail": 0}
    if not path or not os.path.exists(path):
        return events, counts
    try:
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            for line in f:
                line = line.rstrip("\n")
                m = _REPAIR_FIXED_RE.match(line)
                if m:
                    counts["fixed"] += 1
                    fields = [x.strip(" '\"") for x in m.group(2).split(",")]
                    fields = [x for x in fields if x and x != "last_updated_at"]
                    events.append({"ad_number": m.group(1), "status": "reparado", "fields": fields})
                    continue
                m = _REPAIR_SKIP_RE.match(line)
                if m:
                    counts["skip"] += 1
                    events.append({"ad_number": m.group(1), "status": "sin_arreglo", "fields": []})
                    continue
                m = _REPAIR_FAIL_RE.match(line)
                if m:
                    counts["fail"] += 1
                    events.append({"ad_number": m.group(1), "status": "error", "fields": []})
    except Exception:
        pass
    return list(reversed(events[-max_events:])), counts


def tail_log(path, n=14):
    if not path or not os.path.exists(path):
        return []
    try:
        size = os.path.getsize(path)
        block = 8192
        data = ""
        with open(path, "r", encoding="utf-8", errors="ignore") as f:
            while True:
                seek_pos = max(0, size - block)
                f.seek(seek_pos)
                data = f.read()
                if seek_pos == 0 or data.count("\n") > n:
                    break
                block *= 2
        lines = [ln for ln in data.splitlines() if "HTTP Request" not in ln]
        return lines[-n:]
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Snapshot de la DB, refrescado en background (desacoplado de las requests)
# ---------------------------------------------------------------------------
SNAPSHOT_LOCK = threading.Lock()
SNAPSHOT = {"data": None, "error": None, "updated_at": None}


def _snapshot_loop(sb):
    while True:
        try:
            since_iso = (datetime.now(timezone.utc) - timedelta(minutes=SINCE_MIN)).isoformat()
            snap = wp.gather(sb, since_iso, SINCE_MIN)
            with SNAPSHOT_LOCK:
                SNAPSHOT["data"] = snap
                SNAPSHOT["error"] = None
                SNAPSHOT["updated_at"] = datetime.now(timezone.utc)
        except Exception as e:
            with SNAPSHOT_LOCK:
                SNAPSHOT["error"] = str(e)
        time.sleep(SNAPSHOT_INTERVAL)


def serialize_snapshot(snap):
    if snap is None:
        return None
    total = snap["total"]
    return {
        "total": total,
        "recent": snap["recent"],
        "since_min": snap["since_min"],
        "fields": [
            {"name": f, "count": n, "pct": round(n / total * 100, 1) if total else 0}
            for f, n in snap["fields"]
        ],
        "sources": [
            {
                "name": s["name"],
                "total": s["total"],
                "coverage": s["coverage"],
                "pct": round(s["coverage"] / s["total"] * 100, 1) if s["total"] else 0,
            }
            for s in snap["sources"]
        ],
        "ts": snap["ts"].isoformat(),
    }


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------
@app.route("/")
def index():
    return Response(INDEX_HTML, mimetype="text/html")


@app.route("/api/status")
def api_status():
    with SNAPSHOT_LOCK:
        snap = SNAPSHOT["data"]
        err = SNAPSHOT["error"]
        updated_at = SNAPSHOT["updated_at"]

    with JOB_LOCK:
        proc = JOB["proc"]
        active = proc is not None and proc.poll() is None
        job_info = None
        if active:
            log_path = JOB["log_path"]
            heartbeat_seconds = None
            if log_path and os.path.exists(log_path):
                heartbeat_seconds = round(time.time() - os.path.getmtime(log_path), 1)
            job_info = {
                "label": JOB["label"],
                "mode": JOB["mode"],
                "started_at": JOB["started_at"].isoformat(),
                "log_tail": tail_log(log_path),
                "heartbeat_seconds": heartbeat_seconds,
            }
            if JOB["mode"] == "repair":
                events, counts = parse_repair_log(log_path)
                job_info["repair_events"] = events
                job_info["repair_counts"] = counts
                job_info["total_flagged"] = JOB["total_flagged"]
        hist = list(HISTORY[-6:])

    return jsonify({
        "snapshot": serialize_snapshot(snap),
        "snapshot_error": err,
        "snapshot_updated_at": updated_at.isoformat() if updated_at else None,
        "job": job_info,
        "history": list(reversed(hist)),
        "sources_list": wp.SOURCES,
    })


@app.route("/api/start", methods=["POST"])
def api_start():
    data = request.get_json(force=True, silent=True) or {}
    source = (data.get("source") or "").strip()
    if source not in wp.SOURCES:
        return jsonify({"ok": False, "error": "Fuente invalida"}), 400
    ok, msg = start_job(source)
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/stop", methods=["POST"])
def api_stop():
    ok, msg = stop_job()
    return jsonify({"ok": ok, "message": msg})


@app.route("/api/repair/start", methods=["POST"])
def api_repair_start():
    sb = wp.get_client()
    ok, msg = start_repair(sb)
    return jsonify({"ok": ok, "message": msg})


# ---------------------------------------------------------------------------
# Front-end (auto-contenido, sin build step)
# ---------------------------------------------------------------------------
INDEX_HTML = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<title>Zephr - panel de enriquecimiento</title>
<style>
  :root {
    --bg: #0a0a0a; --surface: #131313; --surface-2: #1a1a1a; --line: #262626;
    --text-1: #f5f5f5; --text-2: #a8a8a8; --text-3: #6b6b6b;
    --gold: #e8b84b; --green: #3fb977; --red: #e0554f; --bar-bg: #1e1e1e;
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; padding: 32px; background: var(--bg); color: var(--text-1);
    font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
  }
  h1 { font-size: 19px; font-weight: 600; margin: 0 0 4px; }
  .sub { color: var(--text-3); font-size: 12px; margin: 0 0 24px; }
  .grid-top { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px,1fr)); gap: 12px; margin-bottom: 24px; }
  .card { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 16px; }
  .card .label { font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--text-3); }
  .card .value { font-size: 26px; font-weight: 600; margin-top: 6px; color: var(--gold); }
  .card .value.active-color { color: var(--green); }

  #active-banner {
    display: none; background: var(--surface-2); border: 1px solid var(--green);
    border-radius: 12px; padding: 18px 20px; margin-bottom: 24px;
  }
  #active-banner .row1 { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  #active-banner .title { font-size: 15px; font-weight: 600; display: flex; align-items: center; gap: 8px; }
  #active-banner .elapsed { font-size: 13px; color: var(--text-2); font-variant-numeric: tabular-nums; }
  #active-banner .heartbeat { font-size: 11.5px; margin-top: 6px; display: flex; align-items: center; gap: 6px; }
  #log-tail {
    margin-top: 12px; background: #000; border-radius: 8px; padding: 10px 12px;
    font-family: "SF Mono", Consolas, monospace; font-size: 11.5px; color: var(--text-2);
    max-height: 160px; overflow-y: auto; white-space: pre-wrap; line-height: 1.5;
  }

  #repair-card { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 16px 18px; margin-bottom: 28px; }
  #repair-card.active { border-color: var(--gold); }
  .repair-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 10px; flex-wrap: wrap; gap: 10px; }
  .repair-title { font-size: 14px; font-weight: 600; }
  .repair-stats { display: flex; gap: 18px; font-size: 12px; color: var(--text-2); margin-bottom: 10px; }
  .repair-stats b { color: var(--text-1); font-variant-numeric: tabular-nums; }
  .repair-btn { padding: 7px 16px; border-radius: 7px; border: 1px solid var(--gold); background: rgba(232,184,75,.12); color: var(--gold); font-size: 12.5px; font-weight: 600; cursor: pointer; }
  .repair-btn:hover:not(:disabled) { background: rgba(232,184,75,.2); }
  .repair-btn:disabled { opacity: .5; cursor: default; }
  #repair-events { max-height: 260px; overflow-y: auto; border-top: 1px solid var(--line); margin-top: 10px; }
  .repair-row { display: flex; align-items: center; gap: 10px; padding: 6px 0; border-bottom: 1px solid var(--line); font-size: 12px; }
  .repair-row:last-child { border-bottom: none; }
  .repair-ad { font-family: "SF Mono", Consolas, monospace; color: var(--text-1); width: 130px; flex-shrink: 0; }
  .repair-fields { flex: 1; color: var(--text-3); }
  .repair-badge { font-size: 10.5px; font-weight: 600; padding: 2px 7px; border-radius: 5px; flex-shrink: 0; }
  .repair-badge.reparado { background: rgba(63,185,119,.15); color: var(--green); }
  .repair-badge.sin_arreglo { background: rgba(168,168,168,.15); color: var(--text-2); }
  .repair-badge.error { background: rgba(224,85,79,.15); color: var(--red); }

  section { margin-bottom: 28px; }
  section > h2 { font-size: 13px; font-weight: 600; color: var(--text-2); text-transform: uppercase; letter-spacing: .04em; margin: 0 0 14px; }

  #sources-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px,1fr)); gap: 12px; }
  .source-card { background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 14px 16px; }
  .source-card.active { border-color: var(--green); }
  .source-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 8px; }
  .source-name { font-size: 13.5px; font-weight: 600; display: flex; align-items: center; gap: 6px; }
  .source-pct { font-size: 12px; color: var(--text-3); font-variant-numeric: tabular-nums; }
  .track { height: 8px; background: var(--bar-bg); border-radius: 4px; overflow: hidden; margin-bottom: 8px; }
  .fill { height: 100%; background: linear-gradient(90deg, #b8862f, var(--gold)); border-radius: 4px; transition: width .4s ease; }
  .source-meta { font-size: 11.5px; color: var(--text-3); margin-bottom: 10px; }
  .btn {
    width: 100%; padding: 7px 0; border-radius: 7px; border: 1px solid var(--line);
    background: var(--surface-2); color: var(--text-1); font-size: 12.5px; font-weight: 600;
    cursor: pointer; transition: background .15s;
  }
  .btn:hover:not(:disabled) { background: #232323; }
  .btn:disabled { color: var(--text-3); cursor: default; opacity: .6; }
  .btn-stop { background: rgba(224,85,79,.12); border-color: var(--red); color: var(--red); }
  .btn-stop:hover { background: rgba(224,85,79,.2); }

  .dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: var(--green); box-shadow: 0 0 6px var(--green); }
  .dot.pulsing { animation: pulse 1.4s infinite; }
  @keyframes pulse { 0%,100% { opacity: 1; } 50% { opacity: .35; } }

  #history-list .hist-row { display: flex; align-items: center; gap: 12px; padding: 7px 0; border-bottom: 1px solid var(--line); font-size: 12.5px; }
  #history-list .hist-row:last-child { border-bottom: none; }
  .hist-source { flex: 1; color: var(--text-2); }
  .hist-label { font-size: 11px; font-weight: 600; padding: 2px 8px; border-radius: 5px; }
  .hist-completado { background: rgba(63,185,119,.15); color: var(--green); }
  .hist-detenido { background: rgba(168,168,168,.15); color: var(--text-2); }
  .hist-error { background: rgba(224,85,79,.15); color: var(--red); }
  .hist-dur { color: var(--text-3); font-variant-numeric: tabular-nums; width: 70px; text-align: right; }
  .muted { color: var(--text-3); font-size: 12.5px; }

  #notif-btn { font-size: 11.5px; padding: 5px 12px; border-radius: 6px; border: 1px solid var(--line); background: var(--surface); color: var(--text-2); cursor: pointer; }

  #toasts { position: fixed; top: 20px; right: 20px; display: flex; flex-direction: column; gap: 8px; z-index: 999; }
  .toast {
    background: var(--surface-2); border: 1px solid var(--line); border-radius: 8px;
    padding: 10px 16px; font-size: 13px; opacity: 0; transform: translateX(20px);
    transition: opacity .25s, transform .25s; box-shadow: 0 4px 16px rgba(0,0,0,.4);
  }
  .toast.show { opacity: 1; transform: translateX(0); }
  .toast-success { border-color: var(--green); }
  .toast-error { border-color: var(--red); }
</style>
</head>
<body>
  <div style="display:flex; align-items:flex-start; justify-content:space-between;">
    <div>
      <h1>Zephr &mdash; panel de enriquecimiento por IA</h1>
      <p class="sub" id="last-update">conectando...</p>
    </div>
    <button id="notif-btn" onclick="requestNotifPermission()">Activar notificaciones</button>
  </div>

  <div class="grid-top">
    <div class="card"><div class="label">Total ADs</div><div class="value" id="total-ads">-</div></div>
    <div class="card"><div class="label">Procesados (&uacute;lt. 60 min)</div><div class="value" id="recent-count">-</div></div>
    <div class="card"><div class="label">Motor</div><div class="value" id="engine-state">Inactivo</div></div>
  </div>

  <div id="active-banner">
    <div class="row1">
      <div class="title"><span class="dot pulsing"></span> <span id="active-source-name"></span></div>
      <div class="elapsed" id="active-elapsed">00:00</div>
    </div>
    <div class="heartbeat" id="heartbeat"></div>
    <div id="log-tail"></div>
  </div>

  <div id="repair-card">
    <div class="repair-head">
      <div class="repair-title">Reparacion de textos cortados</div>
      <button class="repair-btn" id="repair-btn" onclick="startRepair()">Iniciar reparacion</button>
    </div>
    <div class="repair-stats" id="repair-stats"></div>
    <div id="repair-events"></div>
  </div>

  <section>
    <h2>Autoridades</h2>
    <div id="sources-grid"></div>
  </section>

  <section>
    <h2>Historial reciente</h2>
    <div id="history-list"><p class="muted">Cargando...</p></div>
  </section>

  <div id="toasts"></div>

<script>
let prevActive = null;
let notifPermission = 'default';
let stallWarned = false;

function toast(msg, type) {
  const el = document.createElement('div');
  el.className = 'toast' + (type ? ' toast-' + type : '');
  el.textContent = msg;
  document.getElementById('toasts').appendChild(el);
  requestAnimationFrame(() => el.classList.add('show'));
  setTimeout(() => { el.classList.remove('show'); setTimeout(() => el.remove(), 300); }, 5000);
}

function notify(title, body) {
  if (notifPermission === 'granted') {
    try { new Notification(title, { body: body }); } catch (e) {}
  }
}

async function requestNotifPermission() {
  if (!('Notification' in window)) { toast('Este navegador no soporta notificaciones', 'error'); return; }
  const p = await Notification.requestPermission();
  notifPermission = p;
  document.getElementById('notif-btn').textContent = p === 'granted' ? 'Notificaciones activas' : 'Activar notificaciones';
}

async function startSource(name) {
  const res = await fetch('/api/start', { method: 'POST', headers: {'Content-Type':'application/json'}, body: JSON.stringify({source: name}) });
  const data = await res.json();
  if (!data.ok) toast(data.message || 'No se pudo iniciar', 'error');
}

async function stopJob() {
  const res = await fetch('/api/stop', { method: 'POST' });
  const data = await res.json();
  if (!data.ok) toast(data.message || 'No se pudo detener', 'error');
}

async function startRepair() {
  const btn = document.getElementById('repair-btn');
  btn.disabled = true;
  btn.textContent = 'Calculando alcance...';
  try {
    const res = await fetch('/api/repair/start', { method: 'POST' });
    const data = await res.json();
    if (!data.ok) toast(data.message || 'No se pudo iniciar', 'error');
  } finally {
    btn.disabled = false;
  }
}

function fmtElapsed(startedIso) {
  const s = Math.max(0, Math.floor((Date.now() - new Date(startedIso).getTime()) / 1000));
  const m = Math.floor(s / 60), sec = s % 60;
  return String(m).padStart(2,'0') + ':' + String(sec).padStart(2,'0');
}

function render(data) {
  const snap = data.snapshot;
  const job = data.job;

  document.getElementById('last-update').textContent = snap ? ('actualizado ' + new Date(snap.ts).toLocaleTimeString()) : (data.snapshot_error ? 'error leyendo la base: ' + data.snapshot_error : 'sin datos aun');

  if (snap) {
    document.getElementById('total-ads').textContent = snap.total.toLocaleString();
    document.getElementById('recent-count').textContent = snap.recent.toLocaleString();
  }
  const engineEl = document.getElementById('engine-state');
  engineEl.textContent = job ? 'Activo' : 'Inactivo';
  engineEl.className = 'value' + (job ? ' active-color' : '');

  const banner = document.getElementById('active-banner');
  if (job) {
    banner.style.display = '';
    document.getElementById('active-source-name').textContent = job.label;
    document.getElementById('active-elapsed').textContent = fmtElapsed(job.started_at);
    const logEl = document.getElementById('log-tail');
    logEl.textContent = (job.log_tail || []).join('\\n');
    logEl.scrollTop = logEl.scrollHeight;

    const hb = document.getElementById('heartbeat');
    const secs = job.heartbeat_seconds;
    if (secs === null || secs === undefined) {
      hb.innerHTML = '';
    } else if (secs < 30) {
      hb.innerHTML = '<span class="dot"></span> vivo — actividad hace ' + secs.toFixed(0) + 's';
      hb.style.color = 'var(--green)';
      stallWarned = false;
    } else if (secs < 90) {
      hb.innerHTML = '&#9679; ralentizado — sin actividad hace ' + secs.toFixed(0) + 's';
      hb.style.color = 'var(--gold)';
    } else {
      hb.innerHTML = '&#9679; posible cuelgue — sin actividad hace ' + Math.round(secs) + 's. Revisa si el proceso sigue vivo.';
      hb.style.color = 'var(--red)';
      if (!stallWarned) {
        stallWarned = true;
        toast('Sin actividad hace ' + Math.round(secs) + 's en "' + job.label + '" — puede haberse colgado', 'error');
        notify('Zephr - posible cuelgue', job.label + ' no muestra actividad hace ' + Math.round(secs) + 's');
      }
    }
  } else {
    banner.style.display = 'none';
    stallWarned = false;
  }

  const nowActive = job ? job.label : null;
  if (prevActive !== null && nowActive !== prevActive) {
    if (nowActive) {
      toast('Se encendio: ' + nowActive, 'success');
      notify('Zephr', 'Se encendio: ' + nowActive);
    } else if (prevActive) {
      toast(prevActive + ' termino', 'success');
      notify('Zephr', prevActive + ' termino de procesarse');
    }
  }
  prevActive = nowActive;

  // --- panel de reparacion ---
  const repairActive = job && job.mode === 'repair';
  const repairCard = document.getElementById('repair-card');
  const repairBtn = document.getElementById('repair-btn');
  repairCard.className = repairActive ? 'active' : '';
  if (repairActive) {
    repairBtn.textContent = 'Detener';
    repairBtn.onclick = stopJob;
    repairBtn.disabled = false;
  } else {
    repairBtn.textContent = job ? 'Ocupado' : 'Iniciar reparacion';
    repairBtn.onclick = startRepair;
    repairBtn.disabled = !!job;
  }
  const statsEl = document.getElementById('repair-stats');
  const eventsEl = document.getElementById('repair-events');
  if (repairActive) {
    const c = job.repair_counts || {fixed:0, skip:0, fail:0};
    const total = job.total_flagged;
    const pct = total ? Math.min(100, (c.fixed / total * 100)) : null;
    statsEl.innerHTML =
      '<span>Reparados: <b>' + c.fixed.toLocaleString() + (total ? ' / ' + total.toLocaleString() : '') + '</b></span>' +
      '<span>Sin arreglo: <b>' + c.skip.toLocaleString() + '</b></span>' +
      '<span>Errores: <b>' + c.fail.toLocaleString() + '</b></span>' +
      (pct !== null ? '<span>Avance: <b>' + pct.toFixed(1) + '%</b></span>' : '');
    const events = job.repair_events || [];
    eventsEl.innerHTML = events.length ? events.map(e => {
      const fieldsTxt = e.fields && e.fields.length ? e.fields.join(', ') : (e.status === 'sin_arreglo' ? 'seguia incompleto, se dejo para otro intento' : (e.status === 'error' ? 'fallo al guardar' : '—'));
      return '<div class="repair-row"><span class="repair-ad">' + e.ad_number + '</span>' +
             '<span class="repair-fields">' + fieldsTxt + '</span>' +
             '<span class="repair-badge ' + e.status + '">' + e.status.replace('_',' ') + '</span></div>';
    }).join('') : '<p class="muted" style="padding:10px 0;">Arrancando...</p>';
  } else {
    statsEl.innerHTML = '<span class="muted">No hay reparacion corriendo. Este modo re-extrae solo las filas cuyo texto quedo cortado a mitad de palabra por un bug ya corregido — no toca el resto de la base.</span>';
    eventsEl.innerHTML = '';
  }

  if (snap) {
    const sorted = [...snap.sources].sort((a,b) => a.total - b.total);
    const grid = document.getElementById('sources-grid');
    grid.innerHTML = '';
    for (const s of sorted) {
      const isActive = job && job.mode === 'source' && job.label === s.name;
      const disabled = !!job && !isActive;
      const card = document.createElement('div');
      card.className = 'source-card' + (isActive ? ' active' : '');
      const dot = isActive ? '<span class="dot pulsing"></span> ' : '';
      const btn = isActive
        ? '<button class="btn btn-stop" onclick="stopJob()">Detener</button>'
        : '<button class="btn" ' + (disabled ? 'disabled' : '') + ' onclick="startSource(\\'' + s.name + '\\')">' + (disabled ? 'Ocupado' : 'Encender') + '</button>';
      card.innerHTML =
        '<div class="source-head"><span class="source-name">' + dot + s.name + '</span><span class="source-pct">' + s.pct.toFixed(1) + '%</span></div>' +
        '<div class="track"><div class="fill" style="width:' + Math.min(s.pct,100) + '%"></div></div>' +
        '<div class="source-meta">' + s.coverage.toLocaleString() + ' / ' + s.total.toLocaleString() + '</div>' +
        btn;
      grid.appendChild(card);
    }
  }

  const hist = data.history || [];
  const histEl = document.getElementById('history-list');
  if (hist.length) {
    histEl.innerHTML = hist.map(h => {
      const dur = Math.max(0, Math.round((new Date(h.ended_at) - new Date(h.started_at)) / 1000));
      const mm = Math.floor(dur / 60), ss = dur % 60;
      const label = h.stopped ? 'detenido' : (h.ok ? 'completado' : 'error');
      return '<div class="hist-row"><span class="hist-source">' + h.label + '</span>' +
             '<span class="hist-label hist-' + label + '">' + label + '</span>' +
             '<span class="hist-dur">' + mm + 'm ' + ss + 's</span></div>';
    }).join('');
  } else {
    histEl.innerHTML = '<p class="muted">Sin corridas completadas todavia.</p>';
  }
}

async function poll() {
  try {
    const res = await fetch('/api/status');
    const data = await res.json();
    render(data);
  } catch (e) { /* red caida momentaneamente, se reintenta en el siguiente tick */ }
}

poll();
setInterval(poll, 2000);
</script>
</body>
</html>
"""


if __name__ == "__main__":
    sb = wp.get_client()
    threading.Thread(target=_snapshot_loop, args=(sb,), daemon=True).start()
    threading.Thread(target=_watcher, daemon=True).start()
    print("Dashboard: http://127.0.0.1:8787")
    app.run(host="127.0.0.1", port=8787, debug=False, use_reloader=False)
