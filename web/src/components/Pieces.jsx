import { flatten, formatValue } from '../utils/flatten'

// The page furniture RunView and TrainingView share.

export function Panel({ title, note, actions, children, wide }) {
  return <section className={`panel${wide ? ' panel-wide' : ''}`}>
    <header className="panel-head"><div><h2>{title}</h2>{note && <p className="panel-note">{note}</p>}</div>{actions}</header>
    {children}
  </section>
}

export function Toggle({ options, value, onChange, label }) {
  return <div className="toggle" role="group" aria-label={label}>
    {options.map(([v, text]) => <button key={v} className={v === value ? 'toggle-on' : ''} onClick={() => onChange(v)}>{text}</button>)}
  </div>
}

export function Tile({ label, value, tone }) {
  return <div className="tile"><div className="tile-label">{label}</div><div className={`tile-value${tone ? ` tone-${tone}` : ''}`}>{value}</div></div>
}

export function KeyValues({ data }) {
  const flat = flatten(data)
  return <table className="kv"><tbody>
    {Object.entries(flat).map(([k, v]) => <tr key={k}><th>{k}</th><td>{formatValue(v, k)}</td></tr>)}
  </tbody></table>
}
