"""
Zephr - populate_db.py
Ejecuta todos los scrapers y persiste los resultados en Supabase.

Uso:
    python populate_db.py                        # todas las fuentes
    python populate_db.py --source easa          # fuente individual
    python populate_db.py --dry-run              # scrape pero no guarda
    python populate_db.py --drs                  # sync FAA DRS (requiere DRS_API_ENABLED)
"""

import argparse
import hashlib
import json
import logging
import os
import re
import signal
import sys
import traceback
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

from utils import normalize_ad_number
import enrich

# Agregar directorio raiz al path

from scrapers import faa_drs_ads, faa_drs_api, faa_drs_sync, federal_register
from scrapers.common.easa_document_class import DOCUMENT_CLASSES
from scrapers.federal_register import search_ads as fr_search
from scrapers.faa_govinfo import search_ads as govinfo_search
from scrapers.easa import (
    search_ads as easa_search,
    search_main_page as easa_main,
    enrich_details as easa_enrich_details,
)
from scrapers.transport_canada import (
    search_ads as tc_search,
    enrich_details as tc_enrich_details,
)
from scrapers.anac_brazil import search_ads as anac_brazil_search
from scrapers.dgac_chile import search_ads as dgac_chile_search
from scrapers.anac_argentina import search_ads as anac_argentina_search
from scrapers.aerocivil_colombia import search_ads as aerocivil_search
from scrapers.casa_australia import search_ads as casa_search
from scrapers.gcaa_uae import search_ads as gcaa_search
from scrapers.uk_caa import search_ads as uk_caa_search

load_dotenv()

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constantes
# ---------------------------------------------------------------------------
SOURCE_LABELS = {
    "federal_register": "FAA",
    "easa": "EASA",
    "transport_canada": "Transport Canada",
    "anac_brazil": "ANAC Brasil",
    "dgac_chile": "DGAC Chile",
    "anac_argentina": "ANAC Argentina",
    "aerocivil_colombia": "AEROCIVIL Colombia",
    "casa_australia": "CASA Australia",
    "gcaa_uae": "GCAA UAE",
    "uk_caa": "UK CAA",
}

ALL_SOURCES = list(SOURCE_LABELS.keys())

# ---------------------------------------------------------------------------
# Fuentes deshabilitadas del run diario (verificado en vivo 2026-07-28; los
# comentarios de cada scraper ya lo sospechaban, esto lo confirma):
#   - aerocivil_colombia: el sitio se mudo a SharePoint. El WAF devuelve 403 a
#     CUALQUIER request simple, headers de navegador real incluidos; la propia
#     REST API de SharePoint tambien 403. Necesita browser real (Playwright).
#   - casa_australia: bloqueo a nivel de red — las 6 URLs candidatas dan
#     timeout o ConnectionError, ni siquiera llegan a servir HTML. Probable
#     bloqueo por IP/datacenter o challenge JS que nunca responde a un GET liso.
#   - gcaa_uae: el sitio SI responde (200, ~1.3MB) y no esta bloqueado — pero
#     el listado de ADs vive detras de un postback ASP.NET AJAX
#     (UpdatePanel/ScriptManager); el HTML inicial no trae ninguna fila de
#     datos. Es el unico de los tres arreglable sin pelear un WAF: necesita
#     Playwright para ejecutar el postback, no un fix de regex.
#
# El cron diario (`python populate_db.py`, sin --source) las salta para no
# reportar "completed, 0 ADs" cada dia como si fuera cobertura real. Siguen
# corribles a mano (`--source gcaa_uae`) para debug o cuando se les ponga
# Playwright.
# ---------------------------------------------------------------------------
DISABLED_SOURCES = {"aerocivil_colombia", "casa_australia", "gcaa_uae"}
DAILY_SOURCES = [s for s in ALL_SOURCES if s not in DISABLED_SOURCES]

# ---------------------------------------------------------------------------
# Supabase client
# ---------------------------------------------------------------------------
def get_supabase_client():
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError(
            "SUPABASE_URL y SUPABASE_SERVICE_KEY son requeridos en el archivo .env"
        )
    return create_client(url, key)


# ---------------------------------------------------------------------------
# Date parser
# ---------------------------------------------------------------------------
_DATE_FORMATS = [
    "%Y-%m-%d",          # 2025-03-21
    "%d/%m/%Y",          # 21/03/2025
    "%d %b %Y",          # 21 Mar 2025
    "%d %B %Y",          # 21 March 2025
    "%B %d, %Y",         # March 21, 2025
    "%b %d, %Y",         # Mar 21, 2025
    "%Y/%m/%d",          # 2025/03/21
    "%m/%d/%Y",          # 03/21/2025
    "%d-%m-%Y",          # 21-03-2025
    "%d-%b-%Y",          # 21-Mar-2025
]

def parse_date(raw: str) -> str | None:
    """Intenta convertir raw a YYYY-MM-DD. Retorna None si no puede."""
    if not raw:
        return None
    raw = raw.strip()
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    # Intentar extraer una fecha con regex como ultimo recurso
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", raw)
    if m:
        return f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
    return None


# ---------------------------------------------------------------------------
# Status mapping  (raw source value -> normalized DB enum)
# DB constraint ads_status_chk allows: New | Revision | Superseded | Cancelled | NULL
# ---------------------------------------------------------------------------
_STATUS_MAP = {
    "current":             "New",
    "final rule":          "New",
    "final":               "New",
    "active":              "New",
    "issued":              "New",
    "effective":           "New",
    "in force":            "New",
    "mandatory":           "New",
    "emergency":           "New",
    "interim":             "New",
    "notice":              "New",
    "amendment":           "Revision",
    "amended":             "Revision",
    "corrected":           "Revision",
    "correction":          "Revision",
    "corrected amendment": "Revision",
    "revision":            "Revision",
    "revised":             "Revision",
    "re-issued":           "Revision",
    "superseded":          "Superseded",
    "replaced":            "Superseded",
    "cancelled":           "Cancelled",
    "canceled":            "Cancelled",
    "withdrawn":           "Cancelled",
    "rescinded":           "Cancelled",
    "terminated":          "Cancelled",
    "void":                "Cancelled",
}


def _map_status(raw):
    if not raw:
        return None
    return _STATUS_MAP.get(raw.strip().lower(), None)

# ---------------------------------------------------------------------------
# Normalizer
# ---------------------------------------------------------------------------
def normalize(row: dict, source_key: str) -> dict | None:
    """
    Mapea el dict de un scraper al esquema de la tabla 'ads'.
    Retorna None si ad_number esta vacio.

    Ademas del mapeo base, deriva el modelo de dominio AD completo via enrich:
    issuing_authority + state_of_design, status/priority/product_type canonicos,
    y best-effort de unsafe_condition / corrective_action / compliance_time /
    amoc desde el texto disponible (Subject + Abstract si el scraper lo aporta).
    """
    ad_number = normalize_ad_number(row.get("AD_Number") or "")
    if not ad_number:
        return None

    subject = (row.get("Subject") or "").strip() or None
    abstract = (row.get("Abstract") or "").strip()
    raw_status = (row.get("Status") or "").strip() or None

    # Explicit scraper-provided values win; otherwise derive from available text.
    unsafe = (row.get("Unsafe_Condition") or "").strip() or enrich.extract_unsafe_condition(abstract)
    corrective = (row.get("Corrective_Action") or "").strip() or enrich.extract_corrective_action(abstract)
    compliance_time = (row.get("Compliance_Time") or "").strip() or enrich.extract_compliance_time(abstract, subject or "")
    amoc = bool(row.get("AMOC_Available")) or enrich.detect_amoc(abstract, subject or "")

    # Quien EMITIO el AD, que no siempre es la base donde lo leimos: cada
    # autoridad republica lo que reconoce. El estado de diseno se deriva del
    # emisor, no del corpus — antes un AD canadiense leido en EASA quedaba
    # guardado como "European Union". Ver enrich.issuing_authority().
    issuer = enrich.issuing_authority(
        ad_number, source_key, (row.get("PDF_Link") or "").strip() or None
    )

    record = {
        "source": source_key,
        "source_label": SOURCE_LABELS.get(source_key, source_key),
        "ad_number": ad_number,
        "issuing_authority": issuer,
        "state_of_design": enrich.state_of_design(issuer),
        "subject": subject,
        "make": (row.get("Make") or "").strip() or None,
        "model": (row.get("Model") or "").strip() or None,
        "product_type": enrich.normalize_product_type(row.get("Product_Type")),
        "published_date": parse_date(row.get("Publication_Date") or row.get("Published_Date")),
        "effective_date": parse_date(row.get("Effective_Date")),
        "status": _map_status(row.get("Status")),
        "priority": enrich.priority(raw_status, subject or "", row.get("Type") or ""),
        "unsafe_condition": unsafe or None,
        "corrective_action": corrective or None,
        "compliance_time": compliance_time or None,
        "amoc_available": amoc,
        "docket": (row.get("Docket") or "").strip() or None,
        "pdf_link": (row.get("PDF_Link") or "").strip() or None,
        "raw_data": row,
    }

    # ads.document_class: solo EASA la declara, y solo se manda cuando se
    # conoce. Una fila sin la clave no menciona la columna; ojo que en un lote
    # donde otra fila si la trae, PostgREST la completa con NULL (columns =
    # union de claves, missing=null), que es lo mismo que "desconocida".
    doc_class = row.get("Document_Class")
    if source_key == "easa" and doc_class in DOCUMENT_CLASSES:
        record["document_class"] = doc_class
    return record


# ---------------------------------------------------------------------------
# Dedup en memoria por (source, ad_number)
# ---------------------------------------------------------------------------
# Campos cuya presencia hace a una fila mas completa/confiable que otra con el
# mismo (source, ad_number). pdf_link pesa mas que el resto junto: una fila sin
# documento es estrictamente peor que cualquiera que lo tenga, sin importar que
# tan lleno este el resto.
_DEDUP_FIELDS = (
    "subject", "make", "model", "product_type", "effective_date",
    "docket", "unsafe_condition", "corrective_action", "compliance_time",
)


