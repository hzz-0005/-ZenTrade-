import sqlite3

import pytest
from pydantic import ValidationError

from webapp.core.models import SessionSpec
from webapp.store import db


def _spec(**overrides):
    values = {
        "ticker": "600519.SS",
        "start_date": "2026-01-01",
        "end_date": "2026-01-31",
        "initial_capital": 100_000,
    }
    values.update(overrides)
    return SessionSpec(**values)


def test_session_spec_defaults_to_adaptive():
    assert _spec().decision_architecture == "adaptive"


def test_session_spec_rejects_unknown_architecture():
    with pytest.raises(ValidationError):
        _spec(decision_architecture="twelve_agents")


def test_migrate_maps_legacy_full_graph_rows():
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE sessions (
          id TEXT PRIMARY KEY,
          use_full_graph INTEGER NOT NULL DEFAULT 0,
          initial_position TEXT NOT NULL DEFAULT 'full'
        );
        CREATE TABLE trades (
          id INTEGER PRIMARY KEY,
          entry_fee REAL NOT NULL DEFAULT 0,
          sold_shares REAL NOT NULL DEFAULT 0,
          partial_realized_pnl REAL NOT NULL DEFAULT 0
        );
        INSERT INTO sessions (id, use_full_graph) VALUES ('graph', 1);
        INSERT INTO sessions (id, use_full_graph) VALUES ('fast', 0);
        """
    )

    db._migrate(conn)

    rows = dict(conn.execute("SELECT id, decision_architecture FROM sessions"))
    assert rows == {"graph": "classic_graph", "fast": "fast"}

