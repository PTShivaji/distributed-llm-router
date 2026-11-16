import type { ReactNode } from 'react'
import {
  AlertTriangle, Ban, CalendarClock, ChevronLeft, ChevronRight, Coins, FlaskConical, Gauge, Hash, Info, Percent, ShieldCheck,
  Wallet,
} from 'lucide-react'

import type {
  BudgetRefusalsResponse, BudgetSpendResponse, BudgetStatusResponse, ProcessCounterGroup, SpendDay, SpendGroup,
} from '../../api/types'
import { costBasis, countedTodayText, limitText, NOT_CONFIGURED_TEXT, processValueText, remainingTodayText, UNAVAILABLE_TEXT } from '../../lib/budget'
import {
  activeDays, counterLabelText, dailyCostRows, enforcementState, formatUtc, OUTCOME_TEXT, pageCount, pageWindow, reasonText,
  STORE_SCOPE_TEXT, STORE_TEXT, uniqueNotes, utilization,
} from '../../lib/budgetDashboard'
import { formatInteger, formatPercent, formatUsd } from '../../lib/format'
import { DailySpendChart } from '../charts/DailySpendChart'
import { EmptyState, SkeletonRows } from '../feedback/States'
import { Badge } from '../ui/Badge'
import { Button } from '../ui/Button'
import { Card, CardBody, CardHeader } from '../ui/Card'
import { KpiCard } from '../ui/KpiCard'

const yesNo = (v: boolean) => (v ? 'Yes' : 'No')
const enabled = (v: boolean) => (v ? 'Enabled' : 'Disabled')
const TH = 'py-2 pr-3 font-medium'
const TH_NUM = 'py-2 pr-3 text-right font-medium'
const TD_NUM = 'py-2 pr-3 text-right tabular-nums'

/** A labelled region (one per dashboard section) holding a card. */
function Section({ title, description, actions, children, testId }: {
  title: string; description?: ReactNode; actions?: ReactNode; children: ReactNode; testId?: string
}) {
  return (
    <section aria-label={title} data-testid={testId}>
      <Card>
        <CardHeader title={title} description={description} actions={actions} />
        <CardBody className="space-y-4">{children}</CardBody>
      </Card>
    </section>
  )
}

function Facts({ rows, label }: { rows: [string, ReactNode][]; label: string }) {
  return (
    <dl className="grid grid-cols-1 gap-x-6 text-sm sm:grid-cols-2" aria-label={label}>
      {rows.map(([k, v]) => (
        <div key={k} className="flex justify-between gap-4 border-b border-slate-100 py-2" data-testid={`fact-${k}`}>
          <dt className="text-slate-500">{k}</dt>
          <dd className="text-right text-slate-900">{v}</dd>
        </div>
      ))}
    </dl>
  )
}

