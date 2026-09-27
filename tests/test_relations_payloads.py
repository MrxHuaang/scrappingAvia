"""Lo que `populate_relations` envia no puede hacer fallar el statement entero.

Complementa test_applicability_pairs.py: alli se prueba la forma de los pares,
aqui el payload que sale de verdad hacia PostgREST/la RPC, incluido el
contrato v2 (model_status, product_type, empty_evidence, lectura de la
respuesta de la RPC, reenvio de un lote ante deadlock).
"""

import pytest
from bs4 import BeautifulSoup
from postgrest.exceptions import APIError

import populate_db
import test_easa_row_parse
from scrapers.easa import _parse_results_table


class FakeQuery:
    def __init__(self, sink, name):
        self._sink = sink
        self._name = name
        self._payload = None

    # --- upsert / select usados por populate_relations ---
    def upsert(self, payload, on_conflict=None):
        self._sink.append((self._name, on_conflict, payload))
        return self

    def select(self, *_a, **_k):
        return self

    def in_(self, *_a, **_k):
        return self

    def eq(self, *_a, **_k):
        return self

    def order(self, *_a, **_k):
        return self

    def range(self, *_a, **_k):
        return self

    def limit(self, *_a, **_k):
        return self

    def execute(self):
        return type("Resp", (), {"data": []})()


class FakeRpcQuery:
    """Simula la respuesta de `replace_ad_applicability`: un status por AD, en
    el orden del payload, degradando `success_empty` sin `empty_evidence` a
    `failed` — igual que hace la RPC real (comentario de la migracion v2)."""

    def __init__(self, batch):
        self._batch = batch

    def execute(self):
        data = []
        for e in self._batch:
            st = e["status"]
            if st == "success_empty" and not (e.get("empty_evidence") or "").strip():
                st = "failed"
            data.append({"ad_id": e["ad_id"], "status": st, "rows_now": len(e.get("rows") or [])})
        return type("Resp", (), {"data": data})()


class FakeSupabase:
    def __init__(self):
        self.upserts = []
        self.rpcs = []

    def table(self, name):
        return FakeQuery(self.upserts, name)

    def rpc(self, name, params):
        self.rpcs.append((name, params))
        return FakeRpcQuery(params["p_ads"])


@pytest.fixture
def fake(monkeypatch):
    sb = FakeSupabase()
    # `populate_relations` resuelve ad_number -> ads.id contra la base; aqui se
    # devuelve un mapa fijo para no necesitar red.
    monkeypatch.setattr(populate_db, "_fetch_ad_ids",
                        lambda _sb, _src, numbers: {n: f"id-{n}" for n in numbers})
    monkeypatch.setattr(populate_db, "_write_errors", [])
    return sb


def _sb_payloads(fake_sb):
    return [p for (name, _oc, p) in fake_sb.upserts if name == "ad_service_bulletins"]


def _applic_elements(fake_sb):
    return [e for (name, params) in fake_sb.rpcs if name == "replace_ad_applicability"
            for e in params["p_ads"]]


def test_a_repeated_service_bulletin_is_sent_once(fake):
    """D-22.

    Postgres aborta con 21000 (`ON CONFLICT DO UPDATE command cannot affect row
    a second time`) si la misma sentencia propone dos veces la misma clave. El
    upsert va en lotes de 500, asi que un solo AD que cite el mismo SB dos veces
    tiraba el lote completo.
    """
    rows = [{
        "ad_number": "EASA-1",
        "make": "AIRBUS",
        "model": "A320",
        "product_type": "Aircraft",
        "raw_data": {"Service_Bulletins": ["SB-100", "SB-100", " SB-100 ", "SB-200"]},
    }]
    populate_db.populate_relations(fake, "easa", rows)

    sent = [r for payload in _sb_payloads(fake) for r in payload]
    keys = [(r["ad_id"], r["sb_number"]) for r in sent]
    assert len(keys) == len(set(keys)), f"claves duplicadas en un mismo upsert: {keys}"
    assert sorted(k[1] for k in keys) == ["SB-100", "SB-200"]


