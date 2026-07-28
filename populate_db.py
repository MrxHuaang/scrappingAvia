"""
Zephr - populate_db.py
Ejecuta todos los scrapers y persiste los resultados en Supabase.

Uso:
    python populate_db.py                        # todas las fuentes
    python populate_db.py --source easa          # fuente individual
    python populate_db.py --dry-run              # scrape pero no guarda
"""

import argparse
import json
import logging
import os
import re
import sys
import traceback
from datetime import datetime, timezone

from dotenv import load_dotenv
from supabase import create_client

from utils import normalize_ad_number
import enrich

# Agregar directorio raiz al path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scrapers.federal_register import search_ads as fr_search
from scrapers.faa_govinfo import search_ads as govinfo_search
from scrapers.faa_drs import search_ads as drs_search
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
    state_of_design, status/priority/product_type canonicos, y best-effort de
    unsafe_condition / corrective_action / compliance_time / amoc desde el texto
    disponible (Subject + Abstract si el scraper lo aporta).
    """
    ad_number = normalize_ad_number(row.get("AD_Number") or "")
    if not ad_number:
        return None

    subject = (row.get("Subject") or "").strip() or None
    abstract = (row.get("Abstract") or "").strip()
    raw_status = (row.get("Status") or "").strip() or None
    text_blob = " ".join(t for t in (subject or "", abstract, row.get("Type") or "") if t)

    # Explicit scraper-provided values win; otherwise derive from available text.
    unsafe = (row.get("Unsafe_Condition") or "").strip() or enrich.extract_unsafe_condition(abstract)
    corrective = (row.get("Corrective_Action") or "").strip() or enrich.extract_corrective_action(abstract)
    compliance_time = (row.get("Compliance_Time") or "").strip() or enrich.extract_compliance_time(abstract, subject or "")
    amoc = bool(row.get("AMOC_Available")) or enrich.detect_amoc(abstract, subject or "")

    return {
        "source": source_key,
        "source_label": SOURCE_LABELS.get(source_key, source_key),
        "ad_number": ad_number,
        "state_of_design": enrich.state_of_design(source_key),
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

    # Deduplicar en memoria por (source, ad_number) â€” Ãºltimo gana
    # Necesario para TC que retorna el mismo AD bajo mÃºltiples modelos
    deduped: dict[tuple, dict] = {}
    for r in rows:
        key = (r["source"], r["ad_number"])
        deduped[key] = r
    rows = list(deduped.values())
    logger.info(f"Filas tras dedup: {len(rows)}")

    # Verificar cuales ya existen para contar new vs updated
    source_vals = list({r["source"] for r in rows})

    try:
        existing_resp = (
            supabase.table("ads")
            .select("source, ad_number")
            .in_("source", source_vals)
            .execute()
        )
        existing = {
            (rec["source"], rec["ad_number"])
            for rec in (existing_resp.data or [])
        }
    except Exception as e:
        logger.warning(f"No se pudo pre-verificar existentes: {e}")
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
        logger.warning(f"No se pudo pre-verificar superseded links: {e}")
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
            logger.warning(f"  relations: no se pudieron leer ids ({source}): {e}")
    return ids


def populate_relations(supabase, source: str, normalized: list[dict]) -> None:
    """
    Tras el upsert de `ads`, puebla las tablas relacionales derivadas de cada AD:
      - ad_applicability  (make/model afectados)
      - ad_service_bulletins (SBs referenciados en subject/abstract)
      - superseded_to_ad_id (si el scraper aporto 'Superseded_By')
    Idempotente: borra applicability previa de estos ad_ids y reinserta;
    SBs por upsert (ad_id, sb_number).
    """
    if not normalized:
        return
    id_map = _fetch_ad_ids(supabase, source, [r["ad_number"] for r in normalized])
    if not id_map:
        return

    applic_rows: list[dict] = []
    sb_rows: list[dict] = []
    # (ad_id_to_update, target_ad_number) — the row at ad_id is superseded BY target.
    supersede_updates: list[tuple[str, str]] = []
    touched_ids: list[str] = []

    for r in normalized:
        ad_id = id_map.get(r["ad_number"])
        if not ad_id:
            continue
        touched_ids.append(ad_id)

        make = (r.get("make") or "").strip()
        model = (r.get("model") or "").strip()
        if make:
            # EASA packs several models in "A, B, C"; one applicability row each.
            models = [m.strip() for m in (model.split(",") if model else [""]) if True]
            for mod in (models or [""]):
                applic_rows.append({
                    "ad_id": ad_id,
                    "make": make,
                    "model": mod or None,
                    "confidence": "high" if make and mod else "medium",
                })

        raw = r.get("raw_data") or {}
        # Explicit SB list (EASA detail page) wins; else mine subject/abstract.
        explicit_sbs = raw.get("Service_Bulletins")
        sbs = explicit_sbs if isinstance(explicit_sbs, list) else \
            enrich.extract_service_bulletins(raw.get("Subject") or "", raw.get("Abstract") or "")
        for sb in sbs:
            sb = (sb or "").strip()
            if sb:
                sb_rows.append({"ad_id": ad_id, "sb_number": sb[:120]})

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

    # ad_applicability: delete-then-insert for touched ads (scraper-owned table)
    BATCH = 300
    for i in range(0, len(touched_ids), BATCH):
        chunk = touched_ids[i : i + BATCH]
        try:
            supabase.table("ad_applicability").delete().in_("ad_id", chunk).execute()
        except Exception as e:
            logger.warning(f"  applicability: delete fallo: {e}")
    for i in range(0, len(applic_rows), 500):
        try:
            supabase.table("ad_applicability").insert(applic_rows[i : i + 500]).execute()
        except Exception as e:
            logger.warning(f"  applicability: insert fallo: {e}")

    # ad_service_bulletins: upsert on (ad_id, sb_number)
    for i in range(0, len(sb_rows), 500):
        try:
            supabase.table("ad_service_bulletins").upsert(
                sb_rows[i : i + 500], on_conflict="ad_id,sb_number"
            ).execute()
        except Exception as e:
            logger.warning(f"  service_bulletins: upsert fallo: {e}")

    # supersession: resolve the superseding AD number -> its ads.id.
    # Must also set status='Superseded' to satisfy ads_superseded_coherence_chk.
    if supersede_updates:
        target_numbers = [n for _, n in supersede_updates]
        target_ids = _fetch_ad_ids(supabase, source, target_numbers)
        for ad_id, target_num in supersede_updates:
            tgt = target_ids.get(target_num)
            if tgt and tgt != ad_id:
                try:
                    supabase.table("ads").update(
                        {"superseded_to_ad_id": tgt, "status": "Superseded"}
                    ).eq("id", ad_id).execute()
                except Exception as e:
                    logger.warning(f"  supersede: update fallo {ad_id}: {e}")

    logger.info(
        f"[{source}] relaciones: {len(applic_rows)} applicability, "
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
        logger.warning(f"No se pudo guardar scrape_run para {source}: {e}")


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
      1. DRS (drs.faa.gov)        -> pre-1995 historicos (best-effort)
      2. FR API (year-split)      -> 1994+ via federalregister.gov (contenido
         E identidad: `docket_ids` trae numero AD, docket y amendment)
      3. GovInfo API (MODS XML)   -> opcional (--with-govinfo). Ya no aporta
         identidad; solo completa Docket/Amendment faltantes.

    since (YYYY-MM-DD): acota FR API y GovInfo a publicaciones desde esa fecha
    (runs incrementales); DRS no aplica (historicos pre-1995).
    """
    logger.info("Scraping federal_register (DRS + FR API%s)..." %
                (" + GovInfo MODS" if with_govinfo else ""))
    all_results: list[dict] = []

    # Fase 1: DRS pre-1995 (best-effort, no falla si Akamai bloquea)
    logger.info("  [1] DRS (drs.faa.gov, pre-1995 historicos)...")
    try:
        drs_results = drs_search(pre_govinfo_only=True)
        logger.info(f"  DRS: {len(drs_results)} ADs")
        all_results.extend(drs_results)
    except Exception as e:
        logger.warning(f"  DRS fallo (continua pipeline): {e}")

    # Fase 2: FR API year-split (1994 -> current)
    logger.info("  [2] Federal Register API (year-split, identidad via docket_ids)...")
    if since:
        from scrapers.federal_register import search_ads_by_year_range
        # Year-split desde el anio de --since; el filtro fino por fecha lo da
        # el propio year range (FR API filtra por publication_date por anio).
        fr_results = search_ads_by_year_range(int(since[:4]), datetime.now().year)
    else:
        fr_results = fr_search()
    logger.info(f"  FR API: {len(fr_results)} ADs")

    # Fase 3 (opcional): GovInfo MODS. Solo completa Docket/Amendment; la
    # identidad ya viene de docket_ids y NO debe pisarse con el MODS XML.
    gov_results: list[dict] = []
    if not with_govinfo:
        logger.info("  [3] GovInfo MODS: omitido (--with-govinfo para habilitar)")
        all_results.extend(_merge_faa_results(fr_results, gov_results))
        logger.info(f"federal_register: {len(all_results)} ADs encontrados (pre-dedupe)")
        return all_results

    logger.info("  [3] GovInfo API (MODS XML: completa docket + amendment)...")
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


