"""
Scraper para GCAA UAE — Airworthiness Directives.
URL: https://www.gcaa.gov.ae/en/epublication/pages/airworthinessdirectives.aspx

DESHABILITADO del run diario (ver populate_db.DISABLED_SOURCES), verificado
2026-07-28: el sitio NO bloquea la request (200, ~1.3MB de HTML) — el defecto
real es que el listado de ADs vive detras de un postback ASP.NET AJAX
(UpdatePanel/ScriptManager, __VIEWSTATE presente); un GET liso solo trae el
shell del formulario, sin ninguna fila de datos. De los tres scrapers
deshabilitados es el UNICO sin WAF/bloqueo de red de por medio — arreglable
con Playwright (ejecutar el postback, esperar el grid, leer el DOM resultante)
sin pelear ningun bot-challenge.
"""

import re
import requests
from bs4 import BeautifulSoup

GCAA_URLS = [
    "https://www.gcaa.gov.ae/en/epublication/pages/airworthinessdirectives.aspx",
    "https://www.gcaa.gov.ae/en/eServices/Pages/eServices.aspx",
    "https://www.gcaa.gov.ae/en/epublication/airworthiness-directives",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ar;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def search_ads(keyword=None, make=None, model=None, date_from=None, date_to=None, max_results=200):
    """
    Busca Airworthiness Directives en GCAA (Emiratos Árabes Unidos).
    Usa Session con headers de navegador para evitar bloqueos.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    for url in GCAA_URLS:
        try:
            resp = session.get(url, timeout=15, allow_redirects=True)
            if resp.status_code == 403:
                print(f"[GCAA UAE] 403 Forbidden en {url}")
                continue
            if resp.status_code >= 400:
                print(f"[GCAA UAE] HTTP {resp.status_code} en {url}")
                continue
            resp.raise_for_status()

            results = _parse_page(resp.text, keyword, make, model)
            if results:
                results = results[:max_results]
                print(f"[GCAA UAE] {len(results)} ADs obtenidas de {url}")
                return results
            else:
                print(f"[GCAA UAE] Página accesible pero sin ADs parseables en {url}")

        except requests.Timeout:
            print(f"[GCAA UAE] Timeout en {url}")
            continue
        except Exception as e:
            print(f"[GCAA UAE] Error en {url}: {e}")
            continue

    print("[GCAA UAE] Sitio no disponible — todas las URLs fallaron (403 Forbidden)")
    return []


def _parse_page(html, keyword, make, model):
    soup = BeautifulSoup(html, "html.parser")
    results = []

    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue

        for row in rows[1:]:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            ad_number = cells[0].get_text(strip=True)
            if not ad_number or len(ad_number) < 3:
                continue

            title = cells[1].get_text(strip=True) if len(cells) > 1 else ""
            effective_date = ""
            if len(cells) > 3:
                effective_date = _parse_date(cells[3].get_text(strip=True))
            elif len(cells) > 2:
                effective_date = _parse_date(cells[2].get_text(strip=True))

            pdf_link = ""
            for cell in cells:
                link = cell.find("a", href=True)
                if link:
                    href = link["href"]
                    if not href.startswith("http"):
                        href = "https://www.gcaa.gov.ae" + href
                    pdf_link = href
                    break

            ad = {
                "Source": "GCAA UAE",
                "AD_Number": ad_number.strip(),
                "Make": _extract_make(title or ad_number),
                "Model": "",
                "Subject": title or ad_number,
                "Effective_Date": effective_date,
                "PDF_Link": pdf_link,
                "Product_Type": "",
                "Status": "Final Rule",
                "Docket": "",
            }

            if _matches_filters(ad, keyword, make, model):
                results.append(ad)

    return results


def _parse_date(text):
    m = re.search(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})", text)
    if m:
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    m = re.search(r"(\d{4})-(\d{1,2})-(\d{1,2})", text)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
    return ""


def _extract_make(text):
    known = [
        "Boeing", "Airbus", "Cessna", "Piper", "Beechcraft", "Bell",
        "Sikorsky", "Embraer", "Bombardier", "ATR", "Eurocopter",
        "Robinson", "Rolls-Royce", "General Electric", "Pratt",
    ]
    upper = text.upper()
    for m in known:
        if m.upper() in upper:
            return m
    return ""


def _matches_filters(ad, keyword, make, model):
    if keyword:
        kl = keyword.lower()
        text = f"{ad['AD_Number']} {ad['Subject']} {ad['Make']}".lower()
        if kl not in text:
            return False
    if make and make.lower() not in ad["Make"].lower() and make.lower() not in ad["Subject"].lower():
        return False
    if model and model.lower() not in ad["Subject"].lower():
        return False
    return True


if __name__ == "__main__":
    results = search_ads(make="Boeing", max_results=10)
    print(f"Encontrados: {len(results)}")
    if results:
        print(results[0])
