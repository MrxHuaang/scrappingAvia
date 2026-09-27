"""DRS data-pull client: paging, cursor validation, errors and key handling.

Everything runs against `drs_fakes.FakeSession`, which reproduces the measured
behaviour of the API (750-row pages, strict second-resolution date filter,
empty 403 for a bad key, 400 with errorMessage for an unknown filter).
"""

from datetime import datetime, timedelta, timezone

import pytest
import requests

from drs_fakes import FAKE_CONTACT, FAKE_KEY, FakeResponse, FakeSession, load_fixture, make_doc
from scrapers import faa_drs_api as drs
from scrapers.common import http


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda _s: None)


def _client(session, **kw):
    kw.setdefault("sleep", lambda _s: None)
    kw.setdefault("retry_delay", 0)
    return drs.DrsClient(FAKE_KEY, contact_email=kw.pop("contact_email", FAKE_CONTACT), session=session, **kw)


def _corpus(n, lm="2024-05-30T19:48:53.441Z"):
    base = load_fixture("adfrawd_documents.json")["documents"][0]
    return [make_doc(base, f"GUID{i:028d}", lm, f"2024-01-{i:02d}") for i in range(n)]


# ---------------------------------------------------------------- flag/env --

def test_the_client_is_off_unless_the_flag_is_on():
    with pytest.raises(drs.DrsDisabledError):
        drs.DrsClient.from_env({"DRS_API_KEY": FAKE_KEY})
    with pytest.raises(drs.DrsDisabledError):
        drs.DrsClient.from_env({"DRS_API_KEY": FAKE_KEY, "DRS_API_ENABLED": "0"})
    assert drs.is_enabled({"DRS_API_ENABLED": "true"})
    assert drs.is_enabled({"DRS_API_ENABLED": " 1 "})
    assert not drs.is_enabled({})


def test_enabled_without_a_key_is_a_config_error():
    with pytest.raises(drs.DrsConfigError):
        drs.DrsClient.from_env({"DRS_API_ENABLED": "1", "DRS_API_KEY": "  ", "DRS_CONTACT_EMAIL": FAKE_CONTACT})


@pytest.mark.parametrize("bad_contact", [None, "", "   ", "not-an-email", "missing-domain@", "@no-local-part.com"])
def test_enabled_with_a_missing_or_implausible_contact_fails_closed(bad_contact):
    env = {"DRS_API_ENABLED": "1", "DRS_API_KEY": FAKE_KEY}
    if bad_contact is not None:
        env["DRS_CONTACT_EMAIL"] = bad_contact
    with pytest.raises(drs.DrsConfigError):
        drs.DrsClient.from_env(env)


def test_the_key_travels_only_in_the_header_and_never_in_repr():
    session = FakeSession({"ADFREAD": _corpus(2)})
    client = drs.DrsClient.from_env(
        {"DRS_API_ENABLED": "1", "DRS_API_KEY": FAKE_KEY, "DRS_CONTACT_EMAIL": FAKE_CONTACT},
        session=session, sleep=lambda _s: None,
    )
    client.fetch_page("ADFREAD")
    call = session.calls[0]
    assert call["headers"]["x-api-key"] == FAKE_KEY
    assert FAKE_KEY not in call["url"]
    assert FAKE_KEY not in repr(call["params"])
    assert FAKE_KEY not in repr(client)


def test_the_user_agent_carries_the_contact_and_not_the_key():
    session = FakeSession({"ADFREAD": _corpus(1)})
    client = drs.DrsClient.from_env(
        {"DRS_API_ENABLED": "1", "DRS_API_KEY": FAKE_KEY, "DRS_CONTACT_EMAIL": FAKE_CONTACT},
        session=session, sleep=lambda _s: None,
    )
    client.fetch_page("ADFREAD")
    ua = session.calls[0]["headers"]["User-Agent"]
    assert ua == f"{drs.CLIENT_NAME}/{drs.CLIENT_VERSION} (+mailto:{FAKE_CONTACT})"
    assert FAKE_KEY not in ua


