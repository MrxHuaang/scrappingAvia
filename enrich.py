"""
Zephr - enrich.py
Derivacion de los campos enriquecidos del modelo de dominio AD a partir de lo que
los scrapers ya extraen (status crudo, titulo, abstract, product_type, etc.).

Mantiene la logica en un solo lugar para que todas las fuentes la compartan.
Campos canonicos esperados por la tabla `ads`:
  - state_of_design : pais emisor (mapeado desde el source key)
  - status          : New | Revision | Superseded | Cancelled
  - priority        : Emergency | Normal
  - product_type    : Aircraft | Engine | Propeller | Appliance | Helicopter
  - amoc_available  : bool
  - unsafe_condition / corrective_action / compliance_time : texto best-effort
  - service_bulletins : lista de numeros SB referenciados
"""

import re


# ---------------------------------------------------------------------------
# State of Design (pais emisor del AD)
# ---------------------------------------------------------------------------
STATE_OF_DESIGN = {
    "federal_register": "United States",
    "easa": "European Union",
    "transport_canada": "Canada",
    "anac_brazil": "Brazil",
    "dgac_chile": "Chile",
    "anac_argentina": "Argentina",
    "aerocivil_colombia": "Colombia",
    "casa_australia": "Australia",
    "gcaa_uae": "United Arab Emirates",
}


def state_of_design(source_key: str) -> str | None:
    return STATE_OF_DESIGN.get(source_key)


# ---------------------------------------------------------------------------
# Status canonico (New | Revision | Superseded | Cancelled)
# ---------------------------------------------------------------------------
def canonical_status(raw: str | None, ad_number: str = "") -> str | None:
    s = (raw or "").strip().lower()
    if not s:
        return None
    if "supersed" in s:
        return "Superseded"
    if "cancel" in s:
        return "Cancelled"
    if "correct" in s or "revision" in s or "amend" in s or re.search(r"\bR\d+\b", ad_number or ""):
        return "Revision"
    # Current, Final Rule, Emergency AD, NPRM, Interim... -> nueva directiva vigente
    return "New"


# ---------------------------------------------------------------------------
# Priority (Emergency | Normal)
# ---------------------------------------------------------------------------
def priority(raw_status: str | None, *texts: str) -> str:
    blob = " ".join((t or "") for t in (raw_status, *texts)).lower()
    if "emergency" in blob:
        return "Emergency"
    return "Normal"


# ---------------------------------------------------------------------------
# Product type canonico
# ---------------------------------------------------------------------------
def normalize_product_type(pt: str | None) -> str | None:
    s = (pt or "").strip().lower()
    if not s:
        return None
    if "engine" in s or "turbofan" in s or "turbine" in s or "turboprop" in s:
        return "Engine"
    if "propeller" in s:
        return "Propeller"
    if "helicopter" in s or "rotorcraft" in s or "rotary" in s:
        return "Helicopter"
    if "appliance" in s:
        return "Appliance"
    if any(k in s for k in ("aircraft", "airplane", "airship", "glider", "balloon", "aeroplane")):
        return "Aircraft"
    cap = (pt or "").strip().capitalize()
    return cap if cap in ("Aircraft", "Engine", "Propeller", "Appliance", "Helicopter") else None


# ---------------------------------------------------------------------------
# AMOC disponible
# ---------------------------------------------------------------------------
def detect_amoc(*texts: str) -> bool:
    blob = " ".join((t or "") for t in texts).lower()
    return ("alternative method of compliance" in blob) or bool(re.search(r"\bamoc\b", blob))


# ---------------------------------------------------------------------------
# Service Bulletins referenciados
# ---------------------------------------------------------------------------
_SB_RE = re.compile(
    r"\b(?:Service\s+Bulletin|Alert\s+Service\s+Bulletin|Mandatory\s+Service\s+Bulletin|ASB|MSB|SB)\b"
    r"[\s:.#]*((?:No\.?\s*)?[A-Z0-9][A-Z0-9\-–/.]{2,})",
    re.I,
)


