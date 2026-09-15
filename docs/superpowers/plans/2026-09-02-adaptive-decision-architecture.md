# Adaptive Decision Architecture Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a default three-tier adaptive decision architecture with zero-call monitoring, one-call fast decisions, and two-call committee reviews while retaining the classic graph and fast modes as selectable web options.

**Architecture:** Persist an explicit architecture enum per session and let `SessionManager` construct one of three existing-style decision-agent interfaces. The new `AdaptiveDecisionAgent` reuses deterministic trigger logic, delegates structured decisions to a focused fast agent or a two-stage committee, and leaves execution and hard risk control in `BacktestEngine`.

**Tech Stack:** Python 3.10+, Pydantic, FastAPI, SQLite, LangChain chat models, Vue 3, Vite, pytest.

**Spec:** `docs/superpowers/specs/2026-09-02-adaptive-decision-architecture-design.md`

## Global Constraints

- Preserve `classic_graph` behavior and the legacy `use_full_graph` column.
- New sessions default to `decision_architecture="adaptive"`.
- Adaptive normal path targets 0 calls, fast path 1 call, committee path 2 calls.
- LLM output never bypasses portfolio reconciliation, execution validation, no-lookahead clamping, or hard risk controls.
- Existing sessions map `use_full_graph=1` to `classic_graph` and `0` to `fast`.
- This source snapshot has no usable Git metadata; replace commit steps with verified task checkpoints.

---

### Task 1: Persist and validate the architecture choice

**Files:**
- Modify: `webapp/core/models.py`
- Modify: `webapp/store/db.py`
- Modify: `webapp/server/routers/sessions.py`
- Test: `tests/test_webapp_architecture_modes.py`

**Interfaces:**
- Produces: `DecisionArchitecture = Literal["adaptive", "classic_graph", "fast"]`.
- Produces: `SessionSpec.decision_architecture: DecisionArchitecture = "adaptive"`.
- Produces: `sessions.decision_architecture TEXT NOT NULL`.
- Preserves: `SessionSpec.use_full_graph` as an optional compatibility input during migration.

- [ ] **Step 1: Write failing model and migration tests**

```python
from pathlib import Path
import sqlite3

import pytest
from pydantic import ValidationError

from webapp.core.models import SessionSpec
from webapp.store import db


def _spec(**overrides):
    values = dict(
        ticker="600519.SS",
        start_date="2026-01-01",
        end_date="2026-01-31",
        initial_capital=100000,
    )
    values.update(overrides)
    return SessionSpec(**values)


def test_session_spec_defaults_to_adaptive():
    assert _spec().decision_architecture == "adaptive"


def test_session_spec_rejects_unknown_architecture():
    with pytest.raises(ValidationError):
        _spec(decision_architecture="twelve_agents")


def test_migrate_maps_legacy_full_graph(tmp_path: Path):
    path = tmp_path / "legacy.db"
    conn = sqlite3.connect(path)
    conn.executescript("""
        CREATE TABLE sessions (id TEXT PRIMARY KEY, use_full_graph INTEGER NOT NULL DEFAULT 0);
        INSERT INTO sessions VALUES ('graph', 1);
        INSERT INTO sessions VALUES ('fast', 0);
    """)
    db._migrate(conn)
    rows = dict(conn.execute("SELECT id, decision_architecture FROM sessions"))
    assert rows == {"graph": "classic_graph", "fast": "fast"}
```

- [ ] **Step 2: Run tests and confirm they fail on the missing field/column**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_webapp_architecture_modes.py -v`

Expected: failure mentioning `decision_architecture`.

- [ ] **Step 3: Add the enum field and idempotent migration**

Implement in `webapp/core/models.py`:

```python
DecisionArchitecture = Literal["adaptive", "classic_graph", "fast"]


class SessionSpec(BaseModel):
    # existing fields remain unchanged
    decision_architecture: DecisionArchitecture = "adaptive"
    use_full_graph: bool | None = None
```

Add the column to `_DDL`, then add this migration before other session migrations:

```python
if "decision_architecture" not in cols:
    conn.execute(
        "ALTER TABLE sessions ADD COLUMN decision_architecture "
        "TEXT NOT NULL DEFAULT 'adaptive'"
    )
    conn.execute(
        "UPDATE sessions SET decision_architecture="
        "CASE WHEN use_full_graph=1 THEN 'classic_graph' ELSE 'fast' END"
    )
