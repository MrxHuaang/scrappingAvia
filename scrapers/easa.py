"""
Scraper para EASA Safety Publications Tool
Obtiene Airworthiness Directives desde ad.easa.europa.eu
"""

import requests
from bs4 import BeautifulSoup
import time
import re
import os
import json


EASA_BASE_URL = "https://ad.easa.europa.eu"
EASA_SEARCH_URL = f"{EASA_BASE_URL}/search/advanced"
EASA_RESULTS_URL = f"{EASA_BASE_URL}/search/advanced/result"  # FIXED: sin trailing slash
EASA_EXPORT_CSV = f"{EASA_BASE_URL}/search/export/csv"

# FIXED: EASA muestra 20 ADs por pagina (sin link de paginacion en HTML — es JS)
_PAGE_SIZE = 20

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


def search_ads(keyword=None, model_keyword=None, doc_types=None, date_from=None, date_to=None, max_pages=50):
    """
    Busca ADs en EASA.

    Args:
        keyword: Palabra clave general
        model_keyword: Modelo/fabricante a buscar
        doc_types: Lista de tipos (AD, EAD, PAD, SIB, PSD, SD)
        date_from: Fecha inicio (DD/MM/YYYY)
        date_to: Fecha fin (DD/MM/YYYY)
        max_pages: Maximo de paginas a recorrer

    Returns:
        Lista de diccionarios con los ADs encontrados.
    """
    session = requests.Session()

    try:
        session.get(EASA_SEARCH_URL, headers=HEADERS, timeout=30)
    except requests.RequestException:
        pass

    params = {}
    search_term = keyword or model_keyword
    if search_term:
        params["fi_keyword"] = search_term

    if doc_types:
        for dt in doc_types:
            params[f"fi_doctype_{dt.lower()}"] = "on"

    if date_from:
        params["fi_date_start"] = date_from
    if date_to:
        params["fi_date_end"] = date_to

    all_results = []
    page = 1
    total_available = None  # FIXED: se lee del HTML en page 1

    while page <= max_pages:
        # FIXED: page 1 usa la URL base; page 2+ usa ?page=N con la misma sesion
        url = EASA_RESULTS_URL if page == 1 else f"{EASA_RESULTS_URL}/?page={page}"

        try:
            resp = session.get(url, params=params if page == 1 else {}, headers=HEADERS, timeout=60)

            # Rate limiting: exponential backoff hasta 3 reintentos (2s, 4s, 8s)
            retry_delay = 2
            for attempt in range(3):
                if resp.status_code != 429:
                    break
                print(f"[EASA] Rate limit (429) en pagina {page}, reintento {attempt + 1}/3 en {retry_delay}s...")
                time.sleep(retry_delay)
                resp = session.get(url, params=params if page == 1 else {}, headers=HEADERS, timeout=60)
                retry_delay *= 2

            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            # FIXED: leer total de resultados del texto "Displaying X to Y out of total of Z"
            if total_available is None:
                total_available = _parse_total_results(soup)
                max_page_calc = ((total_available - 1) // _PAGE_SIZE + 1) if total_available else max_pages
                effective_max = min(max_pages, max_page_calc)
                print(f"[EASA] Total disponible: {total_available} ADs ({effective_max} paginas)")

            results = _parse_results_table(soup)

            if not results:
                print(f"[EASA] Pagina {page}: sin resultados, deteniendo.")
                break

            all_results.extend(results)

            # Log detallado cada 100 paginas, resumen breve el resto
            if page % 100 == 0:
                print(f"[EASA] *** PROGRESO: Pagina {page}/{effective_max} | Total acumulado: {len(all_results)} ADs ***")
            else:
                print(f"[EASA] Pagina {page}/{effective_max}: {len(results)} ADs | acumulado: {len(all_results)}")

            # FIXED: parar cuando ya tenemos todos o llegamos al limite
            if len(all_results) >= (total_available or 0) or page >= effective_max:
                break

            page += 1
            time.sleep(0.8)

        except requests.RequestException as e:
            print(f"[EASA] Error en pagina {page}: {e}")
            break

    # Filtrar por modelo si se especifico
    if model_keyword and all_results:
        model_lower = model_keyword.lower()
        filtered = [
            r for r in all_results
            if model_lower in r.get("Model", "").lower()
            or model_lower in r.get("Make", "").lower()
            or model_lower in r.get("Subject", "").lower()
            or model_lower in r.get("AD_Number", "").lower()
        ]
        if filtered:
            print(f"[EASA] Filtrados {len(filtered)} de {len(all_results)} por '{model_keyword}'")
            all_results = filtered

    print(f"[EASA] Total ADs obtenidos: {len(all_results)}")
    return all_results


def search_main_page(model_keyword=None, max_pages=5):
    """Obtiene ADs de la pagina principal de EASA (lista reciente)."""
    session = requests.Session()
    all_results = []

    # FIXED: search_main_page redirige a la misma URL de resultados sin filtros
    # La pagina principal lista los ADs mas recientes, es equivalente a result/ sin params
    total_available = None
    for page in range(1, max_pages + 1):
        url = EASA_RESULTS_URL if page == 1 else f"{EASA_RESULTS_URL}/?page={page}"  # FIXED

        try:
            resp = session.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            if total_available is None:
                total_available = _parse_total_results(soup)
                print(f"[EASA] Total disponible: {total_available} ADs")

            results = _parse_results_table(soup)

            if not results:
                print(f"[EASA] Pagina principal {page}: sin resultados, deteniendo.")
                break

            all_results.extend(results)
            print(f"[EASA] Pagina principal {page}: {len(results)} ADs | acumulado: {len(all_results)}")
            time.sleep(0.8)

        except requests.RequestException as e:
            print(f"[EASA] Error pagina principal {page}: {e}")
            break

    if model_keyword and all_results:
        model_lower = model_keyword.lower()
        filtered = [
            r for r in all_results
            if model_lower in r.get("Model", "").lower()
            or model_lower in r.get("Make", "").lower()
            or model_lower in r.get("Subject", "").lower()
        ]
        if filtered:
            all_results = filtered

    print(f"[EASA] Total ADs obtenidos: {len(all_results)}")
    return all_results


_PARSE_FAILURES = 0


def _parse_results_table(soup):
    """
    Parsea la tabla de resultados de EASA.
    """
    global _PARSE_FAILURES
    results = []

    table = soup.find("table", class_="ad-list")
    if not table:
        table = soup.find("table")
        if not table:
            return results

    rows = table.find_all("tr")

    for row in rows:
        cells = row.find_all("td")
        if len(cells) < 5:
            continue

        try:
            # Columna 0: AD Number
            ad_number = ""
            ad_link = cells[0].find("a", class_="easa_cd")
            if not ad_link:
                ad_link = cells[0].find("a")
            if ad_link:
                ad_number = ad_link.get_text(strip=True)

            if not ad_number:
                continue

            # Columna 2: Issue Date
            issue_date = cells[2].get_text(strip=True) if len(cells) > 2 else ""

            # Columna 3: Subject
            subject = cells[3].get_text(strip=True) if len(cells) > 3 else ""

            # Columna 4: Approval Holder / Type Designation
            make = ""
            model = ""
            if len(cells) > 4:
                tree_cell = cells[4]

                tc_holders = tree_cell.find_all("li", class_="tc_holder")
                makes_list = []
                models_list = []

                for holder in tc_holders:
                    holder_name = holder.find(string=True, recursive=False)
                    if holder_name:
                        holder_name = holder_name.strip()
                        if holder_name:
                            makes_list.append(holder_name)

                    types = holder.find_all("li", class_="type")
                    for t in types:
                        type_text = t.get_text(strip=True)
                        if type_text:
                            models_list.append(type_text)

                make = ", ".join(makes_list) if makes_list else ""
                model = ", ".join(models_list) if models_list else ""

            # Columna 5: Effective Date
            effective_date = cells[5].get_text(strip=True) if len(cells) > 5 else ""

            # Columna 6: Attachments (PDF links)
            pdf_link = ""
            if len(cells) > 6:
                for link in cells[6].find_all("a"):
                    href = link.get("href", "")
                    if href:
                        if not href.startswith("http"):
                            href = f"{EASA_BASE_URL}{href}"
                        if ".pdf" in href.lower() or "blob" in href.lower():
                            pdf_link = href
                            break
                        if not pdf_link:
                            pdf_link = href

            # FIXED: extraer product_type desde keywords del subject
            subject_lower = subject.lower()
            if re.search(r"\bengine\b|\bengines\b|\bmotor\b", subject_lower):
                product_type = "Engine"
            elif re.search(r"\bpropeller\b|\bpropellers\b", subject_lower):
                product_type = "Propeller"
            elif re.search(r"\bhelicopter\b|\bhelicopters\b|\brotorcraft\b", subject_lower):
                product_type = "Helicopter"
            elif re.search(r"\bappliance\b|\bavionics\b|\bequipment\b", subject_lower):
                product_type = "Appliance"
            else:
                product_type = "Aircraft"  # FIXED: default sensato (mayoría son ADs de aeronave)

            results.append({
                "Source": "EASA",
                "AD_Number": ad_number,
                "Subject": subject,
                "Make": make,
                "Model": model,
                "Effective_Date": effective_date or issue_date,
                "Status": "Current",
                "Product_Type": product_type,  # FIXED: ya no hardcodeado a ""
                "Docket": "",
                "PDF_Link": pdf_link,
            })

        except Exception as e:
            _PARSE_FAILURES += 1
            if _PARSE_FAILURES <= 10:
                print(f"[EASA] WARNING row parse failed: {type(e).__name__}: {e}")
            continue

    return results


def _parse_total_results(soup):
    """
    FIXED: Lee el total de resultados del texto que EASA incluye en el HTML:
    'Displaying records 1 to 20 out of a total of 18199 publications.'
    Retorna el numero entero, o None si no lo encuentra.
    """
    for tag in soup.find_all(string=re.compile(r"total of", re.I)):
        m = re.search(r"total of\s+([\d,]+)", tag, re.I)
        if m:
            return int(m.group(1).replace(",", ""))
    return None


# ---------------------------------------------------------------------------
# Enriquecimiento via pagina de detalle (ad.easa.europa.eu/ad/{number})
# La pagina de listado no trae los campos profundos; el detalle expone (estructurado,
# sin PDF): Issue date, Supersedure, Publication(s)=Service Bulletins, y el Remark
# estandar de AMOC. La prosa (unsafe condition / required action / compliance time)
# solo vive en el PDF -> queda fuera de este paso.
# Llamar SOLO desde el batch (run_easa), NO desde search_ads (frenaria la busqueda
# en vivo del app). Cacheado por AD_Number en outputs/easa_detail_cache.json.
# ---------------------------------------------------------------------------
EASA_DETAIL_URL = f"{EASA_BASE_URL}/ad"
_DETAIL_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "outputs", "easa_detail_cache.json",
)
_detail_cache = None


