# Texto de los ADs FAA y campos derivados

El texto regulatorio de cada AD FAA se baja de GovInfo, se parte en secciones
anclado al encabezado de identidad del AD, se guarda en el cache full-text y
desde ahí va a la base. De esas mismas secciones salen `compliance_time`,
`amoc_available` y `effective_date`. Este documento describe el recorrido y
qué significa cada estado; el formato de las secciones y las reglas de
anclaje están en "Texto del AD en secciones" de CLAUDE.md.

## Recorrido

1. **Descarga** (`scrapers/federal_register._fetch_fulltext_fields`): un GET
   por documento FR a `api.govinfo.gov/packages/FR-{fecha}/granules/{doc}/htm`
   con `GOVINFO_API_KEY`, en paralelo por página de resultados
   (`_prefetch_fulltext`). Sin la clave no hay intento y nada cambia.
2. **Parseo** (`enrich.split_ad_sections`): el bloque del AD esperado (el de
   `docket_ids` o el respaldo anclado al amendment) en secciones
   `{position, letter, heading, kind, body}`, y el estado del intento.
3. **Cache v2** (`outputs/fr_fulltext_cache.json`): una entrada por documento
   con el estado, el anchor, las secciones, el sha256 del granulo y `detail`,
   el motivo cuando el intento no dejó texto: "GovInfo HTTP 500", "GovInfo
   request failed: ConnectionError", "no identity header declares the
   expected AD and amendment", "empty GovInfo response or access page",
   "anchored block without sections" o "GovInfo body could not be parsed".
4. **Registro FR** (`_parse_document`): de la entrada salen los modelos de
   applicability, `Compliance_Time`, `AMOC_Available` y `Effective_Date` (ver
   más abajo).
5. **Base** (`populate_db.populate_text_sections`, dentro de
   `populate_relations`, solo `federal_register`): el intento y sus secciones
   por `replace_ad_text_sections`, un AD por llamada (una transacción por AD).

## Estados

El parser declara lo que encontró; la base decide qué se guarda. Python no
decide si el texto está completo: lo decide `ad_text_is_complete` en la base
(hay `applicability`, `compliance` o `required_actions`, y
`unsafe_condition`, todas con cuerpo).

| Intento (parser) | Significado | Se reintenta | Lleva secciones | Lo que guarda la base (`last_attempt_status`) |
|---|---|---|---|---|
| `extracted` | Bloque anclado con al menos una sección | No (definitivo) | Sí | `ok` si está completo: reemplaza secciones y `text_*`. `partial` si no: las guarda solo si el AD no tenía texto conservado |
| `unanchored` | Ningún encabezado declara el AD con su amendment | No (definitivo) | No | `unanchored`; no toca secciones |
| `empty` | Respuesta vacía o página "request access" | Sí, en la corrida siguiente | No | `empty`; no toca secciones |
| `error` | HTTP distinto de 2xx, red, bloque sin secciones o parseo fallido | Sí, en la corrida siguiente | No | `error`; no toca secciones |

`ad_text_documents` separa el **texto conservado** (`text_provider`,
`text_document_id`, `text_source_url`, `text_sha256`, `text_anchor`,
`text_fetched_at`: solo cambian cuando un intento lo reemplaza) del **último
intento** (`last_attempt_at`, `last_attempt_status`, `last_attempt_detail`:
siempre se escriben). Un `error` o un `unanchored` después de un texto válido
deja ese texto y sus secciones intactos y solo mueve `last_attempt_*`. El
cliente no deja de mandar un intento fallido para proteger el texto: esa
garantía es de la base, y el intento fallido es información: dice cuándo y
por qué el texto conservado dejó de poder renovarse.

## Qué se manda a la base

```json
{
  "p_ad_id": "<ads.id>",
  "p_attempt": {
    "status": "extracted | unanchored | empty | error",
    "provider": "govinfo",
    "document_id": "<document_number FR, p. ej. 2024-08105>",
    "source_url": "https://www.govinfo.gov/content/pkg/FR-<fecha>/html/<doc>.htm",
    "sha256": "<sha256 del granulo o null>",
    "anchor": "<encabezado de identidad; null si no es extracted>",
    "detail": "<motivo o null>"
  },
  "p_sections": [
    {"position": 1, "letter": "a", "heading": "Effective Date",
     "kind": "effective_date", "body": "This airworthiness directive (AD) is effective May 22, 2024."}
  ]
}
```

- `p_sections` va vacío en todo intento que no sea `extracted` (la RPC lo
  rechazaría). `source_url` es la URL pública del granulo: la API key nunca
  viaja.
