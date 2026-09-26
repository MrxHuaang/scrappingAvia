"""
Applicability de los ADs de la FAA (Federal Register y GovInfo): titular y
modelos a partir del título y del abstract.

Única implementación para las dos fuentes FAA. Produce la forma que consume
`populate_db._applicability_pairs()`:

    raw_data["Applicability"] = [{"make": <titular>, "models": [<modelo>, ...]}]

Reglas (docs/applicability-contract.md):

* El TITULAR sale del título ("Airworthiness Directives; <Titular> Airplanes |
  Helicopters | Turbofan Engines | Propellers | ..."). Si el título dice "Type
  Certificate Previously Held by X" o "(Formerly X)", el titular es el ACTUAL y
  X queda en `previously_held_by` (informativo: `_applicability_pairs` lo
  ignora). Un título sin titular inequívoco ("Various Aircraft", "X and Various
  Restricted Category Helicopters", dos fabricantes unidos por "and") no emite
  `Applicability` ni `Make`: el AD viaja `failed` y sus filas guardadas no se
  tocan. Declarar solo uno de varios titulares sería un subconjunto parcial.
* Los MODELOS salen del abstract ("for all/certain <Titular> Model A, B, and C
  airplanes"), y del título cuando el abstract no los trae (títulos legados
  "Boeing Model 747 Series Airplanes", GovInfo sin abstract). Sin modelo
  extraíble la fila va sin modelos (`model_status=unknown`): nunca "aplica a
  todo".
* Los modelos se conservan como los escribe la fuente ("737-600", "-700",
  "747 series"). La tokenización, las subseries y los alias de fabricante los
  resuelve la base (`ad_model_tokens`, `ad_make_canonical`), no este módulo.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

__all__ = ["FRApplicability", "extract_applicability", "applicability_fields"]


# ---------------------------------------------------------------------------
# Vocabulario
# ---------------------------------------------------------------------------
# Sustantivo de producto con el que la FAA cierra la lista de modelos. El
# lookbehind evita "Turbo-Propeller" (el guion no es un límite válido aquí).
_NOUN = (
    r"(?<![\w-])(?:airplanes?|aircraft|helicopters?|rotorcraft|gliders?|motorgliders?"
    r"|sailplanes?|airships?|balloons?|gyroplanes?|engines?|propellers?|appliances?)\b"
)
# Calificativos del motor/hélice que van entre la lista y el sustantivo.
_ADJ = (
    r"(?:series|model|turbofan|turbojet|turboprop|turboshaft|reciprocating|piston"
    r"|turbine|gas|controllable|fixed-pitch|five-bladed|four-bladed|three-bladed"
    r"|two-bladed|variable-pitch|constant-speed)"
)
_NOUN_RE = re.compile(_NOUN, re.I)
# Frase de producto al FINAL del título: "... Series Turboprop and Turboshaft Engines".
_TRAILING_NOUN_PHRASE_RE = re.compile(
    rf"(?:[\s,]+(?:{_ADJ}|and))*\s+{_NOUN}\W*$", re.I
)
# "Model"/"Models" con mayúscula: en el título y en el abstract introduce la
# lista. La minúscula ("... model turbofan engines") es un calificativo.
_MODEL_KW_RE = re.compile(r"\bModels?\b")
# Frases que califican al producto y cortan el titular: "Airplanes Equipped
# With ...", "Units Approved Under TSO ...; Installed on ...".
_QUALIFIER_RE = re.compile(
    r"\s+(?:equipped|powered|fitted|modified|approved|installed|including|having"
    r"|certificated|excluding)\b",
    re.I,
)
# Sufijos societarios: el titular termina en el último que aparece antes de
# la lista de modelos ("Rolls-Royce plc RB211 Trent 768-60" -> "Rolls-Royce plc").
_SUFFIX_RE = re.compile(
    r"(?<![\w.])(?:Inc\.?|Incorporated|Corp\.?|Corporation|Company|Co\.|Ltd\.?|Limited"
    r"|LLC|L\.L\.C\.|GmbH|AG|KG|S\.A\.S\.?|SAS|S\.A\.|plc|PLC|S\.p\.[Aa]\.?|SpA|srl|S\.r\.l\."
    r"|s\.r\.o\.|a\.s\.|AB|ULC|LP|Partnership|N\.V\.|B\.V\.|Oy|Division)(?![\w])"
)
_PREVIOUS_RE = re.compile(
    r"^\(\s*(?:Type\s+Certificate\s+)?(?:Previously\s+Held\s+by|Previously|Formerly(?:\s+Known\s+as)?)\s+",
    re.I,
)
# Palabras de prosa: si aparecen dentro de una "lista" del abstract, lo que se
# capturó es una frase y no una enumeración de modelos.
_PROSE_RE = re.compile(
    r"\b(?:that|which|who|with|equipped|installed|having|except|by|to|is|are|was|were"
    r"|requires?|prompted|this|the|on|in|of|for)\b"
)
# Fin de oración dentro del abstract ("Ltd. Model" NO lo es).
_SENTENCE_END_RE = re.compile(
    r"\.\s+(?:This|The|We|That|These|Those|AD|As|In|Since|After|Additionally|Also|It|There|For)\b"
)
_JOINERS = {"and", "or", "&", "series", "model", "models", "mark", "mk.", "variant", ","}
_ROMAN_RE = re.compile(r"^[IVX]+,?$")
_GENERIC_WORDS = {"the", "and", "of", "de", "company", "inc", "inc.", "corporation", "limited", "ltd", "ltd."}
_MAX_LIST_CHARS = 700


@dataclass(frozen=True)
class FRApplicability:
    """Resultado de la extracción. `holder` None = sin titular inequívoco."""

    holder: str | None
    models: tuple[str, ...] = ()
    previously_held_by: tuple[str, ...] = field(default_factory=tuple)

    def raw_applicability(self) -> list[dict] | None:
        """La forma `[{make, models[]}]` de raw_data, o None si no hay titular."""
        if not self.holder:
            return None
        item: dict = {"make": self.holder, "models": list(self.models)}
        if self.previously_held_by:
            item["previously_held_by"] = list(self.previously_held_by)
        return [item]


# ---------------------------------------------------------------------------
# Helpers de texto
# ---------------------------------------------------------------------------
def _mask_parens(s: str) -> str:
    """Misma longitud que `s`, con cada paréntesis balanceado (y su contenido)
    reemplazado por '\\x01'. Los regex sobre la versión enmascarada no ven
    comas, "and" ni sustantivos internos ("(Regional Jet Series 700, 701 & 702)").
    Un '(' sin cierre se deja tal cual."""
    out = list(s)
    stack: list[int] = []
    for i, ch in enumerate(s):
        if ch == "(":
            stack.append(i)
        elif ch == ")" and stack:
            start = stack.pop()
            if not stack:
                for j in range(start, i + 1):
                    out[j] = "\x01"
    return "".join(out)


def _paren_end(s: str, i: int) -> int:
    """Índice tras el ')' que cierra el '(' en `s[i]`, o -1 si no cierra."""
    depth = 0
    for j in range(i, len(s)):
        if s[j] == "(":
            depth += 1
        elif s[j] == ")":
            depth -= 1
            if depth == 0:
                return j + 1
    return -1


def _clean_spaces(s: str) -> str:
    # Cortes de línea del FR: "BN- 2A", "747- 400F", "and - 145EP".
    s = re.sub(r"(?<=\w)-\s+(?=\w)", "-", s)
    s = re.sub(r"(?<![\w])-\s+(?=\d)", "-", s)
    return re.sub(r"\s+", " ", s).strip()


def _split_items(region: str) -> list[str]:
    """Parte una lista de modelos por comas, ';', 'and' y 'or' fuera de paréntesis."""
    masked = _mask_parens(region)
    cuts = [(m.start(), m.end()) for m in re.finditer(r"\s*[,;]\s*|\s+(?:and|or)\s+", masked)]
    items, prev = [], 0
    for a, b in cuts:
        items.append(region[prev:a])
        prev = b
    items.append(region[prev:])
    return items


def _clean_item(item: str, trim_words: bool) -> str:
    item = _clean_spaces(item)
    item = re.sub(r"^(?:(?:and|or|all|certain|Models?)\s+)+", "", item)
    while True:
        new = re.sub(
            r"\s+(?:models?|turbofan|turbojet|turboprop|turboshaft|reciprocating|piston|controllable)$",
            "", item, flags=re.I,
        )
        if new == item:
            break
        item = new
    if trim_words:
        # Lista sin sustantivo de producto (appliance: "Model 602 Smoke
        # Detectors"): se queda con las palabras hasta el último token con número.
        # Un ítem que no arranca con una designación ("Part Numbers 065-...")
        # ya no es un modelo.
        words = item.split(" ")
        if not (re.search(r"\d", words[0]) or words[0].isupper()):
            return ""
        while len(words) > 1 and not re.search(r"\d", words[-1]):
            words.pop()
        item = " ".join(words)
    item = item.strip(" ,;.")
    if not re.search(r"[A-Za-z0-9]", item) or item.lower() in {"series", "model", "models", "all"}:
        return ""
    return item


def _dedupe(items) -> tuple[str, ...]:
    return tuple(dict.fromkeys(i for i in items if i))


# ---------------------------------------------------------------------------
# Lista de modelos a partir de una posición
# ---------------------------------------------------------------------------
_TRAILING_ADJ_RE = re.compile(
    r"(?:\s+(?:model|turbofan|turbojet|turboprop|turboshaft|reciprocating|piston|controllable"
    r"|gas|turbine|aircraft))+\s*$",
    re.I,
)


def _parse_models(text: str, pos: int, *, prose: bool, allow_no_noun: bool) -> tuple[str, ...]:
    """
    Modelos listados en `text` desde `pos`: "[Model(s)] A, B, and C [series]
    <sustantivo>", encadenando "; and Model D <sustantivo>". `prose=True`
    (abstract) corta en fin de oración y rechaza capturas con palabras de prosa.
    `allow_no_noun` (títulos de appliance) acepta la lista hasta el final.
    """
    masked = _mask_parens(text)
    limit = len(text)
    if prose:
        m_end = _SENTENCE_END_RE.search(masked, pos)
        if m_end:
            limit = m_end.start()
        limit = min(limit, pos + _MAX_LIST_CHARS)

    regions: list[tuple[str, bool]] = []
    p = pos
    first = True
    while p < limit:
        m_kw = re.match(r"[\s,]*(?:(?:all|certain)\s+)?([Mm]odels?\s+)?", masked[p:limit])
        start = p + m_kw.end()
        has_kw = bool(m_kw.group(1))
        lead = text[start:limit].split(None, 3)[:3]
        if not lead:
            break
        # Sin "Model", la lista tiene que arrancar con una designación: un
        # número (o romano) en los tres primeros tokens ("PW4164", "Arriel 1B",
        # "Jetstream Series 3101", "Turmo IV A").
        if not has_kw and not any(re.search(r"\d", t) or _ROMAN_RE.match(t) for t in lead):
            break
        m_noun = _NOUN_RE.search(masked, start, limit)
        if not m_noun:
            if allow_no_noun and first:
                regions.append((text[start:limit], True))
            break
        region = text[start:m_noun.start()]
        if prose and _PROSE_RE.search(_mask_parens(region)):
            break
        if prose:
            # "... and Drinks Can hot air balloons": las palabras en minúscula
            # antes del sustantivo lo califican; "series" y las letras sueltas
            # ("Discus b") sí son parte de la designación.
            region = re.sub(r"(?:\s+(?!series\b)[a-z]{3,})+\s*$", "", region)
        regions.append((region, False))
        first = False
        p = m_noun.end()
        cont = re.match(r"\s*[,;]?\s*(?:and\s+)?", masked[p:limit])
        nxt = masked[p + cont.end():limit]
        if not (cont.group(0).strip() and (re.match(r"(?:(?:all|certain)\s+)?Models?\s", nxt)
                                           or re.match(r"\S*\d", nxt))):
            break
        p += cont.end()

    items: list[str] = []
    for region, trim in regions:
        region = _TRAILING_ADJ_RE.sub("", region)
        items.extend(_clean_item(i, trim) for i in _split_items(region))
    return _dedupe(items)


# ---------------------------------------------------------------------------
# Titular desde el título
# ---------------------------------------------------------------------------
def _subject(title: str) -> str:
    # "\w*worthiness": la FR API trae al menos un título con la "A" comida.
    s = re.sub(r"^\s*\w*worthiness\s+Directives?\s*[;:,]?\s*", "", title or "", flags=re.I)
    s = re.sub(r"\s*;\s*(?:Correction|Correcting\s+Amendment)\.?\s*$", "", s, flags=re.I)
    return _clean_spaces(s).rstrip(". ")


def _pull_previous_holders(seg: str) -> tuple[str, list[str], bool]:
    """Quita los paréntesis "(Type Certificate Previously Held by ...)" /
    "(Formerly ...)" del segmento. Devuelve (segmento, titulares previos,
    el paréntesis cerraba el segmento)."""
    previous: list[str] = []
    at_end = False
    i = 0
    while True:
        i = seg.find("(", i)
        if i < 0:
            break
        end = _paren_end(seg, i)
        if end < 0:
            break
        group = seg[i:end]
        m = _PREVIOUS_RE.match(group)
        if not m:
            i = end
            continue
        inner = group[m.end():-1]
        for part in re.split(r";|,\s*Formerly(?:\s+Known\s+as)?\s+", inner, flags=re.I):
            part = part.strip(" ,;")
            if part:
                previous.append(part)
        at_end = not seg[end:].strip()
        seg = (seg[:i].rstrip() + " " + seg[end:].lstrip()).strip()
    return seg, previous, at_end


def _model_ish(tok: str) -> bool:
    t = tok.strip(",;")
    return (
        bool(re.search(r"\d", tok))
        or t.lower() in _JOINERS
        or re.fullmatch(_ADJ, t, re.I) is not None
        or bool(_ROMAN_RE.match(t))
        or bool(re.fullmatch(r"[A-Z]", t))
        or t.startswith("-")
        or tok == ","
    )


def _split_holder_models(pre: str) -> tuple[str, int | None]:
    """Separa "<titular> <lista>" cuando el título no trae "Model" (motores
    legados: "Pratt & Whitney PW4164, PW4168 Series"). Devuelve (titular, índice
    en `pre` donde empieza la lista o None)."""
    masked = _mask_parens(pre)
    # 1) Último sufijo societario o sigla entre paréntesis seguido de algo con número.
    anchors = [m.end() for m in _SUFFIX_RE.finditer(masked)]
    for m in re.finditer(r"\x01+", masked):
        group = pre[m.start():m.end()]
        # Sigla suelta ("(IAE)", "(SMA)"), no un prefijo pegado ("(L)O-360").
        standalone = (m.start() == 0 or pre[m.start() - 1].isspace()) and (
            m.end() == len(pre) or pre[m.end()] in " ,"
        )
        if standalone and len(group) <= 27 and not re.search(r"\d", group):
            anchors.append(m.end())
    if anchors:
        a = max(anchors)
        rest = pre[a:]
        if re.search(r"\d", rest):
            return pre[:a].strip(" ,"), a
        # Lo que sigue al sufijo sin ningún número describe el producto
        # ("Lindstrand Balloons Ltd. Hot Air Balloons"), no es parte del nombre.
        return pre[:a].strip(" ,"), None
    # 2) Consumo hacia atrás de tokens con forma de modelo.
    toks = [(m.start(), m.group(0)) for m in re.finditer(r"\S+", masked)]
    k = len(toks)
    while k > 1:
        start, tok_m = toks[k - 1]
        tok = pre[start:start + len(tok_m)]
        if "\x01" in tok_m:
            left = pre[toks[k - 2][0]:toks[k - 2][0] + len(toks[k - 2][1])]
            if not re.search(r"\d", left):
                break
        elif not _model_ish(tok):
            break
        k -= 1
    consumed = [pre[s:s + len(t)] for s, t in toks[k:]]
    if k == len(toks) or not any(re.search(r"\d", c) or _ROMAN_RE.match(c) for c in consumed):
        return pre.strip(" ,"), None
    # "Turbomeca Arriel 1B, 1D": la familia ("Arriel") va con los modelos si la
    # lista arranca con un número pelado y a la izquierda queda un titular.
    first = consumed[0].strip(",")
    left = pre[toks[k - 1][0]:toks[k - 1][0] + len(toks[k - 1][1])]
    rest = pre[:toks[k - 1][0]]
    if (re.match(r"^(?:\d|[IVX]+$)", first) and not _SUFFIX_RE.fullmatch(left)
            and re.search(r"[A-Za-z]{3}", rest)):
        k -= 1
    return pre[:toks[k][0]].strip(" ,"), toks[k][0]


def _longest_prefix_in(candidate: str, abstract: str) -> str | None:
    """Prefijo de palabras más largo de `candidate` que aparece tal cual (con
    mayúsculas) en el abstract. Titulares de appliance sin sufijo: "Zodiac
    Aerotechnics Oxygen Mask Regulators" -> "Zodiac Aerotechnics"."""
    words = candidate.split()
    for n in range(len(words), 0, -1):
        prefix = " ".join(words[:n]).strip(" ,")
        if not any(len(w) >= 3 and w.lower() not in _GENERIC_WORDS for w in prefix.split()):
            continue
        if re.search(rf"(?<![\w-]){re.escape(_fold(prefix))}(?![\w-])", abstract):
            return prefix
    return None


