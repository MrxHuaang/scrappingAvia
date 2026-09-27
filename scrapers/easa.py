"""
Scraper para EASA Safety Publications Tool
Obtiene Airworthiness Directives desde ad.easa.europa.eu
"""

import requests
from bs4 import BeautifulSoup
import time
import re
import os
from scrapers.common.http import browser_headers, get_with_backoff
from scrapers.common.cache import load_json_cache, save_json_cache
from scrapers.common.easa_document_class import document_class

from scrapers.common.log import get_logger

log = get_logger(__name__)


EASA_BASE_URL = "https://ad.easa.europa.eu"
EASA_SEARCH_URL = f"{EASA_BASE_URL}/search/advanced"
EASA_RESULTS_URL = f"{EASA_BASE_URL}/search/advanced/result"  # FIXED: sin trailing slash
EASA_EXPORT_CSV = f"{EASA_BASE_URL}/search/export/csv"

# FIXED: EASA muestra 20 ADs por pagina (sin link de paginacion en HTML — es JS)
_PAGE_SIZE = 20

HEADERS = browser_headers()


def search_ads(keyword=None, model_keyword=None, doc_types=None, date_from=None, date_to=None,
               max_pages=50, stop_when=None):
    """
    Busca ADs en EASA.

    Args:
        keyword: Palabra clave general
        model_keyword: Modelo/fabricante a buscar
        doc_types: Lista de tipos (AD, EAD, PAD, SIB, PSD, SD)
        date_from: Fecha inicio (DD/MM/YYYY)
        date_to: Fecha fin (DD/MM/YYYY)
        max_pages: Maximo de paginas a recorrer
        stop_when: Callback opcional `(rows_de_esta_pagina) -> bool`. Cuando
            devuelve True el recorrido para ahi. La lista viene ordenada de la
            mas nueva a la mas vieja, asi que quien llama puede decidir que ya
            no hay nada que aprender mas abajo. Sin callback el comportamiento
            es el de siempre: recorrer hasta max_pages o hasta el total.

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
    seen = 0  # filas leidas del listado, incluidas las que no se ingieren
    page = 1
    total_available = None  # FIXED: se lee del HTML en page 1

    while page <= max_pages:
        # FIXED: page 1 usa la URL base; page 2+ usa ?page=N con la misma sesion
        url = EASA_RESULTS_URL if page == 1 else f"{EASA_RESULTS_URL}/?page={page}"

        try:
            # Backoff exponencial en 429/5xx (helper comun de scrapers)
            resp = get_with_backoff(url, session=session,
                                    params=params if page == 1 else {},
                                    headers=HEADERS, timeout=60, tag="EASA")
            if resp is None:
                raise requests.RequestException(f"pagina {page}: reintentos agotados")
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            # FIXED: leer total de resultados del texto "Displaying X to Y out of total of Z"
            if total_available is None:
                total_available = _parse_total_results(soup)
                max_page_calc = ((total_available - 1) // _PAGE_SIZE + 1) if total_available else max_pages
                effective_max = min(max_pages, max_page_calc)
                log.info(f"[EASA] Total disponible: {total_available} ADs ({effective_max} paginas)")

            parsed = _parse_results_table(soup)

            if not parsed:
                log.info(f"[EASA] Pagina {page}: sin resultados, deteniendo.")
                break

            # Una pagina puede traer solo PADs cancelados: se filtran DESPUES
            # de decidir si el listado se acabo, y el total se compara contra
            # lo leido, no contra lo que se guarda.
            seen += len(parsed)
            results = drop_cancelled_proposals(parsed)
            all_results.extend(results)

            # Log detallado cada 100 paginas, resumen breve el resto
            if page % 100 == 0:
                log.info(f"[EASA] *** PROGRESO: Pagina {page}/{effective_max} | Total acumulado: {len(all_results)} ADs ***")
            else:
                log.info(f"[EASA] Pagina {page}/{effective_max}: {len(results)} ADs | acumulado: {len(all_results)}")

            # FIXED: parar cuando ya tenemos todos o llegamos al limite
            if seen >= (total_available or 0) or page >= effective_max:
                break

            # Parada incremental: quien llama sabe que hay en la BD y decide.
            # El callback se consulta DESPUES de acumular la pagina, asi que lo
            # que ya se leyo nunca se pierde por parar.
            if stop_when is not None and stop_when(results):
                log.info(f"[EASA] Pagina {page}: el llamador pidio parar (recorrido incremental).")
                break

            page += 1
            time.sleep(0.8)

        except requests.RequestException as e:
            log.info(f"[EASA] Error en pagina {page}: {e}")
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
            log.info(f"[EASA] Filtrados {len(filtered)} de {len(all_results)} por '{model_keyword}'")
            all_results = filtered

    log.info(f"[EASA] Total ADs obtenidos: {len(all_results)}")
    return all_results


def search_main_page(model_keyword=None, max_pages=5):
    """Obtiene ADs de la pagina principal de EASA (lista reciente)."""
    session = requests.Session()
    all_results = []

    # FIXED: search_main_page redirige a la misma URL de resultados sin filtros
    # La pagina principal lista los ADs mas recientes, es equivalente a result/ sin params
    total_available = None
    for page in range(1, max_pages + 1):
        url = EASA_RESULTS_URL if page == 1 else f"{EASA_RESULTS_URL}/?page={page}"  # FIXED

        try:
            resp = session.get(url, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "lxml")

            if total_available is None:
                total_available = _parse_total_results(soup)
                log.info(f"[EASA] Total disponible: {total_available} ADs")

            parsed = _parse_results_table(soup)

            if not parsed:
                log.info(f"[EASA] Pagina principal {page}: sin resultados, deteniendo.")
                break

            results = drop_cancelled_proposals(parsed)
            all_results.extend(results)
            log.info(f"[EASA] Pagina principal {page}: {len(results)} ADs | acumulado: {len(all_results)}")
            time.sleep(0.8)

        except requests.RequestException as e:
            log.info(f"[EASA] Error pagina principal {page}: {e}")
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

    log.info(f"[EASA] Total ADs obtenidos: {len(all_results)}")
    return all_results


_PARSE_FAILURES = 0

# Nodos del arbol "TC holder" de EASA que no son un fabricante: la clase del
# documento ("Safety Information Bulletin", "Safety Directives"), el cajon de
# appliances por capitulo ATA ("APPLIANCES", el titular real va en la prosa del
# Subject) y la declaracion de que el tipo no tiene titular ("ORPHANED (No TC
# Holder)"). Salen de https://ad.easa.europa.eu/json/ con la misma clase CSS
# que cualquier empresa real; nada en el markup los distingue.
_TAXONOMY_PLACEHOLDERS = frozenset(
    name.casefold() for name in (
        "Safety Information Bulletin",
        "APPLIANCES",
        "ORPHANED (No TC Holder)",
        "Safety Directives",
        "Preliminary Safety Directives",
    )
)


# De todos los placeholders, solo ORPHANED cuelga tipos de aeronave reales
# ("SLINGSBY", "BRANTLY"); bajo los demas los "tipos" son categorias de EASA
# ("Airworthiness", "ATA 25 EQUIPMENT / FURNISHINGS"), no modelos.
_PLACEHOLDERS_WITH_REAL_TYPES = frozenset({"orphaned (no tc holder)"})


def _placeholder_key(holder_name: str) -> str:
    return " ".join(holder_name.split()).casefold()


def _is_taxonomy_placeholder(holder_name: str) -> bool:
    return _placeholder_key(holder_name) in _TAXONOMY_PLACEHOLDERS


def _parse_results_table(soup):
    """
    Parsea la tabla de resultados de EASA.
    """
    global _PARSE_FAILURES
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

            # Columna 4: Approval Holder / Type Designation.
            #
            # Un AD puede listar N approval holders, cada uno con sus propios
            # type designations. Esa estructura se conserva en `applicability`
            # (una entrada por holder, con SUS modelos) porque es la unica forma
            # de reconstruirla despues: los nombres de holder contienen comas
            # ("McDermott 214 Holdings, LLC", "REVO, Inc.") y re-partir el string
            # unido por ", " los corta al medio.
            make = ""
            model = ""
            applicability = []
            if len(cells) > 4:
                tree_cell = cells[4]

                tc_holders = tree_cell.find_all("li", class_="tc_holder")
                models_list = []

                for holder in tc_holders:
                    holder_name = holder.find(string=True, recursive=False)
                    holder_name = holder_name.strip() if holder_name else ""
                    holder_models = [
                        text for text in (t.get_text(strip=True) for t in holder.find_all("li", class_="type"))
                        if text
                    ]

                    # Un placeholder de la taxonomia declara que en ese lugar
                    # NO hay empresa: no es un holder ilegible, asi que sale de
                    # la lista de holders (ni Applicability ni Make). Solo los
                    # tipos de ORPHANED son modelos reales ("SLINGSBY") y quedan
                    # en Model; los de los demas son categorias y se descartan.
                    # Si solo habia placeholders la lista queda vacia y el AD
                    # viaja `failed`, como cualquier lista vacia.
                    if _is_taxonomy_placeholder(holder_name):
                        if _placeholder_key(holder_name) in _PLACEHOLDERS_WITH_REAL_TYPES:
                            models_list.extend(holder_models)
                        continue

                    models_list.extend(holder_models)

                    # Un holder cuyo nombre no se lee (envuelto en otro
                    # elemento, seccion cortada) viaja con `make` vacio en vez
                    # de saltarse: los demas no son la applicability completa
                    # del AD, y populate_db lo declara `failed` entero.
                    applicability.append({"make": holder_name, "models": holder_models})

                # `Make` es el holder PRIMARIO, no la lista unida: el string
                # unido llegaba a 494 chars y aparecia tal cual como opcion en
                # el filtro Make de la UI. Los N holders viven en
                # `Applicability` -> ad_applicability (misma convencion que
                # Model, cuyo listado completo tambien vive alli).
                make = next((h["make"] for h in applicability if h["make"]), "")
                # Model se mantiene como el join de TODOS los types del AD: es
                # lo que filtra `search_ads` sobre ads.model, y recortarlo al
                # holder primario perderia recall en la busqueda por modelo.
                model = ", ".join(models_list) if models_list else ""

            # Columna 5: Effective Date
            effective_date = cells[5].get_text(strip=True) if len(cells) > 5 else ""

            # Columna 6: Attachments (PDF links)
            pdf_link = ""
            attachment_links = []
            if len(cells) > 6:
                for link in cells[6].find_all("a"):
                    href = link.get("href", "")
                    if href:
                        if not href.startswith("http"):
                            href = f"{EASA_BASE_URL}{href}"
                        attachment_links.append(href)
                        if ".pdf" in href.lower() or "blob" in href.lower():
                            pdf_link = href
                            break
                        if not pdf_link:
                            pdf_link = href

            # Clase de la publicacion (AD, EAD, PAD, SIB, SD, ADR): el listado
            # mezcla todas. La cola del link del adjunto la declara; el icono
            # de clase del Subject es el respaldo. None si ninguna la dice.
            doc_class = document_class(
                [pdf_link, *attachment_links], icon_alt=_class_icon_alt(cells[3]),
            )

            # FIXED: extraer product_type desde keywords del subject
            subject_lower = subject.lower()
            if re.search(r"\bengine\b|\bengines\b|\bmotor\b", subject_lower):
                product_type = "Engine"
            elif re.search(r"\bpropeller\b|\bpropellers\b", subject_lower):
                product_type = "Propeller"
            elif re.search(r"\bhelicopter\b|\bhelicopters\b|\brotorcraft\b", subject_lower):
                product_type = "Helicopter"
            elif re.search(r"\bappliance\b|\bavionics\b|\bequipment\b", subject_lower):
                product_type = "Appliance"
            else:
                product_type = "Aircraft"  # FIXED: default sensato (mayoría son ADs de aeronave)

            results.append({
                "Source": "EASA",
                "AD_Number": ad_number,
                "Subject": subject,
                "Make": make,
                "Model": model,
                # [{make, models[]}] por approval holder — populate_relations lo
                # usa para escribir una fila de ad_applicability por par real.
                "Applicability": applicability,
                "Effective_Date": effective_date or issue_date,
                # Un aviso de cancelacion (numero terminado en -CN) se guarda
                # Cancelled; el resto de lo que lista EASA esta vigente.
                "Status": "Cancelled" if _is_cancellation_notice(ad_number) else "Current",
                "Product_Type": product_type,  # FIXED: ya no hardcodeado a ""
                "Docket": "",
                "PDF_Link": pdf_link,
                "Document_Class": doc_class,
            })

        except Exception as e:
            _PARSE_FAILURES += 1
            if _PARSE_FAILURES <= 10:
                log.info(f"[EASA] WARNING row parse failed: {type(e).__name__}: {e}")
            continue

    return results


# Aviso de cancelacion: EASA le agrega "-CN" al numero de lo que cancela
# (2024-0109-CN cancela el AD 2024-0109; 26-098-CN, el PAD 26-098).
_CANCELLATION_RE = re.compile(r"-CN$", re.I)


def _is_cancellation_notice(ad_number: str) -> bool:
    return bool(_CANCELLATION_RE.search((ad_number or "").strip()))


def drop_cancelled_proposals(rows: list[dict]) -> list[dict]:
    """
    Quita los PADs cancelados (clase PAD y numero -CN): la cancelacion de una
    propuesta que nunca fue directiva no entra al catalogo. Los ADs cancelados
    y los demas avisos -CN se quedan, con status Cancelled.
    """
    return [
        r for r in rows
        if not (r.get("Document_Class") == "PAD" and _is_cancellation_notice(r.get("AD_Number", "")))
    ]


def _class_icon_alt(subject_cell):
    """`alt` del icono de clase del Subject (`adclass_*.gif`), o None."""
    for img in subject_cell.find_all("img"):
        if "adclass_" in (img.get("src") or ""):
            return img.get("alt")
    return None


def _parse_total_results(soup):
    """
    FIXED: Lee el total de resultados del texto que EASA incluye en el HTML:
    'Displaying records 1 to 20 out of a total of 18199 publications.'
    Retorna el numero entero, o None si no lo encuentra.
    """
    for tag in soup.find_all(string=re.compile(r"total of", re.I)):
        m = re.search(r"total of\s+([\d,]+)", tag, re.I)
        if m:
            return int(m.group(1).replace(",", ""))
    return None


# ---------------------------------------------------------------------------
# Enriquecimiento via pagina de detalle (ad.easa.europa.eu/ad/{number})
# La pagina de listado no trae los campos profundos; el detalle expone (estructurado,
# sin PDF): Issue date, Supersedure, Publication(s)=Service Bulletins, y el Remark
# estandar de AMOC. La prosa (unsafe condition / required action / compliance time)
# solo vive en el PDF -> queda fuera de este paso.
# Llamar SOLO desde el batch (run_easa), NO desde search_ads (frenaria la busqueda
# en vivo del app). Cacheado por AD_Number en outputs/easa_detail_cache.json.
# ---------------------------------------------------------------------------
EASA_DETAIL_URL = f"{EASA_BASE_URL}/ad"
_DETAIL_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "outputs", "easa_detail_cache.json",
)
_detail_cache = None


def _load_detail_cache():
    global _detail_cache
    if _detail_cache is None:
        _detail_cache = load_json_cache(_DETAIL_CACHE_PATH)
    return _detail_cache


def _save_detail_cache():
    if _detail_cache is not None:
        save_json_cache(_DETAIL_CACHE_PATH, _detail_cache)


# Numero de AD dentro del texto EASA: forma moderna 2024-0195(R1) o legado
# nacional con prefijo (F-1998-173, US-2002-08-03). Guard anti-PAD abajo.
_EASA_AD_NUM_RE = re.compile(
    r"(?<![-\w])((?:[A-Z]{1,2}-)?\d{4}-\d{3,4}(?:R\d+)?)(?![-\d])"
)


def _extract_supersedure(text: str) -> tuple[str | None, list[str]]:
    """
    (superseded_by, supersedes_all) desde el texto de la pagina de detalle.
    La pagina expone el dato como campo estructurado "Supersedure"; fallback a
    prosa. Excluye referencias a PAD (Proposed AD).
    """
    # 1) campo estructurado: "Supersedure\n<contenido hasta el proximo label>"
    m = re.search(r"Supersedure\s*:?\s*\n(.{0,400}?)(?:\n[A-Z][a-z]+(?:\s[a-z]+)*\s*\n|\Z)", text, re.S)
    block = m.group(1) if m else ""
    scope = block or text

    superseded_by = None
    supersedes: list[str] = []

    def _nums(segment: str) -> list[str]:
        found = []
        for nm in _EASA_AD_NUM_RE.finditer(segment):
            before = segment[max(0, nm.start() - 12):nm.start()]
            if re.search(r"PAD\s*$", before, re.I):
                continue  # referencia a un Proposed AD, no supersession
            found.append(nm.group(1).strip())
        return list(dict.fromkeys(found))

    by = re.search(r"superseded\s+by\s*:?\s*((?:EASA\s+)?AD\s+)?([^\n]{0,120})", scope, re.I)
    if by:
        cands = _nums(by.group(0))
        if cands:
            superseded_by = cands[0]

    sup = re.search(r"(?:this\s+AD\s+)?supersedes?\s*:?\s*([^\n]{0,200})", scope, re.I)
    if sup:
        supersedes = [n for n in _nums(sup.group(0)) if n != superseded_by]

    # fallback prosa vieja (compat con el regex original)
    if not superseded_by and not supersedes:
        m2 = re.search(r"supersede[sd]?\s+(?:by\s+)?EASA AD\s+([A-Z0-9][A-Z0-9\-/]+)", text, re.I)
        if m2:
            if re.search(r"superseded by", text, re.I):
                superseded_by = m2.group(1).strip()
            else:
                supersedes = [m2.group(1).strip()]

    return superseded_by, supersedes


def _parse_detail(text: str) -> dict:
    """Extrae campos estructurados del texto plano de la pagina de detalle."""
    out = {"publication_date": None, "amoc": False,
           "service_bulletins": [], "supersedes": None, "superseded_by": None,
           "supersedes_all": []}

    m = re.search(r"Issue date\s+(\d{4}-\d{2}-\d{2})", text)
    if m:
        out["publication_date"] = m.group(1)

    out["amoc"] = "alternative methods of compliance" in text.lower() \
        or "alternative method of compliance" in text.lower()

    superseded_by, supersedes_all = _extract_supersedure(text)
    out["superseded_by"] = superseded_by
    out["supersedes_all"] = supersedes_all
    out["supersedes"] = supersedes_all[0] if supersedes_all else None

    # Bloque "Publication(s) and related information" -> referencias SB/AOT
    pub = re.search(
        r"Publication\(s\)[^\n]*\n(.*?)(?:\nPAD number|\nRemarks|\nDownload)",
        text, re.S | re.I,
    )
    if pub:
        sbs = []
        _SKIP = ("the use of later", "acceptable for compliance",
                 "requirements of this ad", "compliance with the requirements",
                 "consultation", "comments were received")
        for line in pub.group(1).splitlines():
            line = line.strip()
            low = line.lower()
            if not line or any(s in low for s in _SKIP) or line[0].islower():
                continue
            tok = re.search(r"[A-Z]{2,}[A-Z0-9]*[-/][A-Z0-9\-/]*\d", line)
            sbs.append((tok.group(0) if tok else line)[:120])
        out["service_bulletins"] = sbs[:20]

    return out


def _fetch_detail(ad_number: str, session: requests.Session) -> dict:
    cache = _load_detail_cache()
    # "supersedes_all" => entrada escrita con el parser nuevo de Supersedure.
    # Entradas viejas se re-piden una vez (los hits existentes siguen validos
    # como input del backfill mientras tanto).
    if ad_number in cache and "supersedes_all" in cache[ad_number]:
        return cache[ad_number]
    result = {"publication_date": None, "amoc": False,
              "service_bulletins": [], "supersedes": None, "superseded_by": None,
              "supersedes_all": []}
    try:
        resp = session.get(f"{EASA_DETAIL_URL}/{ad_number}", headers=HEADERS, timeout=40)
        if resp.ok:
            soup = BeautifulSoup(resp.text, "lxml")
            text = re.sub(r"[ \t]+", " ", soup.get_text("\n"))
            result = _parse_detail(text)
    except requests.RequestException:
        pass
    cache[ad_number] = result
    return result


# ---------------------------------------------------------------------------
# Parseo del PDF del AD (unica fuente de la prosa: Reason / Required Action(s)
# and Compliance Time(s) / Ref. Publications). PDFs EASA son texto, no escaneos.
# ---------------------------------------------------------------------------
FETCH_PDF = True
_PDF_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "outputs", "easa_pdf_cache.json",
)
_pdf_cache = None


def _load_pdf_cache():
    global _pdf_cache
    if _pdf_cache is None:
        _pdf_cache = load_json_cache(_PDF_CACHE_PATH)
    return _pdf_cache


def _save_pdf_cache():
    if _pdf_cache is not None:
        save_json_cache(_PDF_CACHE_PATH, _pdf_cache)


def _clean_pdf(s: str) -> str:
    # Quitar footers/headers repetidos del PDF EASA.
    s = re.sub(r"EASA AD No\.?:.*", " ", s)
    s = re.sub(r"TE\.CAP\.[0-9\-]+", " ", s)
    s = re.sub(r"Page \d+ of \d+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _pdf_section(txt: str, start: str, ends: list[str], cap: int = 600) -> str | None:
    i = txt.find(start)
    if i < 0:
        return None
    j = i + len(start)
    end = len(txt)
    for lbl in ends:
        k = txt.find(lbl, j)
        if 0 <= k < end:
            end = k
    seg = _clean_pdf(txt[j:end])
    return seg[:cap].strip() or None


_EASA_COMPLIANCE = re.compile(
    r"(before further flight"
    r"|before exceeding[^.;]{0,80}"
    r"|within\s+\d[\d,]*\s*(?:flight\s+)?(?:hours?|days?|weeks?|months?|years?|cycles?|landings?|FH|FC)\b[^.;]{0,40}"
    r"|at intervals not to exceed[^.;]{0,60}"
    r"|before the threshold[^.;]{0,60})",
    re.I,
)


def _parse_pdf_fields(text: str) -> dict:
    out = {"unsafe_condition": None, "corrective_action": None,
           "compliance_time": None, "service_bulletins": []}
    if not text:
        return out
    txt = re.sub(r"[ \t]+", " ", text)

    out["unsafe_condition"] = _pdf_section(
        txt, "Reason:",
        ["Required Action", "Ref. Publications", "Remarks:", "Effective Date"], cap=500,
    )
    action = _pdf_section(
        txt, "Required Action(s) and Compliance Time(s):",
        ["Ref. Publications", "Remarks:"], cap=900,
    )
    if action:
        m = _EASA_COMPLIANCE.search(action)
        out["compliance_time"] = re.sub(r"\s+", " ", m.group(0)).strip() if m else None
        # Strip the standard lead-in boilerplate to reach the real action text.
        action = re.sub(
            r"^Required as indicated by this AD,\s*unless the action[^:]*:\s*",
            "", action, flags=re.I,
        )
        action = re.sub(
            r"^(?:Inspection\(s\)|Action\(s\)|Modification\(s\)|Replacement\(s\)|Corrective action\(s\))\s*:\s*",
            "", action, flags=re.I,
        )
        out["corrective_action"] = action[:500].strip() or None

    refs = _pdf_section(txt, "Ref. Publications:", ["Remarks:", "Effective Date", "Appendix"], cap=800)
    if refs:
        sbs = []
        for m in re.finditer(r"\b(?:SB|VSB|ASB|Service Bulletin)\s+([A-Z0-9][A-Z0-9\-/]+)", refs, re.I):
            tok = m.group(1).strip().rstrip(".")
            if tok and tok.upper() not in (x.upper() for x in sbs):
                sbs.append(tok)
        out["service_bulletins"] = sbs[:20]
    return out


def _fetch_pdf(ad_number: str, pdf_link: str, session: requests.Session) -> dict:
    cache = _load_pdf_cache()
    if ad_number in cache:
        return cache[ad_number]
    result = {"unsafe_condition": None, "corrective_action": None,
              "compliance_time": None, "service_bulletins": []}
    if pdf_link and ".pdf" in pdf_link.lower():
        try:
            import io
            from pypdf import PdfReader
            resp = session.get(pdf_link, headers=HEADERS, timeout=60)
            if resp.ok and resp.content[:4] == b"%PDF":
                reader = PdfReader(io.BytesIO(resp.content))
                text = "\n".join((p.extract_text() or "") for p in reader.pages[:8])
                result = _parse_pdf_fields(text)
        except Exception:
            pass
    cache[ad_number] = result
    return result


def enrich_details(results: list[dict]) -> list[dict]:
    """
    Para cada AD del listado, baja su pagina de detalle y agrega las claves que
    consume normalize()/populate_relations: Publication_Date, AMOC_Available,
    Service_Bulletins, Supersedes, Superseded_By. Idempotente + cacheado.
    """
    session = requests.Session()
    session.headers.update(HEADERS)
    total = len(results)
    dcache = _load_detail_cache()
    pcache = _load_pdf_cache()
    for i, row in enumerate(results, 1):
        num = (row.get("AD_Number") or "").strip()
        if not num:
            continue
        cached = (num in dcache) and (num in pcache)
        d = _fetch_detail(num, session)
        if d.get("publication_date"):
            row["Publication_Date"] = d["publication_date"]
        row["AMOC_Available"] = bool(d.get("amoc"))
        sbs = list(d.get("service_bulletins") or [])
        supersedes_all = list(d.get("supersedes_all") or [])
        if not supersedes_all and d.get("supersedes"):
            supersedes_all = [d["supersedes"]]
        # Regla implicita de revisiones: 2024-0195R2 supersede 2024-0195R1 (y
        # la base). Aplica solo si la pagina no declaro un Supersedure explicito.
        if not supersedes_all:
            rev = re.match(r"^(.+?)R(\d+)$", num)
            if rev:
                base, n = rev.group(1), int(rev.group(2))
                supersedes_all = [f"{base}R{n - 1}" if n > 1 else base]
        if supersedes_all:
            row["Supersedes"] = ", ".join(dict.fromkeys(supersedes_all))
        if d.get("superseded_by"):
            row["Superseded_By"] = d["superseded_by"]

        # Prosa: solo del PDF (Reason / Required Action(s) and Compliance Time(s)).
        if FETCH_PDF:
            pf = _fetch_pdf(num, row.get("PDF_Link") or "", session)
            if pf.get("unsafe_condition"):
                row["Unsafe_Condition"] = pf["unsafe_condition"]
            if pf.get("corrective_action"):
                row["Corrective_Action"] = pf["corrective_action"]
            if pf.get("compliance_time"):
                row["Compliance_Time"] = pf["compliance_time"]
            for sb in pf.get("service_bulletins") or []:
                if sb not in sbs:
                    sbs.append(sb)
        if sbs:
            row["Service_Bulletins"] = sbs

        if i % 200 == 0:
            log.info(f"[EASA] detalle+pdf: {i}/{total} enriquecidos")
            _save_detail_cache()
            _save_pdf_cache()
        if not cached:  # no dormir si ya estaba en cache (re-run rapido)
            time.sleep(0.15)
    _save_detail_cache()
    _save_pdf_cache()
    return results


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
        log.info(f"[EASA] Error exportando CSV: {e}")
        return None
