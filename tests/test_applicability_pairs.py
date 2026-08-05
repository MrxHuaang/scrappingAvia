"""D-21: lo que se le manda a Postgres no puede tumbar el lote entero, y lo que
entra en `ad_applicability` no puede ser un fabricante inventado.

D-21 aparecio el 2026-08-04, la primera vez en 19 dias que EASA y Transport
Canada llegaron a la fase de escritura de relaciones. El timeout lo habia estado
ocultando: una fuente que nunca termina de enriquecer nunca llega a escribir, y
lo que no se ejecuta no falla.

`replace_ad_applicability` valida el payload por LLAMADA, antes del triestado por
AD, asi que una sola fila invalida aborta hasta 300 ADs. Dos ADs de EASA con
`make` de 457 y 494 chars tumbaron dos lotes completos.

El `make` largo era el sintoma ruidoso. La causa: EASA lista N approval holders
por AD y el scraper los unia con ", ". Dos caminos posibles para deshacerlo, y
solo uno es correcto:

  * partirlo por coma NO funciona — los nombres traen comas propias ("REVO,
    Inc.", "McDermott 214 Holdings, LLC"), y sobre las 765 filas afectadas
    produce 1972 tokens de los que 316 son sufijos societarios ("INC." x194,
    "LLC" x56, "GmbH" x42): 288 ADs quedarian con al menos un fabricante falso,
    servido despues como opcion del filtro Make por `aircraft_makes()`;
  * conservar la estructura del origen SI funciona — cada `li.tc_holder` del
    listado de EASA anida sus propios `li.type`, asi que el scraper puede emitir
    `raw_data["Applicability"] = [{make, models[]}]` y cada holder mantiene sus
    modelos, en `high`, sin cruzarlos con los de otro fabricante.
"""

import populate_db


def _pairs(make, model, applicability=None):
    row = {"make": make, "model": model}
    if applicability is not None:
        row["raw_data"] = {"Applicability": applicability}
    return populate_db._applicability_pairs(row)


# ------------------------------------------------- estructura por approval holder

def test_each_holder_keeps_its_own_models():
    """La atribucion que el producto cartesiano habria falseado.

    El AD 15-117 lista 9 titulares de TC y 60 modelos; cruzarlos pondria el R44
    de Robinson bajo Airbus Helicopters. Con la estructura del origen no hay que
    elegir entre inventar y perder el modelo.
    """
    pairs = _pairs(
        "AIRBUS HELICOPTERS", "AS 350 B, 407, R44",
        applicability=[
            {"make": "AIRBUS HELICOPTERS", "models": ["AS 350 B"]},
            {"make": "BELL TEXTRON INC.", "models": ["407"]},
            {"make": "ROBINSON", "models": ["R44"]},
        ],
    )
    assert pairs == [
        ("AIRBUS HELICOPTERS", "AS 350 B"),
        ("BELL TEXTRON INC.", "407"),
        ("ROBINSON", "R44"),
    ]


def test_holder_without_models_is_declared_make_only():
    assert _pairs("KAMOV", "", applicability=[{"make": "KAMOV", "models": []}]) == [
        ("KAMOV", None),
    ]


def test_commas_inside_a_holder_name_survive():
    """El caso que hace irreversible el join: la coma es parte del nombre."""
    pairs = _pairs(
        "REVO, Inc.", "Lake LA-4",
        applicability=[
            {"make": "REVO, Inc.", "models": ["Lake LA-4"]},
            {"make": "McDermott 214 Holdings, LLC", "models": []},
        ],
    )
    assert pairs == [
        ("REVO, Inc.", "Lake LA-4"),
        ("McDermott 214 Holdings, LLC", None),
    ]


def test_malformed_entries_are_skipped():
    pairs = _pairs("BOEING", "737-800", applicability=[
        {"make": "  ", "models": ["X"]},
        "no soy un dict",
        {"make": "BOEING", "models": ["737-800", "  ", None]},
    ])
    assert pairs == [("BOEING", "737-800")]


# --------------------------------------------------------------- camino escalar

def test_one_make_with_several_models_pairs_each_model():
    assert _pairs("AIRBUS", "A320, A321, A330") == [
        ("AIRBUS", "A320"),
        ("AIRBUS", "A321"),
        ("AIRBUS", "A330"),
    ]


def test_one_make_without_model_still_declares_the_make():
    assert _pairs("AIRBUS", "") == [("AIRBUS", None)]
    assert _pairs("AIRBUS", None) == [("AIRBUS", None)]


def test_a_joined_make_is_never_split_into_fabricated_manufacturers():
    """Sin estructura, `make` viaja entero: partirlo produciria "INC." y "LLC".

    La fila resultante no matcheara ninguna aeronave, pero eso es visible y
    corregible; un fabricante inventado en el desplegable Make no lo es. Si
    ademas pasa el limite de la RPC, lo corta el guard de APPLICABILITY_TEXT_MAX
    en populate_relations, que lo reporta.
    """
    joined = "AIRBUS HELICOPTERS, BELL TEXTRON INC., ROBINSON"
    assert _pairs(joined, "") == [(joined, None)]


def test_empty_structure_falls_back_to_the_scalar_fields():
    assert _pairs("Embraer", "ERJ-190", applicability=[]) == [("Embraer", "ERJ-190")]


# ------------------------------------------------------------------ degenerados

def test_no_make_declares_nothing():
    # Sin fabricante no hay fila valida; el AD queda success_empty, no con una
    # fila que la RPC rechazaria.
    assert _pairs("", "A320") == []
    assert _pairs(None, None) == []


def test_blank_model_segments_are_dropped_not_emitted_as_empty_strings():
    assert _pairs("AIRBUS", "A320, , A321") == [("AIRBUS", "A320"), ("AIRBUS", "A321")]


def test_the_client_bound_matches_the_rpc_bound():
    """Guarda de deriva.

    `replace_ad_applicability` valida `length(...) > 256` por campo. Si la RPC
    sube o baja ese numero y esta constante no lo sigue, se vuelve a mandar un
    campo que aborta el lote entero — que es exactamente D-21.
    """
    assert populate_db.APPLICABILITY_TEXT_MAX == 256
