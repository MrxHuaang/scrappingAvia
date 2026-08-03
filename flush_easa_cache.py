"""
Vuelca los campos profundos cacheados de EASA (detail + pdf) a la tabla ads via
UPDATE fila-por-fila (commit inmediato => reanudable si se interrumpe).
No hace fetching: solo lee los JSON de cache y escribe a DB.
"""
import json
import os
from dotenv import load_dotenv
from supabase import create_client

from utils import normalize_ad_number

load_dotenv()
sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

detail = json.load(open("outputs/easa_detail_cache.json", encoding="utf-8"))
pdf = json.load(open("outputs/easa_pdf_cache.json", encoding="utf-8"))

keys = set(detail) | set(pdf)
print(f"flush EASA: {len(keys)} ADs en cache")

# Resumable: saltar ADs ya volcados (published_date no nulo == ya procesado).
done_set = set()
page = 0
while True:
    resp = (sb.table("ads").select("ad_number")
            .eq("source", "easa").not_.is_("published_date", "null")
            .range(page * 1000, page * 1000 + 999).execute())
    rows = resp.data or []
    done_set.update(r["ad_number"] for r in rows)
    if len(rows) < 1000:
        break
    page += 1
print(f"ya volcados (skip): {len(done_set)}")

done = 0
written = 0
for num in keys:
    d = detail.get(num, {})
    p = pdf.get(num, {})
    patch = {}
    if d.get("publication_date"):
        patch["published_date"] = d["publication_date"]
    amoc = bool(d.get("amoc")) or bool(p.get("amoc"))
    patch["amoc_available"] = amoc
    if p.get("unsafe_condition"):
        patch["unsafe_condition"] = p["unsafe_condition"]
    if p.get("corrective_action"):
        patch["corrective_action"] = p["corrective_action"]
    if p.get("compliance_time"):
        patch["compliance_time"] = p["compliance_time"]

    # Solo escribir si hay algo util mas alla del amoc=False por defecto
    if len(patch) <= 1 and not patch.get("amoc_available"):
        done += 1
        continue

    adn = normalize_ad_number(num)
    if adn in done_set:
        done += 1
        continue
    try:
        sb.table("ads").update(patch).eq("source", "easa").eq("ad_number", adn).execute()
        written += 1
    except Exception as e:
        print(f"  WARN {adn}: {e}")
    done += 1
    if done % 500 == 0:
        print(f"  {done}/{len(keys)} procesados, {written} escritos")

print(f"LISTO: {done} procesados, {written} filas actualizadas")
