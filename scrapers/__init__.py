"""
Zephr Scrapers - Modulo de scrapers para Airworthiness Directives.
Fuentes: Federal Register (USA), EASA (Europa), Transport Canada.
"""

from scrapers.federal_register import search_ads as federal_register_search
from scrapers.easa import search_ads as easa_search, search_main_page as easa_main_page
from scrapers.transport_canada import search_ads as tc_search, get_manufacturers as tc_get_manufacturers

# Deliberate re-exports (the package's public surface).
__all__ = [
    "federal_register_search",
    "easa_search",
    "easa_main_page",
    "tc_search",
    "tc_get_manufacturers",
]
