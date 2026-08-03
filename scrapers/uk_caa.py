"""
Scraper para UK CAA — Airworthiness Directives (UK) y Mandatory Permit Directives.
URL: https://www.caa.co.uk/data-and-publications/publications/publication-series/

Post-Brexit el Reino Unido salio del sistema EASA y la CAA emite sus propios ADs
con prefijo "G-" (mas los legado "CAP747-"). Solo publica AD propio cuando
DIVERGE del estado de diseno, asi que el volumen es bajo (~200 docs) pero es
exactamente el punto ciego de cumplimiento para una aeronave en registro G-.

Las paginas de serie son HTML server-rendered, sin JS ni paginacion: cada
documento es un <article class="m-results__item"> con un <dl> de
Reference / Status / Version date. El link al PDF NO esta en el listado, vive en
la pagina de detalle como /publication/download/{id}, asi que hace falta un fetch
por documento (cacheado en disco, patron de scrapers/easa.py).

Dos decisiones de alcance, ambas para no corromper la tabla `ads`:

1. La serie "Emergency ADs" de la CAA mezcla EADs de OTRAS autoridades
   ("EASA EAD 2026-0119-E", "Transport Canada EAD CF-2026-12-E") junto a las
   propias. Ingerir esas como UK CAA duplicaria ADs que ya existen bajo su
   fuente real con otro numero. Solo se toman las de autoria UK.

2. La serie "State of Design ADs NOT adopted by the CAA" se EXCLUYE por
   completo. Son ADs ajenos que la CAA decidio expresamente no adoptar: no son
   directivas en vigor, y presentarlas como AD del UK CAA le diria al usuario
   que debe cumplir algo que su autoridad declaro inaplicable. Es dato valioso
   pero de otra forma (una declaracion de NO aplicabilidad), no cabe en `ads`.
"""

import os
import re
import time

import requests
from bs4 import BeautifulSoup
from scrapers.common.http import browser_headers
from scrapers.common.cache import load_json_cache, save_json_cache
from scrapers.common.http import get_with_backoff

BASE = "https://www.caa.co.uk"
SERIES_BASE = f"{BASE}/data-and-publications/publications/publication-series"

# (slug, etiqueta corta para logs). El orden importa poco: se deduplica por
# numero de AD, y la primera aparicion gana.
SERIES = [
    ("airworthiness-directives-uk-from-1-january-2021", "AD 2021+"),
    ("airworthiness-directives-uk-up-to-31-december-2020", "AD pre-2021"),
    ("mandatory-permit-directives", "MPD"),
    ("emergency-airworthiness-directives", "EAD"),
]

# Referencias de autoria UK. Todo lo que no matchee (p.ej. "EASA EAD 2026-0119-E",
# "FAA AD 2020-26-16", "ANAC AD 2024-04-01") pertenece a otra fuente y se descarta.
UK_REF_RE = re.compile(r"^(?:G-\d{4}-\d{3,4}|CAP747-\d{3}|MPD\s*\d{4}-\d{3})", re.I)

HEADERS = browser_headers({"Accept-Language": "en-GB,en;q=0.9"})

_MONTHS = {
    "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
    "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
}

CACHE_PATH = os.path.join("outputs", "uk_caa_detail_cache.json")
_detail_cache = None


def _load_cache():
    global _detail_cache
    if _detail_cache is None:
        _detail_cache = load_json_cache(CACHE_PATH)
    return _detail_cache


def _save_cache():
    if _detail_cache is not None:
        save_json_cache(CACHE_PATH, _detail_cache, indent=1)


def _get(session, url, tries=3):
    """GET con backoff exponencial para 429/5xx (helper comun). Texto o None."""
    resp = get_with_backoff(url, session=session, timeout=25, retries=tries, tag="UK CAA")
    if resp is None:
        return None
    if resp.status_code >= 400:
        print(f"[UK CAA] HTTP {resp.status_code} en {url}")
        return None
    return resp.text


def _parse_date(text):
    """'14-May-2021' -> '2021-05-14'. Tambien acepta ISO y dd/mm/yyyy."""
    t = (text or "").strip()
    m = re.search(r"(\d{1,2})[-/\s]([A-Za-z]{3,})[-/\s](\d{4})", t)
    if m:
        mo = _MONTHS.get(m.group(2).lower()[:3])
        if mo:
            return f"{m.group(3)}-{mo}-{m.group(1).zfill(2)}"
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", t)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
    m = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", t)
    if m:
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    return ""


