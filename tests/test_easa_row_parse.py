"""
Parseo de la columna "Approval Holder / Type Designation" de EASA.

El HTML real anida los type designations DENTRO de cada approval holder
(`li.tc_holder > ul > li.type`). El parser aplanaba las dos listas por separado y
las unia con ", ", perdiendo que modelo pertenece a que fabricante y produciendo
un `Make` de hasta 494 chars que aparecia como opcion del filtro Make en la UI.
"""

from bs4 import BeautifulSoup

import populate_db
import scrapers.easa as easa
from scrapers.easa import _parse_results_table

_MULTI_HOLDER_ROW = """
<table><tr>
  <td><a class="easa_cd">2024-0195</a></td>
  <td></td>
  <td>01/02/2024</td>
  <td>Main Rotor Blade - Inspection</td>
  <td>
    <ul>
      <li class="tc_holder">AIRBUS HELICOPTERS
        <ul><li class="type">AS 350 B</li><li class="type">AS 350 BA</li></ul>
      </li>
      <li class="tc_holder">SIKORSKY
        <ul><li class="type">S-76</li></ul>
      </li>
      <li class="tc_holder">KAMOV</li>
    </ul>
  </td>
  <td>15/02/2024</td>
  <td><a href="/ad/2024-0195.pdf">PDF</a></td>
</tr></table>
"""


def _parse(html):
    return _parse_results_table(BeautifulSoup(html, "html.parser"))


def test_make_is_the_primary_holder_not_the_joined_list():
    [ad] = _parse(_MULTI_HOLDER_ROW)
    assert ad["Make"] == "AIRBUS HELICOPTERS"
    assert ", " not in ad["Make"]


def test_applicability_keeps_holder_to_models_structure():
    [ad] = _parse(_MULTI_HOLDER_ROW)
    assert ad["Applicability"] == [
        {"make": "AIRBUS HELICOPTERS", "models": ["AS 350 B", "AS 350 BA"]},
        {"make": "SIKORSKY", "models": ["S-76"]},
        {"make": "KAMOV", "models": []},
    ]


def test_model_still_carries_every_type_designation():
    """`ads.model` es lo que filtra search_ads; recortarlo perderia recall."""
    [ad] = _parse(_MULTI_HOLDER_ROW)
    assert ad["Model"] == "AS 350 B, AS 350 BA, S-76"


def test_row_without_applicability_cell_does_not_break():
    html = """
    <table><tr>
      <td><a class="easa_cd">2024-0001</a></td><td></td><td>01/01/2024</td>
      <td>Engine fuel pump</td><td></td>
    </tr></table>
    """
    [ad] = _parse(html)
    assert ad["Make"] == ""
    assert ad["Applicability"] == []
    assert ad["Product_Type"] == "Engine"


# ------------------------------------------- markup que el parser no reconoce
#
# Un cambio de markup no puede parecerse a "el AD no aplica a nada". Estas
# variantes del mismo row conservan los holders en el HTML; el parser no los
# ve (o no ve su nombre), y lo que emite tiene que llevar el AD a `failed`
# (tests/test_relations_payloads.py lo prueba hasta el payload de la RPC).

HOLDER_CLASS_RENAMED = _MULTI_HOLDER_ROW.replace('class="tc_holder"', 'class="holder"')
HOLDER_NAME_WRAPPED = _MULTI_HOLDER_ROW.replace(
    "AIRBUS HELICOPTERS\n", "<span>AIRBUS HELICOPTERS</span>\n")
HOLDER_SECTION_TRUNCATED = _MULTI_HOLDER_ROW[:_MULTI_HOLDER_ROW.index("SIKORSKY")]


def test_a_renamed_holder_class_yields_no_holders():
    [ad] = _parse(HOLDER_CLASS_RENAMED)
    assert ad["Applicability"] == []


