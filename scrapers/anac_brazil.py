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
- DA_ModProdE.asp  → Modelos de un fabricante (?OrgCodi=<id>)
- DA_ConsE.asp     → ADs de un modelo particular

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
ANAC_MOD_PROD_URL = f"{ANAC_BASE_URL}/DA_ModProdE.asp"
ANAC_CONS_URL = f"{ANAC_BASE_URL}/DA_ConsE.asp"

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

# Mapping de fabricantes conocidos a sus OrgCodi en ANAC (para búsqueda directa)
# Estos se descubren via DA_ProdE.asp?Letr=<letra> → links a DA_ModProdE.asp?OrgCodi=<id>
KNOWN_MANUFACTURERS = {
    "embraer": "000332",
    "boeing": None,       # Boeing no figura como fabricante ANAC-directo
    "airbus": None,
    "bombardier": None,
    "helibras": "000013",
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
        resp.encoding = "latin-1"
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
        resp.encoding = "latin-1"
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
    Busca ADs para un fabricante dado. Usa estrategia dual:

    1. Intenta buscar directamente en el índice por producto (DA_ProdE)
       si el fabricante tiene un OrgCodi conocido o lo descubrimos vía la
       página del abecedario.
    2. Como fallback robusto, descarga los listados cronológicos (DA_NumE)
       y filtra en memoria por patrones del fabricante en el Subject.
    """
    make_lower = make.lower().strip()

    # --- Estrategia 1: Buscar el OrgCodi del fabricante ---
    org_codi = KNOWN_MANUFACTURERS.get(make_lower)

    if not org_codi:
        # Buscar en la página del abecedario
        org_codi = _discover_org_codi(session, make)

    if org_codi:
        print(f"[ANAC Brasil] Usando página de producto directa (OrgCodi={org_codi}).")
        results = _fetch_by_product(session, org_codi, make)
        if results:
            # Filtrar por modelo si se especificó
            if model:
                model_lower = model.lower()
                results = [
                    r for r in results
                    if model_lower in r.get("Model", "").lower()
                    or model_lower in r.get("Subject", "").lower()
                ]
            print(f"[ANAC Brasil] {len(results)} ADs encontrados vía producto directo.")
            return results

    # --- Estrategia 2: Fallback a listados cronológicos con filtro amplio ---
    print(f"[ANAC Brasil] Delegando búsqueda de '{make}' a listado cronológico con filtro amplio.")
    results = []

    for yr in ["2021-2025", "2016-2020"]:
        batch = _fetch_list_by_number(session, yr)
        results.extend(batch)

    # Filtro amplio: busca por nombre de fabricante Y por prefijos de modelos conocidos
    model_prefixes = _get_model_prefixes_for_make(make_lower)
    filtered = []

    for r in results:
        subject_lower = r.get("Subject", "").lower()
        make_field = r.get("Make", "").lower()
        model_field = r.get("Model", "").lower()
        all_text = f"{make_field} {model_field} {subject_lower}"

        # Coincidencia directa del nombre del fabricante
        if make_lower in all_text:
            filtered.append(r)
            continue

        # Coincidencia por prefijos de modelo (ERJ, EMB, etc.)
        if any(pfx in all_text for pfx in model_prefixes):
            # Asegurarnos de asignar el Make si estaba vacío
            if not r["Make"]:
                r["Make"] = make
            filtered.append(r)
            continue

    # Filtrar por modelo si se especificó
    if model:
        model_lower = model.lower()
        filtered = [
            r for r in filtered
            if model_lower in r.get("Model", "").lower()
            or model_lower in r.get("Subject", "").lower()
        ]

    print(f"[ANAC Brasil] {len(filtered)} ADs filtrados para '{make}'.")
    return filtered


def _discover_org_codi(session, make):
    """
    Descubre el OrgCodi de un fabricante buscando en la página
    DA_ProdE.asp?Letr=<primera letra>.
    """
    first_letter = make.strip()[0].upper()
    make_lower = make.lower()

    try:
        resp = session.get(
            ANAC_LIST_BY_PROD_URL,
            params={"Letr": first_letter},
            timeout=20,
        )
        resp.raise_for_status()
        resp.encoding = "latin-1"
        soup = BeautifulSoup(resp.text, "lxml")

        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True).lower()
            if "DA_ModProdE.asp" in href and make_lower in text:
                match = re.search(r"OrgCodi=(\d+)", href)
                if match:
                    org_codi = match.group(1)
                    print(f"[ANAC Brasil] Descubierto OrgCodi={org_codi} para '{make}'.")
                    return org_codi
    except requests.RequestException as e:
        print(f"[ANAC Brasil] Error descubriendo OrgCodi para '{make}': {e}")

    return None


def _fetch_by_product(session, org_codi, make_name):
    """
    Obtiene ADs desde la página de producto directo del fabricante.
    Primero lista los modelos (DA_ModProdE.asp?OrgCodi=X), luego
    obtiene los ADs de cada modelo (DA_ConsE.asp?...).
    """
    try:
        resp = session.get(
            ANAC_MOD_PROD_URL,
            params={"OrgCodi": org_codi},
            timeout=30,
        )
        resp.raise_for_status()
        resp.encoding = "latin-1"
        soup = BeautifulSoup(resp.text, "lxml")

        # Buscar links a páginas de consulta de ADs por modelo
        model_links = []
        for a in soup.find_all("a", href=True):
            href = a["href"]
            text = a.get_text(strip=True)
            if "DA_ConsE" in href or ("ModCodi" in href and "Prod" not in href):
                model_links.append((text, href))

        if not model_links:
            # Si no hay links a modelos específicos, el fabricante lista ADs en esta misma página
            return _parse_list_table(soup, source_tag="product", make_override=make_name)

        print(f"[ANAC Brasil] {len(model_links)} modelos para {make_name}.")
        all_results = []

        for model_text, href in model_links:
            full_url = _resolve_url(href)
            try:
                resp2 = session.get(full_url, timeout=25)
                resp2.raise_for_status()
                resp2.encoding = "latin-1"
                soup2 = BeautifulSoup(resp2.text, "lxml")
                ads = _parse_list_table(soup2, source_tag="product", make_override=make_name)

                for ad in ads:
                    if not ad["Model"]:
                        ad["Model"] = model_text
                    if not ad["Make"]:
                        ad["Make"] = make_name

                all_results.extend(ads)
                time.sleep(0.3)  # Rate limiting
            except requests.RequestException as e:
                print(f"[ANAC Brasil] Error obteniendo modelo '{model_text}': {e}")

        # Deduplicar
        seen = set()
        unique = []
        for r in all_results:
            key = r["AD_Number"]
            if key not in seen:
                seen.add(key)
                unique.append(r)

        return unique

    except requests.RequestException as e:
        print(f"[ANAC Brasil] Error obteniendo productos para OrgCodi={org_codi}: {e}")
        return []


def _get_model_prefixes_for_make(make_lower):
    """
    Devuelve prefijos de modelo comunes para un fabricante dado,
    para poder encontrar ADs en el subject incluso cuando el nombre
    del fabricante no aparece explícitamente.
    """
    prefix_map = {
        "embraer": ["erj", "emb-", "emb ", "e170", "e175", "e190", "e195", "phenom", "legacy", "praetor", "lineage"],
        "boeing": ["737", "747", "757", "767", "777", "787", "717", "727"],
        "airbus": ["a318", "a319", "a320", "a321", "a330", "a340", "a350", "a380", "a220"],
        "bombardier": ["crj", "dhc-8", "cl-600", "challenger", "global", "learjet", "bd-"],
        "bell": ["bell "],
        "helibras": ["helibras", "esquilo"],
    }
    return prefix_map.get(make_lower, [make_lower])


# ---------------------------------------------------------------------------
# Parser de tablas de listado — MEJORADO
# ---------------------------------------------------------------------------

def _parse_list_table(soup, source_tag="", make_override=None):
    """
    Parsea una tabla HTML de listado de ADs del sitio ANAC Brasil.

    El sitio emite tablas de navegación y tablas de datos mezcladas. La tabla
    de datos correcta se identifica por la presencia de links a
    DA_DetailE.asp?Emd= (página de detalle de cada AD).

    Columnas reales de la tabla de datos (verificadas en DOM):
      Directive No. | Effective Date | Subject / Reference | Action | Texts
    """
    results = []

    tables = soup.find_all("table")
    if not tables:
        return results

    # --- Identificar la tabla de datos por presencia de links DA_DetailE ---
    # IMPORTANTE: lxml anida tablas, así que una tabla padre también "contiene"
    # los links de sus hijas. Buscamos la tabla MÁS INTERNA (menos sub-tablas)
    # que tiene links de detalle, ya que esa es la tabla de datos real.
    candidates_with_links = []

    for t in tables:
        detail_links = t.find_all("a", href=re.compile(r"DA_DetailE\.asp\?Emd=", re.I))
        if detail_links:
            # Contar sub-tablas anidadas — menos = más interno
            child_tables = t.find_all("table")
            candidates_with_links.append((t, len(detail_links), len(child_tables)))

    main_table = None
    if candidates_with_links:
        # Elegir la tabla con MÁS detail links pero MENOS sub-tablas anidadas
        # (la tabla de datos real no tiene sub-tablas)
        candidates_with_links.sort(key=lambda x: (x[2], -x[1]))
        main_table = candidates_with_links[0][0]

    if not main_table:
        # Fallback: tabla con más filas que tenga al menos un link
        fallback = [(t, len(t.find_all("tr"))) for t in tables if t.find("a", href=True)]
        if fallback:
            main_table = max(fallback, key=lambda x: x[1])[0]
        else:
            return results

    rows = main_table.find_all("tr")
    if not rows:
        return results

    # --- Detectar índices de columnas por encabezado ---
    # Buscamos la fila que contiene headers reconocibles
    header_row_idx = 0
    col_num = None
    col_subject = None
    col_date = None
    col_make = None
    col_model = None
    col_action = None   # FIXED: columna "Action" existe en el listado real de ANAC

    for idx, row in enumerate(rows):
        cells_text = [c.get_text(strip=True).lower() for c in row.find_all(["th", "td"])]
        # Verificar si esta fila parece un encabezado
        if any(h in " ".join(cells_text) for h in ["directive", "effective", "subject", "action"]):
            header_row_idx = idx
            col_num = _find_col(cells_text, ["directive", "number", "diretriz", "num"])
            col_subject = _find_col(cells_text, ["subject", "assunto", "reference"])
            col_date = _find_col(cells_text, ["effective", "date", "data"])
            col_make = _find_col(cells_text, ["manufacturer", "fabricante", "holder", "make"])
            col_model = _find_col(cells_text, ["model", "modelo", "aircraft", "type"])
            col_action = _find_col(cells_text, ["action"])  # FIXED: mapear columna Action
            print(f"[ANAC Brasil] DEBUG headers: {cells_text} -> action_col={col_action}")
            break

    # Defaults si no encontramos encabezados
    if col_num is None:
        col_num = 0
    if col_date is None:
        col_date = 1
    if col_subject is None:
        col_subject = 2

    # FIXED: Mapeo del campo "Action" de ANAC a status en inglés.
    # Valores reales observados en DOM: "F" (Final/Terminal), "B" (Repetitive/Terminal),
    # "E" (Emergency), "C" (Cancelled), "R" (Revision/Superseded).
    # Nota: el sitio también usa texto como "TERMINAL", "REPETITIVE / TERMINAL".
    _ACTION_STATUS_MAP = {
        "f": "Current",        # Final Rule → vigente
        "b": "Current",        # Biennial / Repetitive → vigente
        "e": "Emergency AD",   # Emergency
        "c": "Cancelled",      # Cancelled / Cancelada
        "r": "Superseded",     # Replaced / Substituída
        "s": "Superseded",     # Superseded
        "n": "NPRM",           # NPR-AD (Notice of Proposed Regulation)
        "p": "NPRM",           # Proposed
    }

    # --- Parsear filas de datos ---
    for row in rows[header_row_idx + 1:]:
        cells = row.find_all(["td", "th"])
        if not cells or len(cells) < 3:
            continue

        def cell_text(idx):
            if idx is not None and idx < len(cells):
                return cells[idx].get_text(strip=True)
            return ""

        ad_number_raw = cell_text(col_num)
        effective_date_raw = cell_text(col_date)
        subject_raw = cell_text(col_subject)
        make_raw = cell_text(col_make) if col_make is not None else ""
        model_raw = cell_text(col_model) if col_model is not None else ""
        action_raw = cell_text(col_action) if col_action is not None else ""  # FIXED: leer Action

        # Saltar filas vacías, footer, o sub-encabezados
        if not ad_number_raw or not subject_raw:
            continue
        # Saltar filas que parecen footer (ej: "Action:F- Terminal...")
        if ad_number_raw.lower().startswith("action") or "last modified" in ad_number_raw.lower():
            continue

        # --- Extraer link al detalle y al PDF ---
        detail_link = ""
        pdf_link = ""
        emd_id = ""   # FIXED: capturar el ID Emd= para construir URL de detalle

        for cell in cells:
            for a in cell.find_all("a", href=True):
                href = a["href"]
                href_full = _resolve_url(href)
                link_text = a.get_text(strip=True).lower()

                if "detail" in href.lower() or "emd=" in href.lower():
                    detail_link = href_full
                    # FIXED: extraer el ID del Emd para poder construir URL limpia
                    emd_match = re.search(r"Emd=(\d+)", href, re.I)
                    if emd_match:
                        emd_id = emd_match.group(1)
                elif ".pdf" in href.lower() or "pdf" in link_text:
                    if not pdf_link:
                        # FIXED: validar que sea URL absoluta. Si es relativa, resolver con base ANAC
                        pdf_link = href_full
                elif not detail_link and href_full:
                    detail_link = href_full

        # FIXED: debug para ADs sin pdf_link — nos ayuda a ver qué hrefs hay en la fila
        if not pdf_link and ad_number_raw:
            all_hrefs = [a["href"] for c in cells for a in c.find_all("a", href=True)]
            print(f"[ANAC Brasil] WARNING no pdf_link: AD={ad_number_raw!r} hrefs={all_hrefs}")

        # --- Normalizar número de AD ---
        ad_number = _normalize_ad_number(ad_number_raw)

        # --- Normalizar fecha ---
        effective_date = _normalize_date(effective_date_raw)

        # FIXED: Determinar status real desde la columna "Action".
        # Action="F" → Current, "E" → Emergency AD, "C" → Cancelled, etc.
        # También revisamos el subject para "emergência" o "proposed".
        action_key = action_raw.strip().lower()[:1]  # primera letra del código
        subject_lower = subject_raw.lower()

        if "emergência" in subject_lower or "emergency" in subject_lower or action_key == "e":
            status = "Emergency AD"
        elif "cancelada" in subject_lower or "cancelled" in subject_lower or action_key == "c":
            status = "Cancelled"
        elif "substituída" in subject_lower or "superseded" in subject_lower or action_key in ("r", "s"):
            status = "Superseded"
        elif "proposta" in subject_lower or "proposed" in subject_lower or action_key in ("n", "p"):
            status = "NPRM"
        else:
            # FIXED: Fallback: buscar en el mapa de Action, o "Current" como default
            status = _ACTION_STATUS_MAP.get(action_key, "Current")

        # FIXED: product_type desde subject en portugués Y en inglés.
        # Valores reales del campo "Product" en página detalle: AIRPLANE, HELICOPTER, ENGINE, PROPELLER.
        combined = subject_lower
        if re.search(r"\bmotor\b|\bengine\b", combined):
            product_type = "Engine"
        elif re.search(r"\bhélice\b|\bhelice\b|\bpropeller\b", combined):
            product_type = "Propeller"
        elif re.search(r"\bhelicóptero\b|\bhelicoptero\b|\bhelicopter\b|\brotor\b", combined):
            product_type = "Helicopter"
        elif re.search(r"\baplicativo\b|\bappliance\b|\bequipamento\b", combined):
            product_type = "Appliance"
        else:
            product_type = "Aircraft"  # FIXED: default sensato (mayoría son ADs de aeronaves)

        # --- Inferir make/model del subject si no están en columnas propias ---
        if make_override:
            if not make_raw:
                make_raw = make_override
        elif not make_raw:
            make_raw, inferred_model = _extract_make_model_from_subject(subject_raw)
            if not model_raw:
                model_raw = inferred_model

        # Intentar extraer modelo del subject si aún no tenemos
        if not model_raw:
            _, model_raw = _extract_make_model_from_subject(subject_raw)

        # FIXED: Docket — ANAC NO tiene campo "Processo" en el listado HTML.
        # El campo Amendment No. (Emd=) es un identificador interno, no un docket público.
        # Lo dejamos vacío ("") en lugar de duplicar el ad_number.
        docket = ""
        if emd_id:
            # El Emd ID es el único identificador interno disponible — lo guardamos como docket
            # para poder recuperar la página de detalle si se necesita.
            docket = f"Emd:{emd_id}"

        result = {
            "Source": "ANAC (Brasil)",
            "AD_Number": ad_number,
            "Subject": subject_raw,
            "Make": make_raw,
            "Model": model_raw,
            "Effective_Date": effective_date,
            "Status": status,           # FIXED: ya no hardcodeado a "Final Rule"
            "Product_Type": product_type,  # FIXED: ya no ""
            "Docket": docket,           # FIXED: ya no duplica ad_number
            "PDF_Link": pdf_link or detail_link,  # detail como fallback si no hay PDF directo
        }

        results.append(result)

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
        resp.encoding = "latin-1"
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
    El sitio a veces concatena "Amdt NNNN" sin espacio, hay que limpiar.
    """
    if not raw:
        return raw
    # Remover prefijos comunes
    cleaned = re.sub(r"^(DA\s*|AD\s*|E)", "", raw.strip(), flags=re.I)
    # Separar el Amdt si viene pegado (ej: "2021-02-01Amdt 1477" → "2021-02-01")
    cleaned = re.sub(r"Amdt\s*\d+.*$", "", cleaned, flags=re.I)
    return cleaned.strip()


def _normalize_date(raw):
    """
    Normaliza fechas del formato ANAC Brasil a YYYY-MM-DD.
    Acepta: DD/MM/YYYY, DD Mon. YYYY, YYYY-MM-DD
    """
    if not raw:
        return ""

    # Formato DD/MM/YYYY
    m = re.search(r"(\d{1,2})[/\-](\d{1,2})[/\-](\d{4})", raw)
    if m:
        day, month, year = m.group(1), m.group(2), m.group(3)
        return f"{year}-{month.zfill(2)}-{day.zfill(2)}"

    # Formato "15 Feb. 2021" o "23 Mar. 2023"
    MONTH_MAP = {
        "jan": "01", "feb": "02", "mar": "03", "apr": "04", "may": "05", "jun": "06",
        "jul": "07", "aug": "08", "sep": "09", "oct": "10", "nov": "11", "dec": "12",
        "fev": "02", "abr": "04", "mai": "05", "ago": "08", "set": "09", "out": "10", "dez": "12"
    }
    m = re.search(r"(\d{1,2})\s+([A-Za-z]{3,4})\.?\s+(\d{4})", raw)
    if m:
        day = m.group(1).zfill(2)
        month_str = m.group(2).lower()[:3]
        year = m.group(3)
        month = MONTH_MAP.get(month_str, "01")
        return f"{year}-{month}-{day}"

    # Formato YYYY-MM-DD (ya correcto)
    m = re.search(r"(\d{4})[/\-](\d{1,2})[/\-](\d{1,2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2).zfill(2)}-{m.group(3).zfill(2)}"

    return raw.strip()


def _extract_make_model_from_subject(subject):
    """
    Infiere fabricante y modelo a partir del texto del subject/título.

    ANAC Brasil concatena título y modelo sin separador:
    "DETECTORES DE FUMCAÇAERJ190-300; ERJ190-400"
    Así que buscamos patrones de modelo directamente.
    """
    known_makes = [
        "Embraer", "Boeing", "Airbus", "Bombardier", "ATR",
        "Cessna", "Piper", "Beechcraft", "Bell", "Sikorsky",
        "Leonardo", "Robinson", "Rolls-Royce", "General Electric",
        "Pratt & Whitney", "Honeywell", "Textron", "Pilatus",
        "Gulfstream", "Dassault", "Diamond", "Cirrus",
        "Helibras",
    ]

    make = ""
    for known in known_makes:
        if known.lower() in subject.lower():
            make = known
            break

    # Intentar extraer modelo y, si lo encontramos, inferir el fabricante
    model = ""
    model_patterns = [
        # Embraer patterns — estas son las más importantes para ANAC
        (r"\b(ERJ\s*[-\s]?\d{3}(?:[-\s]\d{3})?(?:\s*[A-Z]{0,4})?)", "Embraer"),
        (r"\b(E\d{3}(?:\s*[A-Z]{2})?)\b", "Embraer"),
        (r"\b(EMB[-\s]?\d{3}(?:[A-Z\d]*)?)", "Embraer"),
        # Boeing patterns
        (r"\b(737[-\s]?\w{0,4}(?:\s*MAX)?)\b", "Boeing"),
        (r"\b(747[-\s]?\w{0,4})\b", "Boeing"),
        (r"\b(757[-\s]?\w{0,4})\b", "Boeing"),
        (r"\b(767[-\s]?\w{0,4})\b", "Boeing"),
        (r"\b(777[-\s]?\w{0,4})\b", "Boeing"),
        (r"\b(787[-\s]?\w{0,4})\b", "Boeing"),
        # Airbus patterns
        (r"\b(A\s?3[0-9]{2}(?:[-\s]\d{3})?(?:\s*neo)?)\b", "Airbus"),
        # Bombardier
        (r"\b(CRJ[-\s]?\d{3})\b", "Bombardier"),
        (r"\b(DHC[-\s]?\d+)\b", "de Havilland Canada"),
        (r"\b(BD[-\s]?\d{3}[-\s]?\w+)\b", "Bombardier"),
        # ATR
        (r"\b(ATR[-\s]?\d{2})\b", "ATR"),
        # Helicopter patterns
        (r"\b(AS\s?3[0-9]{2}[A-Z]?\d*)\b", "Eurocopter"),
        (r"\b(EC\s?\d{3}[A-Z]?\d*)\b", "Eurocopter"),
    ]

    for pattern_tuple in model_patterns:
        pattern = pattern_tuple[0]
        implied_make = pattern_tuple[1] if len(pattern_tuple) > 1 else None
        m = re.search(pattern, subject, re.I)
        if m:
            model = m.group(1).strip()
            if not make and implied_make:
                make = implied_make
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
    print("=== ANAC Brasil Zephr - Test ===")
    results = search_ads(make="Embraer", max_results=20)
    print(f"\nTotal: {len(results)} results")
    for r in results[:10]:
        print(
            f"  AD: {r['AD_Number']} | "
            f"Make: {r['Make']} | "
            f"Model: {r['Model']} | "
            f"Date: {r['Effective_Date']} | "
            f"Subject: {r['Subject'][:60]}"
        )
