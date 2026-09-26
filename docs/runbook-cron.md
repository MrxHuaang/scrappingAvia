# Runbook — qué hacer cuando el cron de scraping falla

El cron es `.github/workflows/populate_db.yml` (diario, 06:00 UTC, timeout
180 min). Desde la Fase 2a un run con escrituras fallidas **sale con exit ≠ 0**
y marca `scrape_runs.status='error'` — un job verde significa de verdad que
todo se escribió.

## 1. Diagnóstico rápido

1. **Actions → Populate DB — Daily Scrape → run fallido → logs.** Cada línea
   lleva timestamp y nivel (logger compartido). Buscar el último `[ERROR]` y
   el "Resumen final" (fuentes con error, escrituras fallidas).
2. **Tabla `scrape_runs`** (una fila por fuente y corrida): `status`,
   `error_msg`, `ads_found/ads_new/ads_updated`. Si el panel del frontend
   muestra una fuente en error, el `error_msg` de aquí es la causa.
3. Distinguir el tipo de fallo:
   - **Fuente que no responde / HTML cambiado** → error de UNA fuente; las
     demás siguen (el orquestador aísla por fuente). No es urgente: el catálogo
     es aditivo y el run siguiente recupera.
   - **Escrituras fallidas** (`write_errors > 0`) → problema de BD o de
     contrato de la RPC (`replace_ad_applicability` valida payloads: 22023 =
     payload inválido, revisar el extractor).
   - **Timeout del job** → primer run frío sin cache (ver §3) o una fuente
     colgada.

## 2. Fallos conocidos y su respuesta

| Síntoma | Causa probable | Acción |
|---|---|---|
| FAA sin `compliance_time`/AMOC/supersession | Falta `GOVINFO_API_KEY` en secrets, o el texto del AD no quedó anclado (`unanchored`) | Crear el secret (gratis: api.govinfo.gov/signup); un `unanchored` es definitivo y esos campos salen del abstract (docs/fr-text.md) |
| `text: RPC replace_ad_text_sections fallo` o `fallos seguidos de la RPC` | La migración de `ad_text_*` no está aplicada, o un payload fuera de contrato (22023) | Aplicar la migración en ad-scrapper; si es 22023, revisar el payload contra docs/fr-text.md. El texto ya guardado no se pierde: la RPC nunca lo borra con un intento fallido |
| `429` sostenido de una fuente | Rate limit del sitio | Nada — `get_with_backoff` reintenta con backoff; si persiste días, bajar frecuencia |
| Una fuente devuelve 0 ADs varios días | Cambio de HTML o WAF nuevo | Correr local `python populate_db.py --source <x> --dry-run` y comparar con el sitio |
| `22023` desde `replace_ad_applicability` | El extractor declara mal el tri-estado o duplica clave natural | Revisar contra `docs/applicability-contract.md` |
| `23514` con hint `plan_*` | Cuota/billing del workspace (no aplica al catálogo) | No es del scraper — revisar el trigger en el repo frontend |
| Job cancelado a los 180 min | Run frío re-descargando ~18k PDFs | Verificar que el paso "Restore scraper caches" encontró un cache previo |

## 3. Los caches de `outputs/`

Los caches (FR full-text, PDFs EASA/TC, MODS) viven en `outputs/` y se
restauran/salvan por run (`actions/cache`, clave `outputs-<run_id>`,
`restore-keys: outputs-`). El save corre `if: always()`: un run fallido
igual conserva lo descargado. Si los caches se pierden (expiran a los 7 días
sin uso), el siguiente run es frío y tarda mucho más — es lento, no un bug.
La escritura de cache es atómica (tmp + `os.replace`): un JSON truncado ya no
puede descartar el cache en silencio.

## 4. Re-ejecutar

- **Manual completo**: Actions → workflow_dispatch → source = `all`.
- **Una fuente**: workflow_dispatch → elegir la fuente en el desplegable.
- **Local**: `python populate_db.py --source <x>` (con `.env` completo).
- El upsert es idempotente (`on_conflict=source,ad_number`) y la política del
  catálogo es **aditiva**: re-correr nunca borra ADs existentes. `--reset` es
  la única operación destructiva y jamás va en el cron.

## 5. Escalamiento

Si dos runs consecutivos fallan con escrituras fallidas (no fuentes caídas),
revisar los advisors y logs de Supabase antes del tercer run — el catálogo
puede quedarse hasta 48h desactualizado sin impacto regulatorio (los ADs
siguen visibles; solo falta lo nuevo), pero más allá conviene avisar a los
clientes CAMO del retraso.
