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

# Agregar directorio raiz al path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scrapers.federal_register import search_ads as fr_search
from scrapers.faa_govinfo import search_ads as govinfo_search
from scrapers.faa_drs import search_ads as drs_search
from scrapers.easa import search_ads as easa_search, search_main_page as easa_main
from scrapers.transport_canada import search_ads as tc_search
from scrapers.anac_brazil import search_ads as anac_brazil_search
from scrapers.dgac_chile import search_ads as dgac_chile_search
from scrapers.anac_argentina import search_ads as anac_argentina_search
from scrapers.aerocivil_colombia import search_ads as aerocivil_search
from scrapers.casa_australia import search_ads as casa_search
from scrapers.gcaa_uae import search_ads as gcaa_search

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
    """
    ad_number = normalize_ad_number(row.get("AD_Number") or "")
    if not ad_number:
        return None

    return {
        "source": source_key,
        "source_label": SOURCE_LABELS.get(source_key, source_key),
        "ad_number": ad_number,
        "subject": (row.get("Subject") or "").strip() or None,
        "make": (row.get("Make") or "").strip() or None,
        "model": (row.get("Model") or "").strip() or None,
        "product_type": (row.get("Product_Type") or "").strip() or None,
        "effective_date": parse_date(row.get("Effective_Date")),
        "status": _map_status(row.get("Status")),
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
            rec["raw_data"] = json.dumps(rec["raw_data"], ensure_ascii=False, default=str)
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

    Join key: el FR document_number, que FR API guarda en "Docket" y GovInfo
    expone como "GranuleId" (son el mismo identificador del Federal Register).

    En match: la fila FR es la base (mejor Subject/Make/Model via abstract,
    effective_on real, pdf de federalregister.gov) y GovInfo aporta la
    identidad correcta: AD_Number real del MODS XML, Docket FAA-xxxx-xxxx
    y Amendment 39-xxxxx. Esto repara el defecto de FR API, cuyo titulo casi
    nunca trae el numero AD y cae a document_number como ad_number.

    Filas sin match pasan tal cual; el dedupe last-wins de upsert_ads()
    queda como red de seguridad.
    """
    fr_by_docnum: dict[str, dict] = {}
    for row in fr_rows:
        doc_num = (row.get("Docket") or "").strip()
        if doc_num:
            fr_by_docnum[doc_num] = row

    merged_count = 0
    extra_govinfo: list[dict] = []
    for grow in govinfo_rows:
        granule_id = (grow.get("GranuleId") or "").strip()
        fr_row = fr_by_docnum.get(granule_id)
        if fr_row is not None:
            fr_row["AD_Number"] = grow["AD_Number"]
            if grow.get("Docket") and not grow["Docket"].startswith("FR-"):
                fr_row["Docket"] = grow["Docket"]
            if grow.get("Amendment"):
                fr_row["Amendment"] = grow["Amendment"]
            merged_count += 1
        else:
            extra_govinfo.append(grow)

    logger.info(
        f"  Merge FAA: {merged_count} filas FR enriquecidas con MODS, "
        f"{len(extra_govinfo)} solo-GovInfo, {len(fr_rows) - merged_count} solo-FR"
    )
    return fr_rows + extra_govinfo


def run_federal_register(since: str | None = None) -> list[dict]:
    """
    Source 'federal_register' consolida 3 upstreams FAA bajo el mismo source key:
      1. DRS (drs.faa.gov)        -> pre-1995 historicos (best-effort)
      2. FR API (year-split)       -> 1994+ via federalregister.gov (contenido)
      3. GovInfo API (MODS XML)    -> 1994+ via api.govinfo.gov (identidad:
         numero AD real, docket FAA-xxxx, amendment 39-xxxxx)

    FR API y GovInfo se fusionan por FR document_number (_merge_faa_results).
    since (YYYY-MM-DD): acota FR API y GovInfo a publicaciones desde esa fecha
    (runs incrementales); DRS no aplica (historicos pre-1995).
    """
    logger.info("Scraping federal_register (3 fases: DRS + FR + GovInfo MODS)...")
    all_results: list[dict] = []

    # Fase 1: DRS pre-1995 (best-effort, no falla si Akamai bloquea)
    logger.info("  [1/3] DRS (drs.faa.gov, pre-1995 historicos)...")
    try:
        drs_results = drs_search(pre_govinfo_only=True)
        logger.info(f"  DRS: {len(drs_results)} ADs")
        all_results.extend(drs_results)
    except Exception as e:
        logger.warning(f"  DRS fallo (continua pipeline): {e}")

    # Fase 2: FR API year-split (1994 -> current)
    logger.info("  [2/3] Federal Register API (year-split)...")
    if since:
        from scrapers.federal_register import search_ads_by_year_range
        # Year-split desde el anio de --since; el filtro fino por fecha lo da
        # el propio year range (FR API filtra por publication_date por anio).
        fr_results = search_ads_by_year_range(int(since[:4]), datetime.now().year)
    else:
        fr_results = fr_search()
    logger.info(f"  FR API: {len(fr_results)} ADs")

    # Fase 3: GovInfo MODS â€” identidad real del AD (numero, docket, amendment).
    # El titulo de FR API casi nunca trae el numero AD: sin esta fase, los
    # ad_numbers de FAA quedan como FR document_numbers (defecto historico).
    logger.info("  [3/3] GovInfo API (MODS XML: numero AD real + docket + amendment)...")
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
            if source == "federal_register" and args.since:
                raw_results = run_federal_register(since=args.since)
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

