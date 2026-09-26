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
    "uk_caa": "United Kingdom",
}


def state_of_design(source_key: str) -> str | None:
    return STATE_OF_DESIGN.get(source_key)


# ---------------------------------------------------------------------------
# Autoridad emisora (quien firmo el AD) vs base donde lo leimos
# ---------------------------------------------------------------------------
# Cada autoridad republica las directivas de los estados de diseno que reconoce,
# y nosotros indexamos cada base por separado, asi que la fuente dice DONDE lo
# encontramos, no QUIEN lo emitio. Medido en produccion (2026-09-19):
#
#   easa              18.470 filas - 3.972 numeradas US- (FAA) y 1.109 CF- (TC):
#                     el 27% de ese corpus lo emitio otro.
#   transport_canada   4.798 filas - solo 975 son CF-; el resto son ADs de FAA y
#                     EASA que Canada adopto. Mas de la mitad.
#
# Para un operador no es cosmetico: quien emitio el AD decide de que autoridad
# es el proceso de AMOC y que reglas de estado de diseno aplican.
#
# El numero es la evidencia: un AD republicado conserva el numero del emisor
# —para eso se republica—. Por eso la regla lee el numero DENTRO de su corpus y
# nunca de forma global: `2020-01-02` es un numero de FAA en la base canadiense
# y uno brasileno en la de ANAC, y ninguna forma los distingue por si sola.
#
# Espejo exacto de lib/ads/issuingAuthority.ts en el repo del front (misma tabla
# de casos en los tests de ambos lados).

# FAA: 2024-01-02, legado 98-21-09, revision opcional (2018-23-51R1).
_FAA_NUMBER = re.compile(r"^(?:\d{2}|\d{4})-\d{2}-\d{2}(?:R\d+)?$", re.I)
# EASA: 2024-0195, 2024-0195R1 - cuatro digitos tras el ano, no dos.
_EASA_NUMBER = re.compile(r"^\d{4}-\d{3,4}(?:R\d+)?$", re.I)
# Transport Canada: CF-2015-19, CF-90-03, CF-2026-45-E.
_TC_NUMBER = re.compile(r"^CF-", re.I)
# Numero extranjero que EASA prefija con el estado de diseno.
_EASA_FOREIGN_PREFIX = re.compile(r"^(US|CF)-", re.I)


# Transport Canada pone el pais de origen en el enlace del documento: cn=US,
# cn=CF, cn=EU, cn=BZ. Es la autoridad DICIENDO de donde viene la directiva, que
# vale mas que inferirlo de la forma del numero. Sobre las 4.798 filas
# canadienses: US 2.068, CF 911, EU 524, FR 242, BZ 161, UK 2, GY 2, IL 1, y 887
# sin enlace. La forma del numero y el codigo discrepan en 34 de las 3.503 con
# codigo: ahi esta el valor — "2024-08-01" en la base canadiense es una
# directiva de EMBRAER, y la forma la leeria como de la FAA.
#
# Solo se mapean los cinco codigos que tienen corpus propio. FR, GY e IL quedan
# a la regla del numero: atribuirlos a una autoridad que no indexamos seria
# afirmar mas de lo que podemos mostrar.
_TC_LINK_COUNTRY = re.compile(r"[?&]cn=([A-Za-z]+)", re.I)
_TC_COUNTRY_SOURCE = {
    "US": "federal_register",
    "CF": "transport_canada",
    "EU": "easa",
    "BZ": "anac_brazil",
    "UK": "uk_caa",
}


