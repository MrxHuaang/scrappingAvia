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
| `federal_register`    | FAA                  | OK — consolida DRS + FR API year-split + GovInfo MODS (~13-14k ADs) |
| `transport_canada`    | Transport Canada     | OK — 4735 ADs     |
| `anac_brazil`         | ANAC Brasil          | OK — 450 ADs      |
| `easa`                | EASA                 | activo            |
| `dgac_chile`          | DGAC Chile           | activo            |
| `anac_argentina`      | ANAC Argentina       | activo            |
| `aerocivil_colombia`  | AEROCIVIL Colombia   | sitio inaccesible |
| `casa_australia`      | CASA Australia       | 403 Forbidden     |
| `gcaa_uae`            | GCAA UAE             | activo            |

## Esquema de la tabla `ads` en Supabase

```
source            TEXT   -- source key (ej: "federal_register")
source_label      TEXT   -- label legible (ej: "FAA")
ad_number         TEXT   -- PK natural junto con source
state_of_design   TEXT   -- pais emisor (derivado del source via enrich.STATE_OF_DESIGN)
subject           TEXT
make              TEXT
model             TEXT   -- modelo principal; los multiples van en ad_applicability
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
- `ad_applicability` — make/model afectados (1 AD -> N). Watchlist hace match contra esta.
- `ad_service_bulletins` — SBs referenciados, extraidos de subject/abstract.

**Enriquecimiento (`enrich.py`):** todas las derivaciones viven en un modulo; `normalize()`
lo usa para TODAS las fuentes. Claves explicitas del scraper (`Unsafe_Condition`,
`Corrective_Action`, `Compliance_Time`, `AMOC_Available`, `Publication_Date`,
`Superseded_By`, `Abstract`) tienen prioridad sobre la derivacion automatica.

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

**Defecto FAA conocido (pendiente de backfill, detectado 2026-06-12):** los 13,465
ad_numbers de FAA en BD son FR document_numbers (ej. "2016-03689", "E8-7662"),
NO números AD reales (ej. "2016-14-07"). El título de FR casi nunca trae el número
AD y el scraper caía a document_number como fallback. Fix implementado vía GovInfo
MODS (ver sección FAA); para reparar la BD correr:
`python populate_db.py --source federal_register --reset`

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
- `product_type` se infiere del título/abstract (keywords: engine, propeller, helicopter)

## Variables de entorno requeridas

```
SUPABASE_URL=https://xxx.supabase.co
SUPABASE_SERVICE_KEY=eyJ...   # service role key (para writes)
SUPABASE_ANON_KEY=eyJ...      # anon key (solo lectura)
GOVINFO_API_KEY=...           # gratis en https://api.govinfo.gov/signup/
                              # opcional: si falta, fase GovInfo se skipea
```

## FAA: 3 fuentes consolidadas bajo `source='federal_register'`

El runner `run_federal_register()` ejecuta en orden:

1. **DRS** (`scrapers/faa_drs.py`) — drs.faa.gov via Playwright. Pre-1995
   historicos. Best-effort: si Akamai bloquea o playwright no esta instalado,
   retorna `[]` sin fallar el pipeline. (En la practica casi siempre retorna 0.)
2. **FR API** (`scrapers/federal_register.py`) — federalregister.gov con
   year-split (cap 10k por query se evita iterando 1994→presente). Aporta el
   CONTENIDO: subject/make/model del abstract, effective_on, pdf publico.
   OJO: su ad_number es casi siempre el FR document_number (el titulo no trae
   el numero AD real) — lo corrige la fase 3.
3. **GovInfo MODS** (`scrapers/faa_govinfo.py`) — RE-HABILITADO (2026-06-12).
   Aporta la IDENTIDAD: por cada granulo RULE con "Airworthiness" en el titulo
   se fetchea el MODS XML (`/packages/{pkgId}/granules/{granId}/mods`) y se
   extrae via regex:
   - `AD 2024-01-02` → ad_number real (tambien formato viejo `AD 98-21-09`)
   - `Docket No. FAA-2024-0027` (o legacy `98-NM-123-AD`)
   - `Amendment 39-22653`

   Granulos sin numero AD en MODS ni titulo se DESCARTAN (nunca granuleId como
   ad_number — eso era el bug que obligo a deshabilitarlo).

   **Merge** (`_merge_faa_results()` en populate_db.py): join por FR document
   number (FR API lo guarda en `Docket`; GovInfo lo expone como `GranuleId`).
   En match, la fila FR es base y GovInfo sobreescribe AD_Number/Docket y agrega
   Amendment. No-matcheados pasan tal cual.

   **Cache MODS** (`outputs/govinfo_mods_cache.json`, gitignored): granuleId →
   {ad, docket, amend}, incluye negativos. Primer backfill ≈ 8k listings + 14k
   fetches MODS; a ~0.4s/request medidos (con Session reuse) son ~2-2.5 horas.
   Re-runs posteriores solo pagan los listings (~1 hora). Rate limit GovInfo:
   36,000 req/hr por key — no es el cuello de botella, la latencia HTTP sí.

Dedupe por `(source, ad_number)` con normalizacion ASCII en `normalize_ad_number()`
(utils.py).

### Run incremental

```bash
# Solo publicaciones desde una fecha (year-split desde ese anio + GovInfo MODS)
python populate_db.py --source federal_register --since 2026-01-01
```

### Setup adicional

```bash
pip install playwright
playwright install chromium    # ~150MB, solo Phase DRS
```

### Reset completo

```bash
python populate_db.py --source federal_register --reset   # borra y repobla
```
