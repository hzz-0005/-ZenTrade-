"""In-process registry of running backtest tasks."""
from __future__ import annotations

import asyncio
import logging

from webapp.config import WebappSettings
from webapp.engine.backtest_engine import BacktestEngine
from webapp.engine.decision_agent import DecisionAgent
from webapp.skills.distiller import _get_llm
from webapp.skills.library import SkillLibrary
from webapp.store import db

logger = logging.getLogger(__name__)


class SessionManager:
    def __init__(self, settings: WebappSettings):
        self.settings = settings
        self.tasks: dict[str, asyncio.Task] = {}
        self.engines: dict[str, BacktestEngine] = {}

    def _build_agent(self, architecture: str):
        """Construct only the decision stack selected for this session."""
        if architecture == "adaptive":
            from webapp.engine.adaptive_agent import AdaptiveDecisionAgent

            return AdaptiveDecisionAgent(self.settings)

        if architecture == "classic_graph":
            from webapp.engine.graph_agent import GraphDecisionAgent
            from webapp.engine.hybrid_agent import HybridDecisionAgent

            if getattr(self.settings, "graph_trigger", "on_news") == "always":
                return GraphDecisionAgent(self.settings)
            return HybridDecisionAgent(self.settings)

        if architecture == "fast":
            decision_timeout = (
                180 if getattr(self.settings, "agent_mode", "single") == "panel" else 60
            )
            llm = _get_llm(self.settings, timeout=decision_timeout)
            return DecisionAgent(llm, self.settings)

        raise ValueError(f"unknown decision architecture: {architecture}")

    def start(self, session_id: str) -> None:
        if session_id in self.tasks and not self.tasks[session_id].done():
            return  # already running
        running = sum(1 for t in self.tasks.values() if not t.done())
        if running >= self.settings.max_concurrent_sessions:
            raise RuntimeError(
                f"已有 {running} 个回测在运行（上限 {self.settings.max_concurrent_sessions}），请稍后再试"
            )
        row = db.query_one(
            "SELECT decision_architecture FROM sessions WHERE id=?", (session_id,)
        )
        if row is None:
            raise RuntimeError(f"session {session_id} not found")
        agent = self._build_agent(str(row["decision_architecture"]))

        engine = BacktestEngine(
            session_id, self.settings,
            decision_agent=agent,
            skill_library=SkillLibrary(self.settings),
        )
        self.engines[session_id] = engine
        self.tasks[session_id] = asyncio.create_task(engine.run(), name=f"backtest-{session_id[:8]}")
        logger.info("session %s started", session_id)

    def pause(self, session_id: str) -> None:
        engine = self.engines.get(session_id)
        if engine:
            engine.pause()

    def resume(self, session_id: str) -> None:
        engine = self.engines.get(session_id)
        if engine:
            engine.resume()

    def stop(self, session_id: str) -> None:
        engine = self.engines.get(session_id)
        if engine:
            engine.stop()

    def delete(self, session_id: str) -> None:
        self.stop(session_id)
        task = self.tasks.pop(session_id, None)
        if task and not task.done():
            task.cancel()
        self.engines.pop(session_id, None)
