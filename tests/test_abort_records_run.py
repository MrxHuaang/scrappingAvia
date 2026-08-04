"""Un aborto por timeout tiene que dejar fila en `scrape_runs`.

El workflow ya convierte un desborde en `failure` (proceso acotado con
`timeout(1)` por debajo del presupuesto del job, salida 124). Eso arregla la
senal del lado de GitHub. Del lado de la base seguia sin arreglar: la escritura
de `scrape_runs` vive al final del bucle de la fuente, asi que el SIGINT llegaba
antes y no quedaba fila — la fuente aparecia como *ausencia*, que es justo el
disfraz que dejo a EASA 19 dias sin actualizar sin que nada lo dijera.

Estas pruebas fijan el handler: escribe la fila de error, sale con 124, y no
duplica si la fila ya se escribio.
"""

import signal

import pytest

import populate_db


class FakeTable:
    def __init__(self, sink):
        self._sink = sink

    def insert(self, payload):
        self._sink.append(payload)
        return self

    def execute(self):
        return None


class FakeSupabase:
    """Solo lo que toca `save_scrape_run`."""

    def __init__(self):
        self.inserted = []

    def table(self, name):
        assert name == "scrape_runs"
        return FakeTable(self.inserted)


@pytest.fixture
def abort_env(monkeypatch):
    """Handler aislado: `os._exit` se convierte en SystemExit para poder verlo."""
    exits = []

    def fake_exit(code):
        exits.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(populate_db.os, "_exit", fake_exit)
    monkeypatch.setattr(populate_db, "_ABORTING", False)
    monkeypatch.setattr(populate_db, "_IN_FLIGHT", None)
    return exits


def test_an_abort_mid_source_writes_an_error_row(abort_env, monkeypatch):
    from datetime import datetime, timezone

    sb = FakeSupabase()
    started = datetime(2026, 8, 4, 6, 0, tzinfo=timezone.utc)
    monkeypatch.setattr(
        populate_db,
        "_IN_FLIGHT",
        {"supabase": sb, "source": "easa", "started_at": started},
    )

    with pytest.raises(SystemExit):
        populate_db._on_abort(signal.SIGINT, None)

    assert len(sb.inserted) == 1, "un aborto sin fila es exactamente la ausencia invisible"
    row = sb.inserted[0]
    assert row["source"] == "easa"
    assert row["status"] == "error"
    assert "SIGINT" in row["error_msg"]
    assert row["started_at"] == started.isoformat()


def test_the_abort_exit_code_is_124(abort_env, monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setattr(
        populate_db,
        "_IN_FLIGHT",
        {
            "supabase": FakeSupabase(),
            "source": "easa",
            "started_at": datetime.now(timezone.utc),
        },
    )
    with pytest.raises(SystemExit):
        populate_db._on_abort(signal.SIGTERM, None)
    # 124 es el codigo de `timeout(1)`: se conserva para que correrlo a mano
    # fuera del workflow reporte lo mismo que dentro.
    assert abort_env == [124]


def test_an_abort_between_sources_writes_nothing(abort_env):
    # _IN_FLIGHT queda en None despues de save_scrape_run; abortar ahi no debe
    # duplicar la fila que ya se escribio.
    with pytest.raises(SystemExit):
        populate_db._on_abort(signal.SIGTERM, None)
    assert abort_env == [124]


def test_a_second_signal_exits_without_retrying_the_write(abort_env, monkeypatch):
    from datetime import datetime, timezone

    sb = FakeSupabase()
    monkeypatch.setattr(
        populate_db,
        "_IN_FLIGHT",
        {"supabase": sb, "source": "easa", "started_at": datetime.now(timezone.utc)},
    )
    monkeypatch.setattr(populate_db, "_ABORTING", True)

    with pytest.raises(SystemExit):
        populate_db._on_abort(signal.SIGINT, None)

    assert sb.inserted == [], (
        "la segunda senal llega cuando la primera sigue escribiendo; reintentar "
        "el INSERT ahi solo gasta el margen del --kill-after"
    )
