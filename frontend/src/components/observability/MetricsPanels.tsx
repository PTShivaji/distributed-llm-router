import type { ReactNode } from 'react'
import { Activity, AlertOctagon, ArrowRight, Coins, Gauge, MessageSquare, Server, Type } from 'lucide-react'

import { formatInteger, formatPercent, formatUsd } from '../../lib/format'
import {
  formatBucketRange, formatSeconds, histogramMean, NO_SAMPLES_TEXT, NOT_EXPOSED_TEXT, quantileBucket, readingValue, showReading,
  SKIPPED_OUTCOMES, type Group, type HistogramSummary, type MetricsView, type Reading,
} from '../../lib/observability'
import { Badge } from '../ui/Badge'
import { Card, CardBody, CardHeader } from '../ui/Card'
import { KpiCard } from '../ui/KpiCard'

const int = (n: number) => formatInteger(n)
const sum = (...rs: Reading[]): Reading => {
  if (rs.every((r) => r.kind === 'not_exposed')) return { kind: 'not_exposed' }
  const values = rs.map(readingValue).filter((v): v is number => v !== null)
  return values.length ? { kind: 'value', value: values.reduce((a, b) => a + b, 0) } : { kind: 'no_samples' }
}

/** Shown in place of a list/table when its metric has nothing to show (never a fabricated 0 row). */
function Absent({ state, none }: { state: Reading; none: string }) {
  const text = state.kind === 'not_exposed' ? `${NOT_EXPOSED_TEXT} by this backend.` : state.kind === 'no_samples' ? `${NO_SAMPLES_TEXT}: ${none}` : none
  return <p className="text-xs text-slate-400" data-absent={state.kind}>{text}</p>
}

function GroupList({ title, groups, state, none, label, format = int }: {
  title: string; groups: Group[]; state: Reading; none: string; label: (l: Record<string, string>) => ReactNode; format?: (n: number) => string
}) {
  return (
    <div className="min-w-0 space-y-1.5">
      <p className="text-xs font-semibold text-slate-700">{title}</p>
      {groups.length === 0 ? <Absent state={state} none={none} /> : (
        <ul className="space-y-1 text-xs" aria-label={title}>
          {groups.map((g) => (
            <li key={JSON.stringify(g.labels)} className="flex justify-between gap-2">
              <span className="min-w-0 truncate font-mono">{label(g.labels)}</span>
              <span className="shrink-0 tabular-nums">{format(g.value)}</span>
            </li>
          ))}
        </ul>
      )}
    </div>
  )
}

function Table({ label, head, children }: { label: string; head: string[]; children: ReactNode }) {
  return (
    <div className="-mx-5 overflow-x-auto px-5">
      <table className="w-full min-w-[34rem] text-left text-xs" aria-label={label}>
        <thead className="text-[11px] uppercase tracking-wide text-slate-500">
          <tr>{head.map((h, i) => <th key={h} scope="col" className={`py-1.5 pr-3 font-medium ${i > 1 ? 'text-right' : ''}`}>{h}</th>)}</tr>
        </thead>
        <tbody className="divide-y divide-slate-100 text-slate-700">{children}</tbody>
      </table>
    </div>
  )
}

const td = 'py-1.5 pr-3'
const tdNum = `${td} text-right tabular-nums`

function LatencyCells({ h }: { h: HistogramSummary | null }) {
  if (!h || h.count === 0) return <><td className={tdNum}>—</td><td className={tdNum}>—</td><td className={tdNum}>—</td></>
  if (!h.consistent) return <td className={tdNum} colSpan={3} title="Bucket counts are not cumulative or do not end in +Inf == count">Buckets inconsistent; mean {formatSeconds(histogramMean(h))}</td>
  return (
    <>
      <td className={tdNum}>{formatSeconds(histogramMean(h))}</td>
      <td className={tdNum}>{formatBucketRange(quantileBucket(h, 0.5))}</td>
      <td className={tdNum}>{formatBucketRange(quantileBucket(h, 0.95))}</td>
    </>
  )
}