def _completeness_score(row: dict) -> tuple[int, int]:
    """(tiene_pdf, campos_no_vacios) â€” mayor es mejor."""
    has_pdf = 1 if row.get("pdf_link") else 0
    filled = sum(1 for f in _DEDUP_FIELDS if row.get(f))
    return (has_pdf, filled)


def _dedup_rows(rows: list[dict]) -> list[dict]:
    """
    Un (source, ad_number) puede repetirse dentro de UN mismo scrape (ej.
    Transport Canada devuelve el mismo AD por cada modelo al que aplica). Se
    queda la fila mas completa, no la ultima vista: last-wins puramente
    posicional es el mismo defecto que dejo a AD FAA 2023-12-03 con el PDF de
    su propia correccion en vez del rule â€” aqui pasa un nivel mas arriba, entre
    filas de un mismo scraper en vez de entre documentos FR.
    """
    best: dict[tuple, dict] = {}
    for r in rows:
        key = (r["source"], r["ad_number"])
        prev = best.get(key)
        if prev is None or _completeness_score(r) > _completeness_score(prev):
            best[key] = r
    return list(best.values())


# ---------------------------------------------------------------------------
# Upsert
# ---------------------------------------------------------------------------
def upsert_ads(supabase, rows: list[dict]) -> tuple[int, int]:
    """
    Inserta o actualiza ADs en la tabla 'ads'.
    Retorna (ads_new, ads_updated) â€” conteos aproximados basados en
    si el registro ya existia antes del upsert.
    """
    if not rows:
        return 0, 0

    rows = _dedup_rows(rows)
    logger.info(f"Filas tras dedup: {len(rows)}")

    # Verificar cuales ya existen para contar new vs updated
    source_vals = list({r["source"] for r in rows})

    # Se pagina porque PostgREST limita a 1000 filas por respuesta: sin esto,
    # con >1000 filas ya en BD casi todo se contaba como "nuevo" y los campos
    # ads_new/ads_updated de scrape_runs (el panel de salud) quedaban falseados.
    existing: set[tuple] = set()
    try:
        page = 0
        PAGE = 1000
        while True:
            existing_resp = (
                supabase.table("ads")
                .select("source, ad_number")
                .in_("source", source_vals)
                .order("id")
                .range(page * PAGE, page * PAGE + PAGE - 1)
                .execute()
            )
            chunk = existing_resp.data or []
            for rec in chunk:
                existing.add((rec["source"], rec["ad_number"]))
            if len(chunk) < PAGE:
                break
            page += 1
    except Exception as e:
        _record_write_error(f"No se pudo pre-verificar existentes (ads_new/updated falseados): {e}")
        existing = set()

    # 'linked' = pares (source, ad_number) que ya tienen un enlace de
    # supersedencia (superseded_to_ad_id) puesto por el proceso de linking.
    # El re-scrape diario NO debe pisar su status: la constraint
    # ads_superseded_coherence_chk exige status='Superseded' siempre que
    # superseded_to_ad_id no sea NULL. Forzamos ese status en el payload.
    # Se pagina porque PostgREST limita a 1000 filas por respuesta.
    linked: set[tuple] = set()
    try:
        page = 0
        PAGE = 1000
        while True:
            resp = (
                supabase.table("ads")
                .select("source, ad_number")
                .in_("source", source_vals)
                .not_.is_("superseded_to_ad_id", "null")
                .order("id")
                .range(page * PAGE, page * PAGE + PAGE - 1)
                .execute()
            )
            chunk = resp.data or []
            for rec in chunk:
                linked.add((rec["source"], rec["ad_number"]))
            if len(chunk) < PAGE:
                break
            page += 1
        logger.info(f"ADs con enlace de supersedencia (status forzado): {len(linked)}")
    except Exception as e:
        _record_write_error(f"No se pudo pre-verificar superseded links: {e}")
        linked = set()

    ads_new = sum(1 for r in rows if (r["source"], r["ad_number"]) not in existing)
    ads_updated = len(rows) - ads_new

    # Upsert en lotes de 500 para no exceder limites de Supabase
    BATCH = 500
    for i in range(0, len(rows), BATCH):
        batch = rows[i : i + BATCH]
        payload = []
        for r in batch:
            rec = dict(r)
            # raw_data es jsonb: se guarda como OBJETO. json.dumps() aqui metia
            # un string JSON dentro del jsonb (doble codificado), y entonces
            # raw_data->>'FR_DocNumber' devolvia null en SQL.
            rec["raw_data"] = json.loads(
                json.dumps(rec["raw_data"], ensure_ascii=False, default=str)
            )
            rec["last_updated_at"] = datetime.now(timezone.utc).isoformat()
            # Preservar coherencia con superseded_to_ad_id existente.
            if (rec["source"], rec["ad_number"]) in linked:
                rec["status"] = "Superseded"
            payload.append(rec)

        supabase.table("ads").upsert(
            payload,
            on_conflict="source,ad_number",
        ).execute()

    return ads_new, ads_updated


# ---------------------------------------------------------------------------
# Write-error accounting
#
# A run that lost data must never report itself as 'completed': every failed
# write (and every failed pre-read that falsifies the health counters) is
# recorded here, forces scrape_runs.status='error' for its source, and makes
# the process exit non-zero so GitHub Actions shows red.
# ---------------------------------------------------------------------------
_write_errors: list[str] = []


def _record_write_error(msg: str) -> None:
    _write_errors.append(msg)
    logger.error(msg)


# ---------------------------------------------------------------------------
# Relations: ad_applicability, ad_service_bulletins, supersession
# ---------------------------------------------------------------------------
def _fetch_ad_ids(supabase, source: str, ad_numbers: list[str]) -> dict[str, str]:
    """Mapea ad_number -> ads.id para una fuente (en lotes para PostgREST)."""
    ids: dict[str, str] = {}
    BATCH = 300
    uniq = list({n for n in ad_numbers if n})
    for i in range(0, len(uniq), BATCH):
        chunk = uniq[i : i + BATCH]
        try:
            resp = (
                supabase.table("ads")
                .select("id, ad_number")
                .eq("source", source)
                .in_("ad_number", chunk)
                .execute()
            )
            for rec in resp.data or []:
                ids[rec["ad_number"]] = rec["id"]
        except Exception as e:
            _record_write_error(f"  relations: no se pudieron leer ids ({source}): {e}")
    return ids


# Limites separados de `replace_ad_applicability` (INT-3, migracion v2
# 20260926130000): make y model tienen topes distintos porque un solo holder
# puede legitimamente traer un listado largo de type designations
# ("737-600, -700, -800, ..."). Si la RPC los cambia, cambian aqui: enviar un
# campo mas largo aborta el LOTE ENTERO, no la fila. Lo vigila
# tests/test_applicability_pairs.py.
APPLICABILITY_MAKE_MAX = 256
APPLICABILITY_MODEL_MAX = 2000


class ApplicabilityError(Exception):
    """Un scraper puede marcar con esto que la extraccion de applicability de
    UN AD puntual fallo (ej. PDF de EASA ilegible, listado con estructura
    reconocida pero corrupta). No es "no hay datos" (eso es success_empty)
    sino "no se pudo leer" (failed): la AD entera queda `failed` y sus filas
    guardadas no se tocan; nunca se declara un subconjunto parcial como si
    fuera la verdad completa. `_applicability_pairs` la lanza tambien cuando
    `raw_data["Applicability"]` trae un holder que no se puede interpretar."""


def _holder_pair(holder) -> tuple[str, str | None]:
    """El par (make, model) de un elemento de `raw_data["Applicability"]`, o
    ApplicabilityError si el elemento no se puede interpretar entero: sin
    make, `models` que no es lista (un string se recorreria como caracteres)
    o un modelo que no es string. Un modelo en blanco no es un modelo."""
    if not isinstance(holder, dict):
        raise ApplicabilityError(f"holder que no es objeto: {holder!r:.80}")
    make = holder.get("make")
    if not isinstance(make, str) or not make.strip():
        raise ApplicabilityError(f"holder sin make: {holder!r:.80}")
    models = holder.get("models", [])
    if not isinstance(models, list) or not all(isinstance(m, str) for m in models):
        raise ApplicabilityError(f"models no es una lista de strings: {holder!r:.80}")
    models = [m.strip() for m in models if m.strip()]
    return make.strip(), ", ".join(models) if models else None


def _applicability_pairs(row: dict) -> list[tuple[str, str | None]]:
    """Pares (make, model) para un AD, sin inventar combinaciones ni tokenizar.

    Camino estructurado: el scraper aporta `raw_data["Applicability"]` como
    [{make, models[]}] — un approval holder con SUS modelos. Es el unico camino
    correcto cuando un AD lista varios fabricantes, y EASA/TC lo permiten
    porque el listado anida los type designations dentro de cada holder. Cada
    holder produce UNA fila: sus modelos viajan unidos por ", " en el orden de
    la fuente, en vez de una fila por modelo. Partir "737-600, -700, -800" en
    tres filas aqui no gana nada — `ad_model_index`/`ad_model_family` en
    Postgres ya tokeniza el texto del modelo para el matching de flota — y
    perder la lista completa en una sola fila desperdicia el limite de 2000
    chars que la RPC reserva justo para esto.

    Camino legado (FAA/ANAC/DGAC/UK CAA, que nunca traen mas de un
    fabricante): `make` escalar y `model` completo tal cual lo dejo el
    scraper, en UNA fila — tampoco se parte por coma.

    Lo que este helper NO hace, en ningun camino, es partir `make` por coma.
    Los nombres de holder llevan comas propias ("REVO, Inc.", "McDermott 214
    Holdings, LLC"), asi que el join no es reversible: sobre las 765 filas
    afectadas de EASA, partir da 1972 tokens de los que 316 son sufijos
    societarios ("INC." x194, "LLC" x56, "GmbH" x42), y esos fragmentos
    terminarian como fabricantes en `ad_applicability` y como opciones del
    filtro Make via `aircraft_makes()`. Un `make` largo sin estructura que lo
    explique se reporta arriba contra APPLICABILITY_MAKE_MAX; no se convierte
    en varios fabricantes plausibles.
    """
    raw = row.get("raw_data") or {}
    if raw.get("Applicability_Error"):
        # Enganche para scrapers futuros que procesen detalle por-AD: una
        # extraccion que fallo a medias no es "el AD no aplica a nada".
        raise ApplicabilityError(str(raw["Applicability_Error"]))

    holders = raw.get("Applicability") if isinstance(raw, dict) else None
    if holders is not None and not isinstance(holders, list):
        raise ApplicabilityError(f"Applicability no es una lista: {holders!r:.80}")
    if holders:
        # Toda la estructura o nada: saltar un holder ilegible y declarar los
        # demas como `success_with_rows` le haria podar a la RPC las filas del
        # holder perdido.
        return [_holder_pair(holder) for holder in holders]

    make_field = (row.get("make") or "").strip()
    if not make_field:
        # Sin fabricante no hay fila que declarar: el AD queda `failed` mas
        # abajo, no con una fila invalida.
        return []
    if "," in make_field:
        # Una fuente que empaqueta fabricantes sin atribucion es una regresion
        # del scraper, no un dato a interpretar aqui. Se avisa y se sigue: la
        # fila entra tal cual y, si no cabe, la corta el limite de la RPC.
        logger.warning(
            f"  applicability: make con coma y sin estructura ({make_field[:80]!r}) "
            f"— el scraper deberia emitir raw_data['Applicability']"
        )
    model_field = (row.get("model") or "").strip()
    return [(make_field, model_field or None)]


