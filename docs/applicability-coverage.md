# Cobertura de la extracción de applicability FAA

Mide qué tan bien `scrapers/common/fr_applicability.py` (la única extracción de
titular + modelos para Federal Register y GovInfo) cubre los ADs de la FAA,
comparada con la heurística anterior (`utils.extract_make_model`, eliminada:
buscaba el primero de 38 fabricantes fijos en título + abstract y un solo token
de modelo). El contrato de lo que se emite está en
[applicability-contract.md](applicability-contract.md).

## Cómo se produce el reporte

`scripts/compare_applicability_coverage.py` corre **sin red ni BD** sobre un
volcado JSON y clasifica cada AD, para cada extractor, en tres cubos:

| Cubo | Qué significa en la BD |
|---|---|
| titular + modelos | fila con `model_status=listed` |
| titular sin modelo | fila con `model_status=unknown` |
| sin titular | el AD viaja `failed`: sus filas guardadas no se tocan |

```bash
pip install -e .    # o PYTHONPATH=. delante del comando
python scripts/compare_applicability_coverage.py tests/fixtures/fr_applicability.json
python scripts/compare_applicability_coverage.py dump.json --examples 10 --json outputs/coverage.json
```

`--examples N` imprime los primeros N ADs que cambiaron en cada transición
(legado -> nuevo); `--json` guarda el reporte completo con todos los ejemplos.
Formatos de entrada aceptados (se detectan por ítem): documentos de la FR API
(`title`, `abstract`) como lista, `{"results": [...]}` o `{"<año>": [...]}`;
registros del scraper (`Subject`, `Abstract`); filas de `ads` con `raw_data`.

### El volcado de la muestra de 1754 documentos

Son todos los documentos `RULE` de CFR 14 parte 39 de cuatro años completos
(1998: 725, 2006: 442, 2015: 307, 2024: 280), bajados con una petición por año
a la API pública, con los mismos filtros que `federal_register._search_year()`:

```
GET https://www.federalregister.gov/api/v1/documents.json
    ?conditions[cfr][title]=14&conditions[cfr][part]=39&conditions[type][]=RULE
    &conditions[publication_date][gte]=<año>-01-01
    &conditions[publication_date][lte]=<año>-12-31
    &per_page=1000&order=oldest
    &fields[]=title&fields[]=abstract&fields[]=document_number
    &fields[]=publication_date&fields[]=docket_ids&fields[]=type&fields[]=action
```

Se guarda como `{"1998": [results...], "2006": [...], ...}` y se pasa tal cual
al script. Un año nunca supera 1000 documentos, así que una página por año
basta. Los años se eligieron para cubrir los formatos de título: legado con
modelos en el título (1998, 2006) y moderno con modelos solo en el abstract
(2015, 2024).

## Resultados

### Fixtures (`tests/fixtures/fr_applicability.json`, 42 documentos reales)

Elegidos a propósito entre los casos difíciles (motores legados, hélices,
appliances, "Type Certificate Previously Held by", "Various ..."), así que no
representan la distribución real: sirven de regresión, no de estimación.

| | legado | nuevo |
|---|---:|---:|
| titular + modelos | 19 (45,2 %) | 28 (66,7 %) |
| titular sin modelo (unknown) | 5 (11,9 %) | 9 (21,4 %) |
| sin titular (failed) | 18 (42,9 %) | 5 (11,9 %) |
| titular previo conservado | — | 7 (16,7 %) |

Los diez últimos fixtures cubren el tipo de producto (más abajo: appliances, un
helicóptero, el avión con ", Equipped With ... Engines"), dos de ellos con
número de parte en el título; por eso sube la proporción de titulares sin
modelo: un appliance rara vez trae una designación de modelo.

### Muestra de 1754 documentos (1998, 2006, 2015, 2024)

| | legado | nuevo |
|---|---:|---:|
| titular + modelos | 1101 (62,8 %) | 1690 (96,4 %) |
| titular sin modelo (unknown) | 180 (10,3 %) | 41 (2,3 %) |
| sin titular (failed) | 473 (27,0 %) | 23 (1,3 %) |
| titular previo conservado | — | 90 (5,1 %) |

Además, en 984 ADs (56,1 %) el nuevo lista más de un modelo, donde el legado
se quedaba con uno solo y a menudo cortado ("A300-600 and", "737-8 and"). Solo
122 pares (7,0 %) quedan idénticos: el resto cambia sobre todo porque el
titular ahora es el nombre completo del título ("Cessna Aircraft Company" en
vez de "Cessna") y la lista de modelos está completa. Los alias de fabricante
los resuelve la BD, así que el nombre completo no pierde matching.

Transiciones legado -> nuevo sobre la muestra:

