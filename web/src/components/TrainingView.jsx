import { useEffect, useMemo, useState } from 'react'
import { fetchTraining, fetchTrainingFile } from '../api'
import { parseCsv } from '../utils/csv'
import Chart from '../charts/Chart'
import { INK, GAIN, LOSS, NEUTRAL, fmt, axisStyle, tooltipStyle, legendStyle, base } from '../charts/theme'
import { Panel, Toggle, Tile, KeyValues } from './Pieces'

// What `enoshima walk-forward --train` wrote for one group: post_analysis/train/<id>/.
// Boundaries in its files are JST "YYYY-MM-DD HH:MM", spans [from, to).

const SLIPPAGES = [0, 5, 10]
// Fixed order, validated on the panel surface: the trained curve, the baseline it must beat, the ceiling.
const MAIN = [
  ['trained', 'trained', '#3987e5', 'solid'],
  ['baseline', 'no switching', '#d95926', 'solid'],
  ['hindsight', 'hindsight', '#199e70', 'dashed'],
]
const FAILS = -1  // a heatmap value below the scale: drawn gray
const jst = s => Date.parse(`${s.replace(' ', 'T')}:00+09:00`)
const pv = p => (p == null ? '—' : p < 0.001 ? '<0.001' : fmt(p, 3))
const showParams = params => (params ? Object.entries(params).map(([k, v]) => `${k}=${v}`).join(' ') : 'none')

function cumulative(rows, s) {
  let cum = 0
  return rows.map(r => { cum += r[`pnl${s}`]; return [r.ts * 1000, cum] })
}

function equityOption(bySeries, rules, showRules, s, start) {
  const origin = [start, 0]
  const series = MAIN.filter(([key]) => bySeries[key]).map(([key, name, color, type]) => ({
    name, type: 'line', showSymbol: false, sampling: 'lttb', step: 'end',
    data: [origin, ...cumulative(bySeries[key], s)],
    lineStyle: { width: key === 'trained' ? 2 : 1.5, color, type }, itemStyle: { color },
    emphasis: { disabled: true }, z: 3,
    endLabel: { show: true, color: INK.secondary, fontSize: 11, formatter: p => `${name} ${fmt(p.value[1], 0)}` },
  }))
  if (showRules) for (const rule of rules) {
    if (!bySeries[rule.key]) continue
    series.push({
      name: rule.label, type: 'line', showSymbol: false, sampling: 'lttb', step: 'end',
      // A rule's curve can begin before the trained test (at its own first test hour): drawn from the test's.
      data: [origin, ...cumulative(bySeries[rule.key].filter(r => r.ts * 1000 >= start), s)],
      lineStyle: { width: 1, color: INK.muted, opacity: 0.45 }, itemStyle: { color: INK.muted },
      emphasis: { focus: 'series', lineStyle: { width: 2, opacity: 1 } }, z: 1,
    })
  }
  return {
    ...base,
    legend: { ...legendStyle, data: MAIN.map(m => m[1]) },
    grid: { left: 64, right: 140, top: 28, bottom: 28 },
    tooltip: {
      ...tooltipStyle, trigger: 'axis', axisPointer: { type: 'line', lineStyle: { color: INK.muted } },
      formatter: ps => {
        const main = ps.filter(p => MAIN.some(m => m[1] === p.seriesName))
        return `${new Date(ps[0].value[0]).toLocaleString()}<br/>` + main.map(p => `${p.marker}${p.seriesName}: <b>${fmt(p.value[1])}</b>`).join('<br/>')
      },
    },
    xAxis: { type: 'time', ...axisStyle, splitLine: { show: false } },
    yAxis: { type: 'value', ...axisStyle, name: `cumulative pnl${s}`, nameTextStyle: { color: INK.muted, fontSize: 11 } },
    dataZoom: [{ type: 'inside' }],
    series,
  }
}

