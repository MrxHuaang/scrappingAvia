"""product_subtype de las filas de applicability (enrich.product_subtype).

El catálogo es cerrado y vive en la BD (migración 20260926120000 de
ad-scrapper); la FK compuesta (product_type, product_subtype) rechaza un
subtipo de otro tipo. El clasificador sale del mismo sustantivo que ya decide
el tipo y, sin coincidencia segura, no emite nada — nunca 'other'.
"""

import json
import os

import pytest

import enrich
from enrich import PRODUCT_SUBTYPES, PRODUCT_TYPE_CODE, classify_product_type, product_subtype

_FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures", "fr_applicability.json")
with open(_FIXTURES, encoding="utf-8") as _f:
    REAL_DOCS = {d["document_number"]: d for d in json.load(_f)}


def _faa(doc_number: str) -> tuple[str | None, str | None]:
    """(product_type code, subtype) como los calcula el pipeline para un AD FAA."""
    doc = REAL_DOCS[doc_number]
    subject = doc["title"].split("; ", 1)[-1]
    pt = enrich.normalize_product_type(classify_product_type(subject, doc["abstract"], [{"part": "39"}]))
    code = PRODUCT_TYPE_CODE.get(pt or "")
    return code, product_subtype(code, subject, doc["abstract"])


@pytest.mark.parametrize("doc_number, expected", [
    ("2024-08105", ("aircraft", "airplane")),     # The Boeing Company Airplanes
    ("2024-21112", ("aircraft", "airplane")),     # Textron Aviation Inc. (TC previously held ...) Airplanes
    ("98-24245", ("aircraft", "airplane")),       # ... Airplanes Equipped with J.C. Carter ... Actuators
    ("98-8582", ("aircraft", "glider")),          # Schempp-Hirth K.G. Models ... Sailplanes
    ("2024-14072", ("aircraft", "balloon")),      # Lindstrand Balloons Ltd. Hot Air Balloons
    ("E6-18368", ("engine", "turbine")),          # Pratt & Whitney PW4074 ... Turbofan Engines
    ("06-522", ("engine", "turbine")),            # Turbomeca Arriel ... Turboshaft Engines
    ("2015-16584", ("engine", "turbine")),        # GE Aviation Czech s.r.o. Turboprop Engines
    ("98-22240", ("engine", "piston")),           # ... Reciprocating Engines
    ("2023-28861", ("engine", None)),             # Rolls-Royce Deutschland Ltd & Co KG Engines
    ("06-5284", ("propeller", None)),             # Hamilton Sundstrand Model 14RF-19 Propellers
    ("2024-03920", ("rotorcraft", "helicopter")),  # Robinson Helicopter Company Helicopters
    ("98-2969", ("rotorcraft", "helicopter")),    # Eurocopter France Model SA-365N ... Helicopters
    # Appliances: la pieza es el producto.
    ("E6-13911", ("appliance", "seats")),         # RECARO Aircraft Seating GmbH & Co. ... Seats
    ("2015-11392", ("appliance", "seats")),       # Zodiac Seats France ... Passenger Seat Assemblies
    ("2015-09467", ("appliance", "oxygen")),      # Zodiac Aerotechnics ... Oxygen Mask Regulators
    ("2015-14645", ("appliance", "avionics")),    # Avidyne Corporation Integrated Flight Displays
    ("06-5208", ("appliance", "evacuation")),     # Goodrich Evacuation Systems ... Installed on Airbus
    # Pieza sin categoría en el catálogo: Appliance sin subtipo, nunca 'other'.
    ("98-24089", ("appliance", None)),            # Superior Air Parts, Inc., Piston Pins Installed on ...
    ("06-4646", ("appliance", None)),             # ECi Reciprocating Engine Connecting Rods
    # ", Equipped With ... Engines" tras la coma sigue siendo un avión.
    ("E6-18663", ("aircraft", "airplane")),
    ("2015-08732", ("aircraft", None)),           # Various Aircraft Equipped With Wing Lift Struts
    # El título solo nombra al titular: decide la oración de applicability.
    ("2015-19474", ("appliance", "fire_protection")),  # Kidde Graviner (... fire extinguishers)
    ("2015-01803", ("rotorcraft", "helicopter")),  # Airbus Helicopters (Formerly Eurocopter France)
])
def test_real_faa_titles(doc_number, expected):
    assert _faa(doc_number) == expected


