"""
Scraper para Federal Register API - Airworthiness Directives (FAA)
Reemplaza al scraper directo de FAA DRS que esta bloqueado por Akamai.

La API del Federal Register es gratuita, no requiere API key,
y contiene todos los ADs publicados oficialmente bajo 14 CFR Part 39.

Endpoint: https://www.federalregister.gov/api/v1/documents.json
Documentacion: https://www.federalregister.gov/developers/documentation/api/v1
"""

import requests
import re
import time


FEDERAL_REGISTER_API = "https://www.federalregister.gov/api/v1/documents.json"

HEADERS = {
    "User-Agent": "AD-Scraper/1.0",
    "Accept": "application/json",
}


def search_ads(keyword=None, make=None, model=None, product_type=None, date_from=None, date_to=None, max_pages=10):
    """
    Busca Airworthiness Directives en el Federal Register.

    Args:
        keyword: Palabra clave general
        make: Fabricante (ej: Boeing, Airbus)
        model: Modelo (ej: 737, A320)
        product_type: Tipo de producto (Aircraft, Engine, Propeller)
        date_from: Fecha inicio (YYYY-MM-DD)
        date_to: Fecha fin (YYYY-MM-DD)
        max_pages: Maximo de paginas a recorrer

    Returns:
        Lista de diccionarios con los ADs encontrados.
    """
    # Construir termino de busqueda
    search_terms = []
    if keyword:
        search_terms.append(keyword)
    if make:
        search_terms.append(make)
    if model:
        search_terms.append(model)
    if product_type:
        search_terms.append(product_type)

    search_query = " ".join(search_terms) if search_terms else None

    all_results = []
    page = 1

    while page <= max_pages:
        print(f"[Federal Register] Buscando pagina {page}...", end=" ")

        params = {
            "conditions[cfr][title]": "14",
            "conditions[cfr][part]": "39",
            "conditions[type][]": "RULE",
            "per_page": 100,
            "page": page,
            "order": "newest",
            "fields[]": [
                "title", "abstract", "document_number", "publication_date",
                "effective_on", "pdf_url", "html_url", "agencies",
                "cfr_references", "action", "type"
            ],
        }

        if search_query:
            params["conditions[term]"] = search_query

        if date_from:
            params["conditions[effective_date][gte]"] = date_from
        if date_to:
            params["conditions[effective_date][lte]"] = date_to

        try:
            resp = requests.get(
                FEDERAL_REGISTER_API,
                params=params,
                headers=HEADERS,
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            documents = data.get("results", [])
            total = data.get("count", 0)

            if not documents:
                print("Sin mas resultados.")
                break

            print(f"Encontrados {len(documents)} de {total} total.")

            for doc in documents:
                ad = _parse_document(doc, make, model)
                if ad:
                    all_results.append(ad)

            # Verificar si hay mas paginas
            next_url = data.get("next_page_url")
            if not next_url or len(all_results) >= total:
                break

            page += 1
            time.sleep(0.3)

        except requests.RequestException as e:
            print(f"Error: {e}")
            break

    print(f"[Federal Register] Total ADs obtenidos: {len(all_results)}")
    return all_results


def _parse_document(doc, filter_make=None, filter_model=None):
    """Parsea un documento del Federal Register a formato estandar."""
    try:
        title = doc.get("title", "")
        abstract = doc.get("abstract", "") or ""

        # Extraer numero de AD del titulo
        ad_number = ""
        ad_match = re.search(r"(AD\s*\d{4}[-–]\d{2}[-–]\d{2,4})", title, re.I)
        if ad_match:
            ad_number = ad_match.group(1).replace("\u2013", "-")
        else:
            # Intentar con otro formato
            ad_match = re.search(r"(\d{4}[-–]\d{2}[-–]\d{2,4})", title)
            if ad_match:
                ad_number = ad_match.group(1).replace("\u2013", "-")
            else:
                ad_number = doc.get("document_number", "")

        # Extraer fabricante y modelo del titulo/abstract
        make, model = _extract_make_model(title, abstract)

        # Extraer subject
        subject = title
        # Limpiar prefijo comun de AD titles
        subject = re.sub(r"^Airworthiness Directives[;:]\s*", "", subject, flags=re.I)

        effective_date = doc.get("effective_on", "") or doc.get("publication_date", "")
        pdf_link = doc.get("pdf_url", "") or doc.get("html_url", "")

        # FIXED: Determinar status real desde el campo "type" de la API y el titulo/action,
        # en lugar de hardcodear "Final Rule".
        doc_type = (doc.get("type") or "").upper()       # e.g. "RULE", "PROPOSED_RULE", "NOTICE"
        action_field = (doc.get("action") or "").lower() # e.g. "final rule", "notice of proposed rule..."
        title_lower = title.lower()
        abstract_lower = abstract.lower()

        if doc_type == "PROPOSED_RULE" or "proposed" in title_lower or "nprm" in title_lower or "proposed" in action_field:
            status = "NPRM"
        elif "emergency" in title_lower or "emergency" in action_field:
            status = "Emergency AD"
        elif "correction" in title_lower or "correcting amendment" in title_lower or "correction" in action_field:
            status = "Correction"
        elif "interim" in title_lower or "interim" in action_field:
            status = "Interim Final Rule"
        elif doc_type == "RULE" or "final rule" in action_field:
            status = "Final Rule"
        else:
            status = "Final Rule"  # Fallback conservador

        print(f"[Federal Register] DEBUG status: doc_type={doc_type!r} action={action_field[:40]!r} -> {status}")

        # FIXED: Extraer product_type desde cfr_references (part 39 = airworthiness), titulo y abstract.
        # La API retorna cfr_references como lista de dicts con {title, part, chapter...}.
        cfr_refs = doc.get("cfr_references") or []
        product_type = ""

        # Intentar extraer del titulo/abstract primero (más específico)
        combined = f"{title_lower} {abstract_lower}"
        if re.search(r"\bengine\b", combined):
            product_type = "Engine"
        elif re.search(r"\bpropeller\b", combined):
            product_type = "Propeller"
        elif re.search(r"\bappliance\b", combined):
            product_type = "Appliance"
        elif re.search(r"\baircraft\b|\bairplane\b|\bhelicopter\b|\bairship\b|\bglider\b", combined):
            product_type = "Aircraft"

        # Si no se pudo inferir del texto, usar la parte del CFR como pista
        if not product_type and cfr_refs:
            for ref in cfr_refs:
                part = str(ref.get("part", "") if isinstance(ref, dict) else "")
                if part == "39":
                    product_type = "Aircraft"  # 14 CFR Part 39 = Airworthiness Directives (general)
                    break

        print(f"[Federal Register] DEBUG product_type: cfr_parts={[r.get('part') if isinstance(r,dict) else r for r in cfr_refs]} -> {product_type!r}")

        result = {
            "Source": "FAA (Federal Register)",
            "AD_Number": ad_number.strip(),
            "Subject": subject.strip(),
            "Make": make.strip(),
            "Model": model.strip(),
            "Effective_Date": effective_date,
            "Status": status,           # FIXED: ya no hardcodeado
            "Product_Type": product_type,  # FIXED: ya no hardcodeado a ""
            "Docket": doc.get("document_number", ""),
            "PDF_Link": pdf_link,
        }

        return result

    except Exception as e:
        print(f"[Federal Register] Error parseando documento: {e}")
        return None


def _extract_make_model(title, abstract):
    """Extrae fabricante y modelo del titulo y abstract de un AD."""
    text = f"{title} {abstract}"

    # Fabricantes comunes en aviacion
    known_makes = [
        "The Boeing Company", "Boeing", "Airbus", "Airbus S.A.S.",
        "Bombardier", "Embraer", "Cessna", "Textron Aviation",
        "Gulfstream", "Dassault", "Pilatus", "Beechcraft",
        "Piper", "Mooney", "Cirrus", "Diamond",
        "Rolls-Royce", "General Electric", "Pratt & Whitney",
        "CFM International", "Honeywell", "Safran",
        "Bell", "Sikorsky", "Leonardo", "Robinson",
        "de Havilland", "ATR", "Saab", "BAE Systems",
    ]

    make = ""
    for known in known_makes:
        if known.lower() in text.lower():
            make = known
            break

    # Extraer modelo - patron comun: "Model XXX"
    model = ""
    model_match = re.search(r"Model\s+([\w\-/]+(?:\s+[\w\-/]+)?)", text)
    if model_match:
        model = model_match.group(1)
    else:
        # Intentar patrones especificos de aviones
        patterns = [
            r"\b(737[-\s]?\d{0,4}(?:[-/]\d+)?(?:\s*(?:MAX|NG))?)\b",
            r"\b(A\s?3[0-9]{2}(?:[-/]\d+)?(?:neo)?)\b",
            r"\b(777[-\s]?\d{0,4})\b",
            r"\b(787[-\s]?\d{0,4})\b",
            r"\b(747[-\s]?\d{0,4})\b",
            r"\b(CL[-\s]?\d{3,4})\b",
            r"\b(DHC[-\s]?\d+)\b",
        ]
        for pattern in patterns:
            match = re.search(pattern, text, re.I)
            if match:
                model = match.group(1)
                break

    return make, model


def get_recent_ads(days=30, max_results=200):
    """Obtiene los ADs mas recientes del Federal Register."""
    from datetime import datetime, timedelta

    date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")

    return search_ads(date_from=date_from, max_pages=max_results // 100 + 1)


