"""
Zephr - watch_progress.py
Monitor en vivo del avance de enrich_ai.py. Lee la DB directo (no depende de
logs, buffers, ni del MCP de Supabase — usa supabase-py con las credenciales
del .env).

Salidas:
  - Terminal: barras ASCII sobrescritas en sitio (sin parpadeo).
  - HTML: dashboard visual auto-refrescante (--html), pensado para abrir en
    el navegador y dejarlo de fondo mientras corre el enrich.

Uso:
    python watch_progress.py                    # solo terminal, refresca cada 5s
    python watch_progress.py --html              # + escribe outputs/progress.html
    python watch_progress.py --html --no-terminal
    python watch_progress.py --since 60          # ventana de actividad en minutos
    python watch_progress.py --once              # un solo snapshot
"""

import argparse
import os
import time
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

FIELDS = ["unsafe_condition", "corrective_action", "compliance_time", "published_date"]
SOURCES = [
    "FAA", "EASA", "Transport Canada", "ANAC Brasil",
    "DGAC Chile", "ANAC Argentina",
]

HTML_PATH_DEFAULT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs", "progress.html")


def get_client():
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("SUPABASE_URL y SUPABASE_SERVICE_KEY requeridos en .env")
    return create_client(url, key)


def _count(sb, *, source=None, field_notnull=None, updated_since=None):
    q = sb.table("ads").select("id", count="exact").limit(1)
    if source:
        q = q.eq("source_label", source)
    if field_notnull:
        q = q.not_.is_(field_notnull, "null")
    if updated_since:
        q = q.gt("last_updated_at", updated_since)
    return q.execute().count or 0


# ---------------------------------------------------------------------------
# Snapshot: una sola recoleccion de datos, usada por terminal Y html.
# ---------------------------------------------------------------------------
def gather(sb, since_iso, since_min):
    total = _count(sb)
    recent = _count(sb, updated_since=since_iso)
    fields = [(f, _count(sb, field_notnull=f)) for f in FIELDS]
    sources = []
    for s in SOURCES:
        st = _count(sb, source=s)
        if st == 0:
            continue
        sr = _count(sb, source=s, updated_since=since_iso)
        # Cobertura real (campos ya llenos), no timestamp: un repopulate/upsert
        # masivo toca last_updated_at en todas las filas de golpe y satura "sr",
        # haciendo parecer una fuente "completa" cuando el enrich apenas empieza.
        cov = _count(sb, source=s, field_notnull="unsafe_condition")
        sources.append({"name": s, "total": st, "recent": sr, "coverage": cov, "active": sr > 0})
    return {
        "total": total,
        "recent": recent,
        "since_min": since_min,
        "fields": fields,
        "sources": sources,
        "ts": datetime.now(timezone.utc),
    }


# ---------------------------------------------------------------------------
# Terminal (ASCII, sobrescrito en sitio)
# ---------------------------------------------------------------------------
def _bar_ascii(done, total, width=34):
    pct = (done / total) if total else 0
    filled = int(pct * width)
    return f"[{'#' * filled}{'.' * (width - filled)}] {pct*100:5.1f}%  {done:,}/{total:,}"


def _enable_ansi():
    if os.name == "nt":
        os.system("")  # habilita VT processing en Windows Terminal/conhost moderno


def build_frame(snap, prev_recent):
    L = []
    L.append("=" * 72)
    L.append(f"  ZEPHR - enriquecimiento por IA   (total ADs: {snap['total']:,})")
    L.append("=" * 72)
    rate = ""
    if prev_recent is not None and snap["recent"] > prev_recent:
        rate = f"   (+{snap['recent'] - prev_recent})"
    L.append(f"  >> PROCESADOS (ultimos {snap['since_min']} min): {snap['recent']:,}{rate}")
    L.append("-" * 72)
    L.append("  Cobertura global por campo:")
    for f, n in snap["fields"]:
        L.append(f"    {f:<20} {_bar_ascii(n, snap['total'])}")
    L.append("-" * 72)
    L.append("  Cobertura real por fuente (unsafe_condition lleno):")
    for s in snap["sources"]:
        mark = "  <== activo" if s["active"] else ""
        L.append(f"    {s['name']:<18} {_bar_ascii(s['coverage'], s['total'])}{mark}")
    L.append("-" * 72)
    L.append("  Ctrl+C para salir")
    return L