def test_a_holder_whose_name_cannot_be_read_is_kept_without_make():
    """Saltarlo dejaria a los otros holders como la applicability completa y
    la RPC podaria las filas del que falta; con `make` vacio el AD falla."""
    [ad] = _parse(HOLDER_NAME_WRAPPED)
    assert ad["Applicability"][0] == {"make": "", "models": ["AS 350 B", "AS 350 BA"]}
    assert [h["make"] for h in ad["Applicability"][1:]] == ["SIKORSKY", "KAMOV"]
    assert ad["Make"] == "SIKORSKY"


def test_a_truncated_holder_section_keeps_the_nameless_holder():
    [ad] = _parse(HOLDER_SECTION_TRUNCATED)
    assert [h["make"] for h in ad["Applicability"]] == ["AIRBUS HELICOPTERS", ""]


# ------------------------------------------------------ filas reales del listado
#
# Formas tomadas del listado real de ad.easa.europa.eu (el HTML completo trae
# ademas bandera, fecha efectiva y peso del adjunto). `_row` arma una fila con
# el mismo markup: icono de clase en el Subject, `li.tc_holder > ul > li.type`
# en el arbol y los links `blob` en los adjuntos.

_BLOB = "https://ad.easa.europa.eu/blob"
_SEND_COMMENT = (
    '<br/><a href="#" onclick="return false;"><img alt="send comment" '
    'src="https://ad.easa.europa.eu/static/img/icon/email-orange.gif"/> send comment</a>'
)


def _icon(cls, title):
    return (f'<img alt="{cls}" src="https://ad.easa.europa.eu/static/img/icon/'
            f'adclass_{cls.lower()}.gif" title="{title}"/>')


def _row(number, subject, holders, links=(), icon=""):
    tree = "".join(
        f'<li class="tc_holder" title="TC Holder">{name}<ul>'
        + "".join(f'<li class="type" title="Type">{t}</li>' for t in types)
        + "</ul></li>"
        for name, types in holders
    )
    attachments = "".join(f'<li class="file_pdf"><a href="{h}">297 kb</a></li>' for h in links)
    return f"""
    <table class="ad-list"><tr>
      <td class="cell-date"><a class="easa_cd" href="https://ad.easa.europa.eu/ad/{number}">{number}</a></td>
      <td class="cell-default"></td>
      <td class="cell-date">2026-09-25</td>
      <td class="cell-default">{icon}{subject}</td>
      <td class="cell-tree"><ul class="aircraft-tree">{tree}</ul></td>
      <td class="cell-date"></td>
      <td class="cell-default"><ul class="attachments-small">{attachments}</ul></td>
    </tr></table>
    """


# 2026-0052R2: AD final sin icono; su segundo adjunto es el ZIP del PAD.
AD_ROW = _row(
    "2026-0052R2", "Instruments – Fuse Cover Attachment – Modification",
    [("AIRBUS HELICOPTERS", ["AS 350 / EC 130"])],
    links=[f"{_BLOB}/EASA_AD_2026_0052_R2.pdf/AD_2026-0052R2_1",
           f"{_BLOB}/EASA_PAD_26_031.zip/AD_2026-0052R2_2"],
)
# 26-116: PAD vigente, con icono PAD y el link "send comment" en el Subject.
PAD_ROW = _row(
    "26-116", "Equipment / Furnishings – System Interface Unit – Replacement" + _SEND_COMMENT,
    [("AIRBUS HELICOPTERS", ["H160"])],
    links=[f"{_BLOB}/EASA_PAD_26_116.pdf/PAD_26-116_1"],
    icon=_icon("PAD", "Proposed AD"),
)
# 2026-07R2: SIB cuyo unico "holder" es la etiqueta de su clase.
SOLO_SIB_ROW = _row(
    "2026-07R2", "EASA Safety Publication Tool – Log of Revisions",
    [("Safety Information Bulletin", ["Airworthiness"])],
    links=[f"{_BLOB}/EASA_SIB_2026_07R2.pdf/SIB_2026-07R2_1"],
    icon=_icon("SIB", "Safety Information Bulletin"),
)


def test_document_class_comes_from_the_attachment_link():
    [ad] = _parse(AD_ROW)
    assert ad["Document_Class"] == "AD"
    [pad] = _parse(PAD_ROW)
    assert pad["Document_Class"] == "PAD"
    [sib] = _parse(SOLO_SIB_ROW)
    assert sib["Document_Class"] == "SIB"


