"""scripts/reclassify_product_type.py contra un cliente falso: nunca una base.

Comprueba el plan (qué ADs cambian y por qué camino) y que la escritura usa
los dos caminos existentes: `ads.product_type` (el trigger
`ads_propagate_product_type` lleva el tipo a las filas 'derived') y la RPC
`replace_ad_applicability` para lo que el trigger no hace (subtipos y filas
'explicit'), reenviando las filas guardadas con la misma clave natural.
"""

import io

import pytest

import enrich
import populate_db
from scripts import reclassify_product_type as rp


class _Resp:
    def __init__(self, data):
        self.data = data


class _Query:
    def __init__(self, db, table):
        self.db, self.table, self.filters, self.rng, self.update_values = db, table, [], None, None

    def select(self, *_a, **_k):
        return self

    def eq(self, col, val):
        self.filters.append(lambda r: r.get(col) == val)
        return self

    def in_(self, col, vals):
        vals = set(vals)
        self.filters.append(lambda r: r.get(col) in vals)
        return self

    def order(self, *_a, **_k):
        return self

    def range(self, a, b):
        self.rng = (a, b)
        return self

    def update(self, values):
        self.update_values = values
        return self

    def execute(self):
        rows = [r for r in self.db.tables[self.table] if all(f(r) for f in self.filters)]
        if self.update_values is not None:
            self.db.updates.append((self.table, dict(self.update_values), sorted(r["id"] for r in rows)))
            return _Resp([])
        if self.rng:
            rows = rows[self.rng[0]:self.rng[1] + 1]
        return _Resp([dict(r) for r in rows])


class FakeSupabase:
    def __init__(self, ads, applicability):
        self.tables = {"ads": ads, "ad_applicability": applicability}
        self.updates, self.rpcs = [], []

    def table(self, name):
        return _Query(self, name)

    def rpc(self, name, params):
        self.rpcs.append((name, params))
        data = [{"ad_id": e["ad_id"], "status": e["status"], "rows_now": len(e["rows"])} for e in params["p_ads"]]
        return type("Q", (), {"execute": lambda _self: _Resp(data)})()


def _ad(i, subject, product_type, abstract=""):
    return {"id": f"ad-{i}", "ad_number": f"2024-00-0{i}", "source": "federal_register",
            "subject": subject, "product_type": product_type, "abstract": abstract}


def _row(i, ad_id, product_type, source, subtype=None, make="HOLDER", model="M1"):
    return {"id": f"row-{i}", "ad_id": ad_id, "make": make, "model": model, "series": None,
            "serial_range_start": None, "serial_range_end": None, "confidence": "high",
            "model_status": "listed", "product_type": product_type, "product_subtype": subtype,
            "product_type_source": source}


@pytest.fixture
def fake(monkeypatch):
    ads = [
        # Helicóptero tipado Aircraft, fila derived: tipo por trigger, subtipo por RPC.
        _ad(1, "Robinson Helicopter Company Helicopters", "Aircraft"),
        # Tipo ya correcto, fila explicit sin subtipo: solo RPC.
        _ad(2, "CFM International S.A. Turbofan Engines", "Engine"),
        # Engine -> Appliance sin subtipo, fila derived: solo ads (el trigger basta).
        _ad(3, "Precision Airmotive Corporation Carburetors", "Engine"),
        # Nada que cambiar.
        _ad(4, "The Boeing Company Airplanes", "Aircraft"),
        # Cambia el tipo y no tiene filas.
        _ad(5, "Zodiac Seats France Passenger Seat Assemblies", "Aircraft"),
        # Fila explicit con el tipo viejo: la RPC la corrige (el trigger no la toca).
        _ad(6, "Bell Textron Inc. Helicopters", "Aircraft"),
    ]
    rows = [
        _row(1, "ad-1", "aircraft", "derived"),
        _row(2, "ad-2", "engine", "explicit"),
        _row(3, "ad-3", "engine", "derived"),
        _row(4, "ad-4", "aircraft", "explicit", subtype="airplane"),
        _row(6, "ad-6", "aircraft", "explicit", make="BELL", model=None),
    ]
    rows[4]["model_status"] = "unknown"
    sb = FakeSupabase(ads, rows)
    monkeypatch.setattr(populate_db, "get_supabase_client", lambda: sb)
    monkeypatch.setattr(populate_db, "_write_errors", [])
    return sb


