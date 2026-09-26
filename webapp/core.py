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
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")

# Shared secret injected by the Next.js proxy (SEC-3). /api/search* rejects
# any request lacking the matching X-Internal-Secret header, and answers 503
# while the secret is not configured (fail closed; see proxy_rejection).
INTERNAL_PROXY_SECRET = os.environ.get("INTERNAL_PROXY_SECRET", "")

# Explicit opt-in to run the data routes without the secret in local
# development. Read per request, and honoured only off Vercel and only for
# loopback requests (see local_unauthenticated_allowed).
ALLOW_UNAUTHENTICATED_LOCAL_ENV = "ALLOW_UNAUTHENTICATED_LOCAL"
_LOOPBACK = frozenset({"127.0.0.1", "::1"})

SCRAPER_TIMEOUT = 45  # timeout maximo por fuente (segundos)

# --------------------------------------------------------- supabase clients --
_supabase_admin = None


def get_supabase_admin():
    """
    Cliente con service_role key — para operaciones de servidor (busqueda,
    plan, rate limit). Requiere SUPABASE_SERVICE_KEY: la BD revoca los
    privilegios de escritura de anon/authenticated sobre estas tablas, asi
    que degradar a la anon key aqui ya no es un respaldo funcional — es solo
    un cliente que va a fallar en runtime (o, peor, uno que un dia sí pudiera
    escribir sin pasar por service_role). None si la service key no esta
    configurada; quien llama debe fallar cerrado (ver check_rate_limit) en
    vez de tratarlo como "sin rate limit".
    """
    global _supabase_admin
    if _supabase_admin is None and SUPABASE_URL and SUPABASE_SERVICE_KEY:
        from supabase import create_client
        _supabase_admin = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        logger.info("[ADMIN] Cliente admin creado con SUPABASE_SERVICE_KEY (service_role)")
    return _supabase_admin


def log_admin_key_state() -> None:
    """Un aviso claro al arrancar si falta la service key: sin ella no hay
    cliente admin, y check_rate_limit() falla cerrado (503) en toda ruta de
    datos en vez de correr sin rate limit."""
    if SUPABASE_SERVICE_KEY:
        return
    logger.error(
        "[SEC] SUPABASE_SERVICE_KEY not set: get_supabase_admin() returns "
        "None and every data route answers 503 at the rate limiter until "
        "it is configured."
    )


# ------------------------------------------------------------------- guards --
def require_proxy(f: Callable) -> Callable:
    """
    Guard for data endpoints reached through the Next.js proxy (SEC-3): the
    request must carry the X-Internal-Secret the proxy sets, or it is
    rejected with 403. See proxy_rejection for the unconfigured case.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        denied = proxy_rejection(403)
        if denied:
            return denied
        return f(*args, **kwargs)
    return decorated


def proxy_rejection(denied_status: int) -> tuple[Response, int] | None:
    """
    El unico guard del proxy, compartido por v1 (`require_proxy`, 403) y v2
    (401): None si la request puede seguir, o la respuesta de rechazo.

    - Secreto configurado: pasa solo con un X-Internal-Secret igual;
      si no, `denied_status`.
    - Secreto sin configurar: falla cerrado con 503 y un error generico,
      salvo el opt-in explicito de desarrollo local
      (`local_unauthenticated_allowed`).
    """
    if not INTERNAL_PROXY_SECRET:
        if local_unauthenticated_allowed():
            return None
        return jsonify({"error": "Service unavailable"}), 503
    if proxy_secret_ok():
        return None
    return jsonify({"error": "Unauthorized" if denied_status == 401 else "Forbidden"}), denied_status


def proxy_secret_ok() -> bool:
    """
    True si el secreto esta configurado y la request trae el mismo
    X-Internal-Secret (comparacion en tiempo constante).
    """
    if not INTERNAL_PROXY_SECRET:
        return False
    sent = request.headers.get("X-Internal-Secret", "")
    # En bytes: compare_digest rechaza str con caracteres no ASCII (TypeError),
    # y una cabecera asi es un rechazo, no un 500.
    return hmac.compare_digest(sent.encode("utf-8"), INTERNAL_PROXY_SECRET.encode("utf-8"))


def local_unauthenticated_allowed() -> bool:
    """
    Opt-in de desarrollo local para correr sin secreto. Cuenta solo si
    ALLOW_UNAUTHENTICATED_LOCAL=1, fuera de Vercel (VERCEL sin definir) y
    para una request de loopback: tanto el peer TCP real (el que ProxyFix
    guarda antes de reescribir) como `request.remote_addr` ya reescrito
    deben ser loopback, para que un X-Forwarded-For: 127.0.0.1 enviado
    desde fuera no baste, ni un proxy local que reenvia trafico externo.
    """
    if os.environ.get("VERCEL"):
        return False
    if os.environ.get(ALLOW_UNAUTHENTICATED_LOCAL_ENV, "").strip() != "1":
        return False
    peer = request.environ.get("werkzeug.proxy_fix.orig", {}).get("REMOTE_ADDR", request.remote_addr)
    return peer in _LOOPBACK and request.remote_addr in _LOOPBACK


def log_proxy_guard_state() -> None:
    """Un aviso claro al arrancar si el secreto del proxy falta."""
    if INTERNAL_PROXY_SECRET:
        return
    if os.environ.get(ALLOW_UNAUTHENTICATED_LOCAL_ENV, "").strip() == "1" and not os.environ.get("VERCEL"):
        logger.warning(
            "[SEC] INTERNAL_PROXY_SECRET not set; ALLOW_UNAUTHENTICATED_LOCAL=1 lets "
            "loopback requests use the data routes without it (local development only)."
        )
    else:
        logger.error(
            "[SEC] INTERNAL_PROXY_SECRET not set: every data route answers 503 "
            "until it is configured."
        )


# --------------------------------------------------------------- rate limit --
def check_rate_limit() -> tuple[Response, int] | None:
    """
    Rate limiting respaldado por Supabase (tabla rate_limits).
    Funciona en serverless — no usa memoria local.
    Planes pro/team: sin limite. Plan free: 20 busquedas/dia. Anonimo: 3/hora.
    Devuelve None si la request puede continuar, o la respuesta 429.

    Falla cerrado (503) si el cliente admin no esta disponible (sin
    SUPABASE_SERVICE_KEY): mismo patron que proxy_rejection() para el secreto
    del proxy — sin el, ninguna ruta de datos sigue "sin rate limit", que
    dejaria pasar trafico sin control alguno.
    """
    sb = get_supabase_admin()
    if not sb:
        logger.error("[RATE] Supabase admin client unavailable (missing SUPABASE_SERVICE_KEY); failing closed")
        return jsonify({"error": "Service unavailable"}), 503

    # Identity: prefer the proxy-forwarded user id over the (shared) IP so paying
    # users get their own bucket and aren't throttled by other users' traffic (BUG-2).
    uid = request.headers.get("X-User-Id")
    plan = fetch_user_plan(uid) if uid else None
    if plan in ("pro", "team"):
        return None

    ip = uid or request.remote_addr or "unknown"
    now = time.time()

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
