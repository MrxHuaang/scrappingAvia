"""Paginas HTML: landing, app autenticada, login/registro."""

from flask import Blueprint, render_template

from webapp.core import RouteResult, require_auth

bp = Blueprint("pages", __name__)


@bp.route("/")
def landing() -> RouteResult:
    return render_template("landing.html")


@bp.route("/app")
@require_auth
def index() -> RouteResult:
    return render_template("index.html")


@bp.route("/login")
def login_page() -> RouteResult:
    return render_template("auth.html", mode="login")


@bp.route("/register")
def register_page() -> RouteResult:
    return render_template("auth.html", mode="register")
