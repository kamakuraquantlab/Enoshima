import { useState, useMemo, useEffect, useRef } from 'react'
import { flatten, formatValue } from '../utils/flatten'

const GrafanaIcon = () => (
  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor"
       strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />
  </svg>
)

const HIDDEN_LS_KEY = 'enoshima_hidden_cols'
const SORT_LS_KEY = 'enoshima_sort_keys'

const PINNED_COLS = [
  'start_ts', 'batch_id', 'recommended_in',
  'env_name', 'param_set_id', 'start_str', 'end_str',
  'result.pnl5', 'walk_forward.best_pnl5', 'walk_forward.best_rule', 'result.pnl_cnt0',
  'result.sharpe_ratio_annualized0', 'result.max_drawdown0',
  'execution_time', 'git_rev',
]

function loadHidden() {
  try {
    const s = localStorage.getItem(HIDDEN_LS_KEY)
    return s ? new Set(JSON.parse(s)) : new Set()
  } catch { return new Set() }
}

function saveHidden(set) {
  try {
    if (set.size === 0) localStorage.removeItem(HIDDEN_LS_KEY)
    else localStorage.setItem(HIDDEN_LS_KEY, JSON.stringify([...set]))
  } catch {}
}

function loadSortKeys() {
  try {
    const value = JSON.parse(localStorage.getItem(SORT_LS_KEY) || '[]')
    return Array.isArray(value)
      ? value.filter(key => key && typeof key.col === 'string' && typeof key.asc === 'boolean')
      : []
  } catch { return [] }
}

function saveSortKeys(keys) {
  try {
    if (keys.length === 0) localStorage.removeItem(SORT_LS_KEY)
    else localStorage.setItem(SORT_LS_KEY, JSON.stringify(keys))
  } catch {}
}

function colLabel(col) {
  return col.replace(/^walk_forward\./, 'wf ').replace(/^(result|params|config|trading)\./, '').replaceAll('_', ' ')
}

