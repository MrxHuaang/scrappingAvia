"""
Scraper para ANAC Brasil (Agência Nacional de Aviação Civil)
Obtiene Airworthiness Directives (DA - Diretriz de Aeronavegabilidade)
desde sistemas.anac.gov.br/certificacao/DA/

Estructura del sitio:
- DAE.asp          → Página principal con índice de opciones
- DA_NumE.asp      → Listado por número (con filtro por rango de años)
- DA_ProdE.asp     → Listado por fabricante/producto
- DA_Last2E.asp    → ADs emitidas en las últimas 2 semanas
- DA_DetailE.asp   → Detalle de un AD individual (?Emd=<id>)

El sitio usa HTML clásico sin JavaScript obligatorio ni ViewState de ASP.NET.
Las páginas de listado tienen tablas HTML simples con links a detalles.
"""

import requests
from bs4 import BeautifulSoup
import re
import time


ANAC_BASE_URL = "https://sistemas.anac.gov.br/certificacao/DA"
ANAC_LIST_BY_NUM_URL = f"{ANAC_BASE_URL}/DA_NumE.asp"
ANAC_LIST_BY_PROD_URL = f"{ANAC_BASE_URL}/DA_ProdE.asp"
ANAC_RECENT_URL = f"{ANAC_BASE_URL}/DA_Last2E.asp"
ANAC_DETAIL_URL = f"{ANAC_BASE_URL}/DA_DetailE.asp"

# Rangos de años disponibles en el sitio
YEAR_RANGES = [
    "2021-2025",
    "2016-2020",
    "2011-2015",
    "2006-2010",
    "2001-2005",
    "1996-2000",
]

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "pt-BR,pt;q=0.9,en-US;q=0.8,en;q=0.5",
    "Referer": "https://sistemas.anac.gov.br/certificacao/DA/DAE.asp",
}


# ---------------------------------------------------------------------------
# Función principal de búsqueda
# ---------------------------------------------------------------------------

def search_ads(
    keyword=None,
    make=None,
    model=None,
    date_from=None,
    date_to=None,
    year_range=None,
    max_results=200,
):
    """
    Busca Airworthiness Directives de ANAC Brasil.

    Estrategia:
      - Si se pasa make/model → busca en el índice por fabricante (DA_ProdE)
      - Si se pasa year_range  → busca en el índice por número de ese rango
      - Sin filtros             → trae las más recientes (DA_Last2E) y
                                  complementa con el rango más actual

    Args:
        keyword:    Palabra clave a filtrar en el subject/título
        make:       Fabricante (ej: "Embraer", "Boeing")
        model:      Modelo de aeronave (ej: "ERJ 170")
        date_from:  Fecha inicio YYYY-MM-DD (filtro local sobre effective_date)
        date_to:    Fecha fin   YYYY-MM-DD (filtro local)
        year_range: Rango a consultar, p.ej. "2021-2025". None = más reciente.
        max_results: Límite de resultados a retornar.

    Returns:
        Lista de dicts en el formato estándar del proyecto.
    """
    session = requests.Session()
    session.headers.update(HEADERS)

    all_results = []

    # --- Ruta 1: buscar por fabricante/producto ---
    if make:
        print(f"[ANAC Brasil] Buscando por fabricante: {make}...")
        results = _search_by_manufacturer(session, make, model, keyword)
        all_results.extend(results)

    # --- Ruta 2: buscar por rango de años ---
    elif year_range:
        print(f"[ANAC Brasil] Buscando en rango {year_range}...")
        results = _fetch_list_by_number(session, year_range)
        all_results.extend(results)

    # --- Ruta 3: sin filtros → recientes + rango más actual ---
    else:
        print("[ANAC Brasil] Buscando ADs recientes...")
        recent = _fetch_recent(session)
        all_results.extend(recent)

        if len(all_results) < max_results:
            print("[ANAC Brasil] Complementando con rango 2021-2025...")
            listed = _fetch_list_by_number(session, "2021-2025")
            # Deduplicar por AD_Number
            existing_nums = {r["AD_Number"] for r in all_results}
            for r in listed:
                if r["AD_Number"] not in existing_nums:
                    all_results.append(r)
                    existing_nums.add(r["AD_Number"])

    # --- Filtros locales ---
    if keyword:
        kw = keyword.lower()
        all_results = [
            r for r in all_results
            if kw in r.get("Subject", "").lower()
            or kw in r.get("Make", "").lower()
            or kw in r.get("Model", "").lower()
            or kw in r.get("AD_Number", "").lower()
        ]

    if date_from:
        all_results = [
            r for r in all_results
            if r.get("Effective_Date", "") >= date_from
        ]

    if date_to:
        all_results = [
            r for r in all_results
            if r.get("Effective_Date", "") <= date_to
        ]

    all_results = all_results[:max_results]
    print(f"[ANAC Brasil] Total ADs obtenidos: {len(all_results)}")
    return all_results


