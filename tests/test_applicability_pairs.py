"""D-21 + refactor 2026-09 (PR 1/2, applicability v2): lo que se le manda a
Postgres no puede tumbar el lote entero, y lo que entra en `ad_applicability`
no puede ser un fabricante inventado NI un modelo partido a mitad de camino.

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

Refactor 2026-09: el paso siguiente aplico la misma logica al MODELO. Antes,
`_applicability_pairs` partia el string de modelos por coma y emitia una fila
por modelo ("737-600, -700, -800" -> 3 filas). Eso pierde la forma "-700"
(subserie sin el prefijo) en cuanto se separa de su hermano, y no gana nada:
Postgres ya tokeniza el texto completo del modelo para el matching de flota
(`ad_model_index`/`ad_model_family`, migracion `20260923160000`). El contrato
nuevo es: **un approval holder = una fila**, con todo su texto de modelos
unido por ", " en el orden de la fuente, y la RPC v2 (`20260926130000`) reserva
un limite de 2000 chars para `model` justo para esto (256 para `make`).
"""

import populate_db


def _pairs(make, model, applicability=None, extra_raw=None):
    row = {"make": make, "model": model}
    raw = dict(extra_raw or {})
    if applicability is not None:
        raw["Applicability"] = applicability
    if raw:
        row["raw_data"] = raw
    return populate_db._applicability_pairs(row)


# ------------------------------------------------- estructura por approval holder

