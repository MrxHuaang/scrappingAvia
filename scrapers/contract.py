"""Shared scraper contract types and validation helpers.

The existing scrapers return plain dictionaries because each authority has
source-specific quirks. These helpers document the common shape without forcing
a risky rewrite of every scraper at once.
"""

from __future__ import annotations

from typing import Protocol, TypedDict


class RawAdRecord(TypedDict, total=False):
    Source: str
    AD_Number: str
    Subject: str
    Make: str
    Model: str
    Product_Type: str
    Effective_Date: str
    Status: str
    Docket: str
    PDF_Link: str


class SearchAdsFn(Protocol):
    def __call__(self, *args, **kwargs) -> list[RawAdRecord]:
        """Return raw AD records for one authority."""


REQUIRED_RAW_AD_FIELDS = ("AD_Number",)

EXPECTED_RAW_AD_FIELDS = (
    "AD_Number",
    "Subject",
    "Make",
    "Model",
    "Product_Type",
    "Effective_Date",
    "Status",
    "Docket",
    "PDF_Link",
)


def validate_raw_ad(record: dict) -> tuple[bool, list[str]]:
    """Return whether a raw scraper row has the minimum required contract."""

    missing_required = [
        field for field in REQUIRED_RAW_AD_FIELDS if not str(record.get(field, "")).strip()
    ]
    missing_expected = [
        field for field in EXPECTED_RAW_AD_FIELDS if field not in record
    ]
    return not missing_required, missing_required + missing_expected

