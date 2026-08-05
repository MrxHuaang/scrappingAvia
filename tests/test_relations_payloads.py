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
    """D-21 + D-24, extremo a extremo.

    El AD 15-117 real llevaba 457 chars de `make` concatenado. Ahora sale el
    producto cartesiano completo: campos cortos que pasan la validacion de la
    RPC, y CADA modelo emparejado con cada fabricante — el par correcto es el
    que mantiene el AD dentro del corte de 500 para la aeronave real (D-24),
    los incorrectos son inertes porque ninguna aeronave cruza fabricantes.
    """
    makes = ", ".join(f"FABRICANTE {i:02d} SOCIEDAD ANONIMA DE AERONAVES" for i in range(14))
    rows = [{"ad_number": "EASA-3", "make": makes, "model": "AS 350, 407, R44", "raw_data": {}}]
    populate_db.populate_relations(fake, "easa", rows)

    payloads = [params["p_ads"] for (name, params) in fake.rpcs if name == "replace_ad_applicability"]
    assert payloads, "no se llamo a replace_ad_applicability"
    sent = [row for chunk in payloads for ad in chunk for row in ad["rows"]]
    assert len(sent) == 14 * 3, "14 fabricantes x 3 modelos"
    for row in sent:
        assert len(row["make"]) <= populate_db.APPLICABILITY_TEXT_MAX
        assert len(row.get("model") or "") <= populate_db.APPLICABILITY_TEXT_MAX
        assert row["model"] in ("AS 350", "407", "R44"), "el modelo nunca se omite (D-24)"
        assert row["confidence"] == "high", \
            "make+model presente: el matching de flota exige high para sobrevivir al cap"
    assert not populate_db._write_errors, "un payload valido no debe registrar error de escritura"


def test_an_unsplittable_oversized_field_is_dropped_loudly(fake):
    """Un solo fabricante con un nombre imposible no puede tumbar el lote...

    ...pero tampoco desaparece en silencio: `_record_write_error` fuerza
    status='error' y salida != 0, que es la regla de todo este plan.

    Y el AD viaja como `failed`, NUNCA como `success_empty`: un descarte del
    cliente no es evidencia de "no aplica", y `success_empty` le ordenaria a
    la RPC borrar la applicability almacenada de ese AD.
    """
    rows = [{"ad_number": "EASA-4", "make": "X" * 300, "model": "A320", "raw_data": {}}]
    populate_db.populate_relations(fake, "easa", rows)

    payloads = [params["p_ads"] for (name, params) in fake.rpcs if name == "replace_ad_applicability"]
    sent = [row for chunk in payloads for ad in chunk for row in ad["rows"]]
    assert sent == [], "la fila sobredimensionada no debe viajar a la RPC"
    statuses = [ad["status"] for chunk in payloads for ad in chunk]
    assert statuses == ["failed"], \
        f"un AD con filas descartadas debe declararse failed, no {statuses}"
    assert any("mas de 256 chars" in e for e in populate_db._write_errors), \
        "descartarla en silencio seria el defecto que este plan combate"
