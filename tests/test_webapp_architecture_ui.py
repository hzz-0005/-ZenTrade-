import json
from html.parser import HTMLParser
from pathlib import Path
import subprocess


def _frontend_exports() -> dict:
    script = """
import * as frontend from './webapp/frontend/src/api.js';
const architectures = Array.isArray(frontend.ARCHITECTURES)
  ? frontend.ARCHITECTURES.map(({ value, label, callProfile }) => ({ value, label, callProfile }))
  : null;
const known = typeof frontend.architectureOf === 'function'
  ? frontend.architectureOf('adaptive')
  : null;
const legacy = typeof frontend.architectureOf === 'function'
  ? frontend.architectureOf('legacy_mode')
  : null;
const errorMessage = typeof frontend.toUserMessage === 'function'
  ? frontend.toUserMessage(new Error('网络超时'), '创建失败')
  : null;
const baseForm = {
  ticker: '600519.SS', initial_capital: 100000,
  start_date: '2026-08-01', end_date: '2026-09-01',
  decision_architecture: 'adaptive', commission_rate: ''
};
const blankCommission = typeof frontend.sessionPayload === 'function'
  ? frontend.sessionPayload(baseForm)
  : null;
const zeroCommission = typeof frontend.sessionPayload === 'function'
  ? frontend.sessionPayload({ ...baseForm, commission_rate: 0 })
  : null;
const localDates = typeof frontend.defaultSessionDates === 'function'
  ? frontend.defaultSessionDates(new Date(2026, 8, 3, 6, 0, 0))
  : null;
const sourceDays = Array.from({ length: 205 }, (_, index) => ({ day_index: index }));
const allDays = typeof frontend.collectPages === 'function'
  ? await frontend.collectPages(
      async ({ offset, limit }) => ({ total: sourceDays.length, days: sourceDays.slice(offset, offset + limit) }),
      80,
    )
  : null;
let guardState = null;
if (typeof frontend.createLatestRequestGuard === 'function') {
  const guard = frontend.createLatestRequestGuard();
  const first = guard.begin();
  const second = guard.begin();
  guardState = { first: guard.isCurrent(first), second: guard.isCurrent(second) };
}
const sizing = typeof frontend.decisionSizing === 'function'
  ? frontend.decisionSizing({ position_pct: 0.25, target_position_pct: 0.6 })
  : null;
let chart = {};
try { chart = await import('./webapp/frontend/src/charts/data.js'); } catch {}
const rejectedPrice = typeof chart.markerPlotPrice === 'function'
  ? chart.markerPlotPrice({ date: '2026-09-01', price: 0 }, { '2026-09-01': 101.5 })
  : null;
const filledPrice = typeof chart.markerPlotPrice === 'function'
  ? chart.markerPlotPrice({ date: '2026-09-01', price: 99.5 }, { '2026-09-01': 101.5 })
  : null;
const nextOpenTimeline = typeof frontend.decisionTimeline === 'function'
  ? frontend.decisionTimeline({ date: '2026-01-05', decision_date: '2026-01-05', execution_date: '2026-01-06', valuation_date: '2026-01-06' })
  : null;
const coastTimeline = typeof frontend.decisionTimeline === 'function'
  ? frontend.decisionTimeline({ date: '2026-01-06', decision_date: '2026-01-06', execution_date: null, valuation_date: '2026-01-06' })
  : null;
const cooldownAction = frontend.ACTION?.cooldown || null;
const cooldownDisplayedAction = typeof frontend.displayedAction === 'function'
  ? frontend.displayedAction({ action: 'cooldown' }, { action: 'hold' })
  : null;
console.log(JSON.stringify({
  architectures, known, legacy, errorMessage, blankCommission, zeroCommission,
  localDates, allDays, guardState, sizing, rejectedPrice, filledPrice,
  nextOpenTimeline, coastTimeline, cooldownAction, cooldownDisplayedAction,
}));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)


def _render_session_row() -> str | None:
    script = """
