# Zephr — scrappingAvia (Python scraper backend)

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
| `federal_register`    | FAA                  | OK — consolida DRS + FR API year-split (~13-14k ADs; GovInfo deshabilitado) |
| `transport_canada`    | Transport Canada     | OK — 4716 ADs     |
| `anac_brazil`         | ANAC Brasil          | OK — 179 ADs      |
| `easa`                | EASA                 | activo            |
| `dgac_chile`          | DGAC Chile           | activo            |
| `anac_argentina`      | ANAC Argentina       | activo            |
| `aerocivil_colombia`  | AEROCIVIL Colombia   | sitio inaccesible |
| `casa_australia`      | CASA Australia       | 403 Forbidden     |
| `gcaa_uae`            | GCAA UAE             | activo            |

## Esquema de la tabla `ads` en Supabase

```
source          TEXT   -- source key (ej: "federal_register")
source_label    TEXT   -- label legible (ej: "FAA")
ad_number       TEXT   -- PK junto con source
subject         TEXT
make            TEXT
model           TEXT
product_type    TEXT   -- Aircraft | Engine | Propeller | Helicopter | Appliance
effective_date  DATE   -- YYYY-MM-DD
status          TEXT   -- Final Rule | Current | Cancelled | Superseded | Emergency AD | NPRM
docket          TEXT
pdf_link        TEXT   -- NULL en ~867 TC ADs superseded (esperado, no es bug)
raw_data        JSONB
last_updated_at TIMESTAMPTZ
```

Constraint único: `(source, ad_number)` → upsert usa `on_conflict=source,ad_number`.

## Calidad de datos actual (2026-04-21)

```
ANAC Brasil       179 completos   0 parciales   0 críticos
FAA              5000 completos   0 parciales   0 críticos
Transport Canada 3849 completos   0 parciales 867 críticos (sin pdf_link — superseded, esperado)
TOTAL            9028 completos (91.2%)
```

Los 867 TC críticos son ADs superseded: TC solo guarda link al AD original (FAA/EASA)
y al que los reemplaza, sin PDF propio. No es un bug del scraper.

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
   retorna `[]` sin fallar el pipeline.
2. **FR API** (`scrapers/federal_register.py`) — federalregister.gov con
   year-split (cap 10k por query se evita iterando 1994→presente).
3. **GovInfo** (`scrapers/faa_govinfo.py`) — DESHABILITADO en populate_db.py.
   GovInfo usa FR granule IDs como ad_number (ej. "2023-28800") mientras FR API
   usa numeros FAA (ej. "2024-01-02"). Los formatos difieren → dedupe no funciona
   → duplicados. FR API year-split ya cubre 1994+ sin cap.

   **Para reactivar GovInfo correctamente** (verificado 2026-05-22): el MODS XML
   de cada granulo (`/packages/{pkgId}/granules/{granId}/mods`) contiene el numero
   AD real de FAA. Patrones encontrados via regex sobre el XML:
   - `AD 2024-01-02` (numero AD real)
   - `Docket No. FAA-2024-0027`
   - `Amendment 39-22653`
   - `Project Identifier AD-2023-01202-T`
   - `RIN 2120-AA64`
   - `89 FR 1030` (citation)

   Implementacion futura: tras listar granulos RULE con "Airworthiness" en titulo,
   fetch del MODS XML (1 extra HTTP call por AD), regex `AD\s*(\d{4}-\d{2}-\d{2,4})`
   para extraer numero AD real. Costo: ~14k calls extra a 40 req/s = ~6 min. Valor:
   matching limpio con FR API + metadata rica (docket, amendment, RIN).

   Roadmap dice "Mas sources mientras AEROCIVIL/CASA sigan bloqueados (diminishing
   returns)" — deferido hasta tener mas valor en features de compliance.

Dedupe por `(source, ad_number)` con normalizacion ASCII en `normalize_ad_number()`
(utils.py).

### Setup adicional

```bash
pip install playwright
playwright install chromium    # ~150MB, solo Phase DRS
```

### Reset completo

```bash
python populate_db.py --source federal_register --reset   # borra y repobla
```