def draw(lines):
    """Reposiciona el cursor arriba y sobrescribe linea a linea (sin parpadeo)."""
    out = "\033[H" + "\n".join(line + "\033[K" for line in lines) + "\033[J"
    print(out, end="", flush=True)


# ---------------------------------------------------------------------------
# HTML (dashboard visual, auto-refrescante)
# ---------------------------------------------------------------------------
_HTML_TEMPLATE = """<!doctype html>
<html lang="es">
<head>
<meta charset="utf-8">
<meta http-equiv="refresh" content="{refresh}">
<title>Zephr - avance de enriquecimiento</title>
<style>
  :root {{
    --bg: #0a0a0a; --surface: #131313; --line: #262626;
    --text-1: #f5f5f5; --text-2: #a8a8a8; --text-3: #6b6b6b;
    --gold: #e8b84b; --green: #3fb977; --bar-bg: #1e1e1e;
  }}
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; padding: 32px; background: var(--bg); color: var(--text-1);
    font-family: -apple-system, "Segoe UI", Roboto, sans-serif;
  }}
  h1 {{ font-size: 18px; font-weight: 600; margin: 0 0 4px; }}
  .sub {{ color: var(--text-3); font-size: 12px; margin: 0 0 28px; }}
  .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(180px,1fr)); gap: 12px; margin-bottom: 28px; }}
  .card {{ background: var(--surface); border: 1px solid var(--line); border-radius: 10px; padding: 16px; }}
  .card .label {{ font-size: 11px; text-transform: uppercase; letter-spacing: .06em; color: var(--text-3); }}
  .card .value {{ font-size: 28px; font-weight: 600; margin-top: 6px; color: var(--gold); }}
  .card .value.big {{ color: var(--green); }}
  section {{ margin-bottom: 28px; }}
  section > h2 {{ font-size: 13px; font-weight: 600; color: var(--text-2); text-transform: uppercase; letter-spacing: .04em; margin: 0 0 14px; }}
  .row {{ display: flex; align-items: center; gap: 12px; margin-bottom: 10px; }}
  .row .name {{ width: 170px; flex-shrink: 0; font-size: 13px; color: var(--text-2); }}
  .row .name.active {{ color: var(--gold); font-weight: 600; }}
  .track {{ flex: 1; height: 18px; background: var(--bar-bg); border-radius: 5px; overflow: hidden; position: relative; }}
  .fill {{ height: 100%; background: linear-gradient(90deg, #b8862f, var(--gold)); border-radius: 5px; }}
  .row .pct {{ width: 130px; text-align: right; font-size: 12px; color: var(--text-3); font-variant-numeric: tabular-nums; }}
  .dot {{ display: inline-block; width: 7px; height: 7px; border-radius: 50%; background: var(--green); margin-right: 5px; box-shadow: 0 0 6px var(--green); }}
  footer {{ color: var(--text-3); font-size: 11px; margin-top: 8px; }}
</style>
</head>
<body>
  <h1>Zephr &mdash; enriquecimiento por IA</h1>
  <p class="sub">Actualizado {ts} UTC &middot; refresca cada {refresh}s</p>

  <div class="grid">
    <div class="card"><div class="label">Total ADs</div><div class="value">{total:,}</div></div>
    <div class="card"><div class="label">Procesados (&uacute;lt. {since_min} min)</div><div class="value big">{recent:,}</div></div>
    <div class="card"><div class="label">Fuentes activas</div><div class="value">{active_count}</div></div>
  </div>

  <section>
    <h2>Cobertura global por campo</h2>
    {field_rows}
  </section>

  <section>
    <h2>Cobertura real por fuente (unsafe_condition lleno) &middot; punto verde = actividad reciente</h2>
    {source_rows}
  </section>

  <footer>Ctrl+C en la terminal para detener el monitor &middot; el enrich sigue corriendo aparte</footer>
</body>
</html>
"""


