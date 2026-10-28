import { useEffect, useMemo, useRef, useState } from 'react'
import { fetchRunWalkForward, fetchRunFile, fetchTrainingFile } from '../api'
import { parseCsv } from '../utils/csv'
import Chart from '../charts/Chart'
import { INK, fmt, axisStyle, tooltipStyle, legendStyle, base } from '../charts/theme'
import { Panel } from './Pieces'

// One run against every walk-forward rule of its group's newest training.
// A rule switches among the group's parameter sets; "no switching" is this
// run's set held. Each rule is tested from its own first hour (the range start
// plus its history) to the end, and both curves cover exactly those hours.
// Figures in the table are Enoshima's (runs.json, exact by trade); the charts
// are drawn per JST hour, so a drawdown inside an hour does not show.

const SWITCHING = ['switching', '#3987e5']
const HELD = ['no switching', '#d95926']
const jst = s => Date.parse(`${s.replace(' ', 'T')}:00+09:00`) / 1000
const tone = v => (v > 0 ? 'tone-gain' : v < 0 ? 'tone-loss' : '')
const showParams = params => Object.entries(params ?? {}).map(([k, v]) => `${k}=${v}`).join(' ')

// trades.csv -> [{ts, pnl5}] per JST hour a trade closed in, as Enoshima buckets them.
function hourlyFromTrades(trades) {
  const out = new Map()
  for (const t of trades) {
    const hour = Math.floor(t.close_ts / 3600) * 3600
    out.set(hour, (out.get(hour) ?? 0) + t.pnl_change5)
  }
  return [...out].sort((a, b) => a[0] - b[0]).map(([ts, pnl5]) => ({ ts, pnl5 }))
}

// Cumulative pnl5 and its drawdown from the running high (zero counts as the first high).
function curves(rows, from, to) {
  let cum = 0, peak = 0
  const equity = [[from * 1000, 0]], drawdown = [[from * 1000, 0]]
  for (const r of rows) {
    if (r.ts < from || r.ts >= to) continue
    cum += r.pnl5; peak = Math.max(peak, cum)
    equity.push([r.ts * 1000, cum]); drawdown.push([r.ts * 1000, cum - peak])
  }
  return { equity, drawdown }
}

function lineOption(pairs, yName) {
  return {
    ...base,
    legend: { ...legendStyle, data: pairs.map(p => p[0]) },
    grid: { left: 64, right: 16, top: 28, bottom: 24 },
    tooltip: {
      ...tooltipStyle, trigger: 'axis', axisPointer: { type: 'line', lineStyle: { color: INK.muted } },
      formatter: ps => `${new Date(ps[0].value[0]).toLocaleString()}<br/>` + ps.map(p => `${p.marker}${p.seriesName}: <b>${fmt(p.value[1], 0)}</b>`).join('<br/>'),
    },
    xAxis: { type: 'time', ...axisStyle, splitLine: { show: false } },
    yAxis: { type: 'value', ...axisStyle, name: yName, nameTextStyle: { color: INK.muted, fontSize: 11 } },
    dataZoom: [{ type: 'inside' }],
    series: pairs.map(([name, color, data]) => ({
      name, type: 'line', showSymbol: false, sampling: 'lttb', step: 'end', data,
      lineStyle: { width: name === SWITCHING[0] ? 2 : 1.5, color }, itemStyle: { color }, emphasis: { disabled: true },
    })),
  }
}

// A row's charts are built once it nears the screen: a grid of rules is a hundred rows.
function useNearScreen() {
  const ref = useRef(null)
  const [near, setNear] = useState(false)
  useEffect(() => {
    if (near || !ref.current) return
    const observer = new IntersectionObserver(([entry]) => { if (entry.isIntersecting) setNear(true) }, { rootMargin: '600px' })
    observer.observe(ref.current)
    return () => observer.disconnect()
  }, [near])
  return [ref, near]
}

function RuleRow({ rule, best, switchingRows, heldRows }) {
  const [ref, near] = useNearScreen()
  return <section ref={ref} className={`panel wf-rule${best ? ' wf-best' : ''}`}>
    <RuleStats rule={rule} best={best} />
    {near ? <RuleCharts rule={rule} switchingRows={switchingRows} heldRows={heldRows} />
      : <><div className="wf-rule-chart" style={{ height: 200 }} /><div className="wf-rule-chart" style={{ height: 200 }} /></>}
  </section>
}

function RuleCharts({ rule, switchingRows, heldRows }) {
  const from = jst(rule.from), to = jst(rule.to)
  const { equity, drawdown } = useMemo(() => {
    const sw = curves(switchingRows, from, to), held = curves(heldRows, from, to)
    return {
      equity: lineOption([[...SWITCHING, sw.equity], [...HELD, held.equity]], 'cumulative pnl5'),
      drawdown: lineOption([[...SWITCHING, sw.drawdown], [...HELD, held.drawdown]], 'drawdown, pnl5'),
    }
  }, [switchingRows, heldRows, from, to])
  return <>
    <div className="wf-rule-chart"><Chart option={equity} height={200} group={`wf-${rule.key}`} /></div>
    <div className="wf-rule-chart"><Chart option={drawdown} height={200} group={`wf-${rule.key}`} /></div>
  </>
}

