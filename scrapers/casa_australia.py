"""
Scraper para CASA Australia — Airworthiness Directives.
URL: https://www.casa.gov.au/aircraft/airworthiness/airworthiness-directives

CASA bloquea scraping automatizado (403 Forbidden en todas las URLs).
Tienen data files CSV disponibles, pero también protegidos.
Este scraper intenta múltiples endpoints y devuelve [] si falla.
"""

import re
import requests
from bs4 import BeautifulSoup

CASA_URLS = [
    "https://services.casa.gov.au/airworth/airwd/",
    "https://www.casa.gov.au/airworthiness-directives",
    "https://www.casa.gov.au/aircraft/airworthiness/airworthiness-directives",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
}


def search_ads(keyword=None, make=None, model=None, date_from=None, date_to=None, max_results=200):
    """
    Busca Airworthiness Directives en CASA Australia.
    CASA bloquea acceso automatizado — devuelve [] si no se puede conectar.
    """
    for url in CASA_URLS:
        try:
            resp = requests.get(url, headers=HEADERS, timeout=12, allow_redirects=True)
            if resp.status_code == 403:
                print(f"[CASA Australia] 403 Forbidden en {url}")
                continue
            resp.raise_for_status()

            results = _parse_html(resp.text, keyword, make, model)
            if results:
                results = results[:max_results]
                print(f"[CASA Australia] {len(results)} ADs obtenidas de {url}")
                return results

        except requests.Timeout:
            print(f"[CASA Australia] Timeout en {url}")
            continue
        except Exception as e:
            print(f"[CASA Australia] Error en {url}: {e}")
            continue

    print("[CASA Australia] Sitio no disponible — todas las URLs fallaron (403 Forbidden)")
    return []


def _parse_html(html, keyword, make, model):
    soup = BeautifulSoup(html, "html.parser")
    results = []

    tables = soup.find_all("table")
    for table in tables:
        rows = table.find_all("tr")
        for row in rows:
            cells = row.find_all("td")
            if len(cells) < 2:
                continue

            ad_number = cells[0].get_text(strip=True)
            if not ad_number or not re.search(r"AD/", ad_number, re.I):
                continue

            subject = cells[1].get_text(strip=True) if len(cells) > 1 else ""
            effective_date = ""
            if len(cells) > 2:
                effective_date = _parse_date(cells[2].get_text(strip=True))

            pdf_link = ""
            for cell in cells:
                link = cell.find("a", href=True)
                if link:
                    href = link["href"]
                    if not href.startswith("http"):
                        href = "https://www.casa.gov.au" + href
                    if ".pdf" in href.lower():
                        pdf_link = href
                    elif not pdf_link:
                        pdf_link = href

            make_extracted = _extract_make(subject)

            ad = {
                "Source": "CASA Australia",
                "AD_Number": ad_number.strip(),
                "Make": make_extracted,
                "Model": "",
                "Subject": subject,
                "Effective_Date": effective_date,
                "PDF_Link": pdf_link,
                "Product_Type": "",
                "Status": "Final Rule",
                "Docket": "",
            }

            if _matches_filters(ad, keyword, make, model):
                results.append(ad)

    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        href = a["href"]
        if re.search(r"AD/", text) and len(text) > 5:
            if not href.startswith("http"):
                href = "https://www.casa.gov.au" + href
            ad = {
                "Source": "CASA Australia",
                "AD_Number": text.strip(),
                "Make": _extract_make(text),
                "Model": "",
                "Subject": text.strip(),
                "Effective_Date": "",
                "PDF_Link": href if ".pdf" in href.lower() else "",
                "Product_Type": "",
                "Status": "Final Rule",
                "Docket": "",
            }
            if _matches_filters(ad, keyword, make, model):
                existing = {r["AD_Number"] for r in results}
                if ad["AD_Number"] not in existing:
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
        "Sikorsky", "Embraer", "Bombardier", "de Havilland", "Eurocopter",
        "Robinson", "Pilatus", "Raytheon", "Rolls-Royce", "Pratt",
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