@pytest.mark.parametrize("value,expected", [
    ("a@b.co", True),
    ("first.last+tag@example.com", True),
    ("", False),
    ("   ", False),
    ("not-an-email", False),
    ("missing-domain@", False),
    ("@no-local-part.com", False),
    ("has space@example.com", False),
])
def test_is_plausible_email(value, expected):
    assert drs.is_plausible_email(value) is expected


# ------------------------------------------------------------------ cursor --

@pytest.mark.parametrize("bad", [
    "2026-09-18",                      # date only: the API answers 0 rows, silently
    "2026-09-18T14:28:27Z",            # no milliseconds
    "2026-09-18T14:28:27.520",         # no zone
    "2026-09-18T09:28:27.520-05:00",   # offset zone: accepted by the API, not by us
    "2026-13-18T14:28:27.520Z",        # not a real date
    "",
])
def test_a_malformed_cursor_is_rejected_before_any_request(bad):
    session = FakeSession({"ADFRAWD": _corpus(1)})
    with pytest.raises(ValueError):
        _client(session).fetch_page("ADFRAWD", modified_after=bad)
    assert session.calls == []


def test_format_cursor_is_utc_with_milliseconds():
    moment = datetime(2026, 9, 18, 9, 28, 27, 520999, tzinfo=timezone(timedelta(hours=-5)))
    assert drs.format_cursor(moment) == "2026-09-18T14:28:27.520Z"
    with pytest.raises(ValueError):
        drs.format_cursor(datetime(2026, 9, 18))


def test_a_valid_cursor_and_asc_order_reach_the_query():
    session = FakeSession({"ADFRAWD": _corpus(1, "2026-09-18T15:00:00.000Z")})
    page = _client(session).fetch_page("ADFRAWD", offset=0, modified_after="2026-09-18T14:28:27.520Z")
    assert session.calls[0]["params"] == {
        "offset": 0, "docLastModifiedDateSortOrder": "ASC",
        "docLastModifiedDate": "2026-09-18T14:28:27.520Z",
    }
    assert len(page.documents) == 1


def test_unknown_doctype_is_rejected():
    with pytest.raises(ValueError):
        _client(FakeSession()).fetch_page("ADFRNPRM")


# ------------------------------------------------------------------ paging --

def test_iter_pages_walks_750_row_pages_until_has_more_is_false():
    session = FakeSession({"ADFRAWD": _corpus(1600)})
    pages = list(_client(session).iter_pages("ADFRAWD"))
    assert [len(p.documents) for p in pages] == [750, 750, 100]
    assert [c["params"]["offset"] for c in session.calls] == [0, 750, 1500]
    assert [p.has_more for p in pages] == [True, True, False]
    assert pages[0].total_items == 1600
    guids = [d["documentGuid"] for p in pages for d in p.documents]
    assert len(guids) == len(set(guids)) == 1600


def test_the_client_paces_its_requests():
    waits = []
    now = [0.0]
    session = FakeSession({"ADFRAWD": _corpus(1600)})
    client = drs.DrsClient(FAKE_KEY, contact_email=FAKE_CONTACT, session=session, min_interval=2.0,
                           sleep=waits.append, clock=lambda: now[0])
    list(client.iter_pages("ADFRAWD"))
    assert waits == [2.0, 2.0]
    assert client.requests_made == 3


def test_has_more_with_an_empty_page_stops_instead_of_looping():
    class Stuck(FakeSession):
        def get(self, url, **kw):
            super().get(url, **kw)
            return FakeResponse(200, {"summary": {"hasMoreItems": True, "totalItems": 5}, "documents": []})

    with pytest.raises(drs.DrsRequestError):
        list(_client(Stuck()).iter_pages("ADFRAWD"))


def test_a_real_page_envelope_parses():
    fx = load_fixture("adfrawd_documents.json")
    body = {"summary": fx["summary_asc"], "documents": fx["documents"]}
    session = FakeSession(script={1: FakeResponse(200, body)})
    page = _client(session).fetch_page("ADFRAWD")
    assert page.has_more is True and page.total_items == 20154
    assert page.documents[0]["documentGuid"] == "FR-ADFRAWD-2026-19171-0000000000"


