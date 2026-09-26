"""
Scraper para Federal Register API - Airworthiness Directives (FAA)

La FR API tiene hard cap de 10000 resultados por query. Para superarlo,
fragmentamos la consulta por año via conditions[publication_date][gte]/[lte].
Cobertura: 1994-01-03 en adelante (la API no indexa documentos previos).

Endpoint: https://www.federalregister.gov/api/v1/documents.json
Docs:     https://www.federalregister.gov/developers/documentation/api/v1
"""

import hashlib
import os
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

import requests

import enrich
from enrich import classify_product_type
from utils import (
    ad_identity_from_docket_ids,
    normalize_ad_number,
)

from scrapers.common.cache import load_json_cache, save_json_cache
from scrapers.common.fr_applicability import applicability_fields
from scrapers.common.http import API_HEADERS, get_with_backoff

from scrapers.common.log import get_logger

log = get_logger(__name__)


FEDERAL_REGISTER_API = "https://www.federalregister.gov/api/v1/documents.json"
FR_API_START_YEAR = 1994
PER_PAGE = 1000  # max soportado por la API

HEADERS = API_HEADERS

_FIELDS = [
    "title", "abstract", "document_number", "publication_date",
    "effective_on", "pdf_url", "html_url", "agencies",
    "cfr_references", "action", "type", "raw_text_url",
    # docket_ids ES la identidad del AD (numero, docket, amendment). Ver
    # utils.ad_identity_from_docket_ids.
    "docket_ids", "citation", "correction_of",
]

# ---------------------------------------------------------------------------
# Full text de GovInfo: el texto del AD en secciones (enrich.split_ad_sections)
# y, del cuerpo, compliance_time, AMOC, docket y supersession. Se baja una vez
# por documento y se cachea en outputs/ el resultado del intento (secciones
# incluidas), no el HTML.
# ---------------------------------------------------------------------------
FETCH_FULL_TEXT = True  # poner False para saltar el parseo del cuerpo (mas rapido)
_FULLTEXT_CACHE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "outputs", "fr_fulltext_cache.json",
)
_fulltext_cache: dict | None = None
# Una Session por hilo: requests.Session no es thread-safe, y el prefetch
# concurrente comparte el pool de conexiones por hilo.
_ft_local = threading.local()
# GovInfo admite 40 req/s; 8 hilos con ~1s de latencia por granulo dejan el
# backfill completo en minutos en vez de horas (13.5k granulos en serie).
FULLTEXT_WORKERS = 8

# Cache v2, una entrada por document_number:
#   {v: 2, status, anchor, sections[], sha, compliance_time, amoc, docket,
#    supersedes[], ad_header}
# `status` es el de enrich.split_ad_sections (extracted | unanchored | empty
# | error) y `sha` el sha256 del granulo bajado. `extracted` y `unanchored`
# son definitivos: el mismo documento da el mismo resultado. `empty` y
# `error` se reintentan en la corrida siguiente (una vez por corrida).
#
# Una entrada v1 (sin `v`: solo los campos del cuerpo) se sigue leyendo tal
# cual y se re-pide una vez para obtener sus secciones. Si ese intento falla,
# conserva sus campos del cuerpo: un intento fallido nunca borra datos buenos.
_FT_CACHE_VERSION = 2
_FT_FINAL_STATUSES = ("extracted", "unanchored")
# Documentos ya pedidos en esta corrida: el prefetch y el parseo no repiten
# el GET de un `error`/`empty` dentro de la misma corrida.
_ft_attempted: set[str] = set()
# Hubo intentos que cambiaron el cache desde el ultimo guardado.
_fulltext_dirty = False


def _load_fulltext_cache() -> dict:
    global _fulltext_cache
    if _fulltext_cache is None:
        _fulltext_cache = load_json_cache(_FULLTEXT_CACHE_PATH)
    return _fulltext_cache


def _save_fulltext_cache() -> None:
    """Escribe el cache solo si algun intento lo cambio desde la ultima vez:
    son ~90 MB y un anio sin documentos nuevos no tiene nada que guardar."""
    global _fulltext_dirty
    if _fulltext_cache is not None and _fulltext_dirty:
        save_json_cache(_FULLTEXT_CACHE_PATH, _fulltext_cache)
        _fulltext_dirty = False


