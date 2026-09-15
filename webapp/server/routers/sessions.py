"""Session REST endpoints."""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query

from webapp.config import load_settings
from webapp.core.models import SessionSpec
from webapp.engine.backtest_engine import SessionState
from webapp.engine.data_gateway import detect_market
from webapp.server.session_manager import SessionManager
from webapp.store import db

router = APIRouter(prefix="/api/sessions", tags=["sessions"])
manager: SessionManager | None = None  # wired by app factory


def get_manager() -> SessionManager:
    global manager
    if manager is None:
        manager = SessionManager(load_settings())
    return manager


def _session_dict(row) -> dict:
    d = dict(row)
    d["trading_days"] = json.loads(d.get("trading_days") or "[]")
    return d


def _fix_a_share_suffix(ticker: str) -> str:
    """Correct a wrong/missing A-share exchange suffix from the numeric prefix.

    300308 is Shenzhen ChiNext, but users often type .SS — and Yahoo only
    knows 300308.SZ, so the yfinance fallback would 404. 60x/68x are
    Shanghai, 00x/30x are Shenzhen; anything else (funds, bonds, HK/US
    tickers) passes through untouched.
    """
    t = ticker.strip().upper()
    code, _, suffix = t.partition(".")
    if not (code.isdigit() and len(code) == 6 and suffix in ("", "SS", "SZ")):
        return t
    if code.startswith(("60", "68")):
        return f"{code}.SS"
    if code.startswith(("00", "30")):
        return f"{code}.SZ"
    return t


@router.post("", status_code=201)
async def create_session(req: SessionSpec):
    # async (not def): SessionManager.start() calls asyncio.create_task(),
    # which requires a running event loop — sync endpoints run in a threadpool
    # without one.
    settings = load_settings()
    if req.start_date >= req.end_date:
        raise HTTPException(400, "start_date 必须早于 end_date")
    if req.end_date > datetime.now().strftime("%Y-%m-%d"):
        raise HTTPException(400, "end_date 不能是未来日期（回测需要历史数据）")
    if (datetime.strptime(req.end_date, "%Y-%m-%d") - datetime.strptime(req.start_date, "%Y-%m-%d")).days > 400:
        raise HTTPException(400, "日期区间过长（最多约 400 个自然日）")

    from tradingagents.dataflows.symbol_utils import normalize_symbol

    try:
        canonical = normalize_symbol(req.ticker)
    except Exception:
        canonical = req.ticker.strip().upper()
    canonical = _fix_a_share_suffix(canonical)
    if not canonical or not canonical.replace(".", "").replace("-", "").replace("^", "").isascii():
        raise HTTPException(400, "无效的股票代码（不支持中文名称），A股示例：600519.SS / 000858.SZ；港股：0700.HK；美股：NVDA")

    session_id = uuid.uuid4().hex
    commission = req.commission_rate if req.commission_rate is not None else settings.default_commission_rate
    architecture = req.decision_architecture
    if "decision_architecture" not in req.model_fields_set and req.use_full_graph is not None:
        architecture = "classic_graph" if req.use_full_graph else "fast"
    db.execute(
        "INSERT INTO sessions (id, ticker, canonical_ticker, market, start_date, end_date, "
        "initial_capital, commission_rate, min_commission, slippage_bps, status, cash, created_at, "
        "use_full_graph, decision_architecture, decision_mode, initial_position, execution_timing) "
        "VALUES (?,?,?,?,?,?,?,?,?,?, 'created', ?, ?, ?, ?, 'band', ?, 'next_open')",
        (session_id, req.ticker, canonical, detect_market(canonical), req.start_date, req.end_date,
         req.initial_capital, commission, settings.min_commission, settings.slippage_bps,
         req.initial_capital, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"),
         1 if architecture == "classic_graph" else 0, architecture, req.initial_position),
    )
    try:
        get_manager().start(session_id)
    except RuntimeError as exc:
        db.execute("DELETE FROM sessions WHERE id=?", (session_id,))
        raise HTTPException(429, str(exc)) from exc
    return _session_dict(db.query_one("SELECT * FROM sessions WHERE id=?", (session_id,)))


@router.get("")
def list_sessions(status: str | None = Query(None), limit: int = Query(50, ge=1, le=200)):
    if status:
        rows = db.query("SELECT * FROM sessions WHERE status=? ORDER BY created_at DESC LIMIT ?", (status, limit))
    else:
        rows = db.query("SELECT * FROM sessions ORDER BY created_at DESC LIMIT ?", (limit,))
    return {"sessions": [_session_dict(r) for r in rows]}


@router.get("/{session_id}")
def get_session(session_id: str):
    row = db.query_one("SELECT * FROM sessions WHERE id=?", (session_id,))
    if not row:
        raise HTTPException(404, "会话不存在")
    return _session_dict(row)


@router.post("/{session_id}/pause")
def pause_session(session_id: str):
    _require(session_id, running_only=False)
    get_manager().pause(session_id)
    db.execute("UPDATE sessions SET status=? WHERE id=?", (SessionState.PAUSED, session_id))
    return {"status": "paused"}


