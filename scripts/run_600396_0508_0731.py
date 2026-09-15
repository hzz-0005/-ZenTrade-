"""Live verification run: 600396.SS, 2026-07-01 ~ 2026-07-23 (17 trading days), full graph mode.

Fresh window chosen by the user to exercise re-entry behaviour after a flat/exit
stretch on a different part of the series (07-01 ~ 07-23).

Post-fix expectations:
  - every decision reasoning starts with 【仓位基准】 (real weight/cash)
  - flat days can only be 维持空仓 / 回补建仓 — sell narratives degrade to hold
    with a 【仓位核对】 prefix and a "当前空仓" flag
  - non-Sell single-day sells capped at 50% (分批兑现 flag)
  - plan continuity: prior stop/take-profit/target fed back into the context

Usage: ./.venv/Scripts/python.exe scripts/run_600396_0508_0731.py
Prints progress lines; final section dumps the decision ledger.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from webapp.config import load_settings  # noqa: E402
from webapp.engine.data_gateway import detect_market  # noqa: E402
from webapp.server.session_manager import SessionManager  # noqa: E402
from webapp.store import db  # noqa: E402

TICKER = "600396.SS"
START, END = "2026-07-01", "2026-07-23"  # 17 trading days (07-01 ~ 07-23)
CAPITAL = 1_000_000.0
COMMISSION = 0.0005


async def main() -> int:
    settings = load_settings()
    # Cost/time controls: keep the hybrid gate (on_news + price-band) so stand-pat
    # days cost ~0 LLM calls, and raise the stand-pat cap so the full 9-node
    # pipeline is NOT re-run every day. The full graph still fires on real
    # triggers (news escalation / hard price break / streak cap), which is enough
    # to exercise re-entry & account-state behaviour on a 17-day window.
    settings.graph_trigger = "on_news"
    settings.max_standpat_streak = 10  # coast up to 10 stand-pat days between full-pipeline runs

    session_id = uuid.uuid4().hex
    db.execute(
        "INSERT INTO sessions (id, ticker, canonical_ticker, market, start_date, end_date, "
        "initial_capital, commission_rate, min_commission, slippage_bps, status, cash, created_at, "
        "use_full_graph, decision_mode, initial_position) "
        "VALUES (?,?,?,?,?,?,?,?,?,?, 'created', ?, ?, ?, 'band', ?)",
        (session_id, TICKER, TICKER, detect_market(TICKER), START, END,
         CAPITAL, COMMISSION, settings.min_commission, settings.slippage_bps,
         CAPITAL, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
         1, "full"),
    )
    print(f"session {session_id} created: {TICKER} {START}~{END} "
          f"graph={settings.graph_trigger} standpat_cap={settings.max_standpat_streak}")

    manager = SessionManager(settings)
    manager.start(session_id)

    last_done = -1
    while True:
        row = db.query_one("SELECT status, error, trading_days FROM sessions WHERE id=?",
                           (session_id,))
        status = row["status"]
        days = json.loads(row["trading_days"] or "[]")
        done = db.query_one("SELECT COUNT(*) c FROM daily_records WHERE session_id=?",
                            (session_id,))["c"]
        if done != last_done:
            last_done = done
            print(f"[{time.strftime('%H:%M:%S')}] {done}/{len(days) or '?'} days, status={status}",
                  flush=True)
        if status in ("done", "failed", "stopped"):
            break
        await asyncio.sleep(15)

    print(f"\nstatus={row['status']} error={row['error']}")
    print(f"session_id={session_id}")

    rows = db.query(
        "SELECT date, action, requested_pct, data_flags, decision_json FROM daily_records "
        "WHERE session_id=? ORDER BY date", (session_id,))
    print(f"\n=== ledger ({len(rows)} days) ===")
    for r in rows:
        d = json.loads(r["decision_json"] or "{}")
        reasoning = (d.get("reasoning") or "")[:110].replace("\n", " ")
        flags = (r["data_flags"] or "")[:120]
        print(f"{r['date']} {r['action']:<5} pct={r['requested_pct']!s:<5} | {reasoning}")
        if flags:
            print(f"{'':<12}flags: {flags}")
    return 0 if row["status"] in ("done", "finished") else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