# ------------------------------------------------------------------ errors --

def test_403_is_a_clear_auth_error_without_the_key():
    session = FakeSession({"ADFRAWD": _corpus(1)})
    client = drs.DrsClient("wrong-key", contact_email=FAKE_CONTACT, session=session, sleep=lambda _s: None, retry_delay=0)
    with pytest.raises(drs.DrsAuthError) as exc:
        client.fetch_page("ADFRAWD")
    assert "403" in str(exc.value) and "DRS_API_KEY" in str(exc.value)
    assert "wrong-key" not in str(exc.value)
    assert len(session.calls) == 1, "a 403 is not retried"


@pytest.mark.parametrize("transient", [429, 500, 503, 504, requests.Timeout("read timed out"),
                                       requests.ConnectionError("reset")])
def test_transient_failures_are_retried(transient):
    session = FakeSession({"ADFRAWD": _corpus(3)}, script={1: transient, 2: transient})
    page = _client(session, retries=3).fetch_page("ADFRAWD")
    assert len(page.documents) == 3
    assert len(session.calls) == 3


def test_retries_exhausted_raise_a_request_error():
    session = FakeSession({"ADFRAWD": _corpus(3)}, script={1: 504, 2: 504, 3: 504})
    with pytest.raises(drs.DrsRequestError):
        _client(session, retries=3).fetch_page("ADFRAWD")


def test_a_non_json_or_malformed_200_is_an_error():
    session = FakeSession(script={1: FakeResponse(200, None)})
    with pytest.raises(drs.DrsRequestError):
        _client(session).fetch_page("ADFRAWD")
    session = FakeSession(script={1: FakeResponse(200, {"documents": []})})
    with pytest.raises(drs.DrsRequestError):
        _client(session).fetch_page("ADFRAWD")


# ---------------------------------------------------------------- filtered --

def test_find_by_ad_numbers_posts_the_documented_filter():
    fx = load_fixture("filtered_adnums.json")
    session = FakeSession({"ADFRAWD": fx["documents"]})
    docs = _client(session).find_by_ad_numbers("ADFRAWD", ["2017-24-03", "2024-08-05", "2017-24-03"])
    assert sorted(d["drs:documentNumber"] for d in docs) == ["2017-24-03", "2024-08-05"]
    call = session.calls[0]
    assert call["method"] == "POST"
    assert call["url"] == "https://drs.faa.gov/api/drs/data-pull/ADFRAWD/filtered"
    assert call["json"] == {"offset": 0, "documentFilters": {"drs:documentNumber": ["2017-24-03", "2024-08-05"]}}


def test_find_by_ad_numbers_chunks_long_lists():
    session = FakeSession({"ADFRAWD": _corpus(60)})
    numbers = [f"2024-01-{i:02d}" for i in range(60)]
    docs = _client(session).find_by_ad_numbers("ADFRAWD", numbers)
    assert len(docs) == 60
    assert [len(c["json"]["documentFilters"]["drs:documentNumber"]) for c in session.calls] == [10] * 6


def test_the_fake_rejects_more_than_ten_filter_values_like_the_api():
    session = FakeSession({"ADFRAWD": _corpus(11)})
    body = {"offset": 0, "documentFilters": {"drs:documentNumber": [f"2024-01-{i:02d}" for i in range(11)]}}
    resp = session.post("https://drs.faa.gov/api/drs/data-pull/ADFRAWD/filtered", json=body,
                        headers={"x-api-key": FAKE_KEY})
    assert resp.status_code == 400


def test_a_400_carries_the_api_error_message():
    class BadFilter(FakeSession):
        def post(self, url, *, json=None, **kw):
            json = {**(json or {}), "documentFilters": {"drs:noSuchField": ["x"]}}
            return super().post(url, json=json, **kw)

    with pytest.raises(drs.DrsRequestError) as exc:
        _client(BadFilter()).find_by_ad_numbers("ADFRAWD", ["2017-24-03"])
    assert "400" in str(exc.value) and "filters provided are invalid" in str(exc.value)
