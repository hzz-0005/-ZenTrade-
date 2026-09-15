"""Health / config meta endpoints."""
from __future__ import annotations

import logging
import threading

from fastapi import APIRouter, HTTPException, Query

from webapp.config import load_settings
from webapp.store import db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api", tags=["meta"])

# ---- stock name resolution ----
# A-share code->name table: pulled once per process (~6s, 5551 rows, few hundred KB),
# then served from memory. Thread-safe via lock; failures reset to retry next call.
_A_SHARE_NAMES: dict[str, str] | None = None
_NAME_LOCK = threading.Lock()
_NON_CN_CACHE: dict[str, str] = {}


def _a_share_names() -> dict[str, str]:
    global _A_SHARE_NAMES
    if _A_SHARE_NAMES is not None:
        return _A_SHARE_NAMES
    with _NAME_LOCK:
        if _A_SHARE_NAMES is not None:
            return _A_SHARE_NAMES
        import akshare as ak
        df = ak.stock_info_a_code_name()
        _A_SHARE_NAMES = {
            str(r["code"]): str(r["name"]).replace(" ", "").replace("\u3000", "")
            for _, r in df.iterrows()
        }
        logger.info("A-share name table loaded: %d entries", len(_A_SHARE_NAMES))
        return _A_SHARE_NAMES


@router.get("/meta/stock-name")
def stock_name(ticker: str = Query(..., min_length=2, max_length=16)):
    """Resolve a ticker to its display name (A-share via akshare, others via yfinance)."""
    from webapp.engine.data_gateway import detect_market

    t = ticker.strip().upper()
    market = detect_market(t)
    if market == "cn":
        code = t.split(".")[0]
        try:
            name = _a_share_names().get(code)
        except Exception as exc:
            logger.warning("A-share name table load failed: %s", exc)
            raise HTTPException(503, f"股票名称表加载失败：{exc}") from exc
        if name:
            return {"ticker": t, "name": name, "market": market}
        raise HTTPException(404, f"未找到 A 股代码 {t}（请检查代码，示例：600519.SS / 000858.SZ）")

    if t in _NON_CN_CACHE:
        return {"ticker": t, "name": _NON_CN_CACHE[t], "market": market}
    try:
        import yfinance as yf
        info = yf.Ticker(t).info or {}
        name = info.get("shortName") or info.get("longName")
    except Exception as exc:
        raise HTTPException(503, f"名称查询失败：{exc}") from exc
    if not name:
        raise HTTPException(404, f"未找到代码 {t} 的名称")
    _NON_CN_CACHE[t] = str(name)
    return {"ticker": t, "name": str(name), "market": market}


@router.get("/health")
def health():
    db_ok = True
    try:
        db.query_one("SELECT 1")
    except Exception:
        db_ok = False
    settings = load_settings()
    return {"status": "ok" if db_ok else "db-error", "db_ok": db_ok,
            "llm_provider": settings.llm_provider, "model": settings.model}


@router.get("/config")
def config():
    settings = load_settings()
    return {
        "default_commission_rate": settings.default_commission_rate,
        "max_session_days": settings.max_session_days,
        "max_concurrent_sessions": settings.max_concurrent_sessions,
        "skill_categories": list(settings.skill_categories),
        "skill_top_n": settings.skill_top_n,
    }
