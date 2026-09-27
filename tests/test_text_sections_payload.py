"""Texto FAA hacia `replace_ad_text_sections` (populate_db.populate_text_sections).

Sin red ni base: el cache full-text se llena con granulos reales de
`fixtures/fr_text/` (o con la Session falsa de GovInfo) y la base es un
cliente falso que reproduce la regla de la RPC (migración 20260927100000 de
ad-scrapper): `extracted` + completo reemplaza (ok); `extracted` incompleto
es `partial` y solo se guarda si no hay texto conservado; `unanchored`,
`empty` y `error` registran el intento sin tocar ninguna sección. El cliente
no decide nada de eso: estas pruebas fijan lo que manda y que no trabaja
alrededor de la base.
"""

import os
import threading

import pytest
import requests

import populate_db
from scrapers import federal_register as fr

_DIR = os.path.join(os.path.dirname(__file__), "fixtures", "fr_text")

# document_number -> (publication_date, AD, amendment) de fixtures/fr_text.json
DOCS = {
    "2024-08105": ("2024-04-17", "2024-06-03", "39-22708"),
    "2017-25253": ("2017-11-28", "2017-24-03", "39-19107"),
    "E7-1708": ("2007-02-12", "2007-03-13", "39-14924"),
}


def _html(doc: str) -> str:
    with open(os.path.join(_DIR, f"{doc}.htm"), encoding="utf-8") as f:
        return f.read()


def _complete(sections) -> bool:
    """ad_text_is_complete, tal cual la define la migración."""
    kinds = {s["kind"] for s in sections if (s.get("body") or "").strip()}
    return "applicability" in kinds and "unsafe_condition" in kinds and bool(
        kinds & {"compliance", "required_actions"})


class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db, table):
        self.db, self.table, self.ids = db, table, None

    def select(self, *_a, **_k):
        return self

    def in_(self, col, vals):
        self.ids = set(vals)
        return self

    def eq(self, *_a, **_k):
        return self

    def execute(self):
        if self.table == "ad_text_documents":
            if self.db.fail_reads:
                raise RuntimeError("read failed")
            self.db.reads.append(sorted(self.ids))
            return _Resp([{"ad_id": aid, "text_sha256": d["text_sha256"]}
                          for aid, d in self.db.documents.items() if aid in self.ids])
        return _Resp([])


class FakeTextDb:
    """ad_text_documents + ad_text_sections y la regla de la RPC."""

    def __init__(self):
        self.documents: dict[str, dict] = {}
        self.sections: dict[str, list[dict]] = {}
        self.calls: list[dict] = []
        self.reads: list[list[str]] = []
        self.fail_rpc = 0  # cuántas llamadas fallan antes de responder
        self.fail_reads = False

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, params):
        assert name == "replace_ad_text_sections"
        self.calls.append(params)
        db = self

        class _Call:
            def execute(self_inner):
                if db.fail_rpc:
                    db.fail_rpc -= 1
                    raise RuntimeError("PGRST202 function not found")
                return _Resp(db._replace(params["p_ad_id"], params["p_attempt"], params["p_sections"]))
        return _Call()

    def _replace(self, ad_id, attempt, sections):
        status = attempt["status"]
        assert status in ("extracted", "unanchored", "empty", "error")
        if status != "extracted":
            assert sections == [], "la RPC rechaza secciones en un intento no extracted"
        doc = self.documents.setdefault(ad_id, {"text_sha256": None, "text_fetched": False})
        kept = doc["text_fetched"]
        result = status if status != "extracted" else ("ok" if _complete(sections) else "partial")
        doc["last_attempt_status"], doc["last_attempt_detail"] = result, attempt.get("detail")
        if result == "ok" or (result == "partial" and not kept):
            self.sections[ad_id] = [dict(s) for s in sections]
            doc.update(text_sha256=attempt.get("sha256"), text_anchor=attempt.get("anchor"),
                       text_document_id=attempt.get("document_id"), text_fetched=True)
        return result


