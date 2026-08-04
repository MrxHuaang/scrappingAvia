"""D-21 y D-22: lo que se le manda a Postgres no puede tumbar el lote entero.

Ambos defectos aparecieron el 2026-08-04, la primera vez en 19 dias que EASA y
Transport Canada llegaron a la fase de escritura de relaciones. El timeout los
habia estado ocultando: una fuente que nunca termina de enriquecer nunca llega a
escribir, y lo que no se ejecuta no falla.

D-21: `replace_ad_applicability` valida el payload por LLAMADA, antes del
triestado por AD, asi que una sola fila invalida aborta hasta 300 ADs. Dos ADs
de EASA con `make` de 457 y 494 chars tumbaron dos lotes completos.

D-22: Postgres rechaza con 21000 un ON CONFLICT que propone la misma clave dos
veces en la misma sentencia.
"""

import populate_db


def _pairs(make, model):
    return populate_db._applicability_pairs({"make": make, "model": model})


# --------------------------------------------------------------- un fabricante

def test_one_make_with_several_models_pairs_each_model():
    assert _pairs("AIRBUS", "A320, A321, A330") == [
        ("AIRBUS", "A320"),
        ("AIRBUS", "A321"),
        ("AIRBUS", "A330"),
    ]


def test_one_make_without_model_still_declares_the_make():
    assert _pairs("AIRBUS", "") == [("AIRBUS", None)]
    assert _pairs("AIRBUS", None) == [("AIRBUS", None)]


# ------------------------------------------------------------ varios fabricantes

def test_several_makes_never_build_a_cross_product():
    """La regla que evita afirmar cosas falsas.

    El AD 15-117 lista 9 titulares de TC y 60 modelos sin atribucion por
    fabricante. Cruzarlos pondria el R44 de Robinson bajo Airbus Helicopters.
    """
    pairs = _pairs("AIRBUS HELICOPTERS, BELL TEXTRON INC., ROBINSON", "AS 350, 407, R44")
    assert pairs == [
        ("AIRBUS HELICOPTERS", None),
        ("BELL TEXTRON INC.", None),
        ("ROBINSON", None),
    ]
    assert all(model is None for _, model in pairs), "un modelo atribuido a otro fabricante es un dato inventado"


def test_several_makes_produce_short_fields_instead_of_one_long_one():
    """El caso literal que rompio produccion, reducido.

    Antes se emitia UNA fila con los 457 chars concatenados: pasaba el limite de
    la RPC y ademas no habria coincidido con ninguna aeronave.
    """
    make = ", ".join(f"FABRICANTE NUMERO {i:03d} SOCIEDAD ANONIMA" for i in range(12))
    assert len(make) > populate_db.APPLICABILITY_TEXT_MAX
    pairs = _pairs(make, "MODELO A, MODELO B")
    assert len(pairs) == 12
    assert all(len(mk) <= populate_db.APPLICABILITY_TEXT_MAX for mk, _ in pairs)


# ------------------------------------------------------------------ degenerados

def test_no_make_declares_nothing():
    # Sin fabricante no hay fila valida; el AD queda success_empty, no con una
    # fila que la RPC rechazaria.
    assert _pairs("", "A320") == []
    assert _pairs(None, None) == []


def test_blank_segments_are_dropped_not_emitted_as_empty_strings():
    assert _pairs("AIRBUS, , BOEING", "") == [("AIRBUS", None), ("BOEING", None)]
    assert _pairs("AIRBUS", "A320, , A321") == [("AIRBUS", "A320"), ("AIRBUS", "A321")]


def test_the_client_bound_matches_the_rpc_bound():
    """Guarda de deriva.

    `replace_ad_applicability` valida `length(...) > 256` por campo. Si la RPC
    sube o baja ese numero y esta constante no lo sigue, se vuelve a mandar un
    campo que aborta el lote entero — que es exactamente D-21.
    """
    assert populate_db.APPLICABILITY_TEXT_MAX == 256