def test_two_bulletins_that_collide_after_truncation_are_sent_once(fake):
    """El corte a 120 chars puede colapsar dos SBs distintos en el mismo valor.

    Deduplicar sobre el valor original no bastaria: el constraint ve el truncado.
    """
    long_a = "SB-" + "A" * 130
    long_b = long_a[:120] + "DIFERENTE-COLA"
    rows = [{
        "ad_number": "EASA-2", "make": "AIRBUS", "model": "A320", "product_type": "Aircraft",
        "raw_data": {"Service_Bulletins": [long_a, long_b]},
    }]
    populate_db.populate_relations(fake, "easa", rows)

    sent = [r for payload in _sb_payloads(fake) for r in payload]
    keys = [(r["ad_id"], r["sb_number"]) for r in sent]
    assert len(keys) == len(set(keys))
    assert all(len(r["sb_number"]) <= 120 for r in sent)


def test_the_applicability_payload_carries_no_oversized_field(fake):
    """D-21, extremo a extremo.

    El AD 15-117 real llevaba 457 chars de `make` concatenado. Con la estructura
    del origen (`raw_data["Applicability"]`, un approval holder con SUS modelos)
    sale una fila por fabricante, cada una holgada frente al limite de la RPC.
    """
    holders = [
        {"make": f"FABRICANTE {i:02d} SOCIEDAD ANONIMA DE AERONAVES", "models": [f"MOD-{i:02d}"]}
        for i in range(14)
    ]
    rows = [{
        "ad_number": "EASA-3",
        "make": holders[0]["make"],
        "model": ", ".join(h["models"][0] for h in holders),
        "product_type": "Aircraft",
        "raw_data": {"Applicability": holders},
    }]
    populate_db.populate_relations(fake, "easa", rows)

    sent = [row for e in _applic_elements(fake) for row in e.get("rows") or []]
    assert len(sent) == 14, "un par por approval holder"
    for row in sent:
        assert len(row["make"]) <= populate_db.APPLICABILITY_MAKE_MAX
        assert len(row.get("model") or "") <= populate_db.APPLICABILITY_MODEL_MAX
    assert {(r["make"], r["model"]) for r in sent} == {(h["make"], h["models"][0]) for h in holders}
    assert all(r["confidence"] == "high" for r in sent)
    assert not populate_db._write_errors, "un payload valido no debe registrar error de escritura"


def test_a_joined_make_without_structure_is_reported_not_split(fake):
    """La alternativa descartada, fijada como contrato.

    Partir `make` por coma parece el fix obvio y no lo es: los nombres de holder
    traen comas propias, asi que sobre las 765 filas afectadas de EASA produce
    1972 tokens de los que 316 son sufijos societarios ("INC." x194, "LLC" x56,
    "GmbH" x42). Esos fragmentos acabarian como fabricantes en la BD y como
    opciones del filtro Make. Sin estructura que lo explique, el campo se
    reporta y se descarta; no se convierte en 14 fabricantes plausibles.
    """
    makes = ", ".join(f"FABRICANTE {i:02d} SOCIEDAD ANONIMA DE AERONAVES" for i in range(14))
    rows = [{"ad_number": "EASA-5", "make": makes, "model": "AS 350", "raw_data": {}}]
    populate_db.populate_relations(fake, "easa", rows)

    sent = [row for e in _applic_elements(fake) for row in e.get("rows") or []]
    assert sent == [], "ningun fragmento del string unido debe viajar como fabricante"
    assert any("fuera de limite" in e for e in populate_db._write_errors)


def test_an_unsplittable_oversized_field_is_dropped_loudly(fake):
    """Un solo fabricante con un nombre imposible no puede tumbar el lote...

    ...pero tampoco desaparece en silencio: `_record_write_error` fuerza
    status='error' y salida != 0, que es la regla de todo este plan.
    """
    rows = [{"ad_number": "EASA-4", "make": "X" * 300, "model": "A320", "raw_data": {}}]
    populate_db.populate_relations(fake, "easa", rows)

    sent = [row for e in _applic_elements(fake) for row in e.get("rows") or []]
    assert sent == [], "la fila sobredimensionada no debe viajar a la RPC"
    assert any("fuera de limite" in e for e in populate_db._write_errors), \
        "descartarla en silencio seria el defecto que este plan combate"

    # Y el AD viaja como `failed`, NUNCA como `success_empty`: un descarte del
    # cliente no es evidencia de "no aplica", y `success_empty` le ordenaria a
    # la RPC borrar la applicability ya guardada de ese AD.
    statuses = [e["status"] for e in _applic_elements(fake)]
    assert statuses == ["failed"], \
        f"un AD con filas descartadas debe declararse failed, no {statuses}"


