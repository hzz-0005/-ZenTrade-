<script setup>
import { architectureOf, fmt } from '../api'
import StateBadge from './StateBadge.vue'

const props = defineProps({
  session: { type: Object, required: true },
})
const emit = defineEmits(['open', 'delete'])

function progressOf() {
  const total = (props.session.trading_days || []).length
  return total ? Math.min(100, Math.round((props.session.current_day_index / total) * 100)) : 0
}
</script>

<template>
  <tr class="click" @click="emit('open')">
    <td>
      <RouterLink class="asset-cell session-link" :to="`/sessions/${session.id}`" @click.stop>
        <span class="asset-monogram">{{ session.canonical_ticker.slice(0, 2) }}</span>
        <span><b>{{ session.canonical_ticker }}</b><small>{{ session.market.toUpperCase() }} MARKET</small></span>
      </RouterLink>
    </td>
    <td><span class="arch-tag">{{ architectureOf(session.decision_architecture).shortLabel }}</span></td>
    <td class="num date-cell"><b>{{ session.start_date }}</b><span>至 {{ session.end_date }}</span></td>
    <td><StateBadge :status="session.status" /></td>
    <td>
      <div class="row-progress">
        <span class="num">{{ session.current_day_index }}/{{ (session.trading_days || []).length || '…' }}</span>
        <div class="progress"><i :style="{ width: progressOf() + '%' }"></i></div>
      </div>
    </td>
    <td class="num result-value" :class="fmt.cls(session.total_return_pct)">{{ fmt.pct(session.total_return_pct) }}</td>
    <td class="num muted">{{ session.max_drawdown_pct != null ? session.max_drawdown_pct + '%' : '—' }}</td>
    <td class="num equity-cell">{{ fmt.money(session.final_equity ?? session.cash) }}</td>
    <td>
      <button class="icon-btn danger" :aria-label="`删除 ${session.canonical_ticker} 会话`" title="删除会话" @click.stop="emit('delete')">
        <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 7h14M9 7V4h6v3m2 0-1 13H8L7 7m3 4v5m4-5v5" /></svg>
      </button>
    </td>
  </tr>
</template>
