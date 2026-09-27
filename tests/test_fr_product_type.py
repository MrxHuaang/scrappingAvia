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
    ("Airbus Helicopters Model EC135 Helicopters", "Helicopter"),
    ("Rolls-Royce Deutschland Ltd & Co KG Turbofan Engines", "Engine"),
    ("Schempp-Hirth Flugzeugbau GmbH Gliders", "Aircraft"),
    # El motor como calificativo del avión no lo convierte en AD de motor.
    ("Boeing Model 747-200 Series Airplanes Equipped With General Electric "
     "CF6-80C2 Series Engines", "Aircraft"),
    ("Airbus Model A330 Airplanes Powered by Rolls-Royce Trent 700 Engines", "Aircraft"),
    ("Various Helicopters Fitted With Turbomeca Arriel Turboshaft Engines", "Helicopter"),
    ("Airbus Model A300 Airplanes, Equipped With General Electric CF6-50 Series Engines", "Aircraft"),
    # Helicopters/Rotorcraft/Gyroplanes: Helicopter, que el filtro separa de Aircraft.
    ("Robinson Helicopter Company Helicopters", "Helicopter"),
    ("Airbus Helicopters (Formerly Eurocopter France)", "Helicopter"),
    ("Rotorway International Gyroplanes", "Helicopter"),
    # Pieza como producto: Appliance.
    ("Zodiac Seats France (Formerly Sicma Aero Seat) Passenger Seat Assemblies", "Appliance"),
    ("Zodiac Aerotechnics (Formerly Intertechnique Aircraft Systems) Oxygen Mask Regulators", "Appliance"),
    ("Kannad Emergency Locator Transmitters", "Appliance"),
    ("Superior Air Parts, Inc., Piston Pins Installed on Teledyne Continental Motors "
     "Reciprocating Engines", "Appliance"),
    ("Goodrich Evacuation Systems Approved Under Technical Standard Order (TSO) TSO-C69b and "
     "Installed on Airbus Model A330-200 and -300 Series Airplanes", "Appliance"),
    # El sustantivo solo califica a la pieza que sigue.
    ("Engine Components Incorporated (ECi) Reciprocating Engine Connecting Rods", "Appliance"),
    ("RECARO Aircraft Seating GmbH & Co. (RECARO) Model 3410 Seats", "Appliance"),
    # APU: Engine (subtipo apu), no Appliance.
    ("Honeywell International Inc. Auxiliary Power Units", "Engine"),
    # "Aircraft" en el nombre de un titular anterior no decide: cae al abstract
    # (que aquí habla de un motor). "Turbo-Propeller" tampoco es una hélice.
    ("Zodiac Aerotechnics (Formerly Intertechnique Aircraft Systems)", "Engine"),
    ("Turbo-Propeller Powered General Dynamics (Convair) Model 240, 340, and 440 Series Airplanes",
     "Aircraft"),
])
def test_title_product_noun_wins_over_the_abstract(subject, expected):
    abstract = "This AD was prompted by reports of an engine fire and propeller damage."
    assert classify_product_type(subject, abstract) == expected


def test_without_a_title_noun_falls_back_to_the_abstract():
    assert classify_product_type("Kidde Aerospace", "smoke detectors on engines") == "Engine"
    assert classify_product_type("Honeywell", "installed on various airplanes") == "Aircraft"
    assert classify_product_type("Bell", "for all Bell Model 407 helicopters") == "Helicopter"


@pytest.mark.parametrize("subject, abstract, expected", [
    # Un sustantivo dentro del nombre del titular no decide; con el título sin
    # producto, decide la oración de applicability del abstract.
    ("Sikorsky Aircraft Corporation (Type Certificate Previously Held by Schweizer Aircraft "
     "Corporation)",
     "We are adopting a new airworthiness directive (AD) for Sikorsky Aircraft Corporation "
     "(Sikorsky) Model 269A and 269D helicopters. This AD requires inspecting the engine mount.",
     "Helicopter"),
    ("Bell Helicopter Textron Canada Limited",
     "We are adopting a new airworthiness directive (AD) for certain Bell Helicopter Textron "
     "Canada Limited (Bell) Model 206L-1 helicopters. This AD was prompted by an engine fire.",
     "Helicopter"),
    ("Airship Industries (UK) Ltd.",
     "The FAA is adopting a new AD for all Airship Industries (UK) Ltd. Model Skyship 600 airships.",
     "Aircraft"),
    ("Diamond Aircraft Industries Inc. Airplanes", "", "Aircraft"),
    ("Lycoming Engines Reciprocating Engines (Type Certificate previously held by Textron "
     "Lycoming Division, AVCO Corporation)", "", "Engine"),
    ("Kidde Graviner",
     "We are adopting a new airworthiness directive (AD) for certain Kidde Graviner hand-operated "
     "fire extinguishers. This AD was prompted by a report.", "Appliance"),
    ("The Boeing Company",
     "We are adopting a new airworthiness directive (AD) for all The Boeing Company Model 757 "
     "airplanes. This AD was prompted by reports of latently failed engine fuel valves.", "Aircraft"),
])
def test_a_noun_inside_the_holder_name_does_not_decide(subject, abstract, expected):
    assert classify_product_type(subject, abstract) == expected


def test_a_mixed_engine_and_rotorcraft_title_follows_the_last_noun():
    """Caso mixto conocido: el AD cubre motores y aeronaves; gana el último
    sustantivo, como en cualquier otro título."""
    title = "General Electric Company Engines, and Various Restricted Category Rotorcraft"
    assert classify_product_type(title) == "Helicopter"


def test_then_to_cfr_part_39():
    assert classify_product_type("Unknown", "", [{"part": "39"}]) == "Aircraft"
    assert classify_product_type("Unknown", "", []) == ""
