// Dark only, like the rest of the viewer. Validated against the panel surface
// #151923 (dataviz validator: categorical all-pairs, diverging pair).
export const INK = { primary: '#f0efec', secondary: '#c3c2b7', muted: '#898781', grid: '#2c2c2a', axis: '#383835' }
export const SURFACE = '#151923'

// Each slippage evaluation keeps its colour wherever it appears.
export const SLIPPAGE_COLOR = { 0: '#199e70', 5: '#3987e5', 10: '#d95926' }
export const SLIPPAGE_LABEL = { 0: 'pnl0 (no extra slippage)', 5: 'pnl5 (0.005%)', 10: 'pnl10 (0.01%)' }

// Polarity: gain and loss, the diverging pair with a gray midpoint.
export const GAIN = '#3987e5'
export const LOSS = '#e66767'
export const NEUTRAL = '#383835'

export const fmt = (v, digits = 2) =>
  v == null || Number.isNaN(v) ? '—' : Number(v).toLocaleString(undefined, { maximumFractionDigits: digits, minimumFractionDigits: digits })

export const axisStyle = {
  axisLine: { lineStyle: { color: INK.axis } },
  axisTick: { show: false },
  axisLabel: { color: INK.muted, fontSize: 11 },
  splitLine: { lineStyle: { color: INK.grid } },
}

export const tooltipStyle = {
  backgroundColor: '#0f1117',
  borderColor: '#2d3748',
  textStyle: { color: INK.primary, fontSize: 12 },
  confine: true,
}

export const legendStyle = { textStyle: { color: INK.secondary, fontSize: 11 }, top: 0, right: 8, itemHeight: 8, itemWidth: 14 }

export const base = { backgroundColor: 'transparent', animation: false, textStyle: { fontFamily: 'ui-monospace, monospace' } }
