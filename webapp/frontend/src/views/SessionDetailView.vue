<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { ACTION, api, architectureOf, createLatestRequestGuard, decisionSizing, decisionTimeline, displayedAction, fmt, toUserMessage } from '../api'
import EquityChart from '../components/EquityChart.vue'
import PriceChart from '../components/PriceChart.vue'
import SideDrawer from '../components/SideDrawer.vue'
import StateBadge from '../components/StateBadge.vue'

const props = defineProps({ id: String })
const detail = ref(null)
const days = ref([])
const review = ref(null)
const kline = ref([])
const markers = ref([])
const equityData = ref([])
const initial = ref(0)
const stockName = ref(null)
const trades = ref([])
const activeTab = ref('log')
const pageError = ref('')
const controlError = ref('')
const drawer = reactive({ open: false, day: null, detail: null, loading: false, error: null })
const drawerGuard = createLatestRequestGuard()
let drawerTimeoutId = null
let timer = null

const LIVE = ['created', 'running', 'paused', 'completed', 'reviewing']
const architecture = computed(() => architectureOf(detail.value?.decision_architecture))
const curEquity = computed(() => (days.value.length ? days.value[days.value.length - 1].equity : null))
const progressPct = computed(() => {
  const total = (detail.value?.trading_days || []).length
  return total ? Math.min(100, Math.round(((detail.value?.current_day_index || 0) / total) * 100)) : 0
})
const winRateNote = computed(() => {
  const closed = trades.value.filter((trade) => trade.status === 'closed').length
  const open = trades.value.filter((trade) => trade.status !== 'closed').length
  if (detail.value?.win_rate == null) {
    return closed === 0 ? `尚未平仓（当前 ${open} 笔持仓中）` : '—'
  }
  return null
})
const tradeReviews = computed(() => parseArray(review.value?.trade_reviews_json))
const createdSkills = computed(() => parseArray(review.value?.skills_created_json))
const closeMap = computed(() => {
  const map = {}
  let previous = null
  for (const candle of kline.value) {
    map[candle.date] = {
      close: candle.close,
      change: previous ? ((candle.close / previous - 1) * 100) : null,
    }
    previous = candle.close
  }
  return map
})

function parseArray(value) {
  try {
    const parsed = JSON.parse(value || '[]')
    return Array.isArray(parsed) ? parsed : []
  } catch {
    return []
  }
}

function fmtLatency(ms) {
  if (ms == null) return ''
  if (ms >= 60_000) return `${(ms / 60_000).toFixed(1)} 分钟`
  if (ms >= 1_000) return `${(ms / 1_000).toFixed(1)} 秒`
  return `${ms} ms`
}

async function refresh() {
  detail.value = await api.getSession(props.id)
  days.value = await api.allDays(props.id)
  try { trades.value = (await api.trades(props.id)).trades } catch { trades.value = [] }
  try { review.value = await api.review(props.id) } catch { review.value = null }
}

async function loadCharts() {
  const [chart, price] = await Promise.all([api.chart(props.id), api.kline(props.id)])
  markers.value = chart.markers
  kline.value = price.kline
  equityData.value = chart.equity
  initial.value = chart.initial_capital
}

async function openDay(day) {
  const requestToken = drawerGuard.begin()
  if (drawerTimeoutId) clearTimeout(drawerTimeoutId)
  drawer.day = day
  drawer.detail = null
  drawer.error = null
  drawer.loading = true
  drawer.open = true
  let timeoutId
  const timeout = new Promise((_, reject) => {
    timeoutId = setTimeout(() => reject(new Error('请求超时（>60s），该交易日 LLM 决策可能仍在生成')), 60_000)
    drawerTimeoutId = timeoutId
  })
  try {
    const result = await Promise.race([api.dayDetail(props.id, day.day_index), timeout])
    if (drawerGuard.isCurrent(requestToken)) drawer.detail = result
  } catch (error) {
    if (drawerGuard.isCurrent(requestToken)) drawer.error = error?.message || String(error)
  } finally {
    clearTimeout(timeoutId)
    if (drawerTimeoutId === timeoutId) drawerTimeoutId = null
    if (drawerGuard.isCurrent(requestToken)) drawer.loading = false
  }
}

