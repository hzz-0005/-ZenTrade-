<script setup>
import { onBeforeUnmount, onMounted, ref } from 'vue'
import { RouterLink, RouterView } from 'vue-router'
import { api } from './api'

const backend = ref({ label: '正在连接', state: 'pending' })
let healthTimer = null

async function refreshBackend() {
  try {
    const health = await api.health()
    backend.value = health.db_ok
      ? { label: health.model || '服务正常', state: 'online' }
      : { label: '数据库异常', state: 'error' }
  } catch {
    backend.value = { label: '后端离线', state: 'error' }
  }
}

onMounted(() => {
  refreshBackend()
  healthTimer = setInterval(refreshBackend, 30_000)
})
onBeforeUnmount(() => clearInterval(healthTimer))
</script>

<template>
  <a class="skip-link" href="#main-content">跳到主要内容</a>
  <div class="shell">
    <aside class="sidebar" aria-label="主要导航">
      <RouterLink class="brand" to="/" aria-label="TradingAgents 首页">
        <span class="brand-mark" aria-hidden="true"><i></i><i></i><i></i></span>
        <span class="brand-copy">
          <strong>TradingAgents</strong>
          <small>Research Terminal</small>
        </span>
      </RouterLink>

      <nav class="side-nav" aria-label="研究工作台">
        <p class="nav-caption">研究工作台</p>
        <RouterLink class="nav-item" to="/">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5h16v5H4zM4 14h7v5H4zM15 14h5v5h-5z" /></svg>
          <span>回测会话</span>
        </RouterLink>
        <RouterLink class="nav-item" to="/create">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 5v14M5 12h14" /></svg>
          <span>新建回测</span>
        </RouterLink>
        <RouterLink class="nav-item" to="/skills">
          <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 6.5A2.5 2.5 0 0 1 7.5 4H19v15H7.5A2.5 2.5 0 0 1 5 16.5zM5 16.5A2.5 2.5 0 0 1 7.5 14H19" /></svg>
          <span>策略经验库</span>
        </RouterLink>
      </nav>

      <div class="sidebar-foot">
        <div class="engine-card">
          <div class="engine-head">
            <span class="status-light" :class="backend.state" aria-hidden="true"></span>
            <span>本地研究引擎</span>
          </div>
          <strong role="status" aria-live="polite">{{ backend.label }}</strong>
          <small>历史数据 · 决策审计 · 经验闭环</small>
        </div>
        <p>仅供研究，不构成投资建议</p>
      </div>
    </aside>

    <main id="main-content" class="main" tabindex="-1">
      <RouterView />
    </main>
  </div>
</template>
