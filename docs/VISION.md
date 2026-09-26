# Zephr — Visión de producto y roadmap

> Última actualización: 2026-06-12

## El problema real

Un técnico de mantenimiento, un taller (MRO), un operador o un CAMO que gestiona
aeronavegabilidad hoy tiene que:

1. Revisar manualmente N sitios de autoridades (FAA, EASA, Transport Canada, ANAC…),
   cada uno con su buscador, su formato y su idioma.
2. Decidir qué ADs aplican a cada aeronave de su flota, sin tooling: PDFs, planillas
   Excel y conocimiento tribal.
3. Demostrar ante un auditor o autoridad que cada decisión (aplica / no aplica /
   cumplido / recurrente) se tomó, cuándo y por quién.

El costo de un AD omitido no es un bug: es una aeronave no aeronavegable, una
sanción regulatoria o un incidente. Y la trazabilidad de las decisiones es la
diferencia entre pasar una auditoría y perder un certificado.

## Para quién

| Segmento | Dolor principal | Disposición a pagar |
|---|---|---|
| Técnicos independientes | Buscar en N sitios, sin alertas | Baja (free tier / Pro individual) |
| Talleres / MROs | Aplicabilidad por aeronave de clientes, evidencia | Media-alta |
| Operadores con flota | Monitoreo continuo + cumplimiento recurrente | Alta |
| CAMO / compliance | Audit trail defendible, reportes para autoridad | Alta (Team/Enterprise) |

## Por qué Zephr y no buscar a mano

Zephr **no es un buscador de ADs**. Es un **sistema de registro de cumplimiento**
(system of record) sobre un índice multi-autoridad normalizado:

- **Indice unificado**: ~37.000 ADs de FAA, EASA, Transport Canada, ANAC Brasil y
  más, con esquema único (`source`, `ad_number`, make/model, status, fechas, PDF).
- **Aplicabilidad por aeronave/flota**: cada aeronave registrada matchea ADs por
  make/model (v1 ILIKE; v2 con extracción LLM de applicability desde el PDF).
- **Audit log inmutable**: cada cambio de estado de cumplimiento queda registrado
  con email + timestamp (`ad_compliance_log`, insert-only). Esto es defensa legal,
  y es el diferenciador difícil de copiar para un buscador.
- **Cumplimiento recurrente**: `next_due_at` + detección de vencidos.
- **Detección de superseded/cancelled**: el estado del AD viene de la fuente y se
  refresca con cada scrape.

El moat no es el scraping (replicable): es el **historial de decisiones de
cumplimiento del cliente** acumulado dentro de Zephr + la normalización
multi-autoridad. Cuanto más tiempo lo usa un operador, más caro le sale irse.

## Estado actual (2026-06-12)

### Datos

| Fuente | ADs en BD | Estado |
|---|---|---|
| EASA | 18.282 | OK — scrape completo 910 páginas |
| FAA (`federal_register`) | 13.465 | OK con defecto de identidad (ver abajo) |
| Transport Canada | 4.735 | OK — 867 superseded sin PDF propio (esperado) |
| ANAC Brasil | 450 | OK |
| DGAC Chile | 44 | Parciales (sitio pobre en metadata) |
| ANAC Argentina | 16 | Parciales |
| AEROCIVIL Colombia | 0 | Sitio inaccesible |
| CASA Australia | 0 | 403 Forbidden (bloqueo anti-bot; no se evade) |
| GCAA UAE | 0 | Bloqueado |
| **Total** | **~37.000** | 97,5% completos |

### Defecto FAA detectado y solución implementada

**Diagnóstico (2026-06-12):** ~100% de los `ad_number` de FAA en BD son FR
*document numbers* (ej. `2016-03689`, `E8-7662`), no números AD reales
(ej. `2016-14-07`). Causa: el título del Federal Register casi nunca incluye el
número AD, y el scraper caía al `document_number` como fallback.

