import { useCallback, useEffect, useMemo, useState } from 'react'
import { fetchPrices, fetchRun, fetchRunFile } from '../api'
import { parseCsv } from '../utils/csv'
import { Panel, Toggle, Tile, KeyValues } from './Pieces'
import Chart from '../charts/Chart'
import {
  INK, SLIPPAGE_COLOR, SLIPPAGE_LABEL, GAIN, LOSS, NEUTRAL,
  fmt, axisStyle, tooltipStyle, legendStyle, base,
} from '../charts/theme'

const SLIPPAGES = [0, 5, 10]
const GROUPINGS = [
  ['month', 'Month'], ['week', 'Week'], ['day', 'Day'], ['weekday', 'Weekday'],
  ['monthday', 'Day of month'], ['hour', 'Hour (JST)'], ['holding', 'Holding time'], ['side', 'Side'],
]
const TIME_GROUPINGS = ['month', 'week', 'day', 'weekday', 'monthday', 'hour', 'weekday_hour']
// Every grouping is recomputed from the trades in the selected range; these list
// only the periods that traded, and the rest keep by_*.json's keys and order.
const CALENDAR_GROUPINGS = ['month', 'week', 'day']
const WEEKDAYS = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']
// Enoshima's ledger.HOLDING_BUCKETS: upper bounds in seconds.
const HOLDING_BUCKETS = [[60, '<1m'], [300, '1-5m'], [1800, '5-30m'], [7200, '30m-2h'], [28800, '2-8h'], [Infinity, '>8h']]
// eval.py's extra slippage rates, for a trade's return.
const SLIPPAGE_RATE = { 0: 0, 5: 0.00005, 10: 0.0001 }
const JST = 9 * 3600