def test_model_exceeding_its_own_larger_limit_is_dropped_too(fake):
    """make y model tienen limites SEPARADOS (256 / 2000) desde la RPC v2: un
    modelo de mas de 2000 chars debe descartarse aunque el make quepa de sobra.
    """
    rows = [{"ad_number": "EASA-7", "make": "AIRBUS", "model": "A" * 2001, "raw_data": {}}]
    populate_db.populate_relations(fake, "easa", rows)

    sent = [row for e in _applic_elements(fake) for row in e.get("rows") or []]
    assert sent == []
    assert any("fuera de limite" in e for e in populate_db._write_errors)


def test_an_ad_with_one_bad_holder_and_one_good_one_is_still_failed_not_partial(fake):
    """Un AD con estructura (varios holders) donde solo uno de ellos no cabe.

    El holder valido (SIKORSKY/S-76) por si solo pasaria como `success_with_rows`
    con una fila. Pero el AD tiene mas de lo que se esta declarando -- el holder
    descartado no es "el AD no aplica a Sikorsky", es un dato que no se pudo
    escribir. Sale `failed` con `rows: []`, nunca una fila parcial.
    """
    rows = [{
        "ad_number": "EASA-6", "make": "X" * 300, "model": "", "raw_data": {
            "Applicability": [
                {"make": "X" * 300, "models": []},
                {"make": "SIKORSKY", "models": ["S-76"]},
            ]
        },
    }]
    populate_db.populate_relations(fake, "easa", rows)

    elements = [p for (n, params) in fake.rpcs if n == "replace_ad_applicability"
                for p in params["p_ads"]]
    assert len(elements) == 1
    assert elements[0]["status"] == "failed"
    assert elements[0]["rows"] == [], "failed no puede llevar filas (la RPC lo rechaza)"


def test_applicability_error_marks_the_whole_ad_failed_with_no_rows(fake):
    """El AD tiene un raw_data['Applicability_Error'] (extraccion fallida a
    medias): nunca se declara un subconjunto parcial como si fuera completo."""
    rows = [{
        "ad_number": "EASA-8", "make": "AIRBUS", "model": "A320",
        "raw_data": {"Applicability_Error": "PDF illegible"},
    }]
    populate_db.populate_relations(fake, "easa", rows)

    elements = [p for (n, params) in fake.rpcs if n == "replace_ad_applicability"
                for p in params["p_ads"]]
    assert len(elements) == 1
    assert elements[0]["status"] == "failed"
    assert elements[0]["rows"] == []
    assert any("extraccion fallo" in e for e in populate_db._write_errors)


def test_a_malformed_holder_next_to_a_valid_one_sends_failed_without_rows(fake):
    """AIRBUS/A320 valido y un segundo holder sin make
    con 737-800. Mandar solo AIRBUS como `success_with_rows` hacia que la RPC
    podara la fila BOEING/737-800 guardada; `failed` las conserva las dos."""
    rows = [{
        "ad_number": "EASA-15", "make": "AIRBUS", "model": "A320, 737-800",
        "product_type": "Aircraft", "raw_data": {"Applicability": [
            {"make": "AIRBUS", "models": ["A320"]},
            {"make": "", "models": ["737-800"]},
        ]},
    }]
    populate_db.populate_relations(fake, "easa", rows)

    assert _applic_elements(fake) == [{"ad_id": "id-EASA-15", "status": "failed", "rows": []}]
    assert any("extraccion fallo" in e for e in populate_db._write_errors)


# ------------------------------------------------------- model_status invariantes

def test_model_status_is_listed_iff_model_text_is_present(fake):
    rows = [{
        "ad_number": "EASA-9", "make": "AIRBUS", "model": "A320", "product_type": "Aircraft",
        "raw_data": {"Applicability": [
            {"make": "AIRBUS", "models": ["A320"]},
            {"make": "AIRBUS", "models": []},
        ]},
    }]
    populate_db.populate_relations(fake, "easa", rows)

    sent = [row for e in _applic_elements(fake) for row in e.get("rows") or []]
    by_model = {r["model"]: r["model_status"] for r in sent}
    assert by_model["A320"] == "listed"
    assert by_model[None] == "unknown"
    # `none` no lo emite ninguna fuente hoy: no hay senal fiable de "todos los
    # modelos" en ningun scraper activo.
    assert all(r["model_status"] in ("listed", "unknown") for r in sent)