def test_document_class_falls_back_to_the_class_icon():
    html = _row("2026-10", "SZD-50-3 - Notification of uncertified parts installed",
                [("ALLSTAR PZL GLIDER", ["SZD-50"])],
                icon=_icon("SIB", "Safety Information Bulletin"))
    [ad] = _parse(html)
    assert ad["Document_Class"] == "SIB"


def test_document_class_is_unknown_without_link_or_icon():
    [ad] = _parse(_MULTI_HOLDER_ROW)   # adjunto sin cola de clase, sin icono
    assert ad["Document_Class"] is None


# ------------------------------------------- placeholders de la taxonomia EASA
#
# "Safety Information Bulletin", "APPLIANCES", "ORPHANED (No TC Holder)" y
# "Safety Directives" son nodos del arbol de holders de EASA que declaran que
# en ese lugar no hay empresa. No son un holder ilegible: salen de la lista de
# holders (ni Applicability ni Make). Solo los tipos de ORPHANED son modelos
# reales y quedan en Model; los de los demas son categorias de EASA. Con un
# holder real el AD queda con ese; si solo habia placeholders la lista queda
# vacia y el AD viaja `failed`.

SIB_PAIR_ROW = _row(
    # 2026-10: ALLSTAR PZL GLIDER + la etiqueta de clase como segundo holder.
    "2026-10", "SZD-50-3 - Notification of uncertified parts installed",
    [("ALLSTAR PZL GLIDER", ["SZD-50"]),
     ("Safety Information Bulletin", ["Airworthiness"])],
    links=[f"{_BLOB}/EASA_SIB_2026_10.pdf/SIB_2026-10_1"],
    icon=_icon("SIB", "Safety Information Bulletin"),
)
ORPHANED_PAIR_ROW = _row(
    # US-2021-26-09: BRANTLY + el tipo huerfano del mismo fabricante.
    "US-2021-26-09", "Tail Rotor Hub - Inspections / Removal",
    [("BRANTLY HELICOPTERS INDUSTRIES U.S.A. Co., Ltd.", ["305"]),
     ("ORPHANED (No TC Holder)", ["BRANTLY"])],
    links=[f"{_BLOB}/2021-26-09.pdf/AD_US-2021-26-09_1"],
)
APPLIANCES_PAIR_ROW = _row(
    # Appliance con un holder real ademas del cajon ATA.
    "2025-0100", "Equipment / Furnishings – Oxygen Mask – Inspection",
    [("APPLIANCES", ["ATA 35 OXYGEN"]),
     ("BAE SYSTEMS (OPERATIONS) LTD", ["BAe 146"])],
    links=[f"{_BLOB}/EASA_AD_2025_0100.pdf/AD_2025-0100_1"],
)
APPLIANCES_ONLY_ROW = _row(
    # G-2026-0004: appliance sin TC; EASA lo cuelga del capitulo ATA.
    "G-2026-0004", "TECHTEST - HR SMITH GROUP - Equipment / Furnishings – Inspection",
    [("APPLIANCES", ["ATA 25 EQUIPMENT / FURNISHINGS"])],
    links=[f"{_BLOB}/G-2026-0004.pdf/AD_G-2026-0004_1"],
)
ORPHANED_ONLY_ROW = _row(
    # 2016-0214: AD real sobre un tipo sin titular de TC.
    "2016-0214", "Rudder Pivot Pins – Inspection / Replacement",
    [("ORPHANED (No TC Holder)", ["SLINGSBY"])],
    links=[f"{_BLOB}/EASA_AD_2016_0214.pdf/AD_2016-0214_1"],
)


def test_a_solo_sib_placeholder_leaves_no_holder():
    [ad] = _parse(SOLO_SIB_ROW)
    assert ad["Make"] == ""
    assert ad["Applicability"] == []


def test_a_sib_placeholder_next_to_a_real_holder_is_dropped():
    [ad] = _parse(SIB_PAIR_ROW)
    assert ad["Make"] == "ALLSTAR PZL GLIDER"
    assert ad["Applicability"] == [{"make": "ALLSTAR PZL GLIDER", "models": ["SZD-50"]}]
    assert ad["Model"] == "SZD-50"


