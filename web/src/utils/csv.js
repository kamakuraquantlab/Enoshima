// trades.csv is written by Python's csv module from plain numbers and words:
// no quoting to undo. Numeric columns become numbers, empty cells null.
export function parseCsv(text) {
  const lines = text.trim().split(/\r?\n/)
  if (!lines.length || !lines[0]) return []
  const header = lines[0].split(',')
  return lines.slice(1).map(line => {
    const cells = line.split(',')
    const row = {}
    header.forEach((col, i) => {
      const v = cells[i]
      if (v === '' || v === undefined) row[col] = null
      else { const n = Number(v); row[col] = Number.isNaN(n) ? v : n }
    })
    return row
  })
}
