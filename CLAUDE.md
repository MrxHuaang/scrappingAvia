# Zephr — scrappingAvia (Python scraper backend)

> **Convención de commits (IMPORTANTE):** NO agregar trailer `Co-Authored-By:`
> ni ninguna atribución a Claude/IA en los mensajes de commit ni en los cuerpos
> de PR. Los commits van solo a nombre del autor humano configurado en git.

## Qué es este repo

Backend de scraping de Airworthiness Directives (ADs) para la plataforma Zephr.
Escrito en Python + Flask. Persiste en Supabase. Frontend en Next.js vive en repo separado (`ad-scrapper`).

## Rutas importantes

```
scrapers/           Scrapers por fuente (un archivo por autoridad)
populate_db.py      Orquestador: ejecuta scrapers y hace upsert en Supabase
scripts/            Utilidades de mantenimiento
  validate_ads.py   Reporte de calidad de datos (completos / parciales / críticos)
outputs/            CSVs generados (ads_to_rescrape.csv, etc.) — gitignored
app.py              Flask API (legacy)
utils.py            Helpers compartidos
.env                Credenciales locales (nunca commitear)
```

## Fuentes de ADs y sus source keys

| Source key (BD)       | Label en BD          | Estado scraper    |
|-----------------------|----------------------|-------------------|
| `federal_register`    | FAA                  | OK — FR API year-split, identidad via `docket_ids` (13.061 ADs) |
| `transport_canada`    | Transport Canada     | OK — 4735 ADs     |
| `anac_brazil`         | ANAC Brasil          | OK — 450 ADs      |
| `easa`                | EASA                 | activo            |
| `dgac_chile`          | DGAC Chile           | activo            |
| `anac_argentina`      | ANAC Argentina       | activo            |
| `aerocivil_colombia`  | AEROCIVIL Colombia   | **deshabilitado del cron** — WAF 403 (sitio se mudo a SharePoint) |
| `casa_australia`      | CASA Australia       | **deshabilitado del cron** — bloqueo de red (timeout, ni 403 explicito) |
| `gcaa_uae`            | GCAA UAE             | **deshabilitado del cron** — sitio OK (200), pero el listado esta detras de un postback ASP.NET AJAX que un GET liso no dispara |
| `uk_caa`              | UK CAA               | activo            |

**Fuentes muertas (2026-07-28).** Las tres de arriba llevaban >1 mes devolviendo
0 ADs cada corrida diaria, marcadas `completed` sin error — cobertura falsa.
`populate_db.DISABLED_SOURCES` las saca del run sin `--source` (el cron real);
siguen invocables a mano (`--source gcaa_uae`) para debug. Ninguna es un fix de
parsing: las tres necesitan Playwright (browser real). GCAA es la unica sin WAF
de por medio — la mas facil de las tres si alguien retoma esto.

## Esquema de la tabla `ads` en Supabase

```
source            TEXT   -- source key (ej: "federal_register")
source_label      TEXT   -- label legible (ej: "FAA")
ad_number         TEXT   -- PK natural junto con source
state_of_design   TEXT   -- pais emisor (derivado del source via enrich.STATE_OF_DESIGN)
subject           TEXT
make              TEXT   -- fabricante PRINCIPAL; los multiples van en ad_applicability
model             TEXT   -- todos los type designations del AD (lo que filtra search_ads)
product_type      TEXT   -- enum: Aircraft|Engine|Propeller|Appliance|Helicopter
published_date    DATE   -- fecha de publicacion (distinta de effective)
effective_date    DATE   -- YYYY-MM-DD
status            TEXT   -- enum CANONICO: New | Revision | Superseded | Cancelled
priority          TEXT   -- enum: Emergency | Normal
unsafe_condition  TEXT   -- best-effort desde el abstract (FAA)
corrective_action TEXT   -- best-effort desde el abstract (FAA)
compliance_time   TEXT   -- best-effort ("within N flight hours")
amoc_available    BOOL   -- true si el texto menciona AMOC
superseded_to_ad_id UUID -- FK a ads.id del AD que lo reemplaza (si status=Superseded)
docket            TEXT
pdf_link          TEXT   -- NULL en ~867 TC ADs superseded (esperado, no es bug)
raw_data          JSONB  -- dict crudo del scraper (preserva valores originales)
first_seen_at     TIMESTAMPTZ
last_updated_at   TIMESTAMPTZ
```