# Igual que max_ads/max_total_rows de replace_ad_applicability (migracion D-21,
# 20260805100000). Un AD con varios approval holders puede aportar muchas filas
# (peor visto en produccion: >1200 filas para un solo AD EASA), asi que un lote
# fijo por CANTIDAD DE ADS puede igual pasarse del tope de filas de la RPC. El
# margen de 2x bajo max_total_rows deja aire para que ADs promedio (pocas filas)
# sigan viajando 300 a la vez sin acercarse al limite real de la RPC.
APPLICABILITY_BATCH_ADS = 300
APPLICABILITY_BATCH_ROWS = 10000


def _applicability_batches(payload: list[dict]) -> list[list[dict]]:
    """
    Parte `payload` en lotes acotados por CANTIDAD DE ADS y por FILAS
    ACUMULADAS. Un AD cuyas propias filas ya superan el tope de filas viaja
    SOLO en su lote: si la RPC lo rechaza (por ejemplo por pasarse de
    max_total_rows), el rechazo queda confinado a ese AD y no arrastra a los
    demas. Preserva el orden y no pierde ni duplica ningun elemento.
    """
    batches: list[list[dict]] = []
    batch: list[dict] = []
    batch_rows = 0

    def _flush():
        nonlocal batch, batch_rows
        if batch:
            batches.append(batch)
        batch, batch_rows = [], 0

    for element in payload:
        n_rows = len(element.get("rows") or [])
        if n_rows > APPLICABILITY_BATCH_ROWS:
            # Ni siquiera solo cabria con margen; se manda igual (la RPC es la
            # autoridad final) pero aislado, para no arrastrar a nadie mas.
            _flush()
            batches.append([element])
            continue
        if batch and (len(batch) >= APPLICABILITY_BATCH_ADS or batch_rows + n_rows > APPLICABILITY_BATCH_ROWS):
            _flush()
        batch.append(element)
        batch_rows += n_rows
    _flush()
    return batches


# SQLSTATE de deadlock. PostgREST lo devuelve en `code` y postgrest-py lo deja
# en `APIError.code`; cualquier otra excepcion (transporte incluido) no lo trae.
DEADLOCK_SQLSTATE = "40P01"


def _replace_applicability(supabase, batch: list[dict]):
    """Una llamada a `replace_ad_applicability` y, si Postgres la aborto por
    deadlock, UN reenvio del mismo lote. Cada intento es una llamada nueva: la
    transaccion abortada ya se revirtio entera, asi que reenviar no duplica
    nada. Si el segundo intento falla, ese error es el que sube; cualquier otro
    error no se reintenta."""
    try:
        return supabase.rpc("replace_ad_applicability", {"p_ads": batch}).execute()
    except Exception as e:
        if str(getattr(e, "code", "")) != DEADLOCK_SQLSTATE:
            raise
        logger.warning("  applicability: deadlock (40P01) en la RPC, se reenvia el lote una vez")
    return supabase.rpc("replace_ad_applicability", {"p_ads": batch}).execute()


def _send_applicability_batches(supabase, payload: list[dict]) -> dict[str, int]:
    """Envia cada lote de `_applicability_batches(payload)` a la RPC (con un
    reenvio ante deadlock, `_replace_applicability`) y lee la respuesta real
    (`ad_id, status, rows_now` por AD, en orden de payload).

    El status devuelto es la autoridad, no el declarado: la RPC puede
    degradar un elemento a `failed` en silencio (la migracion v2 lo hace con
    un `success_empty` sin `empty_evidence`, para no romper llamadores
    viejos). Sin leer `resp.data` ese degradado pasaria desapercibido — se
    cuenta y se loguea aqui.
    """
    counts: dict[str, int] = {"success_with_rows": 0, "success_empty": 0, "failed": 0}
    db_failed: list[str] = []
    for i, batch in enumerate(_applicability_batches(payload)):
        try:
            resp = _replace_applicability(supabase, batch)
            for rec in resp.data or []:
                st = rec.get("status")
                counts[st] = counts.get(st, 0) + 1
                if st == "failed":
                    db_failed.append(rec.get("ad_id"))
        except Exception as e:
            _record_write_error(f"  applicability: RPC replace fallo (lote {i + 1}): {e}")
    if db_failed:
        shown = db_failed[:20]
        suffix = f" (+{len(db_failed) - 20} mas)" if len(db_failed) > 20 else ""
        logger.warning(
            f"  applicability: la BD reporto {len(db_failed)} AD(s) failed: "
            f"{shown}{suffix}"
        )
    return counts


# ads.product_type (canonico via enrich.normalize_product_type) -> codigo del
# catalogo de la BD: enrich.PRODUCT_TYPE_CODE. None (AD sin tipo derivable) se
# omite del payload adrede: la RPC/el trigger ya sabe derivarlo de
# `ads.product_type` (mismo valor, resuelto server-side), y mandar 'unknown'
# explicito lo marcaria como product_type_source='explicit' para siempre,
# impidiendo que una correccion futura de ads.product_type se propague (eso
# solo pasa sobre filas 'derived'). product_subtype solo viaja junto a su
# product_type (la RPC rechaza uno sin el otro) y solo si enrich.product_subtype
# lo deriva con seguridad.


# `success_empty` no lo declara ningun extractor (docs/applicability-contract.md).
# Le ordena a la RPC borrar las filas guardadas del AD, asi que exige que la
# FUENTE declare explicitamente "no hay pares", y ninguna fuente activa lo
# hace: un listado sin make, un titulo generico o una lista de holders vacia
# son datos ausentes, no evidencia. En EASA, `Applicability == []` es que el
# selector `li.tc_holder` no encontro nada, y un cambio de markup da
# exactamente lo mismo que un AD sin holders. Sin pares, el AD viaja `failed`.


# ---------------------------------------------------------------------------
# Texto del AD en secciones (FAA): ad_text_documents / ad_text_sections
# ---------------------------------------------------------------------------
# Limites de `replace_ad_text_sections` (migracion 20260927100000 de
# ad-scrapper). Si la RPC los cambia, cambian aqui: un payload que se pase
# aborta la llamada del AD. Lo vigila tests/test_text_sections_payload.py.
TEXT_SECTIONS_MAX = 200
TEXT_FIELD_MAX = 2048      # cada campo del intento y cada heading
TEXT_LETTER_MAX = 8
TEXT_BODY_MAX = 200000
TEXT_PROVIDER = "govinfo"
# La RPC es de un AD por llamada (una transaccion por AD). Si falla esta
# cantidad de llamadas seguidas (RPC ausente, base caida) no se sigue
# martillando: el resto se registra como un solo error de escritura.
TEXT_MAX_CONSECUTIVE_FAILURES = 20


def _text_sections_over_limits(sections: list[dict]) -> str | None:
    """Motivo por el que la RPC rechazaria estas secciones, o None."""
    if len(sections) > TEXT_SECTIONS_MAX:
        return f"{len(sections)} sections, limit is {TEXT_SECTIONS_MAX}"
    for s in sections:
        heading, body, letter = s.get("heading") or "", s.get("body") or "", s.get("letter") or ""
        if not heading.strip() or not body.strip():
            return f"section {s.get('position')} without heading or body"
        if len(heading) > TEXT_FIELD_MAX or len(letter) > TEXT_LETTER_MAX or len(body) > TEXT_BODY_MAX:
            return f"section {s.get('position')} over the field limits"
    return None


def _text_payload(raw: dict, entry: dict) -> tuple[dict, list[dict], str | None]:
    """(p_attempt, p_sections, problema) de un AD FAA desde su entrada del
    cache full-text v2.

    El intento dice lo que encontro el parser (extracted | unanchored | empty
    | error); la base decide ok/partial con `ad_text_is_complete` y nunca
    reemplaza texto conservado con un intento peor. Por eso aqui no se decide
    nada sobre completitud ni se evita mandar un intento fallido: un `error`
    tras un texto valido se registra como intento y el texto queda.

    Solo un intento `extracted` lleva secciones (la RPC rechaza las demas). Si
    las secciones no caben en los limites de la RPC, el intento viaja como
    `error` sin secciones y se devuelve el problema para registrarlo: el AD
    queda con su texto anterior y la corrida en error.
    """
    doc = (raw.get("FR_DocNumber") or "").strip()
    status = entry.get("status")
    sections = [
        {"position": s.get("position"), "letter": s.get("letter"), "heading": s.get("heading"),
         "kind": s.get("kind"), "body": s.get("body")}
        for s in (entry.get("sections") or [])
    ] if status == "extracted" else []
    attempt = {
        "status": status,
        "provider": TEXT_PROVIDER,
        "document_id": doc,
        "source_url": federal_register.govinfo_source_url(raw.get("Publication_Date") or "", doc),
        "sha256": entry.get("sha"),
        "anchor": entry.get("anchor") if status == "extracted" else None,
        "detail": entry.get("detail"),
    }
    return _checked_text_attempt(attempt, sections)


