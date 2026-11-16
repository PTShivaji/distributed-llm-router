import { useState, type ReactNode } from 'react'
import { CheckCircle2, Database, Gauge, HardDrive, Info, Layers, RefreshCw, ShieldAlert, Target, Trash2, XCircle, Zap } from 'lucide-react'

import { api } from '../api/endpoints'
import type { CacheClearResponse, CacheStatsResponse } from '../api/types'
import { ClearCacheDialog } from '../components/cache/ClearCacheDialog'
import { CacheLookupSplit } from '../components/cache/CacheLookupSplit'
import { SectionError } from '../components/feedback/SectionError'
import { SkeletonRows } from '../components/feedback/States'
import { ConnectionStatus } from '../components/layout/ConnectionStatus'
import { Badge, type BadgeTone } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { Card, CardBody, CardHeader } from '../components/ui/Card'
import { KpiCard } from '../components/ui/KpiCard'
import { PageHeader } from '../components/ui/PageHeader'
import { useApiQuery } from '../hooks/useApiQuery'
import { refreshBackendHealth } from '../hooks/useBackendHealth'
import { cacheMode, countersMeasured, embedderView, lookups, measured, type CacheMode } from '../lib/cache'
import { formatBytes, formatClock, formatInteger, formatPercent } from '../lib/format'

const MODE: Record<CacheMode, { label: string; tone: BadgeTone }> = {
  disabled: { label: 'Cache disabled', tone: 'neutral' },
  unavailable: { label: 'Redis unreachable', tone: 'danger' },
  exact_only: { label: 'Exact match only', tone: 'success' },
  semantic: { label: 'Exact + semantic', tone: 'info' },
  semantic_lexical: { label: 'Exact + lexical approximate (unsafe)', tone: 'warning' },
}

const UNAVAILABLE = 'Unavailable'
const num = (v: number | null) => (v === null ? UNAVAILABLE : formatInteger(v))

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-0.5 px-3 py-2">
      <dt className="text-slate-500">{label}</dt>
      <dd className="min-w-0 break-all text-right text-slate-900">{children}</dd>
    </div>
  )
}

function OnOff({ on, onLabel = 'Enabled', offLabel = 'Disabled' }: { on: boolean; onLabel?: string; offLabel?: string }) {
  return on
    ? <Badge tone="success" icon={<CheckCircle2 className="h-3 w-3" aria-hidden="true" />}>{onLabel}</Badge>
    : <Badge tone="neutral" icon={<XCircle className="h-3 w-3" aria-hidden="true" />}>{offLabel}</Badge>
}

interface ClearRecord { result: CacheClearResponse; at: number }

/**
 * Cache Management: GET /api/v1/cache/stats (observability token) and DELETE /api/v1/cache (admin token, per request).
 * GET /cache/stats is NOT side-effect free: it prunes expired index entries and trims each scope to its size cap.
 * So it is fetched on open and on explicit Refresh only: never polled, never re-fetched automatically after a clear.
 */
