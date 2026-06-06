"""
Zephr - Servidor Web
Interfaz web para busqueda de Airworthiness Directives.
Fuentes: Federal Register (USA), EASA (Europa), Transport Canada, ANAC (Brasil).
"""

import os
import io
import re
import sys
import json
import base64
import time
import logging
import traceback
import zipfile
import threading
import hmac
import socket
import ipaddress
from urllib.parse import urlparse
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor, as_completed

import stripe
import requests as http_requests
from functools import wraps
from flask import Flask, render_template, request, jsonify, send_file, Response, redirect, session
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix
import pandas as pd

# Agregar el directorio raiz al path para importar scrapers
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from scrapers.federal_register import search_ads as fr_search
from scrapers.easa import search_ads as easa_search, search_main_page as easa_main_page
from scrapers.transport_canada import search_ads as tc_search
from scrapers.anac_brazil import search_ads as anac_brazil_search
from scrapers.dgac_chile import search_ads as dgac_chile_search
from scrapers.aerocivil_colombia import search_ads as aerocivil_search
from scrapers.anac_argentina import search_ads as anac_argentina_search
from scrapers.casa_australia import search_ads as casa_search
from scrapers.gcaa_uae import search_ads as gcaa_search
from math import ceil
from utils import map_product_type, run_with_timeout, VALID_SOURCES, MAX_RESULTS_PER_SOURCE


app = Flask(__name__)
app.config['JSON_SORT_KEYS'] = False

# Trust one hop of X-Forwarded-* (Vercel/Next proxy) so request.remote_addr is the
# real client IP, not the proxy IP (BUG-2).
app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

# CORS (SEC-5): never combine wildcard origins with credentials.
# Configure an explicit allowlist via ALLOWED_ORIGINS (comma-separated). If unset,
# allow any origin but WITHOUT credentials (server-to-server proxy calls are unaffected).
_raw_origins = os.environ.get("ALLOWED_ORIGINS", "").strip()
if _raw_origins and _raw_origins != "*":
    ALLOWED_ORIGINS = [o.strip() for o in _raw_origins.split(",") if o.strip()]
    CORS(app, origins=ALLOWED_ORIGINS, supports_credentials=True)
else:
    ALLOWED_ORIGINS = "*"
    CORS(app, origins="*", supports_credentials=False)

APP_VERSION = "1.1.0"

# Shared secret injected by the Next.js proxy (SEC-3). When set, /api/search*,
# /api/export* reject any request lacking the matching X-Internal-Secret header.
INTERNAL_PROXY_SECRET = os.environ.get("INTERNAL_PROXY_SECRET", "")

# Supabase Auth (optional — app works without it)
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY")
SUPABASE_SERVICE_KEY = os.environ.get("SUPABASE_SERVICE_KEY")
IS_SERVERLESS = bool(os.environ.get("VERCEL"))

# Flask session signing key (SEC-6). Fail fast in production instead of falling back
# to a publicly-known constant (which would make session cookies forgeable).
_flask_secret = os.environ.get("FLASK_SECRET_KEY")
if not _flask_secret:
    if IS_SERVERLESS or os.environ.get("FLASK_ENV") == "production":
        raise RuntimeError(
            "FLASK_SECRET_KEY must be set in production. Generate with: "
            "python -c \"import secrets; print(secrets.token_hex(32))\""
        )
    _flask_secret = "dev-secret-change-in-production"
app.secret_key = _flask_secret

# Stripe (optional — works without it)
stripe.api_key = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY", "")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET", "")
STRIPE_PRO_PRICE_ID = os.environ.get("STRIPE_PRO_PRICE_ID", "")
STRIPE_TEAM_PRICE_ID = os.environ.get("STRIPE_TEAM_PRICE_ID", "")
BASE_URL = os.environ.get("BASE_URL", "http://localhost:5000")

logger = logging.getLogger(__name__)

_supabase = None
_supabase_admin = None
# True si get_supabase_admin() construyó el cliente con SUPABASE_SERVICE_KEY (no fallback anon).
_SUPABASE_ADMIN_USES_SERVICE_ROLE = None

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

OUTPUT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "results")
os.makedirs(OUTPUT_DIR, exist_ok=True)

# Timeout máximo por fuente (en segundos)
SCRAPER_TIMEOUT = 45

PDF_DOWNLOAD_TIMEOUT = 15
PDF_MAX_WORKERS = 5
PDF_USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"

# Fuentes que tienen datos precargados en Supabase y deben buscarse primero en la BD.
# Si la búsqueda en BD retorna resultados, no se hace scraping en vivo.
# Si no hay datos en BD (o la fuente no está aquí), se hace scraping en vivo como fallback.
DB_BACKED_SOURCES = frozenset([
    "easa", "federal_register", "transport_canada", "anac_brazil",
])


# ------------------------------------------------------------------
# Supabase DB search
# ------------------------------------------------------------------
_SUPABASE_PAGE_SIZE = 1000  # PostgREST max rows per request

