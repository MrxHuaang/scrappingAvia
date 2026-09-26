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
  - secciones del texto de un rule FAA : split_ad_sections
"""

import html
import re

from utils import normalize_ad_number


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


# Lo que viene tras "Installed on" / "Equipped with" / "Approved Under"
# describe DÓNDE va (o bajo qué norma) el producto, no cuál es: "Superior Air
# Parts, Inc., Piston Pins Installed on Teledyne Continental Motors
# Reciprocating Engines" es un AD de la pieza. A diferencia del corte de
# arriba, este vale tras cualquier palabra ("Units, Equipped with ...",
# "Airplanes, Equipped With ... Engines"). "Powered" no está: "Turbo-Propeller
# Powered ... Airplanes" es un avión, y lo resuelve el corte de arriba.
_INSTALLED_ON_RE = re.compile(r"[\s,;]+(?:installed|equipped|fitted|approved)\b.*$", re.I | re.S)

# Palabras de rotorcraft dentro del grupo aircraft: la FAA las cierra igual
# ("... Helicopters") pero el tipo canónico es Helicopter.
_ROTORCRAFT_NOUN_RE = re.compile(r"^(?:helicopters?|rotorcraft|gyroplanes?)$", re.I)
# Pieza nombrada en el título: la palabra que convierte al AD en un AD de
# appliance cuando no hay sustantivo de producto que la supere ("Passenger
# Seats", "Emergency Locator Transmitters", "Oxygen Mask Regulators") o cuando
# el sustantivo solo la califica ("Reciprocating Engine Connecting Rods",
# "RECARO Aircraft Seating"). Nombres de pieza concretos: "Parts",
# "Components" o "Systems" van en razones sociales y no cuentan.
_PART_RE = re.compile(
    r"\b(?:seats?|seating|berths?|belts?|harness(?:es)?|restraints?|transmitters?|receivers?"
    r"|transponders?|radios?|autopilots?|displays?|indicators?|instruments?|altimeters?"
    r"|gauges?|detectors?|extinguishers?|bottles?|masks?|regulators?|cylinders?|valves?"
    r"|pumps?|carburetors?|magnetos?|starters?|generators?|alternators?|batter(?:y|ies)"
    r"|hoists?|actuators?|tires?|wheels?|brakes?|lights?|lamps?|slides?|rafts?|seals?"
    r"|connecting\s+rods?|pins?|servos?|controllers?|computers?|sensors?|lifts?|hooks?)\b",
    re.I,
)
_APU_RE = re.compile(r"\b(?:auxiliary\s+power\s+units?|APUs?)\b", re.I)


# "(Type Certificate Previously Held by Allison Engine Company)", "(Formerly
# Intertechnique Aircraft Systems)": nombran a un titular anterior, no al
# producto. Se borran con espacios para no mover las posiciones.
_PREVIOUS_HOLDER_RE = re.compile(r"\((?:Type\s+Certificate\s+)?(?:Previously|Formerly)\b[^()]*\)", re.I)
# Primera oración del abstract: la de applicability ("for all Sikorsky Model
# 269A ... helicopters."). "Ltd. Model" no la corta.
_FIRST_SENTENCE_RE = re.compile(r"\.\s+(?=(?:This|The|We|That|These|AD|As|In|Since)\b)")


def _faa_holders(subject: str, abstract: str) -> tuple[str | None, tuple[str, ...]]:
    """(titular, titulares anteriores) del título FAA, con la misma extracción
    de applicability. Import diferido: `scrapers/__init__` importa el scraper
    FR, que a su vez importa este módulo."""
    from scrapers.common.fr_applicability import title_holders

    return title_holders(subject or "", abstract or "")


def _blank(text: str, start: int, end: int) -> str:
    return text[:start] + " " * (end - start) + text[end:]


def _product_phrase(subject: str, abstract: str | None = None, *, installed_cut: bool = True) -> str:
    """El título donde nombra el producto, con las mismas posiciones que
    `subject`: sin los paréntesis de titular anterior, sin el calificativo
    "<Airplanes> Equipped With ...", (salvo `installed_cut=False`) sin lo que
    sigue a "Installed on ..." y, en un título FAA (`abstract` no None), con
    el titular borrado: su sustantivo no decide ("Sikorsky Aircraft
    Corporation", "Airship Industries", "Lycoming Engines")."""
    title = subject or ""
    if abstract is not None:
        holder, _ = _faa_holders(title, abstract)
        i = title.find(holder) if holder else -1
        if 0 <= i <= 4:
            title = _blank(title, 0, i + len(holder))
    title = _PREVIOUS_HOLDER_RE.sub(lambda m: " " * len(m.group(0)), title)
    title = _AIRCRAFT_QUALIFIER.sub(r"\1", title, count=1)
    return _INSTALLED_ON_RE.sub("", title, count=1) if installed_cut else title


def _last_noun(text: str) -> "re.Match | None":
    last = None
    for m in _TITLE_PRODUCT_NOUNS.finditer(text):
        last = m
    return last


def _abstract_product_noun(subject: str, abstract: str) -> tuple[str, "re.Match"] | None:
    """Respaldo cuando el título no nombra producto fuera del titular ("Bell
    Helicopter Textron Canada Limited", "Airbus Helicopters (Formerly
    Eurocopter France)"): el PRIMER sustantivo de producto de la primera
    oración del abstract, con los nombres de titular borrados. Es la oración
    de applicability ("for certain Bell Model 206L-1 ... helicopters"); el
    resto del abstract habla de motores y hélices aunque el AD sea de otra
    cosa. Devuelve (texto, match) para que el subtipo lea el mismo sustantivo."""
    text = _applicability_sentence(subject, abstract)
    m = _TITLE_PRODUCT_NOUNS.search(text)
    return (text, m) if m else None


def _applicability_sentence(subject: str, abstract: str) -> str:
    """Primera oración del abstract sin nombres de titular y cortada en
    "installed on": "for certain (formerly ...) flightcrew oxygen mask
    regulators as installed on, but not limited to, various airplanes" es un
    AD del regulador, no de un avión."""
    text = re.split(_FIRST_SENTENCE_RE, _abstract_without_holders(subject, abstract), maxsplit=1)[0]
    return _INSTALLED_ON_RE.sub("", text, count=1)


def _abstract_without_holders(subject: str, abstract: str) -> str:
    """El abstract con el titular y los anteriores borrados: "for Sikorsky
    Aircraft Corporation (Sikorsky) Model 269A ... helicopters" no es un AD de
    Aircraft."""
    holder, previous = _faa_holders(subject, abstract)
    text = abstract or ""
    for name in (holder, *previous):
        if name:
            text = re.sub(re.escape(name), lambda m: " " * len(m.group(0)), text, flags=re.I)
    return text


def _deciding_noun(subject: str, abstract: str | None) -> tuple[str, "re.Match"] | None:
    """(texto, sustantivo) que decide el tipo: el último de la frase de
    producto del título y, en un título FAA sin él, el del abstract. Lo
    comparten `classify_product_type` y `product_subtype`, así el subtipo sale
    siempre del mismo sustantivo que decidió el tipo."""
    phrase = _product_phrase(subject, abstract)
    noun = _last_noun(phrase)
    if noun:
        return phrase, noun
    if abstract and _title_product_type(subject, abstract) is None:
        return _abstract_product_noun(subject, abstract)
    return None


def _noun_type(noun: "re.Match") -> str:
    if noun.lastgroup == "aircraft":
        return "Helicopter" if _ROTORCRAFT_NOUN_RE.match(noun.group(0)) else "Aircraft"
    return {"engine": "Engine", "propeller": "Propeller", "appliance": "Appliance"}[noun.lastgroup]


def _title_product_type(subject: str, abstract: str = "") -> str | None:
    """Tipo canónico que declara el título FAA (fuera del titular), o None."""
    phrase = _product_phrase(subject, abstract)
    noun = _last_noun(phrase)
    if noun:
        # "Reciprocating Engine Connecting Rods", "... Model 3410 Seats": el
        # sustantivo califica a una pieza que viene detrás.
        tail = re.split(r"[,;(]", phrase[noun.end():], maxsplit=1)[0]
        if _PART_RE.search(" ".join(tail.split()[:3])):
            return "Appliance"
        return _noun_type(noun)
    if _APU_RE.search(phrase):
        return "Engine"
    placed_on = _TITLE_PRODUCT_NOUNS.search(_product_phrase(subject, abstract, installed_cut=False))
    if placed_on or _PART_RE.search(phrase):
        # "<pieza> Installed on <titular> Airplanes" o una pieza sin producto
        # alguno ("Passenger Seats"): el AD es de la pieza.
        return "Appliance"
    return None


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
    Engines"). Helicopters/Rotorcraft/Gyroplanes son Helicopter, no Aircraft:
    el filtro de tipo los separa. Un AD cuyo producto es una pieza ("Passenger
    Seats", "<pieza> Installed on ... Airplanes", "Reciprocating Engine
    Connecting Rods") es Appliance; "Auxiliary Power Units" es Engine (subtipo
    apu). Un sustantivo dentro del nombre del titular no cuenta ("Sikorsky
    Aircraft Corporation", "Lycoming Engines").

    Si el título no nombra producto fuera del titular, decide el primer
    sustantivo de la oración de applicability del abstract ("for certain Bell
    Model 206L-1 ... helicopters"), y si tampoco hay, la regla vieja sobre el
    abstract (sin los nombres de titular) y luego CFR parte 39.
    """
    from_title = _title_product_type(subject, abstract)
    if from_title:
        return from_title
    from_abstract = _abstract_product_noun(subject, abstract) if abstract else None
    if from_abstract:
        return _noun_type(from_abstract[1])
    if abstract and _PART_RE.search(_applicability_sentence(subject, abstract)):
        # "for certain Kidde Graviner hand-operated fire extinguishers": la
        # oración de applicability nombra una pieza y ningún producto.
        return "Appliance"

    text = _abstract_without_holders(subject, abstract).lower() if abstract else ""
    if re.search(r"\bengines?\b", text):
        return "Engine"
    if re.search(r"\bpropellers?\b", text):
        return "Propeller"
    if re.search(r"\bappliances?\b", text):
        return "Appliance"
    if re.search(r"\baircraft\b|\bairplanes?\b|\bairships?\b|\bgliders?\b", text):
        return "Aircraft"
    if re.search(r"\bhelicopters?\b|\brotorcraft\b", text):
        return "Helicopter"
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
def _one_kind(kinds, text: str) -> str | None:
    found = {code for code, rx in kinds if rx.search(text)}
    return found.pop() if len(found) == 1 else None


def product_subtype(product_type_code: str | None, subject: str, abstract: str | None = None) -> str | None:
    """
    Subtipo del catálogo para una fila de applicability, o None.

    Se deriva del mismo sustantivo que decide el tipo y SOLO si pertenece a
    `product_type_code`: un "... Helicopters" que otra fuente tipara Aircraft
    no produce 'helicopter' (es de rotorcraft) — se omite en vez de mentir sobre
    el tipo o el subtipo. Sin coincidencia segura devuelve None; nunca adivina
    ni cae a 'other'.

    `abstract` (aunque sea "") marca un título FAA: el titular se ignora y, si
    el título no nombra producto fuera de él, se lee el sustantivo del abstract
    que usó `classify_product_type`. Con `abstract=None` (EASA/TC/UK CAA/ANAC)
    se lee el subject tal cual.

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
    code: str | None = None
    found = _deciding_noun(subject or "", abstract)
    text, noun = found if found else ("", None)
    if product_type_code in ("aircraft", "rotorcraft"):
        if noun and noun.lastgroup == "aircraft":
            code = _NOUN_SUBTYPE.get(noun.group(0).lower())
    elif product_type_code == "engine":
        if noun and noun.lastgroup == "engine":
            chain = _ENGINE_CHAIN_RE.search(text[: noun.start()].rstrip())
            code = _NOUN_SUBTYPE.get(noun.group(0).lower()) or (
                _one_kind(_ENGINE_KIND, chain.group(0)) if chain else None)
        elif not noun and _APU_RE.search(_product_phrase(subject, abstract)):
            code = "apu"
    elif product_type_code == "appliance":
        code = _one_kind(_APPLIANCE_KIND, _product_phrase(subject, abstract))
        if code is None and abstract and _title_product_type(subject, abstract) is None:
            # El título solo nombra al titular ("Kidde Graviner"): la pieza
            # está en la oración de applicability, igual que para el tipo.
            code = _one_kind(_APPLIANCE_KIND, _applicability_sentence(subject, abstract))
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


# ---------------------------------------------------------------------------
# Texto del rule FAA (GovInfo) en secciones, anclado al encabezado de identidad
# ---------------------------------------------------------------------------
# El granulo de GovInfo trae el documento FR entero: preámbulo, discusión y la
# enmienda a 14 CFR 39.13 con el texto regulatorio del AD. Solo ese bloque es
# el AD, y solo vale si su encabezado DECLARA el AD esperado:
#
#   2024-06-03 The Boeing Company: Amendment 39-22708; Docket No. FAA-...
#   98-02-51  Boeing: Amendment 39-10362. Docket 98-NM-04-AD.
#   Airbus: Amendment 39-10410. Docket 98-NM-22-AD.        (1998, sin número)
#
# Una cita en el cuerpo ("This AD replaces AD ...", la tabla "Affected ADs",
# una corrección que reproduce el corchete del preámbulo) no declara nada: es
# el error de identidad de 2026-07-28 (CLAUDE.md) pero con el texto en vez del
# número. Sin encabezado propio el intento es `unanchored`, aunque el número
# aparezca citado.

# Identidad en el corchete del preámbulo: "[Docket No. ...; Amendment
# 39-10410; AD 98-06-27]". La usa el respaldo de identidad de
# federal_register (docket_ids sin AD) y, aquí, el encabezado regulatorio que
# no trae número propio o lo trae mal escrito ("2005-NM-03-11 Boeing:").
FT_HEADER_AD_RE = re.compile(
    r"Amendment\s*(?:No\.?\s*)?39[-–—]\d{3,6}\s*[;,]?\s*"
    r"(?:AD|A\.D\.)?\s*((?:\d{4}|\d{2})[-–—]\d{2}[-–—]\d{2,4}(?:R\d+)?)\b",
    re.I,
)


def header_ad_for_amendment(txt: str, amendment: str) -> str | None:
    """AD que el corchete del preámbulo empareja con `amendment`, o None."""
    if not amendment:
        return None
    pat = re.compile(
        re.escape(amendment).replace(r"\-", "[-–—]")
        + r"\s*[;,]?\s*(?:AD|A\.D\.)?\s*((?:\d{4}|\d{2})[-–—]\d{2}[-–—]\d{2,4}(?:R\d+)?)\b",
        re.I,
    )
    m = pat.search(txt)
    return normalize_ad_number(m.group(1)) if m else None


# Encabezado del bloque regulatorio: al inicio de línea, número (opcional en
# 1998), titular hasta ":" (puede ocupar varias líneas: "Honeywell
# International Inc. (formerly ...; and\nAiResearch ...): Amendment") y el
# amendment, a veces tras el docket ("Rolls-Royce plc: Docket No. FAA
# 2017-1117; Amendment 39-19112"). El titular puede traer corchetes
# ("R[eacute]gional"), pero una línea que empieza con "["
# o con comillas (el corchete del preámbulo, una corrección que lo cita) no
# es un encabezado.
_BLOCK_HEADER_RE = re.compile(
    r"^(?:(?P<ad>\d{2,4}[-–—][\dA-Z]{2,4}[-–—]\d{2}(?:[-–—]\d{2})?(?:\s?R\d+)?)[ \t]+)?"
    r"(?P<holder>[A-Za-z][^\n:`]{0,200}(?:\n(?![ \t]*\n)[^\n:`]{1,150}){0,3})"
    r":\s*(?:Docket[^:\n]{0,60}?;\s*)?Amendment\s+(?:No\.?\s*)?(?P<amendment>39\s*[-–—]\s*\d{3,6})",
    re.M,
)
# Fin del bloque: la firma ("Issued in Renton, Washington, on ...", "Issued
# on May 2, 2024.") o el pie del documento FR.
_BLOCK_END_RE = re.compile(r"^[ \t]*(?:Issued\s+(?:in|on)\b|\[FR\s+Doc\.)", re.M | re.I)
_HEADER_END_RE = re.compile(r"\n(?=[ \t]*\n|[ \t]+\S|\*[ \t]*\*)")
_PAGE_MARK_RE = re.compile(r"^[ \t]*\[\[Page \d+\]\][ \t]*\n?", re.M)
_HTML_TAG_RE = re.compile(r"<[^>]+>")

# Tipo de sección por encabezado y por contenido: una sola tabla. El orden
# importa: "Alternative Methods of Compliance" es amoc antes que compliance, y
# "Actions and Compliance" es required_actions. El patrón de contenido mira el
# principio del cuerpo (los párrafos legados no tienen encabezado propio).
# required_actions por contenido solo vale después de compliance o de la
# condición insegura: antes, un párrafo que empieza con "Within ..." no es una
# acción requerida.
SECTION_KINDS = {
    "effective_date": (
        r"effective date",
        r"^this (?:ad|amendment|airworthiness directive)\b[^.]{0,40}?\b(?:becomes|is) effective",
    ),
    "affected_ads": (
        r"affected ads?\b|ads? affected",
        r"^this ad (?:supersedes|replaces|revises) (?:ad|airworthiness directive|emergency ad)\b",
    ),
    "applicability": (r"applicab", r"^this ad (?:applies to|affects)\b"),
    "subject": (r"^subject\b", r"^(?:air transport association|joint aircraft system)"),
    "unsafe_condition": (
        r"unsafe condition|^reason\b",
        r"^(?:this ad (?:was prompted|results from)|to (?:prevent|detect|correct|ensure|minimize"
        r"|reduce|avoid|preclude)\b)",
    ),
    "amoc": (r"alternative method", r"alternative methods? of compliance|different method of compliance"),
    "compliance": (
        r"^compliance\b(?! and)|compliance times?",
        r"^(?:comply with this ad|required as indicated|you are responsible for having the actions)",
    ),
    "material": (
        r"incorporation by reference|material incorporated",
        r"incorporation by reference|shall be (?:done|accomplished) in accordance with",
    ),
    "related_information": (r"related information|additional information", None),
    "required_actions": (
        r"required actions?|^requirements?\b|\bactions?\b|inspections?\b|\btests?\b|\bchecks?\b"
        r"|replacement|corrective|modification|terminating",
        r"^(?:within|before|prior to|at the|upon|after|when|thereafter|if\b)|\b(?:inspect|replace"
        r"|perform|do|modify|remove|install|insert|accomplish|revise|repair)\b",
    ),
}
_KIND_RES = {
    kind: (re.compile(h, re.I), re.compile(c, re.I) if c else None)
    for kind, (h, c) in SECTION_KINDS.items()
}

# Encabezado con letra (formato >= 2007): "(c) Applicability" solo en su
# línea (o en hasta tres: "(h) Initial and Repetitive Inspections of ... at
# the\nForward Keel Beam Attachment Joint"), seguido de una línea en blanco o
# sangrada y sin puntuación final (un párrafo legado de una línea, "(d)
# Special flight permits will not be issued.", no lo es).
_LETTER_HEADING_RE = re.compile(
    r"^[ \t]*\(([a-z])\)[ \t]+([A-Z][^\n]{0,150}?(?:\n(?!\([a-z]\))[A-Za-z(][^\n]{0,150}?){0,2})"
    r"(?<![.:;,])[ \t]*\n"
    r"(?=[ \t]*\n|[ \t]+\S)",
    re.M,
)
# Encabezado sin letra (formato 2000-2007): "Unsafe Condition" solo en su
# línea, entre líneas en blanco; los párrafos "(d) ..." van debajo. En 2005
# también en pregunta: "What Is the Unsafe Condition Presented in This AD?".
_PLAIN_HEADING_RE = re.compile(r"^[A-Z][A-Za-z0-9 ,&()/'\-]{1,100}\??$")
# Párrafo legado: "    Applicability: ...", "    (a) Within 5 flight cycles ...".
# La etiqueta también va sola en su línea ("Applicability", rotorcraft 2007).
_LABEL_RE = re.compile(r"^(Applicability|Compliance)(?::\s*|\s*$)")
_LETTER_PARA_RE = re.compile(r"^\(([a-z])\)(?:\s+|(?=\(\d))")
_TABLE_LINE_RE = re.compile(r"-{8,}|\.{6,}|\S {3,}\S")
_CLOSING_KINDS = ("amoc", "material", "effective_date")
_EMBEDDED_UNSAFE_RE = re.compile(r"(?<=\.)\s+(?=To (?:prevent|detect|correct|ensure|minimize|reduce|avoid|preclude)\b)")


def fr_document_text(html_text: str | None) -> str:
    """Texto plano del granulo de GovInfo: sin etiquetas, entidades resueltas
    y sin las marcas de página "[[Page 27375]]" que cortan los párrafos."""
    text = html.unescape(_HTML_TAG_RE.sub("", html_text or "")).replace("\r\n", "\n")
    return _PAGE_MARK_RE.sub("", text)


def _ad_key(ad: str | None) -> str:
    return re.sub(r"\s+", "", normalize_ad_number(ad or "")).upper()


def _amendment_key(amendment: str | None) -> str:
    return re.sub(r"\s+", "", (amendment or "").replace("–", "-").replace("—", "-"))


def _declared_ad(m: "re.Match", text: str) -> str:
    """AD que declara un encabezado: su número, o el que el preámbulo empareja
    con su amendment cuando no trae uno bien formado."""
    ad = _ad_key(m.group("ad"))
    if _FAA_NUMBER.match(ad):
        return ad
    return _ad_key(header_ad_for_amendment(text, _amendment_key(m.group("amendment"))))


def _header_end(text: str, m: "re.Match") -> int:
    """Fin del párrafo del encabezado ("...; Docket No. FAA-2023-1400;
    Project Identifier AD-2022-01374-T."): la línea en blanco, una línea
    sangrada o la elisión "* * *" que le siga."""
    end = _HEADER_END_RE.search(text, m.end())
    return end.end() if end else len(text)


def _is_excerpt(text: str, m: "re.Match") -> bool:
    """Un encabezado entre elisiones "* * * * *" es la cita de una corrección
    ("... is corrected to read as follows: * * * <encabezado> * * *"): nombra
    al AD pero no trae su texto."""
    return text[_header_end(text, m):].lstrip().startswith("*")


def _paragraph_text(lines: list[str]) -> str:
    """Un párrafo en una línea; las tablas conservan sus líneas."""
    if any(_TABLE_LINE_RE.search(ln) for ln in lines[1:]):
        return "\n".join(ln.rstrip() for ln in lines).strip("\n")
    text = ""
    for ln in lines:
        ln = ln.strip()
        if not ln:
            continue
        # "FAA-\n2023-1400": el guion del corte es parte de la palabra.
        text += ln if text.endswith("-") or not text else " " + ln
    return text


def _paragraphs(block: str) -> list[tuple[str, bool]]:
    """(texto, es_encabezado_sin_letra) por párrafo. Un párrafo empieza en una
    línea sangrada o tras una línea en blanco; las continuaciones van en la
    columna 0."""
    lines = block.split("\n")
    out: list[tuple[str, bool]] = []
    cur: list[str] = []

    def flush():
        if cur:
            out.append((_paragraph_text(cur), False))
            cur.clear()

    for i, ln in enumerate(lines):
        if not ln.strip():
            flush()
            continue
        prev_blank = i == 0 or not lines[i - 1].strip()
        next_blank = i + 1 >= len(lines) or not lines[i + 1].strip()
        if prev_blank and next_blank and _PLAIN_HEADING_RE.match(ln.rstrip()):
            flush()
            out.append((ln.strip(), True))
            continue
        if ln[:1].isspace() and cur and not _TABLE_LINE_RE.search(ln):
            flush()
        cur.append(ln)
    flush()
    return out


def _lead(text: str) -> str:
    """Encabezado de un párrafo legado: hasta el primer '.' o ':' de fin de
    frase, 120 caracteres como mucho."""
    m = re.match(r"(.{1,120}?)[.:](?:\s|$)", text)
    lead = m.group(1) if m else text if len(text) <= 120 else text[:120].rsplit(" ", 1)[0]
    return lead.strip()


def _raw_sections(block: str) -> list[dict]:
    """[{letter, heading, body}] del bloque, en orden, según su formato."""
    # 1) >= 2007: "(a) Effective Date" ... en secuencia desde (a). La
    # secuencia descarta continuaciones que empiezan con "(h) of this AD".
    headings, expected = [], "a"
    for m in _LETTER_HEADING_RE.finditer(block):
        if m.group(1) == expected:
            headings.append(m)
            expected = chr(ord(expected) + 1)
    if len(headings) >= 2:
        out = []
        for i, m in enumerate(headings):
            end = headings[i + 1].start() if i + 1 < len(headings) else len(block)
            body = "\n\n".join(t for t, _ in _paragraphs(block[m.end():end]))
            out.append({"letter": m.group(1), "heading": " ".join(m.group(2).split()), "body": body})
        return out

    # 2) Hasta 2007, por párrafos:
    #   - legado: "Applicability:", "Compliance:", la frase "To prevent ...,
    #     accomplish the following:" y cada párrafo (a), (b), ... es una sección
    #     cuyo encabezado es su propia frase inicial (`lead`: el tipo sale solo
    #     del contenido);
    #   - 2000-2007: además, encabezados sin letra ("Unsafe Condition") que
    #     agrupan los párrafos con letra que les siguen.
    # Las notas y los subpárrafos (1), (2) van con la sección en curso. Los
    # encabezados sin letra cuentan solo si al menos dos van seguidos de un
    # párrafo con letra: un listado de modelos legado también tiene líneas
    # sueltas ("General Aviation Airplanes").
    paras = _paragraphs(block)
    use_headings = sum(
        1 for (_, is_heading), (nxt, _) in zip(paras, paras[1:])
        if is_heading and _LETTER_PARA_RE.match(nxt)
    ) >= 2
    out, expected = [], "a"
    for text, is_heading in paras:
        label = _LABEL_RE.match(text)
        letter = _LETTER_PARA_RE.match(text)
        in_heading = bool(out) and out[-1].get("grouped", False)
        if is_heading and use_headings:
            out.append({"letter": None, "heading": text, "body": "", "grouped": True})
        elif label:
            out.append({"letter": None, "heading": label.group(1), "body": text[label.end():]})
        elif letter and letter.group(1) == expected:
            expected = chr(ord(expected) + 1)
            # Un encabezado agrupa sus párrafos, pero el AMOC, la incorporación
            # por referencia y la fecha efectiva que le siguen sin encabezado
            # propio son secciones aparte ("New Requirements of This AD" y
            # debajo, al final, "(e) An alternative method of compliance...").
            if in_heading and out[-1]["letter"] and _section_kind(None, text, False) in _CLOSING_KINDS:
                in_heading = False
            if in_heading:
                out[-1]["letter"] = out[-1]["letter"] or letter.group(1)
                out[-1]["body"] = f"{out[-1]['body']}\n\n{text}".strip()
            else:
                out.append({"letter": letter.group(1), "heading": _lead(text[letter.end():]),
                            "body": text, "lead": True})
        elif _KIND_RES["unsafe_condition"][1].match(text) and not in_heading:
            out.append({"letter": None, "heading": _lead(text), "body": text, "lead": True})
        elif out:
            out[-1]["body"] = f"{out[-1]['body']}\n\n{text}".strip()
    # "Compliance: Required as indicated, unless already done. To prevent ...,
    # accomplish the following:" en un solo párrafo: la condición insegura es
    # su propia sección.
    for i, sec in enumerate(out):
        split = sec["heading"] == "Compliance" and _EMBEDDED_UNSAFE_RE.search(sec["body"])
        if split:
            unsafe = sec["body"][split.end():]
            sec["body"] = sec["body"][:split.start()]
            out.insert(i + 1, {"letter": None, "heading": _lead(unsafe), "body": unsafe, "lead": True})
            break
    return out


def _section_kind(heading: str | None, body: str, after_compliance: bool) -> str:
    """Tipo por encabezado y, si no lo da (o es la frase inicial de un párrafo
    legado, `heading=None`), por el principio del cuerpo."""
    for kind, (heading_re, _) in _KIND_RES.items():
        if heading and heading_re.search(heading):
            return kind
    lead = _LETTER_PARA_RE.sub("", body[:300], count=1)
    for kind, (_, content_re) in _KIND_RES.items():
        if content_re is None or (kind == "required_actions" and not after_compliance):
            continue
        if content_re.search(lead):
            return kind
    return "other"


def split_ad_sections(html_text: str | None, expected_ad: str | None,
                      expected_amendment: str | None = None) -> dict:
    """
    Secciones del AD `expected_ad` dentro de un granulo de GovInfo.

    Devuelve {status, anchor, sections[]}; cada sección es {position, letter,
    heading, kind, body} con `position` desde 1 en el orden del documento y
    `kind` de SECTION_KINDS (o 'other'). `status`:

    - extracted: bloque anclado con al menos una sección.
    - unanchored: ningún encabezado regulatorio declara `expected_ad`, o el
      que lo declara trae otro amendment que `expected_amendment`. Es
      determinista: el mismo documento da el mismo resultado.
    - empty: respuesta vacía o la página "request access" de GovInfo.
    - error: bloque anclado del que no sale ninguna sección, o el parseo falló.

    Nunca decide si el texto está completo: eso lo decide la base con una
    sola regla sobre los `kind` enviados.
    """
    result = {"status": "error", "anchor": None, "sections": []}
    try:
        if not (html_text or "").strip() or "request access" in html_text[:600].lower():
            result["status"] = "empty"
            return result
        text = fr_document_text(html_text)
        headers = list(_BLOCK_HEADER_RE.finditer(text))
        want = _ad_key(expected_ad)
        idx = next((i for i, m in enumerate(headers)
                    if want and _declared_ad(m, text) == want and not _is_excerpt(text, m)), None)
        if idx is None:
            result["status"] = "unanchored"
            return result
        head = headers[idx]
        if expected_amendment and _amendment_key(head.group("amendment")) != _amendment_key(expected_amendment):
            result["status"] = "unanchored"
            return result

        body_start = _header_end(text, head)
        ends = [len(text)]
        if idx + 1 < len(headers):
            ends.append(headers[idx + 1].start())
        stop = _BLOCK_END_RE.search(text, body_start)
        if stop:
            ends.append(stop.start())
        block = text[body_start:min(ends)]
        result["anchor"] = re.sub(r"\s+", " ", text[head.start():body_start]).strip()

        sections, after_compliance = [], False
        for raw in _raw_sections(block):
            body = raw["body"].strip()
            if not body:
                continue
            kind = _section_kind(None if raw.get("lead") else raw["heading"], body, after_compliance)
            after_compliance = after_compliance or kind in ("compliance", "unsafe_condition")
            sections.append({"position": len(sections) + 1, "letter": raw["letter"],
                             "heading": raw["heading"], "kind": kind, "body": body})
        result["sections"] = sections
        result["status"] = "extracted" if sections else "error"
    except Exception:  # un documento raro no tumba la corrida
        result.update(status="error", anchor=None, sections=[])
    return result