def test_an_orphaned_placeholder_next_to_a_real_holder_is_dropped():
    [ad] = _parse(ORPHANED_PAIR_ROW)
    assert ad["Make"] == "BRANTLY HELICOPTERS INDUSTRIES U.S.A. Co., Ltd."
    assert ad["Applicability"] == [
        {"make": "BRANTLY HELICOPTERS INDUSTRIES U.S.A. Co., Ltd.", "models": ["305"]}]
    assert ad["Model"] == "305, BRANTLY"


def test_an_appliances_placeholder_before_a_real_holder_does_not_take_the_make():
    [ad] = _parse(APPLIANCES_PAIR_ROW)
    assert ad["Make"] == "BAE SYSTEMS (OPERATIONS) LTD"
    assert ad["Applicability"] == [{"make": "BAE SYSTEMS (OPERATIONS) LTD", "models": ["BAe 146"]}]


def test_an_appliances_only_holder_leaves_no_holder():
    [ad] = _parse(APPLIANCES_ONLY_ROW)
    assert ad["Make"] == ""
    assert ad["Applicability"] == []
    assert ad["Model"] == ""


def test_an_orphaned_only_holder_leaves_no_holder():
    [ad] = _parse(ORPHANED_ONLY_ROW)
    assert ad["Make"] == ""
    assert ad["Applicability"] == []
    # El tipo es dato real: sigue en ads.model para la busqueda por modelo.
    assert ad["Model"] == "SLINGSBY"


def test_safety_directive_placeholders_leave_no_holder():
    html = _row("SD-2019-01", "Safety Directive",
                [("Safety Directives", ["Airworthiness"]),
                 ("  preliminary  safety directives ", ["Airworthiness"])],
                links=[f"{_BLOB}/SD-2019-01.pdf/SD_SD-2019-01_1"])
    [ad] = _parse(html)
    assert ad["Make"] == ""
    assert ad["Applicability"] == []
    assert ad["Model"] == ""


def test_a_solo_sib_placeholder_leaves_no_model():
    [ad] = _parse(SOLO_SIB_ROW)
    assert ad["Model"] == ""


def test_a_company_that_only_contains_a_placeholder_word_is_kept():
    html = _row("2026-0001", "Seats – Inspection",
                [("AERO SEATS AND SYSTEMS", ["S-1"]), ("APPLIANCES LTD", ["X"])])
    [ad] = _parse(html)
    assert [h["make"] for h in ad["Applicability"]] == ["AERO SEATS AND SYSTEMS", "APPLIANCES LTD"]


def test_an_unreadable_holder_next_to_a_placeholder_still_fails_the_ad():
    """Descartar placeholders no tapa un holder ilegible: ese sigue viajando
    con make vacio y el AD entero falla."""
    html = _row("2026-0003", "x", [("<span>REAL CO</span>", ["A"]), ("APPLIANCES", ["ATA 25"])])
    [ad] = _parse(html)
    assert ad["Applicability"] == [{"make": "", "models": ["A"]}]


# ------------------------------------------------------ avisos de cancelacion
#
# EASA le agrega "-CN" al numero de lo que cancela. Un AD cancelado (y
# cualquier otro aviso -CN) se queda en el catalogo con status Cancelled; la
# cancelacion de un PAD, una propuesta que nunca fue directiva, no entra.

AD_CANCELLATION_ROW = _row(
    "2024-0109-CN", "Landing Gear – Inspection – Cancellation",
    [("AIRBUS S.A.S.", ["A320-214"])],
    links=[f"{_BLOB}/EASA_AD_2024_0109_CN.pdf/AD_2024-0109-CN_1"],
)
# 26-098-CN: PAD cancelado (holder APPLIANCES, como en el listado real).
PAD_CANCELLATION_ROW = _row(
    "26-098-CN", "Equipment / Furnishings – Safety Belts – Inspection" + _SEND_COMMENT,
    [("APPLIANCES", ["ATA 25 EQUIPMENT / FURNISHINGS"])],
    links=[f"{_BLOB}/EASA_PAD_26_098_CN.pdf/PAD_26-098-CN_1"],
    icon=_icon("PAD", "Proposed AD"),
)


