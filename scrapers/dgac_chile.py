"""
Scraper para DGAC Chile — Directivas de Aeronavegabilidad nacionales.
URL: https://www.dgac.gob.cl/normativa/reglamentacion-aeronautica/directiva-aeronavegab/

La DGAC publica una tabla HTML estática con ~30 DAs vigentes.
Son directivas nacionales chilenas, no FAA/EASA.
"""

import re
import requests
from bs4 import BeautifulSoup
from scrapers.common.http import browser_headers

DGAC_URL = "https://www.dgac.gob.cl/normativa/reglamentacion-aeronautica/directiva-aeronavegab/"

HEADERS = browser_headers()


def search_ads(keyword=None, make=None, model=None, date_from=None, date_to=None, max_results=200):
    """
    Busca Directivas de Aeronavegabilidad en DGAC Chile.
    Scrapes the static HTML table from the DGAC website.
    """
    try:
        resp = requests.get(DGAC_URL, headers=HEADERS, timeout=20)
        resp.raise_for_status()
    except Exception as e:
        print(f"[DGAC Chile] Error al conectar: {e}")
        return []

    soup = BeautifulSoup(resp.text, "html.parser")

    results = []
    tables = soup.find_all("table")

    for table in tables:
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 3:
                continue

            ad_number = cells[0].get_text(strip=True)
            if not ad_number or not re.match(r"DA\s*\d", ad_number, re.I):
                continue

            applicability = cells[1].get_text(strip=True) if len(cells) > 1 else ""
            date_text = cells[2].get_text(strip=True) if len(cells) > 2 else ""

            pdf_link = ""
            for cell in cells:
                link = cell.find("a", href=True)
                if link and link["href"].endswith(".pdf"):
                    href = link["href"]
                    if not href.startswith("http"):
                        href = "https://www.dgac.gob.cl" + href
                    pdf_link = href
                    break

            make_extracted = _extract_make(applicability)
            effective_date = _parse_date(date_text)

            ad = {
                "Source": "DGAC Chile",
                "AD_Number": ad_number.strip(),
                "Make": make_extracted,
                "Model": "",
                "Subject": applicability,
                "Effective_Date": effective_date,
                "PDF_Link": pdf_link,
                "Product_Type": "",
                "Status": "Final Rule",
                "Docket": "",
            }

            if _matches_filters(ad, keyword, make, model):
                results.append(ad)

    results = results[:max_results]
    print(f"[DGAC Chile] Total DAs obtenidas: {len(results)}")
    return results


def _extract_make(text):
    known = [
        "Boeing", "Cessna", "Piper", "Beechcraft", "Robinson",
        "Lycoming", "Continental", "McCauley", "Scheibe",
    ]
    upper = text.upper()
    for m in known:
        if m.upper() in upper:
            return m
    return ""


def _parse_date(text):
    m = re.search(r"(\d{1,2})[./\-](\w{3,})[./\-](\d{4})", text)
    if m:
        months = {
            "ene": "01", "feb": "02", "mar": "03", "abr": "04",
            "may": "05", "jun": "06", "jul": "07", "ago": "08",
            "sep": "09", "oct": "10", "nov": "11", "dic": "12",
        }
        mo = months.get(m.group(2).lower()[:3], "01")
        return f"{m.group(3)}-{mo}-{m.group(1).zfill(2)}"

    m2 = re.search(r"(\d{1,2})[./\-](\d{1,2})[./\-](\d{4})", text)
    if m2:
        return f"{m2.group(3)}-{m2.group(2).zfill(2)}-{m2.group(1).zfill(2)}"

    m3 = re.search(r"(\d{4})", text)
    if m3:
        return f"{m3.group(1)}-01-01"

    return ""


def _matches_filters(ad, keyword, make, model):
    if keyword:
        kl = keyword.lower()
        text = f"{ad['AD_Number']} {ad['Subject']} {ad['Make']}".lower()
        if kl not in text:
            return False
    if make:
        if make.lower() not in ad["Make"].lower() and make.lower() not in ad["Subject"].lower():
            return False
    if model:
        if model.lower() not in ad["Subject"].lower():
            return False
    return True