def _load_detail_cache():
    global _detail_cache
    if _detail_cache is None:
        try:
            with open(_DETAIL_CACHE_PATH, encoding="utf-8") as f:
                _detail_cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            _detail_cache = {}
    return _detail_cache


def _save_detail_cache():
    if _detail_cache is None:
        return
    try:
        os.makedirs(os.path.dirname(_DETAIL_CACHE_PATH), exist_ok=True)
        with open(_DETAIL_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_detail_cache, f, ensure_ascii=False)
    except OSError:
        pass


def _parse_detail(text: str) -> dict:
    """Extrae campos estructurados del texto plano de la pagina de detalle."""
    out = {"publication_date": None, "amoc": False,
           "service_bulletins": [], "supersedes": None, "superseded_by": None}

    m = re.search(r"Issue date\s+(\d{4}-\d{2}-\d{2})", text)
    if m:
        out["publication_date"] = m.group(1)

    out["amoc"] = "alternative methods of compliance" in text.lower() \
        or "alternative method of compliance" in text.lower()

    m = re.search(r"supersede[sd]?\s+(?:by\s+)?EASA AD\s+([A-Z0-9][A-Z0-9\-/]+)", text, re.I)
    if m:
        if re.search(r"superseded by", text, re.I):
            out["superseded_by"] = m.group(1).strip()
        else:
            out["supersedes"] = m.group(1).strip()

    # Bloque "Publication(s) and related information" -> referencias SB/AOT
    pub = re.search(
        r"Publication\(s\)[^\n]*\n(.*?)(?:\nPAD number|\nRemarks|\nDownload)",
        text, re.S | re.I,
    )
    if pub:
        sbs = []
        _SKIP = ("the use of later", "acceptable for compliance",
                 "requirements of this ad", "compliance with the requirements",
                 "consultation", "comments were received")
        for line in pub.group(1).splitlines():
            line = line.strip()
            low = line.lower()
            if not line or any(s in low for s in _SKIP) or line[0].islower():
                continue
            tok = re.search(r"[A-Z]{2,}[A-Z0-9]*[-/][A-Z0-9\-/]*\d", line)
            sbs.append((tok.group(0) if tok else line)[:120])
        out["service_bulletins"] = sbs[:20]

    return out