def _fulltext_is_final(entry: dict | None) -> bool:
    return bool(entry) and entry.get("v") == _FT_CACHE_VERSION and entry.get("status") in _FT_FINAL_STATUSES


def _fulltext_pending(doc_number: str) -> bool:
    """El documento necesita un GET: sin entrada definitiva y sin intento en
    esta corrida."""
    return (bool(doc_number) and doc_number not in _ft_attempted
            and not _fulltext_is_final(_load_fulltext_cache().get(doc_number)))


_FT_DOCKET_RE = re.compile(
    r"Docket\s*No\.?\s*[:,]?\s*(FAA[-\s]?\d{4}[-–—]\d{1,6})", re.I
)

# "This AD replaces AD 2020-06-14" / "The FAA is superseding AD 2018-23-51R1".
# Exige el keyword AD para no matchear fechas/dockets; acepta forma Emergency.
_FT_SUPERSEDE_RE = re.compile(
    r"(?:supersed(?:es|ing)|replaces)\s+(?:Emergency\s+)?"
    r"(?:Airworthiness\s+Directive\s*\(?AD\)?|AD)\s*"
    r"((?:\d{2}|\d{4})-\d{2}-\d{2}(?:R\d+)?)(?![-\d])",
    re.I,
)


def _body_fields(html_text: str, ad: str | None, amendment: str | None) -> dict:
    """Lo que sale de un granulo bajado: el intento de secciones y los campos
    del cuerpo."""
    txt = enrich.fr_document_text(html_text)
    # Identidad: viene de `docket_ids` (utils.ad_identity_from_docket_ids). El
    # cuerpo SOLO es respaldo para los ~100 docs (casi todos 1998) cuyo
    # docket_ids no trae el AD, y el match va ANCLADO al amendment del corchete
    # del preambulo. Buscar el primer "AD nnnn-nn-nn" suelto del cuerpo es lo
    # que rompio la base: el rule cita al AD que reemplaza antes de nombrarse.
    first = enrich.FT_HEADER_AD_RE.search(txt)
    ad_header = (enrich.header_ad_for_amendment(txt, amendment or "")
                 or (normalize_ad_number(first.group(1)) if first else None))
    split = enrich.split_ad_sections(html_text, ad or ad_header, amendment)
    out = {
        "v": _FT_CACHE_VERSION,
        "status": split["status"],
        "anchor": split["anchor"],
        "sections": split["sections"],
        "sha": hashlib.sha256(html_text.encode("utf-8")).hexdigest(),
        "compliance_time": None, "amoc": False, "docket": None, "supersedes": [],
        "ad_header": ad_header,
    }
    if split["status"] == "empty":
        return out
    low = txt.lower()
    out["amoc"] = (
        "alternative method of compliance" in low
        or "alternative methods of compliance" in low
    )
    out["compliance_time"] = enrich.extract_compliance_from_fulltext(txt)
    m = _FT_DOCKET_RE.search(txt)
    if m:
        out["docket"] = normalize_ad_number(m.group(1)).replace(" ", "-").upper()
    # Supersession: "This AD replaces AD 2020-06-14" /
    # "The FAA is superseding AD 2018-23-51" en el cuerpo del rule.
    out["supersedes"] = [
        normalize_ad_number(n) for n in dict.fromkeys(_FT_SUPERSEDE_RE.findall(txt))
    ]
    return out