```

Update session creation so `use_full_graph` is derived from the enum for new requests and `decision_architecture` is inserted explicitly. If an older caller omits the enum but explicitly sends `use_full_graph=true`, resolve it to `classic_graph`.

- [ ] **Step 4: Run the focused tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_webapp_architecture_modes.py -v`

Expected: all Task 1 tests pass.

- [ ] **Step 5: Checkpoint**

Record Task 1 as complete only after a fresh temporary database contains the column and both legacy rows map correctly.

### Task 2: Route sessions to the selected agent

**Files:**
- Modify: `webapp/server/session_manager.py`
- Test: `tests/test_webapp_session_manager_architecture.py`

**Interfaces:**
- Consumes: `sessions.decision_architecture` from Task 1.
- Produces: `SessionManager._build_agent(architecture: str)` returning an object with `reset_day()` and `decide(ctx)`.
- Defers import of `AdaptiveDecisionAgent` to avoid constructing LLM clients for unused modes.

- [ ] **Step 1: Write failing routing tests**

```python
from unittest.mock import MagicMock, patch

from webapp.config import WebappSettings
from webapp.server.session_manager import SessionManager


def test_build_agent_selects_adaptive():
    manager = SessionManager(WebappSettings())
    sentinel = object()
    with patch("webapp.engine.adaptive_agent.AdaptiveDecisionAgent", return_value=sentinel):
        assert manager._build_agent("adaptive") is sentinel


def test_build_agent_rejects_corrupt_database_value():
    manager = SessionManager(WebappSettings())
    try:
        manager._build_agent("unknown")
    except ValueError as exc:
        assert "unknown decision architecture" in str(exc)
    else:
        raise AssertionError("invalid architecture was accepted")
```

Add equivalent patched assertions for `classic_graph` and `fast`.

- [ ] **Step 2: Run tests and confirm `_build_agent` is missing**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_webapp_session_manager_architecture.py -v`

Expected: `AttributeError` for `_build_agent`.

- [ ] **Step 3: Extract explicit agent construction**

Implement `_build_agent`. `classic_graph` retains the existing `WEBAPP_GRAPH_TRIGGER` choice between `GraphDecisionAgent` and `HybridDecisionAgent`; `fast` retains `DecisionAgent`; `adaptive` constructs `AdaptiveDecisionAgent`. Change `start()` to query `decision_architecture` and call this method.

- [ ] **Step 4: Run focused routing tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_webapp_session_manager_architecture.py -v`

Expected: all mode selection tests pass without invoking a real model.

### Task 3: Implement the adaptive trigger policy

**Files:**
- Create: `webapp/engine/adaptive_agent.py`
- Modify: `webapp/config.py`
- Test: `tests/test_webapp_adaptive_agent.py`

**Interfaces:**
- Produces: `AdaptiveDecisionAgent(settings, fast_agent=None, committee=None)`.
- Produces: `AdaptiveDecisionAgent.decide(ctx) -> tuple[Decision, str, str, list[str]]`.
- Produces path markers `adaptive:path=zero_call`, `adaptive:path=fast`, and `adaptive:path=committee` in flags/audit.
- Consumes existing `rule_hits`, `_technical_breakdown`, `_technical_improvement`, and `DailyContext` fields.

- [ ] **Step 1: Write failing zero-call and trigger-routing tests**

Use `MagicMock` delegates and a minimal `DailyContext` fixture. Cover:

```python
def test_existing_thesis_with_no_change_is_zero_call(ctx_with_previous):
    fast = MagicMock()
    committee = MagicMock()
    agent = AdaptiveDecisionAgent(WebappSettings(), fast_agent=fast, committee=committee)
    decision, _, _, flags = agent.decide(ctx_with_previous)
    assert decision.action == "hold"
    assert "adaptive:path=zero_call" in flags
    fast.decide.assert_not_called()
    committee.decide.assert_not_called()


def test_first_day_uses_committee(ctx_without_previous):
    committee = MagicMock()
    committee.decide.return_value = (Decision(action="hold"), "audit", "response", [])
    agent = AdaptiveDecisionAgent(WebappSettings(), fast_agent=MagicMock(), committee=committee)
    agent.decide(ctx_without_previous)
    committee.decide.assert_called_once_with(ctx_without_previous)
```