function closeDrawer() {
  drawerGuard.invalidate()
  if (drawerTimeoutId) {
    clearTimeout(drawerTimeoutId)
    drawerTimeoutId = null
  }
  drawer.open = false
  drawer.loading = false
}

async function ctrl(action) {
  controlError.value = ''
  try {
    await api.controlSession(props.id, action)
    await refresh()
  } catch (error) {
    controlError.value = toUserMessage(error, '控制操作失败')
  }
}

function selectTab(tab) {
  activeTab.value = tab
}

function onTabKeydown(event) {
  if (!['ArrowLeft', 'ArrowRight'].includes(event.key)) return
  event.preventDefault()
  const nextTab = activeTab.value === 'log' ? 'trades' : 'log'
  selectTab(nextTab)
  document.getElementById(`decision-tab-${nextTab}`)?.focus()
}

function decisionOf(day) {
  try { return JSON.parse(day.decision_json || '{}') } catch { return {} }
}
const decOf = decisionOf
const sizingOf = decisionSizing
const timelineOf = decisionTimeline
const actionOf = (day) => displayedAction(day, decisionOf(day))
function flagsOf(day) { return parseArray(day.data_flags) }
function skillsOf(day) { return parseArray(day.skills_injected) }
function coastBand(day) {
  const marker = flagsOf(day).find((flag) => flag.startsWith('coast:'))
  return marker ? marker.replace('coast: 沿用', '').trim() : '—'
}
function dayEquityChange(day) {
  const index = days.value.findIndex((item) => item.day_index === day.day_index)
  if (index <= 0) return null
  const previous = days.value[index - 1].equity
  return previous ? (day.equity / previous - 1) * 100 : null
}

onMounted(async () => {
  try {
    await refresh()
    await loadCharts()
  } catch (error) {
    pageError.value = toUserMessage(error, '会话加载失败')
    return
  }

  api.stockName(detail.value.canonical_ticker)
    .then((result) => { stockName.value = result.name })
    .catch(() => {})

  if (days.value.length && !LIVE.includes(detail.value.status)) {
    const acted = [...days.value].reverse().find((day) => day.action === 'buy' || day.action === 'sell')
    openDay(acted || days.value[days.value.length - 1])
  }
  if (LIVE.includes(detail.value.status)) {
    timer = setInterval(async () => {
      try {
        await refresh()
        await loadCharts()
        if (!LIVE.includes(detail.value.status)) { clearInterval(timer); timer = null }
      } catch (error) {
        pageError.value = toUserMessage(error, '数据同步失败')
      }
    }, 3000)
  }
})
onBeforeUnmount(() => {
  drawerGuard.invalidate()
  if (drawerTimeoutId) clearTimeout(drawerTimeoutId)
  if (timer) clearInterval(timer)
})
</script>