# ---------------------------------------------------------------------------
# Scraper de la página de ADs recientes (últimas 2 semanas)
# ---------------------------------------------------------------------------

def _fetch_recent(session):
    """Obtiene los ADs emitidos en las últimas 2 semanas."""
    try:
        resp = session.get(
            ANAC_RECENT_URL,
            params={"St": "N"},
            timeout=30,
        )
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "latin-1"
        soup = BeautifulSoup(resp.text, "lxml")
        return _parse_list_table(soup, source_tag="recent")
    except requests.RequestException as e:
        print(f"[ANAC Brasil] Error en recientes: {e}")
        return []


# ---------------------------------------------------------------------------
# Scraper del índice por número (con rango de años)
# ---------------------------------------------------------------------------

def _fetch_list_by_number(session, year_range="2021-2025"):
    """
    Obtiene el listado de ADs para un rango de años dado.
    URL: DA_NumE.asp?Diretriz=2021-2025
    """
    try:
        resp = session.get(
            ANAC_LIST_BY_NUM_URL,
            params={"Diretriz": year_range},
            timeout=30,
        )
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "latin-1"
        soup = BeautifulSoup(resp.text, "lxml")
        results = _parse_list_table(soup, source_tag="list_num")
        print(f"[ANAC Brasil] Rango {year_range}: {len(results)} ADs encontrados.")
        return results
    except requests.RequestException as e:
        print(f"[ANAC Brasil] Error en listado {year_range}: {e}")
        return []


# ---------------------------------------------------------------------------
# Scraper del índice por fabricante/producto
# ---------------------------------------------------------------------------

def _search_by_manufacturer(session, make, model=None, keyword=None):
    """
    Descartamos el camino del abecedario DA_ProdE.asp por su arquitectura obsoleta 
    llena de subtablas de modelos vacías e inaccesibles, procedemos con un dump del listado general
    por rangos y ejecutamos filtrado en memoria (Fallback escalado).
    """
    print(f"[ANAC Brasil] Delegando búsqueda de fabricante '{make}' a listado general cronológico (Fallback Mejorado).")
    results = []
    
    # Buscamos en los dos rangos históricos más recientes prioritariamente para no saturar al servidor viejo
    for year_range in ["2021-2025", "2016-2020"]:
        batch = _fetch_list_by_number(session, year_range)
        results.extend(batch)
        
    make_lower = make.lower()
    filtered_results = []
    
    for r in results:
        # Se verifica si 'make' conincide estricitamente
        if make_lower in r.get("Make", "").lower() or make_lower in r.get("Subject", "").lower():
            filtered_results.append(r)
            
    # Filtramos recursivamente por modelo si existe
    if model:
        model_lower = model.lower()
        filtered_results = [
            r for r in filtered_results
            if model_lower in r.get("Model", "").lower()
            or model_lower in r.get("Subject", "").lower()
        ]
        
    print(f"[ANAC Brasil] {len(filtered_results)} ADs filtrados en memoria para '{make}'.")
    return filtered_results


# ---------------------------------------------------------------------------
# Parser de tablas de listado
# ---------------------------------------------------------------------------