@pytest.mark.parametrize("code, subject, expected", [
    # Rotorcraft (EASA/TC tipan Helicopter).
    ("rotorcraft", "Robinson Helicopter Company Helicopters", "helicopter"),
    ("rotorcraft", "Rotorway International Gyroplanes", "gyroplane"),
    ("rotorcraft", "Rotors - Main Rotor Blade - Inspection", None),
    # Aircraft.
    ("aircraft", "Glaser-Dirks Flugzeugbau GmbH Model DG-500M Gliders", "glider"),
    ("aircraft", "Airships Inc. Airships", "airship"),
    ("aircraft", "Airbus Model A330 Airplanes Powered by Rolls-Royce Trent 700 Engines", "airplane"),
    # Motores.
    ("engine", "CFM International S.A. Turbofan Engines", "turbine"),
    ("engine", "Rolls-Royce Corporation Turboprop and Turboshaft Engines", "turbine"),
    ("engine", "Technify Motors GmbH Reciprocating Engines", "piston"),
    ("engine", "magniX USA, Inc. Electric Engines", "electric"),
    # El nombre del titular no cuenta: solo la cadena pegada al sustantivo.
    ("engine", "Rolls-Royce Corporation (Formerly Allison Gas Turbine Division, and Detroit "
               "Diesel Allison) 250-B Series Turboshaft Engines", "turbine"),
    # Dos categorías en la misma cadena: no se elige.
    ("engine", "Acme Reciprocating and Turboprop Engines", None),
    # APU vs motor.
    ("engine", "Honeywell International Inc. Auxiliary Power Units", "apu"),
    # El producto es el APU; los aviones son dónde va instalado.
    ("engine", "Honeywell International Inc. APUs Installed on Airbus Airplanes", "apu"),
    ("aircraft", "Honeywell International Inc. APUs Installed on Airbus Airplanes", None),
    # Appliances: la pieza nombrada, una sola.
    ("appliance", "Zodiac Seats France Passenger Seats", "seats"),
    ("appliance", "Kannad Emergency Locator Transmitters", "elt"),
    ("appliance", "Zodiac Aerotechnics Oxygen Mask Regulators", "oxygen"),
    ("appliance", "Kidde Graviner Hand-Operated Fire Extinguishers", "fire_protection"),
    ("appliance", "Goodrich Evacuation Systems Installed on Airbus Model A330 Airplanes", "evacuation"),
    ("appliance", "Honeywell RCZ-833J/K Communication Units", "avionics"),
    # Lo que va tras "Installed on" no cuenta: sigue siendo un asiento.
    ("appliance", "Acme Passenger Seats Installed on Airplanes Equipped With Oxygen Systems", "seats"),
    # Dos piezas distintas o ninguna conocida: se omite, nunca 'other'.
    ("appliance", "Safran Oxygen Cylinder and Fire Extinguisher Assemblies", None),
    ("appliance", "Precision Airmotive Corporation Carburetors", None),
    # Hélice: el catálogo no tiene subtipos.
    ("propeller", "Hartzell Propeller Inc. Turboprop Propellers", None),
])
def test_edge_cases(code, subject, expected):
    assert product_subtype(code, subject) == expected


def test_turbo_propeller_does_not_become_a_propeller():
    title = ("Turbo-Propeller Powered General Dynamics (Convair) Model 240, 340, and 440 "
             "Series Airplanes")
    assert classify_product_type(title) == "Aircraft"
    assert product_subtype("aircraft", title) == "airplane"
    assert product_subtype("engine", title) is None
    assert product_subtype("propeller", title) is None


def test_a_subtype_of_another_type_is_never_produced():
    subjects = [d["title"].split("; ", 1)[-1] for d in REAL_DOCS.values()] + [
        "Robinson Helicopter Company Helicopters", "CFM International S.A. Turbofan Engines",
        "Zodiac Seats France Passenger Seats", "Honeywell International Inc. Auxiliary Power Units",
        "Lindstrand Balloons Ltd. Hot Air Balloons", "Kannad Emergency Locator Transmitters",
    ]
    for code in list(PRODUCT_SUBTYPES) + ["unknown", "", None]:
        for subject in subjects:
            got = product_subtype(code, subject)
            assert got is None or got in PRODUCT_SUBTYPES[code], (code, subject, got)
            assert got != "other"


def test_catalog_codes_match_the_product_type_mapping():
    assert set(PRODUCT_TYPE_CODE.values()) == set(PRODUCT_SUBTYPES)
