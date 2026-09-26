"""
ADs de la FAA anteriores al Federal Register, desde los registros de DRS.

La FR API y GovInfo empiezan el 1994-01-03 (`federal_register.FR_API_START_YEAR`):
ningun AD emitido antes esta en `ads`. DRS si los trae, desde 41-47-01 (unos
6.800 en ADFRAWD). Este modulo convierte los registros de `drs_documents` de
esos ADs en registros del mismo formato que emite el scraper del Federal
Register, para que entren a `ads` por el mismo camino que cualquier fila FAA
(`populate_db.normalize` -> `upsert_ads` -> `populate_relations`) con source
`federal_register`. No habla con la base ni con la API: recibe registros y
devuelve registros.

Que AD entra (determinista, con los datos de DRS y sin consultar la base):

- su numero declara un anio anterior a 1994 (`YY-NN-NN` con YY entre 40 y 93,
  tambien `T93-..`, `.. R1`, `.. L`; los de cuatro digitos son de 2001 en
  adelante), y
- ninguno de sus documentos DRS tiene fecha de publicacion desde 1994-01-01.

La segunda condicion deja fuera las revisiones de ADs viejos publicadas
despues con el mismo numero de raiz pero su propio numero ("74-08-09 R3",
publicado en 2012: la FR API ya lo trae) y los numeros de 1993 publicados en
enero de 1994. Del barrido completo de ADFRAWD salen 6.826 ADs, uno por
documento. Como segunda barrera, `populate_db` nunca pisa una fila
`federal_register` que no haya creado este camino (`raw_data.Origin`).

Applicability sin emparejar: Make y Model de DRS son arrays NO paralelos (de
1.372 documentos con varias marcas, solo 97 tienen tantas marcas como
modelos), asi que nunca se adivina que modelo es de que marca:

- una sola marca: una fila con esa marca y todos los modelos (`listed`);
- varias marcas: una fila por marca, sin modelos (`unknown`);
- ninguna: sin filas (el AD queda `failed`, no `success_empty`).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from datetime import date, datetime, timezone

#: Primer dia que cubre la FR API; lo publicado antes solo esta en DRS.
FEDERAL_REGISTER_START = date(1994, 1, 1)
#: Marca en `raw_data` de las filas que crea este camino. `populate_db` solo
#: reescribe una fila `federal_register` existente si la lleva.
ORIGIN = "drs"
#: Tope de `model` por fila de `replace_ad_applicability`
#: (`populate_db.APPLICABILITY_MODEL_MAX`; lo vigila un test). Una marca con
#: mas modelos de los que caben se reparte en varias filas de esa misma marca.
MODEL_TEXT_MAX = 2000

_YEAR_RE = re.compile(r"^T?(\d{2}|\d{4})-\d{2}-\d{2,4}(?!\d)", re.I)
_TITLE_PREFIX_RE = re.compile(r"^\s*Airworthiness\s+Directives?\s*[;:]\s*", re.I)
_LITERAL_ESCAPE_RE = re.compile(r"\\[ntr]")
# Fragmentos que DRS separa de la marca al partir por coma ("Rockwell
# Collins", "Inc."): se vuelven a pegar a la marca anterior. Solo sufijos
# societarios sueltos; nada que pueda ser un fabricante por si mismo.
_CORPORATE_SUFFIX_RE = re.compile(
    r"^(?:Inc|LLC|L\.L\.C|Ltd|Co|Corp|GmbH|S\.A|SA|AG|plc|N\.V|B\.V|S\.p\.A)\.?$", re.I)
_PLACEHOLDERS = frozenset({"n/a", "na", "none", "unknown"})


def ad_year(ad_number: str | None) -> int | None:
    """Anio que declara el numero de AD ("85-05-03" -> 1985, "T91-11-51" ->
    1991, "2017-17-03" -> 2017), o None si no tiene esa forma."""
    m = _YEAR_RE.match((ad_number or "").strip())
    if not m:
        return None
    year = int(m.group(1))
    if len(m.group(1)) == 4:
        return year
    # Dos digitos: el AD mas antiguo es de 1941 y los de 2001 en adelante
    # usan cuatro, asi que 00-39 son 2000-2039.
    return 1900 + year if year >= 40 else 2000 + year


def may_predate_federal_register(ad_number: str | None) -> bool:
    """Filtro barato por numero, antes de leer los documentos."""
    year = ad_year(ad_number)
    return year is not None and year < FEDERAL_REGISTER_START.year


def _published_before_federal_register(record: dict) -> bool:
    published = record.get("publication_date")
    return not published or str(published)[:10] < FEDERAL_REGISTER_START.isoformat()


def predates_federal_register(records: list[dict]) -> bool:
    """True si el AD de estos documentos (todos del mismo `ad_number`) es
    anterior a la FR API: numero anterior a 1994 y ningun documento
    publicado desde 1994-01-01."""
    return bool(records) and may_predate_federal_register(records[0].get("ad_number")) \
        and all(_published_before_federal_register(r) for r in records)


def group_by_ad(records: Iterable[dict]) -> dict[str, list[dict]]:
    groups: dict[str, list[dict]] = {}
    for rec in records:
        number = rec.get("ad_number")
        if number:
            groups.setdefault(number, []).append(rec)
    return groups


def pick_document(records: list[dict]) -> dict:
    """El documento que representa al AD: `Current` primero, luego el
    `doc_last_modified` mas reciente, luego el guid menor. Es el mismo orden
    que usa `link_ads_to_drs` para `ads.drs_document_guid`, asi que la fila y
    su enlace salen del mismo documento (hay ADs con dos `Current`)."""
    ordered = sorted(records, key=lambda r: str(r.get("document_guid") or ""))
    ordered.sort(key=_modified, reverse=True)
    ordered.sort(key=lambda r: r.get("status") != "Current")
    return ordered[0]


def _modified(record: dict) -> datetime:
    """`doc_last_modified` como instante: la API lo da con `Z` y la base lo
    devuelve con `+00:00`, y como texto no se comparan bien entre si."""
    value = record.get("doc_last_modified")
    try:
        moment = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return datetime.min.replace(tzinfo=timezone.utc)
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _plain(text) -> str:
    """Texto de un campo de DRS en una linea: los "\\n" literales que traen
    los titulos antiguos y cualquier espacio repetido pasan a un espacio."""
    if not isinstance(text, str):
        return ""
    return " ".join(_LITERAL_ESCAPE_RE.sub(" ", text).split())


def _strings(value) -> list[str]:
    if not isinstance(value, list):
        return []
    return [s for s in (_plain(v) for v in value) if s]


def clean_makes(makes) -> list[str]:
    """Marcas de DRS sin repetidos (sin distinguir mayusculas), sin marcas de
    relleno ("N/A") y con los sufijos societarios que DRS separo por la coma
    vueltos a pegar a la marca anterior."""
    out: list[str] = []
    for make in _strings(makes):
        if make.lower() in _PLACEHOLDERS:
            continue
        if _CORPORATE_SUFFIX_RE.match(make) and out:
            out[-1] = f"{out[-1]}, {make}"
            continue
        if make.lower() not in (m.lower() for m in out):
            out.append(make)
    return out


def _model_chunks(models: list[str]) -> list[list[str]]:
    """Modelos en grupos cuyo texto unido por ", " cabe en MODEL_TEXT_MAX."""
    chunks: list[list[str]] = []
    size = 0
    for model in models:
        extra = len(model) + (2 if chunks and chunks[-1] else 0)
        if not chunks or size + extra > MODEL_TEXT_MAX:
            chunks.append([])
            size, extra = 0, len(model)
        chunks[-1].append(model)
        size += extra
    return chunks


def applicability(makes, models) -> list[dict]:
    """`raw_data["Applicability"]` sin emparejar marcas y modelos (ver el
    docstring del modulo)."""
    holders = clean_makes(makes)
    if len(holders) == 1:
        listed = list(dict.fromkeys(_strings(models)))
        chunks = _model_chunks(listed) or [[]]
        return [{"make": holders[0], "models": chunk} for chunk in chunks]
    return [{"make": make, "models": []} for make in holders]


def _status(record: dict, superseded_by: list[str]) -> str:
    """Estado crudo para `populate_db.normalize` (`_map_status`): `Current`
    es un AD vigente; `Historical` con AD sucesor es `Superseded`; el resto
    (`Historical` sin sucesor, `Pending`) queda sin estado canonico: DRS no
    dice si se cancelo o se reemplazo."""
    status = record.get("status") or ""
    if status == "Historical" and superseded_by:
        return "Superseded"
    return status


def _product_type(payload: dict) -> str:
    types = _strings(payload.get("drs:adfrawdProductType"))
    subtypes = [s.lower() for s in _strings(payload.get("drs:adfrawdProductSubType"))]
    if len(set(types)) != 1:
        return ""
    if types[0] == "Aircraft" and "rotorcraft" in subtypes:
        return "Rotorcraft"
    return types[0]


def scraper_row(records: list[dict]) -> dict:
    """Registro con las claves del scraper del Federal Register para un AD
    (todos los documentos DRS de un mismo `ad_number`)."""
    doc = pick_document(records)
    payload = doc.get("payload") or {}
    makes = doc.get("makes") or []
    models = doc.get("models") or []
    holders = applicability(makes, models)
    superseded_by = _strings(payload.get("drs:adfrawdSupersededBy"))
    supersedes = _strings(payload.get("drs:adfrawdSupersededAD"))
    docket = _plain(payload.get("drs:adfrawdDocketNo"))
    subject = _TITLE_PREFIX_RE.sub("", _plain(payload.get("drs:title"))) \
        or _plain(payload.get("drs:adfrawdSubject"))

    row = {
        "Source": "FAA (DRS)",
        "Origin": ORIGIN,
        "AD_Number": doc["ad_number"],
        "Subject": subject,
        "Abstract": _plain(payload.get("drs:adfrawdSummary")),
        "Publication_Date": doc.get("publication_date") or "",
        "Effective_Date": doc.get("effective_date") or "",
        "Status": _status(doc, superseded_by),
        "Product_Type": _product_type(payload),
        "Docket": "" if docket.lower() in _PLACEHOLDERS else docket,
        "PDF_Link": doc.get("document_url") or "",
        "Amendment": _plain(payload.get("drs:adfrawdAmendment")),
        "Make": holders[0]["make"] if holders else "",
        "Model": ", ".join(dict.fromkeys(_strings(models))),
        "DRS_Document_Guid": doc.get("document_guid") or "",
        "DRS_Status": doc.get("status") or "",
        "DRS_Download_URL": doc.get("main_download_url") or "",
    }
    if holders:
        row["Applicability"] = holders
    if len(superseded_by) == 1:
        row["Superseded_By"] = superseded_by[0]
    if supersedes:
        row["Supersedes"] = ", ".join(supersedes)
    return row


def pre_federal_register_rows(records: Iterable[dict]) -> list[dict]:
    """Registros del scraper de los ADs anteriores a la FR API entre
    `records` (documentos de `drs_documents`, de uno o varios ADs), en orden
    de numero de AD."""
    groups = group_by_ad(records)
    return [scraper_row(docs) for number, docs in sorted(groups.items())
            if predates_federal_register(docs)]
