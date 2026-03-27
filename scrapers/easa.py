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
EASA_RESULTS_URL = f"{EASA_BASE_URL}/search/advanced/result/"
EASA_EXPORT_CSV = f"{EASA_BASE_URL}/search/export/csv"

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

    while page <= max_pages:
        print(f"[EASA] Buscando pagina {page}...", end=" ")

        try:
            if page == 1:
                url = EASA_RESULTS_URL
                resp = session.get(url, params=params, headers=HEADERS, timeout=60)
            else:
                url = f"{EASA_RESULTS_URL}/page-{page}"
                resp = session.get(url, params=params, headers=HEADERS, timeout=60)

            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            results = _parse_results_table(soup)

            if not results:
                print("Sin mas resultados.")
                break

            print(f"Encontrados {len(results)} resultados.")
            all_results.extend(results)

            if not _has_next_page(soup, page):
                break

            page += 1
            time.sleep(0.8)

        except requests.RequestException as e:
            print(f"Error: {e}")
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

    for page in range(1, max_pages + 1):
        print(f"[EASA] Obteniendo pagina principal {page}...", end=" ")

        try:
            if page == 1:
                url = EASA_BASE_URL
            else:
                url = f"{EASA_BASE_URL}/ad-list/page-{page}"

            resp = session.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            results = _parse_results_table(soup)

            if not results:
                print("Sin mas resultados.")
                break

            print(f"Encontrados {len(results)} resultados.")
            all_results.extend(results)

            if not _has_next_page(soup, page):
                break

            time.sleep(0.8)

        except requests.RequestException as e:
            print(f"Error: {e}")
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


def _parse_results_table(soup):
    """
    Parsea la tabla de resultados de EASA.
    """
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

            results.append({
                "Source": "EASA",
                "AD_Number": ad_number,
                "Subject": subject,
                "Make": make,
                "Model": model,
                "Effective_Date": effective_date or issue_date,
                "Status": "Current",
                "Product_Type": "",
                "Docket": "",
                "PDF_Link": pdf_link,
            })

        except Exception:
            continue

    return results


def _has_next_page(soup, current_page):
    """Verifica si hay una pagina siguiente."""
    next_page = current_page + 1

    pagination = soup.find_all("a", href=re.compile(rf"page-{next_page}"))
    if pagination:
        return True

    next_btn = soup.find("a", string=re.compile(r"next|>|>|>>", re.I))
    if next_btn:
        return True

    return False


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
