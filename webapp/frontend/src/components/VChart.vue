<script setup>
import { onMounted, onBeforeUnmount, ref, watch } from 'vue'
import { init, use } from 'echarts/core'
import { CandlestickChart, LineChart, ScatterChart } from 'echarts/charts'
import {
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  TooltipComponent,
} from 'echarts/components'
import { CanvasRenderer } from 'echarts/renderers'

use([
  CandlestickChart,
  LineChart,
  ScatterChart,
  DataZoomComponent,
  GridComponent,
  LegendComponent,
  MarkAreaComponent,
  TooltipComponent,
  CanvasRenderer,
])

const props = defineProps({
  option: { type: Object, required: true },
  height: { type: Number, default: 300 },
})
const el = ref(null)
let chart = null
let ro = null

function render() {
  if (!el.value) return
  chart = chart || init(el.value)
  chart.setOption(props.option, true)
}

onMounted(() => {
  render()
  ro = new ResizeObserver(() => chart && chart.resize())
  ro.observe(el.value)
})
onBeforeUnmount(() => {
  ro && ro.disconnect()
  chart && chart.dispose()
  chart = null
})
watch(() => props.option, render, { deep: true })
</script>

<template>
  <div ref="el" :style="{ width: '100%', height: height + 'px' }" />
</template>
