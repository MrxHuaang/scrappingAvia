"""
Scraper para Transport Canada CAWIS
Obtiene Airworthiness Directives desde wwwapps.tc.gc.ca
"""

import requests
from bs4 import BeautifulSoup
import re
import time
import os
import io
import json
from scrapers.common.http import browser_headers

from scrapers.common.log import get_logger

log = get_logger(__name__)


TC_BASE_URL = "https://wwwapps.tc.gc.ca/Saf-Sec-Sur/2/cawis-swimn"
TC_SEARCH_URL = f"{TC_BASE_URL}/AD_as.aspx"
TC_RECENT_URL = f"{TC_BASE_URL}/AD_r.aspx"

HEADERS = browser_headers()


def _get_viewstate(soup):
    """Extrae los campos de ViewState necesarios para postback."""
    fields = {}
    for field_name in [
        "__VIEWSTATE", "__VIEWSTATEGENERATOR", "__EVENTVALIDATION",
        "__EVENTTARGET", "__EVENTARGUMENT", "__LASTFOCUS",
    ]:
        tag = soup.find("input", {"name": field_name})
        if tag:
            fields[field_name] = tag.get("value", "")
    return fields


def get_manufacturers(session=None):
    """Obtiene la lista de fabricantes disponibles en CAWIS."""
    if session is None:
        session = requests.Session()

    try:
        resp = session.get(TC_SEARCH_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        select = soup.find("select", {"id": re.compile(r"ddlMake", re.I)})
        if not select:
            select = soup.find("select", {"name": re.compile(r"ddlMake", re.I)})

        manufacturers = []
        if select:
            options = select.find_all("option")
            for opt in options:
                value = opt.get("value", "")
                text = opt.get_text(strip=True)
                if value and text and value != "0" and text != "Select...":
                    manufacturers.append({"value": value, "text": text})

        return manufacturers, soup

    except requests.RequestException as e:
        log.info(f"[TC] Error obteniendo fabricantes: {e}")
        return [], None


def get_models_for_manufacturer(session, soup, manufacturer_value):
    """
    Obtiene las listas de modelos (Aircraft, Engine, Propeller)
    para un fabricante dado. Requiere postback de ASP.NET.
    """
    viewstate = _get_viewstate(soup)
    post_data = dict(viewstate)

    select = soup.find("select", {"id": re.compile(r"ddlMake", re.I)})
    select_name = select.get("name", "ctl00$ContentPlaceHolder1$ddlMake") if select else "ctl00$ContentPlaceHolder1$ddlMake"

    post_data[select_name] = manufacturer_value
    post_data["__EVENTTARGET"] = select_name
    post_data["__EVENTARGUMENT"] = ""

    try:
        resp = session.post(
            TC_SEARCH_URL,
            data=post_data,
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=30,
        )
        resp.raise_for_status()
        new_soup = BeautifulSoup(resp.text, "lxml")

        models = {"aircraft": [], "engine": [], "propeller": []}

        for model_type, field_id in [
            ("aircraft", "lbAirframe"),
            ("engine", "lbEngine"),
            ("propeller", "lbPropeller"),
        ]:
            select = new_soup.find("select", {"id": re.compile(field_id, re.I)})
            if select:
                options = select.find_all("option")
                for opt in options:
                    value = opt.get("value", "")
                    text = opt.get_text(strip=True)
                    if value and text:
                        models[model_type].append({"value": value, "text": text})

        return models, new_soup

    except requests.RequestException as e:
        log.info(f"[TC] Error obteniendo modelos: {e}")
        return {"aircraft": [], "engine": [], "propeller": []}, soup


def search_ads(manufacturer_name=None, model_name=None, search_type="aircraft"):
    """
    Busca ADs en Transport Canada CAWIS.

    Args:
        manufacturer_name: Nombre del fabricante
        model_name: Nombre del modelo (opcional)
        search_type: Tipo: "aircraft", "engine", "propeller"

    Returns:
        Lista de diccionarios con los ADs encontrados.
    """
    session = requests.Session()

    log.info("[TC] Obteniendo pagina de busqueda...")
    manufacturers, soup = get_manufacturers(session)

    if not manufacturers or not soup:
        log.info("[TC] No se pudieron obtener los fabricantes.")
        return []

    selected_mfr = None
    if manufacturer_name:
        mfr_lower = manufacturer_name.lower()
        for mfr in manufacturers:
            if mfr_lower in mfr["text"].lower():
                selected_mfr = mfr
                break

        if not selected_mfr:
            log.info(f"[TC] Fabricante '{manufacturer_name}' no encontrado.")
            return []

    if not selected_mfr:
        log.info("[TC] No se especifico fabricante.")
        return []

    log.info(f"[TC] Fabricante seleccionado: {selected_mfr['text']}")

    log.info("[TC] Obteniendo modelos...")
    models, new_soup = get_models_for_manufacturer(session, soup, selected_mfr["value"])

    type_models = models.get(search_type, [])
    if not type_models:
        log.info(f"[TC] No hay modelos de tipo '{search_type}' para {selected_mfr['text']}.")
        for st, ms in models.items():
            if ms:
                log.info(f"  {st}: {len(ms)} modelos")
        return []

    selected_models = []
    if model_name:
        model_lower = model_name.lower()
        for m in type_models:
            if model_lower in m["text"].lower():
                selected_models.append(m)

        if not selected_models:
            log.info(f"[TC] Modelo '{model_name}' no encontrado.")
            return []
    else:
        selected_models = type_models

    all_results = []
    for sel_model in selected_models:
        log.info(f"[TC] Buscando ADs para {selected_mfr['text']} - {sel_model['text']}...")
        results = _do_search(session, new_soup, selected_mfr, sel_model, search_type)
        all_results.extend(results)
        time.sleep(1)

    log.info(f"[TC] Total ADs obtenidos: {len(all_results)}")
    return all_results


def _do_search(session, soup, manufacturer, model, search_type):
    """Realiza la busqueda real en CAWIS."""
    viewstate = _get_viewstate(soup)
    post_data = dict(viewstate)

    select = soup.find("select", {"id": re.compile(r"ddlMake", re.I)})
    select_name = select.get("name", "ctl00$ContentPlaceHolder1$ddlMake") if select else "ctl00$ContentPlaceHolder1$ddlMake"
    post_data[select_name] = manufacturer["value"]

    field_mapping = {
        "aircraft": "lbAirframe",
        "engine": "lbEngine",
        "propeller": "lbPropeller",
    }

    model_field_id = field_mapping.get(search_type, "lbAirframe")
    model_select = soup.find("select", {"id": re.compile(model_field_id, re.I)})
    model_field_name = (
        model_select.get("name", f"ctl00$ContentPlaceHolder1${model_field_id}")
        if model_select
        else f"ctl00$ContentPlaceHolder1${model_field_id}"
    )
    post_data[model_field_name] = model["value"]

    button_mapping = {
        "aircraft": "btnSearchAirframe",
        "engine": "btnSearchEngine",
        "propeller": "btnSearchPropeller",
    }

    btn_id = button_mapping.get(search_type, "btnSearchAirframe")
    btn = soup.find("input", {"id": re.compile(btn_id, re.I)})
    btn_name = (
        btn.get("name", f"ctl00$ContentPlaceHolder1${btn_id}")
        if btn
        else f"ctl00$ContentPlaceHolder1${btn_id}"
    )
    post_data[btn_name] = "All AD"

    post_data["__EVENTTARGET"] = ""
    post_data["__EVENTARGUMENT"] = ""

    try:
        resp = session.post(
            TC_SEARCH_URL,
            data=post_data,
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
            timeout=60,
            allow_redirects=True,
        )
        resp.raise_for_status()
        result_soup = BeautifulSoup(resp.text, "lxml")
        return _parse_tc_results(result_soup, manufacturer["text"], model["text"])

    except requests.RequestException as e:
        log.info(f"[TC] Error en busqueda: {e}")
        return []


def _parse_tc_results(soup, make_name, model_name):
    """Parsea los resultados de busqueda de Transport Canada."""
    results = []

    # FIXED: La tabla real del DOM tiene IDs específicos según el tipo de búsqueda.
    # "gridDirectiveModel" → búsqueda por modelo (columnas: New, Model, Ctry, AD Number, EN, FR, Subject, SB Ref, Repeat)
    # "gridADNumberRecent" → ADs recientes (columnas: New, Entry Date, Manufacturer, Applicability, Ctry, AD Number, EN, FR, Subject, Issue Date)
    table = soup.find("table", id=re.compile(r"gridDirectiveModel|gridADNumberRecent", re.I))
    if not table:
        # Fallback: buscar tabla por encabezado (comportamiento original)
        tables = soup.find_all("table")
        for t in tables:
            if t.find(["th", "td"], string=re.compile(r"AD.*Number|Directive|Applicability", re.I)):
                table = t
                break

    if not table:
        no_records = soup.find(string=re.compile(r"no.*record|no.*result|not.*found", re.I))
        if no_records:
            log.info(f"  Sin resultados para {make_name} - {model_name}")
        return results

    rows = table.find_all("tr")

    header_row = rows[0] if rows else None
    col_map = {}
    if header_row:
        headers = [th.get_text(strip=True).lower() for th in header_row.find_all(["th", "td"])]
        log.info(f"[TC] DEBUG headers encontrados: {headers}")
        for i, h in enumerate(headers):
            # FIXED: mapeo ampliado con headers reales del DOM
            if ("ad" in h and "number" in h) or h in ("ad number",):
                col_map["ad_number"] = i
            elif "subject" in h or "ad subject" in h:
                col_map["subject"] = i
            elif "issue date" in h or "entry date" in h or ("effective" in h and "date" in h):
                col_map["effective_date"] = i
            elif "status" in h:
                col_map["status"] = i
            elif "applicability" in h:
                # FIXED: columna "Applicability" existe en tabla de recientes → product_type
                col_map["applicability"] = i
            elif "manufacturer" in h:
                col_map["manufacturer"] = i
            elif h in ("en",):
                # FIXED: la columna "EN" contiene el link al PDF en inglés
                col_map["pdf_col"] = i

    for row in rows[1:]:
        cells = row.find_all("td")
        if not cells:
            continue

        try:
            cell_texts = [c.get_text(strip=True) for c in cells]

            ad_number = cell_texts[col_map.get("ad_number", 0)] if cell_texts else ""
            subject = cell_texts[col_map.get("subject", -1)] if col_map.get("subject") is not None and len(cell_texts) > col_map.get("subject", 0) else ""
            if not subject and len(cell_texts) > 1:
                # Fallback: última columna larga suele ser el subject
                subject = max(cell_texts, key=len)
            effective_date = cell_texts[col_map.get("effective_date", -1)] if col_map.get("effective_date") is not None and len(cell_texts) > col_map.get("effective_date", 0) else ""

            # FIXED: El link PDF usa el patrón "AD_dl.aspx?ad=...&ft=pdf" — NO tiene ".pdf" en el href.
            # El selector original (.pdf en href) NUNCA matcheaba. Ahora buscamos ft=pdf, lbPDFE id, o blob.
            pdf_link = ""

            # Estrategia 1: buscar en la columna "EN" específicamente (más confiable)
            if col_map.get("pdf_col") is not None and col_map["pdf_col"] < len(cells):
                pdf_cell = cells[col_map["pdf_col"]]
                a_tag = pdf_cell.find("a", href=True)
                if a_tag:
                    href = a_tag.get("href", "")
                    pdf_link = href if href.startswith("http") else f"{TC_BASE_URL}/{href.lstrip('/')}"

            # Estrategia 2: barrer todas las celdas buscando href con ft=pdf, .pdf, blob, lbPDFE
            if not pdf_link:
                for cell in cells:
                    for link in cell.find_all("a", href=True):
                        href = link.get("href", "")
                        link_id = link.get("id", "").lower()
                        if href and (
                            "ft=pdf" in href.lower()        # FIXED: patrón real de CAWIS
                            or "lbpdfe" in link_id          # FIXED: ID real del link EN PDF
                            or ".pdf" in href.lower()
                            or "blob" in href.lower()
                            or "ad_dl" in href.lower()      # FIXED: patrón download de CAWIS
                        ):
                            pdf_link = href if href.startswith("http") else f"{TC_BASE_URL}/{href.lstrip('/')}"
                            break
                    if pdf_link:
                        break

            # Estrategia 3: cualquier link al detalle del AD como fallback
            if not pdf_link:
                for cell in cells:
                    for link in cell.find_all("a", href=True):
                        href = link.get("href", "")
                        if href and ("ad_html" in href.lower() or "AD_dsp" in href):
                            pdf_link = href if href.startswith("http") else f"{TC_BASE_URL}/{href.lstrip('/')}"
                            break
                    if pdf_link:
                        break

            # FIXED: debug para cada AD sin pdf_link
            if not pdf_link and ad_number:
                log.info(f"[TC] WARNING no pdf_link: AD={ad_number!r} make={make_name!r} hrefs={[a.get('href','') for c in cells for a in c.find_all('a',href=True)]}")

            # FIXED: status real a partir del subject (no hay columna propia en CAWIS)
            subject_lower = subject.lower()
            if col_map.get("status") is not None and len(cell_texts) > col_map.get("status", 0):
                status = cell_texts[col_map["status"]]
            elif "emergency" in subject_lower:
                status = "Emergency AD"
            elif "proposed" in subject_lower or "nprm" in subject_lower:
                status = "NPRM"
            elif "cancelled" in subject_lower or "canceled" in subject_lower:
                status = "Cancelled"
            elif "superseded" in subject_lower:
                status = "Superseded"
            else:
                status = "Current"  # FIXED: fallback real de TC (no "Final Rule")

            # Supersession desde el subject: "(Superseded by CF-2019-33)".
            superseded_by = None
            m_sup = re.search(r"superseded\s+by\s+(CF-\d{2,4}-\d{2,3}(?:R\d+)?)", subject, re.I)
            if m_sup:
                superseded_by = m_sup.group(1).upper()

            # FIXED: docket = AD Number (es el identificador CAWIS, equivalente a docket)
            docket = ad_number

            # FIXED: product_type desde columna "Applicability" o contexto de búsqueda
            applicability = ""
            if col_map.get("applicability") is not None and len(cell_texts) > col_map.get("applicability", 0):
                applicability = cell_texts[col_map["applicability"]]
            combined = f"{applicability} {subject}".lower()
            if re.search(r"\bengine\b", combined):
                product_type = "Engine"
            elif re.search(r"\bpropeller\b", combined):
                product_type = "Propeller"
            elif re.search(r"\bappliance\b", combined):
                product_type = "Appliance"
            elif re.search(r"\bhelicopter\b|\baircraft\b|\bairplane\b|\bairship\b", combined):
                product_type = "Aircraft"
            else:
                product_type = "Aircraft"  # FIXED: CAWIS es principalmente ADs de aeronaves

            # FIXED: make desde columna Manufacturer si existe (tabla de recientes)
            if col_map.get("manufacturer") is not None and len(cell_texts) > col_map.get("manufacturer", 0):
                resolved_make = cell_texts[col_map["manufacturer"]] or make_name
            else:
                resolved_make = make_name

            if ad_number:
                row_out = {
                    "Source": "Transport Canada",
                    "AD_Number": ad_number,
                    "Subject": subject,
                    "Make": resolved_make,
                    "Model": model_name,
                    "Effective_Date": effective_date,
                    "Status": status,           # FIXED: ya no vacío
                    "Product_Type": product_type,  # FIXED: ya no vacío
                    "Docket": docket,           # FIXED: ya no vacío
                    "PDF_Link": pdf_link,
                }
                if superseded_by and superseded_by != ad_number:
                    row_out["Superseded_By"] = superseded_by
                results.append(row_out)

        except Exception as e:
            log.info(f"[TC] WARNING error parseando fila: {e}")
            continue

    log.info(f"[TC] Parseados {len(results)} ADs (make={make_name!r}, model={model_name!r})")
    return results


# ---------------------------------------------------------------------------
# Enriquecimiento via PDF del AD (AD_dl.aspx?...&ft=pdf). El PDF de TC trae
# secciones limpias: Background (unsafe), Corrective Actions, Compliance.
# Cacheado por AD_Number. Llamar desde el batch (run_transport_canada).
# ---------------------------------------------------------------------------
FETCH_PDF = True
_TC_PDF_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "outputs", "tc_pdf_cache.json",
)
_tc_pdf_cache = None