def _checked_text_attempt(attempt: dict, sections: list[dict]) -> tuple[dict, list[dict], str | None]:
    """Un intento `extracted` cuyas secciones o ancla no caben en los limites
    de la RPC viaja como `error` sin secciones, con el problema devuelto para
    registrarlo. Comun a GovInfo y DRS."""
    status = attempt["status"]
    problem = _text_sections_over_limits(sections) if status == "extracted" else None
    if problem is None and (attempt["anchor"] or "") and len(attempt["anchor"]) > TEXT_FIELD_MAX:
        problem = f"anchor over {TEXT_FIELD_MAX} characters"
    if problem:
        attempt.update(status="error", anchor=None, detail=f"sections not sent: {problem}")
        sections = []
    if attempt["detail"]:
        attempt["detail"] = attempt["detail"][:TEXT_FIELD_MAX]
    return attempt, sections, problem


def _stored_text_state(supabase, ad_ids: list[str]) -> dict[str, str | None] | None:
    """ad_id -> text_sha256 de ad_text_documents (None si el AD tiene intentos
    pero ningun texto conservado). Un AD sin fila no aparece. None si la
    lectura fallo (registrado)."""
    kept = _kept_text(supabase, ad_ids)
    return None if kept is None else {ad_id: rec.get("text_sha256") for ad_id, rec in kept.items()}


def _kept_text(supabase, ad_ids: list[str]) -> dict[str, dict] | None:
    """ad_id -> {text_sha256, text_provider} de ad_text_documents (los dos
    None si el AD tiene intentos pero ningun texto conservado). Un AD sin
    fila no aparece. None si la lectura fallo (registrado)."""
    kept: dict[str, dict] = {}
    BATCH = 300
    for i in range(0, len(ad_ids), BATCH):
        try:
            resp = (supabase.table("ad_text_documents")
                    .select("ad_id, text_sha256, text_provider")
                    .in_("ad_id", ad_ids[i:i + BATCH])
                    .execute())
            for rec in resp.data or []:
                kept[rec["ad_id"]] = {"text_sha256": rec.get("text_sha256"),
                                      "text_provider": rec.get("text_provider")}
        except Exception as e:
            _record_write_error(f"  text: no se pudo leer ad_text_documents: {e}")
            return None
    return kept


def _text_needs_send(entry: dict, fresh: bool, stored: dict[str, str | None] | None, ad_id: str) -> bool:
    """Un intento de esta corrida se manda siempre: es el ultimo intento real
    del AD. Uno guardado en el cache de una corrida anterior solo si la base
    todavia no lo tiene: sin fila en ad_text_documents, o un `extracted` sin
    texto conservado. Reenviarlo cada noche falsearia `last_attempt_at` y
    reescribiria las mismas secciones de ~13.5k ADs."""
    if fresh:
        return True
    if stored is None:  # la pre-lectura fallo: solo lo que es de hoy
        return False
    if ad_id not in stored:
        return True
    return entry.get("status") == "extracted" and stored[ad_id] is None


def _send_text_sections(supabase, payload: list[tuple[str, dict, list[dict]]]) -> dict[str, int]:
    """Una llamada a `replace_ad_text_sections` por AD y el estado que la base
    guardo (ok | partial | unanchored | empty | error), contado. Cada fallo
    de la RPC es un error de escritura; tras TEXT_MAX_CONSECUTIVE_FAILURES
    seguidos se abandona el resto con un solo error."""
    counts: dict[str, int] = {}
    consecutive = 0
    for n, (ad_id, attempt, sections) in enumerate(payload):
        try:
            resp = supabase.rpc("replace_ad_text_sections", {
                "p_ad_id": ad_id, "p_attempt": attempt, "p_sections": sections,
            }).execute()
            stored = resp.data if isinstance(resp.data, str) else "unknown"
            counts[stored] = counts.get(stored, 0) + 1
            consecutive = 0
        except Exception as e:
            _record_write_error(f"  text: RPC replace_ad_text_sections fallo ({ad_id}): {e}")
            counts["rpc_failed"] = counts.get("rpc_failed", 0) + 1
            consecutive += 1
            if consecutive >= TEXT_MAX_CONSECUTIVE_FAILURES:
                left = len(payload) - n - 1
                if left:
                    _record_write_error(
                        f"  text: {consecutive} fallos seguidos de la RPC, {left} AD(s) sin enviar"
                    )
                    counts["not_sent"] = left
                break
    return counts


def populate_text_sections(supabase, normalized: list[dict], id_map: dict[str, str]) -> dict[str, int]:
    """Texto de los ADs FAA hacia ad_text_documents/ad_text_sections, desde el
    cache full-text v2 que dejo la corrida (`federal_register.fulltext_entry`).

    Mismo `id_map` que applicability (ad_number -> ads.id de `_fetch_ad_ids`).
    Un AD sin entrada v2 (sin GOVINFO_API_KEY no hay intento) no viaja: no hay
    intento que registrar. Devuelve los conteos del estado que guardo la base.
    """
    by_ad: dict[str, tuple[dict, dict, bool]] = {}
    for r in normalized:
        ad_id = id_map.get(r["ad_number"])
        raw = r.get("raw_data") or {}
        doc = (raw.get("FR_DocNumber") or "").strip()
        entry = federal_register.fulltext_entry(doc)
        if not ad_id or not entry or entry.get("v") != 2:
            continue
        by_ad[ad_id] = (raw, entry, federal_register.fulltext_attempted(doc))
    if not by_ad:
        return {}

    stored = None
    if any(not fresh for _, _, fresh in by_ad.values()):
        stored = _stored_text_state(supabase, list(by_ad))
    payload = []
    for ad_id, (raw, entry, fresh) in by_ad.items():
        if not _text_needs_send(entry, fresh, stored, ad_id):
            continue
        attempt, sections, problem = _text_payload(raw, entry)
        if problem:
            _record_write_error(
                f"  text: {raw.get('AD_Number') or ad_id}: secciones fuera de limite ({problem}), "
                f"se registra el intento como error"
            )
        payload.append((ad_id, attempt, sections))

    counts = _send_text_sections(supabase, payload)
    logger.info(
        f"[federal_register] texto: {len(payload)} intentos enviados de {len(by_ad)} ADs con entrada "
        f"({sum(1 for *_, f in by_ad.values() if f)} de esta corrida); BD: "
        + ", ".join(f"{k}={v}" for k, v in sorted(counts.items()))
    )
    return counts


# Texto de DRS (`drs_documents.regulatory_text`, texto plano) para los ADs FAA
# sin texto de GovInfo conservado: los anteriores a 1994, que GovInfo no
# tiene, y los que GovInfo no pudo anclar. Mismo parser (modo texto plano),
# mismos estados de intento y misma RPC; la base aplica la misma regla de
# completitud y nunca reemplaza texto conservado con un intento peor.
TEXT_PROVIDER_DRS = "drs"
DRS_TEXT_DOCTYPE = "ADFRAWD"   # ADFREAD no trae texto regulatorio
DRS_TEXT_BATCH = 500           # numeros de AD por lectura de drs_documents


def _drs_text_payload(record: dict) -> tuple[dict, list[dict], str | None]:
    """(p_attempt, p_sections, problema) de un AD desde su documento DRS,
    anclado al numero del propio AD como en GovInfo. El amendment no se
    exige: el de DRS puede ser el de la ultima enmienda ("Amendment 39-5571
    as amended by Amendment 39-6115")."""
    ad = record["ad_number"]
    text = record.get("regulatory_text") or ""
    parsed = enrich.split_ad_sections(text, ad, plain=True)
    status = parsed["status"]
    detail = {
        "unanchored": f"no regulatory header in the DRS text declares AD {ad}",
        "empty": "the DRS document has no regulatory text",
        "error": "anchored DRS text without sections",
    }.get(status)
    attempt = {
        "status": status,
        "provider": TEXT_PROVIDER_DRS,
        "document_id": record.get("document_guid"),
        "source_url": record.get("document_url"),
        "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest() if text.strip() else None,
        "anchor": parsed["anchor"] if status == "extracted" else None,
        "detail": detail,
    }
    sections = [dict(s) for s in parsed["sections"]] if status == "extracted" else []
    return _checked_text_attempt(attempt, sections)


def _drs_text_needs_send(kept: dict | None, sha256: str | None) -> bool:
    """El texto de DRS solo entra donde no hay texto de GovInfo conservado:
    AD sin fila en ad_text_documents, con intentos pero sin texto, o con
    texto de DRS que cambio. Un texto de GovInfo conservado nunca se toca, y
    uno de DRS igual al que ya esta no se reenvia."""
    if kept is None or not kept.get("text_sha256"):
        return True
    return kept.get("text_provider") == TEXT_PROVIDER_DRS and kept.get("text_sha256") != sha256