// trained − baseline, by JST day: the figure the bootstrap tests.
function differenceOption(bySeries, s) {
  const day = ts => new Date((ts + 9 * 3600) * 1000).toISOString().slice(0, 10)
  const diff = {}
  for (const [key, sign] of [['trained', 1], ['baseline', -1]]) {
    for (const r of bySeries[key] ?? []) diff[day(r.ts)] = (diff[day(r.ts)] ?? 0) + sign * r[`pnl${s}`]
  }
  const days = Object.keys(diff).sort()
  let cum = 0
  const line = days.map(d => { cum += diff[d]; return [d, cum] })
  return {
    ...base,
    grid: { left: 64, right: 16, top: 16, bottom: 28 },
    tooltip: {
      ...tooltipStyle, trigger: 'axis', axisPointer: { type: 'shadow', shadowStyle: { color: 'rgba(255,255,255,0.04)' } },
      formatter: ps => `${ps[0].name} JST<br/>that day: <b>${fmt(diff[ps[0].name])}</b><br/>cumulative: <b>${fmt(line[ps[0].dataIndex][1])}</b>`,
    },
    xAxis: { type: 'category', ...axisStyle, data: days },
    yAxis: { type: 'value', ...axisStyle, name: `trained − baseline, pnl${s}`, nameTextStyle: { color: INK.muted, fontSize: 11 } },
    dataZoom: days.length > 60 ? [{ type: 'inside' }] : [],
    series: [
      {
        type: 'bar', barCategoryGap: 1, data: days.map(d => ({
          value: diff[d], itemStyle: { color: diff[d] >= 0 ? GAIN : LOSS, opacity: 0.55, borderRadius: diff[d] >= 0 ? [4, 4, 0, 0] : [0, 0, 4, 4] },
        })),
      },
      { type: 'line', showSymbol: false, data: line.map(p => p[1]), lineStyle: { width: 2, color: INK.primary }, itemStyle: { color: INK.primary } },
    ],
  }
}

// Every candidate's score at every retraining; the chosen one ringed.
function rankingOption(folds, rules) {
  const labels = [...rules.map(r => r.label), ...new Set(folds.flatMap(f => f.ranking.filter(r => r.key === 'baseline').map(r => r.label)))]
  const x = folds.map(f => f.test[0].slice(0, 10))
  const data = [], chosen = []
  folds.forEach((f, i) => {
    for (const r of f.ranking) {
      const y = labels.indexOf(r.label)
      if (y >= 0) data.push({ value: [i, y, r.passes ? r.score : FAILS], row: r, label: { show: !r.passes, formatter: '×', color: INK.secondary } })
    }
    const y = labels.indexOf(f.label)
    if (y >= 0) chosen.push([i, y])
  })
  return {
    ...base,
    grid: { left: 170, right: 16, top: 8, bottom: 64 },
    tooltip: {
      ...tooltipStyle, trigger: 'item', formatter: p => {
        if (p.seriesType === 'scatter') return `${folds[p.value[0]].label}<br/>chosen for ${folds[p.value[0]].test.join(' → ')}`
        const r = p.data.row
        return `${r.label}<br/>trained on ${folds[p.value[0]].train.join(' → ')}<br/>score <b>${r.passes ? fmt(r.score, 3) : 'fails the filters'}</b>`
          + `<br/>pnl5 ${fmt(r.pnl5)} · pnl10 ${fmt(r.pnl10)}<br/>dd5 ${fmt(r.dd5)} · top share ${fmt(r.top_share5, 2)} · ${r.trades} trades`
      },
    },
    xAxis: { type: 'category', data: x, ...axisStyle, splitLine: { show: false } },
    yAxis: { type: 'category', data: labels, ...axisStyle, inverse: true, splitLine: { show: false } },
    visualMap: {
      min: 0, max: 1, calculable: false, orient: 'horizontal', left: 'center', bottom: 4, itemHeight: 160, itemWidth: 10,
      inRange: { color: ['#1c2433', '#256abf', '#86b6ef'] }, outOfRange: { color: NEUTRAL },
      textStyle: { color: INK.muted, fontSize: 11 }, formatter: v => fmt(v, 2), seriesIndex: 0,
    },
    series: [
      { type: 'heatmap', data, itemStyle: { borderColor: '#151923', borderWidth: 2, borderRadius: 2 } },
      { type: 'scatter', data: chosen, symbol: 'rect', symbolSize: 14, itemStyle: { color: 'transparent', borderColor: INK.primary, borderWidth: 2 }, z: 3 },
    ],
  }
}

