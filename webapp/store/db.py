"""SQLite storage for the backtest webapp.

Single lock-guarded connection in WAL mode; all calls are made from
asyncio.to_thread by callers, so a plain threading.Lock suffices.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path

from webapp.config import DATA_DIR

_DDL = """
PRAGMA journal_mode=WAL;

CREATE TABLE IF NOT EXISTS sessions (
  id TEXT PRIMARY KEY,
  ticker TEXT NOT NULL,
  canonical_ticker TEXT NOT NULL,
  market TEXT NOT NULL,
  start_date TEXT NOT NULL,
  end_date TEXT NOT NULL,
  initial_capital REAL NOT NULL,
  commission_rate REAL NOT NULL,
  min_commission REAL NOT NULL,
  slippage_bps REAL NOT NULL,
  status TEXT NOT NULL DEFAULT 'created',
  trading_days TEXT NOT NULL DEFAULT '[]',
  current_day_index INTEGER NOT NULL DEFAULT 0,
  cash REAL NOT NULL,
  shares REAL NOT NULL DEFAULT 0,
  avg_cost REAL NOT NULL DEFAULT 0,
  final_equity REAL,
  total_return_pct REAL,
  max_drawdown_pct REAL,
  win_rate REAL,
  llm_call_count INTEGER NOT NULL DEFAULT 0,
  error TEXT,
  created_at TEXT NOT NULL,
  started_at TEXT,
  finished_at TEXT,
  use_full_graph INTEGER NOT NULL DEFAULT 0,
  decision_architecture TEXT NOT NULL DEFAULT 'adaptive',
  decision_mode TEXT NOT NULL DEFAULT 'band',
  initial_position TEXT NOT NULL DEFAULT 'full',
  execution_timing TEXT NOT NULL DEFAULT 'next_open'
);

CREATE TABLE IF NOT EXISTS daily_records (
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  day_index INTEGER NOT NULL,
  date TEXT NOT NULL,
  decision_date TEXT,
  execution_date TEXT,
  valuation_date TEXT,
  execution_status TEXT NOT NULL DEFAULT 'legacy',
  decision_json TEXT NOT NULL,
  action TEXT NOT NULL,
  requested_pct REAL,
  executed_shares REAL,
  executed_price REAL,
  fee REAL,
  cash_after REAL NOT NULL,
  shares_after REAL NOT NULL,
  equity REAL NOT NULL,
  data_flags TEXT NOT NULL DEFAULT '[]',
  skills_injected TEXT NOT NULL DEFAULT '[]',
  context_json TEXT,
  prompt_text TEXT,
  response_text TEXT,
  llm_calls INTEGER NOT NULL DEFAULT 1,
  latency_ms INTEGER,
  PRIMARY KEY (session_id, day_index)
);

