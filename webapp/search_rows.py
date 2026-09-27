"""
Adaptador unico de filas de busqueda: fila de la BD (snake_case) -> resultado
con las claves PascalCase que consume la UI. Lo usan los dos contratos de
/api/search y /api/search/stream.
"""

#: Etiqueta humana por source key. Es lo que la UI muestra y lo que el filtro
#: de plan del proxy lee en `results[].Source`, asi que no puede cambiar.
_SOURCE_LABELS = {
    "easa": "EASA",
    "federal_register": "FAA (Federal Register)",
    "transport_canada": "Transport Canada",
    "anac_brazil": "ANAC Brasil",
    "dgac_chile": "DGAC Chile",
    "anac_argentina": "ANAC Argentina",
    "aerocivil_colombia": "AEROCIVIL Colombia",
    "casa_australia": "CASA Australia",
    "gcaa_uae": "GCAA UAE",
    "uk_caa": "UK CAA",
}


def row_to_result(row: dict) -> dict:
    """
    Fila de la BD (snake_case, la que devuelven `search_ads` y `search_ads_v2`)
    -> resultado de busqueda con las claves PascalCase que consume la UI
    (`ADResult`), la exportacion y el filtro de plan del proxy.

    Es el unico adaptador: los dos contratos de busqueda pasan por aqui para
    que una fila tenga la misma forma venga por donde venga. Los campos que
    solo trae el contrato nuevo (`match_confidence`, `applicability_unknown`,
    `has_text`, `document_class`) salen en null/false cuando la fila no los
    tiene, que es lo que significan: sin confianza calculada, applicability
    conocida, sin texto, clase de documento desconocida.
    """
    source = row.get("source", "") or ""
    return {
        "Source": _SOURCE_LABELS.get(source, row.get("source_label", "")),
        "AD_Number": row.get("ad_number", ""),
        "Subject": row.get("subject", "") or "",
        "Make": row.get("make", "") or "",
        "Model": row.get("model", "") or "",
        "Effective_Date": row.get("effective_date", "") or "",
        "Status": row.get("status", "") or "",
        "Product_Type": row.get("product_type", "") or "",
        "Docket": row.get("docket", "") or "",
        "PDF_Link": row.get("pdf_link", "") or "",
        "Superseded_By": row.get("superseded_by", "") or "",
        # Quien EMITIO el AD, que no siempre es la base donde lo leimos:
        # EASA lista ADs de la FAA y de Canada, y Canada lista los de la
        # FAA y EASA. El cliente pinta esta autoridad, no `Source`.
        "Issuing_Authority": row.get("issuing_authority", "") or source,
        "Match_Confidence": row.get("match_confidence") or None,
        "Applicability_Unknown": bool(row.get("applicability_unknown")),
        "Has_Text": bool(row.get("has_text")),
        # Clase del documento EASA (AD, EAD, PAD, SIB, SD, ADR): la UI marca
        # un SIB para que no se lea como AD. Null fuera de EASA o si no se sabe.
        "Document_Class": row.get("document_class") or None,
    }