def _load_tc_cache():
    global _tc_pdf_cache
    if _tc_pdf_cache is None:
        try:
            with open(_TC_PDF_CACHE_PATH, encoding="utf-8") as f:
                _tc_pdf_cache = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            _tc_pdf_cache = {}
    return _tc_pdf_cache


def _save_tc_cache():
    if _tc_pdf_cache is None:
        return
    try:
        os.makedirs(os.path.dirname(_TC_PDF_CACHE_PATH), exist_ok=True)
        with open(_TC_PDF_CACHE_PATH, "w", encoding="utf-8") as f:
            json.dump(_tc_pdf_cache, f, ensure_ascii=False)
    except OSError:
        pass


def _tc_section(txt: str, start: str, ends: list[str], cap: int = 600) -> str | None:
    i = txt.find(start)
    if i < 0:
        return None
    j = i + len(start)
    end = len(txt)
    for lbl in ends:
        k = txt.find(lbl, j)
        if 0 <= k < end:
            end = k
    seg = re.sub(r"\s+", " ", txt[j:end]).strip(" :\n")
    return seg[:cap].strip() or None


_TC_SECTIONS = ["Subject:", "Applicability:", "Compliance:", "Background:",
                "Corrective Action", "Disposition", "Authorization", "Enquiries",
                "Effective Date:", "Type Certificate:"]