function RuleStats({ rule, best }) {
  const h = rule.held ?? {}
  const pnlDiff = rule.pnl5 - h.pnl5, ddDiff = rule.dd5 - h.dd5
  return <div className="wf-rule-stats">
      <h2>{rule.label}{best && <span className="wf-best-badge">best</span>}</h2>
      <div className="chips">
        <span className="chip">history <b>{rule.history}</b></span>
        <span className="chip">every <b>{rule.every}</b></span>
        <span className="chip">window <b>{rule.window}</b></span>
      </div>
      <div className="run-sub">tested {rule.from} → {rule.to} JST</div>
      <table className="group-table">
        <thead><tr><th></th><th>switching</th><th>no switching</th><th>difference</th></tr></thead>
        <tbody>
          <tr><td>pnl5</td><td className={tone(rule.pnl5)}>{fmt(rule.pnl5, 0)}</td><td className={tone(h.pnl5)}>{fmt(h.pnl5, 0)}</td><td className={tone(pnlDiff)}>{fmt(pnlDiff, 0)}</td></tr>
          <tr><td>pnl10</td><td>{fmt(rule.pnl10, 0)}</td><td>{fmt(h.pnl10, 0)}</td><td className={tone(rule.pnl10 - h.pnl10)}>{fmt(rule.pnl10 - h.pnl10, 0)}</td></tr>
          <tr><td>max dd5</td><td>{fmt(rule.dd5, 0)}</td><td>{fmt(h.dd5, 0)}</td><td className={tone(-ddDiff)}>{fmt(ddDiff, 0)}</td></tr>
          <tr><td>trades</td><td>{rule.trades}</td><td>{h.trades}</td><td></td></tr>
        </tbody>
      </table>
    </div>
}

export default function RunWalkForwardView({ target, onBack, runHref, trainingHref }) {
  const [detail, setDetail] = useState(null)
  const [curve, setCurve] = useState(null)
  const [held, setHeld] = useState(null)
  const [error, setError] = useState('')

  useEffect(() => {
    const controller = new AbortController()
    const { signal } = controller
    setDetail(null); setCurve(null); setHeld(null); setError('')
    fetchRunWalkForward(target, signal).then(async d => {
      setDetail(d)
      const training = { env: target.env, trainId: d.train_id, source: target.source }
      const [curveCsv, tradesCsv] = await Promise.all([
        fetchTrainingFile(training, 'curve.csv', signal), fetchRunFile(target, 'trades.csv', signal),
      ])
      const bySeries = {}
      for (const r of parseCsv(curveCsv)) (bySeries[r.series] ??= []).push(r)
      setCurve(bySeries); setHeld(hourlyFromTrades(parseCsv(tradesCsv)))
    }).catch(e => { if (e.name !== 'AbortError') setError(e.message) })
    return () => controller.abort()
  }, [target.env, target.runId, target.source])

  if (error) return <div className="run-view"><button className="back" onClick={onBack}>← Runs</button><div role="alert" className="alert-error">{error}</div></div>
  if (!detail) return <div className="loading">Loading…</div>

  const { meta, rules } = detail
  const best = rules.find(r => r.key === detail.best)
  return <div className="run-view">
    <div className="run-head">
      <button className="back" onClick={onBack}>← Runs</button>
      <div className="run-title">
        <h1>{meta.env ?? target.env} <span className="run-id">{target.runId}</span> walk-forward</h1>
        <div className="run-sub">{meta.strategy} · runs {meta.start_str} → {meta.end_str} · {meta.runs} parameter sets in the group · training {detail.train_id}</div>
        <div className="chips">
          {Object.entries(meta.group ?? {}).map(([k, v]) => <span key={k} className="chip">{k}=<b>{String(v)}</b></span>)}
          <span className="chip">no switching holds <b>{showParams(detail.params)}</b></span>
        </div>
      </div>
      <div className="run-actions">
        <a className="btn-link" href={runHref(target)} target="_blank" rel="noreferrer">run charts</a>
        <a className="btn-link" href={trainingHref({ source: target.source, train_id: detail.train_id })} target="_blank" rel="noreferrer">group training</a>
      </div>
    </div>

    <Panel wide title="Each rule against this run held" note="A rule chooses a parameter set from the group every interval, on the history before, and trades it until the next choice. Each row is tested from the range start plus its history, so rows with a shorter history cover more hours. Best is the highest switching pnl5.">
      <div className="group-table-wrap"><table className="group-table">
        <thead><tr><th>rule</th><th>tested from</th><th>switching pnl5</th><th>no switching pnl5</th><th>difference</th><th>switching dd5</th><th>no switching dd5</th></tr></thead>
        <tbody>{rules.map(r => {
          const h = r.held ?? {}, d = r.pnl5 - h.pnl5
          return <tr key={r.key} className={r.key === detail.best ? 'wf-best-row' : ''}>
            <td><a href={`#${r.key}`} onClick={e => { e.preventDefault(); document.getElementById(r.key)?.scrollIntoView({ behavior: 'smooth' }) }}>{r.label}</a></td>
            <td>{r.from}</td>
            <td className={tone(r.pnl5)}>{fmt(r.pnl5, 0)}</td><td className={tone(h.pnl5)}>{fmt(h.pnl5, 0)}</td>
            <td className={tone(d)}>{fmt(d, 0)}</td><td>{fmt(r.dd5, 0)}</td><td>{fmt(h.dd5, 0)}</td>
          </tr>
        })}</tbody>
      </table></div>
    </Panel>

    {curve === null ? <div className="loading">Loading curves…</div> :
      rules.map(r => <div key={r.key} id={r.key}>
        <RuleRow rule={r} best={r === best} switchingRows={curve[r.key] ?? []} heldRows={held} />
      </div>)}
  </div>
}