def test_an_ad_cancellation_notice_is_written_cancelled():
    [ad] = _parse(AD_CANCELLATION_ROW)
    assert ad["Status"] == "Cancelled"
    assert ad["Document_Class"] == "AD"
    assert populate_db.normalize(ad, "easa")["status"] == "Cancelled"


def test_a_current_publication_is_still_new():
    for html in (AD_ROW, PAD_ROW):
        [ad] = _parse(html)
        assert ad["Status"] == "Current"
        assert populate_db.normalize(ad, "easa")["status"] == "New"


def test_a_cancelled_pad_is_dropped_and_everything_else_kept():
    rows = [_parse(h)[0] for h in (AD_ROW, PAD_ROW, PAD_CANCELLATION_ROW, AD_CANCELLATION_ROW)]
    kept = easa.drop_cancelled_proposals(rows)
    assert [r["AD_Number"] for r in kept] == ["2026-0052R2", "26-116", "2024-0109-CN"]


def test_a_cancellation_notice_of_unknown_class_is_kept():
    """Sin clase no se sabe si era un PAD: se guarda (Cancelled) en vez de perderlo."""
    [ad] = _parse(_row("2024-0110-CN", "Cancellation", [("AIRBUS S.A.S.", ["A320"])]))
    assert ad["Document_Class"] is None
    assert easa.drop_cancelled_proposals([ad]) == [ad]


class _Resp:
    def __init__(self, text):
        self.text = text

    def raise_for_status(self):
        pass


def _listing(total, *rows_html):
    body = "".join(h.split("<table class=\"ad-list\">", 1)[1].rsplit("</table>", 1)[0] for h in rows_html)
    return f"<p>Displaying records 1 to 20 out of a total of {total} publications.</p>" \
           f"<table class=\"ad-list\">{body}</table>"


def test_a_page_of_only_cancelled_pads_does_not_end_the_walk(monkeypatch):
    """El fin del listado lo decide lo leido, no lo que se guarda: una pagina
    que solo trae PADs cancelados no puede cortar el recorrido."""
    pages = [
        _listing(3, PAD_CANCELLATION_ROW),
        _listing(3, AD_ROW),
        _listing(3, AD_CANCELLATION_ROW),
    ]
    asked = []

    def fake_get(url, session=None, params=None, headers=None, timeout=None, tag=None):
        asked.append(url)
        return _Resp(pages[len(asked) - 1])

    monkeypatch.setattr(easa, "get_with_backoff", fake_get)
    monkeypatch.setattr(easa.requests.Session, "get", lambda *a, **k: None)
    monkeypatch.setattr(easa.time, "sleep", lambda _s: None)
    monkeypatch.setattr(easa, "_PAGE_SIZE", 1)

    out = easa.search_ads(max_pages=10)

    assert len(asked) == 3, "recorre las tres paginas: el total cuenta lo leido"
    assert [r["AD_Number"] for r in out] == ["2026-0052R2", "2024-0109-CN"]


def test_the_send_comment_link_is_not_part_of_a_pad_subject():
    [pad] = _parse(PAD_ROW)
    assert pad["Subject"] == "Equipment / Furnishings – System Interface Unit – Replacement"


def test_a_send_comment_glued_to_the_subject_text_is_stripped():
    """Si el link llega como texto suelto (sin <a>), igual se quita."""
    html = _row("26-114", "Turbine Blades – Inspectionsend comment", [("ROLLS-ROYCE", ["BR700"])])
    [pad] = _parse(html)
    assert pad["Subject"] == "Turbine Blades – Inspection"


def test_a_subject_that_merely_mentions_comments_is_untouched():
    [ad] = _parse(_row("2026-0002", "Comment sheet – send comments to EASA", [("X", ["Y"])]))
    assert ad["Subject"] == "Comment sheet – send comments to EASA"