def test_product_type_is_mapped_to_the_catalog_code_or_omitted(fake):
    rows = [
        {"ad_number": "EASA-10", "make": "AIRBUS", "model": "A320", "product_type": "Aircraft",
         "raw_data": {}},
        {"ad_number": "EASA-11", "make": "BELL", "model": "407", "product_type": "Helicopter",
         "raw_data": {}},
        {"ad_number": "EASA-12", "make": "HONEYWELL", "model": "TFE731", "product_type": None,
         "raw_data": {}},
    ]
    populate_db.populate_relations(fake, "easa", rows)

    sent = [row for e in _applic_elements(fake) for row in e.get("rows") or []]
    by_make = {r["make"]: r for r in sent}
    assert by_make["AIRBUS"]["product_type"] == "aircraft"
    assert by_make["BELL"]["product_type"] == "rotorcraft"
    # Sin tipo derivable, el campo se omite del payload -- la RPC/el trigger lo
    # derivan server-side de ads.product_type y la fila queda 'derived'.
    assert "product_type" not in by_make["HONEYWELL"]


def test_product_subtype_travels_only_with_its_product_type(fake):
    rows = [
        {"ad_number": "FR-10", "make": "CFM", "model": "CFM56-7B", "product_type": "Engine",
         "subject": "CFM International S.A. Turbofan Engines", "raw_data": {}},
        {"ad_number": "FR-11", "make": "ROBINSON", "model": "R44", "product_type": "Aircraft",
         "subject": "Robinson Helicopter Company Helicopters", "raw_data": {}},
        {"ad_number": "FR-12", "make": "AUSTRO", "model": "E4", "product_type": "Engine",
         "subject": "Austro Engine GmbH Engines", "raw_data": {}},
        {"ad_number": "FR-13", "make": "HONEYWELL", "model": "", "product_type": None,
         "subject": "Honeywell International Inc. Turbofan Engines", "raw_data": {}},
    ]
    populate_db.populate_relations(fake, "federal_register", rows)

    by_make = {r["make"]: r for e in _applic_elements(fake) for r in e.get("rows") or []}
    assert by_make["CFM"]["product_subtype"] == "turbine"
    # 'helicopter' es de rotorcraft: bajo aircraft se omite, no se fuerza.
    assert by_make["ROBINSON"]["product_type"] == "aircraft"
    assert "product_subtype" not in by_make["ROBINSON"]
    # "Engines" a secas no dice cual.
    assert "product_subtype" not in by_make["AUSTRO"]
    # Sin product_type la RPC rechazaria el subtipo: nunca viaja solo.
    assert "product_type" not in by_make["HONEYWELL"]
    assert "product_subtype" not in by_make["HONEYWELL"]


# ------------------------------------------------------------ empty_evidence

@pytest.mark.parametrize("source", [
    "easa", "federal_register", "transport_canada", "anac_brazil",
    "dgac_chile", "anac_argentina", "uk_caa",
])
def test_no_source_declares_success_empty(fake, source):
    """Ninguna fuente activa da una senal explicita de "el AD no aplica a
    nada": una lista de holders vacia es un selector que no encontro nada
    (EASA con otro markup da lo mismo), no la fuente declarando ausencia.
    Sin esa senal, `success_empty` borraria filas reales."""
    rows = [{"ad_number": "X-1", "make": "", "model": "", "product_type": None,
             "raw_data": {"Applicability": []}}]
    populate_db.populate_relations(fake, source, rows)

    assert _applic_elements(fake) == [{"ad_id": "id-X-1", "status": "failed", "rows": []}]


@pytest.mark.parametrize("html", [
    test_easa_row_parse.HOLDER_CLASS_RENAMED,
    test_easa_row_parse.HOLDER_NAME_WRAPPED,
    test_easa_row_parse.HOLDER_SECTION_TRUNCATED,
], ids=["class-renamed", "name-wrapped", "section-truncated"])
def test_easa_markup_the_parser_does_not_recognize_keeps_the_stored_rows(fake, html):
    """Del HTML al payload: la fuente sigue listando sus holders, el parser no
    los ve enteros, y el AD viaja `failed` sin filas (la RPC no toca lo
    guardado) en vez de `success_empty` o de un `success_with_rows` parcial."""
    [scraped] = _parse_results_table(BeautifulSoup(html, "html.parser"))
    populate_db.populate_relations(fake, "easa", [populate_db.normalize(scraped, "easa")])

    assert _applic_elements(fake) == [{"ad_id": "id-2024-0195", "status": "failed", "rows": []}]


