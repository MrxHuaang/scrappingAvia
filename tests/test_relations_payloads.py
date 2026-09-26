"""Lo que `populate_relations` envia no puede hacer fallar el statement entero.

Complementa test_applicability_pairs.py: alli se prueba la forma de los pares,
aqui el payload que sale de verdad hacia PostgREST/la RPC, incluido el
contrato v2 (model_status, product_type, empty_evidence, lectura de la
respuesta de la RPC).
"""

import pytest

import populate_db


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


# ------------------------------------------------------------ empty_evidence

def test_easa_empty_holder_list_is_success_empty_with_evidence(fake):
    """`Applicability` vacia (el scraper recorrio la seccion y no encontro
    holders) SI autoriza a la RPC a borrar lo guardado."""
    rows = [{"ad_number": "EASA-13", "make": "", "model": "", "product_type": None,
             "raw_data": {"Applicability": []}}]
    populate_db.populate_relations(fake, "easa", rows)

    elements = [p for (n, params) in fake.rpcs if n == "replace_ad_applicability"
                for p in params["p_ads"]]
    assert elements[0]["status"] == "success_empty"
    assert elements[0]["empty_evidence"].strip()


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


def test_transport_canada_never_emits_empty_evidence():
    """El scraper de TC nunca emite `raw_data['Applicability']` (solo EASA lo
    hace) -- no hay forma de distinguir "recorri y no habia holders" de "no
    pude leerlo", asi que es ambiguo y no se emite evidencia nunca."""
    assert populate_db._empty_evidence("transport_canada", {"raw_data": {}}) is None
    assert populate_db._empty_evidence("transport_canada", {"raw_data": {"Applicability": []}}) is None


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
