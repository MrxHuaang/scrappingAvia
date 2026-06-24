"""
Zephr - watch_progress.py
Monitor en vivo del avance de enrich_ai.py. Lee la DB directo (no depende de
logs ni buffers).

Muestra:
  - ACTIVIDAD: cuantos ADs enriquecidos en la ventana reciente (last_updated_at).
    Este es el contador de "cuantos lleva" la corrida actual.
  - Cobertura global por campo (barras).
  - Desglose por fuente (procesados recientes / total de la fuente).

Uso:
    python watch_progress.py                 # refresca cada 5s, ventana 180 min
    python watch_progress.py --since 60      # cuenta actividad de los ultimos 60 min
    python watch_progress.py --once          # un solo snapshot
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


def get_client():
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("SUPABASE_URL y SUPABASE_SERVICE_KEY requeridos en .env")
    return create_client(url, key)


def count(sb, *, source=None, field_notnull=None, updated_since=None):
    q = sb.table("ads").select("id", count="exact").limit(1)
    if source:
        q = q.eq("source_label", source)
    if field_notnull:
        q = q.not_.is_(field_notnull, "null")
    if updated_since:
        q = q.gt("last_updated_at", updated_since)
    return q.execute().count or 0


def bar(done, total, width=34):
    pct = (done / total) if total else 0
    filled = int(pct * width)
    return f"[{'#' * filled}{'.' * (width - filled)}] {pct*100:5.1f}%  {done:,}/{total:,}"


def _enable_ansi():
    """Activa secuencias ANSI en consola Windows (no-op en Unix)."""
    if os.name == "nt":
        os.system("")  # habilita VT processing en Windows Terminal/conhost moderno


def build_frame(sb, since_iso, since_min, prev_recent):
    total = count(sb)
    recent = count(sb, updated_since=since_iso)
    L = []
    L.append("=" * 72)
    L.append(f"  ZEPHR - enriquecimiento por IA   (total ADs: {total:,})")
    L.append("=" * 72)
    rate = ""
    if prev_recent is not None and recent > prev_recent:
        rate = f"   (+{recent - prev_recent})"
    L.append(f"  >> PROCESADOS (ultimos {since_min} min): {recent:,}{rate}")
    L.append("-" * 72)
    L.append("  Cobertura global por campo:")
    for f in FIELDS:
        n = count(sb, field_notnull=f)
        L.append(f"    {f:<20} {bar(n, total)}")
    L.append("-" * 72)
    L.append("  Por fuente (procesados recientes / total fuente):")
    for s in SOURCES:
        st = count(sb, source=s)
        if st == 0:
            continue
        sr = count(sb, source=s, updated_since=since_iso)
        mark = "  <== activo" if sr else ""
        L.append(f"    {s:<18} {sr:>4} recientes / {st:>6} total{mark}")
    L.append("-" * 72)
    L.append("  Ctrl+C para salir")
    return L, recent


def draw(lines):
    """Reposiciona el cursor arriba y sobrescribe linea a linea (sin parpadeo)."""
    # \033[H = cursor al inicio; \033[K = borra resto de la linea; \033[J = borra abajo
    out = "\033[H" + "\n".join(line + "\033[K" for line in lines) + "\033[J"
    print(out, end="", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--interval", type=int, default=5)
    ap.add_argument("--since", type=int, default=180, help="ventana de actividad en minutos")
    ap.add_argument("--once", action="store_true")
    args = ap.parse_args()
    sb = get_client()
    _enable_ansi()

    since_iso = (datetime.now(timezone.utc) - timedelta(minutes=args.since)).isoformat()
    prev = None
    first = True
    while True:
        lines, prev = build_frame(sb, since_iso, args.since, prev)
        if first:
            print("\033[2J\033[H", end="")  # limpia una sola vez al arrancar
            first = False
        draw(lines)
        if args.once:
            print()
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