def test_each_holder_is_one_row_with_all_its_models_joined():
    """El AD 15-117 lista 9 titulares de TC y 60 modelos; cruzarlos pondria el R44
    de Robinson bajo Airbus Helicopters. Con la estructura del origen no hay que
    elegir entre inventar y perder el modelo — y ahora tampoco se parte por holder.
    """
    pairs = _pairs(
        "AIRBUS HELICOPTERS", "AS 350 B, 407, R44",
        applicability=[
            {"make": "AIRBUS HELICOPTERS", "models": ["AS 350 B", "AS 355"]},
            {"make": "BELL TEXTRON INC.", "models": ["407"]},
            {"make": "ROBINSON", "models": ["R44"]},
        ],
    )
    assert pairs == [
        ("AIRBUS HELICOPTERS", "AS 350 B, AS 355"),
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


def test_a_subseries_list_is_not_split_it_travels_as_one_row():
    """El caso que motivo el refactor: "-700" solo tiene sentido junto a "737-600"."""
    pairs = _pairs(
        "BOEING", "",
        applicability=[{"make": "BOEING", "models": ["737-600, -700, -800, -900"]}],
    )
    assert pairs == [("BOEING", "737-600, -700, -800, -900")]


# ------------------------------------------------ ApplicabilityError (enganche)

def test_applicability_error_is_raised_when_the_scraper_flags_it():
    """Enganche para extractores futuros que procesen detalle por-AD: una
    extraccion que fallo a medias debe poder marcar el AD como `failed` sin
    fingir un `success_empty` ni un `success_with_rows` parcial.
    """
    row = {"make": "", "model": "", "raw_data": {"Applicability_Error": "PDF illegible"}}
    try:
        populate_db._applicability_pairs(row)
        assert False, "debia lanzar ApplicabilityError"
    except populate_db.ApplicabilityError as e:
        assert "PDF illegible" in str(e)


# --------------------------------------------------------------- camino escalar

def test_one_make_with_several_models_travels_as_one_row_not_split():
    assert _pairs("AIRBUS", "A320, A321, A330") == [
        ("AIRBUS", "A320, A321, A330"),
    ]


def test_one_make_without_model_still_declares_the_make():
    assert _pairs("AIRBUS", "") == [("AIRBUS", None)]
    assert _pairs("AIRBUS", None) == [("AIRBUS", None)]


def test_a_joined_make_is_never_split_into_fabricated_manufacturers():
    """Sin estructura, `make` viaja entero: partirlo produciria "INC." y "LLC".

    La fila resultante no matcheara ninguna aeronave, pero eso es visible y
    corregible; un fabricante inventado en el desplegable Make no lo es. Si
    ademas pasa el limite de la RPC, lo corta el guard de
    APPLICABILITY_MAKE_MAX en populate_relations, que lo reporta.
    """
    joined = "AIRBUS HELICOPTERS, BELL TEXTRON INC., ROBINSON"
    assert _pairs(joined, "") == [(joined, None)]


def test_empty_structure_falls_back_to_the_scalar_fields():
    assert _pairs("Embraer", "ERJ-190", applicability=[]) == [("Embraer", "ERJ-190")]


# ------------------------------------------------------------------ degenerados

def test_no_make_declares_nothing():
    # Sin fabricante no hay fila valida; el AD queda success_empty (con
    # evidencia) o failed mas abajo, no con una fila invalida.
    assert _pairs("", "A320") == []
    assert _pairs(None, None) == []


def test_the_client_bounds_match_the_rpc_bounds():
    """Guarda de deriva.

    `replace_ad_applicability` v2 valida `length(make) > 256` y
    `length(model) > 2000` por campo, por separado. Si la RPC cambia
    cualquiera de los dos y estas constantes no lo siguen, se vuelve a mandar
    un campo que aborta el lote entero — que es exactamente D-21.
    """
    assert populate_db.APPLICABILITY_MAKE_MAX == 256
    assert populate_db.APPLICABILITY_MODEL_MAX == 2000
    assert populate_db.APPLICABILITY_BATCH_ADS <= 500
    assert populate_db.APPLICABILITY_BATCH_ROWS * 2 <= 20000, \
        "margen 2x bajo max_total_rows de la RPC"


# ------------------------------------------------------------------- batching
#
# Un AD con varios approval holders puede aportar muchas filas (peor visto en
# produccion: >1200 para un solo AD EASA), asi que un lote fijo por CANTIDAD DE
# ADS puede igual pasarse del tope de filas de la RPC (max_total_rows). El
# corte tiene que ir tambien por filas acumuladas.

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
    # 30 ADs de 1000 filas: por conteo de ADs cabrian todos en un lote, pero
    # 30000 filas superan el limite del servidor.
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


# ------------------------------------------------ identidad del texto de modelo
#
# El contrato nuevo es "no tokenizar en Python": cualquiera de estas formas
# reales debe sobrevivir intacta (solo btrim), sea cual sea su puntuacion —
# listas con coma, "and", "/", parentesis, subseries sin prefijo. Postgres
# hace la tokenizacion real para el matching de flota.

_REAL_MODEL_STRINGS = [
    "737-600, -700, -800, -900",
    "A320-211, A320-212, A320-214, A320-231, A320-232, A320-233",
    "PA-28-140, PA-28-150, PA-28-151, PA-28-160, PA-28-161",
    "206B, 206B3, 206L, 206L-1, 206L-3, 206L-4",
    "DHC-8-102, DHC-8-103, DHC-8-106",
    "SA 315B, SA 316B, SA 316C, SA 319B",
    "Model 172 and Model 172RG",
    "Cessna 172 and 172S (Skyhawk SP)",
    "PA-31-310, PA-31-325, PA-31-350 (Navajo/Chieftain series)",
    "EC120 B / EC130 B4 / EC130 T2",
    "AS 350 B, B1, B2, B3, BA and BB",
    "R22 Beta, R22 Beta II",
    "R44, R44 II, R44 Cadet",
    "MD 900 / MD 902 (Explorer)",
    "S-76A, S-76B, S-76C, S-76C+, S-76C++",
    "Bell 407, 407GX, 407GXi, 407GXP",
    "King Air 200, 200C, 200T, B200, B200C",
    "Learjet 35, 35A, 36, 36A",
    "Gulfstream G-IV, G-IV-X, G-IV-SP",
    "Falcon 900, 900B, 900C, 900DX, 900EX",
    "ATR 42-300, 42-320, 42-500",
    "ATR 72-101, 72-102, 72-201, 72-202, 72-212, 72-212A",
    "Dash 8-Q400 (all series)",
    "737 MAX 7, MAX 8, MAX 9, MAX 10",
    "A330-200, A330-200F, A330-300, A330-800, A330-900",
    "PC-12/45, PC-12/47, PC-12/47E",
    "TBM 700, TBM 850, TBM 900, TBM 910, TBM 940",
    "Robinson R66 (all serial numbers)",
    "AW109E, AW109S, AW109SP (Grand New)",
    "Bell 429, 429 WLG",
    "EMB-110P1, EMB-110P2 (Bandeirante)",
    "Saab 340A, 340B, 340B+ (Plus)",
    "Model 1900C, 1900D (Beechcraft)",
    "Airbus A350-941, A350-1041",
]


def test_model_text_identity_preserved_across_real_world_strings():
    """>=30 cadenas reales, cada una debe sobrevivir intacta al helper (solo
    strip), sea cual sea su puntuacion interna."""
    assert len(_REAL_MODEL_STRINGS) >= 30
    for s in _REAL_MODEL_STRINGS:
        assert _pairs("SOME MAKE", s) == [("SOME MAKE", s)]
        # Y el mismo texto sobrevive igual dentro de un holder estructurado.
        assert _pairs("SOME MAKE", "", applicability=[
            {"make": "SOME MAKE", "models": [s]}
        ]) == [("SOME MAKE", s)]