const pad = n => String(n).padStart(2, '0')
function fmtTime(ms) {
  const d = new Date(ms)
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())} ${pad(d.getHours())}:${pad(d.getMinutes())}`
}
function fmtDuration(s) {
  if (s == null) return '—'
  if (s < 60) return `${s.toFixed(0)}s`
  if (s < 3600) return `${(s / 60).toFixed(1)}m`
  return `${(s / 3600).toFixed(1)}h`
}

function tradeTimeGroups(timestamp) {
  const date = new Date((timestamp + 9 * 3600) * 1000)
  const year = date.getUTCFullYear()
  const month = pad(date.getUTCMonth() + 1)
  const day = pad(date.getUTCDate())
  const hour = pad(date.getUTCHours())
  const weekday = WEEKDAYS[date.getUTCDay()]
  const isoDate = new Date(Date.UTC(year, date.getUTCMonth(), date.getUTCDate()))
  const isoDay = isoDate.getUTCDay() || 7
  isoDate.setUTCDate(isoDate.getUTCDate() + 4 - isoDay)
  const isoYear = isoDate.getUTCFullYear()
  const week = Math.ceil((((isoDate - Date.UTC(isoYear, 0, 1)) / 86400000) + 1) / 7)
  return {
    month: `${year}-${month}`,
    week: `${isoYear}-W${pad(week)}`,
    day: `${year}-${month}-${day}`,
    weekday,
    monthday: day,
    hour,
    weekday_hour: `${weekday} ${hour}`,
  }
}

function holdingBucket(t) {
  if (t.open_ts == null) return 'unknown'
  return HOLDING_BUCKETS.find(([bound]) => t.close_ts - t.open_ts < bound)[1]
}

// A JST date as YYYY-MM-DD, and back to the timestamp of its midnight.
function jstDate(ts) { return new Date((ts + JST) * 1000).toISOString().slice(0, 10) }
function jstMidnight(date) { return Date.parse(`${date}T00:00:00Z`) / 1000 - JST }

function emptyGroup() {
  const row = { trades: 0 }
  for (const s of SLIPPAGES) Object.assign(row, { [`wins${s}`]: 0, [`pnl${s}`]: 0, [`peak${s}`]: 0, [`trough${s}`]: 0, [`dd${s}`]: 0 })
  return row
}

function groupByOpenTime(trades, templates) {
  const names = Object.keys(templates)
  const groups = Object.fromEntries(names.map(name => [
    name,
    CALENDAR_GROUPINGS.includes(name) ? {} : Object.fromEntries(Object.keys(templates[name]).map(key => [key, emptyGroup()])),
  ]))
  // By open time; a trade with no open still counts in holding and side.
  const opened = t => t.open_ts ?? t.close_ts
  for (const trade of [...trades].sort((a, b) => opened(a) - opened(b))) {
    const labels = { ...(trade.open_ts != null && tradeTimeGroups(trade.open_ts)), holding: holdingBucket(trade), side: trade.side }
    for (const name of names) {
      if (labels[name] == null) continue
      const rows = groups[name]
      const row = rows[labels[name]] ??= emptyGroup()
      row.trades++
      for (const s of SLIPPAGES) {
        const change = trade[`pnl_change${s}`]
        if (change == null) continue
        row[`wins${s}`] += change > 0 ? 1 : 0
        row[`pnl${s}`] += change
        row[`peak${s}`] = Math.max(row[`peak${s}`], row[`pnl${s}`])
        row[`trough${s}`] = Math.min(row[`trough${s}`], row[`pnl${s}`])
        row[`dd${s}`] = Math.max(row[`dd${s}`], row[`peak${s}`] - row[`pnl${s}`])
      }
    }
  }
  for (const name of CALENDAR_GROUPINGS) {
    if (groups[name]) groups[name] = Object.fromEntries(Object.entries(groups[name]).sort(([a], [b]) => (a < b ? -1 : 1)))
  }
  return groups
}

// The summary tiles' figures over a range's trades, as Enoshima computes them for the whole run.
function rangeSummary(trades, s) {
  const changes = trades.map(t => t[`pnl_change${s}`])
  let cum = 0, high = 0, dd = 0, streak = 0, longest = 0
  for (const c of changes) {
    cum += c; high = Math.max(high, cum); dd = Math.max(dd, high - cum)
    streak = c > 0 ? 0 : streak + 1; longest = Math.max(longest, streak)
  }
  const wins = changes.filter(c => c > 0).length
  return {
    [`pnl${s}`]: cum, [`pnl_cnt${s}`]: changes.length, [`max_drawdown${s}`]: dd, [`max_loss_streak${s}`]: longest,
    [`win_rate${s}`]: changes.length ? (100 * wins) / changes.length : 0,
    [`sharpe_ratio_annualized${s}`]: sharpe(trades, s),
  }
}

// eval.evaluate_strategy: per-trade return on open cost, annualized by the mean gap between closes.
function sharpe(trades, s) {
  if (trades.length < 2) return 0
  const returns = trades.map(t => (t[`pnl_change${s}`] / t.open_cost) * (1 + SLIPPAGE_RATE[s]))
  const ts = trades.map(t => t.close_ts).sort((a, b) => a - b)
  const gap = (ts[ts.length - 1] - ts[0]) / (ts.length - 1)
  const mean = returns.reduce((a, b) => a + b, 0) / returns.length
  const sd = Math.sqrt(returns.reduce((a, r) => a + (r - mean) ** 2, 0) / (returns.length - 1))
  if (sd === 0) return Infinity
  return gap > 0 ? (mean / sd) * Math.sqrt((365.25 * 86400) / gap) : 0
}

// ── derived series ────────────────────────────────────────────────────────

function equitySeries(trades, s) {
  let cum = 0, high = 0
  const equity = [], underwater = []
  for (const t of trades) {
    cum += t[`pnl_change${s}`]
    high = Math.max(high, cum)
    equity.push([t.close_ts * 1000, cum])
    underwater.push([t.close_ts * 1000, cum - high])
  }
  return { equity, underwater }
}

function niceStep(raw) {
  const p = 10 ** Math.floor(Math.log10(raw))
  const m = raw / p
  return (m <= 1 ? 1 : m <= 2 ? 2 : m <= 2.5 ? 2.5 : m <= 5 ? 5 : 10) * p
}

// Bins with zero on an edge, so no bin mixes gains and losses.
function histogram(values, target = 40) {
  if (!values.length) return []
  const lo = Math.min(...values), hi = Math.max(...values)
  if (lo === hi) return [{ from: lo, to: hi, count: values.length }]
  const step = niceStep((hi - lo) / target)
  const start = Math.floor(lo / step) * step
  const n = Math.max(1, Math.ceil((hi - start) / step + 1e-9))
  const bins = Array.from({ length: n }, (_, i) => ({ from: start + i * step, to: start + (i + 1) * step, count: 0 }))
  for (const v of values) bins[Math.min(n - 1, Math.floor((v - start) / step))].count++
  return bins
}

// ── chart options ─────────────────────────────────────────────────────────

const PRICE_COLOR = INK.secondary
// Room for the equity's end labels and, beyond them, the price axis; the drawdown below matches it to line up.
const EQUITY_RIGHT = 120

// The whole run, shown through `zoom`: the page's range, as [start, end] ms or null for all of it.
function equityOption(trades, selected, market, zoom) {
  const series = SLIPPAGES.map(s => ({
    name: SLIPPAGE_LABEL[s], type: 'line', showSymbol: false, sampling: 'lttb',
    data: equitySeries(trades, s).equity,
    lineStyle: { width: s === selected ? 2 : 1.25, color: SLIPPAGE_COLOR[s], opacity: s === selected ? 1 : 0.55 },
    itemStyle: { color: SLIPPAGE_COLOR[s] },
    emphasis: { disabled: true },
    endLabel: { show: true, color: INK.secondary, fontSize: 11, formatter: p => fmt(p.value[1], 0) },
  }))
  // The market's price on the right axis, behind the equity curves.
  const prices = market?.prices ?? []
  if (prices.length) series.push({
    name: market.market, type: 'line', yAxisIndex: 1, showSymbol: false, data: prices, z: 1,
    lineStyle: { width: 1, color: PRICE_COLOR, opacity: 0.45 }, itemStyle: { color: PRICE_COLOR },
    emphasis: { disabled: true },
  })
  return {
    ...base,
    legend: { ...legendStyle, data: series.map(x => x.name) },
    grid: { left: 64, right: EQUITY_RIGHT, top: 28, bottom: 28 },
    tooltip: {
      ...tooltipStyle, trigger: 'axis', axisPointer: { type: 'line', lineStyle: { color: INK.muted } },
      formatter: ps => `${fmtTime(ps[0].value[0])}<br/>` + ps.map(p => `${p.marker}${p.seriesName}: <b>${fmt(p.value[1])}</b>`).join('<br/>'),
    },
    xAxis: timeAxis(trades),
    yAxis: [
      { type: 'value', ...axisStyle, name: 'cumulative PnL', nameTextStyle: { color: INK.muted, fontSize: 11 } },
      // Unnamed: the legend names it, and a name here would sit under the legend.
      { type: 'value', ...axisStyle, show: prices.length > 0, scale: true, splitLine: { show: false }, position: 'right', offset: 60 },
    ],
    dataZoom: [{ type: 'inside', ...zoomWindow(zoom) }],
    series,
  }
}

// Both charts span the run's first close to its last, so their linked windows line up.
function timeAxis(trades) {
  const axis = { type: 'time', ...axisStyle, splitLine: { show: false } }
  return trades.length ? { ...axis, min: trades[0].close_ts * 1000, max: trades[trades.length - 1].close_ts * 1000 } : axis
}

function zoomWindow(zoom) {
  return zoom ? { startValue: zoom[0], endValue: zoom[1] } : { start: 0, end: 100 }
}

function underwaterOption(trades, s, zoom) {
  const { underwater } = equitySeries(trades, s)
  return {
    ...base,
    grid: { left: 64, right: EQUITY_RIGHT, top: 8, bottom: 48 },
    tooltip: {
      ...tooltipStyle, trigger: 'axis', axisPointer: { type: 'line', lineStyle: { color: INK.muted } },
      formatter: ps => `${fmtTime(ps[0].value[0])}<br/>drawdown pnl${s}: <b>${fmt(ps[0].value[1])}</b>`,
    },
    xAxis: timeAxis(trades),
    yAxis: { type: 'value', ...axisStyle, max: 0, name: 'drawdown', nameTextStyle: { color: INK.muted, fontSize: 11 } },
    dataZoom: [{ type: 'inside', ...zoomWindow(zoom) }, {
      type: 'slider', ...zoomWindow(zoom), height: 18, bottom: 6, borderColor: '#2d3748', backgroundColor: 'transparent',
      fillerColor: 'rgba(57,135,229,0.15)', dataBackground: { lineStyle: { color: INK.muted }, areaStyle: { color: NEUTRAL } },
      textStyle: { color: INK.muted }, handleStyle: { color: INK.secondary },
    }],
    series: [{
      type: 'line', showSymbol: false, sampling: 'lttb', data: underwater, step: 'end',
      lineStyle: { width: 1.5, color: LOSS }, areaStyle: { color: LOSS, opacity: 0.25 },
    }],
  }
}

function histogramOption(trades, s) {
  const bins = histogram(trades.map(t => t[`pnl_change${s}`]))
  return {
    ...base,
    grid: { left: 56, right: 16, top: 16, bottom: 40 },
    tooltip: {
      ...tooltipStyle, trigger: 'item',
      formatter: p => { const b = bins[p.dataIndex]; return `${fmt(b.from)} … ${fmt(b.to)}<br/><b>${b.count}</b> trade(s)` },
    },
    xAxis: {
      type: 'category', ...axisStyle, data: bins.map(b => fmt(b.from + (b.to - b.from) / 2, 1)),
      name: `trade PnL change (pnl${s})`, nameLocation: 'middle', nameGap: 26, nameTextStyle: { color: INK.muted, fontSize: 11 },
    },
    yAxis: { type: 'value', ...axisStyle, minInterval: 1 },
    series: [{
      type: 'bar', barCategoryGap: 1,
      data: bins.map(b => ({ value: b.count, itemStyle: { color: b.from >= 0 ? GAIN : LOSS, borderRadius: [4, 4, 0, 0] } })),
    }],
  }
}

function holdingOption(trades, s) {
  const pts = trades.filter(t => t.open_ts != null).map(t => [Math.max(1, t.close_ts - t.open_ts), t[`pnl_change${s}`]])
  const style = color => ({ color, opacity: 0.6, borderColor: '#151923', borderWidth: 1 })
  return {
    ...base,
    grid: { left: 56, right: 16, top: 16, bottom: 40 },
    tooltip: { ...tooltipStyle, trigger: 'item', formatter: p => `held ${fmtDuration(p.value[0])}<br/>pnl${s}: <b>${fmt(p.value[1])}</b>` },
    xAxis: {
      type: 'log', ...axisStyle, splitLine: { show: false }, name: 'holding time', nameLocation: 'middle', nameGap: 26,
      nameTextStyle: { color: INK.muted, fontSize: 11 }, axisLabel: { ...axisStyle.axisLabel, formatter: v => fmtDuration(v) },
    },
    yAxis: { type: 'value', ...axisStyle },
    series: [
      { type: 'scatter', symbolSize: 8, large: pts.length > 5000, data: pts.filter(p => p[1] > 0), itemStyle: style(GAIN) },
      { type: 'scatter', symbolSize: 8, large: pts.length > 5000, data: pts.filter(p => p[1] <= 0), itemStyle: style(LOSS),
        markLine: { silent: true, symbol: 'none', lineStyle: { color: INK.axis, type: 'solid' }, label: { show: false }, data: [{ yAxis: 0 }] } },
    ],
  }
}

function groupPnlOption(rows, s) {
  const keys = Object.keys(rows)
  const dense = keys.length > 40
  return {
    ...base,
    grid: { left: 64, right: 16, top: 16, bottom: dense ? 56 : 32 },
    tooltip: {
      ...tooltipStyle, trigger: 'axis', axisPointer: { type: 'shadow', shadowStyle: { color: 'rgba(255,255,255,0.04)' } },
      formatter: ps => { const r = rows[ps[0].name]; return `${ps[0].name}<br/>pnl${s}: <b>${fmt(r[`pnl${s}`])}</b><br/>trades: ${r.trades} · win ${r.trades ? fmt(100 * r[`wins${s}`] / r.trades, 0) : '—'}%<br/>max drawdown: ${fmt(r[`dd${s}`])}` },
    },
    xAxis: { type: 'category', ...axisStyle, data: keys, axisLabel: { ...axisStyle.axisLabel, rotate: dense ? 45 : 0 } },
    yAxis: { type: 'value', ...axisStyle, name: `pnl${s}`, nameTextStyle: { color: INK.muted, fontSize: 11 } },
    dataZoom: dense ? [{ type: 'inside' }] : [],
    series: [{
      type: 'bar', barMaxWidth: 28,
      data: keys.map(k => {
        const v = rows[k][`pnl${s}`]
        return { value: v, itemStyle: { color: v >= 0 ? GAIN : LOSS, borderRadius: v >= 0 ? [4, 4, 0, 0] : [0, 0, 4, 4] } }
      }),
    }],
  }
}

function groupTradesOption(rows) {
  const keys = Object.keys(rows)
  return {
    ...base,
    grid: { left: 64, right: 16, top: 8, bottom: 8 },
    tooltip: { ...tooltipStyle, trigger: 'axis', axisPointer: { type: 'shadow', shadowStyle: { color: 'rgba(255,255,255,0.04)' } }, formatter: ps => `${ps[0].name}<br/>trades: <b>${ps[0].value}</b>` },
    xAxis: { type: 'category', ...axisStyle, data: keys, axisLabel: { show: false } },
    yAxis: { type: 'value', ...axisStyle, minInterval: 1, name: 'trades', nameTextStyle: { color: INK.muted, fontSize: 11 } },
    dataZoom: keys.length > 40 ? [{ type: 'inside' }] : [],
    series: [{ type: 'bar', barMaxWidth: 28, data: keys.map(k => rows[k].trades), itemStyle: { color: INK.muted, borderRadius: [4, 4, 0, 0] } }],
  }
}

const HEAT_METRICS = {
  pnl: { label: 'PnL', value: (r, s) => (r.trades ? r[`pnl${s}`] : null), diverging: true },
  trades: { label: 'Trades', value: r => r.trades, diverging: false },
  win: { label: 'Win rate %', value: (r, s) => (r.trades ? (100 * r[`wins${s}`]) / r.trades : null), diverging: false },
}

function heatmapOption(rows, s, metric) {
  const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun']
  const hours = Array.from({ length: 24 }, (_, h) => String(h).padStart(2, '0'))
  const m = HEAT_METRICS[metric]
  const data = []
  for (const [key, row] of Object.entries(rows)) {
    const [d, h] = key.split(' ')
    const v = m.value(row, s)
    if (v != null) data.push([hours.indexOf(h), days.indexOf(d), v])
  }
  const vals = data.map(d => d[2])
  const absMax = Math.max(1e-9, ...vals.map(Math.abs))
  const visualMap = m.diverging
    ? { min: -absMax, max: absMax, inRange: { color: [LOSS, NEUTRAL, GAIN] } }
    : { min: metric === 'win' ? 0 : 0, max: metric === 'win' ? 100 : Math.max(1, ...vals), inRange: { color: ['#1c2433', '#256abf', '#86b6ef'] } }
  return {
    ...base,
    grid: { left: 48, right: 16, top: 8, bottom: 64 },
    tooltip: { ...tooltipStyle, trigger: 'item', formatter: p => {
      const r = rows[`${days[p.value[1]]} ${hours[p.value[0]]}`]
      return `${days[p.value[1]]} ${hours[p.value[0]]}:00 JST<br/>pnl${s}: <b>${fmt(r[`pnl${s}`])}</b><br/>trades: ${r.trades} · win ${r.trades ? fmt(100 * r[`wins${s}`] / r.trades, 0) : '—'}%`
    } },
    xAxis: { type: 'category', data: hours, ...axisStyle, splitLine: { show: false }, splitArea: { show: false } },
    yAxis: { type: 'category', data: days, ...axisStyle, inverse: true, splitLine: { show: false } },
    visualMap: {
      ...visualMap, calculable: false, orient: 'horizontal', left: 'center', bottom: 4, itemHeight: 160, itemWidth: 10,
      textStyle: { color: INK.muted, fontSize: 11 }, formatter: v => fmt(v, metric === 'trades' ? 0 : 1),
    },
    series: [{ type: 'heatmap', data, itemStyle: { borderColor: '#151923', borderWidth: 2, borderRadius: 2 } }],
  }
}

// ── pieces ─────────────────────────────────────────────────────────────────

function GroupTable({ rows, s }) {
  return <div className="group-table-wrap"><table className="group-table">
    <thead><tr><th>group</th><th>trades</th><th>win %</th><th>pnl{s}</th><th>peak{s}</th><th>trough{s}</th><th>max dd{s}</th></tr></thead>
    <tbody>{Object.entries(rows).map(([k, r]) => {
      const pnl = r[`pnl${s}`]
      return <tr key={k} className={r.trades ? '' : 'group-empty'}>
        <td>{k}</td><td>{r.trades}</td><td>{r.trades ? fmt(100 * r[`wins${s}`] / r.trades, 0) : '—'}</td>
        <td className={pnl > 0 ? 'tone-gain' : pnl < 0 ? 'tone-loss' : ''}>{fmt(pnl)}</td>
        <td>{fmt(r[`peak${s}`])}</td><td>{fmt(r[`trough${s}`])}</td><td>{fmt(r[`dd${s}`])}</td>
      </tr>
    })}</tbody>
  </table></div>
}

// ── the page ───────────────────────────────────────────────────────────────

export default function RunView({ target, onBack }) {
  const [run, setRun] = useState(null)
  const [trades, setTrades] = useState(null)
  const [groups, setGroups] = useState({})
  const [error, setError] = useState('')
  const [s, setS] = useState(5)
  const [grouping, setGrouping] = useState('month')
  const [heatMetric, setHeatMetric] = useState('pnl')
  // The page's range, [start, end] epoch seconds, or null for the whole run. The
  // slider under the drawdown sets it, and so do the date inputs.
  const [range, setRange] = useState(null)
  const [market, setMarket] = useState(null)
  const [marketNote, setMarketNote] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    const { signal } = controller
    setRun(null); setTrades(null); setGroups({}); setError(''); setRange(null); setMarket(null)
    fetchRun(target, signal).then(async detail => {
      setRun(detail)
      const names = detail.files.filter(f => f.startsWith('by_'))
      const loaded = await Promise.all(names.map(n => fetchRunFile(target, n, signal).then(d => [n.slice(3, -5), d])))
      setGroups(Object.fromEntries(loaded))
      if (detail.files.includes('trades.csv')) setTrades(parseCsv(await fetchRunFile(target, 'trades.csv', signal)))
      else setTrades([])
    }).catch(e => { if (e.name !== 'AbortError') setError(e.message) })
    return () => controller.abort()
  }, [target.env, target.runId, target.source])

  const all = useMemo(() => (trades ? [...trades].sort((a, b) => a.close_ts - b.close_ts) : []), [trades])
  const first = all[0]?.close_ts, last = all[all.length - 1]?.close_ts
  const bounds = all.length ? [jstDate(first), jstDate(last)] : ['', '']
  const narrowed = range != null
  // Any range that holds every trade is the whole run.
  const select = useCallback((lo, hi) => {
    setRange(lo <= first && hi >= last ? null : [lo, hi])
  }, [first, last])
  const onZoom = useCallback((startMs, endMs) => select(startMs / 1000, endMs / 1000), [select])
  // Opened from the table with its date range: start narrowed to those JST days, as the table summarized them.
  useEffect(() => {
    if (!all.length || !(target.from || target.to)) return
    select(target.from ? jstMidnight(target.from) : first, target.to ? jstMidnight(target.to) + 86399 : last)
  }, [all, target.from, target.to])
  // A trade is in the range if it closed in it, as the equity curve plots it.
  const sorted = useMemo(() => (range ? all.filter(t => t.close_ts >= range[0] && t.close_ts <= range[1]) : all), [all, range])
  const zoom = useMemo(() => range && [range[0] * 1000, range[1] * 1000], [range])
  // The market most of the run's trades were in, priced over the selected days.
  const marketId = useMemo(() => {
    const counts = {}
    for (const t of all) counts[`${t.exchange}:${t.symbol}`] = (counts[`${t.exchange}:${t.symbol}`] ?? 0) + 1
    return Object.entries(counts).sort((a, b) => b[1] - a[1])[0]?.[0]
  }, [all])
  // The whole run's days, or the range to the minute: a narrower window is priced at a finer step.
  const priceFrom = range ? Math.floor(range[0] / 60) * 60 : bounds[0] && jstMidnight(bounds[0])
  const priceTo = range ? Math.ceil(range[1] / 60) * 60 : bounds[1] && jstMidnight(bounds[1]) + 86400
  useEffect(() => {
    if (!marketId || !priceFrom) return
    const controller = new AbortController()
    setMarketNote(`Loading ${marketId} prices…`)
    fetchPrices(marketId, priceFrom, priceTo, controller.signal)
      .then(body => { setMarket(body); setMarketNote(body.prices.length ? '' : `No ${marketId} trades on disk for these days.`) })
      .catch(e => { if (e.name !== 'AbortError') setMarketNote(e.message) })
    return () => controller.abort()
  }, [marketId, priceFrom, priceTo])
  const displayGroups = useMemo(() => trades ? { ...groups, ...groupByOpenTime(sorted, groups) } : groups, [trades, sorted, groups])
  const equity = useMemo(() => equityOption(all, s, market, zoom), [all, s, market, zoom])
  const underwater = useMemo(() => underwaterOption(all, s, zoom), [all, s, zoom])
  const hist = useMemo(() => histogramOption(sorted, s), [sorted, s])
  const holding = useMemo(() => holdingOption(sorted, s), [sorted, s])
  const groupRows = displayGroups[grouping]
  const groupPnl = useMemo(() => groupRows && groupPnlOption(groupRows, s), [groupRows, s])
  const groupTrades = useMemo(() => groupRows && groupTradesOption(groupRows), [groupRows])
  const heat = useMemo(() => displayGroups.weekday_hour && heatmapOption(displayGroups.weekday_hour, s, heatMetric), [displayGroups.weekday_hour, s, heatMetric])

  if (error) return <div className="run-view"><button className="back" onClick={onBack}>← Results</button><div role="alert" className="alert-error">{error}</div></div>
  if (!run) return <div className="loading">Loading…</div>

  const { meta } = run
  const summary = narrowed ? rangeSummary(sorted, s) : run.summary
  const pnl = summary[`pnl${s}`]
  const holdings = sorted.filter(t => t.open_ts != null).map(t => t.close_ts - t.open_ts).sort((a, b) => a - b)
  const medianHold = holdings.length ? holdings[Math.floor(holdings.length / 2)] : null

  return <div className="run-view">
    <div className="run-head">
      <button className="back" onClick={onBack}>← Results</button>
      <div className="run-title">
        <h1>{meta.env ?? target.env} <span className="run-id">{target.runId}</span></h1>
        <div className="run-sub">{meta.strategy} · {meta.start_str} → {meta.end_str} JST · batch {meta.batch_id}{meta.is_temporary_run ? ' · temporary' : ''}</div>
        <div className="chips">{Object.entries(meta.params ?? {}).map(([k, v]) => <span key={k} className="chip">{k}=<b>{String(v)}</b></span>)}</div>
      </div>
      <div className="run-actions">
        <Toggle label="Slippage" value={s} onChange={setS} options={SLIPPAGES.map(x => [x, `pnl${x}`])} />
        {run.grafana_url && <a className="btn-link" href={run.grafana_url} target="_blank" rel="noreferrer">Grafana ↗</a>}
      </div>
    </div>

    {all.length > 0 && <div className="range-bar" role="group" aria-label="Date range">
      <span className="range-label">Range (JST, by close)</span>
      <input type="date" value={range ? jstDate(range[0]) : bounds[0]} min={bounds[0]} max={range ? jstDate(range[1]) : bounds[1]} aria-label="From"
        onChange={e => e.target.value && select(jstMidnight(e.target.value), range ? range[1] : last)} />
      <span className="range-sep">→</span>
      <input type="date" value={range ? jstDate(range[1]) : bounds[1]} min={range ? jstDate(range[0]) : bounds[0]} max={bounds[1]} aria-label="To"
        onChange={e => e.target.value && select(range ? range[0] : first, jstMidnight(e.target.value) + 86399)} />
      <button className="back" disabled={!narrowed} onClick={() => setRange(null)}>Whole run</button>
      <span className="range-note">{narrowed
        ? `${fmtTime(range[0] * 1000)} → ${fmtTime(range[1] * 1000)} · ${sorted.length} of ${all.length} trades`
        : 'Drag the slider under the drawdown, or scroll on the equity chart, to narrow every panel'}</span>
    </div>}

    <div className="tiles">
      <Tile label={`PnL (pnl${s})`} value={fmt(pnl)} tone={pnl > 0 ? 'gain' : pnl < 0 ? 'loss' : null} />
      <Tile label="Trades" value={summary[`pnl_cnt${s}`] ?? '—'} />
      <Tile label="Win rate" value={`${fmt(summary[`win_rate${s}`], 1)}%`} />
      <Tile label="Max drawdown" value={fmt(summary[`max_drawdown${s}`])} />
      <Tile label="Sharpe (annualized)" value={fmt(summary[`sharpe_ratio_annualized${s}`], 3)} />
      <Tile label="Longest losing streak" value={summary[`max_loss_streak${s}`] ?? '—'} />
      <Tile label="Median holding" value={fmtDuration(medianHold)} />
    </div>

    {trades === null ? <div className="loading">Loading trades…</div> : !all.length ? <div className="empty">This run closed no position.</div> : <>
      <Panel wide title="Equity" note={`The whole run's cumulative closed PnL at each slippage rate, shown over the range; drawdown below follows the selected rate. ${market?.prices.length ? `Grey, right axis: ${market.market}'s last trade price every ${fmtDuration(market.step)}.` : marketNote} The slider sets the range for the whole page.`}>
        <Chart option={equity} height={300} group="equity" onZoom={onZoom} />
        <Chart option={underwater} height={150} group="equity" onZoom={onZoom} />
      </Panel>
      {!sorted.length ? <div className="empty">No position closed in this range.</div> : <div className="panel-row">
        <Panel title="Trade PnL distribution" note={`${sorted.length} closed positions, pnl${s} change per trade`}>
          <Chart option={hist} height={260} />
        </Panel>
        <Panel title="Holding time vs PnL" note="One dot per trade; holding time on a log scale">
          <Chart option={holding} height={260} />
        </Panel>
      </div>}
    </>}

    {groupRows && <Panel wide title="Grouped" note="Each group's own PnL curve from zero: for weekday, hour, holding time and side, it is the strategy traded only in that group."
      actions={<Toggle label="Grouping" value={grouping} onChange={setGrouping} options={GROUPINGS.filter(([k]) => displayGroups[k])} />}>
      <Chart option={groupPnl} height={240} />
      <Chart option={groupTrades} height={90} />
      <GroupTable rows={groupRows} s={s} />
    </Panel>}

    {heat && <Panel wide title="Weekday × hour (JST)" actions={<Toggle label="Heatmap metric" value={heatMetric} onChange={setHeatMetric} options={Object.entries(HEAT_METRICS).map(([k, m]) => [k, m.label])} />}>
      <Chart option={heat} height={280} />
    </Panel>}

    <div className="panel-row">
      <Panel title="Summary" note={narrowed ? 'summary.json: the whole run, not the selected range' : null}><KeyValues data={run.summary} /></Panel>
      <Panel title="Meta"><KeyValues data={meta} /></Panel>
    </div>
  </div>
}