export function MetricsKpis({ view }: { view: MetricsView }) {
  const tokens = sum(view.tokens.input, view.tokens.output)
  const ratio = view.cache.hitRatio
  return (
    <div className="grid grid-cols-2 gap-3 md:grid-cols-4" data-testid="metrics-kpis">
      <KpiCard label="HTTP requests" icon={<Server className="h-4 w-4" />} value={showReading(view.http.total, int)} hint="All routes, incl. this page's requests" />
      <KpiCard label="In flight" icon={<Activity className="h-4 w-4" />} value={showReading(view.http.inProgress, int)} hint="Gauge at scrape time; includes the scrape" />
      <KpiCard label="HTTP errors" icon={<AlertOctagon className="h-4 w-4" />} tone="danger" value={showReading(view.errors.total, int)} hint="Failed requests (status ≥ 400)" />
      <KpiCard label="Chat requests" icon={<MessageSquare className="h-4 w-4" />} tone="brand" value={showReading(view.chat.total, int)} hint="Completed /chat requests" />
      <KpiCard label="Provider attempts" icon={<Server className="h-4 w-4" />} value={showReading(view.attempts.total, int)} hint="Includes failed and skipped" />
      <KpiCard label="Cache hit ratio" icon={<Gauge className="h-4 w-4" />} tone="success"
        value={ratio.kind === 'value' && Number.isNaN(ratio.value) ? 'Undefined' : showReading(ratio, (v) => formatPercent(v))}
        hint={ratio.kind === 'value' && Number.isNaN(ratio.value) ? 'No hits or misses yet' : 'Hits ÷ (hits + misses)'} />
      <KpiCard label="Tokens" icon={<Type className="h-4 w-4" />} value={showReading(tokens, int)} hint="Input + output, provider calls only" />
      <KpiCard label="Estimated cost" icon={<Coins className="h-4 w-4" />} tone="warning" value={showReading(view.cost.total, formatUsd)}
        hint={view.cost.placeholder ? 'Placeholder pricing: not real spend' : 'Estimate, not an invoice'} />
    </div>
  )
}

export function HttpPanel({ view }: { view: MetricsView }) {
  return (
    <Card data-testid="http-panel">
      <CardHeader title="HTTP traffic and errors"
        description="llm_router_http_requests_total, llm_router_http_request_duration_seconds (histogram, seconds), llm_router_errors_total" />
      <CardBody className="space-y-5">
        {view.http.byClass.length > 0 && (
          <div className="flex flex-wrap gap-2" aria-label="Requests by status class">
            {view.http.byClass.map((c) => (
              <Badge key={c.labels.class} tone={c.labels.class === '5xx' ? 'danger' : c.labels.class === '4xx' ? 'warning' : 'neutral'}>
                {c.labels.class}: <span className="tabular-nums" data-testid={`status-${c.labels.class}`}>{formatInteger(c.value)}</span>
              </Badge>
            ))}
          </div>
        )}
        {view.http.routes.length === 0 ? <Absent state={view.http.total} none="no HTTP requests." /> : (
          <Table label="Requests by route" head={['Route', 'Method', 'Requests', '4xx', '5xx', 'Mean', 'p50 bucket', 'p95 bucket']}>
            {view.http.routes.map((r) => (
              <tr key={`${r.method} ${r.route}`}>
                <td className={`${td} max-w-[16rem] truncate font-mono`} title={r.route}>{r.route}</td>
                <td className={`${td} font-mono`}>{r.method}</td>
                <td className={tdNum}>{formatInteger(r.requests)}</td>
                <td className={tdNum}>{formatInteger(r.byClass['4xx'] ?? 0)}</td>
                <td className={tdNum}>{formatInteger(r.byClass['5xx'] ?? 0)}</td>
                <LatencyCells h={r.latency} />
              </tr>
            ))}
          </Table>
        )}
        <p className="text-[11px] text-slate-500">
          Latency is server-side time per request. Mean = sum ÷ count (exact). The p50/p95 columns name the histogram <em>bucket</em> that
          contains that percentile; the true value lies somewhere inside it, so they are ranges, not measurements.
        </p>
        <div>
          <p className="mb-1.5 text-xs font-semibold text-slate-700">Errors by category and code</p>
          {view.errors.rows.length === 0 ? <Absent state={view.errors.total} none="no failed requests." /> : (
            <ul className="grid grid-cols-1 gap-1 text-xs sm:grid-cols-2" aria-label="Errors by category and code">
              {view.errors.rows.map((g) => (
                <li key={`${g.labels.category}/${g.labels.code}`} className="flex justify-between gap-2 rounded bg-slate-50 px-2 py-1">
                  <span className="min-w-0 truncate"><span className="font-medium">{g.labels.category}</span> · <span className="font-mono">{g.labels.code}</span></span>
                  <span className="tabular-nums">{formatInteger(g.value)}</span>
                </li>
              ))}
            </ul>
          )}
        </div>
      </CardBody>
    </Card>
  )
}

