<script setup>
import { computed, onMounted, reactive, ref } from 'vue'
import { api, toUserMessage } from '../api'

const skills = ref([])
const categories = ref([])
const filter = reactive({ category: '' })
const loading = ref(true)
const errorText = ref('')

const filtered = computed(() =>
  filter.category ? skills.value.filter((skill) => skill.category === filter.category) : skills.value)
const summary = computed(() => ({
  enabled: skills.value.filter((skill) => skill.enabled).length,
  applied: skills.value.reduce((sum, skill) => sum + (skill.times_applied || 0), 0),
  helpful: skills.value.reduce((sum, skill) => sum + (skill.times_helpful || 0), 0),
}))

function successPct(skill) {
  return Math.round((skill.success_rate || 0) * 100)
}

async function load() {
  try {
    skills.value = (await api.listSkills()).skills
    categories.value = [...new Set(skills.value.map((skill) => skill.category))]
    errorText.value = ''
  } catch (error) {
    errorText.value = toUserMessage(error, '经验库加载失败')
  } finally {
    loading.value = false
  }
}

async function toggle(skill) {
  try {
    await api.updateSkill(skill.id, { enabled: !skill.enabled })
    await load()
  } catch (error) {
    errorText.value = toUserMessage(error, '状态更新失败')
  }
}

async function del(skill) {
  if (!confirm('删除该经验？此操作无法撤销。')) return
  try {
    await api.deleteSkill(skill.id)
    await load()
  } catch (error) {
    errorText.value = toUserMessage(error, '删除失败')
  }
}

onMounted(load)
</script>

<template>
  <header class="page-head">
    <div>
      <p class="eyebrow">Strategy memory</p>
      <h1 class="page-title">策略经验库</h1>
      <p class="page-sub">查看 Agent 从历史交易中提炼的规则，并控制哪些经验可进入后续决策。</p>
    </div>
  </header>

  <div v-if="errorText" class="notice error" role="alert">
    <span>{{ errorText }}</span>
    <button class="icon-btn" aria-label="关闭提示" @click="errorText = ''">×</button>
  </div>

  <section class="overview-strip skill-overview" aria-label="经验库概览">
    <div><span>经验总数</span><strong>{{ skills.length }}</strong></div>
    <div><span>当前启用</span><strong class="accent">{{ summary.enabled }}</strong></div>
    <div><span>累计应用</span><strong>{{ summary.applied }}</strong></div>
    <div><span>有效帮助</span><strong>{{ summary.helpful }}</strong></div>
  </section>

  <section class="panel skill-panel">
    <div class="panel-head skill-toolbar">
      <div>
        <p class="panel-kicker">Learned rules</p>
        <h2>经验条目</h2>
      </div>
      <label class="filter-control">
        <span class="sr-only">按分类筛选</span>
        <select v-model="filter.category">
          <option value="">全部分类</option>
          <option v-for="category in categories" :key="category" :value="category">{{ category }}</option>
        </select>
      </label>
    </div>

    <div class="library-explainer">
      <span class="method-icon" aria-hidden="true">i</span>
      <p>经验从来源会话结束后的下一日生效，并按决策日期过滤，避免将未来复盘结论带入历史决策。</p>
    </div>

    <div v-if="loading" class="skill-list" aria-label="正在加载经验">
      <div v-for="n in 4" :key="n" class="skill skeleton-card">
        <span class="skeleton block-lg"></span><span class="skeleton"></span><span class="skeleton block-lg"></span>
      </div>
    </div>

    <div v-else-if="!filtered.length" class="empty-state">
      <div class="empty-mark book" aria-hidden="true">
        <svg viewBox="0 0 24 24"><path d="M5 5.5A2.5 2.5 0 0 1 7.5 3H19v16H7.5A2.5 2.5 0 0 1 5 16.5zM5 16.5A2.5 2.5 0 0 1 7.5 14H19" /></svg>
      </div>
      <h3>{{ filter.category ? '这个分类暂时没有经验' : '经验库还没有内容' }}</h3>
      <p>{{ filter.category ? '切换到全部分类查看其他条目。' : '完成一轮回测与复盘后，Agent 提炼的第一条经验会出现在这里。' }}</p>
      <button v-if="filter.category" class="btn" @click="filter.category = ''">查看全部分类</button>
      <RouterLink v-else class="btn primary" to="/create">创建回测</RouterLink>
    </div>

    <div v-else class="skill-list">
      <article v-for="skill in filtered" :key="skill.id" class="skill" :class="{ disabled: !skill.enabled }">
        <div class="skill-rail" aria-hidden="true"><span :style="{ height: successPct(skill) + '%' }"></span></div>
        <div class="body">
          <div class="skill-head">
            <span class="category-label">{{ skill.category }}</span>
            <span class="status-text" :class="skill.enabled ? 'enabled' : 'disabled'">
              <i></i>{{ skill.enabled ? '参与决策' : '已停用' }}
            </span>
          </div>
          <p class="skill-statement">{{ skill.statement }}</p>
          <div class="skill-metrics">
            <span><small>来源</small><b>{{ skill.source_ticker || '手动添加' }}</b></span>
            <span><small>创建日期</small><b class="num">{{ (skill.created_at || '').slice(0, 10) || '—' }}</b></span>
            <span><small>应用</small><b class="num">{{ skill.times_applied || 0 }} 次</b></span>
            <span><small>帮助</small><b class="num">{{ skill.times_helpful || 0 }} 次</b></span>
            <span class="success-metric"><small>历史有效率</small><b class="num">{{ successPct(skill) }}%</b></span>
          </div>
        </div>
        <div class="ops">
          <button class="btn quiet" @click="toggle(skill)">{{ skill.enabled ? '停用' : '启用' }}</button>
          <button class="icon-btn danger" aria-label="删除经验" title="删除经验" @click="del(skill)">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 7h14M9 7V4h6v3m2 0-1 13H8L7 7m3 4v5m4-5v5" /></svg>
          </button>
        </div>
      </article>
    </div>
  </section>
</template>
