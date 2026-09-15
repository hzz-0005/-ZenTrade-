/** 全站共享的 ECharts 主题令牌（dataviz 规范：隐退网格/坐标轴、文本用文本色、已验证配色） */
export const COLORS = {
  accent: '#6c9cff',
  buy: '#6c9cff',
  sell: '#d5a84f',
  up: '#f06a68',
  down: '#3fb99f',
  dim: '#8b98aa',
  grid: '#1a2430',
  axis: '#273342',
}

export const BASE = {
  backgroundColor: 'transparent',
  textStyle: {
    fontFamily: "'Microsoft YaHei','Microsoft YaHei UI','PingFang SC',Arial,sans-serif",
    fontWeight: 500,
  },
  tooltip: {
    backgroundColor: '#141d28',
    borderColor: '#2a3848',
    textStyle: { color: '#edf2f8', fontSize: 12, fontWeight: 500 },
    extraCssText: 'box-shadow: 0 16px 40px rgba(0,0,0,.36); border-radius: 8px;',
  },
}

export const CATEGORY_AXIS = {
  axisLine: { lineStyle: { color: COLORS.axis } },
  axisTick: { show: false },
  axisLabel: { color: COLORS.dim, fontSize: 11, fontWeight: 500 },
}

export const VALUE_AXIS = {
  axisLine: { show: false },
  axisLabel: { color: COLORS.dim, fontSize: 11, fontWeight: 500 },
  splitLine: { lineStyle: { color: COLORS.grid } },
  nameTextStyle: { color: '#7f8da0', fontSize: 11, fontWeight: 500 },
}

export const LEGEND = {
  textStyle: { color: '#8b98aa', fontSize: 11, fontWeight: 500 },
  itemWidth: 12,
  itemHeight: 8,
  icon: 'roundRect',
}
