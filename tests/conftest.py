"""
Fixture de la suite de contrato HTTP (Fase 3b).

La app se importa con TODOS los servicios externos vacios (Supabase, Stripe,
proxy secret): el comportamiento "sin configurar" es parte del contrato y es
exactamente lo que corre en CI, donde no hay secretos. Los valores se vacian
ANTES de importar app porque sus constantes se leen a nivel de modulo.
"""

import os
import sys

# Antes de importar app: entorno limpio y determinista.
for var in (
    "SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_KEY",
    "STRIPE_SECRET_KEY", "STRIPE_PUBLISHABLE_KEY", "STRIPE_WEBHOOK_SECRET",
    "STRIPE_PRO_PRICE_ID", "STRIPE_TEAM_PRICE_ID",
    "INTERNAL_PROXY_SECRET", "ALLOWED_ORIGINS",
    "ENABLE_DEBUG_API", "ENABLE_STRIPE_DEBUG",
    "FLASK_ENV", "VERCEL",
):
    os.environ.pop(var, None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import app as app_module  # noqa: E402
from webapp import core  # noqa: E402


@pytest.fixture()
def client():
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture()
def authed_client(client):
    """Cliente con una sesion Flask valida (token + user), sin Supabase real."""
    with client.session_transaction() as s:
        s["access_token"] = "test-token"
        s["user"] = {"id": "00000000-0000-0000-0000-000000000001",
                     "email": "test@example.com", "name": "Test", "picture": ""}
        s["plan"] = "free"
    return client


@pytest.fixture()
def proxied(monkeypatch):
    """Activa el guard del proxy (leido en runtime dentro del decorador, que
    vive en webapp.core desde el refactor a blueprints)."""
    monkeypatch.setattr(core, "INTERNAL_PROXY_SECRET", "s3cret-de-prueba")
    return "s3cret-de-prueba"
