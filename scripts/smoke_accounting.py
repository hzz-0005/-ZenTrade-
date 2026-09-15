"""Offline regression for the 2026-08-30 fix round (no network, temp DB):

  1. trades table migration adds entry_fee / sold_shares / partial_realized_pnl
  2. partial-sell accounting: scaled-out trades report real total PnL
  3. add-to-position resets the partial-slice bookkeeping
  4. coast restore after restart (no full-pipeline burn on resume)
  5. macro news prefetch is skipped when macro filler is off
  6. A-share Yahoo suffix helpers (300308 -> .SZ, 600519 -> .SS)
  7. forced initial full-position buy on day 0 (policy, zero LLM calls)
  8. sell from flat degrades to a no-op hold instead of a rejected day
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import tempfile
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from webapp.config import WebappSettings  # noqa: E402
from webapp.core.models import Decision, Fill  # noqa: E402
from webapp.core.portfolio import ExecutionModel, Portfolio  # noqa: E402
from webapp.engine.backtest_engine import BacktestEngine  # noqa: E402
from webapp.engine.data_gateway import DataGateway  # noqa: E402
from webapp.engine.vendors import cn_source  # noqa: E402
from webapp.server.routers.sessions import _fix_a_share_suffix  # noqa: E402
from webapp.store import db  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    print(f"{'OK  ' if ok else 'FAIL'} {name} {detail}")


class FakeAgent:
    def reset_day(self) -> None:
        pass


tmp = Path(tempfile.mkdtemp()) / "verify.db"
db.init_db(tmp)

# ---- 1) migration: start from the pre-fix trades schema, re-init ----
old = sqlite3.connect(str(tmp))
old.executescript("DROP TABLE trades; CREATE TABLE trades ("
                  "id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT NOT NULL, symbol TEXT NOT NULL, "
                  "side TEXT NOT NULL, entry_date TEXT NOT NULL, entry_price REAL NOT NULL, exit_date TEXT, "
                  "exit_price REAL, shares REAL NOT NULL, realized_pnl REAL, fees REAL, return_pct REAL, "
                  "status TEXT NOT NULL DEFAULT 'open', opened_day_index INTEGER, closed_day_index INTEGER);")
old.commit()
old.close()
db.init_db(tmp)  # same connection: executescript no-ops, _migrate ALTERs the columns in
cols = {r[1] for r in db.query("PRAGMA table_info(trades)")}
check("1 trades 迁移补齐三列", {"entry_fee", "sold_shares", "partial_realized_pnl"} <= cols, str(sorted(cols)))

# ---- session row for the engine helpers ----
db.execute(
    "INSERT INTO sessions (id, ticker, canonical_ticker, market, start_date, end_date, "
    "initial_capital, commission_rate, min_commission, slippage_bps, status, cash, created_at) "
    "VALUES ('s1','300308.SS','300308.SZ','cn','2026-01-01','2026-01-10',100000,0.0005,0,0,"
    "'created',100000,'2026-01-01 00:00:00')"
)
eng = BacktestEngine("s1", WebappSettings(), decision_agent=None, skill_library=None)
exec_model = ExecutionModel()
pf = Portfolio(cash=100000.0)


def do(decision: Decision, close: float) -> Fill:
    fill = exec_model.validate_and_fill(decision, pf, close)
    assert fill.action != "rejected", fill
    pf.apply_fill(fill.action, fill.price, fill.shares, fill.fee)
    eng._update_trades("2026-01-05", fill, pf)
    return fill


# ---- 2) buy 50% -> partial sell -> full close ----
fill_buy = do(Decision(action="buy", position_pct=0.5), 100.0)
t = db.query_one("SELECT * FROM trades WHERE session_id='s1' AND status='open'")
check("2a 开仓记录 entry_fee", t["entry_fee"] == fill_buy.fee and t["partial_realized_pnl"] == 0)

fill_p50 = do(Decision(action="sell", position_pct=0.5), 110.0)
t = db.query_one("SELECT * FROM trades WHERE session_id='s1' AND status='open'")
expected_partial = (fill_p50.price - fill_buy.price) * fill_p50.shares - fill_p50.fee
check("2b 部分平仓即时入账",
      abs(t["partial_realized_pnl"] - expected_partial) < 0.01
      and abs(t["sold_shares"] - fill_p50.shares) < 0.001,
      f"partial={t['partial_realized_pnl']:.2f} sold={t['sold_shares']:.0f}")

fill_rest = do(Decision(action="sell", position_pct=1.0), 120.0)
t = db.query_one("SELECT * FROM trades WHERE session_id='s1' AND status='closed'")
expected_total = expected_partial \
    + (fill_rest.price - fill_buy.price) * fill_rest.shares - fill_rest.fee - fill_buy.fee
invested = fill_buy.price * fill_buy.shares + fill_buy.fee
check("2c 清仓汇总含部分平仓盈亏", abs(t["realized_pnl"] - expected_total) < 0.01,
      f"pnl={t['realized_pnl']:.2f} (expect {expected_total:.2f})")
check("2d 收益率按金额口径", abs(t["return_pct"] - expected_total / invested * 100) < 0.01,
      f"ret={t['return_pct']:.3f}%")

# ---- 3) add-to-position resets partial bookkeeping ----
pf = Portfolio(cash=100000.0)
do(Decision(action="buy", position_pct=0.3), 100.0)
do(Decision(action="sell", position_pct=0.5), 105.0)
do(Decision(action="buy", position_pct=0.2), 110.0)
t = db.query_one("SELECT * FROM trades WHERE session_id='s1' AND status='open' ORDER BY id DESC LIMIT 1")
check("3 加仓重置部分平仓簿记", t["partial_realized_pnl"] == 0 and t["sold_shares"] == 0
      and t["entry_fee"] > 0, f"shares={t['shares']:.0f}")

# ---- 4) coast restore ----
decision = Decision(action="buy", position_pct=0.25, confidence=0.7, reasoning="r",
                    recheck_days=3, recheck_upper=108.0, recheck_lower=95.0)
db.execute(
    "INSERT INTO daily_records (session_id, day_index, date, decision_json, action, "
    "cash_after, shares_after, equity) VALUES ('s1',2,'2026-01-05',?,'buy',50000,454.5,99999)",
    (decision.model_dump_json(),),
)
gw = DataGateway("300308.SZ", "cn")
coast = asyncio.run(eng._restore_coast(gw, 5))
check("4a coast 重建", coast is not None and coast["origin_index"] == 2
      and coast["until_index"] == 5 and coast["lower"] == 95.0,
      str({k: coast[k] for k in ("origin_index", "until_index", "lower", "upper")} if coast else None))
check("4b 有效期已过的决策不再重建",
      asyncio.run(eng._restore_coast(gw, 20)) is None
      or asyncio.run(eng._restore_coast(gw, 20))["until_index"] <= 20)

# ---- 5) macro prefetch skipped when disabled ----
calls = {"notice": 0, "macro": 0}
cn_source.fetch_notices_for_date = lambda s, d: calls.__setitem__("notice", calls["notice"] + 1) or []
cn_source.fetch_macro_news_for_date = lambda d, max_items=12: calls.__setitem__("macro", calls["macro"] + 1) or []
gwo = DataGateway("300308.SZ", "cn", macro_news_enabled=False)
gwo.prefetch_news_archive("2026-01-01", "2026-01-03")
check("5a macro 关闭时不预取 CCTV", calls["macro"] == 0 and calls["notice"] == 3, str(calls))
gwm = DataGateway("300308.SZ", "cn", macro_news_enabled=True)
gwm.prefetch_news_archive("2026-01-01", "2026-01-03")
check("5b macro 开启时才预取", calls["macro"] == 3, str(calls))

# ---- 6) A-share suffix helpers ----
cases = {"300308.SS": "300308.SZ", "300308.SZ": "300308.SZ", "300308": "300308.SZ",
         "600519.SS": "600519.SS", "600519": "600519.SS", "688981.SZ": "688981.SS",
         "000858.SS": "000858.SZ", "0700.HK": "0700.HK", "NVDA": "NVDA"}
bad = {k: _fix_a_share_suffix(k) for k, v in cases.items() if _fix_a_share_suffix(k) != v}
check("6a 会话创建后缀纠正", not bad, str(bad))
check("6b yahoo_symbol 映射",
      cn_source.yahoo_symbol("300308.SS") == "300308.SZ"
      and cn_source.yahoo_symbol("600519.SZ") == "600519.SS")
check("6c 基准指数按前缀", cn_source.index_for_ticker("300308.SS") == "sz399001"
      and cn_source.index_for_ticker("600519.SS") == "sh000001")

# ---- 7) forced initial full-position buy (policy day, zero LLM calls) ----
db.execute(
    "INSERT INTO sessions (id, ticker, canonical_ticker, market, start_date, end_date, "
    "initial_capital, commission_rate, min_commission, slippage_bps, status, cash, created_at, initial_position) "
    "VALUES ('s2','300308.SZ','300308.SZ','cn','2026-01-05','2026-01-10',100000,0.0005,0,0,"
    "'created',100000,'2026-01-01 00:00:00','full')"
)
gw_frame = DataGateway("300308.SZ", "cn")
gw_frame._price_frame = pd.DataFrame({
    "Date": pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07"]),
    "Open": [100.0, 101.0, 102.0], "High": [102.0, 103.0, 104.0],
    "Low": [99.0, 100.0, 101.0], "Close": [101.0, 102.0, 103.0],
    "Volume": [1000, 1000, 1000],
})
eng2 = BacktestEngine("s2", WebappSettings(), decision_agent=FakeAgent(), skill_library=None)
pf2 = Portfolio(cash=100000.0)
from webapp.engine.clock import reset_sim_date, set_sim_date  # noqa: E402
_token = set_sim_date("2026-01-05")  # mirror the loop: close_on() is clock-guarded
try:
    eng2._force_initial_buy(0, "2026-01-05", gw_frame, pf2, exec_model)
finally:
    reset_sim_date(_token)
row0 = db.query_one("SELECT * FROM daily_records WHERE session_id='s2' AND day_index=0")
sess2 = db.query_one("SELECT * FROM sessions WHERE id='s2'")
trade2 = db.query_one("SELECT * FROM trades WHERE session_id='s2'")
check("7a 首日强制满仓买入", row0 is not None and row0["action"] == "buy"
      and row0["llm_calls"] == 0 and row0["executed_shares"] > 0,
      f"shares={row0['executed_shares']:.2f} @ {row0['executed_price']}")
check("7b 账户与流水线状态更新", sess2["current_day_index"] == 1 and sess2["shares"] > 0
      and sess2["cash"] < 100000 and pf2.shares > 0)
check("7c 持仓 trade 开立", trade2 is not None and trade2["status"] == "open"
      and trade2["entry_fee"] > 0)

# ---- 8) sell from flat is a no-op hold, not a rejected day ----
pf_flat = Portfolio(cash=1000.0, shares=0.0)
fill_flat = eng._execute(Decision(action="sell", position_pct=0.5), pf_flat, exec_model, 100.0)
check("8 空仓卖出降级为 no-op hold", fill_flat.action == "hold"
      and fill_flat.reason.startswith("sell ignored"), fill_flat.reason)

# ---- 9) stance-aware price-level labels (graph_agent) ----
from webapp.engine.graph_agent import _first_meaningful_sentence, _key_signals, _levels_view  # noqa: E402

# 9a bearish rating (the 300308 d1 case): PM target 535 / trader stop 570
#    below a ~589 close are a downside objective and a further-reduce trigger
#    — never labeled 目标价/止损价, which read as an inverted pair.
parts_bear, flags_bear = _levels_view("Underweight", 589.0, None, 570.0, 535.0)
check("9a 看空评级按下方语义标注", parts_bear == ["下探目标 535", "减仓触发位 570"] and not flags_bear,
      str(parts_bear))

# 9b bullish rating with a below-close target is contradictory -> dropped+flag
parts_contra, flags_contra = _levels_view("Buy", 589.0, None, 570.0, 535.0)
check("9b 看多评级遇下方目标价被忽略并标记",
      parts_contra == ["止损位 570"] and len(flags_contra) == 1 and "矛盾" in flags_contra[0],
      f"{parts_contra} / {flags_contra}")

# 9c normal bullish levels
parts_bull, flags_bull = _levels_view("Buy", 100.0, None, 90.0, 120.0)
check("9c 看多评级正常标注", parts_bull == ["上望目标 120", "止损位 90"] and not flags_bull,
      str(parts_bull))

# 9d sentence splitter no longer cuts tickers on the ASCII period
sent = _first_meaningful_sentence("300308.SZ（中际旭创）价格站上60日均线。MACD金叉。")
check("9d 分句不再切断股票代码", sent.startswith("300308.SZ") and "60日均线" in sent, sent[:40])

# 9e key_signals lead with the stance-correct price line
ks = _key_signals(
    {"market_report": "300308.SZ（中际旭创）价格站上60日均线。MACD金叉。"},
    "Underweight", parts_bear, 570.0, 606.67)
check("9e key_signals 无倒挂无代码碎片",
      ks[0].startswith("价位：下探目标 535") and "决策区间 [570, 606.67]" in ks[0]
      and not any(s.endswith("300308.") for s in ks), str(ks[:2]))

# ---- 10) factor enrichment: earnings trend, PE anchor, sentiment mix ----
import types  # noqa: E402

fake_ak = types.ModuleType("akshare")


def _fake_fin(symbol, start_year):
    return pd.DataFrame({
        "日期": ["2025-03-31", "2025-06-30", "2025-09-30", "2025-12-31"],
        "摊薄每股收益(元)": [1.2, 2.5, 3.9, 5.1],
        "净资产收益率(%)": [3.1, 6.2, 9.4, 12.0],
        "主营业务收入增长率(%)": [5.0, 6.1, 7.2, 8.3],
        "净利润增长率(%)": [1.0, 2.2, 3.3, 4.4],
        "资产负债率(%)": [20.0, 20.5, 21.0, 21.5],
    })


fake_ak.stock_financial_analysis_indicator = _fake_fin
sys.modules["akshare"] = fake_ak  # fetch_financial_snapshot imports lazily
snap_t = cn_source.fetch_financial_snapshot("300308.SZ", "2026-02-03")
# No-lookahead: the 2025-12-31 annual report is not treated as public until
# ~2026-04-30 (conservative disclosure lag), so at 2026-02-03 only the first
# three quarters may appear. A fourth period here would be future data.
check("10a 盈利趋势由远及近（含披露滞后）",
      snap_t is not None
      and snap_t["盈利趋势(近4期,由远及近)"]
      == "营收增速 5.0→6.1→7.2；净利增速 1.0→2.2→3.3",
      str(snap_t and snap_t.get("盈利趋势(近4期,由远及近)")))

# PE anchor: annualized EPS (报告期 12-31 = full year, factor 1) at close 101
gw_frame._fundamentals = {"报告期": "2025-12-31", "摊薄每股收益(元)": 5.1}
gw_frame._fundamentals_key = "2026-01"
_token_pe = set_sim_date("2026-01-05")  # close_on() is clock-guarded, as in the loop
try:
    snap_pe, _ = gw_frame.fundamentals("2026-01-05")
finally:
    reset_sim_date(_token_pe)
check("10b PE 年化估算锚", snap_pe is not None
      and abs(snap_pe.get("市盈率PE(年化估算)", 0) - round(101.0 / 5.1, 1)) < 0.05,
      str(snap_pe and snap_pe.get("市盈率PE(年化估算)")))

# sentiment mix, cn ratings
gws = DataGateway("300308.SZ", "cn")
gws._reports_raw = [
    {"date": "2026-01-20", "rating": "买入"},
    {"date": "2026-01-25", "rating": "买入"},
    {"date": "2026-02-01", "rating": "增持"},
    {"date": "2025-11-01", "rating": "买入"},  # outside the 60d window
]
summary_cn, _ = gws.sentiment("2026-02-03")
check("10c 机构情绪统计(cn)", summary_cn is not None
      and "3 份研报" in summary_cn and "看多 2" in summary_cn and "偏多" in summary_cn,
      str(summary_cn))

# sentiment mix, us actions (1 up / 1 down / 1 hold -> 分歧)
gwu = DataGateway("NVDA", "us")
gwu._reports_raw = [
    {"date": "2026-02-01", "rating": "上调"},
    {"date": "2026-02-02", "rating": "维持"},
    {"date": "2026-01-15", "rating": "下调"},
]
summary_us, _ = gwu.sentiment("2026-02-03")
check("10d 机构情绪统计(us)", summary_us is not None and "分歧" in summary_us, str(summary_us))

# ---- 11) operation profile for the review loop ----
from webapp.skills.distiller import _operation_profile  # noqa: E402

db.executemany(
    "INSERT OR REPLACE INTO price_frames (session_id, date, open, high, low, close, volume) VALUES (?,?,?,?,?,?,?)",
    [("s2", "2026-01-05", 100, 100, 100, 100, 0), ("s2", "2026-01-12", 110, 110, 110, 110, 0)],
)
sess2_row = db.query_one("SELECT * FROM sessions WHERE id='s2'")
eqs = [100, 101, 102, 101, 100, 99, 100, 88, 76, 78]
acts = ["buy", "hold", "sell", "hold", "sell", "sell", "sell", "hold", "hold", "hold"]
pcts = [1.0, 0, 0.05, 0, 0.05, 0.05, 0.05, 0, 0, 0]
days_fake = [
    {"date": f"2026-01-{5 + i:02d}", "action": acts[i],
     "decision_json": json.dumps({"action": acts[i], "position_pct": pcts[i]}),
     "equity": eqs[i], "cash_after": round(eqs[i] * 0.55, 2)}
    for i in range(10)
]
profile = _operation_profile(sess2_row, days_fake, days_fake)
check("11 操作画像点名坏习惯",
      "碎步减仓" in profile and "重新买入 0 次" in profile
      and "两日累计跌幅" in profile and "买入持有" in profile and "跑输基准" in profile
      and "现金使用" in profile,
      profile[:300])

# 11b idle-cash nudge: soft, non-prescriptive
from webapp.engine.backtest_engine import _cash_idle_note  # noqa: E402
note_hit = _cash_idle_note(0.52, 8)
check("11b 现金闲置提醒", note_hit is not None and "部署计划" in note_hit, str(note_hit))
check("11c 不误报正常情形", _cash_idle_note(0.2, 8) is None and _cash_idle_note(0.5, 3) is None)

print(f"\nRESULT: {'PASS' if all(ok for _, ok, _ in results) else 'FAIL'} "
      f"({sum(ok for _, ok, _ in results)}/{len(results)})")
sys.exit(0 if all(ok for _, ok, _ in results) else 1)
