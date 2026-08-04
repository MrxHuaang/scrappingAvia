"""Auth API: sesion Flask respaldada por Supabase Auth (password + Google OAuth)."""

from flask import Blueprint, jsonify, redirect, request, session

from webapp.core import RouteResult, fetch_user_plan, get_supabase, logger

bp = Blueprint("auth", __name__)


def _establish_session(res) -> None:
    """Puebla la sesion Flask desde una respuesta de auth de Supabase."""
    session["access_token"] = res.session.access_token
    meta = res.user.user_metadata or {}
    user_id = str(res.user.id)
    session["user"] = {
        "id": user_id,
        "email": res.user.email,
        "name": meta.get("full_name", ""),
        "picture": meta.get("avatar_url", ""),
    }
    real_plan = fetch_user_plan(user_id)
    session["plan"] = real_plan
    session.modified = True
    logger.info(f"[AUTH] Plan cargado desde Supabase: {real_plan}")


@bp.route("/auth/login", methods=["POST"])
def auth_login() -> RouteResult:
    sb = get_supabase()
    if not sb:
        return jsonify({"error": "Auth no configurado. Configura SUPABASE_URL y SUPABASE_ANON_KEY.", "configured": False}), 200
    try:
        data = request.get_json()
        res = sb.auth.sign_in_with_password({"email": data.get("email", ""), "password": data.get("password", "")})
        _establish_session(res)
        return jsonify({"ok": True, "user": session["user"]})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/auth/register", methods=["POST"])
def auth_register() -> RouteResult:
    sb = get_supabase()
    if not sb:
        return jsonify({"error": "Auth no configurado. Configura SUPABASE_URL y SUPABASE_ANON_KEY.", "configured": False}), 200
    try:
        data = request.get_json()
        sb.auth.sign_up({"email": data.get("email", ""), "password": data.get("password", ""), "options": {"data": {"full_name": data.get("name", "")}}})
        return jsonify({"ok": True, "message": "Revisa tu email para confirmar tu cuenta."})
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@bp.route("/auth/google")
def auth_google() -> RouteResult:
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


@bp.route("/auth/callback")
def auth_callback() -> RouteResult:
    sb = get_supabase()
    if not sb:
        return redirect("/login?error=auth_not_configured")
    code = request.args.get("code")
    if code:
        try:
            res = sb.auth.exchange_code_for_session({"auth_code": code})
            if res.session:
                _establish_session(res)
                next_url = session.pop("auth_next", "/app")
                return redirect(next_url)
        except Exception as e:
            return redirect(f"/login?error={str(e)}")
    return redirect("/login?error=callback_failed")


@bp.route("/auth/logout", methods=["POST"])
def auth_logout() -> RouteResult:
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


@bp.route("/auth/me")
def auth_me() -> RouteResult:
    user = session.get("user")
    if not user:
        return jsonify({"error": "No autenticado"}), 401
    return jsonify({"user": user, "plan": session.get("plan", "free"), "configured": bool(get_supabase())})
