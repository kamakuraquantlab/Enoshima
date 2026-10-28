import { fmt } from '../charts/theme'

// One row per `enoshima walk-forward --train` group, newest first.
const pv = p => (p == null ? '—' : p < 0.001 ? '<0.001' : fmt(p, 3))

export default function TrainingsTable({ trainings, href }) {
  if (!trainings.length) return <div className="empty">No trainings for this environment. Run <code>enoshima walk-forward --env NAME --train</code>.</div>
  return <div className="table-wrap"><div className="table-scroll"><table className="results-table">
    <thead><tr>
      <th className="th-open"></th><th>created</th><th>group</th><th>runs</th><th>tested (JST)</th><th>retrain</th>
      <th>trained pnl5</th><th>no switching pnl5</th><th>difference</th><th>p bootstrap</th><th>retrainings won</th><th>rule now</th>
    </tr></thead>
    <tbody>{trainings.map(t => {
      const r = t.result ?? {}, sig = r.significance ?? {}
      const trained = r.trained?.pnl5, baseline = r.baseline?.pnl5
      const diff = trained != null && baseline != null ? trained - baseline : null
      const wf = t.settings?.walk_forward ?? {}
      return <tr key={t.train_id} className={diff > 0 ? 'row-pos' : diff < 0 ? 'row-neg' : ''}>
        <td className="td-open"><a className="open-run" href={href(t)}>charts</a></td>
        <td>{t.created_ts ? new Date(t.created_ts * 1000).toLocaleString() : t.train_id}</td>
        <td>{Object.entries(t.group ?? {}).map(([k, v]) => `${k}=${v}`).join(' ') || 'all'}</td>
        <td>{t.runs}</td>
        <td>{t.test?.[0]} → {t.test?.[1]}</td>
        <td>{wf.retrain} on {wf.train_window === 'rolling' ? wf.train_history : 'all'}</td>
        <td>{fmt(trained)}</td><td>{fmt(baseline)} ({r.baseline?.kind})</td>
        <td className="cell-pnl">{fmt(diff)}</td>
        <td>{pv(sig.pnl5?.p_bootstrap)}</td>
        <td>{sig.folds_won ?? '—'}/{(sig.folds_won ?? 0) + (sig.folds_lost ?? 0)}</td>
        <td>{r.current?.label}</td>
      </tr>
    })}</tbody>
  </table></div></div>
}
