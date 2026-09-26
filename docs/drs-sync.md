# Sincronización FAA DRS

La API data-pull de DRS (`drs.faa.gov`) es la única fuente que trae todos los
AD de la FAA, incluidos los anteriores a 1994 que no están en la FR API ni en
GovInfo, y el único enlace de DRS que funciona para cada documento. Esta
sincronización los guarda en `drs_documents` y enlaza cada AD FAA de `ads`
con su registro de DRS. **No es una fuente de `ads`**: no crea ni modifica
filas de `ads` (salvo `ads.drs_document_guid`) y no escribe `scrape_runs`.

| Pieza | Dónde |
|---|---|
| Cliente HTTP | `scrapers/faa_drs_api.py` |
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
   `drs_sync_state`, `ads.drs_document_guid` y las tres RPCs.
3. Comprobar que el secret `DRS_API_KEY` existe en Actions y que la key no
   está vencida.
4. En local, contra la base local: `DRS_API_ENABLED=1 python populate_db.py
   --drs --drs-doctype ADFREAD` (una página, unos segundos) y revisar
   `drs_sync_state` y unas filas de `drs_documents`.
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
| Días sin cambios | Normal si DRS no publicó; sospechoso si dura más de una semana | Comparar `summary.totalItems` de una página sin filtro con `count(*)` de `drs_documents` |

## Fuera de alcance por ahora

- Los AD anteriores a 1994 (unos 6.800 en DRS) no entran en `ads`
  (`federal_register`): solo quedan en `drs_documents`.
- El texto regulatorio de DRS no se parte en secciones cuando GovInfo no
  tiene el texto del AD.
- Las marcas de DRS no alimentan todavía candidatos de alias del catálogo
  (TCDS).