Constraint único: `(source, ad_number)` → upsert usa `on_conflict=source,ad_number`.

**Tablas relacionales pobladas en el post-pass (`populate_relations`):**
- `ad_applicability` — make/model afectados (1 AD -> N). Las tablas de usuario y
  Watchlist hacen match contra esta.
- `ad_service_bulletins` — SBs referenciados, extraidos de subject/abstract.

### Applicability multi-fabricante (`Applicability` en raw_data)

Un AD puede listar N approval holders, cada uno con SUS type designations (EASA
lo hace en 765 ADs). Un scraper que pueda verlo emite
`Applicability: [{make, models[]}]` en su record; `populate_db._applicability_pairs()`
escribe una fila de `ad_applicability` por par real. EASA y FAA lo emiten (FAA
via `scrapers/common/fr_applicability.py`, unica extraccion de titular + modelos
para Federal Register y GovInfo; ver docs/applicability-contract.md). Sin esa
clave se usa el camino escalar (`Make` + `Model`), correcto para TC/ANAC, que
nunca traen mas de un fabricante.

> **NUNCA** unir varios makes en un solo string (`", ".join`) ni volver a
> partirlos por coma. Los nombres de holder traen comas propias ("REVO, Inc.",
> "McDermott 214 Holdings, LLC"), asi que el join no es reversible. Ese fue el
> bug de 2026-08: `ads.make` guardaba strings de hasta 494 chars, la UI los
> ofrecia como opcion del filtro Make, y `ad_applicability` cruzaba cada
> fabricante con los modelos de los otros — falsos positivos en el matching de
> flota (un A320 matcheaba cualquier AD que mencionara Airbus entre diez
> holders).

### Gate de filtros de busqueda

`utils.validate_search_filters()` es la unica fuente de la regla y la aplican los
dos endpoints (`/api/search` y `/api/search/stream`, que antes validaban distinto):
**make + al menos otro filtro** (model, product_type, date_from, date_to o
keyword). Excepcion: si el keyword tiene forma de numero de AD o docket
(`utils.is_ad_identifier()`, reusa `AD_NUM_FAA/EASA/TC`) pasa solo — pegar
"2023-12-03" es el uso mas comun y nadie sabe de memoria el fabricante.
Barrer una autoridad entera sin filtros ya **no** esta permitido.

**Enriquecimiento (`enrich.py`):** todas las derivaciones (state_of_design, status
canonico, priority, product_type canonico, AMOC, unsafe/corrective/compliance, SBs)
viven en un solo modulo. `normalize()` en populate_db lo usa para TODAS las fuentes.
Valores explicitos del scraper (claves `Unsafe_Condition`, `Corrective_Action`,
`Compliance_Time`, `AMOC_Available`, `Publication_Date`, `Superseded_By`, `Abstract`)
tienen prioridad sobre la derivacion automatica.

### Cobertura de campos profundos por fuente

| Fuente | Fuente del texto | Campos profundos | Cache |
|--------|------------------|------------------|-------|
| `federal_register` | GovInfo full-text (`api.govinfo.gov/.../htm`) + abstract FR | texto del AD en secciones (`enrich.split_ad_sections`), modelos de la sección Applicability, unsafe/corrective (abstract), compliance_time + AMOC (cuerpo), SBs | `outputs/fr_fulltext_cache.json` (v2) |
| `easa` | pagina detalle `ad.easa.europa.eu/ad/{n}` + **PDF** (`pypdf`) | published_date/AMOC/SBs/supersesion (detalle) + unsafe/corrective/compliance (PDF: Reason / Required Action(s) and Compliance Time(s)) | `easa_detail_cache.json`, `easa_pdf_cache.json` |
| `transport_canada` | **PDF** `AD_dl.aspx?...ft=pdf` (`pypdf`) | unsafe/corrective/compliance/SBs. 3 layouts: propia CF (Background/Corrective/Compliance), EU-adoptada (formato EASA), US-adoptada (FAA, best-effort) | `tc_pdf_cache.json` |
| `anac_brazil` / `anac_argentina` | listado (+Action codes Brasil) | solo escalares: state_of_design, status canonico, priority, product_type, applicability. Prosa profunda **omitida** (PDFs PT/ES, volumen ~466, plataforma EN-only) | — |
| resto (dgac, gcaa, aerocivil, casa) | listado | solo escalares via enrich | — |

