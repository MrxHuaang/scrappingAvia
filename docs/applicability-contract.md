# Contrato de extracción de applicability (INT-3)

Define qué evidencia autoriza a cada extractor a declarar el estado de un AD en el
payload de `replace_ad_applicability` (RPC en el proyecto Supabase, migración v2
`20260926130000`, junto con `20260926120000`). **El estado lo declara el extractor;
nunca se infiere de la forma del payload** — un `make` vacío **no** es evidencia de
nada por sí solo (ver sección de evidencia más abajo: esto cambió respecto a la
versión anterior de este documento).

## Los tres estados

| Estado | Semántica | Efecto en la BD |
|---|---|---|
| `success_with_rows` | El extractor obtuvo la lista de make/model del AD | Upsert de las filas + poda de las que ya no reporta (update solo si algo cambió) |
| `success_empty` | El extractor VIO la fuente, la fuente no lista applicability, **y lo declara con `empty_evidence`** (texto ≤256 chars) | Las filas existentes del AD se eliminan (0 filas es legítimo). **Sin `empty_evidence` la RPC degrada el elemento a `failed` en silencio** — el llamador debe leer `rows_now`/`status` de la respuesta para verlo (`populate_relations` lo hace y lo loguea) |
| `failed` | La extracción no pudo completarse, o no hay evidencia real de ausencia | No se toca nada de ese AD |

Atomicidad: **por lote** — cada llamada RPC es una transacción (≤500 ADs, ≤20000
filas totales, ≤8 MB). Un proceso muerto entre lotes deja los lotes anteriores
completos y los posteriores intactos; ningún AD queda a medias.

## Arquitectura actual del pipeline

En `populate_db.py`, `_applicability_pairs()` deriva pares (make, model) de la fila
normalizada:

- **Camino estructurado** (`raw_data["Applicability"] = [{make, models[]}]`,
  **EASA y FAA** — `easa.py` lo construye recorriendo `li.tc_holder` del
  listado/detalle; `federal_register.py` y `faa_govinfo.py` lo toman de
  `scrapers/common/fr_applicability.py`, ver más abajo): **una fila por approval holder**, con sus modelos unidos
  por `", "` en el orden de la fuente — no una fila por modelo. La
  tokenización de listas como `"737-600, -700, -800"` la hace Postgres
  (`ad_model_index`/`ad_model_family`, migración `20260923160000`), no el
  cliente; partir aquí solo arriesgaba fabricantes inventados sin ganar nada
  de matching.
- **Camino escalar** (Transport Canada/ANAC/DGAC/UK CAA, que nunca traen
  más de un fabricante o no exponen la estructura por holder): `make` +
  `model` completo de la fila, también en una sola fila, sin partir por coma.
- En ningún camino se parte `make` por coma (los nombres de holder traen comas
  propias; ver el docstring de `_applicability_pairs`).

### FAA (`scrapers/common/fr_applicability.py`)

Única extracción para Federal Register y GovInfo, sobre título + abstract:

