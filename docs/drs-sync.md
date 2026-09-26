# Sincronización FAA DRS

La API data-pull de DRS (`drs.faa.gov`) es la única fuente que trae todos los
AD de la FAA, incluidos los anteriores a 1994 que no están en la FR API ni en
GovInfo, y el único enlace de DRS que funciona para cada documento. Esta
sincronización los guarda en `drs_documents` y enlaza cada AD FAA de `ads`
con su registro de DRS. Entre las dos cosas, crea en `ads` (source
`federal_register`) los AD anteriores a 1994 que solo están en DRS y envía el
texto regulatorio de DRS de los AD FAA que no tienen texto de GovInfo. No
escribe `scrape_runs`.

| Pieza | Dónde |
|---|---|
| Cliente HTTP | `scrapers/faa_drs_api.py` |
| AD anteriores a 1994 (conversión) | `scrapers/faa_drs_ads.py` |
| Filas de `ads` y texto | `populate_db.backfill_pre_federal_register`, `populate_db.populate_drs_text_sections` |
| Candidatos a alias (TCDS) | `scripts/tcds_alias_candidates.py` |
| Sincronización | `scrapers/faa_drs_sync.py` |
| CLI | `python populate_db.py --drs` |
| Job | `drs` en `.github/workflows/populate_db.yml` |
| Tablas y RPCs | repo ad-scrapper: `drs_documents`, `drs_sync_state`, `upsert_drs_documents`, `advance_drs_cursor`, `link_ads_to_drs` |

## Requisito previo: autorización de uso comercial

**Antes de encender la sincronización en producción, Hyvento tiene que
contactar a las autoridades del gobierno de EE. UU. (la FAA, equipo DRS) y
obtener por escrito la autorización para el uso comercial de la API de DRS.**

La API no publica términos de uso, cuotas ni límites de peticiones: no hay
nada en la documentación técnica, en la ayuda (`drs.faa.gov/help/helpdetails`,
"DRS API") ni en el formulario de la key (`drs.faa.gov/drs-api`). Lo único
aplicable es el aviso genérico de faa.gov
(`https://www.faa.gov/web_policies/disclaimer`): *"provided for U.S.
Government-authorized use only"*. Que los AD, como textos del Federal
Register, sean de dominio público no dice nada sobre el uso de la
plataforma. Mientras no exista esa autorización, `DRS_API_ENABLED` se queda
apagado.

El requisito cubre todo lo que sale de DRS: `drs_documents`, las filas de
`ads` de los AD anteriores a 1994, el texto con provider `drs` y el script de
candidatos TCDS. Todo depende del mismo flag, y con él apagado nada de eso
corre.

## Qué se guarda

Dos doctypes: **ADFRAWD** (AD finales, unos 20.150 documentos) y **ADFREAD**
(AD de emergencia, unos 290). Por documento, un elemento de
`upsert_drs_documents`:

| Campo | De dónde sale |
|---|---|
| `document_guid` | `documentGuid` (clave del upsert) |
| `document_number` | `drs:documentNumber` tal cual |
| `ad_number` | `drs:documentNumber` normalizado con `utils.normalize_ad_number` |
| `status` | `drs:status`: `Current`, `Historical` o `Pending` |
| `doc_last_modified` | `docLastModifiedDate` (ISO UTC con milisegundos) |
| `publication_date` | `drs:adfrawdCitationPublishDate` / `drs:adfreadCitationPublishDate` (null en los antiguos y en la mayoría de los EAD) |
| `effective_date` | `drs:effectiveDate` |
| `document_url` | `documentURL` (`…/browse/excelExternalWindow/{guid}.0001`) |
| `main_download_url` | `mainDocumentDownloadURL` |
| `makes`, `models` | `drs:…Make`, `drs:…Model`: arrays sin vacíos, sin el centinela `_EMPTY_` y sin repetidos. **No son paralelos**: no se emparejan marca y modelo |
| `regulatory_text` | `drs:adfrawdRegulatoryText` (texto plano; ADFREAD no tiene) |
| `payload` | el documento entero **sin** `SupplementaryInfo` y sin el texto regulatorio (que ya va en su columna) |

