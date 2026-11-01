import { useState, useEffect, useCallback, lazy, Suspense } from 'react'
import { fetchResults, deleteResult } from './api'
import ResultsTable, { loadHidden, saveHidden, loadSortKeys, saveSortKeys } from './components/ResultsTable'
import ControlsModal from './components/ControlsModal'
// The charts are most of the bundle; the table does not wait for them.
const RunView = lazy(() => import('./components/RunView'))

// #/results/<env> lists an env's runs.
function hashEnv() {
  try {
    const m = window.location.hash.match(/^#\/results\/(.*)$/)
    return decodeURIComponent(m?.[1] || '')
  } catch { return '' }
}

// #/run/<source>/<run_id>[?from=YYYY-MM-DD&to=YYYY-MM-DD]: source is the env name (once the env's
// folder, e.g. backtest/NAME, whose last component is the env name: such a link still works). `from`
// and `to` are the table's date range, JST days, which the run page opens narrowed to.
const DAY = /^\d{4}-\d{2}-\d{2}$/
function hashRun() {
  try {
    const m = window.location.hash.match(/^#\/run\/([^/?]+)\/([^/?]+)(?:\?(.*))?$/)
    if (!m) return null
    const source = decodeURIComponent(m[1])
    const query = new URLSearchParams(m[3] || '')
    const day = key => (DAY.test(query.get(key) || '') ? query.get(key) : '')
    return { source, env: source.split('/').pop(), runId: decodeURIComponent(m[2]), from: day('from'), to: day('to') }
  } catch { return null }
}
function runHash(row, range) {
  const base = `#/run/${encodeURIComponent(row.source)}/${encodeURIComponent(row.run_id ?? row.runId)}`
  const query = new URLSearchParams(Object.entries({ from: range?.start, to: range?.end }).filter(([, v]) => v))
  return query.size ? `${base}?${query}` : base
}
export default function App() {
  const [allResults, setAllResults] = useState([])
  const [env, setEnv] = useState(hashEnv)
  const [run, setRun] = useState(hashRun)
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [refresh, setRefresh] = useState(0)
  const [deleteMode, setDeleteMode] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [selectedIdxs, setSelectedIdxs] = useState(new Set())
  const [visibleRows, setVisibleRows] = useState([])
  // Sorting ([{col, asc}], head first) and hidden columns, kept in the browser; set from the header or the controls.
  const [sortKeys, setSortKeysState] = useState(loadSortKeys)
  const [hidden, setHiddenState] = useState(loadHidden)
  const [columns, setColumns] = useState([])
  const [showControls, setShowControls] = useState(false)
  const setSortKeys = useCallback(keys => { setSortKeysState(keys); saveSortKeys(keys) }, [])
  const setHidden = useCallback(set => { setHiddenState(set); saveHidden(set) }, [])
  // The table's date range, JST days 'YYYY-MM-DD' or '' for open; the runs summarized over it, or null.
  const [range, setRange] = useState({ start: '', end: '' })
  const [rangeResults, setRangeResults] = useState(null)
  const [rangeLoading, setRangeLoading] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true); setError(''); setSelectedIdxs(new Set())
    fetchResults(controller.signal).then(setAllResults).catch(e => {
      if (e.name !== 'AbortError') setError(e.message)
    }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [refresh])
  useEffect(() => {
    const onHash = () => {
      const next = hashRun()
      setRun(next)
      if (next) return
      // Runs selected for deletion are not carried onto another env.
      setEnv(previous => {
        const current = hashEnv()
        if (current !== previous) { setSelectedIdxs(new Set()); setSearch('') }
        return current
      })
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  const envs = [...new Set(allResults.map(row => row.env_name))].sort()
  const selectedEnv = env || envs[0] || ''
  const ranged = Boolean(range.start || range.end)
  useEffect(() => {
    setRangeResults(null); setSelectedIdxs(new Set())
    if (!ranged || !selectedEnv) return
    const controller = new AbortController()
    setRangeLoading(true); setError('')
    fetchResults(controller.signal, { env: selectedEnv, ...range }).then(setRangeResults).catch(e => {
      if (e.name !== 'AbortError') setError(e.message)
    }).finally(() => { if (!controller.signal.aborted) setRangeLoading(false) })
    return () => controller.abort()
  }, [ranged, range.start, range.end, selectedEnv, refresh])
  const envResults = allResults.filter(row => row.env_name === selectedEnv)
  const results = ranged ? rangeResults ?? [] : envResults
  // The inputs' bounds: the env's runs' first and last days.
  const firstDay = envResults.reduce((m, r) => (r.start_str && (!m || r.start_str < m) ? r.start_str : m), '').slice(0, 10)
  const lastDay = envResults.reduce((m, r) => (r.end_str && r.end_str > m ? r.end_str : m), '').slice(0, 10)
  const listHash = name => `#/results/${encodeURIComponent(name)}`
  const toDelete = visibleRows.filter(r => selectedIdxs.has(r._rowIdx)).map(r => results[r._rowIdx]).filter(Boolean)
  const toggleRow = idx => setSelectedIdxs(prev => {
    const next = new Set(prev)
    if (next.has(idx)) next.delete(idx); else next.add(idx)
    return next
  })
  const toggleAll = checked => setSelectedIdxs(prev => {
    const next = new Set(prev)
    for (const row of visibleRows) { if (checked) next.add(row._rowIdx); else next.delete(row._rowIdx) }
    return next
  })
  async function removeSelected() {
    if (!toDelete.length || !window.confirm(`Delete ${toDelete.length} run(s)? Each run's files in the data lake are removed: its meta, summary, trades and records.`)) return
    setDeleting(true); setError('')
    try { for (const row of toDelete) await deleteResult(row) }
    catch (e) { setError(e.message) }
    finally { setSelectedIdxs(new Set()); setDeleting(false); setRefresh(v => v+1) }
  }
  if (run) return <div className="app">
    <header className="app-header"><div className="app-title"><span className="app-logo">▲</span><h1>Enoshima</h1></div></header>
    <main className="app-main"><Suspense fallback={<div className="loading">Loading…</div>}><RunView target={run} onBack={() => { window.location.hash = `#/results/${encodeURIComponent(run.env)}` }} /></Suspense></main>
  </div>
  const shownRange = ranged ? `${range.start || firstDay || '…'} → ${range.end || lastDay || '…'}`
    : firstDay ? `${firstDay} → ${lastDay} (whole runs)` : 'whole runs'
  return <div className="app">
    <header className="app-header"><div className="app-title"><span className="app-logo">▲</span><h1>Enoshima</h1></div>
      <div className="app-controls">
        <input aria-label="Filter results by keyword" type="search" className="search-input" placeholder="Filter by keyword…" value={search} onChange={e => setSearch(e.target.value)} />
        {search && <button className="sort-clear" onClick={() => setSearch('')}>Clear filter</button>}
        <span className="result-count" aria-live="polite">{visibleRows.length} / {results.length} results</span>
        <span className="header-spacer" />
        <button className={`range-readout${ranged ? ' range-readout-on' : ''}`} onClick={() => setShowControls(true)}
          title="Each run's result is over the trades closed in these JST days. Change it in Controls.">
          <span className="range-label">Closed (JST)</span> {shownRange}
        </button>
        {deleteMode && <button className="btn-bulk-delete" disabled={loading || deleting || !toDelete.length} onClick={removeSelected}>{deleting ? 'Deleting…' : `Delete (${toDelete.length})`}</button>}
        <button className="btn-controls" onClick={() => setShowControls(true)} aria-haspopup="dialog">
          Controls{sortKeys.length || hidden.size || deleteMode ? ' •' : ''}
        </button>
      </div>
    </header>
    {showControls && <ControlsModal onClose={() => setShowControls(false)}
      onRefresh={() => setRefresh(v => v + 1)} refreshing={loading || deleting}
      sortKeys={sortKeys} onSortKeys={setSortKeys} columns={columns}
      range={range} onRange={setRange} firstDay={firstDay} lastDay={lastDay}
      deleteMode={deleteMode} onDeleteMode={on => { setDeleteMode(on); setSelectedIdxs(new Set()) }}
      deleteCount={toDelete.length} deleting={deleting} onDelete={removeSelected}
      hidden={hidden} onHidden={setHidden} />}
    <nav className="env-tabs" aria-label="Environments">{envs.map(name => <button key={name} className={`env-tab${selectedEnv === name ? ' env-tab-active' : ''}`} disabled={deleting} onClick={() => { window.location.hash = listHash(name) }}>{name}</button>)}</nav>
    <main className="app-main">{error && <div role="alert" className="alert-error">{error}</div>}
      {loading || rangeLoading ? <div className="loading">{loading ? 'Loading…' : 'Summarizing the range…'}</div> : <ResultsTable key={selectedEnv} results={results} search={search} onSearch={setSearch}
        sortKeys={sortKeys} onSortKeys={setSortKeys} hidden={hidden} onHidden={setHidden} onColumnsChange={setColumns} deleteMode={deleteMode} selectedIdxs={selectedIdxs} onToggleRow={toggleRow} onToggleAll={toggleAll} onVisibleRowsChange={setVisibleRows} runHref={row => runHash(row, range)} />}
    </main>
  </div>
}