def issuing_authority(ad_number: str, source_key: str, pdf_link: str | None = None) -> str:
    """
    Source key de la autoridad que EMITIO el AD.

    Devuelve `source_key` cuando no puede probar otra cosa: esta funcion corrige
    una atribucion solo con la evidencia disponible, nunca la inventa.
    """
    if source_key == "transport_canada" and pdf_link:
        m = _TC_LINK_COUNTRY.search(pdf_link)
        if m:
            mapped = _TC_COUNTRY_SOURCE.get(m.group(1).upper())
            if mapped:
                return mapped

    n = (ad_number or "").strip()
    if not n:
        return source_key

    if source_key == "easa":
        # EASA guarda el origen en el prefijo: US-2026-17-08, CF-2026-45. Los
        # numeros nacionales legado (F-, G-, D-) NO son extranjeros: son las
        # directivas de los estados miembro que EASA absorbio y administra.
        m = _EASA_FOREIGN_PREFIX.match(n)
        if not m:
            return source_key
        return "federal_register" if m.group(1).upper() == "US" else "transport_canada"

    if source_key == "transport_canada":
        if _TC_NUMBER.match(n):
            return source_key
        if _FAA_NUMBER.match(n):
            return "federal_register"
        if _EASA_NUMBER.match(n):
            return "easa"
        return source_key

    # El resto de los corpus indexados numera solo lo propio (ANAC "2025-09-01",
    # DGAC "DA 84-01", ANAC AR "ADV 337-DAG", UK CAA "CAP747-055"). Afirmar mas
    # de lo que la evidencia sostiene pondria la bandera equivocada en la otra
    # direccion.
    return source_key


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
# Product type de la FAA desde el título
# ---------------------------------------------------------------------------
# Sustantivo de producto con el que la FAA cierra el título del AD ("The Boeing
# Company Airplanes", "CFM International S.A. Turbofan Engines", "Hamilton
# Sundstrand Propellers"). Plurales incluidos: `\bengine\b` no casa "Engines".
_TITLE_PRODUCT_NOUNS = re.compile(
    r"\b(?P<aircraft>airplanes?|aircraft|helicopters?|rotorcraft|gliders?|sailplanes?"
    r"|airships?|balloons?|gyroplanes?)\b"
    r"|\b(?P<engine>engines?|turbofans?|turbojets?|turboprops?|turboshafts?)\b"
    r"|\b(?P<propeller>propellers?)\b"
    r"|\b(?P<appliance>appliances?)\b",
    re.I,
)

# "... Airplanes Equipped With General Electric CF6-80C2 Series Engines" es un
# AD de avión: el motor viene DESPUÉS del producto, como calificativo. Se corta
# el título en esa frase antes de leer el último sustantivo. Misma regla que la
# migración 20260923180000_fr_product_type_from_title.sql de ad-scrapper.
_AIRCRAFT_QUALIFIER = re.compile(
    r"(\b(?:airplanes?|helicopters?|rotorcraft|aircraft|gliders?|sailplanes?)\b)"
    r"\s+(?:equipped|powered|fitted|having|with)\b.*$",
    re.I,
)


def _title_product_noun(subject: str) -> "re.Match | None":
    """El sustantivo de producto que decide el tipo: el ÚLTIMO del título, tras
    cortar el calificativo "<Airplanes> Equipped With ...". Lo comparten
    `classify_product_type` y `product_subtype`, así el subtipo sale siempre del
    mismo sustantivo que decidió el tipo."""
    title = _AIRCRAFT_QUALIFIER.sub(r"\1", subject or "", count=1)
    last = None
    for m in _TITLE_PRODUCT_NOUNS.finditer(title):
        last = m
    return last


def classify_product_type(subject: str, abstract: str = "", cfr_refs=()) -> str:
    """
    Tipo de producto de un AD de la FAA a partir de su título.

    El título es el campo que declara el producto; el abstract sólo lo
    describe. Antes se buscaba "engine" en título + abstract, y el resumen de
    un AD de avión casi siempre menciona un motor ("...engine fuel pump...",
    "...an engine fire..."): 1.138 ADs cuyo título termina en "Airplanes" o
    "Helicopters" quedaron como Engine, y filtrar por Product type = Aircraft
    los escondía.

    Gana el ÚLTIMO sustantivo de producto del título, porque la FAA lo pone al
    final y el nombre del titular puede llevar otro antes ("Airbus Helicopters
    Model EC135 Helicopters", "Rolls-Royce Deutschland Ltd & Co KG Turbofan
    Engines"). Sin sustantivo en el título se cae a la regla vieja sobre el
    abstract, y luego a CFR parte 39.
    """
    noun = _title_product_noun(subject)
    if noun:
        return {"aircraft": "Aircraft", "engine": "Engine",
                "propeller": "Propeller", "appliance": "Appliance"}[noun.lastgroup]

    text = (abstract or "").lower()
    if re.search(r"\bengines?\b", text):
        return "Engine"
    if re.search(r"\bpropellers?\b", text):
        return "Propeller"
    if re.search(r"\bappliances?\b", text):
        return "Appliance"
    if re.search(r"\baircraft\b|\bairplanes?\b|\bhelicopters?\b|\bairships?\b|\bgliders?\b", text):
        return "Aircraft"
    for ref in cfr_refs or ():
        part = str(ref.get("part", "") if isinstance(ref, dict) else "")
        if part == "39":
            return "Aircraft"
    return ""