`SupplementaryInfo` (el preámbulo del Federal Register) es el 60 % del volumen
y nunca se envía. El enlace AD ↔ DRS se hace por número de AD, nunca por
número FR ni parseando el guid (sus formatos cambiaron en 2021 y tienen
variantes `R1-`, `C1-` y `DRSDOCID`).

`to_record` aplica las mismas reglas que `upsert_drs_documents` (fuente de
verdad: `supabase/migrations/20260927211000_drs_writers.sql` en ad-scrapper),
porque un solo documento inválido aborta la llamada entera (hasta 250) y deja
el cursor parado:

| Campo | Regla |
|---|---|
| `document_guid` | `^[A-Za-z0-9-]{1,64}$` |
| `document_number`, `ad_number` | no vacíos, 64 caracteres como mucho |
| `status` | `Current`, `Historical` o `Pending` |
| `doc_last_modified` | empieza por `YYYY-MM-DDTHH:MM` y es un timestamp válido |
| `publication_date`, `effective_date` | `YYYY-MM-DD` válida o null |
| `document_url`, `main_download_url` | empiezan por `https://drs.faa.gov/`, sin espacios, 2048 caracteres como mucho (la de descarga puede ser null) |
| `makes`, `models` | 2000 elementos como mucho, de 2048 caracteres como mucho |
| `regulatory_text` | 1.000.000 caracteres como mucho |
| `payload` | 256 kB como mucho |

Un documento que incumple alguna no viaja: se cuenta y la corrida sale en
error, pero no frena al resto y el cursor avanza. Una fecha de publicación o
efectiva con otro formato no descarta el documento: viaja como null y el
valor crudo queda en `payload`.

## Primera carga e incremental

**Por qué la primera carga no usa el filtro de fecha.** El filtro
`docLastModifiedDate` es "estrictamente después de" y compara a resolución de
segundo. 14.193 documentos de ADFRAWD (el 70 %) comparten el mismo
`docLastModifiedDate` de una carga masiva (`2024-05-30T19:48:53.441Z`), y en
19 de los 26 cortes de página la última fila y la primera de la siguiente
tienen la misma fecha. Un cursor por fecha no puede partir ese bloque: o lo
salta entero o lo repite entero. La única forma de atravesarlo es `offset`,
que es estable en orden ASC (probado: sin huecos ni duplicados en un barrido
completo).

1. **Primera carga** (sin fila en `drs_sync_state`, o con
   `initial_scan_done = false`): barrido por `offset` de 750 en orden ASC, sin
   filtro de fecha, hasta `hasMoreItems: false`. Al terminar,
   `advance_drs_cursor(máx. doc_last_modified, true)`.
2. **Incremental**: `docLastModifiedDate = cursor − 1 h`, ASC, paginando por
   `offset` hasta el final antes de avanzar. Aunque DRS reescriba miles de
   documentos en el mismo segundo (pasó en 2025-08, 2025-11 y 2026-01), se
   leen todos. El solapamiento de una hora vuelve a traer documentos ya
   guardados: el upsert es idempotente y la RPC no reescribe filas sin
   cambios.
3. **Enlace**: al final, `link_ads_to_drs` con los números de AD de las
   páginas subidas (en lotes de 1000). Si hay varios registros por AD, la RPC
   prefiere `Current`, luego el `doc_last_modified` más reciente y luego el
   guid.

Garantías:

- **Una página fallida detiene la corrida sin avanzar el cursor.** Lo ya
  subido queda (es idempotente) y se enlaza; la corrida siguiente empieza
  desde el mismo cursor (o repite la primera carga desde `offset 0`).
- El cursor avanza al máximo `doc_last_modified` visto, **acotado por la
  hora de inicio de la corrida**: si DRS modifica un documento mientras se
  pagina, el offset se corre y ese documento puede quedar sin ver, pero su
  fecha es posterior al inicio y la corrida siguiente lo trae.
- Dedupe por `documentGuid` dentro de la corrida.
- La fecha del cursor se valida en el cliente: la API devuelve 200 con
  `totalItems: 0` ante una fecha sola u otro formato, un fallo silencioso que
  dejaría la sincronización en "no hay cambios" para siempre. Solo se acepta
  `YYYY-MM-DDTHH:mm:ss.SSSZ`.
