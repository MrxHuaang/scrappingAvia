"""
Verifica contra el PDF que cada fila FAA muestra el documento de SU AD.

Es la comprobacion de extremo a extremo del bug que reporto el usuario: la
pagina mostraba un PDF que no era el del AD. El header regulatorio vive en la
primera pagina del PDF ("[Docket No. ...; Amendment 39-22461; AD 2023-12-03]"),
asi que se compara ese numero con ads.ad_number.

Muestra aleatoria (no toda la base: son 13k descargas).

    python scripts/verify_faa_pdfs.py --sample 40
    python scripts/verify_faa_pdfs.py --ad 2023-12-03 --ad 2024-19-03
"""

import argparse
import io
import os
import random
import re
import sys

import requests
from dotenv import load_dotenv

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, _ROOT)

load_dotenv()
from supabase import create_client  # noqa: E402

SOURCE = "federal_register"
# "[... Amendment 39-22461; AD 2023-12-03]" tal como sale del PDF. Los guiones
# del Federal Register son en/em dash y la fuente embebida los mapea mal: pypdf
# devuelve U+FFFD en los PDFs modernos y U+00B1 ("±") en los de los 90. Ademas
# aparecen espacios sueltos ("Amendment 39- 16906; AD 2010-06-12R1").
_DASHES = re.compile(
    "[‐-―−�­⁃±·─˗]"
)
_HEADER_RE = re.compile(
    r"Amendment\s*39\s*-\s*\d{3,6}\s*[;,]?\s*AD\s*"
    r"((?:\d{4}|\d{2})\s*-\s*\d{2}\s*-\s*\d{2,4}(?:\s*R\s*\d+)?)",
    re.I,
)


def _norm(txt: str) -> str:
    return re.sub(r"\s+", " ", _DASHES.sub("-", txt or ""))


def header_ads(pdf_bytes: bytes) -> list[str]:
    """
    Todos los numeros AD que aparecen en un header regulatorio del PDF.

    Se devuelven TODOS a proposito: el PDF por documento de govinfo trae paginas
    completas del Federal Register, asi que arrastra el header del documento
    vecino. Exigir que el header sea el primero daria falsos positivos.
    """
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(pdf_bytes))
    found = []
    for page in reader.pages:
        for m in _HEADER_RE.finditer(_norm(page.extract_text())):
            found.append(re.sub(r"\s+", "", m.group(1)).upper())
    return list(dict.fromkeys(found))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", type=int, default=40)
    ap.add_argument("--ad", action="append", default=[], help="verifica ADs concretos")
    ap.add_argument("--seed", type=int, default=7)
    args = ap.parse_args()

    sb = create_client(os.environ["SUPABASE_URL"], os.environ["SUPABASE_SERVICE_KEY"])

    if args.ad:
        rows = (sb.table("ads").select("ad_number, pdf_link").eq("source", SOURCE)
                .in_("ad_number", args.ad).execute().data or [])
    else:
        # Muestra por paginas al azar (PostgREST no ordena aleatoriamente).
        total = (sb.table("ads").select("id", count="exact").eq("source", SOURCE)
                 .limit(1).execute()).count or 0
        rnd = random.Random(args.seed)
        rows = []
        while len(rows) < args.sample and total:
            off = rnd.randrange(0, total)
            got = (sb.table("ads").select("ad_number, pdf_link").eq("source", SOURCE)
                   .range(off, off).execute().data or [])
            rows.extend(got)

    session = requests.Session()
    session.headers.update({"User-Agent": "zephr-verify/1.0"})
    ok = mismatch = no_header = failed = 0
    for r in rows:
        ad, link = r["ad_number"], r["pdf_link"]
        if not link:
            failed += 1
            print(f"  {ad}: sin pdf_link")
            continue
        try:
            data = session.get(link, timeout=60).content
            found = header_ads(data)
        except Exception as e:
            failed += 1
            print(f"  {ad}: descarga/parseo fallo ({e})")
            continue
        if not found:
            no_header += 1
            print(f"  {ad}: el PDF no expone el header (escaneado o formato viejo)")
        elif ad.upper() in found:
            ok += 1
        else:
            mismatch += 1
            print(f"  MISMATCH {ad}: el PDF dice AD {', '.join(found)} -> {link}")

    print(f"\nverificados {len(rows)}: {ok} coinciden, {mismatch} MISMATCH, "
          f"{no_header} sin header legible, {failed} fallos de descarga")
    sys.exit(1 if mismatch else 0)


if __name__ == "__main__":
    main()