# ---------------------------------------------------------------------------
# Catálogo de la BD: product_type / product_subtype
# ---------------------------------------------------------------------------
# product_type canónico (normalize_product_type) -> código del catálogo
# (tablas product_type/product_subtype, migración 20260926120000 de
# ad-scrapper). Un AD sin tipo derivable no tiene código: el llamador omite el
# campo y la RPC lo deriva server-side de ads.product_type.
PRODUCT_TYPE_CODE = {
    "Aircraft": "aircraft",
    "Helicopter": "rotorcraft",
    "Engine": "engine",
    "Propeller": "propeller",
    "Appliance": "appliance",
}

# Catálogo CERRADO de subtipos por tipo, copia del seed de la migración. La FK
# compuesta (product_type, product_subtype) rechaza un subtipo de otro tipo;
# aquí se filtra antes para no tumbar el lote. 'appliance/other' existe en el
# catálogo pero no se emite nunca: "no sé cuál" es omitir, no 'other'.
PRODUCT_SUBTYPES = {
    "aircraft": frozenset({"airplane", "glider", "balloon", "airship", "powered_lift"}),
    "rotorcraft": frozenset({"helicopter", "gyroplane"}),
    "engine": frozenset({"turbine", "piston", "apu", "electric"}),
    "propeller": frozenset(),
    "appliance": frozenset({"seats", "avionics", "elt", "oxygen", "fire_protection",
                            "landing_gear", "lighting", "evacuation", "instruments", "other"}),
}

# Sustantivo que decidió el tipo -> subtipo. Solo sustantivos que nombran el
# subtipo sin ambigüedad: "aircraft", "rotorcraft" y "engines" a secas no dicen
# cuál es, y se omiten.
_NOUN_SUBTYPE = {
    "airplane": "airplane", "airplanes": "airplane",
    "glider": "glider", "gliders": "glider", "sailplane": "glider", "sailplanes": "glider",
    "balloon": "balloon", "balloons": "balloon",
    "airship": "airship", "airships": "airship",
    "helicopter": "helicopter", "helicopters": "helicopter",
    "gyroplane": "gyroplane", "gyroplanes": "gyroplane",
    "turbofan": "turbine", "turbofans": "turbine", "turbojet": "turbine", "turbojets": "turbine",
    "turboprop": "turbine", "turboprops": "turbine", "turboshaft": "turbine", "turboshafts": "turbine",
}
# Calificativo del motor delante de "Engines": "Turbofan Engines",
# "Reciprocating Engines", "Turboprop and Turboshaft Engines". Se exige que la
# frase entera apunte a UNA categoría. "Turbo-Propeller" no es "turboprop".
_ENGINE_KIND = (
    ("turbine", re.compile(r"\b(?:turbofans?|turbojets?|turboprops?|turboshafts?|gas\s+turbine|turbine)\b", re.I)),
    ("piston", re.compile(r"\b(?:reciprocating|piston|diesel)\b", re.I)),
    ("electric", re.compile(r"\belectric\b", re.I)),
)
# Solo la cadena de calificativos pegada al sustantivo: el nombre del titular
# puede llevar otros ("Allison Gas Turbine Division, and Detroit Diesel
# Allison) 250-B Series Turboshaft Engines").
_ENGINE_CHAIN_RE = re.compile(
    r"(?:\b(?:turbofans?|turbojets?|turboprops?|turboshafts?|gas|turbine|reciprocating|piston"
    r"|diesel|electric|and|series|model)\b[\s,]*)+$",
    re.I,
)
_APU_RE = re.compile(r"\b(?:auxiliary\s+power\s+units?|APUs?)\b", re.I)
# Appliances: la categoría la da la pieza nombrada. Tiene que salir UNA sola;
# dos ("Oxygen ... and Fire Extinguishers") es no saber, y se omite.
_APPLIANCE_KIND = (
    ("seats", re.compile(r"\b(?:seats?|seating|berths?)\b", re.I)),
    ("elt", re.compile(r"\b(?:emergency\s+locator\s+transmitters?|ELTs?)\b", re.I)),
    ("oxygen", re.compile(r"\boxygen\b", re.I)),
    ("fire_protection", re.compile(
        r"\b(?:fire\s+extinguish\w*|extinguishers?|smoke\s+detect\w*|fire\s+detect\w*|fire\s+suppression)\b", re.I)),
    ("evacuation", re.compile(r"\b(?:evacuation|escape\s+slides?|slides?/rafts?|life\s+rafts?)\b", re.I)),
    ("landing_gear", re.compile(r"\b(?:landing\s+gear|wheels?|brakes?|tires?)\b", re.I)),
    ("lighting", re.compile(r"\b(?:lights?|lighting|lamps?)\b", re.I)),
    ("avionics", re.compile(
        r"\b(?:avionics|transponders?|radios?|autopilots?|flight\s+displays?|navigation\s+units?"
        r"|communication\s+units?|receivers?|flight\s+management|TAWS|traffic\s+(?:alert|collision))\b", re.I)),
    ("instruments", re.compile(r"\b(?:instruments?|indicators?|altimeters?|gauges?)\b", re.I)),
)
# Lo que viene tras "Installed on" / "Equipped with" describe DÓNDE va la
# pieza, no qué pieza es ("Seats Installed on Airplanes Equipped With Oxygen").
_INSTALLED_ON_RE = re.compile(r"\s+(?:installed|equipped|fitted|approved)\b.*$", re.I | re.S)