@pytest.fixture()
def cache(monkeypatch, tmp_path):
    """Cache full-text vacío y aislado; sin intentos en esta corrida."""
    monkeypatch.setattr(fr, "_FULLTEXT_CACHE_PATH", str(tmp_path / "fr_fulltext_cache.json"))
    monkeypatch.setattr(fr, "_fulltext_cache", {})
    monkeypatch.setattr(fr, "_ft_attempted", set())
    monkeypatch.setattr(fr, "_fulltext_dirty", False)
    monkeypatch.setattr(fr, "_ft_local", threading.local())
    monkeypatch.setattr(populate_db, "_write_errors", [])
    return fr._fulltext_cache


@pytest.fixture()
def govinfo(cache, monkeypatch):
    """GovInfo falso: document_number -> (status, texto) o excepción."""
    monkeypatch.setenv("GOVINFO_API_KEY", "clave-de-prueba")
    answers: dict = {}

    class _Session:
        headers: dict = {}

        def get(self, url, timeout=None):
            answer = answers[url.split("/granules/")[1].split("/")[0]]
            if isinstance(answer, Exception):
                raise answer
            status, text = answer
            return type("R", (), {"status_code": status, "text": text, "ok": status < 400})()

    monkeypatch.setattr(requests, "Session", _Session)
    return answers


def _fetch(doc: str):
    pub, ad, amendment = DOCS[doc]
    return fr._fetch_fulltext_fields(doc, pub, ad=ad, amendment=amendment)


def _new_run():
    fr._ft_attempted.clear()


def _row(doc: str) -> dict:
    pub, ad, _ = DOCS[doc]
    return {"ad_number": ad, "raw_data": {"AD_Number": ad, "FR_DocNumber": doc, "Publication_Date": pub}}


def _populate(db, *docs):
    rows = [_row(d) for d in docs]
    return populate_db.populate_text_sections(db, rows, {r["ad_number"]: f"id-{r['ad_number']}" for r in rows})


# ---------------------------------------------------------------------------
# Payload
# ---------------------------------------------------------------------------
def test_extracted_attempt_payload(govinfo):
    govinfo["2024-08105"] = (200, _html("2024-08105"))
    entry = _fetch("2024-08105")
    db = FakeTextDb()
    _populate(db, "2024-08105")

    (call,) = db.calls
    assert call["p_ad_id"] == "id-2024-06-03"
    assert call["p_attempt"] == {
        "status": "extracted",
        "provider": "govinfo",
        "document_id": "2024-08105",
        "source_url": "https://www.govinfo.gov/content/pkg/FR-2024-04-17/html/2024-08105.htm",
        "sha256": entry["sha"],
        "anchor": entry["anchor"],
        "detail": None,
    }
    assert call["p_sections"] == [
        {k: s[k] for k in ("position", "letter", "heading", "kind", "body")} for s in entry["sections"]
    ]
    assert [s["position"] for s in call["p_sections"]] == list(range(1, len(entry["sections"]) + 1))
    assert "clave-de-prueba" not in repr(call), "la API key de GovInfo nunca viaja"
    assert db.documents["id-2024-06-03"]["last_attempt_status"] == "ok"
    assert not populate_db._write_errors


@pytest.mark.parametrize("answer,status,detail", [
    ((500, "Internal Server Error"), "error", "GovInfo HTTP 500"),
    (requests.ConnectionError("reset"), "error", "GovInfo request failed: ConnectionError"),
    ((200, "<html>Please request access</html>"), "empty", "empty GovInfo response or access page"),
])
def test_a_failed_attempt_carries_its_detail_and_no_sections(govinfo, answer, status, detail):
    govinfo["2024-08105"] = answer
    _fetch("2024-08105")
    db = FakeTextDb()
    _populate(db, "2024-08105")
    (call,) = db.calls
    assert call["p_attempt"]["status"] == status
    assert call["p_attempt"]["detail"] == detail
    assert call["p_attempt"]["anchor"] is None
    assert call["p_sections"] == []


