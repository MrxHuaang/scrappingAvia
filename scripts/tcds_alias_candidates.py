"""
Zephr - scripts/tcds_alias_candidates.py
Candidatos a alias de fabricante desde las hojas de datos de certificados de
tipo (TCDS) de FAA DRS, para que una persona los revise.

Lee el doctype TCDSMODEL de la API data-pull de DRS (un documento por hoja y
revision: unos 8.100, 11 paginas de 750) y junta cada nombre que aparece como
titular del certificado (`drs:tcdsmodelTCHolder`, `...TCHolderShortName`) o
como titular anterior (`drs:tcdsmodelFormerHolders`), con sus numeros de TCDS,
sus modelos y, para un titular anterior, quien tiene hoy esas hojas. Cada
nombre se compara con el seed de grupos de fabricantes de ad-scrapper
(`docs/data/manufacturer-groups.csv`, solo lectura):

- known:        el nombre ya es un alias de un grupo (misma clave normalizada);
- new_spelling: no es alias, pero comparte la marca con un grupo conocido
                ("Allison Engine Company Inc." frente al grupo de Allison), o
                es titular anterior de hojas cuyo titular actual ya esta en un
                grupo. Es la fila que mas interesa revisar;
- unknown:      nada en el seed lo relaciona.

El script NO modifica el seed ni escribe en ninguna base: escribe un CSV que
la persona revisa, y de ahi salen (o no) las filas nuevas del seed. Un nombre
parecido no es evidencia de grupo (docs/data/manufacturer-groups.md de
ad-scrapper): la columna `status` es una pista, no una decision.

Uso (la key sale de DRS_API_KEY y solo viaja en `x-api-key`; requiere
DRS_API_ENABLED, como la sync):
    python scripts/tcds_alias_candidates.py --out <ruta fuera del repo>.csv
    python scripts/tcds_alias_candidates.py --input tcds.json --out <ruta>.csv   # sin red
    python scripts/tcds_alias_candidates.py --dump tcds.json --out <ruta>.csv    # guarda lo leido
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import sys
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scrapers import faa_drs_api  # noqa: E402
from scrapers.faa_drs_ads import clean_makes  # noqa: E402

DOCTYPE = "TCDSMODEL"
#: Tope de paginas por corrida: 8.095 documentos medidos son 11 paginas. Si
#: TCDSMODEL crece por encima, el script falla en vez de seguir pidiendo.
MAX_PAGES = 12
DEFAULT_GROUPS = ROOT.parent / "Ad-Scrapper" / "docs" / "data" / "manufacturer-groups.csv"
#: Modelos y numeros de TCDS por fila del CSV (el resto se cuenta).
LIST_MAX = 15
_EMPTY = "_EMPTY_"
#: Campos de un documento TCDSMODEL que usa el reporte (y que guarda --dump).
FIELDS = ("documentGuid", "drs:documentNumber", "drs:status", "drs:tcdsmodelTCHolder",
          "drs:tcdsmodelTCHolderShortName", "drs:tcdsmodelFormerHolders", "drs:tcdsmodelModel",
          "drs:tcdsmodelProductType", "drs:tcdsmodelRevisionDate")

# Sufijos societarios y palabras que no distinguen a un fabricante: fuera de
# la clave con la que se compara un nombre ("The Boeing Company" == "Boeing").
_SUFFIXES = frozenset(
    "inc incorporated llc lp ltd limited corp corporation co company gmbh mbh sa sas sarl ag plc nv bv "
    "spa srl kg the as ab oy pty sl slu sau ohg".split())
# Palabras que no bastan como marca compartida: dos nombres que solo
# coinciden en una de estas no tienen nada que ver entre si.
_GENERIC = frozenset(
    "aero aerospace air aircraft airplane aviation avions american british canadian national general "
    "international industries industrie industrial manufacturing engineering engines engine helicopter "
    "helicopters propeller propellers products systems technologies technology group holdings division "
    "motors power turbine turbines united western eastern northern southern pacific atlantic flight "
    "company de del la le of and for jet jets sales service services societe societa sociedad anonyme "
    "compagnie construcciones costruzioni fabrica fabrique".split())
_STATUS_ORDER = {"new_spelling": 0, "unknown": 1, "known": 2}


def name_key(name: str | None) -> str:
    """Clave de comparacion: sin acentos, sin puntuacion, sin sufijos
    societarios, en minusculas ("Messerschmitt-Bölkow-Blohm GmbH" ->
    "messerschmitt bolkow blohm")."""
    text = unicodedata.normalize("NFKD", name or "")
    text = "".join(c for c in text if not unicodedata.combining(c)).lower().replace("&", " and ")
    # Las siglas con puntos son una palabra: "S.p.A." -> "spa", "G.m.b.H." -> "gmbh".
    text = text.replace(".", "")
    words = re.sub(r"[^a-z0-9]+", " ", text).split()
    return " ".join(w for w in words if w not in _SUFFIXES)


def brand(key: str) -> str | None:
    """La marca de una clave: su primera palabra, si distingue a alguien
    ("allison engine" -> "allison"; "helicopter transport services" -> None,
    porque empieza por una palabra generica)."""
    words = key.split()
    if words and len(words[0]) >= 3 and words[0] not in _GENERIC and not words[0].isdigit():
        return words[0]
    return None


@dataclass(frozen=True)
class Member:
    root: str
    member: str
    alias: str


@dataclass
class Groups:
    """El seed de grupos indexado por clave de alias y por marca."""

    by_key: dict[str, Member] = field(default_factory=dict)
    by_brand: dict[str, set[str]] = field(default_factory=dict)   # marca -> roots
    notes: dict[str, str] = field(default_factory=dict)           # root -> notas unidas, en clave

    @classmethod
    def load(cls, path: Path | str) -> Groups:
        groups = cls()
        with open(path, encoding="utf-8", newline="") as fh:
            for row in csv.DictReader(fh):
                root, member = (row.get("root_canonical") or "").strip(), (row.get("member_canonical") or "").strip()
                if not root:
                    continue
                groups.notes[root] = f"{groups.notes.get(root, '')} {name_key(row.get('note'))}".strip()
                names = [member, root, *(a.strip() for a in (row.get("aliases") or "").split("|"))]
                for alias in (n for n in names if n):
                    key = name_key(alias)
                    if key:
                        groups.by_key.setdefault(key, Member(root, member or root, alias))
                        b = brand(key)
                        if b:
                            groups.by_brand.setdefault(b, set()).add(root)
        return groups

    def classify(self, name: str) -> tuple[str, list[str], Member | None]:
        """(status, roots, member exacto) de un nombre."""
        key = name_key(name)
        exact = self.by_key.get(key)
        if exact:
            return "known", [exact.root], exact
        b = brand(key)
        roots = sorted(self.by_brand.get(b, ())) if b else []
        return ("new_spelling" if roots else "unknown"), roots, None

    def mentioned_in_notes(self, name: str) -> list[str]:
        """Roots cuya nota nombra este nombre: suele ser la razon por la que
        el curador lo dejo fuera ("Continental Copters ... deliberately kept
        out") o dentro."""
        key = name_key(name)
        if len(key) < 5:
            return []
        return sorted(root for root, notes in self.notes.items() if f" {key} " in f" {notes} ")


@dataclass
class Candidate:
    name: str
    roles: set[str] = field(default_factory=set)            # holder | short_name | former_holder
    tcds: set[str] = field(default_factory=set)
    models: list[str] = field(default_factory=list)
    successors: set[str] = field(default_factory=set)       # titular actual de sus hojas (si fue anterior)
    in_current: bool = False
    product_types: set[str] = field(default_factory=set)


def _strings(value) -> list[str]:
    """Nombres de un campo de DRS sin vacios, centinelas ni repetidos. Los
    titulares anteriores de una misma hoja vienen encadenados con una barra
    invertida ("Hiller Aviation", "Fairchild Industries" y "Rogerson Hiller"
    en un solo valor): cada uno es un nombre. Los sufijos que DRS separo por
    la coma ("Inc.", "a.s.") vuelven a su nombre, como en las marcas de los
    AD (`faa_drs_ads.clean_makes`)."""
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list):
        return []
    parts = [p for item in value if isinstance(item, str) for p in item.split("\\")]
    return [n for n in clean_makes(parts) if n != _EMPTY]


