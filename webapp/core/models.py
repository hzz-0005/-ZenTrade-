"""Pydantic domain models for the backtest engine."""
from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field, field_validator


class Decision(BaseModel):
    """Structured output contract for the daily decision LLM call."""

    action: Literal["buy", "sell", "hold"]
    # buy: fraction of available cash to spend; sell: fraction of held shares to sell
    position_pct: float = Field(ge=0.0, le=1.0, default=0.0)
    confidence: float = Field(ge=0.0, le=1.0, default=0.5)
    reasoning: str = ""
    key_signals: list[str] = Field(default_factory=list)
    used_skills: list[str] = Field(default_factory=list)  # ids of injected skills applied
    # --- coasting (band mode): how long this decision stands without re-querying the LLM ---
    recheck_days: int = Field(default=1, ge=1, le=10)
    # price band; if close leaves [lower, upper] before recheck_days, re-decide immediately.
    recheck_upper: float | None = Field(default=None, gt=0)
    recheck_lower: float | None = Field(default=None, gt=0)
    # --- executable risk levels (consumed by the hard risk-control layer) ---
    stop_loss: float | None = Field(default=None, gt=0)
    take_profit: float | None = Field(default=None, gt=0)
    target_position_pct: float | None = Field(default=None, ge=0.0, le=1.0)


class Fill(BaseModel):
    """Validated, executable action resolved by the ExecutionModel."""

    action: Literal["buy", "sell", "hold", "rejected"]
    requested_pct: float = 0.0
    shares: float = 0.0
    price: float = 0.0
    fee: float = 0.0
    reason: str = ""


DecisionArchitecture = Literal["adaptive", "classic_graph", "fast"]


class SessionSpec(BaseModel):
    ticker: str = Field(min_length=1, max_length=32)
    start_date: str
    end_date: str
    initial_capital: float = Field(gt=0)
    commission_rate: float | None = Field(default=None, ge=0.0, le=0.1)
    decision_architecture: DecisionArchitecture = "adaptive"
    # Compatibility input for clients created before decision_architecture.
    # New clients should omit it.
    use_full_graph: bool | None = None
    # "full"  — the engine force-buys a full position at day-0 close (policy,
    #           zero LLM calls); from day 1 the agent manages the holding.
    #           Default because an LLM asked "enter or stay flat?" from cash
    #           almost always answers hold, and the session degenerates into
    #           dead cash (observed: 49 consecutive holds, position 0.0).
    # "agent" — the agent decides the entry itself (legacy behavior).
    initial_position: Literal["full", "agent"] = "full"

    @field_validator("ticker")
    @classmethod
    def _clean_ticker(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("ticker cannot be blank")
        return value

    @field_validator("start_date", "end_date")
    @classmethod
    def _iso_date(cls, value: str) -> str:
        from datetime import date

        try:
            return date.fromisoformat(value).isoformat()
        except (TypeError, ValueError) as exc:
            raise ValueError("date must use YYYY-MM-DD") from exc
