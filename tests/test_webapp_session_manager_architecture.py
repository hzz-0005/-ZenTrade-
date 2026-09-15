import sys
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from webapp.config import WebappSettings
from webapp.server.session_manager import SessionManager


def test_build_agent_selects_adaptive(monkeypatch):
    sentinel = object()
    fake_module = SimpleNamespace(AdaptiveDecisionAgent=lambda settings: sentinel)
    monkeypatch.setitem(sys.modules, "webapp.engine.adaptive_agent", fake_module)

    assert SessionManager(WebappSettings())._build_agent("adaptive") is sentinel


def test_build_agent_selects_fast():
    sentinel = object()
    manager = SessionManager(WebappSettings())
    with (
        patch("webapp.server.session_manager._get_llm", return_value=object()),
        patch("webapp.server.session_manager.DecisionAgent", return_value=sentinel),
    ):
        assert manager._build_agent("fast") is sentinel


def test_build_agent_selects_classic_graph():
    sentinel = object()
    manager = SessionManager(WebappSettings(graph_trigger="on_news"))
    with patch("webapp.engine.hybrid_agent.HybridDecisionAgent", return_value=sentinel):
        assert manager._build_agent("classic_graph") is sentinel


def test_build_agent_rejects_corrupt_database_value():
    with pytest.raises(ValueError, match="unknown decision architecture"):
        SessionManager(WebappSettings())._build_agent("unknown")

