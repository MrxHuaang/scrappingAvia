"""
Una fila de la BD tiene UNA forma de resultado (webapp.search.row_to_result),
venga del contrato de busqueda viejo o del nuevo: la UI, la exportacion y el
filtro de plan del proxy leen las mismas claves.
"""

from webapp.search_rows import row_to_result

_V1_KEYS = {
    "Source", "AD_Number", "Subject", "Make", "Model", "Effective_Date",
    "Status", "Product_Type", "Docket", "PDF_Link", "Superseded_By",
    "Issuing_Authority",
}

# Fila tal como la devuelve search_ads (sin los campos del contrato nuevo).
_LEGACY_ROW = {
    "source": "easa", "source_label": "EASA", "ad_number": "2024-0001",
    "subject": "Fuel pump", "make": "AIRBUS S.A.S.", "model": "A320-214",
    "product_type": "Aircraft", "effective_date": "2024-01-15",
    "status": "New", "docket": None, "pdf_link": "https://example.test/a.pdf",
    "superseded_by": None, "total_count": 1,
}


def test_legacy_row_keeps_its_shape_and_values():
    out = row_to_result(_LEGACY_ROW)
    assert _V1_KEYS <= set(out)
    assert out["Source"] == "EASA"
    assert out["AD_Number"] == "2024-0001"
    assert out["Docket"] == ""            # null -> "" como siempre
    assert out["Superseded_By"] == ""
    # Sin issuing_authority la emisora es la propia fuente.
    assert out["Issuing_Authority"] == "easa"


def test_fields_absent_from_a_legacy_row_are_null_or_false():
    out = row_to_result(_LEGACY_ROW)
    assert out["Match_Confidence"] is None
    assert out["Applicability_Unknown"] is False
    assert out["Has_Text"] is False
    assert out["Document_Class"] is None


def test_v2_row_carries_the_new_fields():
    out = row_to_result({
        **_LEGACY_ROW,
        "source": "easa", "issuing_authority": "federal_register",
        "superseded_by": "2025-0100", "match_confidence": "high",
        "applicability_unknown": True, "has_text": True, "document_class": "SIB",
    })
    assert out["Issuing_Authority"] == "federal_register"
    assert out["Superseded_By"] == "2025-0100"
    assert out["Match_Confidence"] == "high"
    assert out["Applicability_Unknown"] is True
    assert out["Has_Text"] is True
    assert out["Document_Class"] == "SIB"


def test_a_null_document_class_is_null_not_empty():
    assert row_to_result({**_LEGACY_ROW, "document_class": None})["Document_Class"] is None
    assert row_to_result({**_LEGACY_ROW, "document_class": ""})["Document_Class"] is None


def test_source_label_is_the_human_one_the_proxy_filters_on():
    assert row_to_result({"source": "federal_register"})["Source"] == "FAA (Federal Register)"
    # Fuente desconocida: se cae al label que traiga la fila.
    assert row_to_result({"source": "x", "source_label": "X CAA"})["Source"] == "X CAA"
