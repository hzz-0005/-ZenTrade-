"""Throwaway end-to-end smoke test: panel-mode agent over real A-share data.

Verifies the three data gaps that used to be dead:
  1. fundamentals actually reach the prompt (## 基本面 section)
  2. historical news backfill lands (## 新闻与公告 with 公告/宏观 items)
  3. panel system prompt is selected and the final JSON still parses

Runs against a throwaway SQLite file, never the real backtest.db.
"""
from __future__ import annotations

import asyncio
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from webapp.config import WebappSettings, load_settings  # noqa: E402
from webapp.engine.backtest_engine import BacktestEngine  # noqa: E402
from webapp.engine.decision_agent import DecisionAgent  # noqa: E402
from webapp.skills.library import SkillLibrary  # noqa: E402
from webapp.store import db  # noqa: E402

TICKER = "600519.SS"
START, END = "2026-06-24", "2026-06-30"
SESSION_ID = "smoke-panel-001"

REPLY = json.dumps({
    "action": "hold",
    "position_pct": 0.0,
    "confidence": 0.42,
    "reasoning": "【技术面】…【基本面】…【消息面】…【多空焦点】…【操作】…",
    "key_signals": ["技术面：MACD 走平", "基本面：ROE 维持高位"],
    "used_skills": [],
    "recheck_days": 2,
    "recheck_upper": 1500.0,
    "recheck_lower": 1350.0,
}, ensure_ascii=False)


class FakeLLM:
    """Minimal chat-model stand-in; records what it was asked."""

    def __init__(self):
        self.prompts: list[str] = []

    def invoke(self, messages):
        self.prompts.append("\n".join(m for _, m in messages))
        return type("R", (), {"content": REPLY})()

    def with_structured_output(self, schema):
        raise RuntimeError("structured output should not be needed in this smoke test")


def main() -> int:
    # unique file per run: the sandbox blocks unlink(), so never reuse a name
    tmp_db = ROOT / "webapp" / "data" / f"smoke_panel_{int(time.time())}.db"
    db.init_db(tmp_db)

    settings: WebappSettings = load_settings()
    settings.agent_mode = "panel"
    settings.max_llm_calls_per_day = 6  # panel reasoning is longer; leave headroom

    db.execute(
        "INSERT INTO sessions (id, ticker, canonical_ticker, market, start_date, end_date, "
        "initial_capital, commission_rate, min_commission, slippage_bps, status, cash, "
        "shares, avg_cost, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (SESSION_ID, TICKER, TICKER, "cn", START, END,
         1_000_000.0, 0.0003, 5.0, 2.0, "created", 1_000_000.0,
         0.0, 0.0, "2026-08-29 00:00:00"),
    )

    llm = FakeLLM()
    agent = DecisionAgent(llm, settings)
    engine = BacktestEngine(SESSION_ID, settings, agent, SkillLibrary(settings))

    t0 = time.time()
    asyncio.run(engine.run())
    elapsed = time.time() - t0

    session = db.query_one("SELECT * FROM sessions WHERE id=?", (SESSION_ID,))
    print(f"\n=== session status: {session['status']} ({elapsed:.1f}s) ===")
    if session["error"]:
        print("ERROR:", session["error"])
        return 1

    rows = db.query(
        "SELECT day_index, date, action, data_flags, context_json "
        "FROM daily_records WHERE session_id=? ORDER BY day_index",
        (SESSION_ID,),
    )
    print(f"daily_records: {len(rows)}")
    for r in rows:
        ctx = json.loads(r["context_json"] or "{}")
        fund = ctx.get("fundamentals")
        kinds = ctx.get("news_kinds") or {}
        print(
            f"  d{r['day_index']} {r['date']} action={r['action']:<5} "
            f"news={ctx.get('news_count')} {kinds} fund={'None' if fund is None else fund.get('报告期')}"
        )
    print("  flags:", "; ".join(sorted({f for r in rows for f in json.loads(r["data_flags"] or "[]")})))

    prompt = llm.prompts[-1] if llm.prompts else ""
    print("\n=== prompt checks ===")
    for marker in ["分析师小组", "因子权重", "## 盈利与估值", "## 消息面", "## 机构情绪", "多空研究员辩论"]:
        print(f"  {marker:<16} {'OK' if marker in prompt else 'MISSING'}")

    news_section = prompt.split("## 消息面")[-1].split("\n## ")[0]
    print("\n=== 消息面 片段 ===")
    print("\n".join(news_section.strip().splitlines()[:6]) or "(empty)")
    fund_section = prompt.split("## 盈利与估值")[-1].split("\n## ")[0]
    print("\n=== 盈利与估值 片段 ===")
    print("\n".join(fund_section.strip().splitlines()[:8]) or "(empty)")
    senti_section = prompt.split("## 机构情绪")[-1].split("\n## ")[0]
    print("\n=== 机构情绪 片段 ===")
    print("\n".join(senti_section.strip().splitlines()[:3]) or "(empty)")

    ok = ("## 盈利与估值" in prompt) and ("因子权重" in prompt) and len(rows) > 0 and session["status"] == "done"
    print(f"\nRESULT: {'PASS' if ok else 'FAIL'}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
