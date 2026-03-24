"""
Scraper para Transport Canada CAWIS
Obtiene Airworthiness Directives desde wwwapps.tc.gc.ca
Maneja ASP.NET WebForms con postbacks y ViewState.
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
        "__VIEWSTATE",
        "__VIEWSTATEGENERATOR",
        "__EVENTVALIDATION",
        "__EVENTTARGET",
        "__EVENTARGUMENT",
        "__LASTFOCUS",
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

        # Buscar el dropdown de fabricantes
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

    # Simular postback al seleccionar fabricante
    post_data = dict(viewstate)

    # Buscar el nombre real del campo ddlMake
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

        models = {
            "aircraft": [],
            "engine": [],
            "propeller": [],
        }

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
        model_name: Nombre del modelo (opcional, busca todos si no se especifica)
        search_type: Tipo de búsqueda: "aircraft", "engine", "propeller"

    Returns:
        Lista de diccionarios con los ADs encontrados.
    """
    session = requests.Session()

    # Paso 1: Obtener la página y lista de fabricantes
    print("[TC] Obteniendo página de búsqueda...")
    manufacturers, soup = get_manufacturers(session)

    if not manufacturers or not soup:
        print("[TC] No se pudieron obtener los fabricantes.")
        return []

    # Paso 2: Encontrar el fabricante
    selected_mfr = None
    if manufacturer_name:
        mfr_lower = manufacturer_name.lower()
        for mfr in manufacturers:
            if mfr_lower in mfr["text"].lower():
                selected_mfr = mfr
                break

        if not selected_mfr:
            print(f"[TC] Fabricante '{manufacturer_name}' no encontrado.")
            print("[TC] Fabricantes disponibles:")
            for m in manufacturers[:20]:
                print(f"  - {m['text']}")
            return []

    if not selected_mfr:
        print("[TC] No se especificó fabricante.")
        return []

    print(f"[TC] Fabricante seleccionado: {selected_mfr['text']}")

    # Paso 3: Postback para obtener modelos
    print("[TC] Obteniendo modelos...")
    models, new_soup = get_models_for_manufacturer(session, soup, selected_mfr["value"])

    type_models = models.get(search_type, [])
    if not type_models:
        print(f"[TC] No hay modelos de tipo '{search_type}' para {selected_mfr['text']}.")
        # Mostrar lo que hay disponible
        for st, ms in models.items():
            if ms:
                print(f"  {st}: {len(ms)} modelos")
        return []

    # Paso 4: Seleccionar modelo(s)
    selected_models = []
    if model_name:
        model_lower = model_name.lower()
        for m in type_models:
            if model_lower in m["text"].lower():
                selected_models.append(m)

        if not selected_models:
            print(f"[TC] Modelo '{model_name}' no encontrado.")
            print("[TC] Modelos disponibles:")
            for m in type_models[:20]:
                print(f"  - {m['text']}")
            return []
    else:
        selected_models = type_models  # Buscar todos los modelos

    # Paso 5: Realizar búsqueda
    all_results = []
    for sel_model in selected_models:
        print(f"[TC] Buscando ADs para {selected_mfr['text']} - {sel_model['text']}...")

        results = _do_search(session, new_soup, selected_mfr, sel_model, search_type)
        all_results.extend(results)
        time.sleep(1)  # Rate limiting

    print(f"[TC] Total ADs obtenidos: {len(all_results)}")
    return all_results


def _do_search(session, soup, manufacturer, model, search_type):
    """Realiza la búsqueda real en CAWIS."""
    viewstate = _get_viewstate(soup)
    post_data = dict(viewstate)

    # Establecer el fabricante
    select = soup.find("select", {"id": re.compile(r"ddlMake", re.I)})
    select_name = select.get("name", "ctl00$ContentPlaceHolder1$ddlMake") if select else "ctl00$ContentPlaceHolder1$ddlMake"
    post_data[select_name] = manufacturer["value"]

    # Establecer el modelo según el tipo
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

    # Determinar el botón de búsqueda
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

    # Limpiar __EVENTTARGET para submit
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
        print(f"[TC] Error en búsqueda: {e}")
        return []


def _parse_tc_results(soup, make_name, model_name):
    """Parsea los resultados de búsqueda de Transport Canada."""
    results = []

    # Buscar la tabla de resultados
    table = soup.find("table", {"id": re.compile(r"gridDirective", re.I)})
    if not table:
        # Buscar cualquier tabla de resultados
        tables = soup.find_all("table")
        for t in tables:
            if t.find("th", string=re.compile(r"AD.*Number|Directive", re.I)):
                table = t
                break

    if not table:
        # Verificar si hay un mensaje de "No records found"
        no_records = soup.find(string=re.compile(r"no.*record|no.*result|not.*found", re.I))
        if no_records:
            print(f"  Sin resultados para {make_name} - {model_name}")
        return results

    rows = table.find_all("tr")

    # Detectar índices de columnas desde el encabezado
    header_row = rows[0] if rows else None
    col_map = {}
    if header_row:
        headers = [th.get_text(strip=True).lower() for th in header_row.find_all(["th", "td"])]
        for i, h in enumerate(headers):
            if "ad" in h and "number" in h or "directive" in h:
                col_map["ad_number"] = i
            elif "subject" in h or "description" in h:
                col_map["subject"] = i
            elif "effective" in h or "date" in h:
                col_map["effective_date"] = i
            elif "status" in h:
                col_map["status"] = i

    for row in rows[1:]:  # Skip header
        cells = row.find_all("td")
        if not cells:
            continue

        try:
            cell_texts = [c.get_text(strip=True) for c in cells]

            ad_number = cell_texts[col_map.get("ad_number", 0)] if cell_texts else ""
            subject = cell_texts[col_map.get("subject", 1)] if len(cell_texts) > 1 else ""
            effective_date = cell_texts[col_map.get("effective_date", 2)] if len(cell_texts) > 2 else ""

            # Buscar link al PDF
            pdf_link = ""
            for cell in cells:
                for link in cell.find_all("a"):
                    href = link.get("href", "")
                    if ".pdf" in href.lower() or "blob" in href.lower():
                        pdf_link = href if href.startswith("http") else f"{TC_BASE_URL}/{href}"
                        break

            if ad_number:
                results.append({
                    "Source": "Transport Canada",
                    "AD_Number": ad_number,
                    "Subject": subject,
                    "Make": make_name,
                    "Model": model_name,
                    "Effective_Date": effective_date,
                    "Status": "",
                    "Product_Type": "",
                    "Docket": "",
                    "PDF_Link": pdf_link,
                })

        except Exception:
            continue

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


if __name__ == "__main__":
    print("=== Transport Canada AD Scraper - Test ===")

    print("\nObteniendo fabricantes disponibles...")
    manufacturers, _ = get_manufacturers()
    if manufacturers:
        print(f"Fabricantes encontrados: {len(manufacturers)}")
        for m in manufacturers[:15]:
            print(f"  - {m['text']}")
        if len(manufacturers) > 15:
            print(f"  ... y {len(manufacturers) - 15} más")
    else:
        print("No se pudieron obtener los fabricantes.")