def collect(documents: Iterable[dict]) -> dict[str, Candidate]:
    """Nombres de titulares y titulares anteriores de las hojas, por nombre
    exacto (la agrupacion por clave la hace el revisor con la columna
    `name_key`)."""
    found: dict[str, Candidate] = {}

    def add(name: str, role: str, doc: dict, holder: str | None) -> None:
        cand = found.setdefault(name, Candidate(name))
        cand.roles.add(role)
        number = doc.get("drs:documentNumber")
        if isinstance(number, str) and number.strip():
            cand.tcds.add(number.strip())
        for model in _strings(doc.get("drs:tcdsmodelModel")):
            if model not in cand.models:
                cand.models.append(model)
        cand.product_types.update(_strings(doc.get("drs:tcdsmodelProductType")))
        cand.in_current = cand.in_current or doc.get("drs:status") == "Current"
        if role == "former_holder" and holder and holder != name:
            cand.successors.add(holder)

    for doc in documents:
        holders = _strings(doc.get("drs:tcdsmodelTCHolder"))
        holder = holders[0] if holders else None
        for name in holders:
            add(name, "holder", doc, None)
        for name in _strings(doc.get("drs:tcdsmodelTCHolderShortName")):
            add(name, "short_name", doc, None)
        for name in _strings(doc.get("drs:tcdsmodelFormerHolders")):
            add(name, "former_holder", doc, holder)
    return found