CREATE TABLE IF NOT EXISTS trades (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  symbol TEXT NOT NULL,
  side TEXT NOT NULL,
  entry_date TEXT NOT NULL,
  entry_price REAL NOT NULL,
  exit_date TEXT,
  exit_price REAL,
  shares REAL NOT NULL,
  realized_pnl REAL,
  fees REAL,
  return_pct REAL,
  status TEXT NOT NULL DEFAULT 'open',
  opened_day_index INTEGER,
  closed_day_index INTEGER,
  entry_fee REAL NOT NULL DEFAULT 0,
  sold_shares REAL NOT NULL DEFAULT 0,
  partial_realized_pnl REAL NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS session_reviews (
  session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,
  summary TEXT NOT NULL,
  reflection TEXT NOT NULL,
  trade_reviews_json TEXT NOT NULL,
  metrics_json TEXT NOT NULL,
  skills_created_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skills (
  id TEXT PRIMARY KEY,
  category TEXT NOT NULL,
  statement TEXT NOT NULL,
  source_session TEXT,
  source_ticker TEXT,
  source_market TEXT,
  created_at TEXT NOT NULL,
  performance_evidence TEXT NOT NULL DEFAULT '{}',
  times_applied INTEGER NOT NULL DEFAULT 0,
  times_helpful INTEGER NOT NULL DEFAULT 0,
  success_rate REAL NOT NULL DEFAULT 0.5,
  enabled INTEGER NOT NULL DEFAULT 1,
  merged_into TEXT,
  UNIQUE(category, statement)
);

CREATE TABLE IF NOT EXISTS llm_calls (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id TEXT,
  day_index INTEGER,
  purpose TEXT,
  model TEXT,
  latency_ms INTEGER,
  ok INTEGER,
  created_at TEXT
);

CREATE TABLE IF NOT EXISTS price_frames (
  session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
  date TEXT NOT NULL,
  open REAL NOT NULL, high REAL NOT NULL, low REAL NOT NULL,
  close REAL NOT NULL, volume REAL NOT NULL DEFAULT 0,
  PRIMARY KEY (session_id, date)
);

CREATE INDEX IF NOT EXISTS idx_daily_session ON daily_records(session_id, day_index);
CREATE INDEX IF NOT EXISTS idx_trades_session ON trades(session_id);
CREATE INDEX IF NOT EXISTS idx_skills_cat ON skills(category, enabled);
"""

# Reentrant: execute()/query() hold it while get_conn()->init_db() runs, so a
# first-use inside execute() must not self-deadlock.
_lock = threading.RLock()
_conn: sqlite3.Connection | None = None


def _connect(db_path: Path | None = None) -> sqlite3.Connection:
    path = db_path or DATA_DIR / "backtest.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db(db_path: Path | None = None) -> None:
    global _conn
    with _lock:
        if _conn is None:
            _conn = _connect(db_path)
        _conn.executescript(_DDL)
        _migrate(_conn)
        _conn.commit()


def _migrate(conn: sqlite3.Connection) -> None:
    """Lightweight column migration for DBs created before a column existed."""
    cols = {r[1] for r in conn.execute("PRAGMA table_info(sessions)")}
    if "decision_architecture" not in cols:
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN decision_architecture "
            "TEXT NOT NULL DEFAULT 'adaptive'"
        )
        conn.execute(
            "UPDATE sessions SET decision_architecture="
            "CASE WHEN use_full_graph=1 THEN 'classic_graph' ELSE 'fast' END"
        )
    if "decision_mode" not in cols:
        conn.execute("ALTER TABLE sessions ADD COLUMN decision_mode TEXT NOT NULL DEFAULT 'band'")
    if "initial_position" not in cols:
        # 'full': the engine force-buys a full position on day 0 (policy, not
        # the LLM) so the agent manages a holding instead of waiting to enter.
        conn.execute("ALTER TABLE sessions ADD COLUMN initial_position TEXT NOT NULL DEFAULT 'full'")
    if "execution_timing" not in cols:
        # Existing, potentially paused sessions were recorded with the old
        # same-close convention.  Keep them resumable and opt newly-created
        # sessions into next_open explicitly in the session router.
        conn.execute(
            "ALTER TABLE sessions ADD COLUMN execution_timing "
            "TEXT NOT NULL DEFAULT 'same_close'"
        )
    has_daily_records = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='daily_records'"
    ).fetchone()
    if has_daily_records:
        daily_cols = {r[1] for r in conn.execute("PRAGMA table_info(daily_records)")}
        if "decision_date" not in daily_cols:
            conn.execute("ALTER TABLE daily_records ADD COLUMN decision_date TEXT")
        if "execution_date" not in daily_cols:
            conn.execute("ALTER TABLE daily_records ADD COLUMN execution_date TEXT")
        if "valuation_date" not in daily_cols:
            conn.execute("ALTER TABLE daily_records ADD COLUMN valuation_date TEXT")
        if "execution_status" not in daily_cols:
            conn.execute(
                "ALTER TABLE daily_records ADD COLUMN execution_status "
                "TEXT NOT NULL DEFAULT 'legacy'"
            )
    # Partial-sell accounting: entry fee kept separate (charged once at close),
    # plus the running tally of already-realized partial slices.
    trade_cols = {r[1] for r in conn.execute("PRAGMA table_info(trades)")}
    for name in ("entry_fee", "sold_shares", "partial_realized_pnl"):
        if name not in trade_cols:
            conn.execute(f"ALTER TABLE trades ADD COLUMN {name} REAL NOT NULL DEFAULT 0")


def get_conn() -> sqlite3.Connection:
    if _conn is None:
        init_db()
    return _conn


def execute(sql: str, params: tuple = ()) -> None:
    with _lock:
        get_conn().execute(sql, params)
        get_conn().commit()


def executemany(sql: str, seq: list[tuple]) -> None:
    with _lock:
        get_conn().executemany(sql, seq)
        get_conn().commit()


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    with _lock:
        return get_conn().execute(sql, params).fetchall()


def query_one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    with _lock:
        return get_conn().execute(sql, params).fetchone()