def _infer_product_type(subject):
    """
    La CAA no publica tipo de producto, asi que se infiere del titulo.

    Los titulos son "<producto>: <sistema afectado> - <detalle>", y el sistema
    (post-dos-puntos) es mucho mejor senal que el producto: el fabricante suele
    llevar "Aircraft" en la razon social ("Acro Aircraft Seating Ltd"), asi que
    buscar "aircraft" sobre el titulo completo clasificaba unos asientos de
    pasajero como celula. Se evalua el sistema primero y por especificidad
    (helice/rotor/motor antes que celula).

    El fallback es "Aircraft": un AD aplica a un producto con certificado de
    tipo, y sin palabra clave el caso mas probable es celula. Es inferencia, no
    dato de la fuente.
    """
    full = (subject or "").lower()
    system = full.split(":", 1)[1] if ":" in full else full

    def has(text, *keys):
        return any(k in text for k in keys)

    # Motor y estructura se resuelven ANTES que equipamiento porque "cylinder"
    # es ambiguo: culata de motor (Rotax 912) y pintle-to-cylinder del tren
    # (Jetstream) caian como Appliance junto a los cilindros de gas de globo.
    if has(full, "engine", "turbofan", "turboprop", "turboshaft", "powerplant"):
        return "Engine"
    if has(system, "landing gear", "fuselage", "wing", "empennage", "stabiliser",
           "stabilizer", "flight control", "airframe", "structure"):
        return "Helicopter" if has(full, "helicopter", "rotorcraft") else "Aircraft"

    # Equipamiento embarcado: se decide sobre el sistema, nunca sobre el titulo
    # completo, para no confundirse con el nombre del fabricante ("Acro Aircraft
    # Seating Ltd" clasificaba unos asientos como celula).
    if has(system, "seat", "furnishing", "harness", "life jacket", "lifejacket",
           "life raft", "beacon", "elt", "battery", "cylinder", "extinguisher",
           "oxygen", "escape slide", "emergency equipment"):
        return "Appliance"
    if has(system, "propeller", "airscrew"):
        return "Propeller"
    if has(full, "helicopter", "rotorcraft", "gyroplane") or has(system, "rotor"):
        return "Helicopter"
    if has(system, "turbine", "apu"):
        return "Engine"
    if has(full, "balloon", "glider", "sailplane", "airship"):
        return "Aircraft"
    return "Aircraft"


def _extract_make(subject):
    """
    Manufacturer del titulo. La CAA titula "<Fabricante> <modelo>: <asunto>",
    asi que la lista cubre lo comun y el fallback usa el primer token antes de
    los dos puntos.
    """
    known = [
        "Airbus Helicopters", "Airbus", "Boeing", "Bombardier", "Embraer",
        "de Havilland", "ATR", "Leonardo", "Agusta", "Sikorsky", "Bell",
        "Robinson", "Pilatus", "Cessna", "Textron", "Piper", "Beechcraft",
        "Diamond", "Cirrus", "Extra", "Grob", "Schempp-Hirth", "Schleicher",
        "DG Flugzeugbau", "Cameron Balloons", "Lindstrand", "Ultramagic",
        "Rolls-Royce", "Pratt & Whitney", "General Electric", "CFM",
        "Safran", "Turbomeca", "Lycoming", "Continental", "Rotax",
        "Britten-Norman", "Scottish Aviation", "Slingsby", "Auster",
        "Hawker", "BAE Systems", "Reims", "Socata", "Gulfstream",
        "Honeywell", "Garmin", "Thales", "Liebherr", "Woodward",
    ]
    s = subject or ""
    low = s.lower()
    for k in known:
        if k.lower() in low:
            return k
    head = s.split(":", 1)[0].strip()
    m = re.match(r"([A-Z][A-Za-z&.\-]+(?:\s+[A-Z][A-Za-z&.\-]+)?)", head)
    return m.group(1).strip() if m else ""


def _parse_series(html, label):
    """Extrae los documentos de una pagina de serie."""
    soup = BeautifulSoup(html, "html.parser")
    out = []

    for art in soup.select("article.m-results__item"):
        link = art.select_one("h3 a[href]")
        if not link:
            continue
        title = re.sub(r"\s+", " ", link.get_text(strip=True))
        href = link["href"]
        detail_url = href if href.startswith("http") else BASE + href

        fields = {}
        for dl in art.find_all("dl"):
            terms = dl.find_all("dt")
            defs = dl.find_all("dd")
            for dt, dd in zip(terms, defs):
                key = re.sub(r"\s+", " ", dt.get_text(strip=True)).lower()
                fields[key] = re.sub(r"\s+", " ", dd.get_text(strip=True))

        ref = fields.get("reference", "").strip()
        if not ref:
            continue
        if not UK_REF_RE.match(ref):
            # Serie de EADs: documento de otra autoridad, no es un AD del UK CAA.
            continue

        out.append({
            "ref": ref,
            "title": title,
            "status": fields.get("status", ""),
            "date": _parse_date(fields.get("version date", "")),
            "detail_url": detail_url,
            "series": label,
        })

    return out