- Una key rechazada (403) corta todos los doctypes con un error que nombra
  `DRS_API_KEY`. 429, 5xx, timeouts y errores de red se reintentan con
  backoff (`scrapers.common.http.request_with_backoff`); entre peticiones hay
  al menos 2 s, porque la API no publica límites.
- El POST `filtered` admite como mucho 10 valores por filtro: con 11 responde
  400 con el mismo mensaje que una clave desconocida. Las búsquedas por
  número de AD van de 10 en 10.

## Llamadas a la base

```text
upsert_drs_documents({"p_doctype": "ADFRAWD", "p_documents": [ ≤ 250 elementos ]})
  -> {"inserted": n, "updated": n, "unchanged": n}
advance_drs_cursor({"p_doctype": "ADFRAWD", "p_last_modified": "2026-09-18T14:31:01.518Z",
                    "p_initial_scan_done": true})
link_ads_to_drs({"p_ad_numbers": ["2017-24-03", "2024-08-05", …]})   -> integer
```

El cursor se lee de la tabla `drs_sync_state` (`select last_modified,
initial_scan_done where doctype = …`) con la service key. Cada llamada a
`upsert_drs_documents` lleva como mucho 250 documentos (la RPC admite 750;
una página reciente pesa unos 8 MB ya sin `SupplementaryInfo`).

Entre las páginas y el enlace, con la misma service key:

```text
drs_documents  select <columnas de to_record> where doctype = 'ADFRAWD' and ad_number in (≤ 250)
ads            select ad_number, origin:raw_data->>Origin where source = 'federal_register' and ad_number in (…)
ads            upsert on_conflict=source,ad_number          (upsert_ads, como cualquier fila FAA)
replace_ad_applicability / ad_service_bulletins / supersesión   (populate_relations)
ad_text_documents select ad_id, text_sha256, text_provider where ad_id in (…)
replace_ad_text_sections({"p_ad_id", "p_attempt": {"provider": "drs", …}, "p_sections"})
```

## ADs anteriores a 1994 en `ads`

La FR API y GovInfo empiezan el 1994-01-03, así que ningún AD emitido antes
estaba en `ads`. DRS los tiene desde 41-47-01: **6.826 ADs** en el barrido
completo de ADFRAWD (un documento cada uno). Tras subir las páginas a
`drs_documents` y antes del enlace, la corrida los convierte en filas de
`ads` con source `federal_register`, por el mismo camino que cualquier fila
FAA: `normalize` → `upsert_ads` → `populate_relations`
(`populate_db.backfill_pre_federal_register`; la conversión está en
`scrapers/faa_drs_ads.py`).

**Qué AD entra.** Regla determinista, con los datos de DRS y sin consultar la
base:

1. el número declara un año anterior a 1994 (`YY-NN-NN` con YY entre 40 y 93,
   también `T93-…`, `… R1`, `… L`; los de cuatro dígitos son de 2001 en
   adelante), y
2. ninguno de sus documentos DRS tiene fecha de publicación desde el
   1994-01-01.

La segunda condición deja fuera las revisiones de ADs viejos publicadas
después con su propio número (`74-08-09 R3`, publicada en 2012: la FR API ya
la trae) y los números de 1993 publicados en enero de 1994. De los 6.844 ADs
con número anterior a 1994, 18 quedan fuera por eso. Además, **una fila
`federal_register` que no creó este camino nunca se reescribe**: antes de
escribir se lee `raw_data->>Origin` de las filas con esos números y se
descartan las que no dicen `drs` (si la lectura falla, no se escribe nada).
Si el scraper del Federal Register produce más tarde el mismo número, su
upsert pisa la fila de DRS y la corrida DRS deja de tocarla.

En un incremental se leen de `drs_documents` todos los documentos de cada AD
candidato, no solo los que llegaron en la corrida, y el documento que
representa al AD es el mismo que elige `link_ads_to_drs`: `Current`, luego
el `doc_last_modified` más reciente, luego el guid menor.

**Campos.**

