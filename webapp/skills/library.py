"""Skill library: categorized lessons distilled from session reviews.

Temporal invariant: select_for_day only returns skills with
created_at <= sim_date, so a session can never be influenced by lessons
learned from its own future or from sessions finished after day T.
"""
from __future__ import annotations

import json
import uuid
from datetime import datetime, timezone

from webapp.config import WebappSettings
from webapp.store import db

_CATEGORY_WEIGHTS = {
    "trend": 1.0, "mean_reversion": 1.0, "risk_control": 0.9,
    "position_sizing": 0.9, "sentiment": 0.7, "timing": 0.8,
    "regime": 0.8, "execution": 0.6,
}
MAX_SKILL_STATEMENT_CHARS = 240


def _normalize_statement(statement: str) -> str:
    value = " ".join(str(statement or "").replace("\x00", "").split())
    if not value:
        raise ValueError("skill statement cannot be blank")
    if len(value) > MAX_SKILL_STATEMENT_CHARS:
        raise ValueError(
            f"skill statement exceeds {MAX_SKILL_STATEMENT_CHARS} characters"
        )
    return value


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


class SkillLibrary:
    def __init__(self, settings: WebappSettings):
        self.settings = settings

    # ---- injection ----

    def select_for_day(self, sim_date: str, top_n: int | None = None) -> list[dict]:
        """Top-N enabled skills whose created_at <= sim_date, scored for relevance."""
        rows = db.query(
            "SELECT * FROM skills WHERE enabled=1 AND merged_into IS NULL "
            "AND substr(created_at,1,10) <= ?",
            (sim_date,),
        )
        if not rows:
            return []
        limit = top_n or self.settings.skill_top_n

        def score(r) -> float:
            # recency: linear decay over ~180 days
            created = datetime.strptime(r["created_at"][:10], "%Y-%m-%d")
            sim = datetime.strptime(sim_date, "%Y-%m-%d")
            age_days = max((sim - created).days, 0)
            recency = max(1.0 - age_days / 180.0, 0.0)
            return 0.5 * (r["success_rate"] or 0.5) + 0.3 * recency + 0.2 * _CATEGORY_WEIGHTS.get(r["category"], 0.5)

        # One lesson per category avoids prompt conflicts such as two timing
        # rules that respectively insist on immediate entry and waiting.  Rank
        # first, then retain the best representative of each category.
        ranked = []
        seen_categories: set[str] = set()
        for row in sorted(rows, key=score, reverse=True):
            category = str(row["category"])
            if category in seen_categories:
                continue
            seen_categories.add(category)
            ranked.append(row)
            if len(ranked) >= limit:
                break
        return [
            {
                "id": r["id"],
                "category": r["category"],
                "statement": r["statement"],
            }
            for r in ranked
        ]

    # ---- CRUD (used by distiller and the REST layer) ----

    def create(self, category: str, statement: str, source_session: str | None,
               source_ticker: str | None, source_market: str | None,
               performance_evidence: dict | None = None,
               effective_date: str | None = None) -> dict:
        if category not in self.settings.skill_categories:
            raise ValueError(f"unsupported skill category: {category}")
        statement = _normalize_statement(statement)
        if self._find_duplicate(category, statement):
            return None
        skill_id = uuid.uuid4().hex
        # created_at doubles as the "effective from" date for select_for_day's
        # temporal clamp. Default to wall-clock now; the distiller passes the
        # session's end_date so a lesson learned from a session covering dates
        # up to T only becomes visible to backtests at dates > T (a skill
        # stamped "now" would never be selected for any historical backtest).
        created_at = f"{effective_date} 00:00:00" if effective_date else _now()
        db.execute(
            "INSERT OR IGNORE INTO skills (id, category, statement, source_session, "
            "source_ticker, source_market, created_at, performance_evidence) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (skill_id, category, statement, source_session, source_ticker,
             source_market, created_at, json.dumps(performance_evidence or {}, ensure_ascii=False)),
        )
        row = db.query_one("SELECT * FROM skills WHERE id=?", (skill_id,))
        return dict(row) if row else None

    def _find_duplicate(self, category: str, statement: str) -> dict | None:
        row = db.query_one(
            "SELECT * FROM skills WHERE category=? AND statement=?",
            (category, statement.strip()),
        )
        return dict(row) if row else None

    def apply_evidence(self, skill_id: str, helpful: bool, evidence: dict) -> None:
        """Attribution loop: bump counters and recompute success_rate."""
        row = db.query_one("SELECT times_applied, times_helpful FROM skills WHERE id=?", (skill_id,))
        if not row:
            return
        applied = row["times_applied"] + 1
        helpful_count = row["times_helpful"] + (1 if helpful else 0)
        db.execute(
            "UPDATE skills SET times_applied=?, times_helpful=?, success_rate=?, "
            "performance_evidence=? WHERE id=?",
            (applied, helpful_count, round(helpful_count / applied, 3),
             json.dumps(evidence, ensure_ascii=False), skill_id),
        )

    def list_skills(self, category: str | None = None, enabled: bool | None = None) -> list[dict]:
        sql = "SELECT * FROM skills WHERE merged_into IS NULL"
        params: list = []
        if category:
            sql += " AND category=?"
            params.append(category)
        if enabled is not None:
            sql += " AND enabled=?"
            params.append(1 if enabled else 0)
        sql += " ORDER BY created_at DESC"
        return [dict(r) for r in db.query(sql, tuple(params))]

    def update(self, skill_id: str, fields: dict) -> dict | None:
        allowed = {"enabled", "statement", "category"}
        sets, params = [], []
        for key, val in fields.items():
            if key in allowed:
                if key == "category" and val not in self.settings.skill_categories:
                    raise ValueError(f"unsupported skill category: {val}")
                if key == "statement":
                    val = _normalize_statement(val)
                sets.append(f"{key}=?")
                params.append(int(val) if isinstance(val, bool) else val)
        if not sets:
            return self.get(skill_id)
        params.append(skill_id)
        db.execute(f"UPDATE skills SET {', '.join(sets)} WHERE id=?", tuple(params))
        return self.get(skill_id)

    def delete(self, skill_id: str) -> None:
        db.execute("DELETE FROM skills WHERE id=?", (skill_id,))

    def get(self, skill_id: str) -> dict | None:
        row = db.query_one("SELECT * FROM skills WHERE id=?", (skill_id,))
        return dict(row) if row else None