def _fetch_detail(ad_number: str, session: requests.Session) -> dict:
    cache = _load_detail_cache()
    if ad_number in cache:
        return cache[ad_number]
    result = {"publication_date": None, "amoc": False,
              "service_bulletins": [], "supersedes": None, "superseded_by": None}
    try:
        resp = session.get(f"{EASA_DETAIL_URL}/{ad_number}", headers=HEADERS, timeout=40)
        if resp.ok:
            soup = BeautifulSoup(resp.text, "lxml")
            text = re.sub(r"[ \t]+", " ", soup.get_text("\n"))
            result = _parse_detail(text)
    except requests.RequestException:
        pass
    cache[ad_number] = result
    return result


# ---------------------------------------------------------------------------
# Parseo del PDF del AD (unica fuente de la prosa: Reason / Required Action(s)
# and Compliance Time(s) / Ref. Publications). PDFs EASA son texto, no escaneos.
# ---------------------------------------------------------------------------
FETCH_PDF = True
_PDF_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "outputs", "easa_pdf_cache.json",
)
_pdf_cache = None


def _load_pdf_cache():
    global _pdf_cache
    if _pdf_cache is None:
        try:
            with open(_PDF_CACHE_PATH, encoding="utf-8") as f:
                _pdf_cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            _pdf_cache = {}
    return _pdf_cache


