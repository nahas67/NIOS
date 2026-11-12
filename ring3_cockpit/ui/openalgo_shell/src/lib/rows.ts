/** Read a loosely-typed telemetry row without inventing values. */
export function cell(row: Record<string, unknown>, key: string): string {
  const value = row[key]
  if (value === null || value === undefined || value === '') return '—'
  if (typeof value === 'number') {
    return Number.isInteger(value) ? String(value) : value.toFixed(2)
  }
  if (typeof value === 'boolean') return value ? 'true' : 'false'
  if (typeof value === 'string') return value
  if (Array.isArray(value)) return value.length === 0 ? '—' : `${value.length} rows`
  return '—'
}

/** Numeric projection of a loosely-typed field, defaulting to zero. */
export function num(row: Record<string, unknown>, key: string): number {
  const value = row[key]
  return typeof value === 'number' && Number.isFinite(value) ? value : 0
}

/** Stable React key for a telemetry row that has no guaranteed id. */
export function rowKey(row: Record<string, unknown>, fallback: number): string {
  for (const key of ['order_id', 'outbox_idempotency_key', 'slicer_id', 'strategy_id']) {
    const value = row[key]
    if (typeof value === 'string' && value.length > 0) return value
  }
  return `row-${fallback}`
}
