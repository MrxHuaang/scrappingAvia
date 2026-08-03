# Contrato de extracción de applicability (INT-3)

Define qué evidencia autoriza a cada extractor a declarar el estado de un AD en el
payload de `replace_ad_applicability` (RPC en el proyecto Supabase, migración
`20260803020000`). **El estado lo declara el extractor; nunca se infiere de la forma
del payload** — un `make` vacío no es prueba de nada por sí solo.

## Los tres estados

| Estado | Semántica | Efecto en la BD |
|---|---|---|
| `success_with_rows` | El extractor obtuvo la lista de make/model del AD | Upsert de las filas + poda de las que ya no reporta (update solo si algo cambió) |
| `success_empty` | El extractor VIO la fuente y la fuente no lista applicability | Las filas existentes del AD se eliminan (0 filas es legítimo) |
| `failed` | La extracción no pudo completarse | No se toca nada de ese AD |

Atomicidad: **por lote** — cada llamada RPC es una transacción (≤500 ADs). Un proceso
muerto entre lotes deja los lotes anteriores completos y los posteriores intactos;
ningún AD queda a medias.

## Arquitectura actual del pipeline

En `populate_db.py`, la applicability se deriva de los campos `make`/`model` que cada
scraper puso en la fila normalizada. Eso fija el contrato así:

- **Un AD presente en la salida del scraper** fue scrapeado con éxito (los scrapers
  descartan o lanzan ante fetch/parse fallido; una fila emitida = listado leído).
  - `make` no vacío → `success_with_rows` (una fila por modelo; EASA empaqueta
    varios modelos separados por coma).
  - `make` vacío → `success_empty`: el listado de la fuente no trae
    fabricante/modelo para ese AD. Es el caso estructural de fuentes de solo
    listado (DGAC Chile, ANAC Argentina en filas antiguas) y de ADs de
    equipo/appliance sin make.
- **Un AD ausente de la salida del scraper** no aparece en el payload — equivale a
  `failed` implícito: sus filas almacenadas quedan intactas. Por esto la corrida
  nocturna nunca puede vaciar la applicability de un AD por un timeout o un cambio
  de HTML: ese AD simplemente no viene en la salida.
- El estado `failed` explícito existe en el contrato para extractores futuros que
  procesen por-AD y puedan fallar parcialmente (p. ej. un detalle EASA con PDF
  ilegible). Hoy `populate_db.py` no lo emite.

## Evidencia por fuente activa

| Fuente | Qué autoriza `success_empty` | Qué es `failed` (ausencia en la salida) |
|---|---|---|
| `federal_register` | La FR API devolvió el documento y ni el título ni los campos estructurados dan make (p. ej. ADs de appliances) | HTTP ≠ 200 de la API, JSON malformado, documento descartado por identidad (sin AD en `docket_ids` ni amendment) |
| `easa` | La fila del listado existe y su columna de tipo/fabricante está vacía | Timeout/HTTP ≠ 200 del listado o del detalle; parse error |
| `transport_canada` | Fila del listado sin columna `model` | Timeout/HTTP ≠ 200; layout de tabla no reconocido |
| `anac_brazil` | Fila del listado sin make/model | Ídem |
| `dgac_chile`, `anac_argentina`, `uk_caa` | Listado leído, sin campos de make | Ídem |

Reglas transversales:
- Ningún extractor emite filas con `make` vacío (la RPC lo rechaza con 22023).
- Duplicados de clave natural dentro de un AD se deduplican en Python **antes** de
  declarar (la RPC rechaza el payload completo si los encuentra).
- Todo fallo de la RPC incrementa `write_errors` → `status='error'` en `scrape_runs`
  y exit ≠ 0 del proceso (regla de la Fase 2a).
