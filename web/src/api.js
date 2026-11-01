const BASE = import.meta.env.BASE_URL

async function fail(response, fallback) {
  const body = await response.json().catch(() => ({}))
  throw new Error(body.error || fallback)
}

// With a range ({env, start, end}, JST dates), each run's result is over the trades closed in it.
export async function fetchResults(signal, range) {
  const query = range ? `?${new URLSearchParams(Object.entries(range).filter(([, v]) => v))}` : ''
  const response = await fetch(`${BASE}api/results${query}`, { signal })
  if (!response.ok) await fail(response, 'Could not load results. Check the API and the data lake.')
  return response.json()
}

function runUrl(env, runId, source) {
  return `${BASE}api/runs/${encodeURIComponent(env)}/${encodeURIComponent(runId)}`
}

export async function fetchRun({ env, runId, source }, signal) {
  const response = await fetch(`${runUrl(env, runId)}?source=${encodeURIComponent(source)}`, { signal })
  if (!response.ok) await fail(response, 'Could not load run')
  return response.json()
}

export async function fetchRunFile({ env, runId, source }, name, signal) {
  const response = await fetch(`${runUrl(env, runId)}/files/${encodeURIComponent(name)}?source=${encodeURIComponent(source)}`, { signal })
  if (!response.ok) await fail(response, `Could not load ${name}`)
  return name.endsWith('.json') ? response.json() : response.text()
}

export async function deleteResult(row) {
  const url = `${BASE}api/results/${encodeURIComponent(row.env_name)}/${encodeURIComponent(row.run_id)}?source=${encodeURIComponent(row.source)}`
  const response = await fetch(url, { method: 'DELETE' })
  if (!response.ok) await fail(response, 'Could not delete run')
}

// A market's last trade price per step over [start, end) epoch seconds, from the recorded data.
export async function fetchPrices(market, start, end, signal) {
  const query = new URLSearchParams({ market, start, end })
  const response = await fetch(`${BASE}api/prices?${query}`, { signal })
  if (!response.ok) await fail(response, `Could not load ${market} prices`)
  return response.json()
}