export function CacheManagementPage() {
  const [refreshKey, setRefreshKey] = useState(0)
  const stats = useApiQuery((signal) => api.cacheStats({ signal }), refreshKey)
  const [dialogOpen, setDialogOpen] = useState(false)
  const [lastClear, setLastClear] = useState<ClearRecord | null>(null)

  const refresh = () => {
    setRefreshKey((k) => k + 1)
    void refreshBackendHealth()
  }

  const s: CacheStatsResponse | null = stats.data
  const mode = s ? cacheMode(s) : null
  const live = s ? countersMeasured(s) : false
  const embedder = s ? embedderView(s) : null
  const loading = stats.loading && !s
  const staleAfterClear = lastClear !== null && (stats.updatedAt ?? 0) < lastClear.at
  const status = stats.error
    ? (s ? 'Refresh failed · showing earlier data' : 'Not loaded')
    : stats.loading ? 'Loading…' : stats.updatedAt ? `Updated ${formatClock(new Date(stats.updatedAt))}` : 'Loading…'
  const counterHint = s && !live ? (s.enabled ? 'Redis unreachable: not measured' : 'Cache disabled: not measured') : undefined

  return (
    <div className="space-y-6">
      <PageHeader
        title="Cache Management"
        description="Response cache status, hit statistics and storage from the backend, plus an admin-only, confirmed cache clear."
        actions={(
          <div className="flex flex-wrap items-center justify-end gap-2">
            <span className="hidden sm:inline-flex"><ConnectionStatus /></span>
            <span className="text-xs text-slate-500" aria-live="polite">{status}</span>
            <Button variant="secondary" size="sm" onClick={refresh} loading={stats.loading}
              icon={<RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />}>Refresh</Button>
          </div>
        )}
      />

      <p className="flex gap-2 rounded-lg border border-slate-200 bg-white p-3 text-xs text-slate-600 shadow-sm" role="note" data-testid="stats-side-effect">
        <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-400" aria-hidden="true" />
        <span>
          <strong className="font-medium text-slate-700">Operational note:</strong> reading <code className="font-mono">GET /api/v1/cache/stats</code> is
          not strictly read-only. While counting, the backend removes expired entries from each scope's index and trims scopes that exceed
          their size cap. This page therefore loads statistics once when opened and again only when you press Refresh; it never polls.
        </span>
      </p>

      {/* Status */}
      <Card>
        <CardHeader title="Cache status" description="GET /api/v1/cache/stats"
          actions={mode ? <Badge tone={MODE[mode].tone}>{MODE[mode].label}</Badge> : undefined} />
        <CardBody>
          {stats.error && <div className="mb-4"><SectionError error={stats.error} credential="observability" onRetry={refresh} /></div>}
          {loading ? <SkeletonRows rows={5} /> : s && (
            <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
              <dl className="divide-y divide-slate-100 rounded-md border border-slate-200 text-sm" aria-label="Configuration">
                <Row label="Response cache"><OnOff on={s.enabled} /></Row>
                <Row label="Redis reachable">{s.enabled ? <OnOff on={s.available} onLabel="Yes" offLabel="No" /> : <span className="text-slate-400">Not applicable</span>}</Row>
                <Row label="Semantic (approximate) hits"><OnOff on={s.semantic_enabled} /></Row>
                <Row label="Embedding provider"><span className="font-mono text-xs">{embedder!.label}</span></Row>
                <Row label="Embeddings are semantic">
                  {s.embedding_provider
                    ? (s.embedding_is_semantic ? <Badge tone="info">Yes (meaning-aware)</Badge> : <Badge tone="neutral">No (lexical)</Badge>)
                    : <span className="text-slate-400">{UNAVAILABLE}</span>}
                </Row>
                <Row label="Similarity threshold">{s.similarity_threshold === null ? <span className="text-slate-400">{UNAVAILABLE}</span> : <span className="tabular-nums">{s.similarity_threshold.toFixed(2)}</span>}</Row>
                <Row label="Entry TTL">{s.ttl_seconds === null ? <span className="text-slate-400">{UNAVAILABLE}</span> : <span className="tabular-nums">{formatInteger(s.ttl_seconds)} s</span>}</Row>
              </dl>
              <div className="space-y-3">
                <p className="text-xs text-slate-600">{embedder!.description}</p>
                <div>
                  <h3 className="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">Backend notes</h3>
                  {s.notes.length === 0 ? <p className="text-xs text-slate-400">No notes reported.</p> : (
                    <ul className="space-y-1.5 text-xs text-slate-700" aria-label="Backend notes" data-testid="backend-notes">
                      {s.notes.map((n, i) => (
                        <li key={`${i}-${n}`} className={`rounded-md px-2.5 py-1.5 ${n.startsWith('WARNING') ? 'bg-amber-50 text-amber-900' : 'bg-slate-50'}`}>{n}</li>
                      ))}
                    </ul>
                  )}
                </div>
              </div>
            </div>
          )}
        </CardBody>
      </Card>

      {/* Statistics */}
      <section aria-labelledby="cache-stats" className="space-y-3">
        <div className="flex flex-wrap items-baseline justify-between gap-x-4 gap-y-1">
          <h2 id="cache-stats" className="text-sm font-semibold text-slate-900">Statistics</h2>
          <span className="text-xs text-slate-500">Counters since the last statistics reset · all scopes</span>
        </div>
        {staleAfterClear && (
          <p className="rounded-md bg-amber-50 p-2.5 text-xs text-amber-900" role="status" data-testid="stale-after-clear">
            These statistics were loaded before the cache was cleared. Press Refresh to load current values.
          </p>
        )}
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <KpiCard label="Exact hits" icon={<Target className="h-4 w-4" />} tone="success" loading={loading}
            value={s ? num(measured(s, s.hits_exact)) : '—'} hint={counterHint} />
          <KpiCard label="Semantic hits" icon={<Zap className="h-4 w-4" />} tone="brand" loading={loading}
            value={s ? num(measured(s, s.hits_semantic)) : '—'}
            hint={counterHint ?? (s && !s.semantic_enabled ? 'Semantic hits are disabled' : undefined)} />
          <KpiCard label="Misses" icon={<XCircle className="h-4 w-4" />} tone="danger" loading={loading}
            value={s ? num(measured(s, s.misses)) : '—'} hint={counterHint} />
          <KpiCard label="Hit rate" icon={<Gauge className="h-4 w-4" />} tone="brand" loading={loading}
            value={s && live ? formatPercent(s.hit_rate) : (s ? UNAVAILABLE : '—')}
            hint={s && live ? (s.hit_rate === null ? 'No lookups yet' : `${formatInteger(lookups(s))} lookups`) : counterHint} />
          <KpiCard label="Total entries" icon={<Database className="h-4 w-4" />} loading={loading}
            value={s ? num(s.total_entries) : '—'} hint={s && s.total_entries === 0 ? 'Cache is empty' : undefined} />
          <KpiCard label="Scopes" icon={<Layers className="h-4 w-4" />} loading={loading}
            value={s ? num(s.scopes) : '—'} hint={s?.scopes != null ? 'Model + settings partitions' : undefined} />
          <KpiCard label="Approx. storage" icon={<HardDrive className="h-4 w-4" />} loading={loading}
            value={s ? (s.approx_storage_bytes === null ? UNAVAILABLE : formatBytes(s.approx_storage_bytes)) : '—'}
            hint={s?.approx_storage_bytes != null ? 'Estimate (Redis MEMORY USAGE sampling)' : undefined} />
          <KpiCard label="Stores" icon={<CheckCircle2 className="h-4 w-4" />} loading={loading}
            value={s ? num(measured(s, s.stores)) : '—'} hint={counterHint ?? (s ? 'Answers written to the cache' : undefined)} />
        </div>
        {s && !loading && (
          <div className="rounded-lg border border-slate-200 bg-white p-4 shadow-sm">
            <CacheLookupSplit stats={s} />
          </div>
        )}
      </section>

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,3fr)_minmax(0,2fr)]">
        {/* How it works */}
        <Card>
          <CardHeader title="How the response cache works" description="What the statistics above mean" />
          <CardBody className="space-y-3 text-sm text-slate-700" data-testid="cache-explainer">
            <div>
              <h3 className="font-semibold text-slate-900">Exact-match caching</h3>
              <p className="text-slate-600">A prompt is normalised and hashed (SHA-256). If the same hash is already cached in the same <em>scope</em>, the stored
                answer is returned. A scope combines the model, provider, provider model, temperature, output-token limit, mock mode, embedder and the
                backend&apos;s cache format version, so an answer is never reused across different models or settings. The prompt text itself is not stored.</p>
            </div>
            <div>
              <h3 className="font-semibold text-slate-900">Semantic (approximate) caching</h3>
              <p className="text-slate-600">When enabled, a prompt without an exact match is compared with stored prompt vectors in its scope; a stored answer is
                reused if the similarity reaches the threshold{s?.similarity_threshold != null ? ` (${s.similarity_threshold.toFixed(2)})` : ''}.</p>
            </div>
            <div>
              <h3 className="font-semibold text-slate-900">Lexical vs. semantic embeddings</h3>
              <p className="text-slate-600">The default local hashing embedder is free and runs offline, but it only measures shared words: two prompts with the
                same words and a different meaning (other names, numbers or word order) can look similar. A meaning-aware embedder calls a provider&apos;s
                embedding API, which is a paid request on every lookup that needs a vector.
                {embedder?.kind === 'lexical' && ' This backend uses the lexical embedder, so no semantic understanding is claimed here.'}</p>
            </div>
            <div>
              <h3 className="font-semibold text-slate-900">Why semantic hits may be disabled</h3>
              <p className="text-slate-600">They are off by default. Even when requested, the backend refuses approximate hits with the lexical embedder because it
                cannot guarantee two prompts are equivalent (an explicit unsafe override exists). Exact-match caching keeps working either way.
                The API does not report the cache format version; entries written by an incompatible version are simply never matched and expire with their TTL.</p>
            </div>
            <div>
              <h3 className="font-semibold text-slate-900">Why hits save calls and spend</h3>
              <p className="text-slate-600">A hit returns the stored answer without calling any provider, so it uses no provider tokens and adds no estimated cost.
                Each chat response reports its cache status and the tokens and cost it saved (see Chat Playground).</p>
            </div>
          </CardBody>
        </Card>

        {/* Admin */}
        <Card>
          <CardHeader title="Clear the cache" description="DELETE /api/v1/cache · requires ADMIN_API_TOKEN" />
          <CardBody className="space-y-4">
            <p className="flex gap-2 text-xs text-slate-600">
              <ShieldAlert className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" aria-hidden="true" />
              Admin action. The admin token is entered in the confirmation dialog, used for that one request and discarded. It is never saved,
              not even for this tab. Failed clears are never retried automatically.
            </p>
            <Button variant="danger" size="md" onClick={() => setDialogOpen(true)} icon={<Trash2 className="h-4 w-4" aria-hidden="true" />}>
              Clear cache…
            </Button>
            {lastClear && (
              <div className="space-y-1 rounded-md border border-emerald-200 bg-emerald-50 p-3 text-sm" role="status" data-testid="clear-result">
                <p className="font-semibold text-emerald-900">
                  {lastClear.result.cleared ? 'Cache cleared' : 'Cache not cleared'} at {formatClock(new Date(lastClear.at))}
                </p>
                <dl className="text-xs text-emerald-900">
                  <div className="flex justify-between gap-4"><dt>Deleted Redis keys</dt><dd className="font-semibold tabular-nums" data-testid="deleted-keys">{formatInteger(lastClear.result.deleted_keys)}</dd></div>
                  <div className="flex justify-between gap-4"><dt>Statistics reset</dt><dd className="font-semibold">{lastClear.result.reset_stats ? 'Yes' : 'No (counters kept)'}</dd></div>
                </dl>
                <p className="text-[11px] text-emerald-800">
                  Deleted keys are Redis keys (entries plus per-scope indexes, vectors and locks), not a count of cached answers.
                  {lastClear.result.deleted_keys === 0 && ' Nothing was cached.'}
                </p>
              </div>
            )}
          </CardBody>
        </Card>
      </div>

      <ClearCacheDialog open={dialogOpen} onClose={() => setDialogOpen(false)}
        onCleared={(result) => setLastClear({ result, at: Date.now() })} />
    </div>
  )
}