def test_unanchored_attempt_says_why_and_sends_no_sections(govinfo):
    govinfo["E7-1708"] = (200, _html("E7-1708"))
    _fetch("E7-1708")
    db = FakeTextDb()
    _populate(db, "E7-1708")
    (call,) = db.calls
    assert call["p_attempt"]["status"] == "unanchored"
    assert call["p_attempt"]["detail"] == "no identity header declares the expected AD and amendment"
    assert call["p_attempt"]["sha256"], "el granulo se bajó: su sha viaja aunque no haya texto"
    assert call["p_sections"] == []
    assert db.sections == {}


def test_the_database_decides_partial_not_the_client(govinfo):
    """2017-25253 (rescisión) no tiene condición insegura: viaja extracted con
    sus secciones y es la base quien la guarda como partial."""
    govinfo["2017-25253"] = (200, _html("2017-25253"))
    entry = _fetch("2017-25253")
    db = FakeTextDb()
    counts = _populate(db, "2017-25253")
    (call,) = db.calls
    assert call["p_attempt"]["status"] == "extracted"
    assert len(call["p_sections"]) == len(entry["sections"])
    assert counts == {"partial": 1}


def test_an_ad_without_a_v2_entry_or_without_an_id_is_not_sent(cache):
    cache["2024-08105"] = {"compliance_time": None, "amoc": False}  # v1: ningún intento v2
    rows = [_row("2024-08105"), _row("2017-25253")]  # la segunda sin entrada
    rows.append({"ad_number": "2099-01-01", "raw_data": {"FR_DocNumber": "2024-08105"}})  # sin ads.id
    db = FakeTextDb()
    assert populate_db.populate_text_sections(db, rows, {"2024-06-03": "id-1", "2017-24-03": "id-2"}) == {}
    assert db.calls == []


def test_the_same_ad_twice_in_the_scrape_is_sent_once(govinfo):
    govinfo["2024-08105"] = (200, _html("2024-08105"))
    _fetch("2024-08105")
    db = FakeTextDb()
    rows = [_row("2024-08105"), _row("2024-08105")]
    populate_db.populate_text_sections(db, rows, {"2024-06-03": "id-1"})
    assert len(db.calls) == 1


def test_sections_over_the_rpc_limits_travel_as_an_error_attempt(cache):
    body = {"heading": "Other", "kind": "other", "letter": None, "body": "x"}
    cache["2024-08105"] = {"v": 2, "status": "extracted", "anchor": "2024-06-03 The Boeing Company: ...",
                           "sha": "abc", "detail": None,
                           "sections": [{**body, "position": i + 1}
                                        for i in range(populate_db.TEXT_SECTIONS_MAX + 1)]}
    fr._ft_attempted.add("2024-08105")
    db = FakeTextDb()
    _populate(db, "2024-08105")
    (call,) = db.calls
    assert call["p_attempt"]["status"] == "error"
    assert call["p_attempt"]["detail"].startswith("sections not sent: 201 sections")
    assert call["p_sections"] == []
    assert any("fuera de limite" in e for e in populate_db._write_errors)


def test_limits_match_the_rpc():
    """Los de replace_ad_text_sections: cambiar uno sin la migración aborta
    la llamada del AD en la base."""
    assert (populate_db.TEXT_SECTIONS_MAX, populate_db.TEXT_FIELD_MAX,
            populate_db.TEXT_LETTER_MAX, populate_db.TEXT_BODY_MAX) == (200, 2048, 8, 200000)


