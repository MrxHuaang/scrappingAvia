"""The shared retry loop: which statuses are retried and what reaches the wire."""

import requests

from scrapers.common import http


class _Resp:
    def __init__(self, status_code):
        self.status_code = status_code


class _Session:
    """Answers each call with the next item of `script` (a status or an exception)."""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def _next(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        item = self.script.pop(0)
        if isinstance(item, Exception):
            raise item
        return _Resp(item)

    def get(self, url, **kwargs):
        return self._next("GET", url, **kwargs)

    def post(self, url, **kwargs):
        return self._next("POST", url, **kwargs)


def test_post_retries_429_5xx_and_timeouts_then_returns_the_answer(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda _s: None)
    session = _Session([429, 503, requests.Timeout("read timed out"), 200])
    resp = http.request_with_backoff(
        "POST", "https://example.test/x", session=session, json={"a": 1}, retries=4, delay=0,
    )
    assert resp.status_code == 200
    assert [c[0] for c in session.calls] == ["POST"] * 4
    assert all(c[2]["json"] == {"a": 1} for c in session.calls)


def test_a_4xx_is_returned_at_once_without_retrying(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda _s: None)
    session = _Session([403])
    resp = http.request_with_backoff("GET", "https://example.test/x", session=session, retries=3, delay=0)
    assert resp.status_code == 403
    assert len(session.calls) == 1


def test_exhausted_retries_return_none(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda _s: None)
    session = _Session([502, 502])
    assert http.request_with_backoff("GET", "https://example.test/x", session=session, retries=2, delay=0) is None


def test_get_with_backoff_sends_no_body(monkeypatch):
    monkeypatch.setattr(http.time, "sleep", lambda _s: None)
    session = _Session([200])
    http.get_with_backoff("https://example.test/x", session=session, params={"q": 1})
    method, _url, kwargs = session.calls[0]
    assert method == "GET" and "json" not in kwargs and kwargs["params"] == {"q": 1}