- El `ads.id` sale del mismo mapa que applicability (`_fetch_ad_ids`). Un AD
  sin entrada v2 en el cache no viaja: no hubo intento que registrar.
- **Cuándo.** Un intento de esta corrida se manda siempre. Uno que viene del
  cache de una corrida anterior solo si la base no lo tiene: el AD no tiene
  fila en `ad_text_documents`, o el intento es `extracted` y el AD no tiene
  texto conservado. Así la primera corrida con la RPC lleva a la base todo lo
  que el cache ya tenía, y las siguientes solo lo nuevo, sin reescribir las
  secciones de ~13.5k ADs cada noche ni falsear `last_attempt_at`. La lectura
  de `ad_text_documents` va en lotes de 300; si falla, se registra y solo
  viajan los intentos de hoy.
- **Límites** (`populate_db.TEXT_*`, los de la RPC): hasta 200 secciones,
  campos del intento y headings de hasta 2048 caracteres, letter de hasta 8,
  body de hasta 200000. Unas secciones que no caben viajan como intento
  `error` sin secciones, con el motivo en `detail`, y se registran como error
  de escritura.
- **Errores.** Cada llamada fallida es un error de escritura (la corrida queda
  en `error` y sale con código distinto de 0). Tras 20 fallos seguidos
  (`TEXT_MAX_CONSECUTIVE_FAILURES`: RPC ausente, base caída) se deja de llamar
  y el resto se registra como un solo error. El log resume los estados que
  devolvió la base (`ok`, `partial`, `unanchored`, `empty`, `error`) y los
  que no llegaron (`rpc_failed`, `not_sent`).

## Despliegue

1. La migración de `ad_text_documents`, `ad_text_sections`,
   `ad_text_is_complete` y `replace_ad_text_sections` (ad-scrapper) va antes
   que este backend: sin la RPC cada AD FAA es un error de escritura.
2. `python populate_db.py --source federal_register`: la primera corrida con
   la RPC lleva a la base las secciones que el cache ya tenía.
3. `scripts/backfill_effective_date.py` (más abajo) para las fechas ya
   guardadas.

## compliance_time y AMOC desde las secciones

`compliance_time` y `amoc_available` de un AD FAA salen de las secciones
clasificadas del texto anclado (`status=extracted`):

- **compliance_time** (`enrich.compliance_time_from_sections`): la primera
  frase de `_COMPLIANCE_PHRASES` ("before further flight", "within N flight
  hours/cycles/days/months ... [after the effective date of this AD]", "at the
  next ... inspection") en las secciones `compliance` o `required_actions`, en
  el orden del documento. Si ninguna la trae, no hay plazo.
- **amoc_available** (`enrich.amoc_from_sections`): hay una sección `amoc`, o
  una sección `required_actions` nombra "alternative method(s) of
  compliance".
- **Sin texto anclado** (`unanchored`, `empty`, `error` o entrada v1 sin
  secciones) el cuerpo no aporta nada y `normalize()` usa el abstract
  (`extract_compliance_time`, `detect_amoc`), como las demás fuentes.

Ambos se derivan al parsear, desde las secciones del cache: las entradas
guardadas antes de este cambio siguen la regla nueva sin bajar nada. Antes
salían del documento FR entero: el AMOC era cualquier mención de "alternative
method of compliance" (el preámbulo discute AMOCs de otros ADs y de los
comentarios del público) y el plazo, la primera frase de plazo tras "(x)
Compliance" o, si no había, **la primera oración de esa sección**, que en los
ADs modernos es la fórmula "Comply with this AD within the compliance times
specified, unless already done." Ninguno de los dos miraba si el texto era del
AD: un documento `unanchored` también aportaba valores.

### Medición sobre los fixtures (`tests/fixtures/fr_text/`)

Los doce granulos reales con AD esperado (diez anclados, dos `unanchored`; el
fixture compuesto de dos ADs no tiene uno solo esperado y queda fuera).
"Antes" es la regla anterior sobre el documento entero; "después", la de las
secciones. La tabla la fija `tests/test_fr_derived_fields.py`.