# ---------------------------------------------------------------------------
# Reemplazo no destructivo: el cliente manda el intento fallido; la base
# conserva el texto
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("failure,status", [
    ((500, "Internal Server Error"), "error"),
    ((200, "<html><pre>garbled</pre></html>"), "unanchored"),
])
def test_valid_text_then_a_failed_attempt_keeps_the_text(govinfo, failure, status):
    govinfo["2024-08105"] = (200, _html("2024-08105"))
    good = _fetch("2024-08105")
    db = FakeTextDb()
    _populate(db, "2024-08105")
    kept = db.sections["id-2024-06-03"]

    # Una corrida posterior vuelve a pedirlo (entrada de otra versión o
    # borrada del cache) y falla.
    _new_run()
    del fr._fulltext_cache["2024-08105"]
    govinfo["2024-08105"] = failure
    _fetch("2024-08105")
    _populate(db, "2024-08105")

    assert len(db.calls) == 2, "el intento fallido se manda: es el último intento real"
    assert db.calls[1]["p_attempt"]["status"] == status and db.calls[1]["p_sections"] == []
    assert db.sections["id-2024-06-03"] == kept
    doc = db.documents["id-2024-06-03"]
    assert doc["text_sha256"] == good["sha"]
    assert doc["last_attempt_status"] == status


def test_failed_refetch_of_a_cached_entry_keeps_its_fields(govinfo):
    """Una entrada no definitiva que se reintenta y vuelve a fallar conserva
    lo que tenía y registra el nuevo intento con su motivo."""
    govinfo["2024-08105"] = (500, "down")
    _fetch("2024-08105")
    _new_run()
    govinfo["2024-08105"] = (503, "busy")
    entry = _fetch("2024-08105")
    assert (entry["status"], entry["detail"]) == ("error", "GovInfo HTTP 503")


# ---------------------------------------------------------------------------
# Qué se manda en cada corrida
# ---------------------------------------------------------------------------
def test_cached_attempts_are_sent_once_then_only_new_attempts(govinfo):
    govinfo["2024-08105"] = (200, _html("2024-08105"))
    govinfo["E7-1708"] = (200, _html("E7-1708"))
    _fetch("2024-08105")
    _fetch("E7-1708")
    db = FakeTextDb()
    _populate(db, "2024-08105", "E7-1708")
    assert len(db.calls) == 2

    # Corrida siguiente: todo viene del cache y la base ya lo tiene.
    _new_run()
    _populate(db, "2024-08105", "E7-1708")
    assert len(db.calls) == 2, "reenviar cada noche falsearía last_attempt_at"
    assert db.reads == [["id-2007-03-13", "id-2024-06-03"]]


def test_a_cached_attempt_the_database_lacks_is_sent(cache):
    """Un cache ya lleno antes de que exista el texto en la base (o una
    llamada que falló): lo que la base no tiene se manda aunque no sea de
    hoy; un extracted también si la base solo tiene intentos sin texto."""
    cache["2024-08105"] = {"v": 2, "status": "extracted", "anchor": "a", "sha": "s1", "detail": None,
                           "sections": [{"position": 1, "letter": "a", "heading": "Applicability",
                                         "kind": "applicability", "body": "b"}]}
    cache["E7-1708"] = {"v": 2, "status": "unanchored", "anchor": None, "sha": "s2", "sections": [],
                        "detail": "no identity header declares the expected AD and amendment"}
    cache["2017-25253"] = {"v": 2, "status": "unanchored", "anchor": None, "sha": "s3", "sections": [],
                           "detail": None}
    db = FakeTextDb()
    db.documents["id-2024-06-03"] = {"text_sha256": None, "text_fetched": False}  # solo intentos
    db.documents["id-2017-24-03"] = {"text_sha256": None, "text_fetched": False}  # ya registrado
    _populate(db, "2024-08105", "E7-1708", "2017-25253")
    assert sorted(c["p_ad_id"] for c in db.calls) == ["id-2007-03-13", "id-2024-06-03"]


