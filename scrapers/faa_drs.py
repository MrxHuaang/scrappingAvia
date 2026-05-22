"""
Scraper para FAA DRS (Dynamic Regulatory System) - drs.faa.gov

DRS es un SPA JavaScript con backend protegido por Akamai. La estrategia:
1. Abrir el listado con Playwright headless.
2. Interceptar las XHRs que el SPA hace para poblar la lista de ADs.
3. Replicar esas calls con httpx usando cookies/headers capturados.
4. Para cada DOCID, fetch detalle y parsear con BeautifulSoup.

Objetivo: ADs pre-1995 que GovInfo no cubre (DOCIDs hex legacy).

Si Akamai bloquea o Playwright no esta instalado, retorna [] sin fallar
el pipeline (best-effort). Loguea con prefijo [DRS].
"""

import re
import time
from datetime import datetime
from urllib.parse import urljoin

from utils import extract_make_model, normalize_ad_number


DRS_BASE = "https://drs.faa.gov"
DRS_AD_LIST = f"{DRS_BASE}/browse/ADFRAWD/doctypeDetails"
DRS_AD_EMERGENCY = f"{DRS_BASE}/browse/ADFREAD/doctypeDetails"

PRE_GOVINFO_CUTOFF = datetime(1995, 1, 1)


def _import_playwright():
    """Import diferido para no fallar si playwright no está instalado."""
    try:
        from playwright.sync_api import sync_playwright
        return sync_playwright
    except ImportError:
        print("[DRS] playwright no instalado - skip. Instalar con: pip install playwright && playwright install chromium")
        return None


def _import_bs4():
    try:
        from bs4 import BeautifulSoup
        return BeautifulSoup
    except ImportError:
        return None


def search_ads(max_results: int = 10000, pre_govinfo_only: bool = True) -> list[dict]:
    """
    Scrape de DRS. pre_govinfo_only filtra ADs anteriores a 1995 (los unicos que
    GovInfo no cubre), evitando duplicados.

    Retorna [] silenciosamente si Playwright no esta disponible o si Akamai bloquea.
    """
    sync_playwright = _import_playwright()
    if not sync_playwright:
        return []

    BeautifulSoup = _import_bs4()
    if not BeautifulSoup:
        print("[DRS] bs4 no disponible - skip")
        return []

    captured_requests: list[dict] = []
    listing_html_pages: list[str] = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            context = browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/131.0.0.0 Safari/537.36",
            )
            page = context.new_page()

            # Listener de network para identificar el endpoint XHR del listado
            def on_request(req):
                url = req.url
                if "drs.faa.gov" in url and (
                    "list" in url.lower() or "search" in url.lower() or "browse" in url.lower()
                ):
                    captured_requests.append({
                        "url": url,
                        "method": req.method,
                        "headers": req.headers,
                    })

            page.on("request", on_request)

            print(f"[DRS] Abriendo {DRS_AD_LIST}...")
            try:
                page.goto(DRS_AD_LIST, wait_until="networkidle", timeout=60000)
            except Exception as e:
                print(f"[DRS] Timeout / error en goto: {str(e).encode('ascii','replace').decode()}")
                browser.close()
                return []

            # Verificar bloqueo Akamai (titulo o body con 'Access Denied')
            content = page.content()
            if "Access Denied" in content or "Reference #" in content[:5000]:
                print("[DRS] Bloqueado por Akamai (Access Denied) - skip")
                browser.close()
                return []

            listing_html_pages.append(content)

            # Intentar paginar: buscar boton 'Next' o scroll infinito
            for click_attempt in range(50):  # tope 50 paginas
                try:
                    next_btn = page.query_selector("button[aria-label*='Next']") \
                               or page.query_selector("a[aria-label*='Next']") \
                               or page.query_selector("button:has-text('Next')")
                    if not next_btn:
                        break
                    if not next_btn.is_enabled():
                        break
                    next_btn.click()
                    page.wait_for_load_state("networkidle", timeout=30000)
                    listing_html_pages.append(page.content())
                    time.sleep(0.5)
                except Exception as e:
                    print(f"[DRS] Paginacion detenida en click {click_attempt}: {str(e).encode('ascii','replace').decode()}")
                    break

            browser.close()

    except Exception as e:
        msg = str(e).encode("ascii", errors="replace").decode("ascii")
        print(f"[DRS] Error fatal Playwright: {msg}")
        return []

    # Parsear todas las paginas HTML acumuladas
    results: list[dict] = []
    for html in listing_html_pages:
        results.extend(_parse_listing_html(html, BeautifulSoup))

    # Dedupe por ad_number
    seen = set()
    deduped = []
    for r in results:
        k = r["AD_Number"]
        if k and k not in seen:
            seen.add(k)
            deduped.append(r)

    if pre_govinfo_only:
        deduped = [r for r in deduped if _is_pre_govinfo(r.get("Effective_Date"))]

    if len(deduped) > max_results:
        deduped = deduped[:max_results]

    print(f"[DRS] Total {'pre-1995' if pre_govinfo_only else 'all'}: {len(deduped)} ADs")
    return deduped


def _is_pre_govinfo(date_str: str) -> bool:
    """True si la fecha es anterior a 1995-01-01."""
    if not date_str:
        return False
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%d-%b-%Y", "%B %d, %Y"):
        try:
            d = datetime.strptime(date_str.strip(), fmt)
            return d < PRE_GOVINFO_CUTOFF
        except ValueError:
            continue
    return False


def _parse_listing_html(html: str, BeautifulSoup) -> list[dict]:
    """
    Parser defensivo: DRS renderiza la tabla via JS pero a veces el HTML
    contiene las filas tras hidratacion. Extraemos cualquier link a un AD detail.
    """
    soup = BeautifulSoup(html, "lxml")
    results = []

    # Links a detalle de AD: patron ...excelExternalWindow/{DOCID}.0001
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "excelExternalWindow" not in href and "DocumentDetail" not in href:
            continue

        text = a.get_text(strip=True)
        ad_number = ""
        m = re.search(r"(AD\s*\d{2,4}[-–]\d{2}[-–]\d{2,4})", text, re.I)
        if m:
            ad_number = m.group(1)

        if not ad_number:
            continue

        ad_number = normalize_ad_number(ad_number)
        full_url = urljoin(DRS_BASE, href)

        # Intentar leer fecha de fila padre
        date_str = ""
        parent = a.find_parent("tr") or a.find_parent("div")
        if parent:
            date_m = re.search(r"(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})", parent.get_text())
            if date_m:
                date_str = date_m.group(1)

        make, model = extract_make_model(text)

        results.append({
            "Source": "FAA (DRS)",
            "AD_Number": ad_number,
            "Subject": text,
            "Make": make,
            "Model": model,
            "Effective_Date": date_str,
            "Status": "Final Rule",
            "Product_Type": "Aircraft",
            "Docket": "",
            "PDF_Link": full_url,
        })

    return results