// Which parameter set the trained curve held, hour by hour.
function heldOption(switches) {
  const names = [...new Set(switches.map(s => showParams(s.params)))]
  const points = switches.flatMap(s => [[jst(s.from), showParams(s.params), s], [jst(s.to), showParams(s.params), s]])
  return {
    ...base,
    grid: { left: 16, right: 16, top: 8, bottom: 28, containLabel: true },
    tooltip: {
      ...tooltipStyle, trigger: 'item',
      formatter: p => { const s = p.data[2]; return `${s.from} → ${s.to} JST<br/>${showParams(s.params)}<br/>rule: ${s.rule}<br/>run ${s.run_id ?? '— (sat out)'}` },
    },
    xAxis: { type: 'time', ...axisStyle, splitLine: { show: false } },
    yAxis: { type: 'category', data: names, ...axisStyle, axisLabel: { ...axisStyle.axisLabel, fontSize: 10 } },
    dataZoom: [{ type: 'inside' }],
    series: [{ type: 'line', step: 'end', symbol: 'none', data: points, lineStyle: { width: 2, color: '#3987e5' } }],
  }
}

function FoldTable({ folds }) {
  return <div className="group-table-wrap"><table className="group-table">
    <thead><tr><th>test (JST)</th><th>trained on</th><th>chosen</th><th>pnl5</th><th>baseline pnl5</th><th>difference</th></tr></thead>
    <tbody>{folds.map(f => {
      const d = f.test_pnl5 - f.baseline_pnl5
      return <tr key={f.test[0]}>
        <td>{f.test[0]} → {f.test[1]}</td><td>{f.train[0]} → {f.train[1]}</td>
        <td>{f.label}{f.fallback ? ' (nothing passed)' : ''}</td>
        <td>{fmt(f.test_pnl5)}</td><td>{fmt(f.baseline_pnl5)}</td>
        <td className={d > 0 ? 'tone-gain' : d < 0 ? 'tone-loss' : ''}>{fmt(d)}</td>
      </tr>
    })}</tbody>
  </table></div>
}

function TotalsTable({ summary, rules }) {
  const rows = [
    ['trained', summary.trained], [`no switching (${summary.baseline.kind})`, summary.baseline], ['hindsight', summary.hindsight],
    ...rules.map(r => [`${r.label} (always)`, summary.rules[r.key]]),
  ]
  return <div className="group-table-wrap"><table className="group-table">
    <thead><tr><th>over the test</th><th>pnl0</th><th>pnl5</th><th>pnl10</th><th>max dd5</th><th>trades</th></tr></thead>
    <tbody>{rows.filter(([, o]) => o).map(([name, o]) => <tr key={name}>
      <td>{name}</td><td>{fmt(o.pnl0)}</td>
      <td className={o.pnl5 > 0 ? 'tone-gain' : o.pnl5 < 0 ? 'tone-loss' : ''}>{fmt(o.pnl5)}</td>
      <td>{fmt(o.pnl10)}</td><td>{fmt(o.dd5)}</td><td>{o.trades}</td>
    </tr>)}</tbody>
  </table></div>
}

