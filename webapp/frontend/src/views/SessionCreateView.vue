<script setup>
import { computed, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { useRouter } from 'vue-router'
import { api, ARCHITECTURES, architectureOf, createLatestRequestGuard, defaultSessionDates, fmt, sessionPayload, toUserMessage } from '../api'

const router = useRouter()
const form = reactive({
  ticker: '600519.SS',
  initial_capital: 100000,
  start_date: '',
  end_date: '',
  commission_rate: null,
  decision_architecture: 'adaptive',
})
const creating = ref(false)
const errorText = ref('')
const cfg = ref(null)
const stockName = ref(null)
const nameState = ref('idle')
let nameTimer = null
const nameGuard = createLatestRequestGuard()

const selectedArchitecture = computed(() => architectureOf(form.decision_architecture))
const capitalText = computed(() => `¥ ${fmt.money(form.initial_capital)}`)

watch(() => form.ticker, (ticker) => {
  const requestToken = nameGuard.begin()
  clearTimeout(nameTimer)
  stockName.value = null
  nameState.value = 'idle'
  if (!ticker || !ticker.trim()) return
  nameTimer = setTimeout(async () => {
    nameState.value = 'loading'
    try {
      const result = await api.stockName(ticker.trim())
      if (!nameGuard.isCurrent(requestToken)) return
      stockName.value = result.name
      nameState.value = 'ok'
    } catch {
      if (!nameGuard.isCurrent(requestToken)) return
      stockName.value = null
      nameState.value = 'miss'
    }
  }, 600)
}, { immediate: true })

async function submit() {
  creating.value = true
  errorText.value = ''
  try {
    const session = await api.createSession(sessionPayload(form))
    router.push(`/sessions/${session.id}`)
  } catch (error) {
    errorText.value = toUserMessage(error, '创建失败')
  } finally {
    creating.value = false
  }
}

onMounted(async () => {
  const dates = defaultSessionDates()
  form.end_date = dates.end
  form.start_date = dates.start
  cfg.value = await api.config().catch(() => null)
})
onBeforeUnmount(() => {
  clearTimeout(nameTimer)
  nameGuard.invalidate()
})
</script>

<template>
  <header class="page-head compact">
    <div>
      <p class="eyebrow">New research run</p>
      <h1 class="page-title">配置一轮回测</h1>
      <p class="page-sub">先确定研究边界，再选择与你的时间预算相匹配的决策架构。</p>
    </div>
  </header>

  <div class="create-layout">
    <section class="panel form-panel">
      <form @submit.prevent="submit">
        <div class="form-section">
          <div class="section-head">
            <span class="section-index">01</span>
            <div><h2>标的与资金</h2><p>输入交易所后缀可以帮助系统准确识别市场规则。</p></div>
          </div>

          <div class="field-grid target-grid">
            <label class="field">
              <span>股票代码</span>
              <input v-model.trim="form.ticker" required autocomplete="off" placeholder="例如 600519.SS、0700.HK、NVDA" />
              <small v-if="nameState === 'ok'" class="field-status success">
                <span aria-hidden="true">✓</span>{{ stockName }}
              </small>
              <small v-else-if="nameState === 'loading'" class="field-status loading">
                <span class="spinner" aria-hidden="true"></span>正在识别标的，首次载入 A 股名称表可能稍慢
              </small>
              <small v-else-if="nameState === 'miss'" class="field-status warning">
                未找到名称，仍可继续；请确认代码和交易所后缀
              </small>
            </label>
            <label class="field">
              <span>初始资金</span>
              <div class="input-prefix"><span>¥</span><input v-model.number="form.initial_capital" type="number" min="1" required /></div>
            </label>
          </div>
        </div>

        <div class="form-section">
          <div class="section-head">
            <span class="section-index">02</span>
            <div><h2>回测边界</h2><p>系统只会使用截至模拟日可取得的资料，并采用保守披露日估计。</p></div>
          </div>
          <div class="field-grid period-grid">
            <label class="field"><span>开始日期</span><input v-model="form.start_date" type="date" required /></label>
            <label class="field"><span>结束日期</span><input v-model="form.end_date" type="date" required /></label>
            <label class="field"><span>佣金费率</span><input v-model.number="form.commission_rate" type="number" min="0" step="0.0001" :placeholder="cfg ? String(cfg.default_commission_rate) : '使用系统默认'" /></label>
          </div>
        </div>

        <div class="form-section architecture-section">
          <div class="section-head">
            <span class="section-index">03</span>
            <div><h2>决策架构</h2><p>同一回测只能使用一种架构，创建后不可切换。</p></div>
          </div>
          <div class="architecture-grid">
            <label v-for="(option, index) in ARCHITECTURES" :key="option.value"
                   class="architecture-card" :class="{ selected: form.decision_architecture === option.value }">
              <input v-model="form.decision_architecture" class="radio-input" type="radio" :value="option.value" />
              <span class="arch-card-top">
                <span class="arch-number">0{{ index + 1 }}</span>
                <span class="arch-eyebrow">{{ option.eyebrow }}</span>
                <span class="selection-mark" aria-hidden="true"></span>
              </span>
              <strong>{{ option.label }}</strong>
              <span class="arch-description">{{ option.description }}</span>
              <span class="arch-specs">
                <small><b>{{ option.speed }}</b>速度</small>
                <small><b>{{ option.callProfile }}</b>调用画像</small>
                <small><b>{{ option.audit }}</b>审计深度</small>
              </span>
            </label>
          </div>
        </div>

        <div v-if="errorText" class="notice error" role="alert">
          <span>{{ errorText }}</span>
          <button class="icon-btn" type="button" aria-label="关闭提示" @click="errorText = ''">×</button>
        </div>

        <div class="form-actions">
          <p>创建后会直接进入运行详情页，你可以随时暂停或终止。</p>
          <button class="btn primary submit-btn" :disabled="creating">
            <span v-if="creating" class="spinner light" aria-hidden="true"></span>
            {{ creating ? '正在启动研究…' : '启动回测' }}
            <svg v-if="!creating" viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14m-5-5 5 5-5 5" /></svg>
          </button>
        </div>
      </form>
    </section>

    <aside class="setup-aside" aria-label="本次运行摘要">
      <div class="summary-card">
        <p class="panel-kicker">Run summary</p>
        <h2>本次运行</h2>
        <div class="summary-asset">
          <span>{{ form.ticker || '—' }}</span>
          <small>{{ stockName || '等待标的确认' }}</small>
        </div>
        <dl class="summary-list">
          <div><dt>初始资金</dt><dd class="num">{{ capitalText }}</dd></div>
          <div><dt>研究区间</dt><dd class="num">{{ form.start_date || '—' }}<br>→ {{ form.end_date || '—' }}</dd></div>
          <div><dt>决策架构</dt><dd>{{ selectedArchitecture.label }}</dd></div>
          <div><dt>调用画像</dt><dd>{{ selectedArchitecture.callProfile }}</dd></div>
        </dl>
        <div v-if="cfg" class="capacity-note">
          最多同时运行 {{ cfg.max_concurrent_sessions }} 个会话<br>
          单次区间上限 {{ cfg.max_session_days }} 个交易日
        </div>
      </div>

      <div class="method-note">
        <span class="method-icon" aria-hidden="true">i</span>
        <div>
          <strong>研究口径说明</strong>
          <p>当前引擎读取 T 日收盘信息并按 T 日收盘成交，是便于策略比较的研究近似。用于实盘时，应把结果视为下一时段计划。</p>
        </div>
      </div>
    </aside>
  </div>
</template>
