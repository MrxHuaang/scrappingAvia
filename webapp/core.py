"""
Nucleo compartido de la app web (Fase 3b).

Aqui vive todo lo transversal a los blueprints: configuracion leida del
entorno, clientes Supabase perezosos, el guard del proxy interno y el rate
limit respaldado por Supabase (con la consulta del plan que lo decide).

Ningun blueprint se importa desde aqui (los blueprints importan de core,
nunca al reves).
"""

import hmac
import logging
import os
import time
from datetime import datetime, timezone
from functools import wraps
from typing import Callable

from flask import Response, jsonify, request
from werkzeug.wrappers.response import Response as WerkzeugResponse

logger = logging.getLogger(__name__)

APP_VERSION = "1.1.0"

# ------------------------------------------------------------------ config --
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

# Shared secret injected by the Next.js proxy (SEC-3). When set, /api/search*
# rejects any request lacking the matching X-Internal-Secret header.
INTERNAL_PROXY_SECRET = os.environ.get("INTERNAL_PROXY_SECRET", "")

SCRAPER_TIMEOUT = 45  # timeout maximo por fuente (segundos)

# --------------------------------------------------------- supabase clients --
_supabase = None
_supabase_admin = None
# True si get_supabase_admin() construyo el cliente con SUPABASE_SERVICE_KEY.
_SUPABASE_ADMIN_USES_SERVICE_ROLE: bool | None = None


def get_supabase():
    """Cliente con anon key — respaldo de get_supabase_admin() sin service key."""
    global _supabase
    if _supabase is None and SUPABASE_URL and SUPABASE_ANON_KEY:
        from supabase import create_client
        _supabase = create_client(SUPABASE_URL, SUPABASE_ANON_KEY)
    return _supabase


def get_supabase_admin():
    """Cliente con service_role key — para operaciones de servidor (busqueda, plan, rate limit)."""
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
        if not proxy_secret_ok():
            return jsonify({"error": "Forbidden"}), 403
        return f(*args, **kwargs)
    return decorated


def proxy_secret_ok() -> bool:
    """
    True si la request trae el X-Internal-Secret del proxy (comparacion en
    tiempo constante), o si el secreto no esta configurado (no-op compatible,
    con warning). Es la unica comprobacion del secreto: la usan `require_proxy`
    y las rutas que deciden su propio status de rechazo.
    """
    if not INTERNAL_PROXY_SECRET:
        logger.warning(
            "[SEC] INTERNAL_PROXY_SECRET not set — data endpoints are unauthenticated"
        )
        return True
    sent = request.headers.get("X-Internal-Secret", "")
    # En bytes: compare_digest rechaza str con caracteres no ASCII (TypeError),
    # y una cabecera asi es un rechazo, no un 500.
    return hmac.compare_digest(sent.encode("utf-8"), INTERNAL_PROXY_SECRET.encode("utf-8"))


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
    plan = fetch_user_plan(uid) if uid else None
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


# ---------------------------------------------------------- plan (service) --
def fetch_user_plan(user_id) -> str:
    """Consulta el plan del usuario desde la tabla profiles en Supabase."""
    sb = get_supabase_admin()
    if not sb:
        return "free"
    try:
        res = sb.table("profiles").select("plan").eq("id", user_id).maybe_single().execute()
        return res.data["plan"] if res.data and res.data.get("plan") else "free"
    except Exception as e:
        logger.info(f"[PLAN] Error fetching plan for {user_id}: {e}")
        return "free"


# Alias de tipo para anotar retornos de rutas Flask.
RouteResult = Response | WerkzeugResponse | str | tuple