def extract_service_bulletins(*texts: str) -> list[str]:
    blob = " ".join((t or "") for t in texts)
    found: list[str] = []
    seen: set[str] = set()
    for m in _SB_RE.finditer(blob):
        sb = re.sub(r"^No\.?\s*", "", m.group(1).strip(), flags=re.I).rstrip(".,;")
        key = sb.upper()
        if sb and key not in seen and len(sb) >= 4:
            seen.add(key)
            found.append(sb)
    return found


# ---------------------------------------------------------------------------
# Unsafe condition / corrective action / compliance time (best-effort sobre
# el abstract estilo FAA: "...prompted by X. This AD requires Y. ...to address Z")
# ---------------------------------------------------------------------------
def _first_sentence(text: str) -> str:
    text = text.strip()
    m = re.match(r"(.+?[.])(\s|$)", text)
    return (m.group(1) if m else text).strip()


def _is_boilerplate_unsafe(s: str) -> bool:
    # FAA closes most abstracts with "...to address the unsafe condition on these
    # products" — generic filler, not the actual condition.
    low = s.strip().lower()
    return low.startswith("the unsafe condition") or low in ("this unsafe condition", "it")


def extract_unsafe_condition(abstract: str | None) -> str | None:
    a = (abstract or "").strip()
    if not a:
        return None
    # The real cause is in "prompted by X"; try it first.
    m = re.search(r"prompted by\s+(.+?)(?:\.\s|$)", a, re.I)
    if m:
        cand = m.group(1).strip().rstrip(".")
        if cand and not _is_boilerplate_unsafe(cand):
            return cand
    # Fall back to "to address X" only if it isn't the generic boilerplate.
    m = re.search(r"to address\s+(.+?)(?:\.\s|$)", a, re.I)
    if m:
        cand = m.group(1).strip().rstrip(".")
        if cand and not _is_boilerplate_unsafe(cand):
            return cand
    return None


def extract_corrective_action(abstract: str | None) -> str | None:
    a = (abstract or "").strip()
    if not a:
        return None
    m = re.search(r"This AD requires\s+(.+?)(?:\.\s|$)", a, re.I)
    if m:
        return m.group(1).strip().rstrip(".") or None
    m = re.search(r"\brequires\s+(.+?)(?:\.\s|$)", a, re.I)
    if m:
        return m.group(1).strip().rstrip(".") or None
    return None


_COMPLIANCE_RE = re.compile(
    r"within\s+\d[\d,]*\s*(?:flight\s+)?(?:hours|days|weeks|months|years|cycles|landings|flights|hrs)\b",
    re.I,
)


def extract_compliance_time(*texts: str) -> str | None:
    blob = " ".join((t or "") for t in texts)
    m = _COMPLIANCE_RE.search(blob)
    return m.group(0).strip() if m else None


# Phrases que aparecen como plazo de cumplimiento en el cuerpo del AD (FAA).
_COMPLIANCE_PHRASES = re.compile(
    r"(before further flight"
    r"|within\s+\d[\d,]*\s*(?:flight\s+|operating\s+)?"
    r"(?:hours?|days?|weeks?|months?|years?|cycles?|landings?|flights?|hrs?)"
    r"(?:\s+(?:time-in-service|after the effective date(?:\s+of this AD)?|TIS))?"
    r"|at the next[^.,;]{0,60}inspection)",
    re.I,
)


def extract_compliance_from_fulltext(text: str | None) -> str | None:
    """
    Extrae el plazo de cumplimiento del cuerpo COMPLETO del AD (no del abstract).
    Localiza la seccion '(g) Compliance' y busca una frase de plazo; si no hay
    patron explicito, cae a la primera oracion de esa seccion.
    """
    if not text:
        return None
    m = re.search(r"\([a-z]\)\s*Compliance\b(.{0,1500}?)(?:\([a-z]\)\s+[A-Z]|$)", text, re.I | re.S)
    scope = m.group(1) if m else text
    pm = _COMPLIANCE_PHRASES.search(scope)
    if pm:
        return re.sub(r"\s+", " ", pm.group(0)).strip()
    if m:
        first = re.split(r"(?<=[.])\s", re.sub(r"\s+", " ", scope).strip())[0]
        first = first.strip()
        if 8 <= len(first) <= 240:
            return first
    return None
