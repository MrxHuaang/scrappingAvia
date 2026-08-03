"""
Zephr - Servidor Web (entry point).

Interfaz web para busqueda de Airworthiness Directives.
Vercel construye este archivo (@vercel/python, ver vercel.json); la app real
se compone en webapp/ (Fase 3b): core (config/guards/servicios) + blueprints
pages, auth, billing, search, export y status. La suite de contrato HTTP en
tests/test_http_contract.py fija el comportamiento observable.
"""

from scrapers.common.log import get_logger
from webapp import create_app

log = get_logger(__name__)

app = create_app()


if __name__ == "__main__":
    log.info("=" * 55)
    log.info("  Zephr - Airworthiness Directives")
    log.info("  Fuentes activas: FAA | EASA | TC | ANAC BR | DGAC CL | ANAC AR")
    log.info("=" * 55)
    log.info("  Landing:  http://127.0.0.1:5000/")
    log.info("  App:      http://127.0.0.1:5000/app")
    log.info("  Health:   http://127.0.0.1:5000/api/health")
    app.run(host="127.0.0.1", port=5000, debug=False)
