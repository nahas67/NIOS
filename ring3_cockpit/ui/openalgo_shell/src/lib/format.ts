export function formatUsd(value: number, digits = 0): string {
  return new Intl.NumberFormat('en-US', {
    style: 'currency',
    currency: 'USD',
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(Number.isFinite(value) ? value : 0)
}

export function formatCompactUsd(value: number): string {
  if (Math.abs(value) >= 1_000_000_000) return `$${(value / 1_000_000_000).toFixed(2)}B`
  if (Math.abs(value) >= 1_000_000) return `$${(value / 1_000_000).toFixed(2)}M`
  if (Math.abs(value) >= 1_000) return `$${(value / 1_000).toFixed(1)}K`
  return formatUsd(value, 2)
}

export function microsToUsd(micros: number): number {
  return Number.isFinite(micros) ? micros / 1_000_000 : 0
}

export function formatBps(value: number): string {
  if (!Number.isFinite(value)) return 'n/a'
  return `${value >= 0 ? '+' : ''}${value.toFixed(1)} bps`
}

export function formatPct(value: number, digits = 1): string {
  if (!Number.isFinite(value)) return 'n/a'
  return `${(value * 100).toFixed(digits)}%`
}

export function truncateDigest(digest: string | null): string {
  if (!digest) return '—'
  return `${digest.slice(0, 10)}…${digest.slice(-6)}`
}

export function formatClock(epochNs: number): string {
  if (!epochNs) return '—'
  const millis = epochNs > 1e15 ? epochNs / 1e6 : epochNs
  return new Date(millis).toLocaleTimeString('en-US', { hour12: false })
}

export function timeAgo(epochMs: number): string {
  if (!epochMs) return 'never'
  const seconds = Math.max(0, Math.round((Date.now() - epochMs) / 1000))
  if (seconds < 2) return 'now'
  if (seconds < 60) return `${seconds}s ago`
  return `${Math.floor(seconds / 60)}m ago`
}
