<script setup>
import { computed } from 'vue'
import VChart from './VChart.vue'
import { BASE, CATEGORY_AXIS, VALUE_AXIS, COLORS } from '../charts/theme'

const props = defineProps({
  equity: { type: Array, default: () => [] },   // [{date, equity, cash}]
  initial: { type: Number, default: 0 },
})
const height = 200

const option = computed(() => {
  if (!props.equity.length) return {}
  const dates = props.equity.map((e) => e.date)
  const mkLine = (key, name, color, dashed = false) => ({
    name, type: 'line', data: props.equity.map((e) => e[key]),
    lineStyle: { color, width: key === 'equity' ? 2 : 1, type: dashed ? 'dashed' : 'solid' },
    itemStyle: { color }, showSymbol: false, emphasis: { focus: 'series' },
  })
  return {
    ...BASE,
    legend: undefined, // 单序列无需图例，标题即命名
    grid: { left: 56, right: 16, top: 14, bottom: 26 },
    tooltip: { ...BASE.tooltip, trigger: 'axis', axisPointer: { type: 'cross', label: { backgroundColor: '#192330' } } },
    xAxis: { type: 'category', data: dates, ...CATEGORY_AXIS },
    yAxis: { type: 'value', scale: true, ...VALUE_AXIS },
    series: [
      mkLine('equity', '账户权益', COLORS.accent),
      mkLine('cash', '现金', COLORS.dim, true),
      {
        name: '初始资金', type: 'line', data: dates.map(() => props.initial), symbol: 'none',
        lineStyle: { color: '#647286', width: 1, type: 'dotted' }, tooltip: { show: false },
      },
    ],
  }
})
</script>

<template>
  <VChart :option="option" :height="height" />
</template>
