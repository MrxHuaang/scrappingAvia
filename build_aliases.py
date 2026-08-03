"""
Extrae fabricantes y modelos reales de la Federal Register API
y construye aircraft_aliases.json para el autocompletado.
Corre una vez: python build_aliases.py
"""

import requests
import json
import time
import collections
import re

API = "https://www.federalregister.gov/api/v1/documents.json"
from scrapers.common.http import API_HEADERS as HEADERS  # noqa: E402

makes_models = collections.defaultdict(set)

for page in range(1, 30):
    params = {
        "conditions[type][]": "RULE",
        "conditions[agencies][]": "federal-aviation-administration",
        "conditions[term]": "airworthiness directives 14 CFR part 39",
        "fields[]": ["title"],
        "per_page": 100,
        "page": page,
        "order": "newest",
    }
    r = requests.get(API, params=params, headers=HEADERS, timeout=20)
    if r.status_code != 200:
        break
    docs = r.json().get("results", [])
    if not docs:
        break

    for d in docs:
        title = d.get("title", "")
        # Títulos tienen forma: "Airworthiness Directives; Boeing Model 737-800 Airplanes"
        m = re.search(r"Airworthiness Directives?;\s*(.+)", title, re.I)
        if not m:
            continue
        rest = m.group(1).strip()
        # Separar fabricante del modelo
        parts = rest.split(" Model ")
        if len(parts) >= 2:
            make = parts[0].strip().rstrip(",;")
            model_raw = parts[1].split(";")[0].split(",")[0].strip()
            model_raw = re.sub(r"\s+airplanes?|\s+helicopters?|\s+aircraft", "", model_raw, flags=re.I).strip()
            if make and model_raw:
                makes_models[make].add(model_raw)

    print(f"Página {page}: {len(docs)} docs procesados")
    time.sleep(0.3)

# Convertir a dict serializable y ordenar
result = {make: sorted(list(models)) for make, models in sorted(makes_models.items()) if models}

with open("aircraft_aliases.json", "w", encoding="utf-8") as f:
    json.dump(result, f, indent=2, ensure_ascii=False)

print(f"\nListo: {len(result)} fabricantes extraídos → aircraft_aliases.json")
for make, models in list(result.items())[:5]:
    print(f"  {make}: {models[:3]}...")