_TC_COMPLIANCE = re.compile(
    r"(before further flight"
    r"|within\s+\d[\d,]*\s*(?:flight\s+|air\s+)?(?:hours?|days?|weeks?|months?|years?|cycles?|landings?)\b[^.;]{0,40}"
    r"|before exceeding[^.;]{0,80})",
    re.I,
)


def _parse_tc_pdf(text: str) -> dict:
    """
    TC publica 3 layouts: propia (Background/Corrective Actions/Compliance),
    EU-adoptada (formato EASA: Reason / Required Action(s) and Compliance Time(s)),
    US-adoptada (formato FAA). Se intentan TC y EASA; US-adoptada cae best-effort.
    """
    out = {"unsafe_condition": None, "corrective_action": None,
           "compliance_time": None, "amoc": False, "service_bulletins": []}
    if not text:
        return out
    txt = re.sub(r"[ \t]+", " ", text)
    def others(lbl):
        return [s for s in _TC_SECTIONS if s != lbl]

    # unsafe: TC Background -> EASA Reason
    out["unsafe_condition"] = (
        _tc_section(txt, "Background:", others("Background:"), cap=500)
        or _tc_section(txt, "Reason:",
                       ["Required Action", "Ref. Publications", "Remarks:", "Effective"], cap=500)
    )
    # corrective: TC Corrective Action(s) -> EASA Required Action(s)
    corr = (
        _tc_section(txt, "Corrective Action", others("Corrective Action"), cap=600)
        or _tc_section(txt, "Required Action(s) and Compliance Time(s):",
                       ["Ref. Publications", "Remarks:"], cap=600)
    )
    if corr:
        corr = re.sub(r"^s?\W+", "", corr).strip()  # quitar resto de "Action(s):"
    out["corrective_action"] = corr or None

    # compliance: seccion TC Compliance -> primera frase; else patron explicito
    comp = _tc_section(txt, "Compliance:", others("Compliance:"), cap=400)
    if comp:
        out["compliance_time"] = re.split(r"(?<=[.])\s", comp)[0].strip()[:240]
    else:
        m = _TC_COMPLIANCE.search(txt)
        if m:
            out["compliance_time"] = re.sub(r"\s+", " ", m.group(0)).strip()

    low = txt.lower()
    out["amoc"] = "alternative means of compliance" in low or "alternative method of compliance" in low

    sbs = []
    # SB token: exige guion + digito para evitar ruido ("label", etc.)
    for m in re.finditer(
        r"\b(?:SB|ASB|Service Bulletin)\s+(?:No\.?\s*)?([A-Z0-9]{2,}[-/][A-Z0-9\-/]*\d[A-Z0-9\-/]*)",
        txt, re.I,
    ):
        t = m.group(1).strip().rstrip(".")
        if t and t.upper() not in (x.upper() for x in sbs):
            sbs.append(t)
    out["service_bulletins"] = sbs[:20]

    # Supersession same-source: solo numeros CF- (los layouts EU/US-adoptados
    # citan ADs EASA/FAA — esas son equivalencias, no supersession de TC).
    m = re.search(
        r"(?:this\s+(?:directive|AD)\s+)?supersedes\s+(?:AD\s+|Airworthiness\s+Directive\s+)?"
        r"(CF-\d{2,4}-\d{2,3}(?:R\d+)?)(?![-\d])",
        txt, re.I,
    )
    out["supersedes"] = m.group(1).upper() if m else None
    return out


