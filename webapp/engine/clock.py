"""Simulated clock — the no-lookahead keystone.

Every data access in a backtest day runs with sim_date set to T; the
data gateway hard-drops anything dated after it. Fail-loud when unset so
a stray live fetch cannot silently slip into a backtest prompt.
"""
from __future__ import annotations

from contextvars import ContextVar

sim_date: ContextVar[str | None] = ContextVar("sim_date", default=None)


def set_sim_date(day: str):
    """Set the simulated 'now' for the current context; returns a reset token."""
    return sim_date.set(day)


def reset_sim_date(token) -> None:
    sim_date.reset(token)


def get_sim_date() -> str:
    day = sim_date.get()
    if day is None:
        raise RuntimeError(
            "sim_date is not set: live data fetch attempted outside a backtest day"
        )
    return day


def is_active() -> bool:
    return sim_date.get() is not None


def clamp_date(day: str) -> str:
    """Clamp a date string to the simulated now (min)."""
    current = sim_date.get()
    return min(day, current) if current else day