def _holder_from_title(subject: str, abstract: str) -> tuple[str | None, tuple[str, ...], str, int | None, bool]:
    """(titular, titulares previos, texto de trabajo, índice en ese texto donde
    empieza la lista de modelos del título, la lista va sin sustantivo)."""
    # Texto de trabajo: sin los paréntesis de titular previo y cortado en el
    # primer calificativo ("Equipped With", "Installed on"). Conserva los
    # segmentos "; and Model ..." para la lista de modelos del título.
    work, previous, _ = _pull_previous_holders(subject)
    q = _QUALIFIER_RE.search(_mask_parens(work))
    if q:
        work = work[: q.start()]
    semi = re.search(r";", _mask_parens(work))
    seg = work[: semi.start()] if semi else work
    seg = seg.rstrip(" ,")
    # ¿El paréntesis de titular previo cerraba el primer segmento? ("Airbus
    # Helicopters (Previously Eurocopter France)": "Helicopters" es del nombre.)
    orig_semi = re.search(r";", _mask_parens(subject))
    _, _, prev_at_end = _pull_previous_holders(subject[: orig_semi.start()] if orig_semi else subject)

    if not seg or re.search(r"\bVarious\b", seg, re.I):
        return None, tuple(previous), work, None, False

    masked = _mask_parens(seg)
    holder: str | None
    models_at: int | None = None
    no_noun = False

    kw = _MODEL_KW_RE.search(masked)
    trailing = _TRAILING_NOUN_PHRASE_RE.search(masked)
    if kw:
        pre = seg[: kw.start()].rstrip(" ,")
        # "McCauley Propeller Systems Propeller Models ...": el sustantivo
        # singular pegado a "Models" no es parte del nombre.
        pre = re.sub(r"\s+(?:propeller|engine|appliance)$", "", pre, flags=re.I)
        holder, split = _split_holder_models(pre)
        models_at = split if split is not None else kw.start()
        no_noun = not _NOUN_RE.search(_mask_parens(work), kw.end())
    elif trailing and not prev_at_end:
        pre = seg[: trailing.start()].rstrip(" ,")
        # "Airbus Helicopters": el sustantivo es parte del nombre cuando el
        # abstract lo usa como titular ("Airbus Helicopters Model EC130T2").
        if abstract and re.search(rf"{re.escape(_fold(seg))}\s+(?:\(|Model)", abstract):
            pre = seg
        if not pre:
            return None, tuple(previous), work, None, False
        holder, models_at = _split_holder_models(pre)
    else:
        # Sin "Model" ni sustantivo de producto al final del primer segmento.
        # Si el segmento termina en una lista ("Dowty Propellers R321/4-82-F/8;
        # ...; and R334/4-82-F/13 Propellers"), el sustantivo está más adelante.
        # Si no (appliances, "Robinson Helicopter Company"), el titular termina
        # en el último sufijo societario (con su sigla, si la lleva) o, sin
        # sufijo, en el prefijo más largo que el abstract usa tal cual.
        holder, models_at = _split_holder_models(seg)
        if models_at is None:
            suffixes = list(_SUFFIX_RE.finditer(masked))
            if suffixes:
                end = suffixes[-1].end()
                abbr = re.match(r"\s*\x01+", masked[end:])
                holder = seg[: end + (abbr.end() if abbr else 0)].strip(" ,")
            elif abstract:
                holder = _longest_prefix_in(seg, abstract)
            else:
                holder = seg if prev_at_end else None

    if holder and re.search(r"\band\b", _mask_parens(holder)):
        # Dos titulares en el título: no se puede atribuir cada modelo al suyo.
        holder = None
    if holder and not re.search(r"[A-Za-z]{2}", holder):
        holder = None
    return holder, tuple(previous), work, models_at, no_noun


