import { useState, useEffect, lazy, Suspense } from 'react'
import { fetchResults, fetchTrainings, deleteResult } from './api'
import ResultsTable from './components/ResultsTable'
import TrainingsTable from './components/TrainingsTable'
// The charts are most of the bundle; the table does not wait for them.
const RunView = lazy(() => import('./components/RunView'))
const TrainingView = lazy(() => import('./components/TrainingView'))
const RunWalkForwardView = lazy(() => import('./components/RunWalkForwardView'))

// #/results/<env> lists runs, #/training/<env> walk-forward trainings.
function hashList() {
  try {
    const m = window.location.hash.match(/^#\/(results|training)\/(.*)$/)
    return { page: m?.[1] || 'results', env: decodeURIComponent(m?.[2] || '') }
  } catch { return { page: 'results', env: '' } }
}
const hashEnv = () => hashList().env

// #/run/<source>/<run_id>: source is the env name (once the env's folder,
// e.g. backtest/NAME, whose last component is the env name: such a link still works).
// #/wf/<source>/<run_id> is the same run against its group's walk-forward rules.
function hashRun(kind = 'run') {
  try {
    const m = window.location.hash.match(new RegExp(`^#/${kind}/([^/]+)/([^/]+)$`))
    if (!m) return null
    const source = decodeURIComponent(m[1])
    return { source, env: source.split('/').pop(), runId: decodeURIComponent(m[2]) }
  } catch { return null }
}
const runHash = row => `#/run/${encodeURIComponent(row.source)}/${encodeURIComponent(row.run_id ?? row.runId)}`
const walkForwardHash = row => `#/wf/${encodeURIComponent(row.source)}/${encodeURIComponent(row.run_id)}`

// #/train/<source>/<train_id>: a training's chart page.
function hashTraining() {
  try {
    const m = window.location.hash.match(/^#\/train\/([^/]+)\/([^/]+)$/)
    if (!m) return null
    const source = decodeURIComponent(m[1])
    return { source, env: source.split('/').pop(), trainId: decodeURIComponent(m[2]) }
  } catch { return null }
}
const trainingHash = row => `#/train/${encodeURIComponent(row.source)}/${encodeURIComponent(row.train_id)}`
export default function App() {
  const [allResults, setAllResults] = useState([])
  const [allTrainings, setAllTrainings] = useState([])
  const [env, setEnv] = useState(hashEnv)
  const [page, setPage] = useState(() => hashList().page)
  const [run, setRun] = useState(hashRun)
  const [training, setTraining] = useState(hashTraining)
  const [walkForward, setWalkForward] = useState(() => hashRun('wf'))
  const [search, setSearch] = useState('')
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [refresh, setRefresh] = useState(0)
  const [deleteMode, setDeleteMode] = useState(false)
  const [deleting, setDeleting] = useState(false)
  const [selectedIdxs, setSelectedIdxs] = useState(new Set())
  const [visibleRows, setVisibleRows] = useState([])
  useEffect(() => {
    const controller = new AbortController()
    setLoading(true); setError(''); setSelectedIdxs(new Set())
    Promise.all([fetchResults(controller.signal), fetchTrainings(controller.signal)]).then(([results, trainings]) => {
      setAllResults(results); setAllTrainings(trainings)
    }).catch(e => {
      if (e.name !== 'AbortError') setError(e.message)
    }).finally(() => { if (!controller.signal.aborted) setLoading(false) })
    return () => controller.abort()
  }, [refresh])
  useEffect(() => {
    const onHash = () => {
      const next = hashRun(), nextTraining = hashTraining(), nextWalkForward = hashRun('wf')
      setRun(next); setTraining(nextTraining); setWalkForward(nextWalkForward)
      if (next || nextTraining || nextWalkForward) return
      // Runs selected for deletion are not carried onto another page.
      setPage(previous => {
        const current = hashList().page
        if (current !== previous) { setSelectedIdxs(new Set()); setDeleteMode(false) }
        return current
      })
      setEnv(previous => {
        const current = hashEnv()
        if (current !== previous) { setSelectedIdxs(new Set()); setSearch('') }
        return current
      })
    }
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  const envs = [...new Set([...allResults, ...allTrainings].map(row => row.env_name))].sort()
  const selectedEnv = env || envs[0] || ''
  const results = allResults.filter(row => row.env_name === selectedEnv)
  const trainings = allTrainings.filter(row => row.env_name === selectedEnv)
  const listHash = (p, name) => `#/${p}/${encodeURIComponent(name)}`
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
    <header className="app-header"><div className="app-title"><span className="app-logo">▲</span><h1>Enoshima</h1><span className="app-subtitle">Enoshima results</span></div></header>
    <main className="app-main"><Suspense fallback={<div className="loading">Loading…</div>}><RunView target={run} onBack={() => { window.location.hash = `#/results/${encodeURIComponent(run.env)}` }} /></Suspense></main>
  </div>
  if (walkForward) return <div className="app">
    <header className="app-header"><div className="app-title"><span className="app-logo">▲</span><h1>Enoshima</h1><span className="app-subtitle">Enoshima results</span></div></header>
    <main className="app-main"><Suspense fallback={<div className="loading">Loading…</div>}><RunWalkForwardView target={walkForward} runHref={runHash} trainingHref={trainingHash} onBack={() => { window.location.hash = `#/results/${encodeURIComponent(walkForward.env)}` }} /></Suspense></main>
  </div>
  if (training) return <div className="app">
    <header className="app-header"><div className="app-title"><span className="app-logo">▲</span><h1>Enoshima</h1><span className="app-subtitle">Enoshima results</span></div></header>
    <main className="app-main"><Suspense fallback={<div className="loading">Loading…</div>}><TrainingView target={training} onBack={() => { window.location.hash = listHash('training', training.env) }} /></Suspense></main>
  </div>
  return <div className="app">
    <header className="app-header"><div className="app-title"><span className="app-logo">▲</span><h1>Enoshima</h1><span className="app-subtitle">Enoshima results</span></div>
      <nav className="page-nav" aria-label="Page">{[['results', `Runs (${results.length})`], ['training', `Training (${trainings.length})`]].map(([p, text]) =>
        <button key={p} className={`page-nav-btn${page === p ? ' page-nav-active' : ''}`} onClick={() => { window.location.hash = listHash(p, selectedEnv) }}>{text}</button>)}</nav>
      <div className="app-controls"><input aria-label="Filter results by keyword" type="search" className="search-input" placeholder="Filter by keyword…" value={search} onChange={e => setSearch(e.target.value)} />
        {search && <button className="sort-clear" onClick={() => setSearch('')}>Clear filter</button>}
        <button className="btn-disable-delete" disabled={loading || deleting} onClick={() => setRefresh(v => v+1)}>Refresh</button>
        {page === 'results' && <button className="btn-enable-delete" disabled={deleting} onClick={() => { setDeleteMode(v => !v); setSelectedIdxs(new Set()) }}>{deleteMode ? 'Disable Delete' : 'Enable Delete'}</button>}
        {page === 'results' && deleteMode && <button className="btn-bulk-delete" disabled={loading || deleting || !toDelete.length} onClick={removeSelected}>{deleting ? 'Deleting…' : `Delete (${toDelete.length})`}</button>}
      </div>
    </header>
    <nav className="env-tabs" aria-label="Environments">{envs.map(name => <button key={name} className={`env-tab${selectedEnv === name ? ' env-tab-active' : ''}`} disabled={deleting} onClick={() => { window.location.hash = listHash(page, name) }}>{name}</button>)}</nav>
    <main className="app-main">{error && <div role="alert" className="alert-error">{error}</div>}
      {loading ? <div className="loading">Loading…</div> : page === 'training' ? <TrainingsTable trainings={trainings} href={trainingHash} /> : <ResultsTable key={selectedEnv} results={results} search={search} onSearch={setSearch} deleteMode={deleteMode} selectedIdxs={selectedIdxs} onToggleRow={toggleRow} onToggleAll={toggleAll} onVisibleRowsChange={setVisibleRows} runHref={runHash} walkForwardHref={walkForwardHash} />}
    </main>
  </div>
}
