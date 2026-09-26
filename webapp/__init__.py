"""
Fabrica de la app web de Zephr (Fase 3b).

app.py (el entry point que Vercel construye) solo hace
`app = create_app()`; toda la composicion vive aqui: configuracion, CORS,
ProxyFix y el registro de los blueprints. La app es solo la API que llama el
proxy de Next.js (busqueda) mas el health check: no sirve HTML, ni
plantillas, ni archivos estaticos. La suite de contrato HTTP
(tests/test_http_contract.py) fija el comportamiento observable.
"""

from flask import Flask, jsonify
from flask_cors import CORS
from werkzeug.middleware.proxy_fix import ProxyFix

from webapp import core, search


def create_app() -> Flask:
    # static_folder=None: sin la ruta implicita /static/<path>.
    app = Flask("app", static_folder=None)
    app.config["JSON_SORT_KEYS"] = False

    # Trust one hop of X-Forwarded-* (Vercel/Next proxy) so request.remote_addr
    # is the real client IP, not the proxy IP (BUG-2).
    app.wsgi_app = ProxyFix(app.wsgi_app, x_for=1, x_proto=1)

    # CORS (SEC-5): never combine wildcard origins with credentials.
    if core.ALLOWED_ORIGINS == "*":
        CORS(app, origins="*", supports_credentials=False)
    else:
        CORS(app, origins=core.ALLOWED_ORIGINS, supports_credentials=True)

    app.register_blueprint(search.bp)

    @app.errorhandler(404)
    def not_found(e):
        return jsonify({"error": "Not found"}), 404

    return app