> federalregister.gov full_text bloquea bots -> FAA usa GovInfo (requiere
> `GOVINFO_API_KEY`). EASA/TC usan PDF (`pypdf`, agregado a requirements).
> Re-scrape completo: `python populate_db.py --source <x> --reset` (FAA ~13k,
> EASA ~18k, TC ~4.7k fetches de cuerpo/PDF en el primer run; cacheado despues).

## Calidad de datos actual (2026-06-12)

```
ANAC Argentina      0 completos  15 parciales   1 crítico
ANAC Brasil       450 completos   0 parciales   0 críticos
DGAC Chile          0 completos  42 parciales   2 críticos
EASA            18282 completos   0 parciales   2 críticos
FAA             13465 completos   0 parciales   0 críticos
Transport Canada 3865 completos   0 parciales 870 críticos (sin pdf_link — superseded, esperado)
TOTAL           36062 completos (97.5%)
```

Los ~870 TC críticos son ADs superseded: TC solo guarda link al AD original (FAA/EASA)
y al que los reemplaza, sin PDF propio. No es un bug del scraper.

**Identidad FAA (reparada 2026-07-28):** los ad_numbers de FAA venian del primer
"AD nnnn-nn-nn" del cuerpo del rule — que es el AD *superseded* — o directamente del
FR document_number. 6075 de las 10171 filas verificables llevaban el numero de otro
AD sobre el PDF correcto. Ahora la identidad sale de `docket_ids` (ver seccion FAA);
la base quedo en 13061 filas, todas con numero de AD bien formado y PDF, y
`scripts/verify_faa_pdfs.py` las contrasta contra el header del propio PDF.

## Cómo correr los scrapers

```bash
# Una sola fuente
python populate_db.py --source transport_canada
python populate_db.py --source federal_register
python populate_db.py --source anac_brazil

# Todas las fuentes
python populate_db.py

# Dry-run (scrape sin guardar)
python populate_db.py --source easa --dry-run

# Reporte de calidad
python scripts/validate_ads.py
```

## Cómo limpiar y re-scrapear una fuente

```python
from supabase import create_client
import os
from dotenv import load_dotenv
load_dotenv()

client = create_client(os.getenv('SUPABASE_URL'), os.getenv('SUPABASE_SERVICE_KEY'))
result = client.table('ads').delete().eq('source', 'transport_canada').execute()
print(f'Eliminados: {len(result.data)}')
```

Luego: `python populate_db.py --source transport_canada`

## Gotchas conocidos

### Encoding en Windows
- La consola Windows usa cp1252 — los scrapers NO deben usar `→`, `✅`, `❌` en prints
- Usar `->` y texto ASCII en todos los `print()` de scrapers
- `scripts/validate_ads.py` ya tiene `sys.stdout.reconfigure(encoding="utf-8")`
- `federal_register.py` línea 172 y 198 usan `->` (ya corregido)
- `anac_brazil.py` línea 480 usa `->` (ya corregido)

### Paginación Supabase
- PostgREST devuelve máximo 1000 filas por query
- Para queries completas usar `.range(page*1000, (page+1)*1000-1)` en loop
- Ver `fetch_all_ads()` en `scripts/validate_ads.py` como referencia

### Transport Canada — estructura HTML
- La tabla de datos se identifica por links `DA_DetailE.asp?Emd=` (ANAC) o equivalente TC
- TC tiene columnas: `new | model | ctry | adnumber | en | fr | adsubject | sbor reference | repeat inspection | issue date`
- Los ADs superseded solo tienen hrefs a `/AD_nd.aspx` y `/ad_r.aspx`, sin PDF propio

