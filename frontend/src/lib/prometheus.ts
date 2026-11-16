/**
 * Minimal parser for the Prometheus text exposition format (GET /metrics) and the few
 * router counters the Routing Analytics page shows. Counter values accumulate since the
 * backend PROCESS started and are per instance: they are not filtered by any date range.
 */

export interface Sample {
  name: string
  labels: Record<string, string>
  value: number
}

const SAMPLE = /^([a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(.*)\})?\s+(\S+)(?:\s+-?\d+)?\s*$/
const LABEL = /([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"\s*,?\s*/gy

function unescape(v: string): string {
  return v.replace(/\\(["\\n])/g, (_, c: string) => (c === 'n' ? '\n' : c))
}

function parseValue(raw: string): number {
  if (raw === '+Inf') return Infinity
  if (raw === '-Inf') return -Infinity
  if (raw === 'NaN') return NaN
  return Number(raw)
}

/** Parses sample lines; comments, blank lines and malformed lines are skipped. */
export function parsePrometheus(text: string): Sample[] {
  const out: Sample[] = []
  for (const line of text.split('\n')) {
    const trimmed = line.trim()
    if (trimmed === '' || trimmed.startsWith('#')) continue
    const m = SAMPLE.exec(trimmed)
    if (!m) continue
    const labels: Record<string, string> = {}
    if (m[2]) {
      LABEL.lastIndex = 0
      let lm: RegExpExecArray | null
      let consumed = 0
      while ((lm = LABEL.exec(m[2])) !== null) {
        labels[lm[1]] = unescape(lm[2])
        consumed = LABEL.lastIndex
      }
      if (consumed !== m[2].length) continue // malformed label set
    }
    const value = parseValue(m[3])
    if (Number.isNaN(value) && m[3] !== 'NaN') continue
    out.push({ name: m[1], labels, value })
  }
  return out
}

/** Sums samples of `name` grouped by the given label(s); zero-valued groups are dropped. Sorted by value desc. */
export function sumBy(samples: readonly Sample[], name: string, keys: readonly string[]): { labels: Record<string, string>; value: number }[] {
  const groups = new Map<string, { labels: Record<string, string>; value: number }>()
  for (const s of samples) {
    if (s.name !== name || !Number.isFinite(s.value)) continue
    const labels = Object.fromEntries(keys.map((k) => [k, s.labels[k] ?? '']))
    const id = JSON.stringify(labels)
    const g = groups.get(id) ?? { labels, value: 0 }
    g.value += s.value
    groups.set(id, g)
  }
  return [...groups.values()].filter((g) => g.value > 0).sort((a, b) => b.value - a.value || JSON.stringify(a.labels).localeCompare(JSON.stringify(b.labels)))
}

export function total(samples: readonly Sample[], name: string, where: Record<string, string> = {}): number {
  return samples
    .filter((s) => s.name === name && Number.isFinite(s.value) && Object.entries(where).every(([k, v]) => s.labels[k] === v))
    .reduce((sum, s) => sum + s.value, 0)
}

export interface RouterCounters {
  /** At least one router series is present (otherwise the text is not this router's /metrics). */
  recognised: boolean
  chatRequests: number
  retries: { model: string; count: number }[]
  retriesTotal: number
  fallbacks: { from: string; to: string; count: number }[]
  fallbacksTotal: number
  /** Provider attempts that ended in an error, by error kind. */
  errorKinds: { kind: string; count: number }[]
}

export function routerCounters(samples: readonly Sample[]): RouterCounters {
  const retries = sumBy(samples, 'llm_router_retries_total', ['model']).map((g) => ({ model: g.labels.model, count: g.value }))
  const fallbacks = sumBy(samples, 'llm_router_fallbacks_total', ['from_model', 'to_model'])
    .map((g) => ({ from: g.labels.from_model, to: g.labels.to_model, count: g.value }))
  const errorKinds = sumBy(samples.filter((s) => s.labels.outcome === 'error'), 'llm_router_provider_attempts_total', ['error_kind'])
    .map((g) => ({ kind: g.labels.error_kind || 'unknown', count: g.value }))
  return {
    recognised: samples.some((s) => s.name.startsWith('llm_router_')),
    chatRequests: total(samples, 'llm_router_chat_requests_total'),
    retries,
    retriesTotal: retries.reduce((n, r) => n + r.count, 0),
    fallbacks,
    fallbacksTotal: fallbacks.reduce((n, f) => n + f.count, 0),
    errorKinds,
  }
}
