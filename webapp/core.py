"""
Nucleo compartido de la app web (Fase 3b).

Aqui vive todo lo transversal a los blueprints: configuracion leida del
entorno, clientes Supabase perezosos, los dos guards (sesion y proxy
interno), el rate limit respaldado por Supabase y los servicios de
identidad/plan que usan tanto auth como billing como el rate limit.

Ningun blueprint se importa desde aqui (los blueprints importan de core,
nunca al reves).
"""

import base64
import hmac
import json
import logging
import os
import time
import traceback
from datetime import datetime, timezone
from functools import wraps
from typing import Callable

import stripe
from flask import Response, jsonify, redirect, request, session
from werkzeug.wrappers.response import Response as WerkzeugResponse

logger = logging.getLogger(__name__)

APP_VERSION = "1.1.0"

# ------------------------------------------------------------------ config --
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
IS_SERVERLESS = bool(os.environ.get("VERCEL"))

# Flask session signing key (SEC-6). Fail fast in production instead of falling
# back to a publicly-known constant (which would make session cookies forgeable).
FLASK_SECRET = os.environ.get("FLASK_SECRET_KEY")
if not FLASK_SECRET:
    if IS_SERVERLESS or os.environ.get("FLASK_ENV") == "production":
        raise RuntimeError(
            "FLASK_SECRET_KEY must be set in production. Generate with: "
            "python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    FLASK_SECRET = "dev-secret-change-in-production"

# Stripe (optional — works without it)
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRO_PRICE_ID = os.environ.get("STRIPE_PRO_PRICE_ID", "")
STRIPE_TEAM_PRICE_ID = os.environ.get("STRIPE_TEAM_PRICE_ID", "")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")

# Shared secret injected by the Next.js proxy (SEC-3). When set, /api/search*,
# /api/export* reject any request lacking the matching X-Internal-Secret header.
INTERNAL_PROXY_SECRET = os.environ.get("INTERNAL_PROXY_SECRET", "")

# CORS allowlist (SEC-5): never combine wildcard origins with credentials.
_raw_origins = os.environ.get("ALLOWED_ORIGINS", "").strip()
if _raw_origins and _raw_origins != "*":
    ALLOWED_ORIGINS: list[str] | str = [o.strip() for o in _raw_origins.split(",") if o.strip()]
else:
    ALLOWED_ORIGINS = "*"

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "results")
os.makedirs(OUTPUT_DIR, exist_ok=True)

SCRAPER_TIMEOUT = 45  # timeout maximo por fuente (segundos)
PDF_DOWNLOAD_TIMEOUT = 15
PDF_MAX_WORKERS = 5
PDF_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# Fuentes con datos precargados en Supabase: se busca primero en la BD y el
# scraping en vivo queda como fallback.
DB_BACKED_SOURCES = frozenset([
    "easa", "federal_register", "transport_canada", "anac_brazil",
])

# --------------------------------------------------------- supabase clients --
_supabase = None
_supabase_admin = None
# True si get_supabase_admin() construyo el cliente con SUPABASE_SERVICE_KEY.
_SUPABASE_ADMIN_USES_SERVICE_ROLE: bool | None = None


def get_supabase():
    """Cliente con anon key — para auth (sign_in, sign_up, OAuth)."""
    global _supabase
    if _supabase is None and SUPABASE_URL and SUPABASE_ANON_KEY:
        from supabase import create_client
        _supabase = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    return _supabase


def get_supabase_admin():
    """Cliente con service_role key — para operaciones de servidor (profiles, billing)."""
    global _supabase_admin, _SUPABASE_ADMIN_USES_SERVICE_ROLE
    if _supabase_admin is None:
        from supabase import create_client
        sk = os.environ.get("SUPABASE_SERVICE_KEY", "")
        logger.info(f"[ADMIN] Creando cliente admin. Service key presente: {bool(sk)}")
        if SUPABASE_URL and SUPABASE_SERVICE_KEY:
            _supabase_admin = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
            _SUPABASE_ADMIN_USES_SERVICE_ROLE = True
            logger.info("[ADMIN] Cliente creado con SUPABASE_SERVICE_KEY (service_role)")
        elif SUPABASE_URL and SUPABASE_ANON_KEY:
            _supabase_admin = get_supabase()
            _SUPABASE_ADMIN_USES_SERVICE_ROLE = False
            logger.warning(
                "[ADMIN] FALLBACK: cliente admin = anon key — RLS puede bloquear upsert en profiles"
            )
        else:
            _SUPABASE_ADMIN_USES_SERVICE_ROLE = None
    return _supabase_admin


# ------------------------------------------------------------------- guards --
def require_auth(f: Callable) -> Callable:
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("access_token"):
            next_url = request.path
            return redirect(f"/login?next={next_url}")
        return f(*args, **kwargs)
    return decorated