CSV_COLUMNS = ("status", "name", "name_key", "roles", "group_roots", "matched_member", "matched_alias",
               "mentioned_in_notes", "successor_holders", "successor_roots", "tcds_numbers", "n_tcds", "models", "n_models",
               "product_types", "in_current_tcds")


def report_rows(candidates: dict[str, Candidate], groups: Groups) -> list[dict]:
    """Una fila por nombre, ordenadas: new_spelling, unknown, known."""
    rows = []
    for cand in candidates.values():
        status, roots, member = groups.classify(cand.name)
        successor_roots = sorted({r for s in cand.successors for r in groups.classify(s)[1]
                                  if groups.classify(s)[0] == "known"})
        if status == "unknown" and successor_roots:
            # Titular anterior de hojas que hoy tiene un fabricante del seed:
            # la pista mas fuerte de que es un nombre anterior de ese grupo.
            status, roots = "new_spelling", successor_roots
        tcds, models = sorted(cand.tcds), cand.models
        rows.append({
            "status": status,
            "name": cand.name,
            "name_key": name_key(cand.name),
            "roles": "|".join(sorted(cand.roles)),
            "group_roots": "|".join(roots),
            "matched_member": member.member if member else "",
            "matched_alias": member.alias if member else "",
            "mentioned_in_notes": "|".join(groups.mentioned_in_notes(cand.name)),
            "successor_holders": "|".join(sorted(cand.successors)),
            "successor_roots": "|".join(successor_roots),
            "tcds_numbers": "|".join(tcds[:LIST_MAX]),
            "n_tcds": len(tcds),
            "models": "|".join(models[:LIST_MAX]),
            "n_models": len(models),
            "product_types": "|".join(sorted(cand.product_types)),
            "in_current_tcds": "yes" if cand.in_current else "no",
        })
    rows.sort(key=lambda r: (_STATUS_ORDER[r["status"]], r["name_key"], r["name"]))
    return rows


def write_csv(rows: list[dict], path: Path | str) -> None:
    with open(path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(rows)


def slim(doc: dict) -> dict:
    return {k: doc.get(k) for k in FIELDS if k in doc}


def fetch(client: faa_drs_api.DrsClient, max_pages: int = MAX_PAGES) -> list[dict]:
    """Todos los documentos TCDSMODEL, reducidos a FIELDS."""
    docs: list[dict] = []
    for page in client.iter_pages(DOCTYPE, max_pages=max_pages):
        docs.extend(slim(d) for d in page.documents)
    return docs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Candidatos a alias de fabricante desde FAA DRS TCDS")
    parser.add_argument("--out", required=True, help="CSV de salida (fuera del repo: es un reporte)")
    parser.add_argument("--groups", default=str(DEFAULT_GROUPS), help="manufacturer-groups.csv (solo lectura)")
    parser.add_argument("--input", help="JSON con los documentos TCDSMODEL ya leidos (sin red)")
    parser.add_argument("--dump", help="Guarda aqui los documentos leidos de la API (reducidos)")
    parser.add_argument("--max-pages", type=int, default=MAX_PAGES)
    args = parser.parse_args(argv)

    if args.input:
        with open(args.input, encoding="utf-8") as fh:
            data = json.load(fh)
        docs = data["documents"] if isinstance(data, dict) else data
    else:
        try:
            client = faa_drs_api.DrsClient.from_env()
        except faa_drs_api.DrsError as e:
            print(f"[TCDS] {e}", file=sys.stderr)
            return 1
        docs = fetch(client, args.max_pages)
        print(f"[TCDS] {len(docs)} documentos en {client.requests_made} peticiones")
        if args.dump:
            with open(args.dump, "w", encoding="utf-8") as fh:
                json.dump({"doctype": DOCTYPE, "documents": docs}, fh, ensure_ascii=False)

    rows = report_rows(collect(docs), Groups.load(args.groups))
    write_csv(rows, args.out)
    counts = {s: sum(1 for r in rows if r["status"] == s) for s in _STATUS_ORDER}
    print(f"[TCDS] {len(rows)} nombres -> {args.out}: " + ", ".join(f"{k}={v}" for k, v in counts.items()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