<template>
  <div v-if="detail" class="detail-page">
    <RouterLink class="back-link" to="/">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M19 12H5m5-5-5 5 5 5" /></svg>
      返回会话
    </RouterLink>

    <header class="asset-header">
      <div class="asset-identity">
        <span class="asset-mark">{{ detail.canonical_ticker.slice(0, 2) }}</span>
        <div>
          <div class="asset-title-line">
            <h1>{{ detail.canonical_ticker }}</h1>
            <span v-if="stockName" class="asset-name">{{ stockName }}</span>
            <StateBadge :status="detail.status" />
          </div>
          <p>
            <span class="arch-tag">{{ architecture.shortLabel }}</span>
            <span class="num">{{ detail.start_date }} — {{ detail.end_date }}</span>
            <span>初始资金 ¥ {{ fmt.money(detail.initial_capital) }}</span>
          </p>
        </div>
      </div>
      <div class="header-actions">
        <button v-if="detail.status === 'running'" class="btn" @click="ctrl('pause')">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M9 6v12M15 6v12" /></svg>暂停
        </button>
        <button v-if="detail.status === 'paused'" class="btn" @click="ctrl('resume')">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="m9 6 9 6-9 6z" /></svg>继续
        </button>
        <button v-if="['running', 'paused'].includes(detail.status)" class="btn danger" @click="ctrl('stop')">停止回测</button>
      </div>
    </header>

    <div v-if="controlError || pageError" class="notice error" role="alert">
      <span>{{ controlError || pageError }}</span>
      <button class="icon-btn" aria-label="关闭提示" @click="controlError = ''; pageError = ''">×</button>
    </div>

    <section class="portfolio-strip" aria-label="组合核心指标">
      <div class="portfolio-primary">
        <span>当前权益</span>
        <strong class="num">¥ {{ fmt.money(curEquity) }}</strong>
        <small>初始 ¥ {{ fmt.money(detail.initial_capital) }}</small>
      </div>
      <div>
        <span>总收益</span>
        <strong class="num" :class="fmt.cls(detail.total_return_pct)">{{ fmt.pct(detail.total_return_pct) }}</strong>
        <small>区间累计</small>
      </div>
      <div>
        <span>最大回撤</span>
        <strong class="num">{{ detail.max_drawdown_pct != null ? detail.max_drawdown_pct + '%' : '—' }}</strong>
        <small>权益峰值回落</small>
      </div>
      <div>
        <span>已平仓胜率</span>
        <strong class="num">{{ detail.win_rate != null ? detail.win_rate + '%' : '—' }}</strong>
        <small>{{ winRateNote || `${trades.filter((trade) => trade.status === 'closed').length} 笔已平仓` }}</small>
      </div>
    </section>

    <div class="run-progress">
      <div class="run-progress-copy">
        <span>研究进度</span>
        <b class="num">{{ detail.current_day_index }}/{{ (detail.trading_days || []).length }} 交易日</b>
      </div>
      <div class="progress"><i :style="{ width: progressPct + '%' }"></i></div>
      <strong class="num">{{ progressPct }}%</strong>
    </div>

    <section class="chart-grid">
      <article class="panel chart-panel price-panel">
        <div class="panel-head">
          <div><p class="panel-kicker">Price action</p><h2>价格与操作</h2></div>
          <span class="chart-legend"><i class="up"></i>涨 <i class="down"></i>跌 · ▲买入 ▼卖出</span>
        </div>
        <PriceChart :kline="kline" :markers="markers" :session-start="detail.start_date" :session-end="detail.end_date" />
      </article>
      <article class="panel chart-panel equity-panel">
        <div class="panel-head">
          <div><p class="panel-kicker">Portfolio curve</p><h2>账户权益</h2></div>
          <span class="panel-meta">现金 / 初始资金</span>
        </div>
        <EquityChart :equity="equityData" :initial="initial" />
      </article>
    </section>

    <section class="panel records-panel">
      <div class="panel-head records-head">
        <div><p class="panel-kicker">Decision ledger</p><h2>交易与决策</h2></div>
        <div class="segmented" role="tablist" aria-label="交易记录类型">
          <button id="decision-tab-log" :class="{ active: activeTab === 'log' }" role="tab"
                  aria-controls="decision-panel-log" :aria-selected="activeTab === 'log'"
                  :tabindex="activeTab === 'log' ? 0 : -1" @click="selectTab('log')" @keydown="onTabKeydown">每日操作</button>
          <button id="decision-tab-trades" :class="{ active: activeTab === 'trades' }" role="tab"
                  aria-controls="decision-panel-trades" :aria-selected="activeTab === 'trades'"
                  :tabindex="activeTab === 'trades' ? 0 : -1" @click="selectTab('trades')" @keydown="onTabKeydown">交易明细 <span>{{ trades.length }}</span></button>
        </div>
      </div>

      <div v-if="activeTab === 'log'" id="decision-panel-log" class="tab-panel" role="tabpanel" aria-labelledby="decision-tab-log">
        <div v-if="days.length" class="table-wrap">
          <table class="decision-table">
          <thead><tr><th>决策 / 估值日期</th><th>收盘 / 涨跌</th><th>操作</th><th>成交明细</th><th>权益 / 日变动</th><th>决策依据</th></tr></thead>
          <tbody>
            <tr v-for="day in days" :key="day.day_index" class="click" tabindex="0" @click="openDay(day)" @keydown.enter="openDay(day)" @keydown.space.prevent="openDay(day)">
              <td class="num date-strong"><b>{{ timelineOf(day).decisionDate }}</b><small>{{ timelineOf(day).valuationLabel }}</small></td>
              <td class="num">
                <template v-if="closeMap[day.date]">
                  <b>{{ closeMap[day.date].close }}</b>
                  <small :class="fmt.cls(closeMap[day.date].change)">
                    {{ closeMap[day.date].change != null ? (closeMap[day.date].change > 0 ? '+' : '') + closeMap[day.date].change.toFixed(2) + '%' : '' }}
                  </small>
                </template>
                <span v-else class="muted">—</span>
              </td>
              <td><StateBadge :action="day.action" /></td>
              <td class="num trade-fill">
                <template v-if="day.executed_shares"><b>{{ day.executed_shares }} 股 @ {{ day.executed_price }}</b><small>{{ timelineOf(day).executionLabel }} · 费用 {{ day.fee }}</small></template>
                <template v-else-if="day.execution_status === 'not_required'"><span class="muted">{{ timelineOf(day).executionLabel }}</span></template>
                <template v-else-if="day.action === 'coast'"><span class="muted">沿用 {{ coastBand(day) }}</span></template>
                <span v-else class="muted">—</span>
              </td>
              <td class="num equity-daily">
                <b>¥ {{ fmt.money(day.equity) }}</b>
                <small :class="fmt.cls(dayEquityChange(day))">{{ dayEquityChange(day) != null ? (dayEquityChange(day) > 0 ? '+' : '') + dayEquityChange(day).toFixed(2) + '%' : '' }}</small>
              </td>
              <td class="reason-cell">
                <p>{{ decisionOf(day).reasoning || '沿用已有决策，未产生新的推理文本。' }}</p>
                <div v-if="(decisionOf(day).key_signals || []).length" class="signal-list">
                  <span v-for="signal in decisionOf(day).key_signals" :key="signal">{{ signal }}</span>
                </div>
                <small>置信度 {{ Math.round((decisionOf(day).confidence || 0) * 100) }}%<template v-if="(decisionOf(day).used_skills || []).length"> · 引用 {{ decisionOf(day).used_skills.length }} 条经验</template></small>
              </td>
            </tr>
          </tbody>
          </table>
        </div>
        <div v-else class="empty-state compact-empty">
          <h3>等待第一个交易日</h3>
          <p>会话开始运行后，每日决策会逐条出现在这里。</p>
        </div>
      </div>

      <div v-else id="decision-panel-trades" class="tab-panel" role="tabpanel" aria-labelledby="decision-tab-trades">
        <div v-if="trades.length" class="table-wrap">
          <table>
          <thead><tr><th>开仓日</th><th>平仓日</th><th>股数</th><th>开仓价</th><th>平仓价</th><th>盈亏</th><th>收益率</th><th>状态</th></tr></thead>
          <tbody>
            <tr v-for="trade in trades" :key="trade.id">
              <td class="num">{{ trade.entry_date }}</td><td class="num">{{ trade.exit_date || '持仓中' }}</td>
              <td class="num">{{ trade.shares }}</td><td class="num">{{ trade.entry_price }}</td><td class="num">{{ trade.exit_price || '—' }}</td>
              <td class="num" :class="fmt.cls(trade.realized_pnl)">{{ trade.realized_pnl ?? '—' }}</td>
              <td class="num" :class="fmt.cls(trade.return_pct)">{{ fmt.pct(trade.return_pct) }}</td>
              <td><span class="badge" :class="trade.status === 'closed' ? 'b-done' : 'b-run'">{{ trade.status === 'closed' ? '已平仓' : '持仓中' }}</span></td>
            </tr>
          </tbody>
          </table>
        </div>
        <div v-else class="empty-state compact-empty">
          <h3>暂时没有交易明细</h3>
          <p>产生实际买卖后，这里会展示开仓、平仓和收益。</p>
        </div>
      </div>
    </section>

    <section v-if="review" class="panel review-panel">
      <div class="panel-head"><div><p class="panel-kicker">Post-run review</p><h2>复盘总结</h2></div></div>
      <div class="review-grid">
        <div><h3>结论摘要</h3><p>{{ review.summary }}</p></div>
        <div><h3>策略反思</h3><p>{{ review.reflection }}</p></div>
      </div>
      <template v-if="tradeReviews.length">
        <div class="section-divider"><span>逐笔点评</span></div>
        <div class="table-wrap">
          <table><thead><tr><th>开仓</th><th>平仓</th><th>评级</th><th>点评</th></tr></thead>
            <tbody><tr v-for="(item, index) in tradeReviews" :key="index"><td class="num">{{ item.entry_date }}</td><td class="num">{{ item.exit_date || '持仓中' }}</td><td><b>{{ item.grade }}</b></td><td>{{ item.comment }}</td></tr></tbody>
          </table>
        </div>
      </template>
      <template v-if="createdSkills.length">
        <div class="section-divider"><span>本次提炼的经验</span></div>
        <div v-for="skill in createdSkills" :key="skill.id" class="skill compact-skill"><div class="body"><span class="category-label">{{ skill.category }}</span><p>{{ skill.statement }}</p></div></div>
      </template>
    </section>

    <section v-else-if="detail.status === 'reviewing'" class="panel review-loading">
      <span class="spinner"></span><div><h3>正在生成复盘</h3><p>Agent 正在点评交易并完成经验去重。</p></div>
    </section>

    <SideDrawer :open="drawer.open" :day="drawer.day" @close="closeDrawer">
      <template v-if="drawer.error">
        <div class="empty-state compact-empty error-state">
          <h3>决策详情加载失败</h3><p>{{ drawer.error }}</p>
          <button class="btn" @click="closeDrawer">关闭</button>
        </div>
      </template>
      <template v-else-if="drawer.detail">
        <div class="sec">Agent 决策</div>
        <div class="dec-card">
          <div class="dec-head">
            <span class="badge" :class="ACTION[actionOf(drawer.detail)]?.badge || 'b-hold'">{{ ACTION[actionOf(drawer.detail)]?.text || actionOf(drawer.detail) || '—' }}</span>
            <span v-if="sizingOf(decOf(drawer.detail)).actionPercent != null" class="dec-kv">动作比例 <b>{{ sizingOf(decOf(drawer.detail)).actionPercent }}%</b></span>
            <span v-if="sizingOf(decOf(drawer.detail)).targetPercent != null" class="dec-kv">目标仓位 <b>{{ sizingOf(decOf(drawer.detail)).targetPercent }}%</b></span>
            <span v-if="decOf(drawer.detail).confidence != null" class="dec-kv">置信度 <span class="conf"><i :style="{ width: Math.min(100, decOf(drawer.detail).confidence * 100) + '%' }"></i></span><b>{{ (decOf(drawer.detail).confidence * 100).toFixed(0) }}%</b></span>
          </div>
          <p v-if="decOf(drawer.detail).reasoning" class="dec-reason">{{ decOf(drawer.detail).reasoning }}</p>
          <p v-else class="muted">无新推理文本：可能是沿用之前决策，或模型输出解析失败后回退为持有。</p>
          <template v-if="(decOf(drawer.detail).key_signals || []).length"><div class="dec-sub">关键信号</div><ul class="dec-signals"><li v-for="(signal, index) in decOf(drawer.detail).key_signals" :key="index">{{ signal }}</li></ul></template>
          <template v-if="(decOf(drawer.detail).used_skills || []).length"><div class="dec-sub">引用的历史经验</div><ul class="dec-signals"><li v-for="(skill, index) in decOf(drawer.detail).used_skills" :key="index">{{ skill }}</li></ul></template>
        </div>

        <div class="sec">执行结果</div>
        <div class="kv-grid">
          <div><span>执行请求比例</span><b>{{ drawer.detail.requested_pct != null ? (drawer.detail.requested_pct * 100).toFixed(0) + '% 可用资金 / 持股' : '—' }}</b></div>
          <div><span>成交</span><b>{{ drawer.detail.executed_shares ? `${drawer.detail.executed_shares} 股 @ ${fmt.money(drawer.detail.executed_price)}` : '未成交' }}</b></div>
          <div><span>成交后现金</span><b>{{ fmt.money(drawer.detail.cash_after) }}</b></div>
          <div><span>成交后持股</span><b>{{ drawer.detail.shares_after }} 股</b></div>
          <div><span>{{ timelineOf(drawer.detail).valuationLabel }}权益</span><b>{{ fmt.money(drawer.detail.equity) }}</b></div>
          <div><span>LLM 调用</span><b>{{ drawer.detail.llm_calls ?? 0 }} 次 · {{ fmtLatency(drawer.detail.latency_ms) || '未调用（沿用）' }}</b></div>
        </div>

        <div class="sec">注入的历史经验（{{ skillsOf(drawer.detail).length }} 条）</div>
        <div v-for="skill in skillsOf(drawer.detail)" :key="skill.id" class="skill compact-skill"><div class="body"><span class="category-label">{{ skill.category }}</span><p>{{ skill.statement }}</p></div></div>
        <p v-if="!skillsOf(drawer.detail).length" class="muted">当日未注入经验</p>
        <div class="sec">数据覆盖情况</div>
        <div v-if="flagsOf(drawer.detail).length" class="flag">{{ flagsOf(drawer.detail).join('；') }}</div><p v-else class="muted">全部数据源正常</p>
        <div class="sec">原始决策 JSON</div><pre class="audit">{{ drawer.detail.decision_json }}</pre>
        <div class="sec">当日完整 Prompt</div><pre class="audit">{{ drawer.detail.prompt_text || '（沿用决策日：当日未调用模型，无 prompt）' }}</pre>
      </template>
      <div v-else class="drawer-loading">
        <span v-if="drawer.loading" class="spinner"></span>
        <div><h3>{{ drawer.loading ? '正在载入完整决策' : '暂无数据' }}</h3><p v-if="drawer.loading">Prompt 与响应可能较大；当日 LLM 耗时 {{ fmtLatency(drawer.day?.latency_ms) || '仍在统计' }}</p></div>
      </div>
    </SideDrawer>
  </div>

  <div v-else-if="pageError" class="empty-state page-error-state">
    <div class="empty-mark" aria-hidden="true">!</div><h2>无法打开这个会话</h2><p>{{ pageError }}</p><RouterLink class="btn" to="/">返回会话列表</RouterLink>
  </div>
  <div v-else class="detail-skeleton" aria-label="正在加载会话详情">
    <span class="skeleton detail-title"></span><span class="skeleton detail-strip"></span>
    <div class="skeleton-chart-grid"><span class="skeleton"></span><span class="skeleton"></span></div>
  </div>
</template>