def _fetch_fulltext_fields(doc_number: str, publication_date: str,
                           ad: str | None = None,
                           amendment: str | None = None) -> dict:
    """
    Baja el cuerpo COMPLETO del rule desde GovInfo (FR-{date}/granules/{docnum}/htm)
    y devuelve la entrada de cache v2 del documento. GovInfo se usa porque
    federalregister.gov bloquea (bot) los endpoints full_text. Requiere
    GOVINFO_API_KEY (leida en runtime; load_dotenv corre tras el import): sin
    ella no hay intento y el cache no se toca.

    `ad` y `amendment` (de docket_ids) anclan las secciones al encabezado de
    identidad del AD; sin `ad` se ancla al AD que el preambulo empareja con el
    amendment (`ad_header`, el mismo respaldo de identidad de _parse_document).
    """
    cache = _load_fulltext_cache()
    cached = cache.get(doc_number)
    key = os.environ.get("GOVINFO_API_KEY", "").strip()
    if not (_fulltext_pending(doc_number) and publication_date and key):
        return cached or {}

    _ft_attempted.add(doc_number)
    session = getattr(_ft_local, "session", None)
    if session is None:
        session = requests.Session()
        session.headers.update({"User-Agent": "AD-Scraper/1.0"})
        _ft_local.session = session
    url = (
        f"https://api.govinfo.gov/packages/FR-{publication_date}"
        f"/granules/{doc_number}/htm?api_key={key}"
    )
    try:
        resp = session.get(url, timeout=40)
        result = _body_fields(resp.text, ad, amendment) if resp.ok else None
    except requests.RequestException:
        result = None
    if result is None:  # HTTP != 2xx o red
        result = {"v": _FT_CACHE_VERSION, "status": "error", "anchor": None, "sections": []}

    if result["status"] in _FT_FINAL_STATUSES or not cached:
        cache[doc_number] = {"compliance_time": None, "amoc": False, "docket": None,
                             "supersedes": [], "ad_header": None, **result}
    else:
        # Intento fallido sobre una entrada con datos: se registra el intento
        # (para reintentarlo) sin perder lo que ya tenia.
        cache[doc_number] = {**cached, "v": _FT_CACHE_VERSION, "status": result["status"]}
    global _fulltext_dirty
    _fulltext_dirty = True
    return cache[doc_number]


def _applicability_section(ft: dict) -> str:
    """Cuerpo de la seccion Applicability del texto anclado, o ""."""
    if ft.get("status") != "extracted":
        return ""
    return next((s["body"] for s in ft.get("sections") or [] if s.get("kind") == "applicability"), "")


def _prefetch_fulltext(docs: list[dict]) -> None:
    """
    Calienta el cache full-text de una pagina de resultados en paralelo.

    El parseo despues corre en serie y encuentra todo cacheado. En serie, un
    backfill completo son ~13.5k GETs a GovInfo de ~1s cada uno (horas); con
    FULLTEXT_WORKERS hilos baja a minutos y sigue muy por debajo del limite de
    40 req/s de la API.
    """
    if not FETCH_FULL_TEXT:
        return
    pending = []
    for d in docs:
        num = (d.get("document_number") or "").strip()
        if not _fulltext_pending(num):
            continue
        ident = ad_identity_from_docket_ids(d.get("docket_ids"))
        pending.append((num, d.get("publication_date") or "", ident["ad"], ident.get("amendment")))

    if not pending:
        return
    log.info(f"[Federal Register]   prefetch full-text: {len(pending)} docs "
             f"({FULLTEXT_WORKERS} hilos)")
    with ThreadPoolExecutor(max_workers=FULLTEXT_WORKERS) as pool:
        list(pool.map(
            lambda a: _fetch_fulltext_fields(a[0], a[1], ad=a[2], amendment=a[3]),
            pending,
        ))


def _request_page(params: dict, retries: int = 3) -> dict | None:
    """Request con backoff exponencial en 429/5xx (helper comun). Un 4xx no se
    reintenta: no se cura solo."""
    resp = get_with_backoff(FEDERAL_REGISTER_API, params=params, headers=HEADERS,
                            timeout=60, retries=retries, tag="Federal Register")
    if resp is None:
        return None
    if resp.status_code >= 400:
        log.info(f"[Federal Register] HTTP {resp.status_code}")
        return None
    return resp.json()


def _search_year(year: int, max_pages: int = 50) -> list[dict]:
    """Trae todos los RULE docs de CFR 14/39 publicados en un año."""
    results = []
    page = 1

    while page <= max_pages:
        params = {
            "conditions[cfr][title]": "14",
            "conditions[cfr][part]": "39",
            "conditions[type][]": "RULE",
            "conditions[publication_date][gte]": f"{year}-01-01",
            "conditions[publication_date][lte]": f"{year}-12-31",
            "per_page": PER_PAGE,
            "page": page,
            "order": "oldest",
            "fields[]": _FIELDS,
        }

        data = _request_page(params)
        if data is None:
            break

        docs = data.get("results", [])
        total = data.get("count", 0)
        if not docs:
            break

        _prefetch_fulltext(docs)
        for doc in docs:
            ad = _parse_document(doc)
            if ad:
                results.append(ad)

        if len(docs) < PER_PAGE or len(results) >= total:
            break

        page += 1
        time.sleep(0.3)

    log.info(f"[Federal Register] {year}: {len(results)} ADs")
    return results


