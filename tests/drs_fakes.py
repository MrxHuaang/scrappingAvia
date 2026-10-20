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
FAKE_CONTACT = "drs-tests@example.com"
#: The real API answers 400 to a filter with more than 10 values.
FILTER_MAX_VALUES = 10


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
        if unknown or len(filters.get("drs:documentNumber") or []) > FILTER_MAX_VALUES:
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


class FakeStore:
    """The three sync RPCs plus the cursor read, recorded call by call."""

    def __init__(self, state: dict[str, dict] | None = None, *, fail_upsert_on: int | None = None,
                 fail_read: bool = False, fail_link: bool | int = False):
        self.state = {k: dict(v) for k, v in (state or {}).items()}
        self.fail_upsert_on = fail_upsert_on
        self.fail_read = fail_read
        # True: every link call fails; an int: that many calls fail, then they answer.
        self.fail_link = fail_link
        self.upserts: list[dict] = []
        self.advances: list[dict] = []
        self.links: list[dict] = []
        self.rows: dict[str, dict] = {}
        self.doctypes: dict[str, str] = {}
        self.document_reads: list[dict] = []

    def read_state(self, doctype):
        if self.fail_read:
            raise RuntimeError("drs_sync_state unreachable")
        return self.state.get(doctype)

    def upsert(self, doctype, documents):
        n = len(self.upserts) + 1
        self.upserts.append({"p_doctype": doctype, "p_documents": copy.deepcopy(documents)})
        if self.fail_upsert_on == n:
            raise RuntimeError("upsert_drs_documents failed")
        inserted = updated = unchanged = 0
        for d in documents:
            prev = self.rows.get(d["document_guid"])
            if prev is None:
                inserted += 1
            elif prev == d:
                unchanged += 1
            else:
                updated += 1
            self.rows[d["document_guid"]] = d
            self.doctypes[d["document_guid"]] = doctype
        return {"inserted": inserted, "updated": updated, "unchanged": unchanged}

    def advance(self, doctype, last_modified, initial_scan_done):
        self.advances.append({"p_doctype": doctype, "p_last_modified": last_modified,
                              "p_initial_scan_done": initial_scan_done})
        self.state[doctype] = {"last_modified": last_modified, "initial_scan_done": initial_scan_done}

    def link(self, ad_numbers):
        self.links.append({"p_ad_numbers": list(ad_numbers)})
        if self.fail_link is True:
            raise RuntimeError("link_ads_to_drs failed")
        if self.fail_link:
            self.fail_link -= 1
            raise RuntimeError("canceling statement due to statement timeout (57014)")
        return len(ad_numbers)

    def documents(self, doctype, ad_numbers):
        """Stored rows of `doctype` for those AD numbers, like a read of drs_documents."""
        self.document_reads.append({"doctype": doctype, "ad_numbers": sorted(set(ad_numbers))})
        wanted = set(ad_numbers)
        return [copy.deepcopy(d) for guid, d in self.rows.items()
                if self.doctypes.get(guid) == doctype and d["ad_number"] in wanted]
