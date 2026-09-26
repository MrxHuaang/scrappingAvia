"""Tipo de producto de los ADs de la FAA: lo declara el título, no el abstract."""

import pytest

from enrich import classify_product_type


@pytest.mark.parametrize("subject, expected", [
    # Casos reales que salían como Engine porque el abstract menciona un motor.
    ("The Boeing Company Airplanes", "Aircraft"),
    ("Boeing Model 737-600, -700, and -800 Series Airplanes", "Aircraft"),
    ("Boeing Model 737-300, 737-400, 737-500, 737-600, 737-700, 737-800, "
     "757-200, 757-200PF, 757-200CB, and 757-300 Series Airplanes", "Aircraft"),
    ("CFM International S.A. Turbofan Engines", "Engine"),
    ("Pratt & Whitney Canada Corp. Turboprop Engines", "Engine"),
    ("Hamilton Sundstrand Corporation Propellers", "Propeller"),
    ("Airbus Helicopters Model EC135 Helicopters", "Aircraft"),
    ("Rolls-Royce Deutschland Ltd & Co KG Turbofan Engines", "Engine"),
    ("Schempp-Hirth Flugzeugbau GmbH Gliders", "Aircraft"),
    # El motor como calificativo del avión no lo convierte en AD de motor.
    ("Boeing Model 747-200 Series Airplanes Equipped With General Electric "
     "CF6-80C2 Series Engines", "Aircraft"),
    ("Airbus Model A330 Airplanes Powered by Rolls-Royce Trent 700 Engines", "Aircraft"),
    ("Various Helicopters Fitted With Turbomeca Arriel Turboshaft Engines", "Aircraft"),
])
def test_title_product_noun_wins_over_the_abstract(subject, expected):
    abstract = "This AD was prompted by reports of an engine fire and propeller damage."
    assert classify_product_type(subject, abstract) == expected


def test_without_a_title_noun_falls_back_to_the_abstract():
    assert classify_product_type("Kidde Aerospace", "smoke detectors on engines") == "Engine"
    assert classify_product_type("Honeywell", "installed on various airplanes") == "Aircraft"


def test_then_to_cfr_part_39():
    assert classify_product_type("Unknown", "", [{"part": "39"}]) == "Aircraft"
    assert classify_product_type("Unknown", "", []) == ""
