"""Loads markdown prompt templates from webapp/prompts/."""
from __future__ import annotations

from pathlib import Path

from webapp.config import PROMPTS_DIR

_cache: dict[str, str] = {}


def _load(name: str) -> str:
    if name not in _cache:
        path: Path = PROMPTS_DIR / name
        _cache[name] = path.read_text(encoding="utf-8")
    return _cache[name]


def decision_system() -> str:
    return _load("decision_system.md")


def decision_system_panel() -> str:
    """Multi-role analyst-panel variant of the decision system prompt."""
    return _load("decision_system_panel.md")


def review_system() -> str:
    return _load("review.md")


def dedupe_system() -> str:
    return _load("dedupe.md")