def _save_pdf_cache():
    if _pdf_cache is None:
        return
    try:
        os.makedirs(os.path.dirname(_PDF_CACHE_PATH), exist_ok=True)
        with open(_PDF_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_pdf_cache, f, ensure_ascii=False)
    except OSError:
        pass


def _clean_pdf(s: str) -> str:
    # Quitar footers/headers repetidos del PDF EASA.
    s = re.sub(r"EASA AD No\.?:.*", " ", s)
    s = re.sub(r"TE\.CAP\.[0-9\-]+", " ", s)
    s = re.sub(r"Page \d+ of \d+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _pdf_section(txt: str, start: str, ends: list[str], cap: int = 600) -> str | None:
    i = txt.find(start)
    if i < 0:
        return None
    j = i + len(start)
    end = len(txt)
    for lbl in ends:
        k = txt.find(lbl, j)
        if 0 <= k < end:
            end = k
    seg = _clean_pdf(txt[j:end])
    return seg[:cap].strip() or None


_EASA_COMPLIANCE = re.compile(
    r"(before further flight"
    r"|before exceeding[^.;]{0,80}"
    r"|within\s+\d[\d,]*\s*(?:flight\s+)?(?:hours?|days?|weeks?|months?|years?|cycles?|landings?|FH|FC)\b[^.;]{0,40}"
    r"|at intervals not to exceed[^.;]{0,60}"
    r"|before the threshold[^.;]{0,60})",
    re.I,
)


def _parse_pdf_fields(text: str) -> dict:
    out = {"unsafe_condition": None, "corrective_action": None,
           "compliance_time": None, "service_bulletins": []}
    if not text:
        return out
    txt = re.sub(r"[ \t]+", " ", text)

    out["unsafe_condition"] = _pdf_section(
        txt, "Reason:",
        ["Required Action", "Ref. Publications", "Remarks:", "Effective Date"], cap=500,
    )
    action = _pdf_section(
        txt, "Required Action(s) and Compliance Time(s):",
        ["Ref. Publications", "Remarks:"], cap=900,
    )
    if action:
        m = _EASA_COMPLIANCE.search(action)
        out["compliance_time"] = re.sub(r"\s+", " ", m.group(0)).strip() if m else None
        # Strip the standard lead-in boilerplate to reach the real action text.
        action = re.sub(
            r"^Required as indicated by this AD,\s*unless the action[^:]*:\s*",
            "", action, flags=re.I,
        )
        action = re.sub(
            r"^(?:Inspection\(s\)|Action\(s\)|Modification\(s\)|Replacement\(s\)|Corrective action\(s\))\s*:\s*",
            "", action, flags=re.I,
        )
        out["corrective_action"] = action[:500].strip() or None

    refs = _pdf_section(txt, "Ref. Publications:", ["Remarks:", "Effective Date", "Appendix"], cap=800)
    if refs:
        sbs = []
        for m in re.finditer(r"\b(?:SB|VSB|ASB|Service Bulletin)\s+([A-Z0-9][A-Z0-9\-/]+)", refs, re.I):
            tok = m.group(1).strip().rstrip(".")
            if tok and tok.upper() not in (x.upper() for x in sbs):
                sbs.append(tok)
        out["service_bulletins"] = sbs[:20]
    return out


def _fetch_pdf(ad_number: str, pdf_link: str, session: requests.Session) -> dict:
    cache = _load_pdf_cache()
    if ad_number in cache:
        return cache[ad_number]
    result = {"unsafe_condition": None, "corrective_action": None,
              "compliance_time": None, "service_bulletins": []}
    if pdf_link and ".pdf" in pdf_link.lower():
        try:
            import io
            from pypdf import PdfReader
            resp = session.get(pdf_link, headers=HEADERS, timeout=60)
            if resp.ok and resp.content[:4] == b"%PDF":
                reader = PdfReader(io.BytesIO(resp.content))
                text = "\n".join((p.extract_text() or "") for p in reader.pages[:8])
                result = _parse_pdf_fields(text)
        except Exception:
            pass
    cache[ad_number] = result
    return result


def enrich_details(results: list[dict]) -> list[dict]:
    """
    Para cada AD del listado, baja su pagina de detalle y agrega las claves que
    consume normalize()/populate_relations: Publication_Date, AMOC_Available,
    Service_Bulletins, Supersedes, Superseded_By. Idempotente + cacheado.
    """
    session = requests.Session()
    session.headers.update(HEADERS)
    total = len(results)
    dcache = _load_detail_cache()
    pcache = _load_pdf_cache()
    for i, row in enumerate(results, 1):
        num = (row.get("AD_Number") or "").strip()
        if not num:
            continue
        cached = (num in dcache) and (num in pcache)
        d = _fetch_detail(num, session)
        if d.get("publication_date"):
            row["Publication_Date"] = d["publication_date"]
        row["AMOC_Available"] = bool(d.get("amoc"))
        sbs = list(d.get("service_bulletins") or [])
        if d.get("supersedes"):
            row["Supersedes"] = d["supersedes"]
        if d.get("superseded_by"):
            row["Superseded_By"] = d["superseded_by"]

        # Prosa: solo del PDF (Reason / Required Action(s) and Compliance Time(s)).
        if FETCH_PDF:
            pf = _fetch_pdf(num, row.get("PDF_Link") or "", session)
            if pf.get("unsafe_condition"):
                row["Unsafe_Condition"] = pf["unsafe_condition"]
            if pf.get("corrective_action"):
                row["Corrective_Action"] = pf["corrective_action"]
            if pf.get("compliance_time"):
                row["Compliance_Time"] = pf["compliance_time"]
            for sb in pf.get("service_bulletins") or []:
                if sb not in sbs:
                    sbs.append(sb)
        if sbs:
            row["Service_Bulletins"] = sbs

        if i % 200 == 0:
            print(f"[EASA] detalle+pdf: {i}/{total} enriquecidos")
            _save_detail_cache()
            _save_pdf_cache()
        if not cached:  # no dormir si ya estaba en cache (re-run rapido)
            time.sleep(0.15)
    _save_detail_cache()
    _save_pdf_cache()
    return results


def export_csv(keyword=None, session=None):
    """Exporta los resultados de busqueda como CSV usando la funcion nativa de EASA."""
    if session is None:
        session = requests.Session()

    params = {}
    if keyword:
        params["fi_keyword"] = keyword

    try:
        session.get(EASA_RESULTS_URL, params=params, headers=HEADERS, timeout=30)
        resp = session.get(EASA_EXPORT_CSV, headers=HEADERS, timeout=60)
        resp.raise_for_status()
        return resp.content
    except requests.RequestException as e:
        print(f"[EASA] Error exportando CSV: {e}")
        return None
