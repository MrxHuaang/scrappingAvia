"""Fakes for the DRS client and sync tests: no network, no database.

`FakeSession` answers the data-pull endpoints from an in-memory corpus the same
way the real API was measured to: pages of `page_size` in ASC order of
docLastModifiedDate, and a `docLastModifiedDate` filter that is strict and
compares at second resolution. `FakeStore` stands in for the Supabase RPCs.
"""

from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "drs"
FAKE_KEY = "test-key-not-a-real-drs-key"


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class FakeResponse:
    def __init__(self, status_code: int, body=None):
        self.status_code = status_code
        self._body = body
        self.headers = {"Content-Type": "application/json"} if body is not None else {"Content-Length": "0"}

    def json(self):
        if self._body is None:
            raise ValueError("empty body")
        return copy.deepcopy(self._body)


def _second(iso: str) -> datetime:
    """docLastModifiedDate truncated to the second, as the API compares it."""
    return datetime.strptime(iso[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)


class FakeSession:
    """In-memory data-pull API.

    `corpus[doctype]` is the list of documents in the API's stable ASC order.
    `script` maps a call number (1-based) to a status code or an exception to
    return/raise instead of the page, to simulate failures mid-run.
    """

    def __init__(self, corpus: dict[str, list[dict]] | None = None, *, page_size: int = 750,
                 script: dict[int, object] | None = None, doctype_name: str = "AD Final Rules"):
        self.corpus = corpus or {}
        self.page_size = page_size
        self.script = dict(script or {})
        self.calls: list[dict] = []

    def _scripted(self):
        item = self.script.get(len(self.calls))
        if item is None:
            return None
        if isinstance(item, Exception):
            raise item
        return FakeResponse(item) if isinstance(item, int) else item

    def get(self, url, *, params=None, headers=None, timeout=None, **_):
        self.calls.append({"method": "GET", "url": url, "params": dict(params or {}), "headers": dict(headers or {})})
        scripted = self._scripted()
        if scripted is not None:
            return scripted
        if (headers or {}).get("x-api-key") != FAKE_KEY:
            return FakeResponse(403)
        doctype = url.rsplit("/", 1)[-1]
        docs = self.corpus.get(doctype, [])
        since = (params or {}).get("docLastModifiedDate")
        if since is not None:
            cut = _second(since)
            docs = [d for d in docs if _second(d["docLastModifiedDate"]) > cut]
        offset = int((params or {}).get("offset", 0))
        page = docs[offset:offset + self.page_size]
        return FakeResponse(200, {
            "summary": {
                "doctypeName": doctype, "count": len(page),
                "hasMoreItems": offset + len(page) < len(docs), "totalItems": len(docs),
                "offset": offset, "sortBy": "docLastModifiedDate", "sortByOrder": "ASC",
            },
            "documents": page,
        })

    def post(self, url, *, json=None, headers=None, timeout=None, **_):
        self.calls.append({"method": "POST", "url": url, "json": copy.deepcopy(json), "headers": dict(headers or {})})
        scripted = self._scripted()
        if scripted is not None:
            return scripted
        if (headers or {}).get("x-api-key") != FAKE_KEY:
            return FakeResponse(403)
        doctype = url.rsplit("/", 2)[-2]
        filters = (json or {}).get("documentFilters") or {}
        unknown = [k for k in filters if k != "drs:documentNumber"]
        if unknown:
            return FakeResponse(400, load_fixture("error_invalid_filter.json"))
        wanted = set(filters.get("drs:documentNumber") or [])
        docs = [d for d in self.corpus.get(doctype, []) if d.get("drs:documentNumber") in wanted]
        offset = int((json or {}).get("offset", 0))
        page = docs[offset:offset + self.page_size]
        return FakeResponse(200, {
            "summary": {"doctypeName": doctype, "count": len(page),
                        "hasMoreItems": offset + len(page) < len(docs), "totalItems": len(docs),
                        "offset": offset},
            "documents": page,
        })


def make_doc(base: dict, guid: str, last_modified: str, ad_number: str | None = None, **extra) -> dict:
    """A copy of a real fixture document with a new identity and timestamp."""
    doc = copy.deepcopy(base)
    doc["documentGuid"] = guid
    doc["documentURL"] = f"https://drs.faa.gov/browse/excelExternalWindow/{guid}.0001"
    doc["docLastModifiedDate"] = last_modified
    if ad_number is not None:
        doc["drs:documentNumber"] = ad_number
    doc.update(extra)
    return doc
