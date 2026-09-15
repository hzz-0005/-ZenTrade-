<script setup>
import { computed, onBeforeUnmount, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { api, toUserMessage } from '../api'
import SessionTableRow from '../components/SessionTableRow.vue'

const router = useRouter()
const sessions = ref([])
const loading = ref(true)
const errorText = ref('')
let timer = null

const overview = computed(() => ({
  total: sessions.value.length,
  live: sessions.value.filter((item) => ['created', 'running', 'paused', 'reviewing'].includes(item.status)).length,
  done: sessions.value.filter((item) => item.status === 'done').length,
}))

async function load() {
  try {
    sessions.value = (await api.listSessions('?limit=200')).sessions
    errorText.value = ''
  } catch (error) {
    errorText.value = toUserMessage(error, '会话加载失败')
  } finally {
    loading.value = false
  }
}

async function del(id) {
  if (!confirm('删除该会话及其全部记录？此操作无法撤销。')) return
  try {
    await api.deleteSession(id)
    await load()
  } catch (error) {
    errorText.value = toUserMessage(error, '删除失败')
  }
}

onMounted(() => {
  load()
  timer = setInterval(load, 4000)
})
onBeforeUnmount(() => clearInterval(timer))
</script>

<template>
  <header class="page-head">
    <div>
      <p class="eyebrow">Backtest desk</p>
      <h1 class="page-title">回测会话</h1>
      <p class="page-sub">集中查看研究进度、组合表现与每个交易日的决策依据。</p>
    </div>
    <RouterLink class="btn primary head-action" to="/create">
      <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
      新建回测
    </RouterLink>
  </header>

  <div v-if="errorText" class="notice error" role="alert">
    <span>{{ errorText }}</span>
    <button class="icon-btn" aria-label="关闭提示" @click="errorText = ''">×</button>
  </div>

  <section class="overview-strip" aria-label="会话概览">
    <div><span>已载入会话</span><strong>{{ overview.total }}</strong></div>
    <div><span>正在处理</span><strong class="accent">{{ overview.live }}</strong></div>
    <div><span>已完成复盘</span><strong>{{ overview.done }}</strong></div>
    <div class="overview-note">
      <span class="live-pulse" aria-hidden="true"></span>
      运行中的会话每 4 秒自动同步
    </div>
  </section>

  <section class="panel data-panel">
    <div class="panel-head">
      <div>
        <p class="panel-kicker">Session ledger</p>
        <h2>研究记录</h2>
      </div>
      <span class="panel-meta">{{ sessions.length }} 个会话</span>
    </div>

    <div v-if="loading" class="session-skeleton" aria-label="正在加载会话">
      <div v-for="n in 5" :key="n" class="skeleton-row">
        <span class="skeleton block-lg"></span><span class="skeleton"></span><span class="skeleton"></span><span class="skeleton block-lg"></span>
      </div>
    </div>

    <div v-else-if="!sessions.length" class="empty-state">
      <div class="empty-mark" aria-hidden="true">
        <svg viewBox="0 0 24 24"><path d="M4 17l5-5 4 3 7-8M17 7h3v3" /></svg>
      </div>
      <h3>从第一轮研究开始</h3>
      <p>选择标的、时间区间与决策架构，系统会逐日生成可审计的交易决策。</p>
      <RouterLink class="btn primary" to="/create">创建回测</RouterLink>
    </div>

    <div v-else class="table-wrap">
      <table class="session-table">
        <thead>
          <tr>
            <th>研究标的</th><th>决策架构</th><th>回测区间</th><th>状态</th><th>进度</th>
            <th>总收益</th><th>最大回撤</th><th>最终权益</th><th><span class="sr-only">操作</span></th>
          </tr>
        </thead>
        <tbody>
          <SessionTableRow v-for="session in sessions" :key="session.id" :session="session"
                           @open="router.push(`/sessions/${session.id}`)" @delete="del(session.id)" />
        </tbody>
      </table>
    </div>
  </section>

  <p class="research-note">研究口径：历史回测使用收盘数据近似模拟，同日信号与成交价格存在理想化假设；实盘应视作下一时段计划。</p>
</template>
