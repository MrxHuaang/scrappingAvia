"""Lo que `populate_relations` envia no puede hacer fallar el statement entero.

Complementa test_applicability_pairs.py: alli se prueba la forma de los pares,
aqui el payload que sale de verdad hacia PostgREST.
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


class FakeSupabase:
    def __init__(self):
        self.upserts = []
        self.rpcs = []

    def table(self, name):
        return FakeQuery(self.upserts, name)

    def rpc(self, name, params):
        self.rpcs.append((name, params))
        return FakeQuery([], name)


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
        "ad_number": "EASA-2", "make": "AIRBUS", "model": "A320",
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
    sale un par por fabricante, cada uno holgado frente al limite de la RPC, y
    sin cruzar el modelo de un fabricante con el nombre de otro.
    """
    holders = [
        {"make": f"FABRICANTE {i:02d} SOCIEDAD ANONIMA DE AERONAVES", "models": [f"MOD-{i:02d}"]}
        for i in range(14)
    ]
    rows = [{
        "ad_number": "EASA-3",
        "make": holders[0]["make"],
        "model": ", ".join(h["models"][0] for h in holders),
        "raw_data": {"Applicability": holders},
    }]
    populate_db.populate_relations(fake, "easa", rows)

    payloads = [params["p_ads"] for (name, params) in fake.rpcs if name == "replace_ad_applicability"]
    assert payloads, "no se llamo a replace_ad_applicability"
    sent = [row for chunk in payloads for ad in chunk for row in ad["rows"]]
    assert len(sent) == 14, "un par por approval holder"
    for row in sent:
        assert len(row["make"]) <= populate_db.APPLICABILITY_TEXT_MAX
        assert len(row.get("model") or "") <= populate_db.APPLICABILITY_TEXT_MAX
    # La estructura permite conservar el modelo: no hay que elegir entre
    # inventar una atribucion y perderla.
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

    payloads = [params["p_ads"] for (name, params) in fake.rpcs if name == "replace_ad_applicability"]
    sent = [row for chunk in payloads for ad in chunk for row in ad["rows"]]
    assert sent == [], "ningun fragmento del string unido debe viajar como fabricante"
    assert any("mas de 256 chars" in e for e in populate_db._write_errors)


def test_an_unsplittable_oversized_field_is_dropped_loudly(fake):
    """Un solo fabricante con un nombre imposible no puede tumbar el lote...

    ...pero tampoco desaparece en silencio: `_record_write_error` fuerza
    status='error' y salida != 0, que es la regla de todo este plan.
    """
    rows = [{"ad_number": "EASA-4", "make": "X" * 300, "model": "A320", "raw_data": {}}]
    populate_db.populate_relations(fake, "easa", rows)

    payloads = [params["p_ads"] for (name, params) in fake.rpcs if name == "replace_ad_applicability"]
    sent = [row for chunk in payloads for ad in chunk for row in ad["rows"]]
    assert sent == [], "la fila sobredimensionada no debe viajar a la RPC"
    assert any("mas de 256 chars" in e for e in populate_db._write_errors), \
        "descartarla en silencio seria el defecto que este plan combate"

    # Y el AD viaja como `failed`, NUNCA como `success_empty`: un descarte del
    # cliente no es evidencia de "no aplica", y `success_empty` le ordenaria a
    # la RPC borrar la applicability ya guardada de ese AD.
    statuses = [ad["status"] for chunk in payloads for ad in chunk]
    assert statuses == ["failed"], \
        f"un AD con filas descartadas debe declararse failed, no {statuses}"


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

    payloads = [params["p_ads"] for (name, params) in fake.rpcs if name == "replace_ad_applicability"]
    element = [ad for chunk in payloads for ad in chunk][0]
    assert element["status"] == "failed"
    assert element["rows"] == [], "failed no puede llevar filas (la RPC lo rechaza)"