export default function ResultsTable({
  results, search, onSearch,
  deleteMode, selectedIdxs, onToggleRow, onToggleAll, onVisibleRowsChange, runHref, walkForwardHref,
}) {
  // sortKeys: [{col, asc}, ...] — head = primary sort
  const [sortKeys, setSortKeys] = useState(loadSortKeys)
  const [hidden, setHidden] = useState(loadHidden)
  const [activeCell, setActiveCell] = useState(null)

  const headerCheckRef = useRef(null)

  const flat = useMemo(() =>
    results.map((r, i) => ({ ...flatten(r), _rowIdx: i })),
    [results]
  )

  const allCols = useMemo(() => {
    const seen = new Set()
    for (const r of flat) {
      for (const k of Object.keys(r)) {
        if (k !== '_rowIdx' && k !== 'run_id' && k !== 'source' && k !== 'walk_forward.train_id' && k !== 'grafana_url') seen.add(k)
      }
    }
    return [...seen].sort((a, b) => {
      const ai = PINNED_COLS.indexOf(a), bi = PINNED_COLS.indexOf(b)
      if (ai !== -1 && bi !== -1) return ai - bi
      if (ai !== -1) return -1
      if (bi !== -1) return 1
      const ap = a.startsWith('params.'), bp = b.startsWith('params.')
      if (ap && !bp) return -1
      if (!ap && bp) return 1
      return a.localeCompare(b)
    })
  }, [flat])

  // A link only where grafana_dashboard.json in the API's env has a dashboard for the run.
  const hasGrafana = useMemo(() => flat.some(r => r.grafana_url), [flat])

  const visibleCols = useMemo(() => allCols.filter(c => !hidden.has(c)), [allCols, hidden])
  const hiddenCols  = useMemo(() => allCols.filter(c =>  hidden.has(c)), [allCols, hidden])

  const hideCol = (col) => {
    const next = new Set(hidden); next.add(col)
    setHidden(next); saveHidden(next)
  }

  const showCol = (col) => {
    const next = new Set(hidden); next.delete(col)
    setHidden(next); saveHidden(next)
  }

  const handleSort = (col) => {
    setSortKeys(prev => {
      const idx = prev.findIndex(k => k.col === col)
      if (idx === -1) {
        const next = [{ col, asc: false }, ...prev]
        saveSortKeys(next)
        return next
      }
      const entry = prev[idx]
      const rest = prev.filter((_, i) => i !== idx)
      const next = [{ col, asc: !entry.asc }, ...rest]
      saveSortKeys(next)
      return next
    })
  }

  const filtered = useMemo(() => {
    if (!search.trim()) return flat
    const q = search.trim().toLowerCase()
    return flat.filter(r =>
      Object.entries(r).some(([k, v]) => k !== '_rowIdx' && String(v ?? '').toLowerCase().includes(q))
    )
  }, [flat, search])

  const sorted = useMemo(() => {
    if (sortKeys.length === 0) return filtered
    return [...filtered].sort((a, b) => {
      for (const { col, asc } of sortKeys) {
        const av = a[col] ?? '', bv = b[col] ?? ''
        const cmp = typeof av === 'number' && typeof bv === 'number'
          ? av - bv : String(av).localeCompare(String(bv))
        if (cmp !== 0) return asc ? cmp : -cmp
      }
      return 0
    })
  }, [filtered, sortKeys])

  useEffect(() => { onVisibleRowsChange(sorted) }, [sorted, onVisibleRowsChange])

  useEffect(() => {
    if (!headerCheckRef.current || !deleteMode) return
    const numVisible = sorted.length
    const numChecked = sorted.filter(r => selectedIdxs.has(r._rowIdx)).length
    headerCheckRef.current.checked = numChecked > 0 && numChecked === numVisible
    headerCheckRef.current.indeterminate = numChecked > 0 && numChecked < numVisible
  }, [sorted, selectedIdxs, deleteMode])

  const handleHeaderCheck = (e) => {
    onToggleAll(e.target.checked)
  }

  if (!results.length) return <div className="empty">No results found.</div>

  return (
    <div className="table-wrap">
      <div className="table-toolbar">
        <span className="result-count">{sorted.length} / {results.length} results</span>
        {sortKeys.length > 0 && (
          <span className="sort-status">
            <span className="sort-status-label">sorted by</span>
            {sortKeys.map((k, i) => (
              <span key={k.col} className="sort-chip">
                {i > 0 && <span className="sort-chip-sep">›</span>}
                <span className="sort-chip-col">{colLabel(k.col)}</span>
                <span className="sort-chip-dir">{k.asc ? '↑' : '↓'}</span>
              </span>
            ))}
            <button
              className="sort-clear"
              onClick={() => { setSortKeys([]); saveSortKeys([]) }}
              title="Clear sort"
            >✕</button>
          </span>
        )}
      </div>

      <div className="table-scroll" onClick={e => { if (e.target === e.currentTarget) setActiveCell(null) }}>
        <table className="results-table">
          <thead>
            <tr>
              {deleteMode && (
                <th className="th-check">
                  <input
                    type="checkbox"
                    ref={headerCheckRef}
                    onChange={handleHeaderCheck}
                    title="Select all visible"
                  />
                </th>
              )}
              <th className="th-open" title="Open the run's charts" />
              <th className="th-open" title="Open the run against its group's walk-forward rules" />
              {hasGrafana && <th className="th-grafana" title="Open in Grafana">↗</th>}
              {visibleCols.map(col => {
                const keyIdx = sortKeys.findIndex(k => k.col === col)
                const isSorted = keyIdx !== -1
                const isAsc = isSorted ? sortKeys[keyIdx].asc : false
                return (
                  <th
                    key={col}
                    className={[
                      isSorted ? 'th-sorted' : '',
                      PINNED_COLS.includes(col) ? 'th-key' : '',
                      activeCell?.col === col ? 'th-col-active' : '',
                    ].filter(Boolean).join(' ')}
                    title={col}
                  >
                    <div className="th-content">
                      <span className="th-inner" onClick={() => handleSort(col)}>
                        <span className="th-label">{colLabel(col)}</span>
                        {isSorted && (
                          <span className="sort-arrow">
                            {isAsc ? '↑' : '↓'}
                            {sortKeys.length > 1 && (
                              <sup className="sort-rank">{keyIdx + 1}</sup>
                            )}
                          </span>
                        )}
                      </span>
                      <button
                        className="th-hide"
                        onClick={e => { e.stopPropagation(); hideCol(col) }}
                        title={`Hide ${col}`}
                      >×</button>
                    </div>
                  </th>
                )
              })}
            </tr>
          </thead>
          <tbody>
            {sorted.map((row) => {
              const pnl5 = row['result.pnl5']
              const isActiveRow = activeCell?.rowIdx === row._rowIdx
              const isChecked = deleteMode && selectedIdxs.has(row._rowIdx)
              const rowClass = [
                pnl5 == null ? '' : pnl5 > 0 ? 'row-pos' : 'row-neg',
                isActiveRow ? 'row-active' : '',
                isChecked ? 'row-checked' : '',
              ].filter(Boolean).join(' ')
              return (
                <tr key={row._rowIdx} className={rowClass}>
                  {deleteMode && (
                    <td className="td-check">
                      <input
                        type="checkbox"
                        checked={isChecked}
                        onChange={() => onToggleRow(row._rowIdx)}
                        onClick={e => e.stopPropagation()}
                      />
                    </td>
                  )}
                  <td className="td-open">
                    <a className="open-run" title="Open the run's charts in a new tab"
                      href={runHref(results[row._rowIdx])} target="_blank" rel="noreferrer"
                      onClick={e => e.stopPropagation()}>charts</a>
                  </td>
                  <td className="td-open">
                    {row['walk_forward.train_id'] != null &&
                      <a className="open-run" title="This run against its group's walk-forward rules, in a new tab"
                        href={walkForwardHref(results[row._rowIdx])} target="_blank" rel="noreferrer"
                        onClick={e => e.stopPropagation()}>walk-forward</a>}
                  </td>
                  {hasGrafana && <td className="td-grafana">
                    <a
                      hidden={!row.grafana_url}
                      href={row.grafana_url ?? undefined}
                      target="_blank"
                      rel="noreferrer"
                      className="grafana-link"
                      title="Open in Grafana"
                      onClick={e => e.stopPropagation()}
                    >
                      <GrafanaIcon />
                    </a>
                  </td>}
                  {visibleCols.map(col => {
                    const isActiveCol = activeCell?.col === col
                    const isActive = isActiveRow && isActiveCol
                    const cls = [
                      col === 'result.pnl5' || col === 'walk_forward.best_pnl5' ? 'cell-pnl' : '',
                      col === 'batch_id' ? 'cell-batch-id' : '',
                      isActiveCol ? 'cell-col-active' : '',
                      isActive ? 'cell-active' : '',
                    ].filter(Boolean).join(' ')
                    return (
                      <td key={col} className={cls} title={String(row[col] ?? '')}
                        onClick={() => {
                          setActiveCell(isActive ? null : { rowIdx: row._rowIdx, col })
                          if (col === 'batch_id' && row[col] != null) {
                            onSearch(String(row[col]))
                          } else {
                            setActiveCell(isActive ? null : { rowIdx: row._rowIdx, col })
                          }
                        }}
                      >
                        {formatValue(row[col], col)}
                      </td>
                    )
                  })}
                </tr>
              )
            })}
          </tbody>
        </table>
      </div>

      {hiddenCols.length > 0 && (
        <div className="hidden-badges">
          <span className="hidden-badges-label">Hidden:</span>
          {hiddenCols.map(col => (
            <button key={col} className="col-badge" onClick={() => showCol(col)} title={`Restore ${col}`}>
              {col.replace(/^(result|params|config|trading)\./, '')}
              <span className="badge-restore">+</span>
            </button>
          ))}
        </div>
      )}
    </div>
  )
}