def _collapse_by_ad_number(rows: list[dict]) -> list[dict]:
    """
    Un AD = una fila. Varios documentos FR pueden compartir numero AD: el rule
    original mas sus "Final rule; correction". El canonico es el rule (nunca la
    correccion) y, entre varios, el de publicacion mas temprana — el documento
    que ESTABLECE el AD, que es el PDF que el usuario espera abrir.

    Los demas documentos quedan listados en Related_FR_Docs para no perderlos.
    Sin este colapso el dedupe last-wins del upsert dejaba ganar a la
    correccion (llega despues en orden cronologico): asi es como AD 2023-12-03
    termino apuntando al PDF de una correccion de tres paginas.
    """
    by_ad: dict[str, list[dict]] = {}
    for r in rows:
        by_ad.setdefault(r["AD_Number"], []).append(r)

    collapsed, multi = [], 0
    for ad, group in by_ad.items():
        if len(group) == 1:
            collapsed.append(group[0])
            continue
        multi += 1
        ordered = sorted(
            group,
            key=lambda r: (bool(r.get("Is_Correction")), r.get("Publication_Date") or ""),
        )
        canonical, rest = ordered[0], ordered[1:]
        canonical["Related_FR_Docs"] = [
            {
                "doc": r.get("FR_DocNumber"),
                "published": r.get("Publication_Date"),
                "status": r.get("Status"),
                "pdf": r.get("PDF_Link"),
            }
            for r in rest
        ]
        collapsed.append(canonical)

    if multi:
        log.info(f"[Federal Register] {multi} numeros AD con varios documentos FR (colapsados)")
    return collapsed


def search_ads_by_year_range(year_from: int, year_to: int) -> list[dict]:
    """Itera año por año y agrega resultados."""
    all_results = []
    for yr in range(year_from, year_to + 1):
        all_results.extend(_search_year(yr))
        _save_fulltext_cache()  # persistir progreso del cache full-text por anio
    log.info(f"[Federal Register] Total {year_from}-{year_to}: {len(all_results)} documentos con numero AD")
    return _collapse_by_ad_number(all_results)


def search_ads(keyword=None, make=None, model=None, product_type=None,
               date_from=None, date_to=None, max_pages=10):
    """
    Compat shim: si no se pasan filtros, hace el year-split completo.
    Mantiene la firma antigua para no romper callers.
    """
    if not any([keyword, make, model, product_type, date_from, date_to]):
        return search_ads_by_year_range(FR_API_START_YEAR, datetime.now().year)

    # Caso filtrado: una sola query (cap aplica si supera 10k, raro con filtros)
    search_terms = [t for t in [keyword, make, model, product_type] if t]
    search_query = " ".join(search_terms) if search_terms else None

    all_results = []
    page = 1
    while page <= max_pages:
        params = {
            "conditions[cfr][title]": "14",
            "conditions[cfr][part]": "39",
            "conditions[type][]": "RULE",
            "per_page": PER_PAGE,
            "page": page,
            "order": "newest",
            "fields[]": _FIELDS,
        }
        if search_query:
            params["conditions[term]"] = search_query
        if date_from:
            params["conditions[publication_date][gte]"] = date_from
        if date_to:
            params["conditions[publication_date][lte]"] = date_to

        data = _request_page(params)
        if data is None:
            break
        docs = data.get("results", [])
        if not docs:
            break
        _prefetch_fulltext(docs)
        for doc in docs:
            ad = _parse_document(doc)
            if ad:
                all_results.append(ad)
        if len(docs) < PER_PAGE:
            break
        page += 1
        time.sleep(0.3)

    return _collapse_by_ad_number(all_results)