def _build_supabase_query(sb, source, keyword, make, model, product_type, date_from, date_to):
    """Construye el query base de Supabase (sin limit/range)."""
    q = sb.table("ads").select(
        "source, source_label, ad_number, subject, make, model, "
        "product_type, effective_date, status, docket, pdf_link"
    )

    if source != "all":
        q = q.eq("source", source)

    search_term = keyword or ""
    if search_term:
        q = q.or_(
            f"ad_number.ilike.%{search_term}%,"
            f"subject.ilike.%{search_term}%,"
            f"make.ilike.%{search_term}%,"
            f"model.ilike.%{search_term}%"
        )

    if make and make != keyword:
        q = q.ilike("make", f"%{make}%")

    if model:
        q = q.or_(
            f"model.ilike.%{model}%,"
            f"subject.ilike.%{model}%"
        )

    if product_type:
        q = q.ilike("product_type", f"%{product_type}%")

    if date_from:
        q = q.gte("effective_date", date_from)
    if date_to:
        q = q.lte("effective_date", date_to)

    q = q.order("effective_date", desc=True)
    return q


def _search_supabase(source, keyword="", make="", model="", product_type="",
                     date_from="", date_to="", max_results=10000):
    """
    Busca ADs en la tabla 'ads' de Supabase.
    Usa paginación para superar el límite de 1000 rows de PostgREST.
    Retorna lista de dicts con el mismo formato que los scrapers.
    """
    sb = get_supabase_admin()
    if not sb:
        return []

    try:
        all_rows = []
        offset = 0

        while offset < max_results:
            batch_size = min(_SUPABASE_PAGE_SIZE, max_results - offset)
            q = _build_supabase_query(sb, source, keyword, make, model, product_type, date_from, date_to)
            q = q.range(offset, offset + batch_size - 1)

            resp = q.execute()
            rows = resp.data or []
            all_rows.extend(rows)

            # Si recibimos menos de lo pedido, no hay más datos
            if len(rows) < batch_size:
                break

            offset += len(rows)

        # Map to scraper output format (PascalCase keys)
        source_labels = {
            "easa": "EASA",
            "federal_register": "FAA (Federal Register)",
            "transport_canada": "Transport Canada",
            "anac_brazil": "ANAC Brasil",
            "dgac_chile": "DGAC Chile",
            "anac_argentina": "ANAC Argentina",
            "aerocivil_colombia": "AEROCIVIL Colombia",
            "casa_australia": "CASA Australia",
            "gcaa_uae": "GCAA UAE",
        }

        results = []
        for row in all_rows:
            results.append({
                "Source": source_labels.get(row.get("source", ""), row.get("source_label", "")),
                "AD_Number": row.get("ad_number", ""),
                "Subject": row.get("subject", "") or "",
                "Make": row.get("make", "") or "",
                "Model": row.get("model", "") or "",
                "Effective_Date": row.get("effective_date", "") or "",
                "Status": row.get("status", "") or "",
                "Product_Type": row.get("product_type", "") or "",
                "Docket": row.get("docket", "") or "",
                "PDF_Link": row.get("pdf_link", "") or "",
            })

        logger.info(f"[DB] {source}: {len(results)} resultados desde Supabase")
        return results

    except Exception as e:
        logger.error(f"[DB] Error buscando en Supabase ({source}): {e}")
        return []