def _parse_list_table(soup, source_tag=""):
    """
    Parsea una tabla HTML de listado de ADs del sitio ANAC Brasil.

    El sitio tiene tablas con columnas típicas:
      DA Number | Subject / Applicability | Effective Date | PDF / Detail link

    Como el HTML puede variar entre páginas, el parser es defensivo:
    primero intenta identificar columnas por encabezado, luego por posición.
    """
    results = []

    # Buscar todas las tablas
    tables = soup.find_all("table")
    if not tables:
        return results

    # Tomar la tabla con más filas (la tabla de datos principal)
    main_table = max(tables, key=lambda t: len(t.find_all("tr")))
    rows = main_table.find_all("tr")

    if not rows:
        return results

    # --- Detectar índices de columnas por encabezado ---
    header_row = rows[0]
    headers = [th.get_text(strip=True).lower() for th in header_row.find_all(["th", "td"])]

    col_num = _find_col(headers, ["number", "número", "da number", "diretriz", "num"])
    col_subject = _find_col(headers, ["subject", "assunto", "description", "título", "title"])
    col_date = _find_col(headers, ["effective", "date", "data", "emissão", "issued"])
    col_make = _find_col(headers, ["manufacturer", "fabricante", "holder", "make"])
    col_model = _find_col(headers, ["model", "modelo", "aircraft", "type"])

    # Si no detectamos columnas por nombre, usamos posición por defecto
    # (patrón común en ANAC: col0=número, col1=subject, col2=date)
    if col_num is None:
        col_num = 0
    if col_subject is None:
        col_subject = 1
    if col_date is None:
        col_date = 2

    # --- Parsear filas de datos ---
    for row in rows[1:]:
        cells = row.find_all(["td", "th"])
        if not cells or len(cells) < 2:
            continue

        def cell_text(idx):
            if idx is not None and idx < len(cells):
                return cells[idx].get_text(strip=True)
            return ""

        ad_number_raw = cell_text(col_num)
        subject_raw = cell_text(col_subject)
        effective_date_raw = cell_text(col_date)
        make_raw = cell_text(col_make) if col_make is not None else ""
        model_raw = cell_text(col_model) if col_model is not None else ""

        # Saltar filas vacías o que son sub-encabezados
        if not ad_number_raw and not subject_raw:
            continue

        # --- Extraer link al detalle y al PDF ---
        detail_link = ""
        pdf_link = ""
        for cell in cells:
            for a in cell.find_all("a", href=True):
                href = a["href"]
                href_full = _resolve_url(href)
                link_text = a.get_text(strip=True).lower()
                if "detail" in href.lower() or "emd=" in href.lower():
                    detail_link = href_full
                elif ".pdf" in href.lower() or "pdf" in link_text:
                    pdf_link = href_full
                elif not detail_link and href_full:
                    detail_link = href_full

        # --- Normalizar número de AD ---
        ad_number = _normalize_ad_number(ad_number_raw)

        # --- Normalizar fecha ---
        effective_date = _normalize_date(effective_date_raw)

        # --- Inferir make/model del subject si no están en columnas propias ---
        if not make_raw:
            make_raw, inferred_model = _extract_make_model_from_subject(subject_raw)
            if not model_raw:
                model_raw = inferred_model

        result = {
            "Source": "ANAC (Brasil)",
            "AD_Number": ad_number,
            "Subject": subject_raw,
            "Make": make_raw,
            "Model": model_raw,
            "Effective_Date": effective_date,
            "Status": "Final Rule",
            "Product_Type": "",
            "Docket": ad_number,
            "PDF_Link": pdf_link or detail_link,
        }

        results.append(result)
        time.sleep(0)  # placeholder para rate limiting si se necesita

    return results


# ---------------------------------------------------------------------------
# Obtener detalle de un AD individual (opcional, para enriquecer datos)
# ---------------------------------------------------------------------------

def get_ad_detail(session, detail_url):
    """
    Obtiene información detallada de un AD individual.
    Útil para enriquecer los datos del listado cuando make/model no están.
    """
    try:
        resp = session.get(detail_url, timeout=20)
        resp.raise_for_status()
        resp.encoding = resp.apparent_encoding or "latin-1"
        soup = BeautifulSoup(resp.text, "lxml")

        detail = {}

        # Buscar tablas de detalle
        tables = soup.find_all("table")
        for table in tables:
            for row in table.find_all("tr"):
                cells = row.find_all(["td", "th"])
                if len(cells) >= 2:
                    label = cells[0].get_text(strip=True).lower()
                    value = cells[1].get_text(strip=True)

                    if any(k in label for k in ["number", "número", "da"]):
                        detail["AD_Number"] = _normalize_ad_number(value)
                    elif any(k in label for k in ["subject", "assunto"]):
                        detail["Subject"] = value
                    elif any(k in label for k in ["effective", "data"]):
                        detail["Effective_Date"] = _normalize_date(value)
                    elif any(k in label for k in ["manufacturer", "fabricante"]):
                        detail["Make"] = value
                    elif any(k in label for k in ["model", "modelo"]):
                        detail["Model"] = value

        # Buscar link al PDF
        for a in soup.find_all("a", href=True):
            if ".pdf" in a["href"].lower():
                detail["PDF_Link"] = _resolve_url(a["href"])
                break

        return detail
    except Exception as e:
        print(f"[ANAC Brasil] Error obteniendo detalle {detail_url}: {e}")
        return {}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _find_col(headers, keywords):
    """Busca el índice de la columna que coincide con alguna keyword."""
    for kw in keywords:
        for i, h in enumerate(headers):
            if kw in h:
                return i
    return None