# ---------------------------------------------------------------------------
# Modelos desde el abstract
# ---------------------------------------------------------------------------
_FR_ENTITY_RE = re.compile(r"\[([A-Za-z])(?:acute|grave|circ|uml|tilde|cedil|ring)\]")
_QUOTES = str.maketrans({"“": '"', "”": '"', "‘": "'", "’": "'"})


def _fold(s: str) -> str:
    """Forma comparable de un texto: sin acentos ni comillas tipográficas, con
    las entidades del FR ("R[eacute]gional") resueltas y "--" como "-". El
    título trae "Régional" y el abstract "R[eacute]gional" o "ATR--GIE"."""
    s = _FR_ENTITY_RE.sub(r"\1", s or "").translate(_QUOTES).replace("--", "-")
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def _holder_variants(holder: str, previous: tuple[str, ...]) -> list[str]:
    """Formas con las que el abstract puede nombrar al titular, de la más
    específica a la menos: completo, sin sufijo societario, sus siglas y sus
    prefijos de palabras ("MHI RJ Aviation ULC" -> "MHI RJ"), y los titulares
    previos."""
    variants = [holder]
    bare = holder
    while True:
        new = re.sub(rf"[\s,]*(?:{_SUFFIX_RE.pattern})\s*$", "", bare).strip(" ,")
        new = re.sub(r"\s*\([^)]*\)\s*$", "", new).strip(" ,")
        if new == bare or not new:
            break
        bare = new
        variants.append(bare)
    variants += re.findall(r"\(([A-Za-z&.\- ]{2,25})\)", holder)
    words = bare.split()
    for n in range(len(words) - 1, 0, -1):
        prefix = " ".join(words[:n]).strip(" ,")
        if any(len(w) >= 3 and w.lower() not in _GENERIC_WORDS for w in prefix.split()):
            variants.append(prefix)
    variants += [re.sub(r"^The\s+", "", v) for v in variants if v.startswith("The ")]
    variants += list(previous)
    return list(dict.fromkeys(_fold(v) for v in variants if v and len(v) >= 2))