**Solución (implementada):** Fase 3 GovInfo re-habilitada con extracción del
número AD real desde el MODS XML de cada granulo, merge campo a campo con FR API
por document number, y cache local para que los re-runs sean baratos. Ver
`CLAUDE.md` sección FAA. **Pendiente: correr el backfill completo** (~25 min):

```bash
python populate_db.py --source federal_register --reset
```

### Frontend (repo `ad-scrapper`)

Implementado: búsqueda multi-autoridad, fleet management, compliance tracking con
audit log, saved/notes/reviewed, watchlist (feed por flota), exports CSV, Stripe
(Free/Pro/Team), landing completa, admin de scrapers.

## Roadmap priorizado

### P0 — Cerrar el loop de valor (sin esto, Zephr es un buscador bonito)

1. **Backfill FAA con números AD reales** (comando arriba; backend listo).
2. **Alertas por aplicabilidad** — email digest (pg_cron + Resend): "3 ADs nuevos
   matchean tu flota esta semana". Es la feature que trae usuarios de vuelta.
   La función `findNewAdsForFleet(since)` ya existe en el frontend; falta el cron
   server-side y el template de email.
3. **Scrape recurrente programado** — hoy los scrapers corren a mano. Un cron
   (GitHub Actions / Supabase Edge / VPS) corriendo `populate_db.py --since` diario
   para FAA y semanal para el resto.

### P1 — Profundidad de compliance (lo que paga un CAMO)

4. **Applicability v2 con LLM** — extraer del PDF el applicability real
   (modelos, seriales, condiciones) a una tabla `ad_applicability` estructurada.
   Mata los falsos positivos del ILIKE y habilita matching por serial.
5. **Evidencia documental** — adjuntar archivos (work orders, logbook entries,
   8130s) a cada registro de cumplimiento. Storage en Supabase.
6. **Detección activa de supersedencia** — grafo `AD supersede AD` (TC y FAA lo
   publican); alertar cuando un AD cumplido es reemplazado.

### P2 — Equipo y escala

7. **Workspaces multi-usuario** — el modelo de datos ya lo soporta (workspace_id);
   falta invitaciones, roles (admin/tech/viewer) y RLS por workspace.
8. **Reportes de auditoría exportables** — PDF brandeado: estado de cumplimiento
   de una aeronave con historial firmado (del audit log).
9. **Scoring de impacto** — clasificar ADs por costo operativo estimado
   (inspección recurrente vs reemplazo de componente vs one-time check).

### P3 — Plataforma

10. **API pública v1 + webhooks** — para integrarse con CAMO software (AMOS, Traxxall).
11. **Más autoridades** — UK CAA, CASA (si publica vía API/bulk), JCAB, DGCA India.
    Diminishing returns mientras AEROCIVIL/CASA sigan bloqueados.
12. **i18n** — `next-intl` ya está instalado; ES/PT son mercados naturales
    (ANAC Brasil/Argentina, DGAC Chile).

## Limitaciones y riesgos conocidos

- **Pre-1994 FAA**: ni FR API ni GovInfo indexan ADs anteriores a 1994 con metadata
  de granulo. Única vía: FAA DRS (Akamai lo bloquea; el scraper Playwright que
  lo intentaba se eliminó porque siempre retornaba 0) o solicitar bulk export a FAA. **No se evaden
  protecciones anti-bot** — decisión de diseño.
- **CASA / GCAA / AEROCIVIL**: bloqueados o caídos. Documentado, no se insiste.
- **Applicability v1 es ILIKE**: genera falsos positivos/negativos. No venderla
  como matching certificado hasta v2.
- **Los scrapers HTML (TC, ANAC, EASA) son frágiles por naturaleza**: cualquier
  rediseño del sitio los rompe. `scrape_runs` en Supabase registra cada corrida;
  el admin panel del frontend los expone.
- **Carga manual**: hasta que exista el cron (P0.3), la frescura de datos depende
  de correr `populate_db.py` a mano.
