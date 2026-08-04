"""
Logging compartido de los scrapers.

El camino del cron (scrapers + populate_db) corre desatendido: cada linea
necesita timestamp y nivel para poder reconstruir una corrida desde los logs
de GitHub Actions. populate_db ya configuraba esto localmente; aqui vive la
unica configuracion y cada modulo pide su logger con get_logger(__name__).

Los scripts CLI interactivos (validate_ads, prune_faa_stale, ...) siguen
usando print a proposito: su stdout ES el reporte.
"""

import logging

_CONFIGURED = False


def get_logger(name: str) -> logging.Logger:
    global _CONFIGURED
    if not _CONFIGURED:
        # basicConfig es no-op si el root ya tiene handlers (p. ej. cuando
        # populate_db configuro primero) — exactamente lo que se quiere.
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        _CONFIGURED = True
    return logging.getLogger(name)