function Callout({ tone = 'info', children, testId }: { tone?: 'info' | 'warning'; children: ReactNode; testId?: string }) {
  const warn = tone === 'warning'
  return (
    <p className={warn ? 'flex gap-2 rounded-md bg-amber-50 p-2.5 text-xs text-amber-900' : 'flex gap-2 rounded-md bg-slate-50 p-2.5 text-xs text-slate-600'}
      data-testid={testId}>
      {warn
        ? <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600" aria-hidden="true" />
        : <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-400" aria-hidden="true" />}
      <span>{children}</span>
    </p>
  )
}

// --------------------------------------------------------------------------- A. overview

export function BudgetOverviewSection({ status, loading, failure }: {
  status: BudgetStatusResponse | null; loading: boolean; failure: ReactNode
}) {
  const s = status
  const util = s ? utilization(s) : null
  const enforcement = s ? enforcementState(s) : null
  const utilHint = !s ? undefined
    : s.limits.daily_usd === null ? 'No global daily limit is configured'
      : s.today.counted_usd === null ? "Today's spend is unknown"
        : s.limits.daily_usd === 0 ? 'The daily limit is $0' : 'Counted today ÷ daily limit'
  const kpiLoading = loading && !s

  return (
    <Section title="Budget overview" testId="budget-overview"
      description={s ? `Budget day ${s.period.day} (UTC) · resets ${formatUtc(s.period.resets_at)} · GET /api/v1/budget/status` : 'GET /api/v1/budget/status'}>
      {failure}
      {(s || kpiLoading) && (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
          <KpiCard label="Daily limit" icon={<Wallet className="h-4 w-4" />} tone="brand" loading={kpiLoading}
            value={s ? limitText(s.limits.daily_usd) : null} hint={s ? 'All clients combined, per UTC day' : undefined} />
          <KpiCard label="Counted today" icon={<Coins className="h-4 w-4" />} tone="warning" loading={kpiLoading}
            value={s ? countedTodayText(s) : null}
            hint={s ? (s.today.available ? 'From the budget counters' : s.today.unavailable_reason) : undefined} />
          <KpiCard label="Remaining today" icon={<Gauge className="h-4 w-4" />} tone="success" loading={kpiLoading}
            value={s ? remainingTodayText(s) : null}
            hint={s ? (s.today.remaining_usd === null ? 'Not reported by the backend' : 'Daily limit minus counted spend') : undefined} />
          <KpiCard label="Utilization" icon={<Percent className="h-4 w-4" />} loading={kpiLoading}
            value={s ? (util === null ? UNAVAILABLE_TEXT : formatPercent(util)) : null} hint={utilHint} />
          <KpiCard label="Daily enforcement" icon={<ShieldCheck className="h-4 w-4" />} loading={kpiLoading}
            value={enforcement?.label ?? null} hint={enforcement?.detail} />
          <KpiCard label="Monthly limit" icon={<CalendarClock className="h-4 w-4" />} loading={kpiLoading}
            value={s ? 'Not supported' : null} hint={s?.monthly.note} />
        </div>
      )}
      {s && util !== null && (
        <div className="space-y-1">
          <div className="h-2 overflow-hidden rounded-full bg-slate-100" role="meter" aria-label="Daily budget utilization"
            aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.round(util * 1000) / 10} aria-valuetext={formatPercent(util)}>
            <div className={util >= 0.9 ? 'h-full bg-red-500' : util >= 0.75 ? 'h-full bg-amber-500' : 'h-full bg-brand-600'}
              style={{ width: `${Math.min(100, util * 100)}%` }} />
          </div>
          <p className="text-[11px] text-slate-500">{countedTodayText(s)} of {limitText(s.limits.daily_usd)} counted today (UTC)</p>
        </div>
      )}
      {s && (
        <>
          <Facts label="Budget configuration" rows={[
            ['Budget period', `${s.period.day} · ${formatUtc(s.period.starts_at)} → ${formatUtc(s.period.resets_at)}`],
            ['Daily limit per client', limitText(s.limits.daily_usd_per_client)],
            ['Daily tokens per client', s.limits.daily_tokens_per_client === null ? NOT_CONFIGURED_TEXT : `${formatInteger(s.limits.daily_tokens_per_client)} tokens`],
            ['Max cost per request', limitText(s.limits.max_cost_per_request_usd)],
            ['Counter store', s.enforcement.store === null ? 'None'
              : `${STORE_TEXT[s.enforcement.store]}${s.enforcement.store_scope ? ` · ${STORE_SCOPE_TEXT[s.enforcement.store_scope]}` : ''}`],
            ['If the store is unreachable', s.enforcement.fail_closed ? 'Paid calls are refused (fail closed)' : 'Paid calls are allowed (fail open)'],
            ['Provider mode', s.enforcement.mock_mode ? 'Mock' : 'Real'],
            ['Real provider calls', enabled(s.enforcement.real_provider_calls_enabled)],
            ['Paid embeddings', enabled(s.enforcement.paid_embeddings_enabled)],
            ['Monthly enforcement', 'Not supported'],
          ]} />
          <Callout testId="applies-to">
            <strong className="font-medium text-slate-700">Applies to:</strong> {s.enforcement.applies_to}.
            {' '}Per-client counters are enforced by the backend but not exposed by this API.
          </Callout>
        </>
      )}
      {kpiLoading && !s && <SkeletonRows rows={4} />}
    </Section>
  )
}