# ------------------------------------------------------------------
# Auth helpers
# ------------------------------------------------------------------
def require_auth(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not session.get("access_token"):
            next_url = request.path
            return redirect(f"/login?next={next_url}")
        return f(*args, **kwargs)
    return decorated


def require_proxy(f):
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


def check_rate_limit():
    """
    Rate limiting respaldado por Supabase (tabla rate_limits).
    Funciona en serverless — no usa memoria local.
    Planes pro/team: sin limite.
    Plan free: 20 busquedas/dia.
    Anonimo: 3 busquedas/hora.
    """
    # Identity: prefer the proxy-forwarded user id over the (shared) IP so paying
    # users get their own bucket and aren't throttled by other users' traffic (BUG-2).
    uid = request.headers.get("X-User-Id")
    plan = session.get("plan")
    if not plan and uid:
        plan = _fetch_user_plan(uid)
    if plan in ("pro", "team"):
        return None

    ip = uid or request.remote_addr or "unknown"
    now = time.time()

    sb = get_supabase_admin()
    if not sb:
        # Sin Supabase no podemos verificar — permitir con warning
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

        # Contar requests recientes de esta IP en la ventana
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

        # Registrar este request
        sb.table("rate_limits").insert({
            "ip": ip,
            "plan": plan or "anonymous",
            "requested_at": datetime.fromtimestamp(now, tz=timezone.utc).isoformat(),
        }).execute()

    except Exception as e:
        logger.warning(f"[RATE] Error en rate limit check: {e} — permitiendo request")

    return None


# ------------------------------------------------------------------
# Paginas
# ------------------------------------------------------------------
@app.route("/")
def landing():
    return render_template("landing.html")


@app.route("/app")
@require_auth
def index():
    return render_template("index.html")


@app.route("/login")
def login_page():
    return render_template("auth.html", mode="login")


@app.route("/register")
def register_page():
    return render_template("auth.html", mode="register")


# ------------------------------------------------------------------
# Auth API
# ------------------------------------------------------------------
@app.route("/auth/login", methods=["POST"])
def auth_login():
    sb = get_supabase()
    if not sb:
        return jsonify({"error": "Auth no configurado. Configura SUPABASE_URL y SUPABASE_ANON_KEY.", "configured": False}), 200
    try:
        data = request.get_json()
        res = sb.auth.sign_in_with_password({"email": data.get("email", ""), "password": data.get("password", "")})
        session["access_token"] = res.session.access_token
        meta = res.user.user_metadata or {}
        user_id = str(res.user.id)
        session["user"] = {
            "id": user_id,
            "email": res.user.email,
            "name": meta.get("full_name", ""),
            "picture": meta.get("avatar_url", ""),
        }
        real_plan = _fetch_user_plan(user_id)
        session["plan"] = real_plan
        session.modified = True
        logger.info(f"[AUTH] Plan cargado desde Supabase: {real_plan}")
        return jsonify({"ok": True, "user": session["user"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/auth/register", methods=["POST"])
def auth_register():
    sb = get_supabase()
    if not sb:
        return jsonify({"error": "Auth no configurado. Configura SUPABASE_URL y SUPABASE_ANON_KEY.", "configured": False}), 200
    try:
        data = request.get_json()
        res = sb.auth.sign_up({"email": data.get("email", ""), "password": data.get("password", ""), "options": {"data": {"full_name": data.get("name", "")}}})
        return jsonify({"ok": True, "message": "Revisa tu email para confirmar tu cuenta."})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.route("/auth/google")
def auth_google():
    sb = get_supabase()
    if not sb:
        return jsonify({"error": "Auth no configurado.", "configured": False}), 200
    try:
        next_url = request.args.get("next", "/app")
        session["auth_next"] = next_url
        res = sb.auth.sign_in_with_oauth({"provider": "google", "options": {"redirect_to": request.url_root.rstrip("/") + "/auth/callback"}})
        return redirect(res.url)
    except Exception as e:
        return redirect(f"/login?error={str(e)}")


@app.route("/auth/callback")
def auth_callback():
    sb = get_supabase()
    if not sb:
        return redirect("/login?error=auth_not_configured")
    code = request.args.get("code")
    if code:
        try:
            res = sb.auth.exchange_code_for_session({"auth_code": code})
            if res.session:
                session["access_token"] = res.session.access_token
                meta = res.user.user_metadata or {}
                user_id = str(res.user.id)
                session["user"] = {
                    "id": user_id,
                    "email": res.user.email,
                    "name": meta.get("full_name", ""),
                    "picture": meta.get("avatar_url", ""),
                }
                real_plan = _fetch_user_plan(user_id)
                session["plan"] = real_plan
                session.modified = True
                logger.info(f"[AUTH] Plan cargado desde Supabase: {real_plan}")
                next_url = session.pop("auth_next", "/app")
                return redirect(next_url)
        except Exception as e:
            return redirect(f"/login?error={str(e)}")
    return redirect("/login?error=callback_failed")


@app.route("/auth/logout", methods=["POST"])
def auth_logout():
    token = session.get("access_token")
    session.clear()
    if token:
        sb = get_supabase()
        if sb:
            try:
                sb.auth.sign_out()
            except Exception:
                pass
    return jsonify({"ok": True})


@app.route("/auth/me")
def auth_me():
    user = session.get("user")
    if not user:
        return jsonify({"error": "No autenticado"}), 401
    return jsonify({"user": user, "plan": session.get("plan", "free"), "configured": bool(get_supabase())})


# ------------------------------------------------------------------
# Stripe — Billing
# ------------------------------------------------------------------
def _jwt_sub_unverified(token):
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


def _user_id_from_flask_session():
    """
    user_id para metadata de Stripe: dict session['user'] o claims del access_token.
    """
    user = session.get("user") or {}
    uid = user.get("id") or user.get("sub") or user.get("user_id") or user.get("uid")
    if uid:
        s = str(uid).strip()
        return s if s else None
    return _jwt_sub_unverified(session.get("access_token"))


def _fetch_user_plan(user_id):
    """Consulta el plan del usuario desde la tabla profiles en Supabase."""
    sb = get_supabase_admin()
    if not sb:
        return "free"
    try:
        res = sb.table("profiles").select("plan").eq("id", user_id).maybe_single().execute()
        return res.data["plan"] if res.data and res.data.get("plan") else "free"
    except Exception as e:
        print(f"[Stripe] Error fetching plan for {user_id}: {e}")
        return "free"


def _update_user_plan(user_id, plan, customer_id=None, subscription_id=None):
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


def _downgrade_to_free_by_customer(customer_id):
    sb = get_supabase_admin()
    if not sb:
        return
    try:
        res = sb.table("profiles").select("id").eq("stripe_customer_id", customer_id).maybe_single().execute()
        if res.data:
            _update_user_plan(res.data["id"], "free")
    except Exception as e:
        print(f"[Stripe] Error downgrading customer {customer_id}: {e}")


@app.route("/api/stripe/config")
def stripe_config():
    return jsonify({
        "publishable_key": STRIPE_PUBLISHABLE_KEY,
        "plans": {
            "pro": {"price_id": STRIPE_PRO_PRICE_ID, "amount": 2900, "name": "Professional"},
            "team": {"price_id": STRIPE_TEAM_PRICE_ID, "amount": 9900, "name": "Team"},
        },
    })


@app.route("/api/stripe/create-checkout-session", methods=["POST"])
@require_auth
def stripe_create_checkout():
    if not stripe.api_key:
        return jsonify({"error": "Stripe no configurado"}), 500
    try:
        data = request.get_json() or {}
        plan = data.get("plan", "pro")
        if plan not in ("pro", "team"):
            plan = "pro"

        logger.info(f"[CHECKOUT] user en sesión: {session.get('user')}")
        logger.info(f"[CHECKOUT] plan solicitado: {plan}")

        user = session.get("user") or {}
        user_id = _user_id_from_flask_session()
        logger.info(f"[CHECKOUT] user_id extraído: {user_id!r}")

        if not user_id:
            logger.error("[CHECKOUT] No se pudo obtener user_id de la sesión")
            return jsonify({"error": "usuario no identificado"}), 401

        price_id = STRIPE_PRO_PRICE_ID if plan == "pro" else STRIPE_TEAM_PRICE_ID
        if not price_id:
            return jsonify({"error": f"Price ID no configurado para plan '{plan}'"}), 500

        meta_user_id = str(user_id)
        meta_plan = str(plan)
        logger.info(f"[CHECKOUT] Creando sesión con metadata: user_id={meta_user_id} plan={meta_plan}")

        checkout = stripe.checkout.Session.create(
            customer_email=user.get("email"),
            payment_method_types=["card"],
            line_items=[{"price": price_id, "quantity": 1}],
            mode="subscription",
            success_url=BASE_URL + "/payment/success?session_id={CHECKOUT_SESSION_ID}",
            cancel_url=BASE_URL + "/payment/cancel",
            metadata={
                "user_id": meta_user_id,
                "plan": meta_plan,
            },
        )
        return jsonify({"checkout_url": checkout.url})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


def _stripe_debug_endpoint_allowed():
    """Endpoints /api/debug/* — Stripe test, ENABLE_STRIPE_DEBUG=1 o ENABLE_DEBUG_API=1."""
    key = os.environ.get("STRIPE_SECRET_KEY") or ""
    if key.startswith("sk_test_"):
        return True
    if os.environ.get("ENABLE_STRIPE_DEBUG") == "1":
        return True
    return os.environ.get("ENABLE_DEBUG_API") == "1"


@app.route("/api/debug/stripe")
def debug_stripe():
    if not _stripe_debug_endpoint_allowed():
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "stripe_configured": bool(stripe.api_key),
        "webhook_secret_configured": bool(STRIPE_WEBHOOK_SECRET and STRIPE_WEBHOOK_SECRET.strip()),
        "supabase_configured": bool(os.environ.get("SUPABASE_URL")),
        "service_key_configured": bool(os.environ.get("SUPABASE_SERVICE_KEY")),
        "is_serverless": IS_SERVERLESS,
    })


def _stripe_event_type(event):
    """Stripe devuelve dict o StripeObject según versión."""
    try:
        return event["type"]
    except Exception:
        return getattr(event, "type", None)


def _stripe_object_keys(obj):
    """Evita crash al loguear (StripeObject no siempre se comporta como dict)."""
    try:
        if isinstance(obj, dict):
            return list(obj.keys())
        if hasattr(obj, "keys"):
            return list(obj.keys())
    except Exception:
        pass
    return f"<{type(obj).__name__}>"


def _stripe_expand_id(value):
    """customer/subscription pueden ser id str o objeto expandido."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, dict) and value.get("id"):
        return value["id"]
    try:
        return getattr(value, "id", None) or str(value)
    except Exception:
        return str(value)


def _stripe_get(obj, key, default=None):
    if obj is None:
        return default
    try:
        if hasattr(obj, "get"):
            return obj.get(key, default)
        return obj[key]
    except Exception:
        return default


@app.route("/api/stripe/webhook", methods=["POST"])
def stripe_webhook():
    """
    Webhook de Stripe deprecado en Flask.
    El webhook canonico vive en Next.js: /api/stripe/webhook
    Este endpoint retorna 410 Gone para evitar procesar eventos duplicados.
    Actualiza la URL del webhook en el dashboard de Stripe a:
    https://ad-scrapper.vercel.app/api/stripe/webhook
    """
    return jsonify({"error": "deprecated — use Next.js webhook"}), 410


@app.route("/payment/success")
def payment_success():
    session_id = request.args.get("session_id")
    plan = "pro"  # default

    sk = app.secret_key or ""
    if not sk or sk == "dev-secret-change-in-production":
        logger.warning(
            "[SUCCESS] FLASK_SECRET_KEY ausente o valor por defecto: "
            "la cookie de sesión puede no persistir bien entre requests (p. ej. en Vercel)."
        )

    if session_id and stripe.api_key:
        try:
            logger.info(f"[SUCCESS] session_id recibido: {session_id}")
            checkout_data = stripe.checkout.Session.retrieve(session_id)
            # Mismo enfoque que el webhook: dict plano, no StripeObject
            checkout_dict = (
                checkout_data.to_dict()
                if hasattr(checkout_data, "to_dict")
                else dict(checkout_data)
            )
            metadata = checkout_dict.get("metadata") or {}
            if not isinstance(metadata, dict):
                metadata = {}
            _raw_plan = metadata.get("plan")
            if isinstance(_raw_plan, str):
                plan = _raw_plan.strip() or "pro"
            else:
                plan = _raw_plan or "pro"
            if plan not in ("pro", "team"):
                plan = "pro"
            user_id = metadata.get("user_id") or _user_id_from_flask_session()
            if isinstance(user_id, str):
                user_id = user_id.strip() or None
            customer_id = _stripe_expand_id(checkout_dict.get("customer"))
            subscription_id = _stripe_expand_id(checkout_dict.get("subscription"))

            logger.info(f"[SUCCESS] metadata (dict): {metadata}")
            logger.info(f"[SUCCESS] plan extraído: {plan}")
            logger.info(f"[SUCCESS] user_id: {user_id}")

            session["plan"] = plan
            session.modified = True
            logger.info(f"[SUCCESS] Plan actualizado en sesión: {plan}")

            if user_id:
                _update_user_plan(user_id, plan, customer_id, subscription_id)

        except Exception as e:
            logger.error(f"[SUCCESS] Error retrieving session: {e}")

    plan_names = {"pro": "Professional", "team": "Team"}
    plan_name = plan_names.get(plan, "Professional")
    return render_template(
        "payment_success.html",
        plan=plan,
        plan_name=plan_name,
    )


@app.route("/payment/cancel")
def payment_cancel():
    return render_template("payment_cancel.html")


# ------------------------------------------------------------------
# Error handlers
# ------------------------------------------------------------------
@app.errorhandler(404)
def page_not_found(e):
    return render_template("404.html"), 404


# ------------------------------------------------------------------
# Helpers para construir las funciones de cada scraper
# ------------------------------------------------------------------

def _make_db_search_fn(src_key, keyword, make, model, product_type, date_from, date_to, max_results, live_fn=None):
    """
    Crea una función que busca primero en Supabase.
    Si Supabase retorna resultados, los usa.  Si no, intenta live_fn como fallback.
    """
    def _db_search():
        db_results = _search_supabase(
            source=src_key,
            keyword=keyword,
            make=make,
            model=model,
            product_type=product_type,
            date_from=date_from,
            date_to=date_to,
            max_results=max_results,
        )
        if db_results:
            return db_results
        # Fallback al scraper en vivo si BD no retorna nada
        if live_fn:
            logger.info(f"[DB] {src_key}: 0 resultados en BD, intentando scraping en vivo...")
            return live_fn()
        return []
    return _db_search


def _build_scrapers(source, keyword, make, model, product_type, date_from, date_to, max_results=20000):
    """Devuelve lista de (source_key, label, fn) a ejecutar."""
    scrapers = []
    fr_max_pages = ceil(max_results / 100)

    if source in ("federal_register", "all"):
        def _fr_live():
            return fr_search(
                keyword=keyword or None,
                make=make or None,
                model=model or None,
                product_type=product_type or None,
                date_from=date_from or None,
                date_to=date_to or None,
                max_pages=fr_max_pages,
            )
        fn = _make_db_search_fn("federal_register", keyword, make, model,
                                product_type, date_from, date_to, max_results, _fr_live)
        scrapers.append(("federal_register", "Federal Register", fn))

    if source in ("easa", "all"):
        _easa_pages = max(2, ceil(max_results / 100))
        def _easa_live():
            search_term = keyword or make or model
            if search_term:
                return easa_search(model_keyword=search_term, max_pages=_easa_pages)
            else:
                return easa_main_page(max_pages=2)
        fn = _make_db_search_fn("easa", keyword, make, model,
                                product_type, date_from, date_to, max_results, _easa_live)
        scrapers.append(("easa", "EASA", fn))

    if source in ("anac_brazil", "all"):
        _anac_limit = min(max_results, 2000)
        def _anac_live():
            return anac_brazil_search(
                keyword=keyword or None,
                make=make or None,
                model=model or None,
                date_from=date_from or None,
                date_to=date_to or None,
                max_results=_anac_limit,
            )
        fn = _make_db_search_fn("anac_brazil", keyword, make, model,
                                product_type, date_from, date_to, max_results, _anac_live)
        scrapers.append(("anac_brazil", "ANAC Brasil", fn))

    if source in ("transport_canada", "all"):
        if make:
            def _tc_live():
                return tc_search(
                    manufacturer_name=make,
                    model_name=model or None,
                    search_type=map_product_type(product_type),
                )
            fn = _make_db_search_fn("transport_canada", keyword, make, model,
                                    product_type, date_from, date_to, max_results, _tc_live)
            scrapers.append(("transport_canada", "Transport Canada", fn))
        else:
            # Sin make: buscar solo en BD (TC live requiere fabricante)
            fn = _make_db_search_fn("transport_canada", keyword, make, model,
                                    product_type, date_from, date_to, max_results, None)
            scrapers.append(("transport_canada", "Transport Canada", fn))

    if source in ("dgac_chile", "all"):
        def _dgac():
            return dgac_chile_search(
                keyword=keyword or None,
                make=make or None,
                model=model or None,
                date_from=date_from or None,
                date_to=date_to or None,
                max_results=min(max_results, 200),
            )
        scrapers.append(("dgac_chile", "DGAC Chile", _dgac))

    if source == "aerocivil_colombia":
        def _aerocivil():
            return aerocivil_search(
                keyword=keyword or None,
                make=make or None,
                model=model or None,
                date_from=date_from or None,
                date_to=date_to or None,
                max_results=min(max_results, 200),
            )
        scrapers.append(("aerocivil_colombia", "AEROCIVIL Colombia", _aerocivil))

    if source in ("anac_argentina", "all"):
        def _anac_ar():
            return anac_argentina_search(
                keyword=keyword or None,
                make=make or None,
                model=model or None,
                date_from=date_from or None,
                date_to=date_to or None,
                max_results=min(max_results, 200),
            )
        scrapers.append(("anac_argentina", "ANAC Argentina", _anac_ar))

    if source == "casa_australia":
        def _casa():
            return casa_search(
                keyword=keyword or None,
                make=make or None,
                model=model or None,
                date_from=date_from or None,
                date_to=date_to or None,
                max_results=min(max_results, 200),
            )
        scrapers.append(("casa_australia", "CASA Australia", _casa))

    if source == "gcaa_uae":
        def _gcaa():
            return gcaa_search(
                keyword=keyword or None,
                make=make or None,
                model=model or None,
                date_from=date_from or None,
                date_to=date_to or None,
                max_results=min(max_results, 200),
            )
        scrapers.append(("gcaa_uae", "GCAA UAE", _gcaa))

    return scrapers


# ------------------------------------------------------------------
# API: Environment info
# ------------------------------------------------------------------
@app.route("/api/env")
def api_env():
    return jsonify({"serverless": IS_SERVERLESS, "plan": session.get("plan", "anonymous")})


# ------------------------------------------------------------------
# API: Health check
# ------------------------------------------------------------------
@app.route("/api/health")
def api_health():
    return jsonify({
        "status": "ok",
        "version": APP_VERSION,
        "sources": ["federal_register", "easa", "transport_canada", "anac_brazil", "dgac_chile", "aerocivil_colombia", "anac_argentina", "casa_australia", "gcaa_uae"],
        "timestamp": datetime.now(timezone.utc).isoformat(),
    })


# ------------------------------------------------------------------
# API: Busqueda de ADs (POST — compatibilidad)
# ------------------------------------------------------------------
@app.route("/api/search", methods=["POST"])
@require_proxy
def api_search():
    rl = check_rate_limit()
    if rl:
        return rl
    try:
        data = request.get_json()
        source = data.get("source", "federal_register")
        keyword = data.get("keyword", "").strip()
        make = data.get("make", "").strip()
        model = data.get("model", "").strip()
        product_type = data.get("product_type", "").strip()
        date_from = data.get("date_from", "").strip()
        date_to = data.get("date_to", "").strip()
        max_results = min(int(data.get("max_results", MAX_RESULTS_PER_SOURCE)), 20000)

        if source not in VALID_SOURCES:
            return jsonify({"error": f"Fuente no válida: '{source}'."}), 400
        if source == "all" and not keyword and not make and not model:
            return jsonify({"error": "Ingresa al menos un término para buscar en todas las fuentes"}), 400

        all_results = []
        errors = []

        scrapers = _build_scrapers(source, keyword, make, model, product_type, date_from, date_to, max_results=max_results)

        if source == "transport_canada" and not make:
            errors.append("Transport Canada requiere un fabricante.")

        for _src_key, label, fn in scrapers:
            results, err = run_with_timeout(fn, SCRAPER_TIMEOUT, label, max_results=max_results)
            if err:
                errors.append(err)
            if results:
                all_results.extend(results)

        return jsonify({
            "results": all_results,
            "count": len(all_results),
            "errors": errors,
        })

    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ------------------------------------------------------------------
# API: Busqueda SSE (GET — streaming)
# ------------------------------------------------------------------
@app.route("/api/search/stream")
@require_proxy
def api_search_stream():
    rl = check_rate_limit()
    if rl:
        return rl
    source = request.args.get("source", "federal_register")
    keyword = request.args.get("keyword", "").strip()
    make = request.args.get("make", "").strip()
    model = request.args.get("model", "").strip()
    product_type = request.args.get("product_type", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    max_results = min(int(request.args.get("max_results", MAX_RESULTS_PER_SOURCE)), 20000)

    # Validación de source
    if source not in VALID_SOURCES:
        def error_gen():
            event = {"source": source, "results": [], "count": 0,
                     "error": f"Fuente no válida: '{source}'. Opciones: {', '.join(sorted(VALID_SOURCES - {'all'}))}"}
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'event': 'done', 'total': 0})}\n\n"
        return Response(error_gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    # Validación de inputs: "all" sin términos no está permitido; fuentes específicas sí pueden ir sin filtro
    if not keyword and not make and not model and source == "all":
        def empty_gen():
            event = {"source": source, "results": [], "count": 0,
                     "error": "Ingresa al menos un término para buscar en todas las fuentes"}
            yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps({'event': 'done', 'total': 0})}\n\n"
        return Response(empty_gen(), mimetype="text/event-stream",
                        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    scrapers = _build_scrapers(source, keyword, make, model, product_type, date_from, date_to, max_results=max_results)

    def generate():
        total = 0
        try:
            if source in ("transport_canada", "all") and not make and source == "transport_canada":
                event = {
                    "source": "transport_canada",
                    "results": [],
                    "count": 0,
                    "error": "Transport Canada requiere un fabricante."
                }
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            for src_key, label, fn in scrapers:
                results, err = run_with_timeout(fn, SCRAPER_TIMEOUT, label, max_results=max_results)
                count = len(results) if results else 0
                total += count

                event = {
                    "source": src_key,
                    "results": results if results else [],
                    "count": count,
                    "error": err,
                }
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"

            yield f"data: {json.dumps({'event': 'done', 'total': total})}\n\n"
        except Exception as e:
            yield f"data: {json.dumps({'event': 'error', 'message': str(e)})}\n\n"
        finally:
            yield f"data: {json.dumps({'event': 'done', 'total': total})}\n\n"

    return Response(
        generate(),
        mimetype="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


# ------------------------------------------------------------------
# API: Exportar resultados
# ------------------------------------------------------------------
def _build_dataframe(results):
    """Construye un DataFrame ordenado a partir de resultados."""
    if not results:
        return pd.DataFrame()
    df = pd.DataFrame(results)
    col_order = [
        "Source", "AD_Number", "Subject", "Make", "Model",
        "Effective_Date", "Status", "Product_Type", "Docket", "PDF_Link"
    ]
    existing_cols = [c for c in col_order if c in df.columns]
    extra_cols = [c for c in df.columns if c not in col_order]
    return df[existing_cols + extra_cols]


def _safe_filename(name):
    """Sanitiza un nombre para usar como archivo."""
    if not name:
        return "unknown"
    return re.sub(r'[<>:"/\\|?*]', '_', name).strip()


MAX_PDF_BYTES = 25 * 1024 * 1024  # 25 MB hard cap per PDF

_PDF_ALLOWED_SUFFIXES = (
    ".faa.gov", ".easa.europa.eu", ".tc.canada.ca", ".tc.gc.ca", ".gov.br",
    ".anac.gov.br", ".gcaa.gov.ae", ".dgac.gob.cl", ".anac.gov.ar",
    ".casa.gov.au", ".aerocivil.gov.co", ".federalregister.gov",
    ".govinfo.gov", ".argentina.gob.ar",
)
_PDF_ALLOWED_EXACT = {
    "faa.gov", "easa.europa.eu", "tc.canada.ca", "tc.gc.ca",
    "gcaa.gov.ae", "federalregister.gov",
}


def _host_resolves_private(host):
    """True if the host resolves to any private/loopback/link-local/reserved IP (SSRF guard)."""
    try:
        infos = socket.getaddrinfo(host, None)
    except Exception:
        return True
    for info in infos:
        try:
            addr = ipaddress.ip_address(info[4][0])
        except ValueError:
            return True
        if (addr.is_private or addr.is_loopback or addr.is_link_local
                or addr.is_reserved or addr.is_multicast or addr.is_unspecified):
            return True
    return False


def _is_safe_pdf_url(url):
    """Only allow http(s) URLs on known regulator domains that don't resolve to private IPs."""
    if not url or not isinstance(url, str):
        return False
    try:
        u = urlparse(url)
    except Exception:
        return False
    if u.scheme not in ("http", "https"):
        return False
    host = (u.hostname or "").lower()
    if not host:
        return False
    allowed = host in _PDF_ALLOWED_EXACT or any(host.endswith(s) for s in _PDF_ALLOWED_SUFFIXES)
    if not allowed:
        return False
    return not _host_resolves_private(host)


def _download_pdf(url, timeout=PDF_DOWNLOAD_TIMEOUT):
    """Descarga un PDF de una fuente permitida y devuelve (bytes, None) o (None, error_msg)."""
    if not _is_safe_pdf_url(url):
        return None, "URL no permitida"
    try:
        resp = http_requests.get(
            url,
            timeout=timeout,
            headers={"User-Agent": PDF_USER_AGENT},
            allow_redirects=False,  # block redirect-based SSRF
            stream=True,
        )
        if resp.status_code != 200:
            return None, f"HTTP {resp.status_code}"
        chunks = []
        total = 0
        for chunk in resp.iter_content(8192):
            total += len(chunk)
            if total > MAX_PDF_BYTES:
                return None, "Archivo demasiado grande"
            chunks.append(chunk)
        content = b"".join(chunks)
        if len(content) < 100:
            return None, "Archivo vacío o inválido"
        return content, None
    except http_requests.Timeout:
        return None, f"Timeout ({timeout}s)"
    except Exception as e:
        return None, str(e)


@app.route("/api/export/<fmt>", methods=["POST"])
@require_proxy
def api_export(fmt):
    rl = check_rate_limit()
    if rl:
        return rl
    try:
        data = request.get_json()
        results = data.get("results", [])

        if not results:
            return jsonify({"error": "No hay resultados para exportar."}), 400

        df = _build_dataframe(results)
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        if fmt == "csv":
            output = io.BytesIO()
            df.to_csv(output, index=False, encoding="utf-8-sig")
            output.seek(0)
            return send_file(
                output, mimetype="text/csv",
                as_attachment=True, download_name=f"ad_results_{timestamp}.csv",
            )

        elif fmt == "excel":
            output = io.BytesIO()
            df.to_excel(output, index=False, engine="openpyxl")
            output.seek(0)
            return send_file(
                output,
                mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                as_attachment=True, download_name=f"ad_results_{timestamp}.xlsx",
            )

        elif fmt == "zip":
            return _export_zip(results, df, timestamp, data.get("filters"))

        else:
            return jsonify({"error": f"Formato no soportado: {fmt}"}), 400

    except Exception as e:
        traceback.print_exc()
        return jsonify({"error": str(e)}), 500


def _export_zip(results, df, timestamp, filters=None):
    """Genera un ZIP con CSV + XLSX + PDFs descargados + README.txt."""
    zip_buffer = io.BytesIO()

    # Descargar PDFs en paralelo
    pdf_tasks = []
    for r in results:
        link = r.get("PDF_Link")
        ad_num = r.get("AD_Number", "unknown")
        if link:
            pdf_tasks.append((ad_num, link))

    pdf_results = {}  # ad_num -> (bytes|None, error|None)

    if pdf_tasks:
        with ThreadPoolExecutor(max_workers=PDF_MAX_WORKERS) as executor:
            futures = {}
            for ad_num, link in pdf_tasks:
                safe_name = _safe_filename(ad_num)
                future = executor.submit(_download_pdf, link)
                futures[future] = (safe_name, link)

            for future in as_completed(futures):
                safe_name, link = futures[future]
                try:
                    content, error = future.result()
                    pdf_results[safe_name] = (content, error, link)
                except Exception as e:
                    pdf_results[safe_name] = (None, str(e), link)

    pdfs_ok = sum(1 for _, (c, e, _) in pdf_results.items() if c is not None)
    pdfs_fail = sum(1 for _, (c, e, _) in pdf_results.items() if c is None)

    with zipfile.ZipFile(zip_buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        # CSV
        csv_buf = io.BytesIO()
        df.to_csv(csv_buf, index=False, encoding="utf-8-sig")
        zf.writestr("ad_results.csv", csv_buf.getvalue())

        # XLSX
        xlsx_buf = io.BytesIO()
        df.to_excel(xlsx_buf, index=False, engine="openpyxl")
        zf.writestr("ad_results.xlsx", xlsx_buf.getvalue())

        # PDFs
        for safe_name, (content, error, link) in pdf_results.items():
            if content:
                zf.writestr(f"PDFs/{safe_name}.pdf", content)
            else:
                error_text = f"No se pudo descargar el PDF.\nURL: {link}\nError: {error}\n"
                zf.writestr(f"PDFs/{safe_name}_NO_DISPONIBLE.txt", error_text)

        # README
        readme_lines = [
            "Zephr - Export",
            "=" * 50,
            f"Fecha de exportacion: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}",
            f"Total de ADs: {len(df)}",
            f"PDFs descargados: {pdfs_ok}",
            f"PDFs no disponibles: {pdfs_fail}",
            "",
        ]
        if filters:
            readme_lines.append("Filtros utilizados:")
            for k, v in filters.items():
                if v:
                    readme_lines.append(f"  {k}: {v}")
        else:
            readme_lines.append("Filtros: (sin filtros registrados)")

        readme_lines += [
            "",
            "Archivos incluidos:",
            "  ad_results.csv    - Formato CSV (UTF-8 BOM)",
            "  ad_results.xlsx   - Formato Excel",
            f"  PDFs/             - {pdfs_ok} PDFs descargados",
            "  README.txt        - Este archivo",
            "",
            "Fuentes activas (búsqueda múltiple): FAA | EASA | TC | ANAC BR | DGAC CL | ANAC AR",
        ]
        zf.writestr("README.txt", "\n".join(readme_lines))

    zip_buffer.seek(0)

    n_docs = len(results)
    download_name = f"AD_Export_{timestamp}_{n_docs}_documentos.zip"

    return send_file(
        zip_buffer, mimetype="application/zip",
        as_attachment=True, download_name=download_name,
    )


# ------------------------------------------------------------------
# API: Estado de las fuentes (ping real)
# ------------------------------------------------------------------
_SOURCE_PING_URLS = {
    "federal_register": ("Federal Register (USA)", "https://www.federalregister.gov"),
    "easa":             ("EASA (Europa)",          "https://ad.easa.europa.eu"),
    "transport_canada": ("Transport Canada",       "https://tc.canada.ca"),
    "anac_brazil":      ("ANAC (Brasil)",          "https://sistemas.anac.gov.br"),
    "dgac_chile":       ("DGAC (Chile)",           "https://www.dgac.gob.cl"),
    "anac_argentina":   ("ANAC (Argentina)",       "https://www.argentina.gob.ar"),
}

def _ping_source(key):
    name, url = _SOURCE_PING_URLS[key]
    try:
        resp = http_requests.head(
            url, timeout=3, allow_redirects=True,
            headers={"User-Agent": "Mozilla/5.0 Zephr-HealthCheck/1.0"},
        )
        status = "online" if resp.status_code < 400 else "error"
    except Exception:
        status = "error"
    return key, name, status

@app.route("/api/status")
def api_status():
    results = {}
    with ThreadPoolExecutor(max_workers=len(_SOURCE_PING_URLS)) as executor:
        futures = [executor.submit(_ping_source, k) for k in _SOURCE_PING_URLS]
        for future in as_completed(futures):
            try:
                key, name, status = future.result(timeout=5)
                results[key] = {"name": name, "status": status}
            except Exception:
                pass

    for key in _SOURCE_PING_URLS:
        if key not in results:
            name = _SOURCE_PING_URLS[key][0]
            results[key] = {"name": name, "status": "unknown"}

    return jsonify({"sources": results})


# ------------------------------------------------------------------
# Entry point
# ------------------------------------------------------------------
if __name__ == "__main__":
    print("\n" + "=" * 55)
    print("  Zephr - Airworthiness Directives")
    print("  Fuentes activas: FAA | EASA | TC | ANAC BR | DGAC CL | ANAC AR")
    print("=" * 55)
    print(f"\n  Landing:  http://127.0.0.1:5000/")
    print(f"  App:      http://127.0.0.1:5000/app")
    print(f"  Health:   http://127.0.0.1:5000/api/health\n")

    app.run(host="127.0.0.1", port=5000, debug=os.environ.get("FLASK_DEBUG") == "1")
