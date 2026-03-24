"""
Scraper para FAA Dynamic Regulatory System (DRS)
Obtiene Airworthiness Directives usando la API REST interna.

La API requiere un JWT token que se obtiene al cargar la página inicialmente.
También necesita cookies de Akamai bot management.
"""

import requests
import time
import re
import json


FAA_BASE_URL = "https://drs.faa.gov"
FAA_API_URL = f"{FAA_BASE_URL}/api/browse/doctype/ADFRAWD"
FAA_PAGE_URL = f"{FAA_BASE_URL}/browse/ADFRAWD/doctypeDetails"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Origin": FAA_BASE_URL,
    "Referer": FAA_PAGE_URL,
    "sec-ch-ua": '"Chromium";v="131", "Not_A Brand";v="24"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}


def _create_session():
    """
    Crea una sesión HTTP que simula un navegador real.
    Primero carga la página principal para obtener cookies y JWT.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    try:
        # Paso 1: Cargar la página principal para obtener cookies de Akamai
        print("[FAA] Inicializando sesión...")
        resp = session.get(
            FAA_BASE_URL,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            timeout=30,
        )

        # Paso 2: Cargar la página de búsqueda de ADs
        resp = session.get(
            FAA_PAGE_URL,
            headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"},
            timeout=30,
        )

        # Paso 3: Buscar el JWT token en la respuesta o en los scripts
        jwt_token = None
        text = resp.text

        # Buscar token en scripts inline o en la configuración de Angular
        jwt_patterns = [
            r'"jwt"\s*:\s*"([^"]+)"',
            r"jwt['\"]?\s*[:=]\s*['\"]([^'\"]+)['\"]",
            r'"token"\s*:\s*"([^"]+)"',
            r"Bearer\s+([A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+\.[A-Za-z0-9\-_]+)",
        ]
        for pattern in jwt_patterns:
            match = re.search(pattern, text)
            if match:
                jwt_token = match.group(1)
                break

        # Paso 4: Intentar obtener el JWT del endpoint de autenticación
        if not jwt_token:
            try:
                auth_resp = session.get(
                    f"{FAA_BASE_URL}/api/auth/token",
                    timeout=15,
                )
                if auth_resp.status_code == 200:
                    auth_data = auth_resp.json() if auth_resp.headers.get("content-type", "").startswith("application/json") else {}
                    jwt_token = auth_data.get("token", auth_data.get("jwt", ""))
            except Exception:
                pass

        if jwt_token:
            session.headers["jwt"] = jwt_token
            session.headers["user"] = "G"
            print("[FAA] Sesión inicializada con JWT.")
        else:
            # Continuar sin JWT, la sesión con cookies podría funcionar
            session.headers["user"] = "G"
            print("[FAA] Sesión inicializada sin JWT (algunas funciones limitadas).")

        return session

    except requests.RequestException as e:
        print(f"[FAA] Error inicializando sesión: {e}")
        session.headers["user"] = "G"
        return session


def get_makes(session=None):
    """Retorna la lista de fabricantes (Makes) disponibles."""
    if session is None:
        session = _create_session()

    url = f"{FAA_API_URL}/metadatas/filtered"
    payload = {"filters": {}}

    try:
        resp = session.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        filters = data.get("filters", data.get("metadatas", {}))
        if isinstance(filters, dict):
            makes = filters.get("Make", [])
            if isinstance(makes, list):
                return [m.get("value", m) if isinstance(m, dict) else m for m in makes], session

        return [], session
    except requests.RequestException as e:
        print(f"[FAA] Error obteniendo filtros: {e}")
        return [], session


def get_models_for_make(make, session=None):
    """Obtiene los modelos disponibles para un fabricante específico."""
    if session is None:
        session = _create_session()

    url = f"{FAA_API_URL}/metadatas/filtered"
    payload = {"filters": {"Make": [make]}, "lastFilter": "Make"}

    try:
        resp = session.post(url, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        filters = data.get("filters", data.get("metadatas", {}))
        if isinstance(filters, dict):
            models = filters.get("Model", [])
            if isinstance(models, list):
                return [m.get("value", m) if isinstance(m, dict) else m for m in models], session

        return [], session
    except requests.RequestException as e:
        print(f"[FAA] Error obteniendo modelos para {make}: {e}")
        return [], session


def search_ads(make=None, model=None, product_type=None, status=None, page_size=100, session=None):
    """
    Busca ADs en la FAA con los filtros especificados.
    
    Si la API da 403, intenta scraping de la página HTML como fallback.
    """
    if session is None:
        session = _create_session()

    # Intentar API primero
    results = _search_via_api(session, make, model, product_type, status, page_size)

    if results is not None:
        return results

    # Fallback: scraping de la página "Save Results List"
    print("[FAA] API bloqueada, intentando scraping HTML...")
    return _search_via_html(session, make, model, product_type)


def _search_via_api(session, make, model, product_type, status, page_size):
    """Busca ADs via la API REST."""
    url = f"{FAA_API_URL}/documents/metadatas"

    filters = {}
    if make:
        filters["Make"] = [make] if isinstance(make, str) else make
    if model:
        filters["Model"] = [model] if isinstance(model, str) else model
    if product_type:
        filters["Product Type"] = [product_type] if isinstance(product_type, str) else product_type
    if status:
        filters["Status"] = [status] if isinstance(status, str) else status

    if not filters.get("Status"):
        filters["Status"] = ["Current"]

    payload = {
        "filters": filters,
        "lastFilter": list(filters.keys())[-1] if filters else "",
        "sort": {"sortField": "Citation Publish Date", "sortOrder": "desc"},
        "page": {"currentPage": 1, "pageSize": page_size},
    }

    all_results = []
    current_page = 1

    while True:
        payload["page"]["currentPage"] = current_page
        print(f"[FAA] API - Buscando página {current_page}...", end=" ")

        try:
            resp = session.post(url, json=payload, timeout=60)

            if resp.status_code == 403:
                print("Bloqueado (403).")
                return None  # Signal to try fallback

            resp.raise_for_status()
            data = resp.json()

            documents = data.get("documentList", data.get("documents", []))
            if not documents:
                print("Sin resultados.")
                break

            total = data.get("totalCount", data.get("total", len(documents)))
            print(f"Encontrados {len(documents)} de {total} resultados.")

            for doc in documents:
                ad = _parse_faa_document(doc)
                if ad:
                    all_results.append(ad)

            if len(all_results) >= total or len(documents) < page_size:
                break

            current_page += 1
            time.sleep(0.5)

        except requests.RequestException as e:
            print(f"Error: {e}")
            if not all_results:
                return None  # Try fallback
            break

    print(f"[FAA] Total ADs obtenidos: {len(all_results)}")
    return all_results


def _search_via_html(session, make, model, product_type):
    """Fallback: busca ADs scrapeando la página HTML directamente."""
    from bs4 import BeautifulSoup

    # Construir URL con parámetros de filtro
    params = []
    if make:
        params.append(f"Make={requests.utils.quote(make)}")
    if model:
        params.append(f"Model={requests.utils.quote(model)}")
    if product_type:
        params.append(f"Product%20Type={requests.utils.quote(product_type)}")
    params.append("Status=Current")

    url = f"{FAA_PAGE_URL}?{'&'.join(params)}"

    try:
        resp = session.get(url, timeout=30)
        resp.raise_for_status()

        # La página es una SPA Angular, por lo que el HTML no tendrá datos de resultados
        # En este caso, informamos al usuario que necesita usar la exportación directa
        print("[FAA] La página es una SPA Angular y no se puede scrapear estáticamente.")
        print("[FAA] Recomendación: Usar la opción 'Save Results List' directamente en el navegador.")
        print(f"[FAA] URL para abrir en navegador: {url}")

        return []

    except requests.RequestException as e:
        print(f"[FAA] Error en scraping HTML: {e}")
        return []


def _parse_faa_document(doc):
    """Parsea un documento de la API FAA a formato estándar."""
    try:
        if isinstance(doc, dict):
            metadata = doc.get("metadata", doc)

            ad_number = (
                metadata.get("adNumber", "")
                or metadata.get("AD Number", "")
                or doc.get("documentNumber", "")
                or doc.get("name", "")
            )
            subject = (
                metadata.get("subjectHeading", "")
                or metadata.get("Subject Heading", "")
                or metadata.get("subject", "")
                or metadata.get("Subject", "")
            )
            make = metadata.get("make", "") or metadata.get("Make", "")
            model = metadata.get("model", "") or metadata.get("Model", "")
            effective_date = metadata.get("effectiveDate", "") or metadata.get("Effective Date", "")
            status = metadata.get("status", "") or metadata.get("Status", "")
            product_type = metadata.get("productType", "") or metadata.get("Product Type", "")
            docket = metadata.get("docketNumber", "") or metadata.get("Docket Number", "")

            pdf_link = ""
            attachments = doc.get("attachments", doc.get("downloadDetails", []))
            if isinstance(attachments, list) and attachments:
                pdf_link = attachments[0].get("url", attachments[0].get("link", ""))
                if pdf_link and not pdf_link.startswith("http"):
                    pdf_link = f"{FAA_BASE_URL}{pdf_link}"
            elif isinstance(doc.get("documentNumber"), str):
                pdf_link = f"{FAA_BASE_URL}/search/document/{doc['documentNumber']}"

            return {
                "Source": "FAA",
                "AD_Number": str(ad_number).strip(),
                "Subject": str(subject).strip(),
                "Make": str(make).strip(),
                "Model": str(model).strip(),
                "Effective_Date": str(effective_date).strip(),
                "Status": str(status).strip(),
                "Product_Type": str(product_type).strip(),
                "Docket": str(docket).strip(),
                "PDF_Link": str(pdf_link).strip(),
            }
    except Exception as e:
        print(f"[FAA] Error parseando documento: {e}")

    return None


if __name__ == "__main__":
    print("=== FAA AD Scraper - Test ===")
    session = _create_session()

    print("\nObteniendo fabricantes disponibles...")
    makes, session = get_makes(session)
    if makes:
        print(f"Fabricantes encontrados: {len(makes)}")
        for m in makes[:10]:
            print(f"  - {m}")
    else:
        print("No se pudieron obtener los fabricantes (API protegida por Akamai).")
        print("Intentando búsqueda directa...")
        results = search_ads(make="The Boeing Company", session=session)
        if results:
            for r in results[:5]:
                print(f"  AD: {r['AD_Number']} - {r['Subject'][:80]}")
        else:
            print("La API de FAA está bloqueando las solicitudes.")
            print("Use la opción de exportar directamente desde el navegador.")