// --------------------------------------------------------------------------- B. daily spending

const BASIS_TONE = { none: 'neutral', simulated: 'demo', estimated: 'info', mixed: 'warning' } as const

function DaysTable({ days }: { days: SpendDay[] }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[720px] text-left text-xs" aria-label="Daily spending (days with requests)">
        <thead className="text-slate-500">
          <tr className="border-b border-slate-100">
            <th scope="col" className={TH}>Day (UTC)</th>
            <th scope="col" className={TH_NUM}>Requests</th>
            <th scope="col" className={TH_NUM}>Successful</th>
            <th scope="col" className={TH_NUM}>Mock / real</th>
            <th scope="col" className={TH_NUM}>Tokens</th>
            <th scope="col" className={TH_NUM}>Est. cost (real)</th>
            <th scope="col" className={TH_NUM}>Simulated (mock)</th>
            <th scope="col" className={TH_NUM}>Placeholder-priced</th>
            <th scope="col" className="py-2 text-right font-medium">Cost unknown</th>
          </tr>
        </thead>
        <tbody className="text-slate-700">
          {days.map((d) => (
            <tr key={d.date} className="border-b border-slate-50 last:border-0">
              <th scope="row" className="py-2 pr-3 font-mono font-normal">{d.date}</th>
              <td className={TD_NUM}>{formatInteger(d.requests)}</td>
              <td className={TD_NUM}>{formatInteger(d.successful_requests)}</td>
              <td className={TD_NUM}>{formatInteger(d.mock_requests)} / {formatInteger(d.real_requests)}</td>
              <td className={TD_NUM}>{formatInteger(d.total_tokens)}</td>
              <td className={TD_NUM}>{formatUsd(d.real_estimated_cost_usd)}</td>
              <td className={TD_NUM}>{formatUsd(d.mock_estimated_cost_usd)}</td>
              <td className={TD_NUM}>{formatInteger(d.placeholder_priced_requests)}</td>
              <td className="py-2 text-right tabular-nums">{formatInteger(d.cost_unknown_requests)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

export function DailySpendSection({ spend, loading, failure }: { spend: BudgetSpendResponse | null; loading: boolean; failure: ReactNode }) {
  const t = spend?.totals
  const basis = t ? costBasis(t) : null
  const kpiLoading = loading && !spend
  return (
    <Section title="Daily spending" testId="budget-spend"
      description={spend ? `${spend.start_date} → ${spend.end_date} (UTC days, inclusive) · persisted request history · GET /api/v1/budget/spend` : 'GET /api/v1/budget/spend'}
      actions={basis ? <Badge tone={BASIS_TONE[basis.kind]}>{basis.label}</Badge> : undefined}>
      {failure}
      {(spend || kpiLoading) && (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3">
          <KpiCard label="Requests" icon={<Hash className="h-4 w-4" />} tone="brand" loading={kpiLoading}
            value={formatInteger(t?.requests)} hint={t ? `${formatInteger(t.successful_requests)} successful` : undefined} />
          <KpiCard label="Total tokens" icon={<Hash className="h-4 w-4" />} loading={kpiLoading} value={formatInteger(t?.total_tokens)} />
          <KpiCard label="Estimated cost (real calls)" icon={<Coins className="h-4 w-4" />} tone="warning" loading={kpiLoading}
            value={formatUsd(t?.real_estimated_cost_usd)}
            hint={t ? `${formatInteger(t.real_requests)} real requests · estimate, not a provider invoice` : undefined} />
          <KpiCard label="Simulated cost (mock calls)" icon={<FlaskConical className="h-4 w-4" />} loading={kpiLoading}
            value={formatUsd(t?.mock_estimated_cost_usd)}
            hint={t ? `${formatInteger(t.mock_requests)} mock requests · no money spent` : undefined} />
          <KpiCard label="Placeholder-priced requests" icon={<AlertTriangle className="h-4 w-4" />} loading={kpiLoading}
            value={formatInteger(t?.placeholder_priced_requests)} hint={t ? 'Costed with placeholder prices' : undefined} />
          <KpiCard label="Cost unknown" icon={<Info className="h-4 w-4" />} loading={kpiLoading}
            value={formatInteger(t?.cost_unknown_requests)} hint={t ? 'Requests whose cost could not be estimated' : undefined} />
        </div>
      )}
      {spend && t && (
        t.requests === 0 ? (
          <EmptyState title="No persisted requests in this range"
            description="Nothing was recorded for these UTC days, so there is no spend to show. Choose a wider range, or send a request from the Chat Playground." />
        ) : (
          <>
            {t.placeholder_priced_requests > 0 && (
              <Callout tone="warning" testId="placeholder-pricing">
                {formatInteger(t.placeholder_priced_requests)} of {formatInteger(t.requests)} requests were costed with placeholder
                prices: these figures are illustrative, not verified provider prices.
              </Callout>
            )}
            <DailySpendChart rows={dailyCostRows(spend.days)} />
            <DaysTable days={activeDays(spend.days)} />
            <p className="text-[11px] text-slate-500">
              The table lists days with persisted requests ({formatInteger(activeDays(spend.days).length)} of {formatInteger(spend.days.length)});
              the chart shows every day of the range. Cost basis reported by the backend: {spend.cost_basis}.
            </p>
          </>
        )
      )}
      {kpiLoading && <SkeletonRows rows={5} />}
    </Section>
  )
}

// --------------------------------------------------------------------------- C. breakdown

function GroupTable({ label, groups, truncated }: { label: string; groups: SpendGroup[]; truncated: boolean }) {
  if (groups.length === 0) return <p className="text-xs text-slate-500">No {label.toLowerCase()} usage in this range.</p>
  return (
    <div className="space-y-2">
      <div className="overflow-x-auto">
        <table className="w-full min-w-[520px] text-left text-xs" aria-label={`Spending by ${label.toLowerCase()}`}>
          <thead className="text-slate-500">
            <tr className="border-b border-slate-100">
              <th scope="col" className={TH}>{label}</th>
              <th scope="col" className={TH_NUM}>Requests</th>
              <th scope="col" className={TH_NUM}>Tokens</th>
              <th scope="col" className={TH_NUM}>Est. cost (real)</th>
              <th scope="col" className={TH_NUM}>Simulated (mock)</th>
              <th scope="col" className="py-2 text-right font-medium">Placeholder-priced</th>
            </tr>
          </thead>
          <tbody className="text-slate-700">
            {groups.map((g) => (
              <tr key={g.name} className="border-b border-slate-50 last:border-0">
                <th scope="row" className="py-2 pr-3 font-mono font-normal">{g.name}</th>
                <td className={TD_NUM}>{formatInteger(g.requests)}</td>
                <td className={TD_NUM}>{formatInteger(g.total_tokens)}</td>
                <td className={TD_NUM}>{formatUsd(g.real_estimated_cost_usd)}</td>
                <td className={TD_NUM}>{formatUsd(g.mock_estimated_cost_usd)}</td>
                <td className="py-2 text-right tabular-nums">{formatInteger(g.placeholder_priced_requests)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {truncated && <p className="text-[11px] text-amber-800" data-testid={`truncated-${label.toLowerCase()}`}>
        The backend returned only the largest {label.toLowerCase()} groups; the rest are not listed (totals above include them).</p>}
    </div>
  )
}

export function SpendBreakdownSection({ spend }: { spend: BudgetSpendResponse }) {
  return (
    <Section title="Spending breakdown" testId="budget-breakdown"
      description="Model that answered each persisted request, and its provider · same range as Daily spending">
      {spend.totals.requests === 0 ? (
        <p className="text-xs text-slate-500">No persisted requests in this range.</p>
      ) : (
        <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
          <div className="min-w-0 space-y-2">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">By provider</h3>
            <GroupTable label="Provider" groups={spend.by_provider} truncated={spend.by_provider_truncated} />
          </div>
          <div className="min-w-0 space-y-2">
            <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">By model</h3>
            <GroupTable label="Model" groups={spend.by_model} truncated={spend.by_model_truncated} />
          </div>
        </div>
      )}
    </Section>
  )
}

// --------------------------------------------------------------------------- D. refusals

function Counts({ label, items, text = (k: string) => k }: { label: string; items: { key: string; count: number }[]; text?: (k: string) => string }) {
  return (
    <div className="min-w-0 space-y-1.5">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">{label}</h3>
      {items.length === 0 ? <p className="text-xs text-slate-500">None</p> : (
        <dl className="divide-y divide-slate-100 rounded-md border border-slate-200 text-sm" aria-label={label}>
          {items.map((i) => (
            <div key={i.key} className="flex justify-between gap-4 px-3 py-1.5">
              <dt className="text-slate-600">{text(i.key)}</dt>
              <dd className="tabular-nums text-slate-900">{formatInteger(i.count)}</dd>
            </div>
          ))}
        </dl>
      )}
    </div>
  )
}

export function RefusalsSection({ refusals, loading, failure, onPage }: {
  refusals: BudgetRefusalsResponse | null; loading: boolean; failure: ReactNode; onPage: (page: number) => void
}) {
  const r = refusals
  const kpiLoading = loading && !r
  const pages = r ? pageCount(r.pagination) : 1
  const shownRange = r ? pageWindow(r.pagination, r.events.length) : null
  return (
    <Section title="Budget refusals" testId="budget-refusals"
      description={r ? `${r.start_date} → ${r.end_date} (UTC days) · ${r.persisted_scope} · GET /api/v1/budget/refusals` : 'GET /api/v1/budget/refusals'}>
      {failure}
      {(r || kpiLoading) && (
        <div className="grid grid-cols-2 gap-3">
          <KpiCard label="Refused attempts" icon={<Ban className="h-4 w-4" />} tone="danger" loading={kpiLoading}
            value={formatInteger(r?.total_refused_attempts)} hint={r ? 'Provider attempts skipped by the budget guard' : undefined} />
          <KpiCard label="Requests with a refusal" icon={<Hash className="h-4 w-4" />} loading={kpiLoading}
            value={formatInteger(r?.refused_requests)} hint={r ? 'Distinct requests (some were served by another model)' : undefined} />
        </div>
      )}
      {r && (r.total_refused_attempts === 0 ? (
        <EmptyState title="No budget refusals in this range"
          description="No provider attempt was refused by the budget guard on these UTC days (only persisted chat attempts are listed)." />
      ) : (
        <>
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <Counts label="By reason" items={r.by_reason} text={reasonText} />
            <Counts label="By provider" items={r.by_provider} />
          </div>
          {r.events.length === 0 ? (
            <EmptyState title="No events on this page" description="The list may have changed since the first page was loaded."
              action={<Button variant="secondary" size="sm" onClick={() => onPage(1)}>Back to first page</Button>} />
          ) : (
            <div className="overflow-x-auto" aria-busy={loading || undefined}>
              <table className="w-full min-w-[760px] text-left text-xs" aria-label="Refusal history">
                <thead className="text-slate-500">
                  <tr className="border-b border-slate-100">
                    <th scope="col" className={TH}>Time (UTC)</th>
                    <th scope="col" className={TH}>Reason</th>
                    <th scope="col" className={TH}>Provider</th>
                    <th scope="col" className={TH}>Model</th>
                    <th scope="col" className={TH_NUM}>Attempt</th>
                    <th scope="col" className={TH}>Outcome</th>
                    <th scope="col" className="py-2 font-medium">Mode</th>
                  </tr>
                </thead>
                <tbody className="text-slate-700">
                  {r.events.map((e, i) => (
                    <tr key={`${e.time}-${i}`} className="border-b border-slate-50 align-top last:border-0">
                      <td className="whitespace-nowrap py-2 pr-3 font-mono">{formatUtc(e.time)}</td>
                      <td className="py-2 pr-3">
                        <span className="font-medium text-slate-800">{reasonText(e.reason)}</span>
                        <span className="block text-[11px] text-slate-500">{e.description}</span>
                      </td>
                      <td className="py-2 pr-3">{e.provider ?? <span className="text-slate-400">Not recorded</span>}</td>
                      <td className="py-2 pr-3 font-mono">{e.model ?? <span className="font-sans text-slate-400">Not recorded</span>}</td>
                      <td className={TD_NUM}>{formatInteger(e.attempt_number)}</td>
                      <td className="py-2 pr-3">{OUTCOME_TEXT[e.request_outcome]}</td>
                      <td className="py-2">{e.mock_mode ? <Badge tone="demo">Mock</Badge> : <Badge>Real</Badge>}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <nav aria-label="Refusal pages" className="flex flex-wrap items-center justify-between gap-2">
            <Button variant="secondary" size="sm" disabled={loading || r.pagination.page <= 1} onClick={() => onPage(r.pagination.page - 1)}
              icon={<ChevronLeft className="h-3.5 w-3.5" aria-hidden="true" />}>Previous</Button>
            <span className="text-xs text-slate-500" data-testid="page-status" aria-live="polite">
              Page {formatInteger(r.pagination.page)} of {formatInteger(pages)}{shownRange ? ` · ${shownRange}` : ''}
            </span>
            <Button variant="secondary" size="sm" disabled={loading || !r.pagination.has_more} onClick={() => onPage(r.pagination.page + 1)}
              icon={<ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />}>Next</Button>
          </nav>
        </>
      ))}
      {kpiLoading && <SkeletonRows rows={4} />}
    </Section>
  )
}

// --------------------------------------------------------------------------- E. embedding / process counters

function CounterTable({ label, groups, valueLabel = 'Count', fmt = formatInteger }: {
  label: string; groups: ProcessCounterGroup[] | null; valueLabel?: string; fmt?: (n: number) => string
}) {
  return (
    <div className="min-w-0 space-y-1.5">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">{label}</h3>
      {groups === null ? <p className="text-xs text-slate-500">{UNAVAILABLE_TEXT} (metrics are disabled on the backend)</p>
        : groups.length === 0 ? <p className="text-xs text-slate-500">None counted by this process</p> : (
          <table className="w-full text-left text-xs" aria-label={label}>
            <thead className="text-slate-500">
              <tr className="border-b border-slate-100"><th scope="col" className={TH}>Labels</th><th scope="col" className="py-2 text-right font-medium">{valueLabel}</th></tr>
            </thead>
            <tbody className="text-slate-700">
              {groups.map((g) => (
                <tr key={counterLabelText(g)} className="border-b border-slate-50 last:border-0">
                  <td className="break-all py-1.5 pr-3 font-mono">{counterLabelText(g)}</td>
                  <td className="py-1.5 text-right tabular-nums">{fmt(g.value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
    </div>
  )
}

export function EmbeddingSection({ status, refusals }: { status: BudgetStatusResponse | null; refusals: BudgetRefusalsResponse | null }) {
  const e = status?.embedding
  return (
    <Section title="Embedding costs and process counters" testId="budget-embedding"
      description="Counted by the API process that answered, since it started: reset on restart, not shared between instances, not history"
      actions={<Badge tone="warning">Process-local</Badge>}>
      {!status && !refusals && <p className="text-xs text-slate-500">{UNAVAILABLE_TEXT}: budget status and refusals are not loaded.</p>}
      {e && (
        <>
          <Facts label="Embeddings" rows={[
            ['Embedding provider', <span key="p" className="font-mono">{e.provider}</span>],
            ['Paid', yesNo(e.paid)],
            ['Counted against the budget', yesNo(e.budgeted)],
            ['Durable history', 'Not persisted'],
            ['Estimated embedding cost (this process)', processValueText(e.process_estimated_cost_usd)],
          ]} />
          {e.process_counters_available
            ? <CounterTable label="Embedding events (this process)" groups={e.process_events} />
            : <Callout>Process counters are {UNAVAILABLE_TEXT.toLowerCase()}: metrics are disabled on the backend.</Callout>}
        </>
      )}
      {refusals && (
        refusals.process_counters_available ? (
          <div className="grid grid-cols-1 gap-4 md:grid-cols-2">
            <CounterTable label="Chat budget refusals (this process)" groups={refusals.process_chat_refusals} />
            <CounterTable label="Embedding budget refusals (this process)" groups={refusals.process_embedding_refusals} />
          </div>
        ) : <Callout>Process refusal counters are {UNAVAILABLE_TEXT.toLowerCase()}: metrics are disabled on the backend.</Callout>
      )}
      <Callout>
        These are Prometheus counters of one API process, not spending records: they are not included in Daily spending and must not be
        read as daily or monthly history.
      </Callout>
    </Section>
  )
}

// --------------------------------------------------------------------------- F. limitations

export function LimitationsSection({ status, spend, refusals }: {
  status: BudgetStatusResponse | null; spend: BudgetSpendResponse | null; refusals: BudgetRefusalsResponse | null
}) {
  const enforcement = status ? enforcementState(status) : null
  const notes = uniqueNotes(status?.notes, spend?.notes, refusals?.notes)
  const items: [string, ReactNode][] = [
    ['Daily budget enforcement', enforcement ? `${enforcement.label}. ${enforcement.detail}` : `${UNAVAILABLE_TEXT} (budget status not loaded).`],
    ['Monthly budget', `Not supported.${status ? ` ${status.monthly.note}` : ''}`],
    ['Mock calls', status ? `The budget applies to ${status.enforcement.applies_to}.` : `${UNAVAILABLE_TEXT} (budget status not loaded).`],
    ['Pricing', spend
      ? `Costs are per-request estimates, never provider invoices; mock costs are simulated. ${formatInteger(spend.totals.placeholder_priced_requests)} of ${formatInteger(spend.totals.requests)} requests in the selected range used placeholder prices.`
      : 'Costs are per-request estimates, never provider invoices; prices may be placeholders.'],
    ['Embedding metrics', 'Process-local Prometheus counters: not persisted, not shared between instances, and not included in spend history.'],
    ['Spend history', 'Built from persisted request records only: periods when persistence was disabled or the database was unreachable are missing.'],
  ]
  return (
    <Section title="Budget limitations and information" testId="budget-limitations">
      <dl className="space-y-2 text-sm">
        {items.map(([k, v]) => (
          <div key={k} className="grid grid-cols-1 gap-1 sm:grid-cols-[12rem_1fr]">
            <dt className="font-medium text-slate-700">{k}</dt>
            <dd className="text-slate-600">{v}</dd>
          </div>
        ))}
      </dl>
      {notes.length > 0 && (
        <div className="space-y-1.5">
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">Notes from the backend</h3>
          <ul className="list-disc space-y-1 pl-5 text-xs text-slate-600" data-testid="backend-notes">
            {notes.map((n) => <li key={n}>{n}</li>)}
          </ul>
        </div>
      )}
    </Section>
  )
}
