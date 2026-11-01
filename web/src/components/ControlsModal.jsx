import { useEffect, useState } from 'react'
import { colLabel } from './ResultsTable'

// The table's controls in one place: sorting, the date range, deleting and hidden columns.
// Everything takes effect as it is changed; closing the modal keeps it.

function SortSection({ sortKeys, onSortKeys, columns }) {
  const [adding, setAdding] = useState('')
  const free = columns.filter(c => !sortKeys.some(k => k.col === c))
  const move = (i, d) => {
    const next = [...sortKeys]
    ;[next[i], next[i + d]] = [next[i + d], next[i]]
    onSortKeys(next)
  }
  return <section className="modal-section">
    <h3>Sorting</h3>
    <p className="modal-note">Applied in order, the first key first. A column header click also sorts by it.</p>
    {sortKeys.length === 0 ? <div className="modal-empty">Not sorted: rows are newest first.</div> :
      <ol className="sort-list">{sortKeys.map((k, i) => <li key={k.col} className="sort-row">
        <span className="sort-rank-n">{i + 1}</span>
        <span className="sort-row-col" title={k.col}>{colLabel(k.col)}</span>
        <button className="mini-btn" onClick={() => onSortKeys(sortKeys.map((x, j) => j === i ? { ...x, asc: !x.asc } : x))}
          title="Flip the direction">{k.asc ? '↑ ascending' : '↓ descending'}</button>
        <button className="mini-btn" disabled={i === 0} onClick={() => move(i, -1)} title="Move up">▲</button>
        <button className="mini-btn" disabled={i === sortKeys.length - 1} onClick={() => move(i, 1)} title="Move down">▼</button>
        <button className="mini-btn mini-danger" onClick={() => onSortKeys(sortKeys.filter((_, j) => j !== i))} title="Remove">✕</button>
      </li>)}</ol>}
    <div className="modal-row">
      <select aria-label="Add a sort key" value={adding} onChange={e => setAdding(e.target.value)}>
        <option value="">Add a column…</option>
        {free.map(c => <option key={c} value={c}>{colLabel(c)} ({c})</option>)}
      </select>
      <button className="mini-btn" disabled={!adding} onClick={() => { onSortKeys([...sortKeys, { col: adding, asc: false }]); setAdding('') }}>Add</button>
      {sortKeys.length > 0 && <button className="mini-btn mini-danger" onClick={() => onSortKeys([])}>Clear all</button>}
    </div>
  </section>
}

function RangeSection({ range, onRange, firstDay, lastDay }) {
  const ranged = Boolean(range.start || range.end)
  return <section className="modal-section">
    <h3>Date range</h3>
    <p className="modal-note">Each run's result over the trades closed in these JST days. Leave both empty for the whole runs.</p>
    <div className="modal-row range-bar">
      <input type="date" aria-label="From" value={range.start} min={firstDay || undefined} max={range.end || lastDay || undefined}
        onChange={e => onRange({ ...range, start: e.target.value })} />
      <span className="range-sep">→</span>
      <input type="date" aria-label="To" value={range.end} min={range.start || firstDay || undefined} max={lastDay || undefined}
        onChange={e => onRange({ ...range, end: e.target.value })} />
      {ranged && <button className="mini-btn" onClick={() => onRange({ start: '', end: '' })}>Whole runs</button>}
    </div>
  </section>
}

function DeleteSection({ deleteMode, onDeleteMode, count, deleting, onDelete }) {
  return <section className="modal-section">
    <h3>Delete</h3>
    <p className="modal-note">Tick runs in the table, then delete them: each run's files in the data lake are removed.</p>
    <div className="modal-row">
      <label className="switch"><input type="checkbox" checked={deleteMode} disabled={deleting} onChange={e => onDeleteMode(e.target.checked)} /> Enable delete</label>
      {deleteMode && <button className="btn-bulk-delete" disabled={deleting || !count} onClick={onDelete}>{deleting ? 'Deleting…' : `Delete (${count})`}</button>}
    </div>
  </section>
}

function ColumnsSection({ columns, hidden, onHidden }) {
  const [filter, setFilter] = useState('')
  const q = filter.trim().toLowerCase()
  const shown = columns.filter(c => !q || c.toLowerCase().includes(q))
  const toggle = col => {
    const next = new Set(hidden)
    if (next.has(col)) next.delete(col); else next.add(col)
    onHidden(next)
  }
  return <section className="modal-section">
    <h3>Columns <span className="modal-count">{hidden.size} hidden of {columns.length}</span></h3>
    <div className="modal-row">
      <input type="search" className="search-input" placeholder="Find a column…" value={filter} onChange={e => setFilter(e.target.value)} />
      <button className="mini-btn" disabled={!hidden.size} onClick={() => onHidden(new Set())}>Show all</button>
    </div>
    <div className="column-list">{shown.map(c => <label key={c} className={`column-item${hidden.has(c) ? ' column-hidden' : ''}`} title={c}>
      <input type="checkbox" checked={!hidden.has(c)} onChange={() => toggle(c)} /> {c}
    </label>)}</div>
  </section>
}

export default function ControlsModal({ onClose, onRefresh, refreshing, ...props }) {
  useEffect(() => {
    const onKey = e => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])
  return <div className="modal-backdrop" onClick={e => { if (e.target === e.currentTarget) onClose() }}>
    <div className="modal" role="dialog" aria-modal="true" aria-label="Table controls">
      <header className="modal-head">
        <h2>Controls</h2>
        <button className="mini-btn" disabled={refreshing} onClick={onRefresh} title="Read the lake again">Refresh</button>
        <button className="modal-close" onClick={onClose} aria-label="Close">✕</button>
      </header>
      <div className="modal-body">
        <SortSection sortKeys={props.sortKeys} onSortKeys={props.onSortKeys} columns={props.columns} />
        <RangeSection range={props.range} onRange={props.onRange} firstDay={props.firstDay} lastDay={props.lastDay} />
        <DeleteSection deleteMode={props.deleteMode} onDeleteMode={props.onDeleteMode} count={props.deleteCount}
          deleting={props.deleting} onDelete={props.onDelete} />
        <ColumnsSection columns={props.columns} hidden={props.hidden} onHidden={props.onHidden} />
      </div>
    </div>
  </div>
}