def _one_kind(kinds, text: str) -> str | None:
    found = {code for code, rx in kinds if rx.search(text)}
    return found.pop() if len(found) == 1 else None


def product_subtype(product_type_code: str | None, subject: str) -> str | None:
    """
    Subtipo del catálogo para una fila de applicability, o None.

    Se deriva del mismo texto que ya decide el tipo (título FAA, subject de
    EASA/TC/UK CAA/ANAC) y SOLO si pertenece a `product_type_code`: un título
    FAA "... Helicopters" tipado Aircraft no produce 'helicopter' (es de
    rotorcraft) — se omite en vez de mentir sobre el tipo o sobre el subtipo.
    Sin coincidencia segura devuelve None; nunca adivina ni cae a 'other'.

    - aircraft/rotorcraft: el sustantivo que decidió el tipo ("Airplanes",
      "Sailplanes", "Hot Air Balloons", "Helicopters").
    - engine: el calificativo delante del sustantivo ("Turbofan Engines" ->
      turbine, "Reciprocating Engines" -> piston); "Auxiliary Power Units" ->
      apu cuando no hay otro sustantivo de motor.
    - appliance: la pieza nombrada (antes de "Installed on ..."), una sola.
    - propeller: el catálogo no tiene subtipos.
    """
    allowed = PRODUCT_SUBTYPES.get(product_type_code or "")
    if not allowed:
        return None
    text = subject or ""
    code: str | None = None
    noun = _title_product_noun(text)
    if noun and product_type_code != "appliance" and _INSTALLED_ON_RE.search(text[: noun.start()]):
        # "... APUs Installed on Airbus Airplanes": el sustantivo nombra dónde
        # va la pieza, no el producto del AD.
        return None
    if product_type_code in ("aircraft", "rotorcraft"):
        if noun and noun.lastgroup == "aircraft":
            code = _NOUN_SUBTYPE.get(noun.group(0).lower())
    elif product_type_code == "engine":
        if noun and noun.lastgroup == "engine":
            chain = _ENGINE_CHAIN_RE.search(text[: noun.start()].rstrip())
            code = _NOUN_SUBTYPE.get(noun.group(0).lower()) or (
                _one_kind(_ENGINE_KIND, chain.group(0)) if chain else None)
        elif not noun and _APU_RE.search(text):
            code = "apu"
    elif product_type_code == "appliance":
        code = _one_kind(_APPLIANCE_KIND, _INSTALLED_ON_RE.sub("", text))
    if code == "other" or code not in allowed:
        return None
    return code


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
