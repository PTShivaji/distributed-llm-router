import type { ReactNode } from 'react'
import { AlertTriangle, ArrowRight, Info, Shuffle } from 'lucide-react'

import type { RateLimitInfo } from '../../api/client'
import type { ChatResponse } from '../../api/types'
import { cacheLabel, intentLabel, isCacheHit, usageSourceLabel } from '../../lib/chat'
import { cn } from '../../lib/cn'
import { formatInteger, formatLatency, formatPercent, formatUsd } from '../../lib/format'
import { formatPricePerMillion } from '../../lib/registry'
import { Badge } from '../ui/Badge'

/** null/undefined are shown as "Unavailable", never as 0. */
function Value({ children, value }: { children?: ReactNode; value: unknown }) {
  if (value === null || value === undefined) return <span className="text-slate-400">Unavailable</span>
  return <>{children}</>
}

function Rows({ rows, label }: { rows: [string, ReactNode][]; label: string }) {
  return (
    <dl className="divide-y divide-slate-100 rounded-md border border-slate-200 bg-white text-sm" aria-label={label}>
      {rows.map(([name, value]) => (
        <div key={name} className="flex flex-wrap items-start justify-between gap-x-4 gap-y-0.5 px-3 py-1.5">
          <dt className="text-slate-500">{name}</dt>
          <dd className="min-w-0 text-right text-slate-900">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

function Panel({ title, icon, children, testId }: { title: string; icon?: ReactNode; children: ReactNode; testId: string }) {
  return (
    <section className="min-w-0 space-y-2" aria-label={title} data-testid={testId}>
      <h4 className="flex items-center gap-1.5 text-xs font-semibold uppercase tracking-wide text-slate-500">{icon}{title}</h4>
      {children}
    </section>
  )
}

const mono = (v: string) => <span className="break-all font-mono text-xs">{v}</span>
const ATTEMPT_TONE: Record<string, 'success' | 'danger' | 'warning' | 'neutral'> = { success: 'success', error: 'danger' }

/**
 * Routing intelligence + usage & cost for one chat response, straight from ChatResponse fields.
 * `rateLimit` comes from the response's X-RateLimit-* headers (absent when limits are off).
 */
export function ResponseDetails({ response, rateLimit = null }: { response: ChatResponse; rateLimit?: RateLimitInfo | null }) {
  const r = response
  const fellBack = r.fallback.used || r.selected_model !== r.model_used
  const hit = isCacheHit(r.cache.status)
  return (
    <div className="grid grid-cols-1 gap-4 lg:grid-cols-2">
      <Panel title="Routing intelligence" icon={<Shuffle className="h-3.5 w-3.5" aria-hidden="true" />} testId="routing-panel">
        <div className={cn('flex flex-wrap items-center gap-2 rounded-md border p-3 text-sm',
          fellBack ? 'border-amber-200 bg-amber-50' : 'border-slate-200 bg-slate-50')} data-testid="model-path">
          <span className="text-xs text-slate-500">Initially selected</span>
          <span className="font-mono text-xs font-medium text-slate-900">{r.selected_model}</span>
          <ArrowRight className="h-3.5 w-3.5 text-slate-400" aria-hidden="true" />
          <span className="text-xs text-slate-500">Actually answered</span>
          <span className="font-mono text-xs font-medium text-slate-900">{r.model_used}</span>
          {fellBack ? <Badge tone="warning">Fallback used</Badge> : <Badge tone="success">No fallback</Badge>}
        </div>
        <Rows label="Classification and routing" rows={[
          ['Complexity', <span><Badge tone="info" className="font-mono text-[11px]">{r.classification.complexity}</Badge>
            <span className="ml-1.5 text-xs tabular-nums text-slate-500">score {r.classification.complexity_score.toFixed(2)}</span></span>],
          ['Classification confidence', <span className="tabular-nums">{formatPercent(r.classification.confidence)}</span>],
          ['Intent', <span>{intentLabel(r.classification.intent.primary)}
            <span className="ml-1.5 text-xs tabular-nums text-slate-500">{formatPercent(r.classification.intent.confidence)}</span></span>],
          ['Tier requested → routed', <span className="font-mono text-xs">{r.routing.requested_tier} → {r.routing.routed_tier}</span>],
          ['Strategy', <span className="font-mono text-xs">{r.routing.strategy}{r.routing.override ? ' (explicit override)' : ''}</span>],
          ['Provider', r.provider],
          ['Provider status (this call)', mono(r.provider_status)],
          ['Provider model', mono(r.provider_model)],
          ['Provider adapter', mono(r.provider_adapter)],
          ['Latency (end to end)', <span className="tabular-nums">{formatLatency(r.latency_ms)}</span>],
          ['Finish reason', <Value value={r.finish_reason}>{r.finish_reason && mono(r.finish_reason)}</Value>],
          ['Cache', <span>{cacheLabel(r.cache.status)}</span>],
        ]} />
        <p className="rounded-md bg-slate-50 p-2.5 text-xs text-slate-600"><strong className="font-medium text-slate-700">Routing reason: </strong>{r.routing.reason}</p>
        <div className="space-y-1 text-xs">
          <p className="text-slate-500">Candidate models</p>
          {r.routing.candidates.length === 0 ? <p className="text-slate-400">None reported</p> : (
            <ol className="flex flex-wrap gap-1" aria-label="Candidate models">
              {r.routing.candidates.map((c) => <li key={c}><Badge tone={c === r.selected_model ? 'info' : 'neutral'} className="font-mono text-[11px]">{c}</Badge></li>)}
            </ol>
          )}
          {r.routing.preferred_capabilities.length > 0 && (
            <p className="text-slate-500">Preferred capabilities: <span className="font-mono text-slate-700">{r.routing.preferred_capabilities.join(', ')}</span></p>
          )}
        </div>
        <div className="space-y-1 text-xs" data-testid="fallback-info">
          <p className="text-slate-500">Fallback chain {r.fallback.used ? '(used)' : '(not used)'}</p>
          {r.fallback.chain.length === 0 ? <p className="text-slate-400">No fallback chain</p> : (
            <ol className="flex flex-wrap items-center gap-1" aria-label="Fallback chain">
              {r.fallback.chain.map((m, i) => (
                <li key={`${m}-${i}`} className="flex items-center gap-1">
                  {i > 0 && <ArrowRight className="h-3 w-3 text-slate-300" aria-hidden="true" />}
                  <Badge tone={m === r.fallback.final_model ? 'success' : 'neutral'} className="font-mono text-[11px]">{m}</Badge>
                </li>
              ))}
            </ol>
          )}
          {r.fallback.reason && <p className="text-amber-800">Fallback reason: {r.fallback.reason}</p>}
        </div>
        <div className="space-y-1">
          <p className="text-xs text-slate-500">Provider attempts ({r.fallback.attempts.length})</p>
          {r.fallback.attempts.length === 0 ? <p className="text-xs text-slate-400">{hit ? 'None: served from the cache.' : 'None reported'}</p> : (
            <div className="overflow-x-auto rounded-md border border-slate-200">
              <table className="w-full min-w-[420px] text-left text-xs" aria-label="Provider attempts">
                <thead className="bg-slate-50 text-slate-500">
                  <tr><th className="px-2 py-1.5 font-medium">#</th><th className="px-2 py-1.5 font-medium">Model</th>
                    <th className="px-2 py-1.5 font-medium">Provider</th><th className="px-2 py-1.5 font-medium">Outcome</th>
                    <th className="px-2 py-1.5 font-medium">Error</th><th className="px-2 py-1.5 text-right font-medium">Latency</th></tr>
                </thead>
                <tbody>
                  {r.fallback.attempts.map((a) => (
                    <tr key={a.attempt_number} className="border-t border-slate-100">
                      <td className="px-2 py-1.5 tabular-nums">{a.attempt_number}</td>
                      <td className="px-2 py-1.5 font-mono">{a.model_id}</td>
                      <td className="px-2 py-1.5">{a.provider}</td>
                      <td className="px-2 py-1.5"><Badge tone={ATTEMPT_TONE[a.outcome] ?? 'warning'}>{a.outcome}</Badge></td>
                      <td className="px-2 py-1.5 font-mono">{a.error_kind ?? '—'}{a.retryable !== null && a.error_kind ? (a.retryable ? ' (retryable)' : ' (not retryable)') : ''}</td>
                      <td className="px-2 py-1.5 text-right tabular-nums">{formatLatency(a.latency_ms)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </Panel>

      <Panel title="Usage and cost" icon={<Info className="h-3.5 w-3.5" aria-hidden="true" />} testId="usage-panel">
        <div className="flex flex-wrap gap-1.5">
          {r.mock_mode
            ? <Badge tone="info">Mock mode: no paid provider call</Badge>
            : <Badge tone="warning">Real provider: may incur cost</Badge>}
          {r.cost.pricing_is_placeholder && <Badge tone="warning">Placeholder pricing</Badge>}
          {r.cost.is_estimate && <Badge tone="neutral">Estimate</Badge>}
        </div>
        <Rows label="Token usage" rows={[
          ['Input tokens', <Value value={r.usage.input_tokens}><span className="tabular-nums">{formatInteger(r.usage.input_tokens)}</span></Value>],
          ['Output tokens', <Value value={r.usage.output_tokens}><span className="tabular-nums">{formatInteger(r.usage.output_tokens)}</span></Value>],
          ['Total tokens', <Value value={r.usage.total_tokens}><span className="tabular-nums">{formatInteger(r.usage.total_tokens)}</span></Value>],
          ['Token source', usageSourceLabel(r.usage.source)],
        ]} />
        <Rows label="Estimated cost" rows={[
          ['Input cost', <Value value={r.cost.input_cost}><span className="tabular-nums">{formatUsd(r.cost.input_cost)}</span></Value>],
          ['Output cost', <Value value={r.cost.output_cost}><span className="tabular-nums">{formatUsd(r.cost.output_cost)}</span></Value>],
          ['Estimated total', <Value value={r.cost.total_cost}><span className="font-medium tabular-nums">{formatUsd(r.cost.total_cost)}</span>
            {r.cost.currency !== 'USD' && <span className="ml-1 text-xs">{r.cost.currency}</span>}</Value>],
          ['Rates (per 1M in / out)', <span className="tabular-nums">{formatPricePerMillion(r.cost.input_cost_per_million)} / {formatPricePerMillion(r.cost.output_cost_per_million)}</span>],
        ]} />
        {r.cost.pricing_is_placeholder && (
          <p className="flex gap-2 rounded-md bg-amber-50 p-2.5 text-xs text-amber-900" role="note">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600" aria-hidden="true" />
            Placeholder pricing: the cost is computed from unverified rates and is a rough estimate only.
          </p>
        )}
        <Rows label="Cache" rows={[
          ['Status', <span>{cacheLabel(r.cache.status)}</span>],
          ['Stored for reuse', r.cache.stored ? 'Yes' : 'No'],
          ...(r.cache.similarity !== null && r.cache.similarity !== undefined
            ? [['Similarity / threshold', <span className="tabular-nums">{r.cache.similarity.toFixed(3)}{r.cache.threshold != null && ` / ${r.cache.threshold.toFixed(2)}`}</span>] as [string, ReactNode]]
            : []),
          ...(hit ? [
            ['Saved tokens', <Value value={r.cache.saved_tokens}><span className="tabular-nums">{formatInteger(r.cache.saved_tokens)}</span></Value>],
            ['Saved cost', <Value value={r.cache.saved_cost}><span className="tabular-nums">{formatUsd(r.cache.saved_cost)}</span></Value>],
            ['Cached answer age', <Value value={r.cache.age_seconds}><span className="tabular-nums">{r.cache.age_seconds != null && `${Math.round(r.cache.age_seconds)} s`}</span></Value>],
            ['Source request', <Value value={r.cache.source_request_id}>{r.cache.source_request_id && mono(r.cache.source_request_id)}</Value>],
          ] as [string, ReactNode][] : []),
          ['Lookup time', <Value value={r.cache.lookup_ms}><span className="tabular-nums">{formatLatency(r.cache.lookup_ms)}</span></Value>],
        ]} />
        {hit && <p className="text-xs text-emerald-700">Served from the response cache: no provider was called for this answer.</p>}
        {rateLimit && rateLimit.limit !== null && (
          <Rows label="Rate limit" rows={[
            ['Limit', <span className="tabular-nums">{formatInteger(rateLimit.limit)}{rateLimit.window && ` per ${rateLimit.window}`}</span>],
            ['Remaining', <Value value={rateLimit.remaining}><span className="tabular-nums" data-testid="rate-limit-remaining">{formatInteger(rateLimit.remaining)}</span></Value>],
            ['Window resets in', <Value value={rateLimit.resetSeconds}><span className="tabular-nums">{rateLimit.resetSeconds != null && `${formatInteger(rateLimit.resetSeconds)} s`}</span></Value>],
          ]} />
        )}
      </Panel>
    </div>
  )
}