| Transición | ADs |
|---|---:|
| titular + modelos -> titular + modelos | 1090 |
| sin titular -> titular + modelos | 436 |
| titular sin modelo -> titular + modelos | 164 |
| sin titular -> titular sin modelo | 26 |
| titular sin modelo -> titular sin modelo | 11 |
| sin titular -> sin titular | 11 |
| titular + modelos -> sin titular | 7 |
| titular sin modelo -> sin titular | 5 |
| titular + modelos -> titular sin modelo | 4 |

## Pérdidas conocidas

Las 16 transiciones que empeoran (las tres últimas filas), más los casos que
siguen sin resolverse, caen en estos grupos. Ninguna borra datos: un AD sin
titular viaja `failed` y conserva sus filas.

- **Nombre de titular con "and"**. La regla "dos titulares unidos por *and* =
  ambiguo" también descarta nombres que llevan la palabra: "Airbus Defense and
  Space S.A. (Formerly Known as Construcciones Aeronauticas, S.A.) Airplanes"
  (2023-28846, 2024-28787). El legado daba "Airbus" + "CN-235", que era el
  fabricante equivocado de todas formas. "Saab AB, Support and Services" queda
  como "Saab AB".
- **"Various" en el título**, aunque nombre un fabricante: "Various
  Sikorsky-Manufactured Transport and Restricted Category Helicopters"
  (2015-25976), "Bell Textron Inc. and Various Restricted Category Helicopters"
  (2024-22095), "General Electric Company Engines, and Various Restricted
  Category Rotorcraft" (2024-05547). Es deliberado: declarar solo al fabricante
  nombrado sería un subconjunto parcial.
- **Appliances instalados en aeronaves de otro titular**. "Goodrich Evacuation
  Systems ... Installed on Airbus Model A330-200 ..." (06-5208) queda
  "Goodrich" sin modelos: los modelos Airbus del título son de la aeronave, no
  del appliance, y el legado los atribuía a Airbus. Lo mismo con "Arrow Falcon
  Exporters, Inc. (Previously Utah State University); ..." (06-7577).
- **Appliances con número de parte en el título**. "Goodyear Aviation Tires,
  Part Number 217K22-1, Installed on ..." (E6-14636) daba el titular "Goodyear
  Aviation Tires, Part", y "SAFT America Inc. Part Number (P/N) ..." (98-25124)
  "SAFT America Inc. Part Number (P/N)". Ahora el título se corta en "Part
  Number"/"P/N": SAFT queda "SAFT America Inc."; Goodyear queda sin titular
  (`failed`), porque el abstract no nombra a Goodyear y "Goodyear Aviation
  Tires" no es un titular inequívoco. Es la transición extra de
  *titular + modelos -> sin titular*.
- **Calificativo que corta un segundo grupo de modelos**. "Boeing Model
  777-200LR Series Airplanes Powered by ... Engines, and Model 777-300ER Series
  Airplanes ..." se queda con 777-200LR: el corte en "Powered by" evita tomar
  los motores como modelos, y con ellos se pierde lo que viene después.
- **Designaciones sin número**. "Rolls-Royce Limited (R-R) Dart series turboprop
  engines", "General Electric Company (GE) GEnx turbofan engine models": el
  titular sale, los modelos no (`unknown`). El legado tampoco los tenía.
- **Abstract vacío o genérico**. "The Boeing Company Airplanes" con abstract
  "for certain The Boeing Company Model airplanes" (2015-27190): el legado
  tomaba "airplanes" como modelo; el nuevo deja el modelo en `unknown`.