### ANAC Brasil — estructura HTML
- Columnas reales: `Directive No. | Effective Date | Subject / Reference | Action | Texts`
- La columna `Action` tiene códigos: F=Current, E=Emergency, C=Cancelled, R/S=Superseded
- `Docket` no está en el listado HTML — solo en página de detalle (`DA_DetailE.asp?Emd=`)
- Se guarda `Emd:{id}` como proxy de docket

### FAA Federal Register — API
- Usa la API REST de federalregister.gov (no scraping HTML)
- Trae hasta 5000 ADs (paginado de 100 en 100)
- `cfr_references[].part == "39"` → Airworthiness Directive
- `product_type` lo decide `enrich.classify_product_type` desde el título (Helicopter y
  Appliance incluidos; abstract solo como respaldo). Reglas en docs/applicability-contract.md

## Variables de entorno requeridas

```
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_SERVICE_KEY=eyJ...   # service role key (para writes)
SUPABASE_ANON_KEY=eyJ...      # anon key (solo lectura)
GOVINFO_API_KEY=...           # gratis en https://api.govinfo.gov/signup/
                              # opcional: si falta, fase GovInfo se skipea
```

## FAA: identidad desde `docket_ids` (`source='federal_register'`)

El numero AD de la FAA sale de **`docket_ids`** de la FR API: el header
regulatorio del documento, ya estructurado.

```json
["Docket No. FAA-2023-0927", "Project Identifier MCAI-2023-00013-T",
 "Amendment 39-22461", "AD 2023-12-03"]
```

`utils.ad_identity_from_docket_ids()` devuelve `{ad, docket, amendment, project}`
y cubre 13405 de los 13509 RULE (99%), incluidas las formas legadas de 1995-1999
(`"Amendment 39-9206, AD 95-09-02"`, `"AD 95-01- 07"`, `"AD 98-18-12-AD"`).

> **NUNCA** minar el numero AD del cuerpo del rule ni del MODS XML con
> "el primer `AD nnnn-nn-nn` del texto". El rule cita al AD que **reemplaza**
> antes de nombrarse, asi que ese match devuelve otro AD. Ese fue el bug de
> 2026-07-28: la ficha de AD 2023-12-03 mostraba el PDF correcto pero, en otras
> miles de filas, el numero pertenecia a un AD distinto (6075 de 10171 filas
> verificables estaban mal). Si `docket_ids` no trae el AD, el respaldo va
> **anclado al amendment** del header (`enrich.header_ad_for_amendment`), y si
> tampoco aparece, el documento se **descarta**: inventar un ad_number con el
> document_number es peor que no tener la fila.

**Un AD = una fila.** Un rule y sus "Final rule; correction" comparten numero
AD. El canonico es el rule (no la correccion) y, entre varios, el de publicacion
mas temprana: `_collapse_by_ad_number()`. Los demas documentos quedan en
`raw_data.Related_FR_Docs`. Sin eso el dedupe last-wins dejaba ganar a la
correccion, que llega despues en orden cronologico.

### Fases del runner

`run_federal_register()`:

1. **FR API** (`scrapers/federal_register.py`) — year-split 1994→presente (evita
   el cap de 10k por query). Aporta identidad Y contenido. El full text de
   GovInfo se sigue bajando para el texto del AD en secciones (ver abajo),
   `compliance_time`, AMOC y supersession, con **prefetch concurrente**
   (`FULLTEXT_WORKERS`, 8 hilos): en serie son ~13.5k GETs de ~1s (horas), con
   prefetch ~10 min. Cache en `outputs/fr_fulltext_cache.json`.
2. **GovInfo MODS** (`scrapers/faa_govinfo.py`) — **opcional**, `--with-govinfo`.
   Ya NO aporta identidad (su regex tiene el mismo defecto); solo completa
   `Docket`/`Amendment` faltantes. Sin el flag no se corre.

Dedupe por `(source, ad_number)` con normalizacion ASCII en
`normalize_ad_number()` (utils.py).

### Texto del AD en secciones (`enrich.split_ad_sections`)