def _parse_document(doc) -> dict | None:
    """Parsea un documento del Federal Register a formato estandar."""
    try:
        title = doc.get("title", "") or ""
        abstract = doc.get("abstract", "") or ""
        doc_number = doc.get("document_number", "") or ""
        pub_date = doc.get("publication_date", "") or ""

        # Identidad del AD: `docket_ids` del propio documento. Es el header
        # regulatorio estructurado y cubre ~99% de los RULE (13405/13509).
        ident = ad_identity_from_docket_ids(doc.get("docket_ids"))
        ad_number = ident["ad"] or ""

        # Full text: secciones, compliance/AMOC/supersession y — solo si
        # docket_ids no trajo el numero — el respaldo anclado al amendment del
        # corchete del preambulo (`ad_header`). El campo 'ad' de las entradas
        # v1 se ignora a proposito: venia del primer "AD nnnn-nn-nn" del
        # cuerpo, que suele ser el AD superseded.
        ft = {}
        if FETCH_FULL_TEXT:
            ft = _fetch_fulltext_fields(
                doc_number, pub_date,
                ad=ad_number or None,
                amendment=ident.get("amendment"),
            )
            if not ad_number and ft.get("ad_header"):
                ad_number = ft["ad_header"]

        ad_number = normalize_ad_number(ad_number)
        if not ad_number:
            # Sin numero AD verificable: se descarta. Usar el document_number
            # como ad_number (comportamiento historico) inventa un AD que no
            # existe y luego no concuerda con el PDF mostrado.
            return None

        docket = ident.get("docket") or ft.get("docket") or doc_number

        subject = re.sub(r"^Airworthiness Directives[;:]\s*", "", title, flags=re.I)

        effective_date = doc.get("effective_on", "") or doc.get("publication_date", "")
        pdf_link = doc.get("pdf_url", "") or doc.get("html_url", "")

        doc_type = (doc.get("type") or "").upper()
        action_field = (doc.get("action") or "").lower()
        title_lower = title.lower()

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
            status = "Final Rule"

        # El abstract va con sus mayusculas: el titular se identifica sobre el
        # (classify_product_type lo pasa a minusculas para el respaldo).
        product_type = classify_product_type(
            subject, abstract, doc.get("cfr_references") or []
        )

        out = {
            "Source": "FAA (Federal Register)",
            "AD_Number": ad_number.strip(),
            "Subject": subject.strip(),
            # Abstract = FAA AD summary; enrich.py mines it for unsafe_condition,
            # corrective_action and Service Bulletin refs.
            "Abstract": abstract.strip(),
            "Publication_Date": pub_date,
            "Effective_Date": effective_date,
            "Status": status,
            "Product_Type": product_type,
            "Docket": docket,
            # FR document_number estable — clave de join con GovInfo MODS en
            # _merge_faa_results(). Se mantiene aunque Docket pase a ser el
            # docket real FAA-xxxx-xxxx.
            "FR_DocNumber": doc_number,
            "PDF_Link": pdf_link,
            "Amendment": ident.get("amendment") or "",
            "Project_Identifier": ident.get("project") or "",
            "Citation": doc.get("citation") or "",
            # Correcciones y el rule original comparten numero AD; el colapso
            # de _collapse_by_ad_number() usa esta bandera para elegir el doc
            # canonico (el rule, no la correccion).
            "Is_Correction": status == "Correction",
            "Correction_Of": (
                (doc.get("correction_of") or {}).get("document_number")
                if isinstance(doc.get("correction_of"), dict)
                else (doc.get("correction_of") or "")
            ) or "",
        }

        # Titular y modelos: Make/Model escalares (ads.make/ads.model) y
        # Applicability [{make, models[]}] para ad_applicability, de una sola
        # extraccion sobre titulo + abstract y, si el texto del rule quedo
        # anclado, su seccion Applicability (los modelos salen de ahi antes
        # que del abstract). Sin titular inequivoco no hay Applicability ni
        # Make y el AD viaja `failed` (docs/applicability-contract.md).
        out.update(applicability_fields(title, abstract, _applicability_section(ft)))

        # compliance_time + AMOC ya vienen del full text bajado arriba (ft).
        if ft.get("compliance_time"):
            out["Compliance_Time"] = ft["compliance_time"]
        out["AMOC_Available"] = bool(ft.get("amoc"))

        # Supersession: el rule nuevo declara a cuales ADs reemplaza. El resolver
        # de populate_relations() marca esos ADs viejos como Superseded -> este.
        supersedes = [n for n in (ft.get("supersedes") or []) if n and n != out["AD_Number"]]
        if supersedes:
            out["Supersedes"] = ", ".join(supersedes)

        return out
    except Exception as e:
        log.info(f"[Federal Register] Error parseando: {e}")
        return None


def get_recent_ads(days=30, max_results=200):
    """Obtiene los ADs mas recientes del Federal Register."""
    from datetime import timedelta
    date_from = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    return search_ads(date_from=date_from, max_pages=max_results // PER_PAGE + 1)
