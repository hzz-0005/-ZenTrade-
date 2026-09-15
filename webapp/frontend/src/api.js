/** 统一 API 客户端：错误规范化 + JSON 处理 */
async function request(path, opts = {}) {
  const resp = await fetch('/api' + path, {
    headers: { 'Content-Type': 'application/json' },
    ...opts,
    body: opts.body !== undefined ? JSON.stringify(opts.body) : undefined,
  })
  if (!resp.ok) {
    const err = await resp.json().catch(() => ({ detail: resp.statusText }))
    const msg = typeof err.detail === 'string' ? err.detail : JSON.stringify(err.detail)
    throw new Error(msg || `HTTP ${resp.status}`)
  }
  return resp.status === 204 ? null : resp.json()
}

export async function collectPages(loadPage, pageSize = 200) {
  const items = []
  let offset = 0
  while (true) {
    const page = await loadPage({ offset, limit: pageSize })
    const batch = Array.isArray(page?.days) ? page.days : []
    items.push(...batch)
    const total = Number(page?.total ?? items.length)
    if (!batch.length || items.length >= total) break
    offset += batch.length
  }
  return items
}

export const api = {
  // sessions
  listSessions: (params = '') => request('/sessions' + params),
  getSession: (id) => request(`/sessions/${id}`),
  createSession: (body) => request('/sessions', { method: 'POST', body }),
  controlSession: (id, action) => request(`/sessions/${id}/${action}`, { method: 'POST', body: {} }),
  deleteSession: (id) => request(`/sessions/${id}`, { method: 'DELETE' }),
  chart: (id) => request(`/sessions/${id}/chart`),
  kline: (id) => request(`/sessions/${id}/kline`),
  days: (id, query = '') => request(`/sessions/${id}/days${query}`),
  allDays: (id) => collectPages(({ offset, limit }) =>
    request(`/sessions/${id}/days?offset=${offset}&limit=${limit}`)),
  dayDetail: (id, dayIndex) => request(`/sessions/${id}/days/${dayIndex}`),
  trades: (id) => request(`/sessions/${id}/trades`),
  review: (id) => request(`/sessions/${id}/review`),
  // skills
  listSkills: (query = '') => request('/skills' + query),
  updateSkill: (id, body) => request(`/skills/${id}`, { method: 'PATCH', body }),
  deleteSkill: (id) => request(`/skills/${id}`, { method: 'DELETE' }),
  // meta
  health: () => request('/health'),
  config: () => request('/config'),
  stockName: (ticker) => request(`/meta/stock-name?ticker=${encodeURIComponent(ticker)}`),
}

export const fmt = {
  pct: (v) => (v == null ? '—' : `${v > 0 ? '+' : ''}${v}%`),
  money: (v) => (v == null ? '—' : Number(v).toLocaleString('zh-CN', { maximumFractionDigits: 2 })),
  cls: (v) => (v > 0 ? 'pos' : v < 0 ? 'neg' : ''),
}

export const ARCHITECTURES = Object.freeze([
  {
    value: 'adaptive',
    label: '自适应精简',
    shortLabel: '自适应',
    eyebrow: '推荐',
    description: '普通波动尽量沿用决策，必要时再增加分析深度，兼顾速度与判断质量。',
    callProfile: '0–2 次 / 决策日',
    speed: '均衡',
    audit: '动态深度',
  },
  {
    value: 'classic_graph',
    label: '经典完整',
    shortLabel: '经典图',
    eyebrow: '完整链路',
    description: '保留分析师、多空辩论、交易员与风险团队，适合完整审计和对照实验。',
    callProfile: '多 Agent / 决策日',
    speed: '较慢',
    audit: '完整审计',
  },
  {
    value: 'fast',
    label: '快速单模型',
    shortLabel: '快速',
    eyebrow: '低延迟',
    description: '重新决策时只运行一次角色面板，适合先验证标的与区间是否值得深入。',
    callProfile: '1 次 / 决策日',
    speed: '最快',
    audit: '单面板',
  },
])

export function architectureOf(value) {
  return ARCHITECTURES.find((item) => item.value === value) || {
    value: value || 'legacy',
    label: '旧版架构',
    shortLabel: '旧版',
    eyebrow: '兼容模式',
    description: '由旧版本创建的会话。',
    callProfile: '未记录',
    speed: '未知',
    audit: '兼容',
  }
}

export function toUserMessage(error, prefix = '操作失败') {
  const detail = error instanceof Error
    ? error.message
    : typeof error === 'string'
      ? error
      : error?.message || '未知错误'
  return `${prefix}：${detail}`
}

export function sessionPayload(form) {
  const payload = { ...form }
  if (payload.commission_rate == null || payload.commission_rate === '') {
    delete payload.commission_rate
  }
  return payload
}

export function formatLocalDate(value) {
  const year = value.getFullYear()
  const month = String(value.getMonth() + 1).padStart(2, '0')
  const day = String(value.getDate()).padStart(2, '0')
  return `${year}-${month}-${day}`
}

export function defaultSessionDates(now = new Date()) {
  const end = new Date(now)
  end.setDate(end.getDate() - 1)
  const start = new Date(now)
  start.setDate(start.getDate() - 30)
  return { start: formatLocalDate(start), end: formatLocalDate(end) }
}

export function createLatestRequestGuard() {
  let current = 0
  return {
    begin() { current += 1; return current },
    isCurrent(token) { return token === current },
    invalidate() { current += 1 },
  }
}

export function decisionSizing(decision = {}) {
  const percent = (value) => Number.isFinite(value) ? Math.round(value * 100) : null
  return {
    actionPercent: percent(decision.position_pct),
    targetPercent: percent(decision.target_position_pct),
  }
}

// `date` is retained for legacy records.  New next-open records deliberately
// expose the three distinct moments so an audit log never labels a T+1 close
// valuation as if it were known at the T-close decision.
export function decisionTimeline(day = {}) {
  const decisionDate = day.decision_date || day.date || null
  const executionDate = day.execution_date || null
  const valuationDate = day.valuation_date || executionDate || decisionDate
  return {
    decisionDate,
    executionDate,
    valuationDate,
    executionLabel: executionDate ? `执行 ${executionDate}` : '无需执行',
    valuationLabel: valuationDate ? `估值 ${valuationDate}` : '估值日期未记录',
  }
}

// Some ledger rows deliberately do not represent the decision's raw action:
// `cooldown` and `coast` are engine states, while `rejected` is an execution
// result. Prefer those states so the list and detail drawer agree.
export function displayedAction(day = {}, decision = {}) {
  return ['cooldown', 'coast', 'rejected'].includes(day.action)
    ? day.action
    : (decision.action || day.action)
}

export const STATUS = {
  created: { text: '已创建', badge: 'b-warn' },
  running: { text: '运行中', badge: 'b-run' },
  paused: { text: '已暂停', badge: 'b-warn' },
  completed: { text: '待复盘', badge: 'b-warn' },
  reviewing: { text: '复盘中', badge: 'b-run' },
  done: { text: '已完成', badge: 'b-done' },
  failed: { text: '失败', badge: 'b-fail' },
  stopped: { text: '已停止', badge: 'b-fail' },
}

export const ACTION = {
  buy: { text: '买入', badge: 'b-buy' },
  sell: { text: '卖出', badge: 'b-sell' },
  hold: { text: '持有', badge: 'b-hold' },
  coast: { text: '沿用决策', badge: 'b-coast' },
  cooldown: { text: '冷静期', badge: 'b-cooldown' },
  rejected: { text: '已拒绝', badge: 'b-rejected' },
}