@pytest.mark.parametrize("html, ad_number, make, model", [
    (test_easa_row_parse.SIB_PAIR_ROW, "2026-10", "ALLSTAR PZL GLIDER", "SZD-50"),
    (test_easa_row_parse.ORPHANED_PAIR_ROW, "US-2021-26-09",
     "BRANTLY HELICOPTERS INDUSTRIES U.S.A. Co., Ltd.", "305"),
    (test_easa_row_parse.APPLIANCES_PAIR_ROW, "2025-0100", "BAE SYSTEMS (OPERATIONS) LTD", "BAe 146"),
], ids=["sib-pair", "orphaned-pair", "appliances-pair"])
def test_an_easa_placeholder_next_to_a_real_holder_sends_only_the_real_rows(fake, html, ad_number, make, model):
    """Del HTML al payload: el placeholder se descarta y el AD es
    `success_with_rows` con las filas del holder real, como cualquier AD; la
    RPC poda las filas del placeholder que hubiera guardadas."""
    [scraped] = _parse_results_table(BeautifulSoup(html, "html.parser"))
    populate_db.populate_relations(fake, "easa", [populate_db.normalize(scraped, "easa")])

    [element] = _applic_elements(fake)
    assert element["ad_id"] == f"id-{ad_number}"
    assert element["status"] == "success_with_rows"
    assert [(r["make"], r["model"]) for r in element["rows"]] == [(make, model)]


@pytest.mark.parametrize("html, ad_number", [
    (test_easa_row_parse.SOLO_SIB_ROW, "2026-07R2"),
    (test_easa_row_parse.APPLIANCES_ONLY_ROW, "G-2026-0004"),
    (test_easa_row_parse.ORPHANED_ONLY_ROW, "2016-0214"),
], ids=["solo-sib", "appliances-only", "orphaned-only"])
def test_an_easa_ad_with_only_placeholders_is_failed(fake, html, ad_number):
    """Solo placeholders: la lista de holders queda vacia, y EASA nunca
    declara `success_empty` (I-02), asi que el AD viaja `failed` y lo
    guardado queda intacto."""
    [scraped] = _parse_results_table(BeautifulSoup(html, "html.parser"))
    row = populate_db.normalize(scraped, "easa")
    assert row["make"] is None                  # ads.make nunca es un placeholder
    populate_db.populate_relations(fake, "easa", [row])

    assert _applic_elements(fake) == [{"ad_id": f"id-{ad_number}", "status": "failed", "rows": []}]


def test_easa_bare_empty_make_without_applicability_key_is_failed_not_empty(fake):
    """Sin la clave `Applicability` (no se llego a esa seccion), un make vacio
    por si solo NO es evidencia de ausencia real -- viaja `failed`."""
    rows = [{"ad_number": "EASA-14", "make": "", "model": "", "product_type": None,
             "raw_data": {}}]
    populate_db.populate_relations(fake, "easa", rows)

    elements = [p for (n, params) in fake.rpcs if n == "replace_ad_applicability"
                for p in params["p_ads"]]
    assert elements[0]["status"] == "failed"


def test_federal_register_appliance_without_make_is_failed_not_empty(fake):
    """Un AD de Appliance SI tiene applicability (fabricante/pieza del
    appliance): declararlo success_empty borraria filas reales. FR nunca
    emite empty_evidence -- sin make, siempre failed."""
    rows = [{"ad_number": "FR-1", "make": "", "model": "", "product_type": "Appliance",
             "raw_data": {}}]
    populate_db.populate_relations(fake, "federal_register", rows)

    elements = [p for (n, params) in fake.rpcs if n == "replace_ad_applicability"
                for p in params["p_ads"]]
    assert elements[0]["status"] == "failed"