El granulo de GovInfo trae el documento FR entero; el AD es solo el bloque de
la enmienda a 14 CFR 39.13, y solo vale si su **encabezado de identidad**
declara el AD esperado (el de `docket_ids`, o el respaldo anclado al amendment):

```
2024-06-03 The Boeing Company: Amendment 39-22708; Docket No. FAA-2023-1400; ...
98-02-51  Boeing: Amendment 39-10362. Docket 98-NM-04-AD.
```

- **Anclaje.** Si ningún encabezado declara el AD, o el que lo declara trae otro
  amendment que `docket_ids`, el intento es `unanchored` aunque el número
  aparezca citado ("This AD replaces AD ...", una corrección que reproduce el
  corchete o el encabezado entre elisiones `* * *`). Un encabezado sin número
  (1998) o con errata ("2005-NM-03-11") declara el AD que el corchete del
  preámbulo empareja con su amendment. En un documento con varios ADs solo se
  toma el bloque del esperado.
- **Bloque**: desde el encabezado hasta el siguiente encabezado o la firma
  ("Issued in/on").
- **Secciones** `{position, letter, heading, kind, body}`. Tres formatos: desde
  ~2007 "(a) Effective Date" ... en secuencia; 2000-2007 encabezados sin letra
  ("Unsafe Condition") que agrupan los párrafos (d), (e)...; legado
  "Applicability:", "Compliance:", "To prevent ..., accomplish the following:"
  y cada párrafo (a), (b)... El `kind` sale de `enrich.SECTION_KINDS` (una sola
  tabla, por encabezado y por contenido): effective_date, affected_ads,
  applicability, subject, unsafe_condition, amoc, compliance, material,
  related_information, required_actions; el resto `other`.
- **Estado** del intento: `extracted` (bloque anclado con ≥1 sección),
  `unanchored`, `empty` (respuesta vacía o "request access"), `error` (HTTP,
  red o bloque sin secciones). Python **no** decide si el texto está completo:
  lo decide la base con una sola regla sobre los `kind`.

**Cache v2** (`outputs/fr_fulltext_cache.json`, una entrada por
document_number): `{v: 2, status, anchor, sections[], sha, compliance_time,
amoc, docket, supersedes[], ad_header}`; `sha` es el sha256 del granulo.
`extracted` y `unanchored` son definitivos; `empty` y `error` se reintentan en la
corrida siguiente, una vez por corrida. Una entrada v1 (sin `v`) se sigue leyendo
y se re-pide una vez para obtener sus secciones; si ese intento falla conserva
sus campos. Sin `GOVINFO_API_KEY` no hay intento y el cache no se toca.
Tamaño esperado con el corpus completo: ~90 MB (unos 6.6 KB de secciones por
documento, ~13.5k documentos). Se reescribe entero al terminar cada año de la
corrida (`search_ads_by_year_range`), pero solo si ese año hubo algún intento
nuevo: una corrida con todo cacheado no lo escribe. Se carga entero en
memoria, también en `scripts/link_supersession.py`.

La sección Applicability anclada es además la fuente de los **modelos** de
`ad_applicability` (antes que el abstract; ver
docs/applicability-contract.md). Las tablas de texto en la base todavía no
existen: por ahora el texto solo vive en el cache.

### Verificar y reparar la identidad

```bash
python scripts/fr_identity.py                 # indice FR -> outputs/fr_identity.json
python scripts/fix_faa_identity.py --audit    # cuantas filas tienen el numero de otro AD
python populate_db.py --source federal_register
python scripts/prune_faa_stale.py             # plan; --execute para borrar las viejas
python scripts/verify_faa_pdfs.py --sample 40 # el PDF de cada fila declara SU AD
python scripts/test_faa_identity.py           # self-check sin red ni DB
```

### Run incremental

```bash
# Solo publicaciones desde una fecha (year-split desde ese anio)
python populate_db.py --source federal_register --since 2026-01-01
```

Los ADs pre-1994 no estan en la FR API ni en GovInfo. El scraper de DRS
(drs.faa.gov via Playwright) se elimino: Akamai lo bloqueaba y devolvia 0 en
cada corrida.

### Reset completo

```bash
python populate_db.py --source federal_register --reset   # borra y repobla
```