def require_proxy(f: Callable) -> Callable:
    """
    Guard for data endpoints reached through the Next.js proxy (SEC-3).
    When INTERNAL_PROXY_SECRET is configured, requests must carry a matching
    X-Internal-Secret header (set by the proxy); otherwise they are rejected.
    If the secret is not configured, the guard is a no-op (backward compatible)
    but logs a warning so the hardening can be rolled out via env config.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        if INTERNAL_PROXY_SECRET:
            sent = request.headers.get("X-Internal-Secret", "")
            if not hmac.compare_digest(sent, INTERNAL_PROXY_SECRET):
                return jsonify({"error": "Forbidden"}), 403
        else:
            logger.warning(
                "[SEC] INTERNAL_PROXY_SECRET not set — data endpoints are unauthenticated"
            )
        return f(*args, **kwargs)
    return decorated


# --------------------------------------------------------------- rate limit --
def check_rate_limit() -> tuple[Response, int] | None:
    """
    Rate limiting respaldado por Supabase (tabla rate_limits).
    Funciona en serverless — no usa memoria local.
    Planes pro/team: sin limite. Plan free: 20 busquedas/dia. Anonimo: 3/hora.
    Devuelve None si la request puede continuar, o la respuesta 429.
    """
    # Identity: prefer the proxy-forwarded user id over the (shared) IP so paying
    # users get their own bucket and aren't throttled by other users' traffic (BUG-2).
    uid = request.headers.get("X-User-Id")
    plan = session.get("plan")
    if not plan and uid:
        plan = fetch_user_plan(uid)
    if plan in ("pro", "team"):
        return None

    ip = uid or request.remote_addr or "unknown"
    now = time.time()

    sb = get_supabase_admin()
    if not sb:
        logger.warning("[RATE] Supabase no disponible, rate limit omitido")
        return None

    try:
        if plan == "free":
            window = 86400   # 24h
            limit = 20
        else:
            window = 3600    # 1h
            limit = 3

        cutoff_iso = datetime.fromtimestamp(now - window, tz=timezone.utc).isoformat()

        resp = sb.table("rate_limits") \
            .select("id", count="exact") \
            .eq("ip", ip) \
            .gte("requested_at", cutoff_iso) \
            .execute()

        count = resp.count or 0

        if count >= limit:
            if plan == "free":
                msg = "Limite de busquedas alcanzado (20/dia). Actualiza tu plan para busquedas ilimitadas."
            else:
                msg = "Limite de busquedas alcanzado (3/hora). Inicia sesion para mas busquedas."
            return jsonify({"error": msg}), 429

        sb.table("rate_limits").insert({
            "ip": ip,
            "plan": plan or "anonymous",
            "requested_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        }).execute()

    except Exception as e:
        logger.warning(f"[RATE] Error en rate limit check: {e} — permitiendo request")

    return None


# --------------------------------------------- identity and plan (services) --
def jwt_sub_unverified(token) -> str | None:
    """Extrae 'sub' (user id) del JWT de Supabase sin verificar firma (solo servidor)."""
    if not token or not isinstance(token, str):
        return None
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        payload_b64 = parts[1]
        pad = "=" * ((4 - len(payload_b64) % 4) % 4)
        raw = base64.urlsafe_b64decode(payload_b64 + pad)
        payload = json.loads(raw.decode("utf-8"))
        sub = payload.get("sub")
        return str(sub).strip() if sub else None
    except Exception:
        return None


def user_id_from_flask_session() -> str | None:
    """user_id para metadata de Stripe: dict session['user'] o claims del access_token."""
    user = session.get("user") or {}
    uid = user.get("id") or user.get("sub") or user.get("user_id") or user.get("uid")
    if uid:
        s = str(uid).strip()
        return s if s else None
    return jwt_sub_unverified(session.get("access_token"))


def fetch_user_plan(user_id) -> str:
    """Consulta el plan del usuario desde la tabla profiles en Supabase."""
    sb = get_supabase_admin()
    if not sb:
        return "free"
    try:
        res = sb.table("profiles").select("plan").eq("id", user_id).maybe_single().execute()
        return res.data["plan"] if res.data and res.data.get("plan") else "free"
    except Exception as e:
        logger.info(f"[Stripe] Error fetching plan for {user_id}: {e}")
        return "free"


def update_user_plan(user_id, plan, customer_id=None, subscription_id=None) -> None:
    logger.info(f"[UPDATE_PLAN] Iniciando: user={user_id} plan={plan}")

    admin = get_supabase_admin()
    if not admin:
        logger.error("[UPDATE_PLAN] ERROR: No hay cliente admin de Supabase")
        return

    logger.info("[UPDATE_PLAN] Cliente admin obtenido, haciendo upsert...")

    try:
        result = admin.table("profiles").upsert({
            "id": user_id,
            "plan": plan,
            "stripe_customer_id": customer_id,
            "stripe_subscription_id": subscription_id,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).execute()
        logger.info(f"[UPDATE_PLAN] Resultado: {result}")
    except Exception as e:
        logger.error(f"[UPDATE_PLAN] ERROR en upsert: {type(e).__name__}: {e}")
        logger.error(f"[UPDATE_PLAN] Traceback: {traceback.format_exc()}")


def downgrade_to_free_by_customer(customer_id) -> None:
    sb = get_supabase_admin()
    if not sb:
        return
    try:
        res = sb.table("profiles").select("id").eq("stripe_customer_id", customer_id).maybe_single().execute()
        if res.data:
            update_user_plan(res.data["id"], "free")
    except Exception as e:
        logger.info(f"[Stripe] Error downgrading customer {customer_id}: {e}")


# Alias de tipo para anotar retornos de rutas Flask.
RouteResult = Response | WerkzeugResponse | str | tuple
