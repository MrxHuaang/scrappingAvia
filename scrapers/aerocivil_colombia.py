"""
Scraper para AEROCIVIL Colombia — Directivas de Aeronavegabilidad.
URL base: https://www.aerocivil.gov.co

DESHABILITADO del run diario (ver populate_db.DISABLED_SOURCES), verificado
2026-07-28: dos de las cuatro URLs candidatas son 404 disfrazados de 200 (el
sitio devuelve su pagina de error con status 200); las otras dos SI cargan
pero son navegacion/contenido administrativo, no Directivas de Aeronavegabilidad
— el listado real se mudo a SharePoint
(.../certificacion-y-licenciamiento/directivas de aeronavegabilidad/Forms/AllItems.aspx),
que el WAF del sitio bloquea con 403 ante CUALQUIER request simple (headers de
navegador real incluidos; la REST API de SharePoint tambien 403). Necesita
browser real (Playwright) para pasar el WAF, y aun asi no esta garantizado.
"""

import re
import requests
from bs4 import BeautifulSoup

# La primera es la ubicacion REAL del listado (SharePoint, bloqueada por WAF
# hoy — ver docstring); las demas son intentos historicos que ya no sirven
# (404 disfrazado de 200, o contenido administrativo sin relacion con DAs).
# Se dejan para no perder el rastro si algun dia el WAF deja pasar un GET.
AEROCIVIL_URLS = [
    "https://www.aerocivil.gov.co/autoridad-de-la-aviacion-civil/certificacion-y-licenciamiento/"
    "directivas%20de%20aeronavegabilidad/forms/allitems.aspx",
    "https://www.aerocivil.gov.co/autoridad/autoridad-tecnica/aeronavegabilidad/directivas-de-aeronavegabilidad",
    "https://www.aerocivil.gov.co/normatividad/normas-de-aeronavegabilidad",
    "https://www.aerocivil.gov.co/normatividad/16-directiva",
    "https://www.aerocivil.gov.co/documentos/377/directivas/",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-CO,es;q=0.9,en-US;q=0.8,en;q=0.5",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


def search_ads(keyword=None, make=None, model=None, date_from=None, date_to=None, max_results=200):
    """
    Busca Directivas de Aeronavegabilidad en AEROCIVIL Colombia.
    Usa Session con headers de navegador para evitar bloqueos.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    for url in AEROCIVIL_URLS:
        try:
            resp = session.get(url, timeout=15, allow_redirects=True)
            if resp.status_code == 403:
                print(f"[AEROCIVIL Colombia] 403 Forbidden en {url}")
                continue
            if resp.status_code >= 400:
                print(f"[AEROCIVIL Colombia] HTTP {resp.status_code} en {url}")
                continue
            resp.raise_for_status()
            results = _parse_page(resp.text, keyword, make, model)
            if results:
                results = results[:max_results]
                print(f"[AEROCIVIL Colombia] {len(results)} DAs obtenidas de {url}")
                return results
        except requests.Timeout:
            print(f"[AEROCIVIL Colombia] Timeout en {url}")
            continue
        except Exception as e:
            print(f"[AEROCIVIL Colombia] Error en {url}: {e}")
            continue

    print("[AEROCIVIL Colombia] Sitio no disponible — todas las URLs fallaron")
    return []


def _parse_page(html, keyword, make, model):
    soup = BeautifulSoup(html, "html.parser")
    results = []

    for a in soup.find_all("a", href=True):
        href = a["href"]
        text = a.get_text(strip=True)
        if not text or len(text) < 15:
            continue

        is_pdf = href.lower().endswith(".pdf")
        has_ad_pattern = bool(re.search(
            r"(DA[\s-]+\d|aeronavegabilidad|airworthiness|AD[\s-]+\d)", text, re.I
        ))
        has_pdf_ad = is_pdf and bool(re.search(
            r"(da[-_]|ad[-_]|directiva[-_])", href, re.I
        ))

        if not has_ad_pattern and not has_pdf_ad:
            continue

        ad_number = _extract_ad_number(text)
        if not ad_number:
            continue

        if not href.startswith("http"):
            href = "https://www.aerocivil.gov.co" + href

        ad = {
            "Source": "AEROCIVIL Colombia",
            "AD_Number": ad_number.strip(),
            "Make": _extract_make(text),
            "Model": "",
            "Subject": text.strip(),
            "Effective_Date": _extract_date(text),
            "PDF_Link": href if is_pdf else "",
            "Product_Type": "",
            "Status": "Final Rule",
            "Docket": "",
        }

        if _matches_filters(ad, keyword, make, model):
            results.append(ad)

    return results


def _extract_ad_number(text):
    m = re.search(r"(DA[\s-]*\d[\d\w\-./]*)", text, re.I)
    if m:
        return m.group(1).strip()
    m = re.search(r"(Directiva\s*\d[\d\w\-./]*)", text, re.I)
    if m:
        return m.group(1).strip()
    return ""


def _extract_date(text):
    m = re.search(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})", text)
    if m:
        return f"{m.group(3)}-{m.group(2).zfill(2)}-{m.group(1).zfill(2)}"
    return ""


def _extract_make(text):
    known = ["Boeing", "Airbus", "Cessna", "Piper", "Beechcraft", "Bell", "Sikorsky", "Embraer"]
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