| Campo de `ads` | De dónde sale |
|---|---|
| `subject` | `drs:title` sin el prefijo "Airworthiness Directives;" (si no hay, `drs:adfrawdSubject`) |
| `published_date`, `effective_date` | `publication_date` / `effective_date` del documento; null si DRS no las tiene (6.290 sin publicación, 1.241 sin efectiva) |
| `status` | `Current` → `New`; `Historical` con AD sucesor (`drs:adfrawdSupersededBy`) → `Superseded`; el resto (`Historical` sin sucesor, `Pending`) sin estado canónico |
| `pdf_link` | `documentURL` de DRS (la descarga necesita la key; queda en `raw_data.DRS_Download_URL`) |
| `docket` | `drs:adfrawdDocketNo`, salvo "Unknown" |
| `product_type` | `drs:adfrawdProductType` (`Helicopter` si el subtipo es `Rotorcraft`) |
| `make`, `model` | la primera marca y todos los modelos (el campo de búsqueda) |
| `raw_data` | el registro, con `Origin: "drs"`, `DRS_Document_Guid`, `DRS_Status` y `Superseded_By`/`Supersedes` |

**Applicability sin emparejar.** Make y Model de DRS no son paralelos, así
que nunca se adivina qué modelo es de qué marca:

| Marcas del AD | Filas de `ad_applicability` | ADs |
|---|---|---|
| una | una fila con esa marca y todos sus modelos (`listed`); si la lista pasa de los 2.000 caracteres del campo `model`, varias filas de esa misma marca | 6.426 |
| varias | una fila por marca, sin modelo (`unknown`) | 343 |
| ninguna | sin filas: el AD queda `failed` | 57 |

Antes se limpian las marcas: sin repetidos (hay ADs con la misma marca once
veces), sin "N/A" y con los sufijos que DRS separó por la coma ("Inc.",
"LLC", "a.s.") pegados otra vez a su marca.

## Texto regulatorio de DRS en secciones

Los ADs FAA sin texto de GovInfo conservado (los anteriores a 1994, que
GovInfo nunca tuvo, y los que GovInfo no pudo anclar) reciben sus secciones
del `regulatory_text` de DRS, con provider `drs`, por la misma RPC
`replace_ad_text_sections` (`populate_db.populate_drs_text_sections`). Corre
en la misma corrida, después de crear las filas anteriores a 1994 y antes
del enlace, para los ADs de las páginas subidas.

- **Mismo parser.** `enrich.split_ad_sections(texto, ad, plain=True)`: un
  modo de texto plano con la misma tabla `SECTION_KINDS` y los mismos
  estados de intento (`extracted`, `unanchored`, `empty`, `error`). Antes, el
  texto se lleva a la forma del gránulo de GovInfo
  (`enrich.plain_regulatory_text`): los `\n` y `\t` literales pasan a saltos
  reales, se quitan las marcas `((Page N))` y los espacios antes de cada
  salto, y el texto en una sola línea que DRS usa desde 2021 se parte antes
  del encabezado del AD y antes de cada párrafo `(a)`, `(b)`… en secuencia,
  con su encabezado en su propia línea.
- **Mismo anclaje.** El encabezado tiene que declarar el número del propio
  AD. En texto plano puede venir sin amendment ("41-47-01 DOUGLAS: …",
  "65-06-04 EUREKA WILLIAMS: Amdt. 39-44 …") y el único sufijo que se tolera
  es la `L` que DRS pone a los AD emitidos por carta. La aplicabilidad escrita
  dentro del párrafo del encabezado ("…: Amendment 39-4425. Applies to …") se
  separa de él, y las etiquetas en frase "Applies to" y "Compliance required"
  cuentan como secciones. El amendment de DRS no se exige: puede ser el de la
  última enmienda.
- **Cuándo se manda.** Si el AD no tiene fila en `ad_text_documents`, si
  tiene intentos pero ningún texto conservado, o si su texto conservado es
  de DRS y cambió (otro sha256). Un texto conservado de GovInfo nunca se
  toca. La base sigue decidiendo `ok` o `partial` con `ad_text_is_complete`.

| Campo del intento | Valor |
|---|---|
| `provider` | `drs` |
| `document_id` | `document_guid` |
| `source_url` | `document_url` |
| `sha256` | del `regulatory_text` tal cual |
| `anchor` | el encabezado anclado |
| `detail` | por qué no hay texto (`unanchored`, `empty`, `error`) |