const SKIP_LABEL: Record<(typeof SKIPPED_OUTCOMES)[number], string> = {
  skipped_unhealthy: 'Skipped: unhealthy', skipped_rate_limited: 'Skipped: rate limit', skipped_budget: 'Skipped: budget',
}

export function RoutingPanel({ view }: { view: MetricsView }) {
  const a = view.attempts
  return (
    <Card data-testid="routing-panel">
      <CardHeader title="Chat routing and provider attempts"
        description="llm_router_chat_requests_total, llm_router_provider_attempts_total, llm_router_provider_latency_seconds (seconds), retries, fallbacks" />
      <CardBody className="space-y-5">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <GroupList title="Chat requests by outcome" groups={view.chat.byOutcome} state={view.chat.total} none="no chat requests." label={(l) => l.outcome} />
          <GroupList title="By cache status" groups={view.chat.byCache} state={view.chat.total} none="no chat requests." label={(l) => l.cache_status} />
          <GroupList title="By complexity" groups={view.chat.byComplexity} state={view.chat.total} none="no chat requests." label={(l) => l.complexity} />
        </div>
        <div>
          <p className="mb-1.5 text-xs font-semibold text-slate-700">Provider attempts by model</p>
          {a.rows.length === 0 ? <Absent state={a.total} none="no provider attempts." /> : (
            <Table label="Provider attempts by model" head={['Model', 'Provider', 'Success', 'Error', ...SKIPPED_OUTCOMES.map((o) => SKIP_LABEL[o])]}>
              {a.rows.map((r) => (
                <tr key={`${r.model}|${r.provider}`}>
                  <td className={`${td} font-mono`}>{r.model}</td>
                  <td className={`${td} font-mono`}>{r.provider}</td>
                  <td className={tdNum}>{formatInteger(r.success)}</td>
                  <td className={tdNum}>{formatInteger(r.error)}</td>
                  {SKIPPED_OUTCOMES.map((o) => <td key={o} className={tdNum}>{formatInteger(r.skipped[o])}</td>)}
                </tr>
              ))}
            </Table>
          )}
          <p className="mt-1.5 text-[11px] text-slate-500">
            An attempt is counted for every candidate the router considered. Skipped candidates were never sent to a provider (unhealthy, rate
            limited or refused by a spend safeguard); errors were sent and failed. Only success and error attempts have latency.
          </p>
        </div>
        <div>
          <p className="mb-1.5 text-xs font-semibold text-slate-700">Provider latency</p>
          {a.latency.length === 0 ? <Absent state={a.total} none="no provider invocations." /> : (
            <Table label="Provider latency" head={['Model', 'Outcome', 'Calls', 'Mean', 'p50 bucket', 'p95 bucket']}>
              {a.latency.map((h) => (
                <tr key={JSON.stringify(h.labels)}>
                  <td className={`${td} font-mono`}>{h.labels.model}<span className="text-slate-400"> · {h.labels.provider}</span></td>
                  <td className={td}>{h.labels.outcome}</td>
                  <td className={tdNum}>{formatInteger(h.count)}</td>
                  <LatencyCells h={h} />
                </tr>
              ))}
            </Table>
          )}
        </div>
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <GroupList title="Failed attempts by error kind" groups={a.errorKinds} state={a.total} none="no failed attempts." label={(l) => l.error_kind} />
          <GroupList title="Retries by model" groups={view.retries.rows} state={view.retries.total} none="no retries." label={(l) => l.model} />
          <div className="min-w-0 space-y-1.5">
            <p className="text-xs font-semibold text-slate-700">Fallbacks (from → to)</p>
            {view.fallbacks.rows.length === 0 ? <Absent state={view.fallbacks.total} none="no fallbacks." /> : (
              <ul className="space-y-1 text-xs" aria-label="Fallbacks">
                {view.fallbacks.rows.map((f) => (
                  <li key={`${f.labels.from_model}>${f.labels.to_model}`} className="flex items-center justify-between gap-2">
                    <span className="flex min-w-0 items-center gap-1 font-mono"><span className="truncate">{f.labels.from_model}</span>
                      <ArrowRight className="h-3 w-3 shrink-0 text-slate-400" aria-label="to" /><span className="truncate">{f.labels.to_model}</span></span>
                    <span className="tabular-nums">{formatInteger(f.value)}</span>
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      </CardBody>
    </Card>
  )
}

export function UsagePanel({ view }: { view: MetricsView }) {
  return (
    <Card data-testid="usage-panel">
      <CardHeader title="Cache, tokens and estimated cost"
        description="llm_router_cache_lookups_total, llm_router_cache_hit_ratio (gauge, 0–1), llm_router_tokens_total, llm_router_estimated_cost_usd_total (USD)" />
      <CardBody className="space-y-4">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <GroupList title="Cache lookups by result" groups={view.cache.byStatus} state={view.cache.lookups} none="no cache lookups." label={(l) => l.status} />
          <GroupList title="Tokens by source" groups={view.tokens.bySource} state={sum(view.tokens.input, view.tokens.output)} none="no tokens consumed." label={(l) => l.source} />
          <GroupList title="Tokens by model and direction" groups={view.tokens.byModel} state={sum(view.tokens.input, view.tokens.output)} none="no tokens consumed."
            label={(l) => `${l.model} · ${l.direction}`} />
          <GroupList title="Estimated cost by model (USD)" groups={view.cost.rows} state={view.cost.total} none="no estimated cost." format={formatUsd}
            label={(l) => `${l.model}${l.pricing === 'placeholder' ? ' · placeholder' : ''}`} />
        </div>
        {view.cost.placeholder && (
          <p className="rounded-md bg-amber-50 p-2.5 text-xs text-amber-900" data-testid="placeholder-pricing">
            Some costs use <strong>placeholder</strong> registry pricing (mock mode). They illustrate the calculation only and are not real spend.
          </p>
        )}
        <p className="text-[11px] text-slate-500">
          The hit ratio counts exact and semantic hits against misses; disabled, bypass and error lookups are excluded. Cache hits use no
          provider tokens. Token sources: provider_reported, estimated or partially_estimated. Estimated cost = tokens × registry prices: it is
          not an invoice or a provider-billed amount.
        </p>
      </CardBody>
    </Card>
  )
}

export function SafeguardsPanel({ view }: { view: MetricsView }) {
  return (
    <Card data-testid="safeguards-panel">
      <CardHeader title="Rate limits and spend safeguards"
        description="llm_router_rate_limit_rejections_total, llm_router_budget_rejections_total, llm_router_embedding_budget_events_total, llm_router_embedding_estimated_cost_usd_total" />
      <CardBody className="space-y-4">
        <div className="grid grid-cols-1 gap-4 sm:grid-cols-3">
          <GroupList title="Rate-limit rejections" groups={view.rateLimit.rows} state={view.rateLimit.total} none="no rejections."
            label={(l) => `${l.scope} · per ${l.window}`} />
          <GroupList title="Budget rejections" groups={view.budget.rows} state={view.budget.total} none="no rejections." label={(l) => l.reason} />
          <GroupList title="Embedding budget events" groups={view.embedding.rows} state={view.embedding.events} none="no paid embedding calls."
            label={(l) => (l.reason && l.reason !== 'none' ? `${l.event} · ${l.reason}` : l.event)} />
        </div>
        <p className="flex flex-wrap items-baseline justify-between gap-2 rounded-md bg-slate-50 px-3 py-2 text-xs">
          <span className="text-slate-600">Estimated paid-embedding cost</span>
          <span className="font-semibold tabular-nums text-slate-900" data-testid="embedding-cost">{showReading(view.embedding.cost, formatUsd)}</span>
        </p>
        <p className="text-[11px] text-slate-500">
          Rate-limit rejections are counted per client or per model and window. Budget rejections are real (paid) provider attempts refused by
          spend safeguards before any call. Embedding events only occur with a paid embedding provider; the local hashing embedder records none.
        </p>
      </CardBody>
    </Card>
  )
}

export function FamiliesPanel({ view }: { view: MetricsView }) {
  return (
    <details className="rounded-lg border border-slate-200 bg-white shadow-sm" data-testid="families">
      <summary className="cursor-pointer px-5 py-3 text-sm font-semibold text-slate-900">
        Exposed metric families ({view.families.length})
        <span className="ml-2 text-xs font-normal text-slate-500">raw names, types and series counts</span>
      </summary>
      <div className="border-t border-slate-100 px-5 py-3">
        {view.malformedLines > 0 && <p className="mb-2 text-xs text-amber-800">{view.malformedLines} line(s) could not be parsed and were ignored.</p>}
        <Table label="Exposed metric families" head={['Name', 'Type', 'Series']}>
          {view.families.map((f) => (
            <tr key={f.name}>
              <td className={`${td} font-mono`} title={f.help}>{f.name}</td>
              <td className={td}>{f.type}</td>
              <td className={tdNum}>{f.series === 0 ? <span className="text-slate-400">none yet</span> : formatInteger(f.series)}</td>
            </tr>
          ))}
        </Table>
      </div>
    </details>
  )
}