def _plan(sb):
    ads = rp.fetch_faa_ads(sb)
    return {c["ad_number"]: c for c in rp.plan(ads, rp.fetch_applicability(sb, [a["id"] for a in ads]))}


def test_plan_lists_only_what_changes_and_by_which_path(fake):
    plan = _plan(fake)
    assert set(plan) == {"2024-00-01", "2024-00-02", "2024-00-03", "2024-00-05", "2024-00-06"}
    assert (plan["2024-00-01"]["after"], plan["2024-00-01"]["subtype"], plan["2024-00-01"]["stale"]) == \
        ("Helicopter", "helicopter", 1)
    assert (plan["2024-00-02"]["type_changes"], plan["2024-00-02"]["subtype"], plan["2024-00-02"]["stale"]) == \
        (False, "turbine", 1)
    # Derived y sin subtipo nuevo: el trigger lo arregla, la fila sigue 'derived'.
    assert (plan["2024-00-03"]["after"], plan["2024-00-03"]["stale"]) == ("Appliance", 0)
    assert (plan["2024-00-05"]["after"], plan["2024-00-05"]["rows"]) == ("Appliance", [])
    assert plan["2024-00-06"]["stale"] == 1


def test_dry_run_is_the_default_and_writes_nothing(fake, capsys):
    assert rp.main([]) == 0
    out, err = capsys.readouterr()
    assert fake.updates == [] and fake.rpcs == []
    lines = out.strip().splitlines()
    assert lines[0] == ",".join(rp.CSV_COLUMNS)
    assert "2024-00-01,Aircraft,Helicopter,helicopter,1,1" in lines
    assert "Aircraft -> Helicopter: 2" in err
    assert "dry-run" in err


def test_apply_updates_ads_by_type_and_sends_the_stored_rows_through_the_rpc(fake, capsys):
    assert rp.main(["--apply"]) == 0
    updates = {vals["product_type"]: ids for (table, vals, ids) in fake.updates if table == "ads"}
    assert updates == {"Helicopter": ["ad-1", "ad-6"], "Appliance": ["ad-3", "ad-5"]}

    (name, params), = fake.rpcs
    assert name == "replace_ad_applicability"
    by_ad = {e["ad_id"]: e for e in params["p_ads"]}
    assert set(by_ad) == {"ad-1", "ad-2", "ad-6"}
    assert all(e["status"] == "success_with_rows" for e in by_ad.values())
    # Misma clave natural y demás campos tal cual; solo cambia la clasificación.
    assert by_ad["ad-1"]["rows"] == [{"make": "HOLDER", "model": "M1", "confidence": "high",
                                      "model_status": "listed", "product_type": "rotorcraft",
                                      "product_subtype": "helicopter"}]
    assert by_ad["ad-2"]["rows"][0]["product_subtype"] == "turbine"
    assert by_ad["ad-6"]["rows"] == [{"make": "BELL", "confidence": "high", "model_status": "unknown",
                                      "product_type": "rotorcraft", "product_subtype": "helicopter"}]


def test_a_subtype_is_never_sent_without_its_type_or_outside_it(fake):
    ads = rp.fetch_faa_ads(fake)
    changes = rp.plan(ads, rp.fetch_applicability(fake, [a["id"] for a in ads]))
    for c in changes:
        if c["subtype"]:
            assert c["code"] and c["subtype"] in enrich.PRODUCT_SUBTYPES[c["code"]]


def test_csv_to_a_file(fake, tmp_path):
    target = tmp_path / "plan.csv"
    assert rp.main(["--csv", str(target)]) == 0
    assert target.read_text(encoding="utf-8").startswith(",".join(rp.CSV_COLUMNS))
    buf = io.StringIO()
    rp.write_csv([], buf)
    assert buf.getvalue().strip() == ",".join(rp.CSV_COLUMNS)