@router.post("/{session_id}/resume")
def resume_session(session_id: str):
    _require(session_id, running_only=False)
    manager = get_manager()
    if session_id in manager.engines:
        manager.resume(session_id)  # just unblock a paused loop
    else:
        # No live loop: the session failed, was stopped, or the server
        # restarted. Start a fresh engine — it resumes from current_day_index
        # so completed days are not recomputed.
        try:
            manager.start(session_id)
        except RuntimeError as exc:
            raise HTTPException(429, str(exc)) from exc
    db.execute(
        "UPDATE sessions SET status=?, error=NULL WHERE id=?",
        (SessionState.RUNNING, session_id),
    )
    return {"status": "running"}


@router.post("/{session_id}/stop")
def stop_session(session_id: str):
    _require(session_id, running_only=False)
    get_manager().stop(session_id)
    db.execute(
        "UPDATE sessions SET status=?, finished_at=datetime('now') WHERE id=?",
        (SessionState.STOPPED, session_id),
    )
    return {"status": "stopped"}


@router.delete("/{session_id}", status_code=204)
def delete_session(session_id: str):
    get_manager().delete(session_id)
    db.execute("DELETE FROM sessions WHERE id=?", (session_id,))


@router.get("/{session_id}/chart")
def chart(session_id: str):
    _require(session_id, running_only=False)
    session = db.query_one("SELECT * FROM sessions WHERE id=?", (session_id,))
    days = db.query(
        "SELECT date, decision_date, execution_date, valuation_date, execution_status, action, executed_price, executed_shares, equity, cash_after "
        "FROM daily_records WHERE session_id=? ORDER BY day_index", (session_id,)
    )
    kline_source = json.loads(session["trading_days"])
    markers = [
        {"date": d["execution_date"] or d["date"],
         "decision_date": d["decision_date"] or d["date"],
         "execution_date": d["execution_date"] or d["date"],
         "execution_status": d["execution_status"], "action": d["action"],
         "price": d["executed_price"], "shares": d["executed_shares"]}
        for d in days if d["action"] in ("buy", "sell", "rejected")
    ]
    # A next-open decision made at T is valued after the T+1 close.  Multiple
    # audit records can legitimately share that valuation session (for
    # example, yesterday's fill and today's no-order coast), but a line chart
    # needs exactly one point per market date.  Keep the later ledger row: it
    # reflects the end-of-session portfolio state without losing either audit
    # record from the daily log endpoint.
    by_valuation_date = {}
    for d in days:
        valuation_date = d["valuation_date"] or d["execution_date"] or d["date"]
        by_valuation_date[valuation_date] = d
    equity = [
        {"date": valuation_date, "equity": d["equity"], "cash": d["cash_after"]}
        for valuation_date, d in sorted(by_valuation_date.items())
    ]
    return {"kline_dates": kline_source if kline_source else [d["date"] for d in days],
            "markers": markers, "equity": equity,
            "initial_capital": session["initial_capital"],
            "session_start": session["start_date"], "session_end": session["end_date"]}


@router.get("/{session_id}/kline")
def kline(session_id: str):
    """Daily OHLCV for the chart candlestick overlay."""
    _require(session_id, running_only=False)
    rows = db.query(
        "SELECT date, open, high, low, close, volume FROM price_frames "
        "WHERE session_id=? ORDER BY date", (session_id,),
    )
    return {"kline": [dict(r) for r in rows]}


@router.get("/{session_id}/days")
def list_days(session_id: str, offset: int = Query(0, ge=0),
              limit: int = Query(50, ge=1, le=200)):
    _require(session_id, running_only=False)
    rows = db.query(
        "SELECT day_index, date, decision_date, execution_date, valuation_date, execution_status, decision_json, action, requested_pct, executed_shares, executed_price, fee, "
        "cash_after, shares_after, equity, data_flags, skills_injected, llm_calls, latency_ms "
        "FROM daily_records WHERE session_id=? ORDER BY day_index LIMIT ? OFFSET ?",
        (session_id, limit, offset),
    )
    total = db.query_one("SELECT COUNT(*) c FROM daily_records WHERE session_id=?", (session_id,))["c"]
    return {"total": total, "days": [dict(r) for r in rows]}


@router.get("/{session_id}/days/{day_index}")
def day_detail(session_id: str, day_index: int):
    _require(session_id, running_only=False)
    row = db.query_one(
        "SELECT * FROM daily_records WHERE session_id=? AND day_index=?", (session_id, day_index)
    )
    if not row:
        raise HTTPException(404, "该交易日记录不存在")
    return dict(row)


@router.get("/{session_id}/trades")
def trades(session_id: str):
    _require(session_id, running_only=False)
    rows = db.query("SELECT * FROM trades WHERE session_id=? ORDER BY id", (session_id,))
    return {"trades": [dict(r) for r in rows]}


@router.get("/{session_id}/review")
def review(session_id: str):
    _require(session_id, running_only=False)
    row = db.query_one("SELECT * FROM session_reviews WHERE session_id=?", (session_id,))
    if not row:
        raise HTTPException(404, "复盘尚未生成（会话需达到 done 状态）")
    return dict(row)


def _require(session_id: str, running_only: bool) -> None:
    row = db.query_one("SELECT status FROM sessions WHERE id=?", (session_id,))
    if not row:
        raise HTTPException(404, "会话不存在")
