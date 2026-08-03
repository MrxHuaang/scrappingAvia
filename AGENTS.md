# Convención de commits (IMPORTANTE)

NO agregar trailer `Co-Authored-By:` ni ninguna atribución a Claude/IA en los
mensajes de commit ni en los cuerpos de PR. Los commits van solo a nombre del
autor humano configurado en git.

# Documentación del repo → CLAUDE.md

**La guía técnica vigente de este repo es [CLAUDE.md](CLAUDE.md).** Este
archivo dejó de duplicarla a propósito.

Las versiones anteriores de este documento (congeladas ~2026-06-12) describían
la identidad FAA **al revés**: presentaban el regex sobre GovInfo MODS y el
fallback a `document_number` como el fix — ese es exactamente el enfoque que
produjo 6.075 filas con el número de otro AD y que la reparación del
2026-07-28 eliminó. La identidad FAA vigente sale de `docket_ids` de la FR API
(`utils.ad_identity_from_docket_ids`); el contrato completo, con sus
prohibiciones, vive en la sección "FAA: identidad desde docket_ids" de
CLAUDE.md. Seguir la descripción vieja reintroduciría el defecto.
