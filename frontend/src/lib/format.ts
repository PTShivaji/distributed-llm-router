/** Display formatting. `null`/`undefined` always render as an em dash (never as 0). */

export const NO_VALUE = '—'

const integer = new Intl.NumberFormat('en-US', { maximumFractionDigits: 0 })

export function formatInteger(value: number | null | undefined): string {
  return value === null || value === undefined || !Number.isFinite(value) ? NO_VALUE : integer.format(value)
}

/** `rate` is a 0..1 fraction as returned by the backend. */
export function formatPercent(rate: number | null | undefined, digits = 1): string {
  if (rate === null || rate === undefined || !Number.isFinite(rate)) return NO_VALUE
  return `${(rate * 100).toFixed(digits)}%`
}

export function formatLatency(ms: number | null | undefined): string {
  if (ms === null || ms === undefined || !Number.isFinite(ms)) return NO_VALUE
  if (ms >= 1000) return `${(ms / 1000).toFixed(2)} s`
  return `${ms < 10 ? ms.toFixed(2) : ms.toFixed(0)} ms`
}

/** USD with enough precision for sub-cent LLM costs. */
export function formatUsd(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return NO_VALUE
  if (value === 0) return '$0.00'
  const abs = Math.abs(value)
  const digits = abs >= 1 ? 2 : abs >= 0.01 ? 4 : 6
  return `$${value.toFixed(digits)}`
}

export function formatClock(date: Date): string {
  return date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' })
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes)) return NO_VALUE
  if (bytes < 1024) return `${bytes} B`
  if (bytes < 1024 ** 2) return `${(bytes / 1024).toFixed(1)} KB`
  return `${(bytes / 1024 ** 2).toFixed(1)} MB`
}
