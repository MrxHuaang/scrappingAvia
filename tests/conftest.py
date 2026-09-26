"""
Fixture de la suite de contrato HTTP (Fase 3b).

La app se importa con TODOS los servicios externos vacios (Supabase, proxy
secret): el comportamiento "sin configurar" es parte del contrato y es
exactamente lo que corre en CI, donde no hay secretos. Los valores se vacian
ANTES de importar app porque sus constantes se leen a nivel de modulo.

Sin secreto el guard del proxy falla cerrado (503). `client` activa a
proposito el opt-in de desarrollo local (el test client llega desde
127.0.0.1) para que los tests de contrato lleguen a las rutas; `strict_client`
es el mismo cliente sin opt-in, y `proxied` configura el secreto, que manda
sobre el opt-in.
"""

import os
import sys

# Antes de importar app: entorno limpio y determinista.
for var in (
    "SUPABASE_URL", "SUPABASE_ANON_KEY", "SUPABASE_SERVICE_KEY",
    "INTERNAL_PROXY_SECRET", "ALLOW_UNAUTHENTICATED_LOCAL",
    "VERCEL",
):
    os.environ.pop(var, None)

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

import app as app_module  # noqa: E402
from webapp import core  # noqa: E402


class _FakeAdminDb:
    """
    Doble permisivo de get_supabase_admin() para tests que no son sobre rate
    limiting: cada tabla se comporta como si estuviera vacia (0 filas), asi
    que check_rate_limit() corre su logica real y siempre encuentra al
    llamador bajo el limite. Los tests sobre el rate limiter mismo (o sobre el
    fail-closed sin service key) fuerzan su propio get_supabase_admin.
    """

    def table(self, name):
        return self

    def select(self, *a, **kw):
        return self

    def eq(self, *a, **kw):
        return self

    def gte(self, *a, **kw):
        return self

    def insert(self, *a, **kw):
        return self

    def maybe_single(self):
        return self

    def execute(self):
        return type("Resp", (), {"data": [], "count": 0})()


@pytest.fixture(autouse=True)
def _admin_client_configured(monkeypatch):
    """
    Por defecto los tests corren como si SUPABASE_SERVICE_KEY existiera: un
    doble permisivo respalda get_supabase_admin() para que el rate limiter
    (que ahora falla cerrado sin la service key) no meta un 503 de sorpresa en
    tests que no son sobre eso. Ver test_route_guards.py para el
    comportamiento sin service key.
    """
    monkeypatch.setattr(core, "get_supabase_admin", lambda: _FakeAdminDb())


@pytest.fixture()
def strict_client(monkeypatch):
    """Cliente sin opt-in local: sin secreto, los datos contestan 503."""
    monkeypatch.delenv("ALLOW_UNAUTHENTICATED_LOCAL", raising=False)
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture()
def client(monkeypatch):
    """Cliente de loopback con el opt-in explicito de desarrollo local."""
    monkeypatch.setenv("ALLOW_UNAUTHENTICATED_LOCAL", "1")
    app_module.app.config["TESTING"] = True
    with app_module.app.test_client() as c:
        yield c


@pytest.fixture()
def proxied(monkeypatch):
    """Activa el guard del proxy (leido en runtime dentro del decorador, que
    vive en webapp.core desde el refactor a blueprints)."""
    monkeypatch.setattr(core, "INTERNAL_PROXY_SECRET", "s3cret-de-prueba")
    return "s3cret-de-prueba"
