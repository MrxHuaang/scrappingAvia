"""
Scraper para EASA Safety Publications Tool
Obtiene Airworthiness Directives desde ad.easa.europa.eu
"""

import requests
from bs4 import BeautifulSoup
import time
import re


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