def populate_drs_text_sections(supabase, store, ad_numbers, *, dry_run: bool = False) -> dict[str, int]:
    """Intentos de texto desde DRS para los ADs FAA de `ad_numbers` (los de
    las paginas subidas en la corrida). El documento de cada AD es el mismo
    que elige `link_ads_to_drs` (`faa_drs_ads.pick_document`). Un AD sin fila
    en `ads` no viaja. Devuelve los conteos del parseo (`parsed_<estado>`) y
    del estado que guardo la base."""
    counts: dict[str, int] = {}

    def bump(key: str, n: int = 1) -> None:
        counts[key] = counts.get(key, 0) + n

    numbers = sorted(set(ad_numbers))
    for i in range(0, len(numbers), DRS_TEXT_BATCH):
        groups = faa_drs_ads.group_by_ad(store.documents(DRS_TEXT_DOCTYPE, numbers[i:i + DRS_TEXT_BATCH]))
        parsed = {n: _drs_text_payload(faa_drs_ads.pick_document(docs)) for n, docs in groups.items()}
        for attempt, _sections, _problem in parsed.values():
            bump(f"parsed_{attempt['status']}")
        if dry_run or not parsed:
            continue
        id_map = _fetch_ad_ids(supabase, "federal_register", list(parsed))
        kept = _kept_text(supabase, list(id_map.values()))
        if kept is None:
            return counts
        payload = []
        for number, (attempt, sections, problem) in parsed.items():
            ad_id = id_map.get(number)
            if not ad_id:
                bump("not_in_ads")
                continue
            if not _drs_text_needs_send(kept.get(ad_id), attempt["sha256"]):
                bump("text_already_kept")
                continue
            if problem:
                _record_write_error(f"  text: {number} (DRS): secciones fuera de limite ({problem}), "
                                    f"se registra el intento como error")
            payload.append((ad_id, attempt, sections))
        for status, n in _send_text_sections(supabase, payload).items():
            bump(status, n)
    logger.info(f"[DRS] texto{' (DRY-RUN)' if dry_run else ''}: "
                + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
    return counts


def populate_relations(supabase, source: str, normalized: list[dict]) -> None:
    """
    Tras el upsert de `ads`, puebla las tablas relacionales derivadas de cada AD:
      - ad_applicability  (make/model afectados)
      - ad_service_bulletins (SBs referenciados en subject/abstract)
      - superseded_to_ad_id (si el scraper aporto 'Superseded_By')
      - ad_text_documents / ad_text_sections (solo FAA, populate_text_sections)
    Applicability va por la RPC transaccional `replace_ad_applicability` con el
    estado triestado por AD del contrato (docs/applicability-contract.md):
    un AD presente en la salida del scraper con pares validos -> success_with_rows;
    sin pares (ninguna fuente da evidencia de ausencia real), o con una fila
    descartada, o con extraccion
    fallida (`ApplicabilityError`) -> failed (no se toca lo guardado);
    ausente de `normalized` -> tampoco se toca (failed implicito, ni siquiera
    viaja en el payload). SBs por upsert (ad_id, sb_number).
    """
    if not normalized:
        return
    id_map = _fetch_ad_ids(supabase, source, [r["ad_number"] for r in normalized])
    if not id_map:
        return

    # ad_id -> filas de applicability deduplicadas por clave natural.
    applic_by_ad: dict[str, list[dict]] = {}
    applic_seen: dict[str, set] = {}
    sb_rows: list[dict] = []
    sb_seen: set[tuple[str, str]] = set()
    # ADs con >=1 fila de applicability descartada por tamano de campo, o cuya
    # extraccion levanto ApplicabilityError. Viajan `failed` en vez de
    # `success_empty`/`success_with_rows`: lo que sobrevivio es un subconjunto
    # accidental, no la verdad completa del AD.
    applic_dropped: set[str] = set()
    # (ad_id_to_update, target_ad_number) — the row at ad_id is superseded BY target.
    supersede_updates: list[tuple[str, str]] = []

    for r in normalized:
        ad_id = id_map.get(r["ad_number"])
        if not ad_id:
            continue
        applic_by_ad.setdefault(ad_id, [])
        applic_seen.setdefault(ad_id, set())

        raw = r.get("raw_data") or {}

        try:
            pairs = _applicability_pairs(r)
        except ApplicabilityError as e:
            # Extraccion fallida a medias: nunca un subconjunto parcial
            # declarado completo. El AD entero queda `failed`.
            _record_write_error(f"  applicability: {r['ad_number']}: extraccion fallo: {e}")
            applic_dropped.add(ad_id)
            pairs = []

        pt_code = enrich.PRODUCT_TYPE_CODE.get(r.get("product_type") or "")
        # Del mismo texto que decide el tipo; None si no pertenece a pt_code o
        # no hay coincidencia segura (docs/applicability-contract.md). En FAA
        # el abstract marca el titulo como FAA (titular ignorado, respaldo en
        # el abstract), igual que en classify_product_type.
        faa_abstract = (raw.get("Abstract") or "") if source == "federal_register" else None
        pt_subtype = (enrich.product_subtype(pt_code, r.get("subject") or "", faa_abstract)
                      if pt_code else None)

        for mk, mod in pairs:
            # La RPC rechaza el payload entero si un AD repite clave natural.
            key = (mk, mod)
            if key in applic_seen[ad_id]:
                continue
            applic_seen[ad_id].add(key)
            if len(mk) > APPLICABILITY_MAKE_MAX or len(mod or "") > APPLICABILITY_MODEL_MAX:
                # Se descarta la fila, pero NUNCA en silencio: un campo que no
                # cabe es un dato que hay que mirar, y `_record_write_error`
                # fuerza status='error' y salida != 0. Ademas marca el AD para
                # que viaje `failed` mas abajo: un descarte de cliente es una
                # anomalia de extraccion, no evidencia de que el AD no aplica a
                # nada, y declararlo `success_empty` le ordenaria a la RPC BORRAR
                # lo que ya tenia guardado.
                _record_write_error(
                    f"  applicability: {r['ad_number']}: campo fuera de limite, "
                    f"fila descartada (make={len(mk)}/{APPLICABILITY_MAKE_MAX}, "
                    f"model={len(mod or '')}/{APPLICABILITY_MODEL_MAX})"
                )
                applic_dropped.add(ad_id)
                continue
            row_payload = {
                "make": mk,
                "model": mod,
                # make+model identifica una aeronave; solo make es mas debil.
                # Alineado con lib/ads/applicability.ts del frontend.
                "confidence": "high" if mod else "medium",
                # listed si hay texto de modelo; unknown si no. `none` (la
                # fuente declara "todos los modelos") no lo emite ninguna
                # fuente hoy — no hay senal fiable de esa declaracion.
                "model_status": "listed" if mod else "unknown",
            }
            if pt_code:
                row_payload["product_type"] = pt_code
                if pt_subtype:
                    row_payload["product_subtype"] = pt_subtype
            applic_by_ad[ad_id].append(row_payload)

        # Explicit SB list (EASA detail page) wins; else mine subject/abstract.
        explicit_sbs = raw.get("Service_Bulletins")
        sbs = explicit_sbs if isinstance(explicit_sbs, list) else \
            enrich.extract_service_bulletins(raw.get("Subject") or "", raw.get("Abstract") or "")
        for sb in sbs:
            sb = (sb or "").strip()[:120]
            # Postgres rechaza el statement entero con 21000 si una misma
            # sentencia ON CONFLICT propone dos veces la misma clave. Un AD
            # puede citar el mismo SB dos veces, y el corte a 120 chars puede
            # ademas colapsar dos SBs distintos en el mismo valor: se dedupe
            # sobre la forma YA truncada, que es la que ve el constraint.
            if sb and (ad_id, sb) not in sb_seen:
                sb_seen.add((ad_id, sb))
                sb_rows.append({"ad_id": ad_id, "sb_number": sb})

        # "This AD is superseded by X" -> this row's superseded_to = X.
        superseded_by = (raw.get("Superseded_By") or "").strip()
        if superseded_by:
            supersede_updates.append((ad_id, normalize_ad_number(superseded_by)))
        # "This AD supersedes Y[, Z...]" -> each OLDER row's superseded_to = this AD.
        # FAA/EASA/TC pueden declarar varios targets separados por coma.
        supersedes = (raw.get("Supersedes") or "").strip()
        for target in (t.strip() for t in supersedes.split(",")):
            if not target:
                continue
            old_id = id_map.get(normalize_ad_number(target))
            if old_id and old_id != ad_id:
                # mark the older AD superseded by this ad_number (resolved below)
                supersede_updates.append((old_id, r["ad_number"]))

    # ad_applicability: reemplazo transaccional por lote via RPC (INT-3). Cada
    # llamada es una transaccion en Postgres: un proceso muerto entre lotes deja
    # los anteriores completos y los posteriores intactos, nunca un AD a medias.
    def _applic_element(aid: str, rows: list[dict]) -> dict:
        if aid in applic_dropped:
            # `failed` significa "no toques lo guardado": el descarte del
            # cliente no es evidencia de que el AD no aplica a nada, y las
            # filas que SI cupieron no son la verdad completa del AD. La RPC
            # rechaza `rows` no vacio salvo con status=success_with_rows.
            return {"ad_id": aid, "status": "failed", "rows": []}
        if rows:
            return {"ad_id": aid, "status": "success_with_rows", "rows": rows}
        # Sin pares no hay base para pedirle a la RPC que borre lo que ya
        # tenia guardado (ver el comentario sobre `success_empty`).
        return {"ad_id": aid, "status": "failed", "rows": []}

    applic_payload = [_applic_element(aid, rows) for aid, rows in applic_by_ad.items()]
    applic_counts = _send_applicability_batches(supabase, applic_payload)

    # Texto del AD en secciones: solo FAA tiene cache full-text con secciones.
    if source == "federal_register":
        populate_text_sections(supabase, normalized, id_map)

    # ad_service_bulletins: upsert on (ad_id, sb_number)
    for i in range(0, len(sb_rows), 500):
        try:
            supabase.table("ad_service_bulletins").upsert(
                sb_rows[i : i + 500], on_conflict="ad_id,sb_number"
            ).execute()
        except Exception as e:
            _record_write_error(f"  service_bulletins: upsert fallo: {e}")

    # supersession: resolve the superseding AD number -> its ads.id.
    # Must also set status='Superseded' to satisfy ads_superseded_coherence_chk.
    # Batched: one read pass skips rows already pointing at the right target
    # (no nightly dead tuples for a no-op), then one PATCH per distinct target
    # with `.in_(id, ...)` instead of one round-trip per row.
    if supersede_updates:
        target_numbers = [n for _, n in supersede_updates]
        target_ids = _fetch_ad_ids(supabase, source, target_numbers)
        pairs = {  # old ads.id -> superseding ads.id (last declaration wins)
            ad_id: target_ids[target_num]
            for ad_id, target_num in supersede_updates
            if target_ids.get(target_num) and target_ids[target_num] != ad_id
        }
        BATCH = 300
        old_ids = list(pairs)
        for i in range(0, len(old_ids), BATCH):
            chunk = old_ids[i : i + BATCH]
            try:
                resp = (
                    supabase.table("ads")
                    .select("id, superseded_to_ad_id, status")
                    .in_("id", chunk)
                    .execute()
                )
                for rec in resp.data or []:
                    if (rec.get("superseded_to_ad_id") == pairs.get(rec["id"])
                            and rec.get("status") == "Superseded"):
                        pairs.pop(rec["id"], None)
            except Exception as e:
                _record_write_error(f"  supersede: pre-lectura fallo: {e}")
        by_target: dict[str, list[str]] = {}
        for ad_id, tgt in pairs.items():
            by_target.setdefault(tgt, []).append(ad_id)
        for tgt, ids in by_target.items():
            for i in range(0, len(ids), BATCH):
                try:
                    supabase.table("ads").update(
                        {"superseded_to_ad_id": tgt, "status": "Superseded"}
                    ).in_("id", ids[i : i + BATCH]).execute()
                except Exception as e:
                    _record_write_error(f"  supersede: update fallo target {tgt}: {e}")

    # Declarado por el cliente (lo que se mando) vs. leido de la RPC (lo que
    # la BD de verdad aplico, que puede degradar a failed; ver
    # _send_applicability_batches). Se loguean ambos porque difieren cuando
    # eso pasa.
    n_declared_failed = sum(1 for e in applic_payload if e["status"] == "failed")
    logger.info(
        f"[{source}] relaciones: {sum(len(v) for v in applic_by_ad.values())} applicability "
        f"({n_declared_failed} declarados failed; "
        f"BD: {applic_counts.get('success_with_rows', 0)} success_with_rows, "
        f"{applic_counts.get('success_empty', 0)} success_empty, "
        f"{applic_counts.get('failed', 0)} failed), "
        f"{len(sb_rows)} service bulletins, {len(supersede_updates)} supersession"
    )


# ---------------------------------------------------------------------------
# scrape_runs helpers
# ---------------------------------------------------------------------------
def save_scrape_run(supabase, source: str, ads_found: int, ads_new: int,
                    ads_updated: int, error_msg: str | None, started_at: datetime) -> None:
    try:
        supabase.table("scrape_runs").insert({
            "source": source,
            "status": "error" if error_msg else "completed",
            "ads_found": ads_found,
            "ads_new": ads_new,
            "ads_updated": ads_updated,
            "error_msg": error_msg,
            "started_at": started_at.isoformat(),
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
    except Exception as e:
        _record_write_error(f"No se pudo guardar scrape_run para {source}: {e}")


# La fuente que se esta scrapeando ahora mismo, o None entre fuentes.
# {"supabase": client, "source": str, "started_at": datetime}
_IN_FLIGHT: dict | None = None
_ABORTING = False


def _on_abort(signum, _frame) -> None:
    """Deja rastro en `scrape_runs` cuando el proceso muere por timeout.

    El workflow acota el proceso con `timeout(1)` unos minutos por debajo del
    presupuesto del job, para que un desborde salga con 124 (failure, que
    notifica) en vez de cancelled (que no). Eso arregla la senal del lado de
    GitHub, pero no la del lado de la base: la escritura de `scrape_runs` vive
    al final del bucle, asi que un SIGINT a mitad del scrape mataba el proceso
    sin dejar fila. El panel de salud volvia a mostrar una *ausencia* — que es
    exactamente el disfraz bajo el que EASA paso 19 dias sin actualizarse.

    Aqui se escribe la fila de error y se sale con 124. Es I/O dentro de un
    handler, deliberadamente: es un solo INSERT y el `--kill-after=60s` del
    workflow deja margen de sobra. Una segunda senal sale en seco.
    """
    global _ABORTING
    if _ABORTING:
        os._exit(124)
    _ABORTING = True

    state = _IN_FLIGHT
    name = signal.Signals(signum).name
    if state is not None:
        logger.error(
            f"[{state['source']}] {name}: proceso abortado (probable timeout). "
            f"Registrando el fallo en scrape_runs antes de salir."
        )
        save_scrape_run(
            state["supabase"],
            source=state["source"],
            ads_found=0,
            ads_new=0,
            ads_updated=0,
            error_msg=(
                f"Abortado por {name} antes de terminar (probable timeout del "
                f"workflow). La corrida no llego a persistir resultados."
            ),
            started_at=state["started_at"],
        )
    else:
        logger.error(f"{name}: proceso abortado fuera de una fuente activa.")
    os._exit(124)


def install_abort_handler() -> None:
    """SIGINT/SIGTERM -> fila de error en scrape_runs, luego exit 124."""
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _on_abort)


# ---------------------------------------------------------------------------
# Runners por fuente
# ---------------------------------------------------------------------------
def _merge_faa_results(fr_rows: list[dict], govinfo_rows: list[dict]) -> list[dict]:
    """
    Fusiona resultados FR API + GovInfo a nivel de campo.

    Join key: el FR document_number, que FR API guarda en "FR_DocNumber" (y,
    como respaldo para filas viejas, en "Docket") y GovInfo expone como
    "GranuleId" (son el mismo identificador del Federal Register).

    IMPORTANTE: GovInfo ya NO aporta el AD_Number. La identidad sale de
    `docket_ids` de la FR API (utils.ad_identity_from_docket_ids), que es el
    header regulatorio estructurado. El AD_Number de GovInfo salia del primer
    "AD nnnn-nn-nn" del MODS XML, que suele ser el AD superseded: pisar la
    identidad con eso es lo que dejo miles de filas con el numero de otro AD.
    GovInfo solo completa Docket y Amendment cuando faltan.

    Filas sin match pasan tal cual; el dedupe last-wins de upsert_ads()
    queda como red de seguridad.
    """
    fr_by_docnum: dict[str, dict] = {}
    for row in fr_rows:
        # FR_DocNumber es el join key estable; Docket puede ser ya el docket
        # real FAA-xxxx-xxxx (nuevo comportamiento del scraper FR).
        doc_num = (row.get("FR_DocNumber") or row.get("Docket") or "").strip()
        if doc_num:
            fr_by_docnum[doc_num] = row

    merged_count = 0
    extra_govinfo: list[dict] = []
    for grow in govinfo_rows:
        granule_id = (grow.get("GranuleId") or "").strip()
        fr_row = fr_by_docnum.get(granule_id)
        if fr_row is not None:
            if grow.get("Docket") and not grow["Docket"].startswith("FR-") \
                    and not (fr_row.get("Docket") or "").upper().startswith("FAA-"):
                fr_row["Docket"] = grow["Docket"]
            if grow.get("Amendment") and not fr_row.get("Amendment"):
                fr_row["Amendment"] = grow["Amendment"]
            merged_count += 1
        else:
            extra_govinfo.append(grow)

    logger.info(
        f"  Merge FAA: {merged_count} filas FR completadas con MODS, "
        f"{len(extra_govinfo)} solo-GovInfo, {len(fr_rows) - merged_count} solo-FR"
    )
    return fr_rows + extra_govinfo


def run_federal_register(since: str | None = None, with_govinfo: bool = False) -> list[dict]:
    """
    Source 'federal_register' consolida upstreams FAA bajo el mismo source key:
      1. FR API (year-split)      -> 1994+ via federalregister.gov (contenido
         E identidad: `docket_ids` trae numero AD, docket y amendment)
      2. GovInfo API (MODS XML)   -> opcional (--with-govinfo). Ya no aporta
         identidad; solo completa Docket/Amendment faltantes.

    since (YYYY-MM-DD): acota FR API y GovInfo a publicaciones desde esa fecha
    (runs incrementales).
    """
    logger.info("Scraping federal_register (FR API%s)..." %
                (" + GovInfo MODS" if with_govinfo else ""))
    all_results: list[dict] = []

    # Fase 1: FR API year-split (1994 -> current)
    logger.info("  [1] Federal Register API (year-split, identidad via docket_ids)...")
    if since:
        from scrapers.federal_register import search_ads_by_year_range
        # Year-split desde el anio de --since; el filtro fino por fecha lo da
        # el propio year range (FR API filtra por publication_date por anio).
        fr_results = search_ads_by_year_range(int(since[:4]), datetime.now().year)
    else:
        fr_results = fr_search()
    logger.info(f"  FR API: {len(fr_results)} ADs")

    # Fase 2 (opcional): GovInfo MODS. Solo completa Docket/Amendment; la
    # identidad ya viene de docket_ids y NO debe pisarse con el MODS XML.
    gov_results: list[dict] = []
    if not with_govinfo:
        logger.info("  [2] GovInfo MODS: omitido (--with-govinfo para habilitar)")
        all_results.extend(_merge_faa_results(fr_results, gov_results))
        logger.info(f"federal_register: {len(all_results)} ADs encontrados (pre-dedupe)")
        return all_results

    logger.info("  [2] GovInfo API (MODS XML: completa docket + amendment)...")
    try:
        # Mismo span que FR API (desde el 1 de enero del anio de --since) para
        # que el merge cubra los mismos documentos; el cache MODS hace baratos
        # los re-fetches dentro del anio.
        gov_from = f"{int(since[:4])}-01-01" if since else None
        gov_results = (
            govinfo_search(date_from=gov_from) if gov_from
            else govinfo_search(year_from=1994)
        )
        logger.info(f"  GovInfo: {len(gov_results)} ADs")
    except Exception as e:
        logger.warning(f"  GovInfo fallo (continua con FR API solo): {e}")
        gov_results = []

    all_results.extend(_merge_faa_results(fr_results, gov_results))
    logger.info(f"federal_register: {len(all_results)} ADs encontrados (pre-dedupe)")
    return all_results


#: Paginas seguidas sin un solo AD desconocido antes de dar el recorrido por
#: terminado. A 20 ADs por pagina son 100 directivas de margen por encima de la
#: ultima novedad — EASA publica del orden de 5 al dia.
EASA_QUIET_PAGES = 5


def _easa_known_ad_numbers(supabase) -> set[str]:
    """Los ad_number de EASA que ya estan en la BD, paginados de a 1000."""
    known: set[str] = set()
    page = 0
    while True:
        resp = (
            supabase.table("ads")
            .select("ad_number")
            .eq("source", "easa")
            .range(page * 1000, page * 1000 + 999)
            .execute()
        )
        rows = resp.data or []
        known.update((r.get("ad_number") or "").strip() for r in rows if r.get("ad_number"))
        if len(rows) < 1000:
            break
        page += 1
    return known


def run_easa() -> list[dict]:
    """
    Recorrido incremental por defecto; completo los domingos o con EASA_FULL=1.

    El recorrido completo son 910 paginas a ~1,25 s cada una: 19 minutos de
    runner cada dia para encontrar las cinco directivas nuevas que EASA publica.
    La lista viene ordenada de la mas nueva a la mas vieja, asi que basta bajar
    hasta que deje de aparecer algo que no tengamos, con EASA_QUIET_PAGES de
    margen.

    El completo sigue corriendo una vez por semana y no es ceremonia: una
    revision puede reaparecer mas abajo de lo que el margen cubre, y un
    recorrido incremental que nunca se contrasta con el corpus entero es una
    forma lenta de quedarse sin darse cuenta.

    Si no hay BD que consultar (dry-run, primer arranque), el recorrido es
    completo: no saber que tenemos no es razon para asumir que lo tenemos todo.
    """
    full = os.environ.get("EASA_FULL", "").strip() == "1"
    known: set[str] = set()
    if not full:
        try:
            known = _easa_known_ad_numbers(get_supabase_client())
            logger.info(f"easa: {len(known)} ADs ya en BD; recorrido incremental")
        except Exception as e:
            logger.info(f"easa: no se pudo leer el corpus ({e}); recorrido completo")
            full = True

    stop_when = None
    if not full and known:
        quiet = {"pages": 0}

        def stop_when(rows: list[dict]) -> bool:
            fresh = [r for r in rows if (r.get("AD_Number") or "").strip() not in known]
            if fresh:
                quiet["pages"] = 0
                return False
            quiet["pages"] += 1
            return quiet["pages"] >= EASA_QUIET_PAGES

    logger.info(f"Scraping easa (search, {'completo' if full else 'incremental'})...")
    results = easa_search(max_pages=910, stop_when=stop_when)
    if not results:
        logger.info("easa search devolvio 0 resultados, usando fallback main page...")
        results = easa_main(max_pages=10)
    logger.info(f"easa: {len(results)} ADs encontrados")
    # Enriquecer cada AD con su pagina de detalle (issue date, AMOC, SBs, supersesion).
    # Cacheado: re-runs no re-bajan. Pesado en el primer full (~18k fetches).
    logger.info("easa: enriqueciendo con paginas de detalle...")
    results = easa_enrich_details(results)
    return results


def run_anac_brazil() -> list[dict]:
    logger.info("Scraping anac_brazil...")
    all_results = []
    for yr in ["2021-2025", "2016-2020", "2011-2015", "2006-2010", "2001-2005", "1996-2000"]:
        logger.info(f"  anac_brazil: rango {yr}...")
        batch = anac_brazil_search(year_range=yr)
        logger.info(f"  anac_brazil [{yr}]: {len(batch)} ADs")
        all_results.extend(batch)
    logger.info(f"anac_brazil: {len(all_results)} ADs encontrados (total)")
    return all_results


def run_transport_canada() -> list[dict]:
    logger.info("Scraping transport_canada...")
    all_results = []
    for manufacturer in ["Boeing", "Airbus", "Bombardier", "Embraer"]:
        logger.info(f"  transport_canada: fabricante {manufacturer}...")
        batch = tc_search(manufacturer_name=manufacturer)
        logger.info(f"  transport_canada [{manufacturer}]: {len(batch)} ADs")
        all_results.extend(batch)
    logger.info(f"transport_canada: {len(all_results)} ADs encontrados (total)")
    logger.info("transport_canada: enriqueciendo con PDFs...")
    all_results = tc_enrich_details(all_results)
    return all_results


def run_dgac_chile() -> list[dict]:
    logger.info("Scraping dgac_chile...")
    results = dgac_chile_search(max_results=500)
    logger.info(f"dgac_chile: {len(results)} ADs encontrados")
    return results


def run_anac_argentina() -> list[dict]:
    logger.info("Scraping anac_argentina...")
    results = anac_argentina_search(max_results=500)
    logger.info(f"anac_argentina: {len(results)} ADs encontrados")
    return results


def run_aerocivil_colombia() -> list[dict]:
    logger.info("Scraping aerocivil_colombia...")
    results = aerocivil_search(max_results=200)
    logger.info(f"aerocivil_colombia: {len(results)} ADs encontrados")
    return results


def run_casa_australia() -> list[dict]:
    logger.info("Scraping casa_australia...")
    results = casa_search(max_results=200)
    logger.info(f"casa_australia: {len(results)} ADs encontrados")
    return results


def run_gcaa_uae() -> list[dict]:
    logger.info("Scraping gcaa_uae...")
    results = gcaa_search(max_results=200)
    logger.info(f"gcaa_uae: {len(results)} ADs encontrados")
    return results


def run_uk_caa() -> list[dict]:
    logger.info("Scraping uk_caa...")
    # El corpus completo del UK CAA son ~200 documentos entre las cuatro series,
    # asi que se pide holgado y no hace falta paginar.
    results = uk_caa_search(max_results=1000)
    logger.info(f"uk_caa: {len(results)} ADs encontrados")
    return results


RUNNERS = {
    "federal_register": run_federal_register,
    "easa": run_easa,
    "anac_brazil": run_anac_brazil,
    "transport_canada": run_transport_canada,
    "dgac_chile": run_dgac_chile,
    "anac_argentina": run_anac_argentina,
    "aerocivil_colombia": run_aerocivil_colombia,
    "casa_australia": run_casa_australia,
    "gcaa_uae": run_gcaa_uae,
    "uk_caa": run_uk_caa,
}


# ---------------------------------------------------------------------------
# FAA DRS: ADs anteriores a 1994 hacia `ads` (source federal_register)
# ---------------------------------------------------------------------------
#: Solo los AD finales: los de emergencia (ADFREAD) empiezan en 1997.
DRS_BACKFILL_DOCTYPE = "ADFRAWD"


def _ads_not_created_from_drs(supabase, ad_numbers: list[str]) -> set[str] | None:
    """Numeros de `ad_numbers` que ya tienen fila `federal_register` creada
    por otro camino (el scraper del Federal Register): esas filas no se tocan.
    None si la lectura fallo (registrado): sin saberlo no se escribe nada."""
    taken: set[str] = set()
    BATCH = 300
    uniq = sorted(set(ad_numbers))
    for i in range(0, len(uniq), BATCH):
        try:
            resp = (supabase.table("ads")
                    .select("ad_number, origin:raw_data->>Origin")
                    .eq("source", "federal_register")
                    .in_("ad_number", uniq[i:i + BATCH])
                    .execute())
        except Exception as e:
            _record_write_error(f"[DRS] ADs anteriores a 1994: no se pudo leer ads: {e}")
            return None
        for rec in resp.data or []:
            if rec.get("origin") != faa_drs_ads.ORIGIN:
                taken.add(rec["ad_number"])
    return taken


def backfill_pre_federal_register(supabase, store, ad_numbers, *, dry_run: bool = False) -> dict[str, int]:
    """Filas de `ads` (source `federal_register`) para los ADs anteriores a
    la FR API entre `ad_numbers`, desde sus documentos en `drs_documents`
    (scrapers/faa_drs_ads.py). Mismo camino que cualquier fila FAA:
    `normalize` -> `upsert_ads` -> `populate_relations`.

    Se leen todos los documentos guardados de cada AD, no solo los de la
    corrida: en un incremental puede llegar solo uno de sus documentos. Una
    fila `federal_register` que no creo este camino nunca se reescribe."""
    counts = {"candidates": 0, "rows": 0, "skipped_existing": 0, "new": 0, "updated": 0}
    candidates = sorted(n for n in set(ad_numbers) if faa_drs_ads.may_predate_federal_register(n))
    counts["candidates"] = len(candidates)
    if not candidates:
        return counts
    documents = store.documents(DRS_BACKFILL_DOCTYPE, candidates)
    rows = [n for n in (normalize(r, "federal_register")
                        for r in faa_drs_ads.pre_federal_register_rows(documents)) if n]
    counts["rows"] = len(rows)
    if rows and not dry_run:
        taken = _ads_not_created_from_drs(supabase, [r["ad_number"] for r in rows])
        if taken is None:
            return counts
        rows = [r for r in rows if r["ad_number"] not in taken]
        counts["skipped_existing"] = len(taken)
        if rows:
            counts["new"], counts["updated"] = upsert_ads(supabase, rows)
            populate_relations(supabase, "federal_register", rows)
    logger.info(
        f"[DRS] ADs anteriores a 1994{' (DRY-RUN)' if dry_run else ''}: {counts['candidates']} candidatos "
        f"por numero, {counts['rows']} filas, {counts['skipped_existing']} ya del Federal Register, "
        f"{counts['new']} nuevas, {counts['updated']} actualizadas"
    )
    return counts


# ---------------------------------------------------------------------------
# FAA DRS: sincronizacion de drs_documents
# ---------------------------------------------------------------------------
def run_drs_sync(doctypes: list[str], dry_run: bool = False, env: dict | None = None,
                 client=None, store=None, supabase=None) -> int:
    """Sincroniza DRS y devuelve el exit code (0 ok, 1 con errores).

    Alimenta `drs_documents` y el vinculo `ads.drs_document_guid`
    (scrapers/faa_drs_sync.py) y, entre los dos, crea en `ads` los ADs
    anteriores a 1994 que solo estan en DRS (`backfill_pre_federal_register`).
    No escribe `scrape_runs`. Una corrida con cualquier error (pagina
    fallida, documento descartado, escritura o enlace fallido) sale != 0 para
    que el job quede en rojo; el cursor solo avanza tras recorrer todas las
    paginas de un doctype.

    Con DRS_API_ENABLED apagado no hace nada y sale 0: el job queda verde y
    el log dice por que no corrio.
    """
    if client is None:
        if not faa_drs_api.is_enabled(env):
            logger.info("[DRS] DRS_API_ENABLED apagado: la sincronizacion DRS no corre")
            return 0
        try:
            client = faa_drs_api.DrsClient.from_env(env)
        except faa_drs_api.DrsError as e:
            logger.error(f"[DRS] {e}")
            return 1
    if store is None:
        if dry_run:
            store = faa_drs_sync.DryRunStore()
        else:
            supabase = supabase or get_supabase_client()
            store = faa_drs_sync.SupabaseDrsStore(supabase)

    write_errors_before = len(_write_errors)
    backfill: dict[str, int] = {}

    text: dict[str, int] = {}

    def _after_upsert(report: faa_drs_sync.SyncReport) -> None:
        # Primero las filas de los ADs anteriores a 1994, para que su texto
        # tenga a que AD ir; el texto corre aunque esas filas fallen.
        numbers = report.ad_numbers(DRS_BACKFILL_DOCTYPE)
        try:
            backfill.update(backfill_pre_federal_register(supabase, store, numbers, dry_run=dry_run))
        finally:
            text.update(populate_drs_text_sections(supabase, store, numbers, dry_run=dry_run))

    # Un llamador que inyecta solo el store (sin base) sincroniza drs_documents
    # y nada mas; la corrida real siempre tiene cliente.
    after = _after_upsert if dry_run or supabase is not None else None
    if after is None:
        logger.info("[DRS] sin cliente de base: no se crean ADs anteriores a 1994 ni se envia texto")
    report = faa_drs_sync.sync(client, store, doctypes, before_link=after)
    errors = report.errors
    for err in errors[:50]:
        _record_write_error(f"[DRS] {err}")
    if len(errors) > 50:
        _record_write_error(f"[DRS] ... y {len(errors) - 50} errores mas")
    lines = [f"Resumen DRS{' (DRY-RUN)' if dry_run else ''}"]
    lines += [
        f"  {r.doctype:8}: {r.mode or '-'}, {r.pages} paginas, {r.documents} documentos, "
        f"cursor -> {r.advanced_to}{' ERROR' if r.error else ''}"
        for r in report.results
    ]
    if backfill:
        lines.append(f"  Pre-1994      : {backfill['rows']} filas, {backfill['new']} nuevas, "
                     f"{backfill['skipped_existing']} ya del Federal Register")
    if text:
        lines.append("  Texto DRS     : " + ", ".join(f"{k}={v}" for k, v in sorted(text.items())))
    # Los errores de la sync ya estan en _write_errors (arriba); ademas cuentan
    # las escrituras fallidas de las filas de `ads` y sus relaciones.
    failed = len(_write_errors) - write_errors_before
    lines += [f"  ADs enlazados : {report.linked}", f"  Errores       : {failed}"]
    bar = "=" * 50
    logger.info("\n".join(["", bar, *lines, bar]))
    return 1 if errors or failed else 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(
        description="Zephr â€” poblar Supabase con ADs de todas las fuentes"
    )
    parser.add_argument(
        "--source",
        choices=ALL_SOURCES,
        default=None,
        help="Fuente a ejecutar. Sin este flag corre DAILY_SOURCES (todas "
             "menos las deshabilitadas: " + ", ".join(sorted(DISABLED_SOURCES)) + ")",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scrape pero no guarda en Supabase",
    )
    parser.add_argument(
        "--reset",
        action="store_true",
        help="DESTRUCTIVO: borra todas las filas de la(s) fuente(s) antes de repoblar",
    )
    parser.add_argument(
        "--since",
        default=None,
        metavar="YYYY-MM-DD",
        help="Solo federal_register: scrape incremental desde esta fecha de publicacion",
    )
    parser.add_argument(
        "--with-govinfo",
        action="store_true",
        help="Solo federal_register: corre tambien la fase GovInfo MODS "
             "(lenta; solo completa docket/amendment, no la identidad)",
    )
    parser.add_argument(
        "--drs",
        action="store_true",
        help="Sincroniza FAA DRS (ADFRAWD + ADFREAD) hacia drs_documents en vez de "
             "scrapear fuentes. Requiere DRS_API_ENABLED y DRS_API_KEY",
    )
    parser.add_argument(
        "--drs-doctype",
        choices=faa_drs_api.DOCTYPES,
        default=None,
        help="Solo con --drs: sincroniza un solo doctype (ADFREAD es una pagina)",
    )
    args = parser.parse_args()

    if args.drs_doctype and not args.drs:
        parser.error("--drs-doctype requiere --drs")
    if args.drs:
        clashing = [flag for flag, on in (("--source", args.source), ("--reset", args.reset),
                                          ("--since", args.since), ("--with-govinfo", args.with_govinfo)) if on]
        if clashing:
            parser.error(f"--drs no se combina con {', '.join(clashing)}")
        doctypes = [args.drs_doctype] if args.drs_doctype else list(faa_drs_api.DOCTYPES)
        sys.exit(run_drs_sync(doctypes, dry_run=args.dry_run))

    if args.since and not re.match(r"^\d{4}-\d{2}-\d{2}$", args.since):
        parser.error("--since debe tener formato YYYY-MM-DD")

    if args.source:
        sources_to_run = [args.source]
    else:
        sources_to_run = DAILY_SOURCES
        logger.info(
            f"Fuentes deshabilitadas del run diario (ver DISABLED_SOURCES): "
            f"{', '.join(sorted(DISABLED_SOURCES))}"
        )

    supabase = None
    if not args.dry_run:
        supabase = get_supabase_client()
        logger.info("Supabase conectado.")
        # Solo con cliente: sin el no hay donde registrar el aborto, y en
        # --dry-run el Ctrl-C debe seguir siendo un Ctrl-C normal.
        install_abort_handler()

    total_new = 0
    total_updated = 0
    total_errors = 0

    global _IN_FLIGHT

    for source in sources_to_run:
        started_at = datetime.now(timezone.utc)
        error_msg = None
        raw_results = []
        write_errors_before = len(_write_errors)
        if supabase is not None:
            _IN_FLIGHT = {"supabase": supabase, "source": source, "started_at": started_at}

        if args.reset and supabase and not args.dry_run:
            logger.warning(f"[{source}] --reset: borrando filas existentes...")
            try:
                # Supabase requiere un filtro; usamos source_eq garantizado
                deleted = supabase.table("ads").delete().eq("source", source).execute()
                logger.info(f"[{source}] Borradas {len(deleted.data or [])} filas")
            except Exception as e:
                # Un reset fallido significa que se upserteara sobre datos que se
                # creian borrados: la corrida no puede reportarse limpia.
                error_msg = f"--reset fallo: {e}"
                logger.error(f"[{source}] Error en --reset: {e}")
                total_errors += 1

        try:
            if source == "federal_register":
                raw_results = run_federal_register(
                    since=args.since, with_govinfo=args.with_govinfo
                )
            else:
                raw_results = RUNNERS[source]()
        except Exception:
            error_msg = traceback.format_exc()
            logger.error(f"[{source}] ERROR durante scraping:\n{error_msg}")
            total_errors += 1

        # Normalizar
        normalized = []
        for row in raw_results:
            norm = normalize(row, source)
            if norm is not None:
                normalized.append(norm)

        skipped = len(raw_results) - len(normalized)
        if skipped:
            logger.info(f"[{source}] {skipped} filas omitidas (ad_number vacio)")

        ads_new = ads_updated = 0

        if args.dry_run:
            logger.info(
                f"[{source}] DRY-RUN: {len(normalized)} ADs listos para guardar (no se guarda nada)"
            )
        elif normalized:
            try:
                ads_new, ads_updated = upsert_ads(supabase, normalized)
                logger.info(
                    f"[{source}] Guardados: {ads_new} nuevos, {ads_updated} actualizados"
                )
                try:
                    populate_relations(supabase, source, normalized)
                except Exception as rel_err:
                    _record_write_error(f"[{source}] relaciones fallaron (ads OK): {rel_err}")
            except Exception:
                error_msg = traceback.format_exc()
                logger.error(f"[{source}] ERROR durante upsert:\n{error_msg}")
                total_errors += 1

        # Escrituras parciales fallidas (applicability, SBs, supersession, o
        # pre-checks que falsean los contadores) degradan la corrida entera:
        # scrape_runs.status='error' y exit code != 0.
        source_write_errors = _write_errors[write_errors_before:]
        if source_write_errors and not error_msg:
            error_msg = (
                f"{len(source_write_errors)} escrituras fallidas: "
                + " | ".join(source_write_errors[:5])
            )[:2000]
            total_errors += 1

        total_new += ads_new
        total_updated += ads_updated

        if not args.dry_run and supabase:
            save_scrape_run(
                supabase,
                source=source,
                ads_found=len(raw_results),
                ads_new=ads_new,
                ads_updated=ads_updated,
                error_msg=error_msg,
                started_at=started_at,
            )

        # La fila ya esta escrita: un aborto a partir de aqui no debe duplicarla.
        _IN_FLIGHT = None

    logger.info(
        f"\n{'='*50}\n"
        f"  Resumen final\n"
        f"  Fuentes procesadas : {len(sources_to_run)}\n"
        f"  ADs nuevos         : {total_new}\n"
        f"  ADs actualizados   : {total_updated}\n"
        f"  Fuentes con error  : {total_errors}\n"
        f"  Escrituras fallidas: {len(_write_errors)}\n"
        f"{'='*50}"
    )

    if total_errors or _write_errors:
        sys.exit(1)


if __name__ == "__main__":
    main()