**Medición** sobre 828 textos reales (79 de 1941 a 2019 y 749 de 2020 en
adelante): 814 `extracted`, 12 `unanchored`, 1 `empty` y 1 `error`; 787
completos (aplicabilidad, condición insegura y cumplimiento o acciones
requeridas).

| Década | Documentos | `extracted` | Completos |
|---|---|---|---|
| 1940 | 10 | 9 | 2 |
| 1950 | 8 | 8 | 3 |
| 1960 | 10 | 10 | 4 |
| 1970 | 8 | 8 | 7 |
| 1980 | 13 | 12 | 10 |
| 1990 | 11 | 11 | 11 |
| 2000 | 8 | 8 | 7 |
| 2010 | 11 | 11 | 9 |
| 2020 | 749 | 737 | 734 |

Casi todos los parciales antiguos no declaran una condición insegura: los AD
de 1941 a 1965 dicen qué hacer, no por qué. Los `unanchored` recientes no
traen su número de AD en el texto (el encabezado es "MD Helicopters, LLC:
Docket No. …") o traen solo la firma.

## Candidatos a alias desde TCDS

`scripts/tcds_alias_candidates.py` lee el doctype TCDSMODEL de DRS (las hojas
de datos de certificados de tipo: 8.095 documentos, 11 páginas) y escribe un
CSV con cada nombre que aparece como titular, nombre corto del titular o
titular anterior, con sus números de TCDS, sus modelos y, para un titular
anterior, quién tiene hoy esas hojas. Cada nombre se compara con
`docs/data/manufacturer-groups.csv` de ad-scrapper, **que solo se lee**:

| `status` | Significa |
|---|---|
| `known` | ya es un alias de un grupo (misma clave, sin acentos, puntuación ni sufijos societarios) |
| `new_spelling` | no es alias, pero su primera palabra distintiva es la marca de un grupo, o es titular anterior de hojas cuyo titular actual ya está en un grupo |
| `unknown` | nada del seed lo relaciona |

El `status` es una pista para quien revisa, no una decisión: un nombre
parecido no es evidencia de grupo (Continental Copters no es Continental), y
la columna `mentioned_in_notes` dice qué notas del seed nombran ese nombre,
que suele ser la razón del curador para dejarlo fuera. **El script no
modifica el seed**: una persona revisa el CSV y aprueba los alias que entran.
El reporte va donde diga `--out`, fuera del repo. Usa la misma key y el mismo
flag que la sincronización, y el mismo requisito de autorización.

```bash
DRS_API_ENABLED=1 python scripts/tcds_alias_candidates.py --dump tcds.json --out candidatos.csv
python scripts/tcds_alias_candidates.py --input tcds.json --out candidatos.csv   # sin red
```

Primera lectura (2026-09-26): 1.754 nombres, 441 `known`, 240
`new_spelling` y 1.073 `unknown`.


## Cómo encenderla

Variables:

| Variable | Dónde | Valor |
|---|---|---|
| `DRS_API_KEY` | `.env` en local; secret `DRS_API_KEY` en Actions | la key de `drs.faa.gov/drs-api` (vence a los 12 meses y se renueva ahí) |
| `DRS_API_ENABLED` | `.env` en local; **variable** de repositorio `DRS_API_ENABLED` en Actions | `true` (o `1`) enciende; vacío o cualquier otro valor apaga |

Con el flag apagado, `populate_db.py --drs` no hace nada y sale 0, y el job
`drs` del workflow ni se ejecuta. La key solo se lee del entorno, viaja solo
en la cabecera `x-api-key` y no aparece en URLs, logs ni errores.

```bash
python populate_db.py --drs                                  # los dos doctypes
python populate_db.py --drs --drs-doctype ADFREAD            # uno solo
python populate_db.py --drs --drs-doctype ADFREAD --dry-run  # sin base: barrido completo, no escribe
```

`--drs` no se combina con `--source`, `--reset`, `--since` ni
`--with-govinfo`.

### Procedimiento de la primera corrida

1. **Tener la autorización escrita de la FAA / equipo DRS para uso
   comercial** (ver arriba). Sin ella no se sigue.
2. Aplicar en la base las migraciones de ad-scrapper con `drs_documents`,
   `drs_sync_state`, `ads.drs_document_guid` y las tres RPCs, y resolver en
   ad-scrapper que las alertas de búsquedas guardadas no traten como nuevas
   las filas anteriores a 1994 (ver "Pendiente en la base").
3. Comprobar que el secret `DRS_API_KEY` existe en Actions y que la key no
   está vencida.
4. En local, contra la base local: `DRS_API_ENABLED=1 python populate_db.py
   --drs --drs-doctype ADFREAD` (una página, unos segundos) y revisar
   `drs_sync_state` y unas filas de `drs_documents`. Antes de ADFRAWD,
   `--drs --drs-doctype ADFRAWD --dry-run` cuenta sin base cuántas filas
   anteriores a 1994 y cuántos textos saldrían (el resumen trae "Pre-1994" y
   "Texto DRS").
5. Luego ADFRAWD. Volumen esperado de la primera carga: **unas 27 páginas de
   750 y unos 274 MB transferidos** (descomprimidos; cada página pesa entre
   2,5 y 20 MB y tarda de 1 a 5,5 s), unos 2 a 5 minutos con la pausa entre
   peticiones. Las corridas diarias siguientes suelen ser una sola página.
6. En producción, crear la variable de repositorio `DRS_API_ENABLED = true`
   (Settings → Secrets and variables → Actions → Variables). El job `drs`
   corre en el cron diario y en cada `workflow_dispatch`, con un solo job a la
   vez (`concurrency: drs-sync`).

## Fallos y qué hacer

| Síntoma | Causa probable | Acción |
|---|---|---|
| `DRS rejected the API key (HTTP 403)` | Key vencida, revocada o mal copiada | Renovar en `drs.faa.gov/drs-api` y actualizar el secret |
| `page N failed, cursor not advanced` | 429/5xx sostenido o timeout tras los reintentos | Nada: la corrida siguiente repite desde el mismo cursor. Si persiste, revisar si Akamai bloquea el runner |
| `could not read drs_sync_state` / fallo de `upsert_drs_documents` | Migraciones no aplicadas o payload fuera de contrato | Aplicar las migraciones; si es un payload, comparar con la tabla de campos de arriba |
| `skipped <guid>: …` | Un documento de DRS fuera de contrato | Mirar el documento en DRS; la corrida avanza igual |
| `[DRS] ADs anteriores a 1994: no se pudo leer ads` | La lectura de propiedad de las filas falló | Nada se escribió; la corrida siguiente lo reintenta con los AD que vuelvan a llegar |
| `after upsert: …` | Falló la escritura de las filas anteriores a 1994 o del texto | El enlace corrió igual; revisar el error (payload de `ads` o de `replace_ad_text_sections`) |
| Días sin cambios | Normal si DRS no publicó; sospechoso si dura más de una semana | Comparar `summary.totalItems` de una página sin filtro con `count(*)` de `drs_documents` |

## Fuera de alcance por ahora

- ADFREAD (AD de emergencia) no trae texto regulatorio: su texto solo está en
  el fichero y no se descarga.
- Los alias de TCDS no entran solos al seed de grupos: salen en un reporte
  que una persona revisa.
- Las alertas de búsquedas guardadas usan `first_seen_at`: la primera carga
  de los AD anteriores a 1994 los haría aparecer como nuevos (ver
  "Pendiente en la base").

## Pendiente en la base (ad-scrapper)

- **Alertas.** Las 6.826 filas anteriores a 1994 entran con `first_seen_at =
  now()` en la primera carga. Antes de encender el flag, las alertas de
  búsquedas guardadas tienen que excluir esas filas (por ejemplo
  `raw_data->>'Origin' = 'drs'`, o un `published_date`/`effective_date`
  anterior a 1994), o cada usuario con una búsqueda de un fabricante antiguo
  recibiría cientos de AD "nuevos".
- Las RPCs no necesitan campos nuevos: las filas usan las columnas de siempre
  de `ads`, `replace_ad_applicability` y `replace_ad_text_sections` ya admite
  el provider `drs`.