def _resolve_url(href):
    """Convierte un href relativo en URL absoluta."""
    if not href:
        return ""
    if href.startswith("http"):
        return href
    if href.startswith("/"):
        return f"https://sistemas.anac.gov.br{href}"
    return f"{ANAC_BASE_URL}/{href.lstrip('/')}"


def _normalize_ad_number(raw):
    """
    Normaliza el número de AD de ANAC Brasil.
    Formato típico: 2022-04-01 o DA 2022-04-01 o 2022-04-01R01
    """
    if not raw:
        return raw
    # Remover prefijos comunes
    cleaned = re.sub(r"^(DA\s*|AD\s*)", "", raw.strip(), flags=re.I)
    return cleaned.strip()


def _normalize_date(raw):
    """
    Normaliza fechas del formato ANAC Brasil (DD/MM/YYYY o similar) a YYYY-MM-DD.
    """
    if not raw:
        return ""
    # Formato DD/MM/YYYY
    m = re.search(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})", raw)
    if m:
        day, month, year = m.group(1), m.group(2), m.group(3)
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"
    # Formato YYYY-MM-DD (ya correcto)
    m = re.search(r"(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"
    return raw.strip()


def _extract_make_model_from_subject(subject):
    """
    Infiere fabricante y modelo a partir del texto del subject/título.
    ANAC Brasil principalmente publica ADs de Embraer, pero también
    adopta ADs de Boeing, Airbus, etc.
    """
    known_makes = [
        "Embraer", "Boeing", "Airbus", "Bombardier", "ATR",
        "Cessna", "Piper", "Beechcraft", "Bell", "Sikorsky",
        "Leonardo", "Robinson", "Rolls-Royce", "General Electric",
        "Pratt & Whitney", "Honeywell", "Textron", "Pilatus",
        "Gulfstream", "Dassault", "Diamond", "Cirrus",
    ]

    make = ""
    for known in known_makes:
        if known.lower() in subject.lower():
            make = known
            break

    # Intentar extraer modelo: patrón "ERJ 170", "737", "A320", etc.
    model = ""
    model_patterns = [
        r"\b(ERJ\s*[-\s]?\d{3}(?:[-\s]\d{3})?(?:\s*[A-Z]{2,4})?)\b",
        r"\b(E\d{3}(?:\s*[A-Z]{2})?)\b",
        r"\b(EMB[-\s]?\d{3})\b",
        r"\b(737[-\s]?\w{0,4}(?:\s*MAX)?)\b",
        r"\b(A\s?3[0-9]{2}(?:[-\s]\d{3})?(?:\s*neo)?)\b",
        r"\b(777[-\s]?\d{0,3})\b",
        r"\b(787[-\s]?\d{0,3})\b",
        r"\b(CRJ[-\s]?\d{3})\b",
        r"\b(DHC[-\s]?\d+)\b",
        r"\b(ATR[-\s]?\d{2})\b",
    ]
    for pattern in model_patterns:
        m = re.search(pattern, subject, re.I)
        if m:
            model = m.group(1).strip()
            break

    return make, model


def get_recent_ads(days=14):
    """
    Wrapper de conveniencia para obtener los ADs más recientes.
    ANAC publica el listado de las últimas 2 semanas directamente.
    """
    session = requests.Session()
    session.headers.update(HEADERS)
    return _fetch_recent(session)


if __name__ == "__main__":
    print("=== ANAC Brasil AD Search - Test ===")
    results = search_ads(make="Embraer", max_results=10)
    for r in results[:5]:
        print(
            f"  AD: {r['AD_Number']} | "
            f"Make: {r['Make']} | "
            f"Date: {r['Effective_Date']} | "
            f"Subject: {r['Subject'][:60]}"
        )