# Lo que puede ir entre el nombre del titular y su lista en el abstract:
# comas, sufijos societarios ("Airbus SAS"), siglas y paréntesis ("(RR)",
# "(type certificate previously held by ...)", "(now Bell Textron Inc.)").
_AFTER_HOLDER_RE = re.compile(rf"(?:[\s,]+(?:{_SUFFIX_RE.pattern}))+")
# Abstract que no repite al titular: "for all Model FAN JET FALCON ...".
_BARE_MODEL_RE = re.compile(r"\b(?:for|to)\s+(?:all|certain)\s+(?=Models?\s)")


def _skip_after_holder(text: str, p: int) -> int:
    while True:
        m = _AFTER_HOLDER_RE.match(text, p)
        if m:
            p = m.end()
            continue
        sk = re.match(r"[\s,]*\(", text[p:])
        if not sk:
            return p
        end = _paren_end(text, p + sk.end() - 1)
        if end < 0 or not (end == len(text) or text[end] in " ,"):
            return p
        p = end


def _models_from_abstract(abstract: str, holder: str, previous: tuple[str, ...]) -> tuple[str, ...]:
    if not abstract:
        return ()
    for variant in _holder_variants(holder, previous):
        for m in re.finditer(rf"(?<![\w-]){re.escape(variant)}(?![\w-])", abstract, re.I):
            models = _parse_models(abstract, _skip_after_holder(abstract, m.end()),
                                   prose=True, allow_no_noun=False)
            if models:
                return models
    m = _BARE_MODEL_RE.search(abstract)
    if m:
        return _parse_models(abstract, m.end(), prose=True, allow_no_noun=False)
    return ()