def run_easa() -> list[dict]:
    logger.info("Scraping easa (search)...")
    results = easa_search(max_pages=910)  # FASE 2 produccion completa
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
        help="Fuente a ejecutar (por defecto: todas)",
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
    args = parser.parse_args()

    if args.since and not re.match(r"^\d{4}-\d{2}-\d{2}$", args.since):
        parser.error("--since debe tener formato YYYY-MM-DD")

    sources_to_run = [args.source] if args.source else ALL_SOURCES

    supabase = None
    if not args.dry_run:
        supabase = get_supabase_client()
        logger.info("Supabase conectado.")

    total_new = 0
    total_updated = 0
    total_errors = 0

    for source in sources_to_run:
        started_at = datetime.now(timezone.utc)
        error_msg = None
        raw_results = []

        if args.reset and supabase and not args.dry_run:
            logger.warning(f"[{source}] --reset: borrando filas existentes...")
            try:
                # Supabase requiere un filtro; usamos source_eq garantizado
                deleted = supabase.table("ads").delete().eq("source", source).execute()
                logger.info(f"[{source}] Borradas {len(deleted.data or [])} filas")
            except Exception as e:
                logger.error(f"[{source}] Error en --reset: {e}")

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
                    logger.warning(f"[{source}] relaciones fallaron (ads OK): {rel_err}")
            except Exception:
                error_msg = traceback.format_exc()
                logger.error(f"[{source}] ERROR durante upsert:\n{error_msg}")
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

    logger.info(
        f"\n{'='*50}\n"
        f"  Resumen final\n"
        f"  Fuentes procesadas : {len(sources_to_run)}\n"
        f"  ADs nuevos         : {total_new}\n"
        f"  ADs actualizados   : {total_updated}\n"
        f"  Fuentes con error  : {total_errors}\n"
        f"{'='*50}"
    )

    if total_errors:
        sys.exit(1)


if __name__ == "__main__":
    main()

