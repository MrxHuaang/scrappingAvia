"""Billing: Stripe checkout, paginas de pago y endpoint de debug gated."""

import os

import stripe
from flask import Blueprint, jsonify, render_template, request, session, current_app

from webapp.core import (
    BASE_URL,
    IS_SERVERLESS,
    STRIPE_PRO_PRICE_ID,
    STRIPE_PUBLISHABLE_KEY,
    STRIPE_TEAM_PRICE_ID,
    STRIPE_WEBHOOK_SECRET,
    RouteResult,
    logger,
    require_auth,
    update_user_plan,
    user_id_from_flask_session,
)

bp = Blueprint("billing", __name__)


@bp.route("/api/stripe/config")
def stripe_config() -> RouteResult:
    return jsonify({
        "publishable_key": STRIPE_PUBLISHABLE_KEY,
        "plans": {
            "pro": {"price_id": STRIPE_PRO_PRICE_ID, "amount": 2900, "name": "Professional"},
            "team": {"price_id": STRIPE_TEAM_PRICE_ID, "amount": 9900, "name": "Team"},
        },
    })


@bp.route("/api/stripe/create-checkout-session", methods=["POST"])
@require_auth
def stripe_create_checkout() -> RouteResult:
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
        user_id = user_id_from_flask_session()
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


def _stripe_debug_endpoint_allowed() -> bool:
    """Endpoints /api/debug/* — Stripe test, ENABLE_STRIPE_DEBUG=1 o ENABLE_DEBUG_API=1."""
    key = os.environ.get("STRIPE_SECRET_KEY") or ""
    if key.startswith("sk_test_"):
        return True
    if os.environ.get("ENABLE_STRIPE_DEBUG") == "1":
        return True
    return os.environ.get("ENABLE_DEBUG_API") == "1"


@bp.route("/api/debug/stripe")
def debug_stripe() -> RouteResult:
    if not _stripe_debug_endpoint_allowed():
        return jsonify({"error": "Not found"}), 404
    return jsonify({
        "stripe_configured": bool(stripe.api_key),
        "webhook_secret_configured": bool(STRIPE_WEBHOOK_SECRET and STRIPE_WEBHOOK_SECRET.strip()),
        "supabase_configured": bool(os.environ.get("SUPABASE_URL")),
        "service_key_configured": bool(os.environ.get("SUPABASE_SERVICE_KEY")),
        "is_serverless": IS_SERVERLESS,
    })


def _stripe_expand_id(value) -> str | None:
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


@bp.route("/api/stripe/webhook", methods=["POST"])
def stripe_webhook() -> RouteResult:
    """
    Webhook de Stripe deprecado en Flask.
    El webhook canonico vive en Next.js: /api/stripe/webhook
    Este endpoint retorna 410 Gone para evitar procesar eventos duplicados.
    Actualiza la URL del webhook en el dashboard de Stripe a:
    https://ad-scrapper.vercel.app/api/stripe/webhook
    """
    return jsonify({"error": "deprecated — use Next.js webhook"}), 410


@bp.route("/payment/success")
def payment_success() -> RouteResult:
    session_id = request.args.get("session_id")
    plan = "pro"  # default

    sk = current_app.secret_key or ""
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
            user_id = metadata.get("user_id") or user_id_from_flask_session()
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
                update_user_plan(user_id, plan, customer_id, subscription_id)

        except Exception as e:
            logger.error(f"[SUCCESS] Error retrieving session: {e}")

    plan_names = {"pro": "Professional", "team": "Team"}
    plan_name = plan_names.get(plan, "Professional")
    return render_template(
        "payment_success.html",
        plan=plan,
        plan_name=plan_name,
    )


@bp.route("/payment/cancel")
def payment_cancel() -> RouteResult:
    return render_template("payment_cancel.html")