| Documento | Estado | compliance_time antes | compliance_time después | AMOC antes → después |
|---|---|---|---|---|
| 2024-08105 | extracted | "Comply with this AD within the compliance times specified, ..." | — | sí → sí |
| 2024-00993 | extracted | "Comply with this AD within ..." | — | sí → sí |
| 2015-23117 | extracted | "Comply with this AD within ..." | "Within 30 days after the effective date of this AD" | sí → sí |
| 2015-06235 | extracted | "Within 30 days" | igual | sí → sí |
| 05-17333 | extracted | "Within 14 days" | igual | sí → sí |
| 05-2578 | extracted | "within 1,800 flight cycles" | igual | sí → sí |
| 98-4716 | extracted | "Within 5 flight cycles" | igual | sí → sí |
| 01-917 | extracted | "Within 50 flight hours after the effective date of this AD" | igual | sí → sí |
| 2017-25253 | extracted | — | — | no → no |
| 2024-13013 | extracted | "Comply with this AD within ..." | "Within 48 months" | sí → sí |
| E8-2623 | unanchored | — | — | no → no |
| E7-1708 | unanchored | — | — | **sí → no** |

Lectura, sin suponer que menos valores es mejor:

- **compliance_time**: 9 → 7 valores, pero antes solo 5 eran un plazo; los
  otros 4 eran la fórmula de la sección Compliance. Después los 7 son plazos:
  los 5 anteriores sin cambio y 2 nuevos (2015-23117 y 2024-13013, que antes
  devolvían la fórmula). Se pierden 2 fórmulas que no eran plazos. Quedan sin
  plazo 2024-08105 (los plazos están en la tabla del boletín de servicio que el
  AD incorpora, no en su texto) y 2024-00993 ("further flight is prohibited
  until ...", que no es una frase de `_COMPLIANCE_PHRASES`): ahí `null` es
  correcto para el texto que tenemos, y `enrich_ai` puede completarlo.
- **AMOC**: 10 → 9. Los diez anclados no cambian (nueve con sección `amoc` y la
  rescisión 2017-25253 sin ella). El que pasa a `false` es E7-1708: su
  encabezado declara un amendment con errata ("39-149824" frente a 39-14924
  en `docket_ids`), así que no se ancla. Su texto probablemente sí es el del
  AD y el `true` era correcto, pero la regla no puede distinguirlo de un
  documento de otro AD, que es justo el caso en que el valor anterior mentía;
  es una pérdida aceptada a cambio de no heredar AMOCs ajenos.
- En el corpus completo cabe esperar el mismo patrón (los ADs modernos que
  guardaban la fórmula pasan a su primer plazo real o a `null`), pero es una
  expectativa, no una medición: conviene medirlo sobre el cache real y
  muestrear a mano ADs de cada época antes de dar la cifra por buena.

## effective_date desde el rule

`scrapers/federal_register.rule_effective_date`, en este orden:

1. La fecha de la sección `effective_date` del texto anclado
   (`enrich.effective_date_from_sections`): la fecha pegada a "effective"
   ("This AD is effective May 22, 2024.", "becomes effective on March 2, 1998,
   to all persons except ..."). En un AD de emergencia, "the emergency AD was
   effective for those owners and operators on the date it was provided" no
   trae fecha y no cuenta.
2. El `effective_on` de la FR API.
3. `NULL`.

Nunca la fecha de publicación: antes era el respaldo cuando faltaba
`effective_on`, y un AD entra en vigor semanas después de publicarse (o antes,
si fue de emergencia). `raw_data.Effective_On` guarda el valor de la FR API tal
cual. En los diez fixtures anclados la sección da fecha en los diez, y en nueve
es distinta de la de publicación (la excepción es 2024-02-51, de emergencia,
efectivo para quien no recibió el aviso el mismo día en que se publicó).

### Backfill de lo guardado

```bash
python scripts/backfill_effective_date.py --csv outputs/effective_date_plan.csv  # dry-run (por defecto)
python scripts/backfill_effective_date.py --apply
```

Lee `ads` (FAA) y el cache full-text local, sin bajar nada, y aplica la misma
regla. El CSV (`id, ad_number, fr_doc, effective_date_before,
effective_date_after, source`) lista cada AD que cambia y sirve para
restaurar. `source` dice de dónde sale el valor nuevo: `section`,
`effective_on`, `none`, o `publication_date` cuando lo guardado era el
respaldo de la fecha de publicación y pasa a NULL. Las filas escritas antes de
`Effective_On` solo tienen `raw_data.Effective_Date`: si difiere de la fecha
de publicación era `effective_on`; si es igual no se distingue del respaldo y
se trata como ausente. `--apply` escribe `ads.effective_date` con el cliente
de `populate_db`, en lotes por fecha nueva; un lote fallido deja la salida
distinta de 0. Pasar a NULL cambia el orden y las alertas por fecha (la
búsqueda ordena `nulls last`): revisar el CSV antes de aplicar.
