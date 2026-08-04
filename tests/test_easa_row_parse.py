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