import { createServer } from 'vite';
import vue from '@vitejs/plugin-vue';
import { createSSRApp, h } from 'vue';
import { renderToString } from '@vue/server-renderer';
const server = await createServer({
  configFile: false, logLevel: 'silent', plugins: [vue()], appType: 'custom',
  server: { middlewareMode: true },
});
let html = null;
try {
  const component = (await server.ssrLoadModule('/src/components/SessionTableRow.vue')).default;
  const RouterLink = {
    props: ['to'],
    setup(props, { slots }) { return () => h('a', { href: props.to }, slots.default?.()); },
  };
  const session = {
    id: 'ui-test', canonical_ticker: '600519.SS', market: 'cn',
    decision_architecture: 'adaptive', start_date: '2026-08-01', end_date: '2026-09-01',
    status: 'done', current_day_index: 20, trading_days: Array(20).fill('2026-08-01'),
    total_return_pct: 8.5, max_drawdown_pct: 3.2, final_equity: 108500,
  };
  const app = createSSRApp({ render: () => h('table', [h('tbody', [h(component, { session })])]) });
  app.component('RouterLink', RouterLink);
  html = await renderToString(app);
} catch {}
await server.close();
console.log(JSON.stringify({ html }));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        cwd=Path("webapp/frontend"),
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return json.loads(result.stdout)["html"]


class _InteractiveElements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.buttons = []

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag == "a":
            self.links.append(values)
        elif tag == "button":
            self.buttons.append(values)


def test_vue_create_form_exposes_all_architectures():
    exported = _frontend_exports()
    assert [item["value"] for item in exported["architectures"]] == [
        "adaptive",
        "classic_graph",
        "fast",
    ]
    assert exported["known"]["label"] == "自适应精简"
    assert exported["known"]["callProfile"] == "0–2 次 / 决策日"
    assert exported["legacy"]["label"] == "旧版架构"


def test_frontend_turns_operation_errors_into_inline_messages():
    exported = _frontend_exports()
    assert exported["errorMessage"] == "创建失败：网络超时"


def test_create_payload_omits_blank_commission_but_preserves_zero():
    exported = _frontend_exports()
    assert "commission_rate" not in exported["blankCommission"]
    assert exported["zeroCommission"]["commission_rate"] == 0


def test_default_session_dates_use_local_calendar_days():
    exported = _frontend_exports()
    assert exported["localDates"] == {"start": "2026-08-04", "end": "2026-09-02"}


def test_frontend_pagination_collects_every_daily_record():
    exported = _frontend_exports()
    assert len(exported["allDays"]) == 205
    assert exported["allDays"][0]["day_index"] == 0
    assert exported["allDays"][-1]["day_index"] == 204


def test_stale_drawer_tokens_and_decision_size_semantics_are_explicit():
    exported = _frontend_exports()
    assert exported["guardState"] == {"first": False, "second": True}
    assert exported["sizing"] == {"actionPercent": 25, "targetPercent": 60}


def test_rejected_marker_uses_daily_close_instead_of_zero_fill_price():
    exported = _frontend_exports()
    assert exported["rejectedPrice"] == 101.5
    assert exported["filledPrice"] == 99.5


def test_frontend_labels_decision_execution_and_valuation_dates_separately():
    """Falling back to one generic date would mislabel T-close/T+1-open results."""
    exported = _frontend_exports()
    assert exported["nextOpenTimeline"] == {
        "decisionDate": "2026-01-05",
        "executionDate": "2026-01-06",
        "valuationDate": "2026-01-06",
        "executionLabel": "执行 2026-01-06",
        "valuationLabel": "估值 2026-01-06",
    }
    assert exported["coastTimeline"]["executionLabel"] == "无需执行"


def test_frontend_labels_cooldown_as_a_calming_period():
    """Removing the action mapping would expose raw `cooldown` in the daily ledger."""
    exported = _frontend_exports()
    assert exported["cooldownAction"] == {"text": "冷静期", "badge": "b-cooldown"}
    assert exported["cooldownDisplayedAction"] == "cooldown"


def test_session_row_renders_separate_native_open_and_delete_controls():
    rendered = _render_session_row()
    assert rendered is not None
    elements = _InteractiveElements()
    elements.feed(rendered)
    assert any(link.get("href") == "/sessions/ui-test" for link in elements.links)
    assert any(button.get("aria-label") == "删除 600519.SS 会话" for button in elements.buttons)


def test_fallback_form_exposes_all_architectures():
    text = Path("webapp/server/static_fallback/index.html").read_text(encoding="utf-8")
    for value in ("adaptive", "classic_graph", "fast"):
        assert f'value="{value}"' in text