export default function TrainingView({ target, onBack }) {
  const [detail, setDetail] = useState(null)
  const [files, setFiles] = useState(null)
  const [error, setError] = useState('')
  const [s, setS] = useState(5)
  const [showRules, setShowRules] = useState(false)

  useEffect(() => {
    const controller = new AbortController()
    const { signal } = controller
    setDetail(null); setFiles(null); setError('')
    fetchTraining(target, signal).then(async d => {
      setDetail(d)
      const [folds, switches, curve] = await Promise.all(['folds.json', 'switches.json', 'curve.csv']
        .map(n => d.files.includes(n) ? fetchTrainingFile(target, n, signal) : null))
      setFiles({ folds: folds ?? [], switches: switches ?? [], curve: curve ? parseCsv(curve) : [] })
    }).catch(e => { if (e.name !== 'AbortError') setError(e.message) })
    return () => controller.abort()
  }, [target.env, target.trainId, target.source])

  const bySeries = useMemo(() => {
    const out = {}
    for (const r of files?.curve ?? []) (out[r.series] ??= []).push(r)
    return out
  }, [files])
  const rules = detail?.meta.rules ?? []
  const start = detail ? jst(detail.meta.test[0]) : 0
  const equity = useMemo(() => equityOption(bySeries, rules, showRules, s, start), [bySeries, rules, showRules, s, start])
  const difference = useMemo(() => differenceOption(bySeries, s), [bySeries, s])
  const ranking = useMemo(() => files && rankingOption(files.folds, rules), [files, rules])
  const held = useMemo(() => files && heldOption(files.switches), [files])

  if (error) return <div className="run-view"><button className="back" onClick={onBack}>← Trainings</button><div role="alert" className="alert-error">{error}</div></div>
  if (!detail) return <div className="loading">Loading…</div>

  const { meta, summary } = detail
  const sig = summary.significance
  const p = sig[`pnl${s}`] ?? sig.pnl5
  const diff = summary.trained[`pnl${s}`] - summary.baseline[`pnl${s}`]
  const wf = meta.settings?.walk_forward ?? {}
  const group = Object.entries(meta.group ?? {})

  return <div className="run-view">
    <div className="run-head">
      <button className="back" onClick={onBack}>← Trainings</button>
      <div className="run-title">
        <h1>{meta.env ?? target.env} <span className="run-id">{target.trainId}</span></h1>
        <div className="run-sub">{meta.strategy} · runs {meta.start_str} → {meta.end_str} · tested {meta.test[0]} → {meta.test[1]} JST · {meta.runs} parameter sets</div>
        <div className="chips">
          {group.map(([k, v]) => <span key={k} className="chip">{k}=<b>{String(v)}</b></span>)}
          <span className="chip">retrain every <b>{wf.retrain}</b> on <b>{wf.train_window === 'rolling' ? `the last ${wf.train_history}` : 'all before'}</b></span>
          <span className="chip">baseline <b>{summary.baseline.kind}</b> {showParams(meta.baseline?.params)}</span>
        </div>
      </div>
      <div className="run-actions">
        <Toggle label="Slippage" value={s} onChange={setS} options={SLIPPAGES.map(x => [x, `pnl${x}`])} />
      </div>
    </div>

    <div className="tiles">
      <Tile label={`Trained (pnl${s})`} value={fmt(summary.trained[`pnl${s}`])} tone={summary.trained[`pnl${s}`] > 0 ? 'gain' : summary.trained[`pnl${s}`] < 0 ? 'loss' : null} />
      <Tile label={`No switching (pnl${s})`} value={fmt(summary.baseline[`pnl${s}`])} />
      <Tile label="Trained − baseline" value={fmt(diff)} tone={diff > 0 ? 'gain' : diff < 0 ? 'loss' : null} />
      {s !== 0 && <Tile label={`p, block bootstrap (${sig.block_days}-day blocks)`} value={pv(p.p_bootstrap)} />}
      <Tile label="Retrainings won" value={`${sig.folds_won}/${sig.folds_won + sig.folds_lost} · p ${pv(sig.p_sign)}`} />
      <Tile label="Rule to trade now" value={<span style={{ fontSize: 14 }}>{summary.current.label}</span>} />
    </div>

    {files === null ? <div className="loading">Loading curves…</div> : <>
      <Panel wide title="Equity over the test" note="Cumulative closed PnL from the first test hour. Hindsight chose on the whole range, test included: a ceiling, not a rule. Each rule's curve is that rule kept for the whole test."
        actions={<Toggle label="Rules" value={showRules} onChange={setShowRules} options={[[false, 'Hide rules'], [true, 'Show rules']]} />}>
        <Chart option={equity} height={320} />
      </Panel>
      <Panel wide title="Trained − no switching, by JST day" note={`Bars are each day's difference; the line is its running total. The bootstrap resamples these days in blocks; p is the chance of a total this large if switching made no difference.`}>
        <Chart option={difference} height={220} />
      </Panel>
      {files.folds.length > 0 && <Panel wide title="Rule chosen at each retraining" note="Each candidate's score on its training window: 1 is best, the mean of its percentiles on pnl5, pnl10, dd5 and top share. Gray cells marked × fail the filters. The ringed cell was traded until the next retraining.">
        <Chart option={ranking} height={Math.max(160, 28 * (rules.length + 1) + 80)} />
      </Panel>}
      {files.switches.length > 0 && <Panel wide title="Parameter set held" note="What the trained curve actually traded, as its rules switched between parameter sets.">
        <Chart option={held} height={Math.max(140, 22 * new Set(files.switches.map(x => showParams(x.params))).size + 60)} />
      </Panel>}
      <div className="panel-row">
        <Panel title="Retrainings"><FoldTable folds={files.folds} /></Panel>
        <Panel title="Totals"><TotalsTable summary={summary} rules={rules} /></Panel>
      </div>
    </>}

    <div className="panel-row">
      <Panel title="Summary"><KeyValues data={{ ...summary, rules: undefined }} /></Panel>
      <Panel title="Meta"><KeyValues data={{ ...meta, rules: undefined }} /></Panel>
    </div>
  </div>
}