def _fetch_tc_pdf(ad_number: str, pdf_link: str, session: requests.Session) -> dict:
    cache = _load_tc_cache()
    # "supersedes" => entrada con el esquema nuevo; las viejas se re-piden una vez.
    if ad_number in cache and "supersedes" in cache[ad_number]:
        return cache[ad_number]
    result = {"unsafe_condition": None, "corrective_action": None,
              "compliance_time": None, "amoc": False, "service_bulletins": [],
              "supersedes": None}
    if pdf_link and "ft=pdf" in pdf_link.lower():
        try:
            from pypdf import PdfReader
            resp = session.get(pdf_link, headers=HEADERS, timeout=60)
            if resp.ok and resp.content[:4] == b"%PDF":
                text = "\n".join((p.extract_text() or "") for p in PdfReader(io.BytesIO(resp.content)).pages[:8])
                result = _parse_tc_pdf(text)
        except Exception:
            pass
    cache[ad_number] = result
    return result


def enrich_details(results: list[dict]) -> list[dict]:
    """
    Enriquece cada AD de TC con la prosa de su PDF (Background/Corrective/Compliance).

    Un AD aparece una vez POR MODELO al que aplica, y la busqueda corre por
    cuatro fabricantes, asi que la corrida diaria trae ~37.000 filas para ~4.800
    ADs distintos. Antes este bucle recorria las 37.000: el PDF salia de cache a
    partir de la segunda, pero la pausa de cortesia se pagaba igual en cada una.
    Medido en el runner: 42 minutos, el 87% de ellos repitiendo trabajo ya hecho.

    Ahora el PDF se busca UNA vez por numero de AD y el resultado se copia a
    todas sus filas. Las duplicadas siguen saliendo de aqui intactas: cada una
    lleva su propio modelo y de ahi salen las filas de `ad_applicability`
    (populate_relations recibe la lista sin deduplicar). Deduplicar antes de
    enriquecer habria sido mas corto y habria perdido esa cobertura.

    La pausa solo corre cuando de verdad se fue a la red. Es cortesia con el
    servidor de Transport Canada, no un ritual: un acierto de cache no toca su
    servidor.
    """
    if not FETCH_PDF:
        return results
    session = requests.Session()
    session.headers.update(HEADERS)
    cache = _load_tc_cache()

    groups: dict[str, list[dict]] = {}
    for row in results:
        num = (row.get("AD_Number") or "").strip()
        if not num:
            continue
        groups.setdefault(num, []).append(row)

    total = len(groups)
    for i, (num, rows) in enumerate(groups.items(), 1):
        # El representante es la fila que trae PDF; sin una asi no hay nada que
        # pedir para ese AD.
        rep = next((r for r in rows if "ft=pdf" in (r.get("PDF_Link") or "").lower()), None)
        if rep is None:
            continue
        cached = num in cache and "supersedes" in cache[num]
        pf = _fetch_tc_pdf(num, rep.get("PDF_Link") or "", session)
        for row in rows:
            if pf.get("unsafe_condition"):
                row["Unsafe_Condition"] = pf["unsafe_condition"]
            if pf.get("corrective_action"):
                row["Corrective_Action"] = pf["corrective_action"]
            if pf.get("compliance_time"):
                row["Compliance_Time"] = pf["compliance_time"]
            row["AMOC_Available"] = bool(pf.get("amoc"))
            if pf.get("service_bulletins"):
                row["Service_Bulletins"] = pf["service_bulletins"]
            if pf.get("supersedes") and pf["supersedes"] != num:
                row["Supersedes"] = pf["supersedes"]
        if i % 100 == 0:
            log.info(f"[TC] pdf: {i}/{total} ADs enriquecidos")
            _save_tc_cache()
        if not cached:
            time.sleep(0.15)
    _save_tc_cache()
    return results


def get_recent_ads(max_pages=1):
    """Obtiene los ADs recientes de Transport Canada."""
    session = requests.Session()
    all_results = []

    try:
        log.info("[TC] Obteniendo ADs recientes...")
        resp = session.get(TC_RECENT_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        results = _parse_tc_results(soup, "", "")
        all_results.extend(results)

        log.info(f"[TC] ADs recientes obtenidos: {len(all_results)}")

    except requests.RequestException as e:
        log.info(f"[TC] Error obteniendo ADs recientes: {e}")

    return all_results