# ---------------------------------------------------------------------------
# API
# ---------------------------------------------------------------------------
def extract_applicability(title: str, abstract: str = "") -> FRApplicability:
    """Titular y modelos de un AD de la FAA a partir de su título y abstract."""
    subject = _subject(title)
    abstract = _clean_spaces(_fold(abstract or ""))
    holder, previous, work, models_at, no_noun = _holder_from_title(subject, abstract)

    # Un título legado que enumera modelos ("Boeing Model 747-100, 747-200B,
    # ... 747SP Series Airplanes") es más preciso que su abstract ("certain
    # Boeing Model 747 series airplanes"); el título moderno no trae modelos
    # ("The Boeing Company Airplanes") y ahí manda el abstract.
    models: tuple[str, ...] = ()
    if models_at is not None:
        models = _parse_models(work, models_at, prose=False, allow_no_noun=no_noun)
    if not models and holder:
        models = _models_from_abstract(abstract, holder, previous)
    return FRApplicability(holder=holder, models=models, previously_held_by=previous)


def applicability_fields(title: str, abstract: str = "") -> dict:
    """Campos del registro del scraper derivados de una sola extracción:
    `Make`/`Model` escalares (columnas `ads.make`/`ads.model`) y, si hay
    titular, `Applicability` para `ad_applicability`."""
    result = extract_applicability(title, abstract)
    out: dict = {"Make": result.holder or "", "Model": ", ".join(result.models)}
    applicability = result.raw_applicability()
    if applicability is not None:
        out["Applicability"] = applicability
    return out