Also cover material-but-not-major news to `fast`, rule-hit news to `committee`, price outside the band to `committee`, technical reversal to `committee`, and streak limits.

- [ ] **Step 2: Run tests and confirm the module is missing**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_webapp_adaptive_agent.py -v`

Expected: import failure for `webapp.engine.adaptive_agent`.

- [ ] **Step 3: Implement deterministic classification and zero-call stand-pat**

Add `adaptive_fast_streak_limit: int = 5` and `adaptive_max_calls_per_day: int = 4` to `WebappSettings`, populated from `WEBAPP_ADAPTIVE_FAST_STREAK_LIMIT` and `WEBAPP_ADAPTIVE_MAX_CALLS_PER_DAY`.

Implement a private classifier returning `(path, reason)` where path is `zero_call`, `fast`, or `committee`. Reuse the existing stand-pat semantics: keep the prior decision's band unchanged, return `hold`, preserve `target_position_pct`, and reset no counters except the zero-call streak. Do not call the news-impact LLM in the classifier.

- [ ] **Step 4: Run adaptive routing tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_webapp_adaptive_agent.py -v`

Expected: all deterministic routing tests pass.

### Task 4: Add one-call fast decisions and the two-call committee

**Files:**
- Create: `webapp/engine/committee_agent.py`
- Modify: `webapp/engine/adaptive_agent.py`
- Modify: `webapp/engine/decision_agent.py`
- Test: `tests/test_webapp_committee_agent.py`
- Test: `tests/test_webapp_adaptive_agent.py`

**Interfaces:**
- Produces: `CommitteeDecisionAgent(settings, analyst_llm=None, decision_llm=None)`.
- Produces: `CommitteeDecisionAgent.decide(ctx) -> tuple[Decision, str, str, list[str]]`.
- Produces exactly two normal-path model invocations: `committee_analysis`, then `committee_decision`.
- Adds a reusable `DecisionAgent.reconcile(decision, ctx, flags)` public wrapper around current account reconciliation.

- [ ] **Step 1: Write failing two-call and reconciliation tests**

```python
def test_committee_normal_path_uses_exactly_two_calls(ctx_with_previous):
    analyst = FakeLLM('{"bull_case":["trend"],"bear_case":["valuation"],'
                      '"risks":["gap"],"uncertainties":["news"],'
                      '"key_levels":{"support":100,"resistance":120}}')
    decider = FakeLLM('{"action":"hold","position_pct":0,"confidence":0.7,'
                      '"reasoning":"balanced","key_signals":[],"used_skills":[]}')
    agent = CommitteeDecisionAgent(WebappSettings(), analyst, decider)
    decision, audit, _, flags = agent.decide(ctx_with_previous)
    assert decision.action == "hold"
    assert analyst.calls == 1
    assert decider.calls == 1
    assert "多头证据" in audit
    assert "adaptive:path=committee" in flags
```

Add tests that the second prompt contains the first report and actual cash/shares, and that a sell on an empty account is reconciled to hold.

- [ ] **Step 2: Run focused tests and confirm failures**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_webapp_committee_agent.py tests\test_webapp_adaptive_agent.py -v`

Expected: missing committee class and public reconciliation method.

- [ ] **Step 3: Implement focused prompts and bounded parsing**

The first prompt asks for one JSON object with `bull_case`, `bear_case`, `risks`, `uncertainties`, and `key_levels`. The second prompt includes that JSON plus `DailyContext.to_user_message()` and asks for the existing `Decision` schema. Parse each once; allow at most one repair only when the normal result is malformed and only while below `adaptive_max_calls_per_day`.

Use low thinking for the fast delegate and high thinking for committee delegates through `webapp.llm.get_llm`. Increment `_call_count` for every actual direct invocation. Reuse `DecisionAgent.reconcile` so account truth remains identical across fast and committee modes.

- [ ] **Step 4: Implement adaptive failure semantics**

If a delegate fails and `ctx.prev_decision` exists, return a zero-call-style stand-pat with `adaptive:degraded=<stage>` and preserve the previous band. If the first committee on a session fails, re-raise so `BacktestEngine` marks the session failed.

- [ ] **Step 5: Run all adaptive and committee tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_webapp_committee_agent.py tests\test_webapp_adaptive_agent.py -v`

