"""
Scraper para Transport Canada CAWIS
Obtiene Airworthiness Directives desde wwwapps.tc.gc.ca
"""

import requests
from bs4 import BeautifulSoup
import re
import time


TC_BASE_URL = "https://wwwapps.tc.gc.ca/Saf-Sec-Sur/2/cawis-swimn"
TC_SEARCH_URL = f"{TC_BASE_URL}/AD_as.aspx"
TC_RECENT_URL = f"{TC_BASE_URL}/AD_r.aspx"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


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
        print(f"[TC] Error obteniendo fabricantes: {e}")
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
        print(f"[TC] Error obteniendo modelos: {e}")
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

    print("[TC] Obteniendo pagina de busqueda...")
    manufacturers, soup = get_manufacturers(session)

    if not manufacturers or not soup:
        print("[TC] No se pudieron obtener los fabricantes.")
        return []

    selected_mfr = None
    if manufacturer_name:
        mfr_lower = manufacturer_name.lower()
        for mfr in manufacturers:
            if mfr_lower in mfr["text"].lower():
                selected_mfr = mfr
                break

        if not selected_mfr:
            print(f"[TC] Fabricante '{manufacturer_name}' no encontrado.")
            return []

    if not selected_mfr:
        print("[TC] No se especifico fabricante.")
        return []

    print(f"[TC] Fabricante seleccionado: {selected_mfr['text']}")

    print("[TC] Obteniendo modelos...")
    models, new_soup = get_models_for_manufacturer(session, soup, selected_mfr["value"])

    type_models = models.get(search_type, [])
    if not type_models:
        print(f"[TC] No hay modelos de tipo '{search_type}' para {selected_mfr['text']}.")
        for st, ms in models.items():
            if ms:
                print(f"  {st}: {len(ms)} modelos")
        return []

    selected_models = []
    if model_name:
        model_lower = model_name.lower()
        for m in type_models:
            if model_lower in m["text"].lower():
                selected_models.append(m)

        if not selected_models:
            print(f"[TC] Modelo '{model_name}' no encontrado.")
            return []
    else:
        selected_models = type_models

    all_results = []
    for sel_model in selected_models:
        print(f"[TC] Buscando ADs para {selected_mfr['text']} - {sel_model['text']}...")
        results = _do_search(session, new_soup, selected_mfr, sel_model, search_type)
        all_results.extend(results)
        time.sleep(1)

    print(f"[TC] Total ADs obtenidos: {len(all_results)}")
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
        print(f"[TC] Error en busqueda: {e}")
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
            print(f"  Sin resultados para {make_name} - {model_name}")
        return results

    rows = table.find_all("tr")

    header_row = rows[0] if rows else None
    col_map = {}
    if header_row:
        headers = [th.get_text(strip=True).lower() for th in header_row.find_all(["th", "td"])]
        print(f"[TC] DEBUG headers encontrados: {headers}")
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
                print(f"[TC] WARNING no pdf_link: AD={ad_number!r} make={make_name!r} hrefs={[a.get('href','') for c in cells for a in c.find_all('a',href=True)]}")

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
                results.append({
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
                })

        except Exception as e:
            print(f"[TC] WARNING error parseando fila: {e}")
            continue

    print(f"[TC] Parseados {len(results)} ADs (make={make_name!r}, model={model_name!r})")
    return results


def get_recent_ads(max_pages=1):
    """Obtiene los ADs recientes de Transport Canada."""
    session = requests.Session()
    all_results = []

    try:
        print("[TC] Obteniendo ADs recientes...")
        resp = session.get(TC_RECENT_URL, headers=HEADERS, timeout=30)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "lxml")

        results = _parse_tc_results(soup, "", "")
        all_results.extend(results)

        print(f"[TC] ADs recientes obtenidos: {len(all_results)}")

    except requests.RequestException as e:
        print(f"[TC] Error obteniendo ADs recientes: {e}")

    return all_results
