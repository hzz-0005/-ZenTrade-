<script setup>
import { computed } from 'vue'
import VChart from './VChart.vue'
import { BASE, CATEGORY_AXIS, VALUE_AXIS, LEGEND, COLORS } from '../charts/theme'
import { markerPlotPrice } from '../charts/data'

const props = defineProps({
  kline: { type: Array, default: () => [] },      // [{date,open,high,low,close}]
  markers: { type: Array, default: () => [] },    // [{date,action,price,shares}]
  sessionStart: { type: String, default: '' },    // 用户选定区间（高亮 + 聚焦）
  sessionEnd: { type: String, default: '' },
})
const height = 360

const data = computed(() => {
  const dates = props.kline.map((k) => k.date)
  const closeByDate = Object.fromEntries(props.kline.map((k) => [k.date, k.close]))
  const idx = (date) => dates.indexOf(date)
  const inRange = (m) => idx(m.date) >= 0
  return {
    dates,
    candles: props.kline.map((k) => [k.open, k.close, k.low, k.high]),
    buys: props.markers.filter((m) => m.action === 'buy' && m.price && inRange(m)),
    sells: props.markers.filter((m) => m.action === 'sell' && m.price && inRange(m)),
    rejects: props.markers
      .filter((m) => m.action === 'rejected' && inRange(m))
      .map((m) => ({ ...m, plotPrice: markerPlotPrice(m, closeByDate) }))
      .filter((m) => m.plotPrice != null),
    startIdx: props.sessionStart ? idx(props.sessionStart) : -1,
    endIdx: props.sessionEnd ? idx(props.sessionEnd) : -1,
  }
})

/** 视窗聚焦到用户选定的区间（前后各留少量上下文）；预热的更早数据仍可向左拖出 */
const zoom = computed(() => {
  const { dates, startIdx, endIdx } = data.value
  const n = dates.length
  if (!n) return [{ type: 'inside' }, { type: 'slider', height: 16, bottom: 2 }]
  let lo = 0, hi = n - 1
  if (startIdx >= 0) lo = Math.max(0, startIdx - 5)
  if (endIdx >= 0) hi = Math.min(n - 1, endIdx + 2)
  const toPct = (i) => Math.round((i / Math.max(n - 1, 1)) * 1000) / 10
  return [
    { type: 'inside', start: toPct(lo), end: toPct(hi) },
    { type: 'slider', height: 16, bottom: 2, start: toPct(lo), end: toPct(hi),
      borderColor: '#2a3848', fillerColor: 'rgba(108,156,255,.12)',
      textStyle: { color: '#7f8da0', fontSize: 10 } },
  ]
})

const option = computed(() => {
  const d = data.value
  if (!d.dates.length) return {}
  const idx = (date) => d.dates.indexOf(date)
  const markArea = d.startIdx >= 0 && d.endIdx >= 0
    ? {
        silent: true,
        data: [[
          { xAxis: d.dates[d.startIdx], itemStyle: { color: 'rgba(108,156,255,.05)' } },
          { xAxis: d.dates[d.endIdx] },
        ]],
      }
    : undefined
  return {
    ...BASE,
    legend: { ...LEGEND, data: ['买入', '卖出', '已拒绝'], top: 0, right: 0 },
    grid: { left: 56, right: 16, top: 30, bottom: 46 },
    tooltip: { ...BASE.tooltip, trigger: 'axis', axisPointer: { type: 'cross', label: { backgroundColor: '#192330' } } },
    xAxis: { type: 'category', data: d.dates, ...CATEGORY_AXIS, boundaryGap: true },
    yAxis: { type: 'value', scale: true, ...VALUE_AXIS },
    dataZoom: zoom.value,
    series: [
      {
        name: 'K线', type: 'candlestick', data: d.candles, markArea,
        itemStyle: { color: COLORS.up, color0: COLORS.down, borderColor: COLORS.up, borderColor0: COLORS.down },
      },
      {
        name: '买入', type: 'scatter', symbol: 'triangle', symbolSize: 13,
        itemStyle: { color: COLORS.buy },
        data: d.buys.map((b) => ({ value: [idx(b.date), b.price * 0.995] })),
        tooltip: { ...BASE.tooltip, formatter: (p) => `**买入** ${d.buys[p.dataIndex]?.shares ?? ''}股 @${d.buys[p.dataIndex]?.price}` },
      },
      {
        name: '卖出', type: 'scatter', symbol: 'triangle', symbolRotate: 180, symbolSize: 13,
        itemStyle: { color: COLORS.sell },
        data: d.sells.map((s) => ({ value: [idx(s.date), s.price * 1.005] })),
        tooltip: { ...BASE.tooltip, formatter: (p) => `**卖出** ${d.sells[p.dataIndex]?.shares ?? ''}股 @${d.sells[p.dataIndex]?.price}` },
      },
      {
        name: '已拒绝', type: 'scatter', symbol: 'diamond', symbolSize: 9,
        itemStyle: { color: 'transparent', borderColor: COLORS.dim, borderWidth: 1.5 },
        data: d.rejects.map((r) => ({ value: [idx(r.date), r.plotPrice] })),
      },
    ],
  }
})
</script>

<template>
  <VChart :option="option" :height="height" />
</template>