- **Titular** = el approval holder del título ("Airworthiness Directives;
  <Titular> Airplanes|Helicopters|Turbofan Engines|Propellers|..."). Con "Type
  Certificate Previously Held by X" / "(Formerly X)" el titular es el ACTUAL y
  X viaja en `previously_held_by` dentro del mismo elemento — informativo,
  `_applicability_pairs` lo ignora.
- **Modelos** = la lista completa del título legado ("Boeing Model 747-100,
  747-200B Series Airplanes") o, si el título no la trae, del abstract ("for
  all/certain <Titular> Model A, B, and C airplanes"), tal como la escribe la
  fuente. Sin modelo extraíble el elemento va con `models: []` →
  `model_status=unknown`, nunca "aplica a todo".
- **Sin titular inequívoco** ("Various Aircraft", "X and Various Restricted
  Category Helicopters", dos fabricantes unidos por "and") no se emite
  `Applicability` ni `Make`: el AD viaja `failed` y sus filas guardadas no se
  tocan. No es `ApplicabilityError` (eso marcaría la corrida en error): es un
  dato que la fuente no da de forma atribuible.
- `Make`/`Model` escalares (`ads.make`/`ads.model`) salen del mismo resultado.
  Los alias de fabricante (Cessna = Textron Aviation, etc.) los resuelve la BD
  (`manufacturer_alias` + `ad_make_canonical`), no Python.
- `scripts/compare_applicability_coverage.py` compara esta extracción con la
  heurística anterior sobre un volcado JSON (sin red ni BD). Resultados,
  pérdidas conocidas y cómo correrlo antes de desplegar:
  [applicability-coverage.md](applicability-coverage.md).

Por AD:
- **Pares válidos** (≥1 tras deduplicar y validar límites) → `success_with_rows`.
- **`success_empty` no lo declara ningún extractor** (tabla más abajo). Exige que
  la fuente **declare explícitamente** "no hay pares", y ninguna fuente activa
  lo hace: "el parser/listado no trajo make" o "el selector no encontró holders"
  nunca es esa evidencia, es un dato ausente, y un dato ausente no autoriza a
  borrar lo ya guardado. Nunca se infiere del número de coincidencias de un
  selector.
- **Sin pares** (make vacío por cualquier motivo — título genérico como
  "Various Aircraft", AD de Appliance sin fabricante declarado, listado sin esa
  columna, lista de holders vacía, etc.), **o** con ≥1 fila
  descartada por límite de tamaño, **o** con `ApplicabilityError` (extracción que
  falló a medias) → `failed`: nunca se declara un subconjunto parcial como si
  fuera la verdad completa, ni se infiere ausencia de un dato simplemente
  faltante.
- **Un AD ausente de la salida del scraper** no aparece en el payload — equivale a
  `failed` implícito: sus filas almacenadas quedan intactas. Por esto la corrida
  nocturna nunca puede vaciar la applicability de un AD por un timeout o un cambio
  de HTML: ese AD simplemente no viene en la salida.

`model_status` por fila: `listed` si hay texto de modelo, `unknown` si no. `none`
(la fuente declara explícitamente "todos los modelos") no lo emite ninguna fuente
hoy — ninguna da una señal distinguible de esa declaración; el día que una la dé,
se define aquí antes de emitirlo.

`product_type` por fila: el código de catálogo (`aircraft/rotorcraft/engine/
propeller/appliance`) derivado de `enrich.normalize_product_type()` sobre el AD
completo (mapeo `Aircraft→aircraft, Helicopter→rotorcraft, Engine→engine,
Propeller→propeller, Appliance→appliance`). Cuando no hay tipo derivable se omite
el campo del payload — la RPC lo deriva server-side de `ads.product_type`
(`product_type_source='derived'`), lo que además deja la fila enganchada a
correcciones futuras de `ads.product_type`; mandar un código explícito la fija
como `'explicit'` para siempre. El mapeo es `enrich.PRODUCT_TYPE_CODE`, y la
clasificación FAA por título (`enrich.classify_product_type`) vive en el mismo
módulo.

`product_subtype` por fila (opcional): lo deriva `enrich.product_subtype()` del
mismo texto que decide el tipo (el título FAA, el subject de EASA/TC/UK CAA/ANAC)
y **solo viaja junto a `product_type`** — la RPC rechaza un subtipo sin tipo, y
la FK compuesta uno que no pertenezca a su tipo. Catálogo cerrado
(`enrich.PRODUCT_SUBTYPES`, copia del seed de la migración `20260926120000`):

| product_type | subtipos | de qué sale |
|---|---|---|
| `aircraft` | airplane, glider, balloon, airship, powered_lift | el sustantivo que decidió el tipo: "Airplanes", "Gliders"/"Sailplanes", "Balloons", "Airships" |
| `rotorcraft` | helicopter, gyroplane | "Helicopters", "Gyroplanes" |
| `engine` | turbine, piston, apu, electric | el calificativo pegado a "Engines": Turbofan/Turbojet/Turboprop/Turboshaft → turbine, Reciprocating/Piston/Diesel → piston, Electric → electric; "Auxiliary Power Units" → apu |
| `propeller` | — | nunca |
| `appliance` | seats, avionics, elt, oxygen, fire_protection, landing_gear, lighting, evacuation, instruments, other | la pieza nombrada antes de "Installed on/Equipped with" |

Reglas: sin coincidencia segura el campo se **omite** (nunca se adivina ni se
cae a `other`, que no se emite); si el texto apunta a dos categorías ("Oxygen
Cylinder and Fire Extinguisher Assemblies", "Reciprocating and Turboprop
Engines") también se omite; un sustantivo que va tras "Installed on" nombra
dónde va la pieza, no el producto. "Engines", "Aircraft" o "Rotorcraft" a secas
no dicen el subtipo. Un subtipo que no pertenece al tipo de la fila no se
produce nunca: la FAA tipa los helicópteros como `Aircraft`
(`classify_product_type`), así que sus filas van con `product_type=aircraft` y
**sin** subtipo, porque `helicopter` es de `rotorcraft`. Con un `product_type`
explícito y sin subtipo, la RPC deja el subtipo en null (también al actualizar
una fila existente).

`ApplicabilityError`: excepción que un scraper puede levantar (vía
`raw_data["Applicability_Error"]`) para señalar que la extracción de
applicability de un AD puntual falló a medias (ej. PDF ilegible en un detalle
por-AD). `_applicability_pairs` la levanta también si `raw_data["Applicability"]`
no se puede interpretar **entero**: no es una lista, o alguno de sus holders no
es un objeto, no trae `make` (string no vacío), trae `models` que no es una
lista (un string se recorrería como caracteres) o un modelo que no es string.
Un holder ilegible nunca se salta: los pares que quedan no son la applicability
completa, y declararlos `success_with_rows` haría que la RPC podara las filas
del holder perdido. Todo el AD viaja `failed`, sin filas.

## Evidencia de ausencia por fuente activa (`empty_evidence`)

Ninguna fuente activa da hoy una señal explícita y verificable de "este AD no
aplica a ningún producto", así que `populate_db` nunca envía `success_empty`.
Una fuente que la dé (una declaración de la fuente, distinta de que un
selector no encuentre nada) se documenta aquí, con un fixture del caso real,
antes de emitirla.

| Fuente | Qué autoriza `success_empty` | Qué es `failed` |
|---|---|---|
| `easa` | Nunca — `raw_data["Applicability"] == []` solo dice que `li.tc_holder` no coincidió con nada en la celda de holders: un cambio de clase o de estructura da la misma lista vacía que un AD sin holders, y el listado no trae ninguna declaración explícita de ausencia | Lista de holders vacía o ausente; un holder cuyo nombre no se lee (envuelto en otro elemento, sección cortada: `easa.py` lo emite con `make` vacío y el AD entero falla); ≥1 fila descartada por tamaño; timeout/HTTP ≠ 200; parse error; AD ausente de la salida |
| `transport_canada` | Nunca — el scraper **no emite** `raw_data["Applicability"]`, así que no hay forma de distinguir "sin holders" de "no pude leerlo": ambiguo, no se emite evidencia | Cualquier make vacío; timeout/HTTP ≠ 200; layout de tabla no reconocido; AD ausente de la salida |
| `federal_register` | Nunca — un AD de tipo `Appliance` **sí** tiene applicability (el fabricante/pieza del propio appliance); declararlo `success_empty` borraría filas reales. Ni el tipo de producto ni un título genérico son la fuente afirmando "no hay pares" | Cualquier make vacío (Appliance incluido), o sea título sin titular inequívoco ("Various ..."); HTTP ≠ 200 de la API, JSON malformado, documento descartado por identidad (sin AD en `docket_ids` ni amendment); AD ausente de la salida |
| `anac_brazil`, `dgac_chile`, `anac_argentina`, `uk_caa` | Nunca — un listado sin make es el scraper **no extrayendo** el dato, no la fuente declarando que el AD no aplica a nada | Cualquier fila sin make; timeout/HTTP ≠ 200 del listado; layout no reconocido; AD ausente de la salida |

En todas las fuentes **todo make vacío viaja `failed`**: las filas de
`ad_applicability` que ya existían para ese AD quedan intactas hasta que una
corrida futura consiga extraer el dato o hasta una limpieza manual explícita.

Reglas transversales:
- Ningún extractor emite filas con `make` vacío (la RPC lo rechaza con 22023).
- `make` ≤256 chars (`APPLICABILITY_MAKE_MAX`), `model` ≤2000 chars
  (`APPLICABILITY_MODEL_MAX`) — límites separados desde la migración v2; una fila
  que se pase de cualquiera de los dos se descarta y marca su AD `failed` (nunca
  parcial).
- Duplicados de clave natural dentro de un AD se deduplican en Python **antes** de
  declarar (la RPC rechaza el payload completo si los encuentra).
- Un lote que Postgres aborta por deadlock (SQLSTATE `40P01`, en `code` del
  error de PostgREST) se reenvía **una vez**, idéntico, como una llamada RPC
  nueva: la transacción abortada ya se revirtió entera. Si el reenvío falla,
  queda su error; ningún otro error se reintenta.
- Todo fallo de la RPC incrementa `write_errors` → `status='error'` en `scrape_runs`
  y exit ≠ 0 del proceso (regla de la Fase 2a). `populate_relations` además lee
  `status` de la respuesta de la RPC (autoridad real, no lo declarado) y loguea
  cuántos ADs quedaron `failed` según la BD.
