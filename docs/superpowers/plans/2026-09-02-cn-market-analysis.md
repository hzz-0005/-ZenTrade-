# A 股市场适配分析 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为 Web 回测的三种决策架构加入 A 股专用盈利阶段、估值和技术解释规则。

**Architecture:** 新增一个无网络依赖的市场适配模块，由数据网关在财务快照返回前统一富化。提示上下文和完整图架构只消费这份统一输出，原有指标计算与执行风控保持不变。

**Tech Stack:** Python 3.12、pandas、Pydantic、pytest、Markdown prompts

**Spec:** `docs/superpowers/specs/2026-09-02-cn-market-analysis-design.md`

## Global Constraints

- 不新增 LLM 调用。
- 不新增每日外部数据请求。
- 所有财务数据的报告期必须不晚于模拟日期。
- 非 A 股行为保持兼容。

---

### Task 1: A 股市场与盈利阶段分类器

**Files:**
- Create: `webapp/engine/market_profile.py`
- Test: `tests/test_webapp_cn_market_profile.py`

**Interfaces:**
- Consumes: `symbol: str`, `market: str`, `fundamentals: dict | None`
- Produces: `enrich_fundamentals(...) -> dict | None` 与 `market_guidance(...) -> str`

- [ ] **Step 1: Write the failing tests** covering board detection, loss, accounting-only turnaround, quality turnaround, and stable earnings.
- [ ] **Step 2: Run test to verify it fails**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_webapp_cn_market_profile.py -q`

Expected: FAIL because `webapp.engine.market_profile` does not exist.

- [ ] **Step 3: Write minimal implementation** with deterministic classification and rendered guidance.
- [ ] **Step 4: Run test to verify it passes** using the same command; expected PASS.

### Task 2: Expand the no-lookahead financial snapshot

**Files:**
- Modify: `webapp/engine/vendors/cn_source.py:130`
- Modify: `webapp/engine/data_gateway.py:263`
- Test: `tests/test_webapp_cn_market_profile.py`

**Interfaces:**
- Consumes: AKShare financial-indicator frame filtered to `日期 <= curr_date`.
- Produces: latest and prior-year comparable EPS/扣非 EPS, cash-flow quality fields, ordinary and adjusted estimated PE, then enriched market fields.

- [ ] **Step 1: Add failing frame-based tests** with literal expected snapshot values and a gateway integration test.
- [ ] **Step 2: Run test to verify the missing fields fail.**
- [ ] **Step 3: Extract `_snapshot_from_indicator_frame` and add the requested fields; compute adjusted PE only for positive adjusted EPS.**
- [ ] **Step 4: Run the focused test and expect PASS.**

### Task 3: Feed one shared rule set to all architectures

**Files:**
- Modify: `webapp/engine/context_builder.py:178`
- Modify: `webapp/engine/committee_agent.py:10`
- Modify: `webapp/engine/graph_agent.py:220`
- Modify: `webapp/prompts/decision_system.md:3`
- Modify: `webapp/prompts/decision_system_panel.md:11`
- Test: `tests/test_webapp_cn_market_profile.py`

**Interfaces:**
- Consumes: `market_guidance(market, symbol, fundamentals)`.
- Produces: a visible A 股 market-adapter section in the daily user context and full-graph extra context.

- [ ] **Step 1: Add a failing context-output test** asserting accounting-turnaround guidance reaches the real rendered prompt.
- [ ] **Step 2: Run it and confirm the A 股 section is absent.**
- [ ] **Step 3: Inject the shared guidance and add concise system rules: no fixed PE cutoff, high PE changes sizing rather than vetoing, and one observable stabilization signal is sufficient for a starter position.**
- [ ] **Step 4: Run focused architecture tests and expect PASS.**

### Task 4: Regression verification

**Files:**
- Test: existing Web architecture and vendor tests.

**Interfaces:**
- Consumes: completed implementation.
- Produces: fresh test and compile evidence.

- [ ] **Step 1: Run focused tests**

Run: `.venv\\Scripts\\python.exe -m pytest tests/test_webapp_cn_market_profile.py tests/test_webapp_committee_agent.py tests/test_webapp_adaptive_agent.py tests/test_vendor_routing.py -q`

- [ ] **Step 2: Compile modified Python files**

Run: `.venv\\Scripts\\python.exe -m py_compile webapp/engine/market_profile.py webapp/engine/context_builder.py webapp/engine/data_gateway.py webapp/engine/vendors/cn_source.py webapp/engine/committee_agent.py webapp/engine/graph_agent.py`

- [ ] **Step 3: Run the complete test suite with a temporary directory outside the repository** so `test_api_key_env.py` cannot discover or modify the project `.env`.

Run: `.venv\\Scripts\\python.exe -m pytest --basetemp "$env:TEMP\\tradingagents-pytest-cn-profile" -q`

