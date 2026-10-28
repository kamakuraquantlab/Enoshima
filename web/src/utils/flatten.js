export function flatten(obj, prefix = '', out = {}) {
  for (const [k, v] of Object.entries(obj ?? {})) {
    if (k === '_idx') continue
    const key = prefix ? `${prefix}.${k}` : k
    if (v !== null && typeof v === 'object' && !Array.isArray(v)) {
      flatten(v, key, out)
    } else {
      out[key] = v
    }
  }
  return out
}

function fmtTs(sec) {
  const d = new Date(sec * 1000)
  const p = n => String(n).padStart(2, '0')
  return `${d.getFullYear()}-${p(d.getMonth()+1)}-${p(d.getDate())} ${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}`
}

export function formatValue(v, col = '') {
  if (v === null || v === undefined) return ''
  if (typeof v === 'number') {
    if (col === 'start_ts' || col === 'end_ts') return fmtTs(v)
    if (Number.isInteger(v)) return v.toString()
    if (col.includes('slippage_rate') || col.includes('box_size_rate')) return v.toFixed(6)
    return v.toFixed(3)
  }
  return String(v)
}
