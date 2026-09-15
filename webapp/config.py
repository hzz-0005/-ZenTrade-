"""Webapp settings, derived from the repo's DEFAULT_CONFIG (env-overridden).

LLM settings (provider/model/backend URL) come straight from
tradingagents.default_config.DEFAULT_CONFIG so the project's .env
(TRADINGAGENTS_* vars) is honored without duplication.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

WEBAPP_ROOT = Path(__file__).resolve().parent
REPO_ROOT = WEBAPP_ROOT.parent
DATA_DIR = WEBAPP_ROOT / "data"
DB_PATH = DATA_DIR / "backtest.db"
PROMPTS_DIR = WEBAPP_ROOT / "prompts"


@dataclass
class WebappSettings:
    # LLM (from DEFAULT_CONFIG / .env)
    llm_provider: str = "glm-cn"
    model: str = "glm-5.3-flash"
    backend_url: str | None = None

    # Execution model
    default_commission_rate: float = 0.0005  # 0.05% per side
    min_commission: float = 0.0              # flat floor per fill
    slippage_bps: float = 0.0                # basis points applied adversely

    # Engine guardrails
    # Cheap single/panel LLM calls per decision day. A normal panel day uses
    # ~6-10 calls (analyst team + debate + trader + triage); 40 is a hard
    # runaway guard, not a target — a buggy retry loop used to be able to burn
    # up to 1000 calls/day before failing. Lower via env if you want a tighter
    # ceiling (WEBAPP_MAX_LLM_CALLS_PER_DAY).
    max_llm_calls_per_day: int = 40
    # Full-pipeline (graph) runs per day. A single graph run fans out ~9 LLM
    # nodes, so it is budgeted separately from the cheap single/panel calls and
    # the hybrid triage. The hybrid scheduler escalates at most a couple of
    # times per day (news / hard price break / stand-pat streak), so 5 is a hard
    # runaway guard while still leaving headroom for a legitimate re-decide or a
    # failed-run retry. (Was 1000 — effectively uncapped.)
    max_graph_runs_per_day: int = 5
    adaptive_fast_streak_limit: int = 5
    adaptive_max_calls_per_day: int = 4
    max_session_days: int = 250
    max_concurrent_sessions: int = 3
    lookback_calendar_days: int = 150        # warmup before start (SMA50 needs ~75 calendar days)

    # Skill library
    skill_top_n: int = 8
    skill_categories: tuple[str, ...] = (
        "trend", "mean_reversion", "risk_control", "position_sizing",
        "sentiment", "timing", "regime", "execution",
    )

    # Coasting (band decision mode): fallback band when the model omits one
    recheck_band_pct: float = 0.03

    # Hard risk-control layer (enforced by the engine, independent of the LLM):
    #   - max_drawdown_stop_pct: when equity drawdown from its running peak
    #     exceeds this, the engine force-reduces the position to
    #     drawdown_reduce_to_pct (0 = full exit) regardless of the model's view.
    risk_budget_pct: float = 0.02  # DEPRECATED: sizing honours the PM's target_position_pct directly; risk control is the hard stop/TP/drawdown layer below.
    max_drawdown_stop_pct: float = 0.20
    drawdown_reduce_to_pct: float = 0.30
    # Engine-side fallback stop-loss. When the model's free-text stop loss is
    # unparsable/absent (the PM or trader simply didn't quote one), the hard
    # risk layer still needs a deterministic stop — otherwise a position rides
    # all the way down to the -max_drawdown_stop_pct circuit-breaker with no
    # earlier exit. Anchored to the position's average cost (not today's close,
    # which would ratchet the stop each day): stop = avg_cost * (1 - pct).
    # None disables the fallback (revert to model-only stops).
    fallback_stop_loss_pct: float | None = 0.08
    # 分批兑现上限：非清仓意图（Underweight/Hold）的单日卖出不超过当前持仓的
    # 这一比例。上涨趋势里"一下砍掉 70%"太激进；目标仓位保持不变，剩余调仓
    # 留待后续再决策日继续。Sell 评级（明确离场）与硬风控层（止损/止盈/回撤
    # 熔断）不受此限。1.0 = 不限制。
    max_daily_sell_frac: float = 0.5

    # Decision agent mode:
    #   "panel"  — multi-role analyst team in one call (tech/fundamental/news
    #              analysts + bull-bear debate + trader), richer reasoning
    #   "single" — plain single-trader prompt (cheaper, less structured)
    agent_mode: str = "panel"

    # How the full multi-agent graph (sessions.use_full_graph) is scheduled:
    #   "on_news" — daily free news triage; escalate to the full pipeline only
    #               when news could move the price band, or price breaks the
    #               band hard. Keeps the stand-pat days at ~0 LLM cost.
    #   "always"  — run the whole pipeline on every decision day (thorough,
    #               but minutes per day).
    graph_trigger: str = "on_news"
    # Price break (in units of the band width, beyond an edge) that alone
    # justifies a fresh full-pipeline run even with no news.
    escalate_breach_ratio: float = 0.25
    # 连续"维持原判断"（stand-pat）天数上限：到达后强制跑一次完整流水线做
    # 全面复核。stand-pat 是零 LLM 成本的捷径，但无限自我续期等于几十天不做
    # 评估（实测：NVDA 会话连续 20+ 天链式 hold；600396 会话 +56% 浮盈无人
    # 复核，坐电梯回 -5%）。
    max_standpat_streak: int = 5

    # Graph execution mode for the full pipeline. "sequential" is the safe
    # default: analysts run one at a time, so each finishes its tool loop
    # "sequential" runs the four analysts one after another; "parallel" fans
    # them out in one super-step. Parallel was previously disabled because the
    # analysts shared a single `messages` list and LangGraph's ToolNode executed
    # the tool_calls of whatever AIMessage was last, orphaning concurrent
    # tool_calls. That is now fixed: each analyst has its own message channel
    # (market_messages / social_messages / news_messages / fundamentals_messages)
    # and its ToolNode is pointed at it via messages_key, so parallel is safe
    # and is the default for a ~4x speedup on the analyst stage.
    graph_pipeline_mode: str = "parallel"
    # Dump the message sequence (incl. orphaned tool_call ids) when the graph
    # fails. Off unless you are debugging a pipeline failure.
    graph_debug: bool = False

    # CCTV 新闻联播 headlines used as low-priority news filler. Off by default:
    # the content is near-useless for single-name decisions AND it is political,
    # which trips GLM's content filter (error 1301) and aborts the session.
    macro_news_enabled: bool = False

    # Paths
    db_path: Path = field(default_factory=lambda: DB_PATH)
    prompts_dir: Path = field(default_factory=lambda: PROMPTS_DIR)

    def __post_init__(self) -> None:
        for name in (
            "default_commission_rate", "min_commission", "slippage_bps",
        ):
            if getattr(self, name) < 0:
                raise ValueError(f"{name} must be non-negative")
        for name in (
            "max_llm_calls_per_day", "max_graph_runs_per_day",
            "adaptive_fast_streak_limit", "adaptive_max_calls_per_day",
            "max_session_days", "max_concurrent_sessions", "skill_top_n",
            "max_standpat_streak",
        ):
            if getattr(self, name) < 1:
                raise ValueError(f"{name} must be at least 1")
        for name in (
            "risk_budget_pct", "max_drawdown_stop_pct",
            "drawdown_reduce_to_pct", "max_daily_sell_frac",
            "recheck_band_pct",
        ):
            value = getattr(self, name)
            if not 0 <= value <= 1:
                raise ValueError(f"{name} must be between 0 and 1")
        if self.fallback_stop_loss_pct is not None and not 0 < self.fallback_stop_loss_pct < 1:
            raise ValueError("fallback_stop_loss_pct must be between 0 and 1")
        if self.agent_mode not in {"single", "panel"}:
            raise ValueError("agent_mode must be single or panel")
        if self.graph_trigger not in {"on_news", "always"}:
            raise ValueError("graph_trigger must be on_news or always")
        if self.graph_pipeline_mode not in {"sequential", "parallel"}:
            raise ValueError("graph_pipeline_mode must be sequential or parallel")


def load_settings() -> WebappSettings:
    import os

    from tradingagents.default_config import DEFAULT_CONFIG

    return WebappSettings(
        llm_provider=str(DEFAULT_CONFIG.get("llm_provider", "glm-cn")).lower(),
        model=str(DEFAULT_CONFIG.get("quick_think_llm", "glm-5.3-flash")),
        backend_url=DEFAULT_CONFIG.get("backend_url"),
        agent_mode=os.environ.get("WEBAPP_AGENT_MODE", "panel").strip().lower() or "panel",
        graph_trigger=(
            os.environ.get("WEBAPP_GRAPH_TRIGGER", "on_news").strip().lower() or "on_news"
        ),
        macro_news_enabled=_env_bool("WEBAPP_MACRO_NEWS", default=False),
        graph_pipeline_mode=(
            os.environ.get("WEBAPP_GRAPH_PIPELINE_MODE", "parallel").strip().lower()
            or "parallel"
        ),
        graph_debug=_env_bool("WEBAPP_GRAPH_DEBUG", default=False),
        risk_budget_pct=_env_float("WEBAPP_RISK_BUDGET", default=0.02),
        max_drawdown_stop_pct=_env_float("WEBAPP_MAX_DRAWDOWN_STOP", default=0.20),
        drawdown_reduce_to_pct=_env_float("WEBAPP_DRAWDOWN_REDUCE_TO", default=0.30),
        max_daily_sell_frac=_env_float("WEBAPP_MAX_DAILY_SELL_FRAC", default=0.5),
        max_standpat_streak=_env_int("WEBAPP_MAX_STANDPAT_STREAK", default=5),
        max_llm_calls_per_day=_env_int("WEBAPP_MAX_LLM_CALLS_PER_DAY", default=40),
        max_graph_runs_per_day=_env_int("WEBAPP_MAX_GRAPH_RUNS_PER_DAY", default=5),
        adaptive_fast_streak_limit=_env_int(
            "WEBAPP_ADAPTIVE_FAST_STREAK_LIMIT", default=5
        ),
        adaptive_max_calls_per_day=_env_int(
            "WEBAPP_ADAPTIVE_MAX_CALLS_PER_DAY", default=4
        ),
    )


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _env_bool(name: str, default: bool) -> bool:
    import os

    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")
