"""
Fabrica de la app web de Zephr (Fase 3b).

app.py (el entry point que Vercel construye) solo hace
`app = create_app()`; toda la composicion vive aqui: configuracion, CORS,
ProxyFix y el registro de los seis blueprints. La suite de contrato HTTP
(tests/test_http_contract.py) es el criterio de que este ensamblaje se
comporta identico al app.py monolitico que reemplaza.
"""

import os

from flask import Flask, render_template
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

from webapp import auth, billing, core, export, pages, search, status

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def create_app() -> Flask:
    # templates/ y static/ viven en la raiz del repo, no dentro de webapp/.
    app = Flask(
        "app",
        template_folder=os.path.join(_ROOT, "templates"),
        static_folder=os.path.join(_ROOT, "static"),
    )
    app.config["JSON_SORT_KEYS"] = False
    app.secret_key = core.FLASK_SECRET

    # Trust one hop of X-Forwarded-* (Vercel/Next proxy) so request.remote_addr
    # is the real client IP, not the proxy IP (BUG-2).
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

    # CORS (SEC-5): never combine wildcard origins with credentials.
    if core.ALLOWED_ORIGINS == "*":
        CORS(app, origins="*", supports_credentials=False)
    else:
        CORS(app, origins=core.ALLOWED_ORIGINS, supports_credentials=True)

    for module in (pages, auth, billing, search, export, status):
        app.register_blueprint(module.bp)

    @app.errorhandler(404)
    def page_not_found(e):
        return render_template("404.html"), 404

    return app
