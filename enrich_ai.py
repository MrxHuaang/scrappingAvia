"""
Zephr - enrich_ai.py
Rellena los campos semanticos de la tabla `ads` (unsafe_condition,
corrective_action, compliance_time, amoc_available, published_date, make,
model, product_type) leyendo el PDF completo de cada AD con un LLM.

Disenado para correr sobre TODA la base de forma resumible y barata:
  - Solo procesa filas con algun campo objetivo en NULL y con pdf_link.
  - Descarga y cachea el PDF en disco (no re-baja en re-corridas).
  - Extrae texto con PyMuPDF; si el PDF es escaneado (texto vacio) lo marca
    como skip para un pase de OCR posterior.
  - Llama a un LLM via endpoint OpenAI-compatible (Ollama local por defecto,
    o Gemini / Groq / OpenAI cambiando --endpoint y --model).
  - Devuelve JSON estructurado; regla estricta: si el dato no esta en el
    documento -> null (no inventar).
  - Solo escribe los campos que estaban en NULL (nunca pisa lo ya bueno).

Uso:
    # 1. Ollama local (gratis, sin limite) - recomendado
    ollama serve            # en otra terminal
    ollama pull qwen2.5:7b-instruct
    python enrich_ai.py --limit 50              # piloto
    python enrich_ai.py                         # toda la base

    # Otra fuente / modelo
    python enrich_ai.py --source "ANAC Brasil"
    python enrich_ai.py --endpoint https://generativelanguage.googleapis.com/v1beta/openai \\
        --model gemini-2.0-flash --api-key $GEMINI_API_KEY

Flags utiles: --dry-run (no escribe), --no-pdf (solo metadata/abstract),
--limit N, --source LABEL, --pdf-cache DIR.
"""

import argparse
import json
import logging
import os
import re
import sys
import hashlib
from datetime import datetime, timezone

import requests
from dotenv import load_dotenv
from supabase import create_client

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("enrich_ai")

# Campos que este script intenta rellenar.
TARGET_FIELDS = (
    "unsafe_condition",
    "corrective_action",
    "compliance_time",
    "published_date",
    "make",
    "model",
    "product_type",
)
PRODUCT_TYPES = {"Aircraft", "Engine", "Propeller", "Appliance", "Helicopter"}

PDF_CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "outputs", "pdf_cache")
MAX_PDF_CHARS = 16000          # recorte de texto enviado al LLM
MIN_TEXT_CHARS = 200           # menos que esto -> probable PDF escaneado

SYSTEM_PROMPT = (
    "You extract structured data from aviation Airworthiness Directive (AD) "
    "documents. Documents may be in English, Spanish or Portuguese. "
    "Return ONLY a JSON object with these keys: unsafe_condition, "
    "corrective_action, compliance_time, published_date, make, model, "
    "product_type, amoc_available. "
    "Rules: extract the information AS STATED in the document. ALWAYS write "
    "every text field in ENGLISH — if the source document is in Spanish or "
    "Portuguese, translate the extracted content to English. Never output "
    "Spanish or Portuguese text. Be concise (max ~300 chars per text field), "
    "but ALWAYS write complete sentences — never cut a word or sentence in "
    "half. If the full detail doesn't fit in ~300 chars, summarize it shorter "
    "rather than truncating mid-word; every text field must end with proper "
    "punctuation (a period), never mid-word. "
    "If a value is not present in the document, use null. NEVER invent data. "
    "published_date must be YYYY-MM-DD or null. "
    "product_type must be exactly one of: Aircraft, Engine, Propeller, "
    "Appliance, Helicopter, or null. "
    "amoc_available is true only if the document mentions an Alternative Method "
    "of Compliance (AMOC), else false."
)


# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------
def get_supabase():
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_SERVICE_KEY", "").strip()
    if not url or not key:
        raise RuntimeError("SUPABASE_URL y SUPABASE_SERVICE_KEY requeridos en .env")
    return create_client(url, key)


