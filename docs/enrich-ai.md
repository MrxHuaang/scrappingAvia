# enrich_ai.py y dashboard_server.py — enriquecimiento por LLM

## Qué hace `enrich_ai.py`

Rellena los campos semánticos de `ads` (`unsafe_condition`,
`corrective_action`, `compliance_time`, `amoc_available`, `published_date`,
`make`, `model`, `product_type`) leyendo el PDF completo de cada AD con un LLM
vía endpoint OpenAI-compatible (Ollama local por defecto; Gemini/Groq/OpenAI
cambiando `--endpoint`/`--model`).

Principios de diseño (ver el docstring del script para el detalle):

- **Resumible y barato**: solo procesa filas con algún campo objetivo en NULL
  y con `pdf_link`; el PDF se cachea en disco y no se re-baja.
- **Paginación por keyset** (`.order("id")` + cursor): procesa el 100% del set
  aunque el propio script vaya escribiendo los campos por los que filtra
  (la paginación por offset saltaba filas garantizadamente — corregido en la
  Fase 2a del plan de endurecimiento).
- **Nunca inventa**: la regla del prompt es dato ausente → `null`, y
  `build_update` solo escribe campos que estaban en NULL (nunca pisa un valor
  bueno).
- **Escrituras por fila a propósito**: cada UPDATE va pegado a una respuesta
  del LLM que tarda segundos — el cuello es el LLM, no la BD, y acumular lotes
  solo arriesgaría perder trabajo (pagado) en un crash. Decisión registrada en
  `docs/quality/traceability.md` del repo frontend.

Modos adicionales: `run_retranslate` (traduce campos que quedaron en PT/ES —
solo guarda si la traducción mejora) y `run_fix_truncated` (repara valores
cortados a mitad de oración por el cap viejo de ~500 chars).

```bash
python enrich_ai.py --limit 50        # piloto
python enrich_ai.py                   # toda la base
python enrich_ai.py --dry-run         # sin escrituras
python enrich_ai.py --source "EASA"
```

Variables: `LLM_ENDPOINT`, `LLM_MODEL`, `LLM_API_KEY` (ver `.env.example`),
más `SUPABASE_URL`/`SUPABASE_SERVICE_KEY` para las escrituras.

## Qué hace `dashboard_server.py`

Panel de control **local** para el enriquecimiento: enciende/apaga el enrich
de una autoridad con un click, muestra estado en vivo (polling), historial de
corridas y las últimas líneas del log. Ejecuta `enrich_ai.py` como subproceso
— **hereda la service key**, así que está protegido:

- **Solo loopback**: cualquier request que no venga de `127.0.0.1`/`::1`
  recibe 403, aunque alguien cambie el `host` de `app.run`.
- **Token opcional**: si `DASHBOARD_TOKEN` está definido en el entorno, toda
  request debe traerlo. El primer `GET /?token=<valor>` lo fija en una cookie
  HttpOnly y los `fetch()` del UI lo arrastran solos.
- **Un solo trabajo a la vez** (la GPU/Ollama es un recurso compartido).

```bash
DASHBOARD_TOKEN=$(python -c "import secrets; print(secrets.token_hex(16))")
python dashboard_server.py
# -> http://127.0.0.1:8787/?token=<el token>
```

## Relación con el cron

Ninguna: el enriquecimiento es un proceso manual/supervisado (requiere un LLM
disponible y revisión del operador), separado del scrape diario. El monitor de
consola alternativo es `watch_progress.py` (conteos por estimación del
planner, intervalo por defecto 60s).