def _fetch_detail(session, detail_url):
    """
    Lee la pagina de detalle: link al PDF y "Review comment".

    El PDF no esta en el listado y pdf_link es campo requerido por
    scripts/validate_ads.py, asi que este fetch es obligatorio. Aprovechando el
    viaje se toma tambien el review comment, que populate_db.normalize() usa como
    Abstract para derivar unsafe_condition / corrective_action / service
    bulletins. Cacheado en disco: no cambia entre corridas.
    """
    cache = _load_cache()
    hit = cache.get(detail_url)
    if isinstance(hit, dict):
        return hit
    if isinstance(hit, str):  # cache de una version previa (solo PDF)
        return {"pdf": hit, "abstract": ""}

    html = _get(session, detail_url)
    if not html:
        return {"pdf": "", "abstract": ""}

    soup = BeautifulSoup(html, "html.parser")

    pdf = ""
    a = soup.select_one('a[href*="/publication/download/"]') or soup.select_one('a[href$=".pdf"]')
    if a and a.get("href"):
        href = a["href"]
        pdf = href if href.startswith("http") else BASE + href

    abstract = ""
    for dl in soup.find_all("dl"):
        terms = dl.find_all("dt")
        defs = dl.find_all("dd")
        for dt, dd in zip(terms, defs):
            if re.sub(r"\s+", " ", dt.get_text(strip=True)).lower() == "review comment":
                text = re.sub(r"\s+", " ", dd.get_text(strip=True)).strip()
                if text and text != "-":
                    abstract = text
                break

    out = {"pdf": pdf, "abstract": abstract}
    cache[detail_url] = out
    return out


def search_ads(keyword=None, make=None, model=None, date_from=None, date_to=None, max_results=200):
    """
    Busca Airworthiness Directives y Mandatory Permit Directives del UK CAA.
    Recorre las series publicadas, deduplica por numero y resuelve el PDF de
    cada documento en su pagina de detalle.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    docs = []
    seen = set()

    for slug, label in SERIES:
        html = _get(session, f"{SERIES_BASE}/{slug}/")
        if not html:
            print(f"[UK CAA] Serie no disponible: {label}")
            continue

        found = _parse_series(html, label)
        added = 0
        for d in found:
            key = d["ref"].upper().replace(" ", "")
            if key in seen:
                continue
            seen.add(key)
            docs.append(d)
            added += 1
        print(f"[UK CAA] {label}: {added} documentos ({len(found)} de autoria UK en la pagina)")
        time.sleep(0.5)

    results = []
    for d in docs:
        ad = {
            "Source": "UK CAA",
            "AD_Number": d["ref"],
            "Make": _extract_make(d["title"]),
            "Model": "",
            "Subject": d["title"],
            "Effective_Date": d["date"],
            "PDF_Link": "",
            "Product_Type": _infer_product_type(d["title"]),
            "Status": d["status"] or "Current",
            "Docket": "",
        }
        if not _matches_filters(ad, keyword, make, model):
            continue
        if not _in_date_range(ad["Effective_Date"], date_from, date_to):
            continue
        results.append((ad, d["detail_url"]))
        if len(results) >= max_results:
            break

    # El detalle se resuelve solo para los que sobrevivieron los filtros: evita
    # ~200 fetches cuando la consulta pedia un fabricante concreto.
    for ad, detail_url in results:
        detail = _fetch_detail(session, detail_url)
        ad["PDF_Link"] = detail.get("pdf", "")
        if detail.get("abstract"):
            ad["Abstract"] = detail["abstract"]
        time.sleep(0.3)
    _save_cache()

    out = [ad for ad, _ in results]
    print(f"[UK CAA] Total documentos obtenidos: {len(out)}")
    return out


def _in_date_range(iso_date, date_from, date_to):
    if not iso_date:
        return True  # sin fecha no se puede excluir con certeza
    if date_from and iso_date < str(date_from)[:10]:
        return False
    if date_to and iso_date > str(date_to)[:10]:
        return False
    return True


def _matches_filters(ad, keyword, make, model):
    if keyword:
        blob = f"{ad['AD_Number']} {ad['Subject']} {ad['Make']}".lower()
        if keyword.lower() not in blob:
            return False
    if make:
        if make.lower() not in ad["Make"].lower() and make.lower() not in ad["Subject"].lower():
            return False
    if model and model.lower() not in ad["Subject"].lower():
        return False
    return True


if __name__ == "__main__":
    found = search_ads(max_results=5)
    print(f"Encontrados: {len(found)}")
    for row in found[:5]:
        print(row)