def test_when_the_stored_state_cannot_be_read_only_new_attempts_go(govinfo):
    govinfo["2024-08105"] = (200, _html("2024-08105"))
    govinfo["E7-1708"] = (200, _html("E7-1708"))
    _fetch("E7-1708")
    _new_run()
    _fetch("2024-08105")
    db = FakeTextDb()
    db.fail_reads = True
    _populate(db, "2024-08105", "E7-1708")
    assert [c["p_ad_id"] for c in db.calls] == ["id-2024-06-03"]
    assert any("ad_text_documents" in e for e in populate_db._write_errors)


# ---------------------------------------------------------------------------
# Contabilidad de errores
# ---------------------------------------------------------------------------
def test_every_rpc_failure_is_a_write_error(govinfo):
    for doc in DOCS:
        govinfo[doc] = (200, _html(doc))
        _fetch(doc)
    db = FakeTextDb()
    db.fail_rpc = 1
    counts = _populate(db, *DOCS)
    assert len(db.calls) == 3
    assert counts["rpc_failed"] == 1 and sum(counts.values()) == 3
    assert len(populate_db._write_errors) == 1


def test_consecutive_rpc_failures_stop_the_text_writes(cache, monkeypatch):
    monkeypatch.setattr(populate_db, "TEXT_MAX_CONSECUTIVE_FAILURES", 3)
    rows, ids = [], {}
    for i in range(10):
        doc = f"2024-{i:05d}"
        cache[doc] = {"v": 2, "status": "unanchored", "anchor": None, "sha": "s", "sections": [],
                      "detail": None}
        fr._ft_attempted.add(doc)
        rows.append({"ad_number": f"2024-00-{i:02d}", "raw_data": {"FR_DocNumber": doc,
                                                                  "Publication_Date": "2024-01-02"}})
        ids[f"2024-00-{i:02d}"] = f"id-{i}"
    db = FakeTextDb()
    db.fail_rpc = 100
    counts = populate_db.populate_text_sections(db, rows, ids)
    assert len(db.calls) == 3
    assert counts == {"rpc_failed": 3, "not_sent": 7}
    assert len(populate_db._write_errors) == 4
    assert "7 AD(s) sin enviar" in populate_db._write_errors[-1]


def test_populate_relations_writes_text_only_for_the_faa(cache, monkeypatch):
    sent = []
    monkeypatch.setattr(populate_db, "populate_text_sections",
                        lambda sb, normalized, id_map: sent.append((normalized, id_map)))
    monkeypatch.setattr(populate_db, "_fetch_ad_ids", lambda _sb, _src, nums: {n: f"id-{n}" for n in nums})
    monkeypatch.setattr(populate_db, "_send_applicability_batches", lambda _sb, _p: {})
    rows = [{"ad_number": "2024-06-03", "make": "BOEING", "model": "737-8", "raw_data": {}}]
    populate_db.populate_relations(FakeTextDb(), "easa", rows)
    assert sent == []
    populate_db.populate_relations(FakeTextDb(), "federal_register", rows)
    assert sent == [(rows, {"2024-06-03": "id-2024-06-03"})]


def test_the_stored_state_is_read_in_batches_and_calls_go_one_ad_each(cache):
    """La pre-lectura de ad_text_documents va en lotes (como las de ids) y la
    RPC recibe un AD por llamada: cada AD es su propia transacción, así que
    un rechazo no arrastra a los demás."""
    rows, ids = [], {}
    for i in range(301):
        doc = f"2023-{i:05d}"
        cache[doc] = {"v": 2, "status": "unanchored", "anchor": None, "sha": "s", "sections": [],
                      "detail": None}
        rows.append({"ad_number": f"2023-01-{i:03d}", "raw_data": {"FR_DocNumber": doc,
                                                                   "Publication_Date": "2023-01-02"}})
        ids[f"2023-01-{i:03d}"] = f"id-{i}"
    db = FakeTextDb()
    counts = populate_db.populate_text_sections(db, rows, ids)
    assert [len(r) for r in db.reads] == [300, 1]
    assert len(db.calls) == 301 and len({c["p_ad_id"] for c in db.calls}) == 301
    assert counts == {"unanchored": 301}
