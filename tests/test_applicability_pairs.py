"""D-21, D-22 y D-24: lo que se manda a Postgres no puede tumbar el lote ni
esconder un AD.

D-21 y D-22 aparecieron el 2026-08-04, la primera vez en 19 dias que EASA y
Transport Canada llegaron a la fase de escritura de relaciones. El timeout los
habia estado ocultando: una fuente que nunca termina de enriquecer nunca llega a
escribir, y lo que no se ejecuta no falla.

D-21: `replace_ad_applicability` valida el payload por LLAMADA, antes del
triestado por AD, asi que una sola fila invalida aborta hasta 300 ADs. Dos ADs
de EASA con `make` de 457 y 494 chars tumbaron dos lotes completos.

D-22: Postgres rechaza con 21000 un ON CONFLICT que propone la misma clave dos
veces en la misma sentencia.

D-24: el primer fix de D-21 emitia `(make, None)` para ADs multi-fabricante
"para no inventar pares". Eso era un falso negativo latente: la confianza la
recalcula `fleet_applicable_ads` desde el match de MODELO (una fila sin modelo
nunca es `high`), ordena por confianza y corta en 500 por aeronave — en flotas
cuyo make coincide con miles de ADs, bajar a `medium` no reordena el AD, lo
expulsa del resultado. Control en el clon: un A330 paso de 3 ADs a 0. Los pares
del producto cartesiano son inertes (ninguna aeronave real cruza fabricantes),
la ausencia no lo es.
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

def test_several_makes_cross_every_model_so_none_loses_its_high_match():
    """D-24: el modelo NUNCA se omite, aunque la atribucion sea incierta.

    El AD 15-117 lista 9 titulares de TC y 60 modelos sin atribucion por
    fabricante. El par (AIRBUS HELICOPTERS, R44) que inventa el producto es
    inerte: ninguna aeronave real tiene el make de un fabricante y el model de
    otro, y el matching exige AMBOS para `high`. El par (ROBINSON, R44) que el
    producto TAMBIEN emite es el que mantiene el AD dentro del corte de 500
    para un R44 real. La version anterior — (make, None) "para no inventar
    datos" — hacia desaparecer el AD: 3 ADs -> 0 en el control del clon.
    """
    pairs = _pairs("AIRBUS HELICOPTERS, BELL TEXTRON INC., ROBINSON", "AS 350, 407, R44")
    assert pairs == [
        ("AIRBUS HELICOPTERS", "AS 350"),
        ("AIRBUS HELICOPTERS", "407"),
        ("AIRBUS HELICOPTERS", "R44"),
        ("BELL TEXTRON INC.", "AS 350"),
        ("BELL TEXTRON INC.", "407"),
        ("BELL TEXTRON INC.", "R44"),
        ("ROBINSON", "AS 350"),
        ("ROBINSON", "407"),
        ("ROBINSON", "R44"),
    ]
    assert ("ROBINSON", "R44") in pairs, "sin este par, un R44 real pierde el AD por el cap de 500"


def test_several_makes_without_models_declare_each_make_alone():
    # Sin lista de modelos no hay nada que cruzar: una fila por fabricante,
    # `medium` es lo maximo honesto que se puede declarar.
    assert _pairs("AIRBUS, BOEING", "") == [("AIRBUS", None), ("BOEING", None)]


def test_several_makes_produce_short_fields_instead_of_one_long_one():
    """El caso literal que rompio produccion, reducido.

    Antes se emitia UNA fila con los 457 chars concatenados: pasaba el limite de
    la RPC y ademas no habria coincidido con ninguna aeronave.
    """
    make = ", ".join(f"FABRICANTE NUMERO {i:03d} SOCIEDAD ANONIMA" for i in range(12))
    assert len(make) > populate_db.APPLICABILITY_TEXT_MAX
    pairs = _pairs(make, "MODELO A, MODELO B")
    assert len(pairs) == 24, "12 fabricantes x 2 modelos"
    assert all(len(mk) <= populate_db.APPLICABILITY_TEXT_MAX for mk, _ in pairs)
    assert all(mod in ("MODELO A", "MODELO B") for _, mod in pairs)


# ------------------------------------------------------------------ degenerados

def test_no_make_declares_nothing():
    # Sin fabricante no hay fila valida; el AD queda success_empty, no con una
    # fila que la RPC rechazaria.
    assert _pairs("", "A320") == []
    assert _pairs(None, None) == []


def test_blank_segments_are_dropped_not_emitted_as_empty_strings():
    assert _pairs("AIRBUS, , BOEING", "") == [("AIRBUS", None), ("BOEING", None)]
    assert _pairs("AIRBUS", "A320, , A321") == [("AIRBUS", "A320"), ("AIRBUS", "A321")]


# ------------------------------------------------------------------- batching

def _payload(n_ads, rows_each):
    return [
        {"ad_id": f"ad-{i}", "status": "success_with_rows",
         "rows": [{"make": "M", "model": f"X{j}"} for j in range(rows_each)]}
        for i in range(n_ads)
    ]


def test_batches_split_by_ad_count():
    batches = populate_db._applicability_batches(_payload(650, 1))
    assert [len(b) for b in batches] == [300, 300, 50]


def test_batches_split_by_accumulated_rows_not_only_by_ad_count():
    """D-24, segunda mitad: el producto cartesiano hace variable el numero de
    filas por AD (maximo observado en produccion: 1.247). Un lote de 300 ADs
    pesados puede superar las 20.000 filas que la RPC rechaza; el corte tiene
    que ir por filas acumuladas, no solo por numero de ADs.
    """
    # 30 ADs de 1.000 filas: por conteo de ADs cabrian todos en un lote, pero
    # 30.000 filas superan el limite del servidor.
    batches = populate_db._applicability_batches(_payload(30, 1000))
    assert len(batches) == 3
    for b in batches:
        assert sum(len(ad["rows"]) for ad in b) <= populate_db.APPLICABILITY_BATCH_ROWS


def test_an_ad_bigger_than_the_batch_bound_travels_alone():
    # Si aun asi la RPC lo rechaza, el fallo (ruidoso) queda acotado a ese AD
    # en vez de tumbar a otros 299.
    payload = _payload(2, 10) + _payload(1, populate_db.APPLICABILITY_BATCH_ROWS + 1)
    batches = populate_db._applicability_batches(payload)
    assert len(batches) == 2
    assert len(batches[1]) == 1
    assert len(batches[1][0]["rows"]) == populate_db.APPLICABILITY_BATCH_ROWS + 1


def test_batches_preserve_every_ad_and_the_order():
    payload = _payload(37, 7)
    batches = populate_db._applicability_batches(payload)
    flat = [ad for b in batches for ad in b]
    assert flat == payload


def test_the_client_bound_matches_the_rpc_bound():
    """Guarda de deriva.

    `replace_ad_applicability` valida `length(...) > 256` por campo y rechaza
    llamadas de mas de 500 ADs o 20.000 filas totales. Si la RPC cambia esos
    numeros y estas constantes no los siguen, se vuelve a mandar un payload que
    aborta el lote entero — que es exactamente D-21.
    """
    assert populate_db.APPLICABILITY_TEXT_MAX == 256
    assert populate_db.APPLICABILITY_BATCH_ADS <= 500
    assert populate_db.APPLICABILITY_BATCH_ROWS * 2 <= 20000, \
        "margen 2x bajo max_total_rows de la RPC"
