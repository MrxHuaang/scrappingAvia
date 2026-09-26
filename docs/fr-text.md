# Texto de los ADs FAA y campos derivados

## compliance_time y AMOC desde las secciones

`compliance_time` y `amoc_available` de un AD FAA salen de las secciones
clasificadas del texto anclado a su encabezado de identidad
(`status=extracted`, ver "Texto del AD en secciones" en CLAUDE.md):

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

Antes ambos salían del documento FR entero: el AMOC era cualquier mención de
"alternative method of compliance" (el preámbulo discute AMOCs de otros ADs y
de los comentarios del público) y el plazo, la primera frase de plazo tras
"(x) Compliance" o, si no había, **la primera oración de esa sección**, que en
los ADs modernos es la fórmula "Comply with this AD within the compliance
times specified, unless already done." Ninguno de los dos miraba si el texto
era del AD: un documento `unanchored` también aportaba valores.

### Medición sobre los fixtures (`tests/fixtures/fr_text/`)

Los doce granulos reales con AD esperado (diez anclados, dos `unanchored`;
el fixture compuesto de dos ADs no tiene uno solo esperado y queda fuera).
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
  documento de otro AD, que es justo el caso en que el valor anterior
  mentía; es una pérdida aceptada a cambio de no heredar AMOCs ajenos.
- En el corpus completo el cambio grande es el mismo que aquí: los ADs
  modernos que antes guardaban la fórmula como `compliance_time` pasan a su
  primer plazo real o a `null`. Conviene muestrear a mano ADs de cada época
  antes de dar la cifra por buena.