def fetch_pending(supabase, source: str | None, page_size: int = 1000):
    """Genera filas con algun campo objetivo NULL y con pdf_link, paginando."""
    offset = 0
    cols = "id,source,source_label,ad_number,subject,pdf_link,raw_data," + ",".join(TARGET_FIELDS) + ",amoc_available"
    while True:
        q = supabase.table("ads").select(cols).not_.is_("pdf_link", "null")
        # al menos uno de los campos de texto en NULL
        q = q.or_(
            "unsafe_condition.is.null,corrective_action.is.null,compliance_time.is.null"
        )
        if source:
            q = q.eq("source_label", source)
        rows = q.range(offset, offset + page_size - 1).execute().data or []
        if not rows:
            return
        for r in rows:
            yield r
        if len(rows) < page_size:
            return
        offset += page_size


# ---------------------------------------------------------------------------
# PDF: descarga con cache + extraccion de texto
# ---------------------------------------------------------------------------
def pdf_path_for(url: str) -> str:
    h = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return os.path.join(PDF_CACHE_DIR, f"{h}.pdf")


def download_pdf(url: str, session: requests.Session) -> bytes | None:
    path = pdf_path_for(url)
    if os.path.exists(path) and os.path.getsize(path) > 0:
        with open(path, "rb") as f:
            return f.read()
    try:
        resp = session.get(url, timeout=45, headers={"User-Agent": "Zephr-enrich/1.0"})
        resp.raise_for_status()
        data = resp.content
        os.makedirs(PDF_CACHE_DIR, exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        return data
    except Exception as e:
        logger.warning(f"  download fallo {url[:60]}: {e}")
        return None


_HTML_SIGNATURES = (b"<!doctype", b"<html", b"<HTML", b"<!DOCTYPE")


def _is_html_response(data: bytes) -> bool:
    """True si el servidor devolvio HTML en vez de un PDF (sesion expirada, redireccion)."""
    snippet = data[:512].lstrip()
    return any(snippet.startswith(sig) or b"\r\n\r\n<" in data[:256] for sig in _HTML_SIGNATURES)


def extract_text(pdf_bytes: bytes) -> str:
    import io
    from pypdf import PdfReader

    if _is_html_response(pdf_bytes):
        logger.debug("  respuesta HTML en lugar de PDF (sesion requerida?)")
        return ""
    try:
        reader = PdfReader(io.BytesIO(pdf_bytes))
    except Exception as e:
        logger.warning(f"  pdf open fallo: {e}")
        return ""
    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return re.sub(r"\n{3,}", "\n\n", "\n".join(parts)).strip()


# ---------------------------------------------------------------------------
# LLM (endpoint OpenAI-compatible)
# ---------------------------------------------------------------------------
def _post_chat(endpoint, model, api_key, system, user, session, json_mode=True):
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": 0,
        # Sin esto, el endpoint OpenAI-compatible de Ollama aplica su propio
        # default (a veces muy bajo, ~128 tokens) y corta el JSON de salida a
        # mitad de una palabra/oracion antes de terminar — la causa real de
        # los textos truncados guardados en la DB, no el limite de _clean_text.
        "max_tokens": 2048,
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    resp = session.post(
        endpoint.rstrip("/") + "/chat/completions",
        json=payload, headers=headers, timeout=180,
    )
    resp.raise_for_status()
    body = resp.json()
    choice = body["choices"][0]
    if choice.get("finish_reason") == "length":
        logger.warning("  respuesta cortada por limite de max_tokens (finish_reason=length)")
    return choice["message"]["content"]


def _looks_incomplete(text, min_len: int = 40) -> bool:
    """Heuristica independiente de longitud fija: un campo descriptivo que no
    termina en puntuacion de cierre y tiene tamano razonable probablemente se
    corto a mitad de oracion/palabra en la generacion (no-determinismo del
    LLM a temperatura 0 en GPU: la misma consulta puede salir completa una vez
    y cortada otra)."""
    if not isinstance(text, str):
        return False
    t = text.rstrip()
    if len(t) < min_len:
        return False
    return not re.search(r'[.!?…)"”\']\s*$', t)


_RETRYABLE_FIELDS = ("unsafe_condition", "corrective_action")


def call_llm(endpoint: str, model: str, api_key: str | None, doc_text: str,
             meta: dict, session: requests.Session, max_retries: int = 2) -> dict | None:
    user = (
        f"AD number: {meta.get('ad_number')}\n"
        f"Source: {meta.get('source_label')}\n"
        f"Subject: {meta.get('subject') or ''}\n\n"
        f"--- DOCUMENT TEXT ---\n{doc_text[:MAX_PDF_CHARS]}"
    )
    last_out = None
    for attempt in range(max_retries + 1):
        try:
            # json_mode=False: el modo json_object de Ollama fuerza decodificacion
            # con gramatica; sin ella el modelo genera libre y confiamos en
            # _parse_json_loose para recuperar el JSON del texto.
            content = _post_chat(endpoint, model, api_key, SYSTEM_PROMPT, user, session, json_mode=False)
            out = _parse_json_loose(content)
        except Exception as e:
            logger.warning(f"  LLM fallo: {e}")
            out = None
        if out is None:
            continue
        last_out = out
        incomplete = [f for f in _RETRYABLE_FIELDS if _looks_incomplete(out.get(f))]
        if not incomplete:
            return out
        if attempt < max_retries:
            logger.info(f"  {incomplete} vino incompleto, reintentando ({attempt + 1}/{max_retries})...")
    return last_out


# ---------------------------------------------------------------------------
# Deteccion de idioma + traduccion forzada a ingles
# ---------------------------------------------------------------------------
# Marcadores fuertes de portugues/espanol (diacriticos y stopwords frecuentes).
_NON_EN_CHARS = re.compile(r"[ãõçñáéíóúâêô]", re.I)
_NON_EN_WORDS = re.compile(
    r"\b(?:de|da|do|dos|das|não|nao|após|apos|voo|vôo|horas|dias|días|meses|anos|años"
    r"|segundo|según|inspeção|inspeções|substituição|operação|fixação|trincas|parede"
    r"|fadiga|cumprimento|efetividade|aeronave|según|vuelo|verificar|realizar)\b",
    re.I,
)


def _non_en_score(text: str) -> int:
    """Cuenta marcadores PT/ES (diacriticos + stopwords). 0 = ingles limpio."""
    if not isinstance(text, str) or not text.strip():
        return 0
    return len(_NON_EN_CHARS.findall(text)) + len(_NON_EN_WORDS.findall(text))


def _looks_non_english(text: str) -> bool:
    if not isinstance(text, str) or not text.strip():
        return False
    if _NON_EN_CHARS.search(text):
        return True
    # >=2 stopwords PT/ES => probablemente no es ingles
    return len(_NON_EN_WORDS.findall(text)) >= 2


_TRANSLATE_SYS = (
    "You are a translator. Translate every value in the given JSON object to "
    "natural English. Keep the exact same keys. If a value is already English, "
    "return it unchanged. Return ONLY the JSON object, nothing else."
)


def translate_fields(endpoint, model, api_key, fields: dict, session) -> dict:
    """Traduce a ingles los valores de texto que parezcan PT/ES. Devuelve dict
    con las mismas claves (texto ingles donde aplicaba)."""
    to_translate = {k: v for k, v in fields.items()
                    if isinstance(v, str) and _looks_non_english(v)}
    if not to_translate:
        return fields
    try:
        content = _post_chat(endpoint, model, api_key, _TRANSLATE_SYS,
                             json.dumps(to_translate, ensure_ascii=False), session)
        out = _parse_json_loose(content) or {}
        result = dict(fields)
        for k in to_translate:
            v = out.get(k)
            if isinstance(v, str) and v.strip():
                result[k] = v.strip()
        return result
    except Exception as e:
        logger.warning(f"  traduccion fallo: {e}")
        return fields


def _parse_json_loose(content: str) -> dict | None:
    """Parsea JSON tolerando fences markdown, // comments y texto antes/despues."""
    try:
        return json.loads(content)
    except Exception:
        pass
    # quitar fence ```json ... ```
    s = re.sub(r"```(?:json)?", "", content)
    # quitar comentarios estilo // y /* */ que algunos modelos meten
    s = re.sub(r"//[^\n]*", "", s)
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    # recortar al primer { ... ultimo }
    i, j = s.find("{"), s.rfind("}")
    if i == -1 or j == -1 or j < i:
        return None
    s = s[i : j + 1]
    # quitar comas colgantes antes de } o ]
    s = re.sub(r",(\s*[}\]])", r"\1", s)
    try:
        return json.loads(s)
    except Exception as e:
        logger.warning(f"  JSON irrecuperable: {e}")
        return None


# ---------------------------------------------------------------------------
# Normalizacion de la salida del LLM -> columnas
# ---------------------------------------------------------------------------
MAX_FIELD_CHARS = 700  # tope de seguridad; el prompt ya pide ~300, esto solo cubre casos raros


def _clean_text(v, max_chars: int = MAX_FIELD_CHARS) -> str | None:
    if not isinstance(v, str):
        return None
    s = v.strip()
    if not s or s.lower() in ("null", "none", "n/a"):
        return None
    if len(s) <= max_chars:
        return s
    # Nunca cortar a mitad de palabra/oracion: preferir el ultimo punto de
    # oracion dentro del limite; si no hay uno razonablemente cerca, cortar
    # en el ultimo espacio (nunca a mitad de una palabra).
    cut = s[:max_chars]
    last_end = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "))
    if last_end >= max_chars * 0.5:
        return cut[: last_end + 1].strip()
    last_space = cut.rfind(" ")
    if last_space > 0:
        return cut[:last_space].rstrip(",;: ") + "…"
    return cut + "…"


def _looks_truncated(text, old_cap: int = 500, tolerance: int = 30) -> bool:
    """Detecta texto cortado a mitad de palabra por el bug viejo de _clean_text
    (corte duro en el caracter 500, sin mirar si terminaba en puntuacion)."""
    if not isinstance(text, str):
        return False
    t = text.rstrip()
    if len(t) < old_cap - tolerance:
        return False
    return not re.search(r'[.!?…)"”\']\s*$', t)


def _clean_date(v) -> str | None:
    if not isinstance(v, str):
        return None
    m = re.search(r"(\d{4})-(\d{2})-(\d{2})", v.strip())
    return m.group(0) if m else None


def build_update(row: dict, out: dict) -> dict:
    """Construye dict de UPDATE solo para campos hoy NULL y con valor nuevo valido."""
    upd = {}
    if row.get("unsafe_condition") is None:
        v = _clean_text(out.get("unsafe_condition"))
        # nunca guardar un corte a mitad de oracion, aunque haya sobrevivido
        # los reintentos de call_llm (la generacion es no-determinista) —
        # mejor dejarlo NULL para que una corrida futura lo intente de nuevo.
        if v and not _looks_incomplete(v):
            upd["unsafe_condition"] = v
    if row.get("corrective_action") is None:
        v = _clean_text(out.get("corrective_action"))
        if v and not _looks_incomplete(v):
            upd["corrective_action"] = v
    if row.get("compliance_time") is None:
        v = _clean_text(out.get("compliance_time"))
        if v:
            upd["compliance_time"] = v
    if row.get("published_date") is None:
        v = _clean_date(out.get("published_date"))
        if v:
            upd["published_date"] = v
    if not row.get("make"):
        v = _clean_text(out.get("make"))
        if v:
            upd["make"] = v
    if not row.get("model"):
        v = _clean_text(out.get("model"))
        if v:
            upd["model"] = v
    if not row.get("product_type"):
        pt = (out.get("product_type") or "")
        pt = pt.strip().capitalize() if isinstance(pt, str) else ""
        if pt in PRODUCT_TYPES:
            upd["product_type"] = pt
    # amoc_available: solo subir a true (no bajar un true existente a false).
    # El LLM a veces devuelve el bool como string ("True"/"true") -> coercer.
    amoc = out.get("amoc_available")
    amoc_true = amoc is True or (isinstance(amoc, str) and amoc.strip().lower() == "true")
    if amoc_true and row.get("amoc_available") is not True:
        upd["amoc_available"] = True
    # estampar timestamp solo si de verdad cambiamos algo (senal de progreso/auditoria)
    if upd:
        upd["last_updated_at"] = datetime.now(timezone.utc).isoformat()
    return upd


_TEXT_FIELDS = ("subject", "unsafe_condition", "corrective_action", "compliance_time")


def run_retranslate(supabase, session, args):
    """Recorre filas con texto ya guardado; donde se vea PT/ES, traduce a ingles
    y sobrescribe. Solo llama al LLM en las filas marcadas (las inglesas se saltan)."""
    stats = {"seen": 0, "fixed": 0, "skipped_en": 0, "fail": 0}
    offset, page = 0, 1000
    while True:
        q = supabase.table("ads").select(
            "id,source_label,ad_number,subject,unsafe_condition,corrective_action,compliance_time"
        )
        if args.source:
            q = q.eq("source_label", args.source)
        rows = q.range(offset, offset + page - 1).execute().data or []
        if not rows:
            break
        for row in rows:
            stats["seen"] += 1
            flagged = {f: row[f] for f in _TEXT_FIELDS
                       if row.get(f) and _looks_non_english(row[f])}
            if not flagged:
                stats["skipped_en"] += 1
                continue
            translated = translate_fields(args.endpoint, args.model,
                                          args.api_key or None, flagged, session)
            # escribir si la traduccion mejoro (menos marcadores PT/ES que el original)
            upd = {f: translated[f] for f in flagged
                   if isinstance(translated.get(f), str)
                   and _non_en_score(translated[f]) < _non_en_score(flagged[f])}
            if not upd:
                stats["fail"] += 1
                continue
            upd["last_updated_at"] = datetime.now(timezone.utc).isoformat()
            ad = row.get("ad_number")
            if args.dry_run:
                logger.info(f"{ad} DRY translate -> {json.dumps({k: v[:60] for k, v in upd.items() if k != 'last_updated_at'}, ensure_ascii=False)}")
            else:
                try:
                    supabase.table("ads").update(upd).eq("id", row["id"]).execute()
                    stats["fixed"] += 1
                    logger.info(f"{ad}: traducido {list(upd.keys())}")
                except Exception as e:
                    logger.warning(f"{ad}: update fallo: {e}")
                    stats["fail"] += 1
            if args.limit and stats["fixed"] >= args.limit:
                logger.info(f"=== FIN retranslate (limit): {stats} ===")
                return
        if len(rows) < page:
            break
        offset += page
    logger.info(f"=== FIN retranslate: {stats} ===")


_TRUNCATABLE_FIELDS = ("unsafe_condition", "corrective_action", "compliance_time")


def run_fix_truncated(supabase, session, args):
    """Encuentra SOLO las filas con texto cortado a mitad de palabra (bug viejo
    del corte duro a 500 caracteres) y las re-extrae desde el PDF para
    completarlas — no relee ni retoca el resto de la base."""
    stats = {"seen": 0, "flagged": 0, "fixed": 0, "no_pdf": 0, "fail": 0}
    offset, page = 0, 1000
    cols = ("id,source_label,ad_number,subject,pdf_link,raw_data,"
            "unsafe_condition,corrective_action,compliance_time")
    while True:
        q = supabase.table("ads").select(cols)
        if args.source:
            q = q.eq("source_label", args.source)
        rows = q.range(offset, offset + page - 1).execute().data or []
        if not rows:
            break
        for row in rows:
            stats["seen"] += 1
            bad_fields = [f for f in _TRUNCATABLE_FIELDS if _looks_truncated(row.get(f))]
            if not bad_fields:
                continue
            stats["flagged"] += 1
            ad = row.get("ad_number")

            if not row.get("pdf_link"):
                stats["no_pdf"] += 1
                continue
            pdf = download_pdf(row["pdf_link"], session)
            doc_text = extract_text(pdf) if pdf else ""
            if len(doc_text) < MIN_TEXT_CHARS and row.get("subject"):
                doc_text = (doc_text + "\n" + row["subject"]).strip()
            if len(doc_text) < MIN_TEXT_CHARS:
                stats["fail"] += 1
                logger.info(f"{ad}: cortado pero sin texto de PDF disponible -> skip")
                continue

            # call_llm ya reintenta internamente si unsafe/corrective salen
            # incompletos, pero la generacion del LLM es no-determinista
            # (temperatura 0 en GPU no garantiza el mismo resultado en cada
            # llamada) — un intento extra a este nivel es barato y reduce
            # aun mas la chance de guardar un corte.
            out = None
            for outer_attempt in range(2):
                candidate = call_llm(args.endpoint, args.model, args.api_key or None, doc_text, row, session)
                if candidate is None:
                    continue
                out = candidate
                still_bad = [f for f in bad_fields if _looks_incomplete(candidate.get(f))]
                if not still_bad:
                    break
                logger.info(f"{ad}: {still_bad} seguia incompleto, reintento externo {outer_attempt + 1}/2")
            if out is None:
                stats["fail"] += 1
                continue
            out = translate_fields(args.endpoint, args.model, args.api_key or None, out, session)

            upd = {}
            for f in bad_fields:
                v = _clean_text(out.get(f))
                # _looks_incomplete detecta cualquier corte a mitad de oracion
                # (no solo el patron viejo de ~500 chars) — es el filtro correcto
                # para decidir si el valor nuevo es seguro de guardar.
                if v and not _looks_incomplete(v):
                    upd[f] = v
            if not upd:
                stats["fail"] += 1
                logger.info(f"{ad}: seguia cortado tras reintentar -> skip")
                continue
            upd["last_updated_at"] = datetime.now(timezone.utc).isoformat()

            if args.dry_run:
                # ojo: preview de 80 chars solo para no saturar el log — el
                # valor real que se guardaria esta completo, esto es SOLO display.
                logger.info(f"{ad} DRY fix -> {list(upd.keys())}: "
                            f"{json.dumps({k: str(v)[:80] + ('…[preview]' if len(str(v)) > 80 else '') for k, v in upd.items() if k != 'last_updated_at'}, ensure_ascii=False)}")
            else:
                try:
                    supabase.table("ads").update(upd).eq("id", row["id"]).execute()
                    stats["fixed"] += 1
                    logger.info(f"{ad}: reparado {list(upd.keys())}")
                except Exception as e:
                    logger.warning(f"{ad}: update fallo: {e}")
                    stats["fail"] += 1

            if args.limit and stats["fixed"] >= args.limit:
                logger.info(f"=== FIN fix-truncated (limit): {stats} ===")
                return
        if len(rows) < page:
            break
        offset += page
    logger.info(f"=== FIN fix-truncated: {stats} ===")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global PDF_CACHE_DIR
    ap = argparse.ArgumentParser()
    ap.add_argument("--endpoint", default=os.environ.get("LLM_ENDPOINT", "http://localhost:11434/v1"),
                    help="endpoint OpenAI-compatible (Ollama: http://localhost:11434/v1)")
    ap.add_argument("--model", default=os.environ.get("LLM_MODEL", "qwen-ad"))
    ap.add_argument("--api-key", default=os.environ.get("LLM_API_KEY", ""))
    ap.add_argument("--source", default=None, help="source_label exacto (ej. 'EASA')")
    ap.add_argument("--limit", type=int, default=0, help="0 = sin limite")
    ap.add_argument("--no-pdf", action="store_true", help="no descargar PDF; usar solo subject/raw_data")
    ap.add_argument("--pdf-cache", default=PDF_CACHE_DIR)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--subject-fallback", action="store_true",
                    help="si PDF falla o retorna HTML, usar solo el subject como texto fuente")
    ap.add_argument("--retranslate", action="store_true",
                    help="no extrae; busca campos ya guardados en PT/ES y los traduce a ingles")
    ap.add_argument("--fix-truncated", action="store_true",
                    help="repara SOLO filas con texto cortado a mitad de palabra (bug del corte a 500 chars)")
    args = ap.parse_args()

    PDF_CACHE_DIR = args.pdf_cache

    supabase = get_supabase()
    session = requests.Session()

    if args.fix_truncated:
        run_fix_truncated(supabase, session, args)
        return

    if args.retranslate:
        run_retranslate(supabase, session, args)
        return

    stats = {"seen": 0, "updated": 0, "skipped_scanned": 0, "no_pdf": 0, "llm_fail": 0, "no_change": 0}
    for row in fetch_pending(supabase, args.source):
        if args.limit and stats["seen"] >= args.limit:
            break
        stats["seen"] += 1
        ad = row.get("ad_number")

        doc_text = ""
        if not args.no_pdf and row.get("pdf_link"):
            pdf = download_pdf(row["pdf_link"], session)
            if pdf:
                doc_text = extract_text(pdf)
        if len(doc_text) < MIN_TEXT_CHARS:
            # sin texto util: cae a metadata (subject) o se salta
            if row.get("subject"):
                doc_text = (doc_text + "\n" + row["subject"]).strip()
            if len(doc_text) < MIN_TEXT_CHARS:
                if args.subject_fallback and row.get("subject"):
                    doc_text = row["subject"]
                elif not args.no_pdf:
                    stats["skipped_scanned"] += 1
                    logger.info(f"[{stats['seen']}] {ad}: PDF sin texto -> skip")
                    continue

        # call_llm ya reintenta internamente si unsafe/corrective salen
        # incompletos; un intento extra aqui es barato (la generacion del LLM
        # es no-determinista a temperatura 0 en GPU) y reduce aun mas la
        # chance de guardar un campo cortado a mitad de oracion.
        out = None
        for _outer_attempt in range(2):
            candidate = call_llm(args.endpoint, args.model, args.api_key or None, doc_text, row, session)
            if candidate is None:
                continue
            out = candidate
            if not any(_looks_incomplete(candidate.get(f)) for f in _RETRYABLE_FIELDS):
                break
        if out is None:
            stats["llm_fail"] += 1
            continue

        # Forzar ingles: si algun campo de texto salio en PT/ES, traducir.
        out = translate_fields(args.endpoint, args.model, args.api_key or None, out, session)

        upd = build_update(row, out)

        # Traducir el subject (titulo) si esta en PT/ES — siempre tiene valor, no
        # es NULL, asi que se sobrescribe solo si la traduccion mejora.
        subj = row.get("subject")
        if subj and _looks_non_english(subj):
            t = translate_fields(args.endpoint, args.model, args.api_key or None,
                                 {"subject": subj}, session)
            ts = t.get("subject")
            if isinstance(ts, str) and _non_en_score(ts) < _non_en_score(subj):
                upd["subject"] = ts
                upd["last_updated_at"] = datetime.now(timezone.utc).isoformat()
        if not upd:
            stats["no_change"] += 1
            logger.info(f"[{stats['seen']}] {ad}: sin campos nuevos")
            continue

        if args.dry_run:
            logger.info(f"[{stats['seen']}] {ad} DRY -> {list(upd.keys())}: "
                        f"{json.dumps({k: (str(v)[:60]) for k, v in upd.items()}, ensure_ascii=False)}")
        else:
            try:
                supabase.table("ads").update(upd).eq("id", row["id"]).execute()
                stats["updated"] += 1
                logger.info(f"[{stats['seen']}] {ad}: +{list(upd.keys())}")
            except Exception as e:
                logger.warning(f"[{stats['seen']}] {ad}: update fallo: {e}")

        if stats["seen"] % 50 == 0:
            logger.info(f"--- progreso: {stats} ---")

    logger.info(f"=== FIN: {stats} ===")


if __name__ == "__main__":
    main()
