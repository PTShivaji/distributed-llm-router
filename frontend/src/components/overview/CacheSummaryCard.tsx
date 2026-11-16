import type { CacheStatsResponse } from '../../api/types'
import { formatBytes, formatInteger, formatPercent } from '../../lib/format'
import { Badge } from '../ui/Badge'
import { Card, CardBody, CardHeader } from '../ui/Card'

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <div className="rounded-md border border-slate-100 bg-slate-50/60 px-3 py-2">
      <dt className="truncate text-[11px] font-medium text-slate-500">{label}</dt>
      <dd className="mt-0.5 text-base font-semibold tabular-nums text-slate-900" data-testid={`cache-${label}`}>{value}</dd>
    </div>
  )
}

export function CacheSummaryCard({ stats }: { stats: CacheStatsResponse }) {
  const semanticBadge = !stats.enabled
    ? <Badge tone="neutral">Cache disabled</Badge>
    : stats.semantic_enabled
      ? <Badge tone="info">Semantic matching on</Badge>
      : <Badge tone="neutral">Exact match only</Badge>
  return (
    <Card>
      <CardHeader title="Response cache" description="From GET /api/v1/cache/stats (counters since last reset)" actions={semanticBadge} />
      <CardBody className="space-y-3">
        {stats.enabled && !stats.available && <Badge tone="danger">Redis unreachable: statistics unavailable</Badge>}
        {/* 2x2 in the narrow sidebar column (xl) and on phones; one row on tablet-width full cards */}
        <dl className="grid grid-cols-2 gap-2 md:grid-cols-4 xl:grid-cols-2">
          <Stat label="Exact hits" value={formatInteger(stats.hits_exact)} />
          <Stat label="Semantic hits" value={formatInteger(stats.hits_semantic)} />
          <Stat label="Misses" value={formatInteger(stats.misses)} />
          <Stat label="Hit rate" value={formatPercent(stats.hit_rate)} />
        </dl>
        <dl className="divide-y divide-slate-100 text-xs">
          {([
            ['Entries', formatInteger(stats.total_entries)],
            ['Approx. size', formatBytes(stats.approx_storage_bytes)],
            ['Embedder', `${stats.embedding_provider ?? '—'}${stats.embedding_provider && !stats.embedding_is_semantic ? ' (lexical)' : ''}`],
          ] as const).map(([label, value]) => (
            <div key={label} className="flex items-baseline justify-between gap-3 py-1.5">
              <dt className="shrink-0 text-slate-500">{label}</dt>
              <dd className="min-w-0 break-all text-right tabular-nums text-slate-700">{value}</dd>
            </div>
          ))}
        </dl>
        {stats.notes.length > 0 && (
          <ul className="space-y-1 text-xs text-slate-500">
            {stats.notes.map((n) => <li key={n}>· {n}</li>)}
          </ul>
        )}
      </CardBody>
    </Card>
  )
}