- **Títulos con dos fabricantes reales** ("Textron Lycoming and Teledyne
  Continental Motors Reciprocating Engines", "Schweizer Aircraft Corporation and
  Hughes Helicopters, Inc. Model 269A ..."): sin titular, como pide el contrato.

## Tipo de producto FAA: antes y después

`enrich.classify_product_type` (la regla única de FR y GovInfo) ahora tipa
Helicopter los títulos que cierran en "Helicopters"/"Rotorcraft"/"Gyroplanes",
y Appliance los que tienen una pieza como producto: una pieza sin sustantivo de
producto ("Passenger Seat Assemblies"), "<pieza> Installed on / Approved Under
... Airplanes", o un sustantivo que solo califica a la pieza que sigue
("Reciprocating Engine Connecting Rods", "RECARO Aircraft Seating ... Seats").
"Auxiliary Power Units" es Engine. Un sustantivo dentro del nombre del titular
no decide ("Sikorsky Aircraft Corporation", "Lycoming Engines"): el titular se
identifica con la misma extracción de applicability y, si el título no nombra
producto fuera de él, decide la oración de applicability del abstract (su
primer sustantivo de producto, o una pieza) y después la regla vieja. Los
valores siguen dentro de `Aircraft|Engine|Propeller|Appliance|Helicopter` (el
check `ads_product_type_chk` y `ad_product_type_code` de la BD).

Distribución sobre la muestra de 1754 documentos:

| `ads.product_type` | antes | después |
|---|---:|---:|
| Aircraft | 1541 | 1356 |
| Helicopter | 0 | 162 |
| Engine | 198 | 190 |
| Appliance | 0 | 31 |
| Propeller | 15 | 15 |

Transiciones (195 ADs; todas explicables):

| Transición | ADs | Por qué | Ejemplos |
|---|---:|---|---|
| Aircraft -> Helicopter | 162 | el sustantivo que decide es Helicopters/Rotorcraft (en el título o, si el título solo nombra al titular, en la oración de applicability) | 98-2969 "Eurocopter France Model SA-365N ... Helicopters", 98-5353 "Robinson Helicopter Company Model R44 Helicopters", 2015-28313 "Sikorsky Aircraft Corporation (Type Certificate Previously Held by Schweizer ...)" con abstract "... Model 269A ... helicopters" |
| Aircraft -> Appliance | 25 | pieza como producto (sin sustantivo, tras "Installed on/Approved Under", o nombrada en la oración de applicability) | 2015-11392 "Zodiac Seats France ... Passenger Seat Assemblies", 06-5208 "Goodrich Evacuation Systems Approved Under TSO ... Installed on Airbus ...", E6-13911 "RECARO Aircraft Seating ... Seats", 2015-19474 "Kidde Graviner" ("... hand-operated fire extinguishers") |
| Engine -> Appliance | 6 | pieza de motor o instalada en un motor | 98-70 "Precision Airmotive Corporation Carburetors", 98-24089 "Superior Air Parts, Inc., Piston Pins Installed on ... Reciprocating Engines", 06-4646 "ECi Reciprocating Engine Connecting Rods" |
| Engine -> Aircraft | 2 | ", Equipped With" tras la coma ya corta el calificativo; título que solo nombra al titular y abstract "for all ... Model 757 airplanes" (la regla vieja caía en "engine" por la prosa) | E6-18663 "Airbus Model A300 Airplanes, Equipped With General Electric CF6-50 Series Engines", 2015-23120 "The Boeing Company" |

**Caso mixto conocido.** "General Electric Company Engines, and Various
Restricted Category Rotorcraft" (2024-05547, 2024-07438) es Helicopter porque
"Rotorcraft" es el último sustantivo; el AD cubre motores y aeronaves y un solo
tipo no lo describe. Ya era Aircraft por la misma razón.

Subtipos resultantes (`enrich.product_subtype`): airplane 1292, helicopter
160, turbine 143, glider 59, piston 13, balloon 1, seats 5, oxygen 4, avionics
3, fire_protection 3, evacuation 2, landing_gear 2; sin subtipo 67 (Engine 34,
Propeller 15, Appliance 12 — carburadores, bombas, bielas, baterías: el
catálogo no tiene su categoría y `other` no se emite —, Aircraft 4,
Helicopter 2).

Las filas ya guardadas conservan el tipo de la migración
`20260923180000_fr_product_type_from_title.sql` (regla anterior) hasta que la
corrida las vuelva a escribir.

## Antes de desplegar: correrlo sobre un volcado de tamaño producción

La muestra cubre cuatro años; la base FAA tiene ~13k ADs de 1994 en adelante.
Antes de dejar que la corrida nocturna reemplace la applicability FAA, conviene
correr el script sobre el corpus completo y revisar las transiciones que
empeoran.

1. **Volcado de la BD local** (ya contiene `Subject` y `Abstract` en `raw_data`):

   ```bash
   psql "postgresql://postgres:postgres@127.0.0.1:54322/postgres" -At -c \
     "select json_agg(json_build_object('ad_number', ad_number, 'raw_data', raw_data))
        from ads where source = 'federal_register'" > outputs/fr_ads_dump.json
   python scripts/compare_applicability_coverage.py outputs/fr_ads_dump.json \
     --examples 20 --json outputs/fr_coverage.json
   ```

2. **O desde la FR API**, con el índice de identidad (trae título y abstract de
   todos los años). El índice es un objeto `{document_number: {...}}`, así que
   hay que pasarlo a lista:

   ```bash
   python scripts/fr_identity.py          # outputs/fr_identity.json, una petición por año
   python -c "import json; d=json.load(open('outputs/fr_identity.json', encoding='utf-8')); \
   json.dump([{'document_number': k, **v} for k, v in d.items()], \
   open('outputs/fr_identity_list.json', 'w', encoding='utf-8'))"
   python scripts/compare_applicability_coverage.py outputs/fr_identity_list.json --examples 20
   ```

Qué mirar en la salida:

- **sin titular (failed)** debería quedar cerca del 1-2 % de la muestra. Esos
  ADs no pierden filas, pero tampoco las ganan: si el porcentaje sube mucho en
  algún rango de años, hay un formato de título que la extracción no reconoce.
- **titular + modelos -> sin titular** y **titular + modelos -> titular sin
  modelo**: revisar los ejemplos uno por uno. Deberían ser de los grupos de
  arriba; uno que no encaje es un caso nuevo para `tests/fixtures`.
- **holder_differs**: titular nuevo que no contiene al del legado. Útil para
  encontrar titulares cortados como el de Goodyear.