def _field_row_html(label, done, total):
    pct = (done / total * 100) if total else 0
    return (
        f'<div class="row"><div class="name">{label}</div>'
        f'<div class="track"><div class="fill" style="width:{pct:.1f}%"></div></div>'
        f'<div class="pct">{pct:.1f}%&nbsp; {done:,}/{total:,}</div></div>'
    )


def _source_row_html(s):
    # Barra = cobertura real (unsafe_condition lleno), no timestamp reciente:
    # un repopulate/upsert masivo toca last_updated_at de golpe y saturaria la
    # barra aunque el enrich apenas vaya empezando.
    pct = (s["coverage"] / s["total"] * 100) if s["total"] else 0
    dot = '<span class="dot"></span>' if s["active"] else ""
    name_cls = "name active" if s["active"] else "name"
    return (
        f'<div class="row"><div class="{name_cls}">{dot}{s["name"]}</div>'
        f'<div class="track"><div class="fill" style="width:{min(pct,100):.1f}%"></div></div>'
        f'<div class="pct">{pct:.1f}%&nbsp; {s["coverage"]:,}/{s["total"]:,}</div></div>'
    )


def render_html(snap, refresh_seconds):
    field_rows = "\n    ".join(_field_row_html(f, n, snap["total"]) for f, n in snap["fields"])
    source_rows = "\n    ".join(_source_row_html(s) for s in snap["sources"]) or "<p style='color:var(--text-3)'>Sin actividad reciente en ninguna fuente.</p>"
    active_count = sum(1 for s in snap["sources"] if s["active"])
    return _HTML_TEMPLATE.format(
        refresh=refresh_seconds,
        ts=snap["ts"].strftime("%Y-%m-%d %H:%M:%S"),
        total=snap["total"],
        recent=snap["recent"],
        since_min=snap["since_min"],
        active_count=active_count,
        field_rows=field_rows,
        source_rows=source_rows,
    )


def write_html(snap, path, refresh_seconds):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(render_html(snap, refresh_seconds))
    os.replace(tmp, path)  # escritura atomica: el navegador nunca lee a medias


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=5)
    ap.add_argument("--since", type=int, default=180, help="ventana de actividad en minutos")
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--html", nargs="?", const=HTML_PATH_DEFAULT, default=None,
                    help=f"escribe un dashboard HTML auto-refrescante (default: {HTML_PATH_DEFAULT})")
    ap.add_argument("--no-terminal", action="store_true", help="no imprimir en terminal (solo --html)")
    args = ap.parse_args()
    sb = get_client()

    if not args.no_terminal:
        _enable_ansi()

    since_iso = (datetime.now(timezone.utc) - timedelta(minutes=args.since)).isoformat()
    prev = None
    first = True
    while True:
        try:
            snap = gather(sb, since_iso, args.since)
        except Exception as e:
            # Consultas de conteo pueden pegarle timeout si la DB esta ocupada
            # con el enrich escribiendo en paralelo. No morir por eso: loguear
            # y reintentar en el siguiente ciclo.
            print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}] snapshot fallo (reintento): {e}")
            if args.once:
                break
            time.sleep(args.interval)
            continue

        if not args.no_terminal:
            lines = build_frame(snap, prev)
            if first:
                print("\033[2J\033[H", end="")
                first = False
            draw(lines)

        if args.html:
            write_html(snap, args.html, refresh_seconds=args.interval)
            if args.no_terminal:
                print(f"[{snap['ts'].strftime('%H:%M:%S')}] {snap['recent']:,} procesados -> {args.html}")

        prev = snap["recent"]
        if args.once:
            if not args.no_terminal:
                print()
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