def test_federal_register_generic_title_without_make_is_failed(fake):
    """Un titulo generico ("Various Aircraft") tampoco es evidencia."""
    rows = [{"ad_number": "FR-2", "make": "", "model": "", "product_type": "Aircraft",
             "subject": "Various Aircraft", "raw_data": {}}]
    populate_db.populate_relations(fake, "federal_register", rows)

    elements = [p for (n, params) in fake.rpcs if n == "replace_ad_applicability"
                for p in params["p_ads"]]
    assert elements[0]["status"] == "failed"


def test_listing_only_source_with_blank_make_is_failed_not_empty(fake):
    """DGAC Chile / ANAC Argentina / UK CAA / ANAC Brasil: un listado sin make
    es ausencia de dato (el scraper no lo extrajo), no la fuente afirmando
    que el AD no aplica a nada -- nunca autoriza a borrar lo guardado."""
    rows = [{"ad_number": "DGAC-1", "make": "", "model": "", "product_type": None,
             "raw_data": {}}]
    populate_db.populate_relations(fake, "dgac_chile", rows)

    elements = [p for (n, params) in fake.rpcs if n == "replace_ad_applicability"
                for p in params["p_ads"]]
    assert elements[0]["status"] == "failed"


# ------------------------------------------------------- lectura de la RPC

def test_db_reported_failed_status_is_counted_from_the_response(fake, caplog):
    """`populate_relations` no confia en lo declarado: lee `status` de la
    respuesta real de la RPC (que puede degradar success_empty sin evidencia a
    failed) y lo cuenta/loguea aparte de lo declarado."""
    import logging
    rows = [
        # Declarado success_with_rows real.
        {"ad_number": "M-1", "make": "AIRBUS", "model": "A320", "product_type": "Aircraft",
         "raw_data": {}},
        # Declarado failed (sin evidencia) -> la fake RPC lo confirma failed.
        {"ad_number": "M-2", "make": "", "model": "", "product_type": "Aircraft",
         "subject": "Various Aircraft", "raw_data": {}},
    ]
    with caplog.at_level(logging.INFO, logger="populate_db"):
        populate_db.populate_relations(fake, "federal_register", rows)

    joined = " ".join(r.message for r in caplog.records)
    assert "success_with_rows" in joined
    assert "failed" in joined


# ------------------------------------------------- reintento ante deadlock

class ScriptedRpcSupabase:
    """`rpc()` levanta, en orden, los errores de `failures` y despues responde
    como la RPC real. Registra cada llamada: un reintento es una llamada nueva
    (una transaccion nueva en Postgres), nunca un reintento dentro de la misma."""

    def __init__(self, *failures):
        self.failures = list(failures)
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, params["p_ads"]))
        if self.failures:
            failure = self.failures.pop(0)

            class _Failing:
                def execute(self):
                    raise failure
            return _Failing()
        return FakeRpcQuery(params["p_ads"])


def _deadlock():
    return APIError({"code": "40P01", "message": "deadlock detected"})


_BATCH = [{"ad_id": "id-A", "status": "success_with_rows",
           "rows": [{"make": "AIRBUS", "model": "A320"}]}]


def test_a_deadlocked_batch_is_resent_once_and_counted(fake):
    sb = ScriptedRpcSupabase(_deadlock())
    counts = populate_db._send_applicability_batches(sb, _BATCH)

    assert [batch for _name, batch in sb.calls] == [_BATCH, _BATCH]
    assert counts["success_with_rows"] == 1
    assert not populate_db._write_errors


def test_two_deadlocks_in_a_row_keep_the_second_error(fake):
    sb = ScriptedRpcSupabase(_deadlock(), APIError({"code": "40P01", "message": "deadlock again"}))
    counts = populate_db._send_applicability_batches(sb, _BATCH)

    assert len(sb.calls) == 2
    assert counts["success_with_rows"] == 0
    assert len(populate_db._write_errors) == 1
    assert "deadlock again" in populate_db._write_errors[0]


@pytest.mark.parametrize("error", [
    APIError({"code": "22023", "message": "invalid payload"}),
    APIError({"code": "40001", "message": "could not serialize access"}),
    RuntimeError("connection reset"),
], ids=["invalid-payload", "serialization-failure", "transport"])
def test_other_errors_are_not_retried(fake, error):
    sb = ScriptedRpcSupabase(error)
    populate_db._send_applicability_batches(sb, _BATCH)

    assert len(sb.calls) == 1
    assert len(populate_db._write_errors) == 1