Expected: exact call counts, prompt composition, reconciliation, budgets, and failure behavior all pass.

### Task 5: Expose the architecture selector in both web frontends

**Files:**
- Modify: `webapp/frontend/src/views/SessionCreateView.vue`
- Modify: `webapp/frontend/src/views/SessionListView.vue`
- Modify: `webapp/frontend/src/views/SessionDetailView.vue`
- Modify: `webapp/server/static_fallback/index.html`
- Test: `tests/test_webapp_architecture_ui.py`

**Interfaces:**
- Consumes API field `decision_architecture`.
- Produces POST bodies with one of `adaptive`, `classic_graph`, or `fast`.
- Displays Chinese labels `自适应精简`、`经典完整`、`快速单模型`.

- [ ] **Step 1: Write a failing source-level UI contract test**

```python
from pathlib import Path


def test_vue_create_form_exposes_all_architectures():
    text = Path("webapp/frontend/src/views/SessionCreateView.vue").read_text(encoding="utf-8")
    for value in ("adaptive", "classic_graph", "fast"):
        assert f"value=\"{value}\"" in text or f"value: '{value}'" in text


def test_fallback_form_exposes_all_architectures():
    text = Path("webapp/server/static_fallback/index.html").read_text(encoding="utf-8")
    for value in ("adaptive", "classic_graph", "fast"):
        assert value in text
```

- [ ] **Step 2: Run the UI contract test and confirm failure**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_webapp_architecture_ui.py -v`

Expected: `adaptive` and `classic_graph` selectors are absent.

- [ ] **Step 3: Replace the checkbox with accessible radio cards**

Default the form to `decision_architecture: 'adaptive'`. Each card contains a label, call-count summary, and intended use. Remove `use_full_graph` from new POST bodies. Add an architecture label helper for list/detail display and mirror the selector in the fallback page.

- [ ] **Step 4: Build the Vue frontend**

Run: `npm run build`

Working directory: `webapp/frontend`

Expected: Vite exits 0 and refreshes `webapp/static`.

- [ ] **Step 5: Run the UI contract test again**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_webapp_architecture_ui.py -v`

Expected: both frontend variants expose all three values.

### Task 6: Verify compatibility, call counts, and regression safety

**Files:**
- Modify: `webapp/README.md`
- Modify: `HANDOVER.md`
- Test: all files created in Tasks 1–5

**Interfaces:**
- Documents the three architecture values and their expected call scale.
- Verifies classic Graph remains constructible without executing paid model calls.

- [ ] **Step 1: Update operator documentation**

Document the default adaptive flow, environment variables, fallback behavior, and the fact that classic Graph remains available for audit/benchmark runs. Replace claims that the full graph is the only deep-review architecture.

- [ ] **Step 2: Run focused tests**

Run: `.\.venv\Scripts\python.exe -m pytest tests\test_webapp_architecture_modes.py tests\test_webapp_session_manager_architecture.py tests\test_webapp_adaptive_agent.py tests\test_webapp_committee_agent.py tests\test_webapp_architecture_ui.py -v`

Expected: all pass with no external API calls.

- [ ] **Step 3: Run the project unit suite**

Run: `.\.venv\Scripts\python.exe -m pytest -m "not integration" -q`

Expected: exit 0; any unrelated pre-existing failure must be recorded separately rather than hidden.

- [ ] **Step 4: Run lint on changed Python files**

Run: `.\.venv\Scripts\python.exe -m ruff check webapp tests\test_webapp_architecture_modes.py tests\test_webapp_session_manager_architecture.py tests\test_webapp_adaptive_agent.py tests\test_webapp_committee_agent.py tests\test_webapp_architecture_ui.py`

Expected: exit 0.

- [ ] **Step 5: Verify the built application imports**

Run: `.\.venv\Scripts\python.exe -c "from webapp.server.app import create_app; app=create_app(); print(app.title)"`

Expected output contains `TradingAgents 回测模拟器`.

- [ ] **Step 6: Final checkpoint**

Report changed files, exact test/build results, and any unverified live-LLM behavior. Do not claim latency improvement from a real provider unless a paid end-to-end run was actually performed.
