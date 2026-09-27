"""
Parseo de la columna "Approval Holder / Type Designation" de EASA.

El HTML real anida los type designations DENTRO de cada approval holder
(`li.tc_holder > ul > li.type`). El parser aplanaba las dos listas por separado y
las unia con ", ", perdiendo que modelo pertenece a que fabricante y produciendo
un `Make` de hasta 494 chars que aparecia como opcion del filtro Make en la UI.
"""

from bs4 import BeautifulSoup

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
