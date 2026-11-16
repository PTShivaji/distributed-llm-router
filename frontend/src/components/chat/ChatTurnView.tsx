import { useState } from 'react'
import { Bot, ChevronDown, User } from 'lucide-react'

import type { ChatTurn } from '../../chat/chatSession'
import { cacheLabel, isCacheHit, rateLimitSummary } from '../../lib/chat'
import { cn } from '../../lib/cn'
import { formatClock, formatInteger, formatLatency, formatUsd } from '../../lib/format'
import { Badge } from '../ui/Badge'
import { Spinner } from '../ui/Spinner'
import { ChatErrorCard } from './ChatErrorCard'
import { CopyButton, FormattedAnswer } from './FormattedAnswer'
import { ResponseDetails } from './ResponseDetails'

const clock = (iso: string | null) => (iso ? formatClock(new Date(iso)) : '—')

export function ChatTurnView({ turn, latest, busy, onRetry, onEdit }: {
  turn: ChatTurn
  /** The most recent turn shows its routing/usage details expanded. */
  latest: boolean
  busy: boolean
  onRetry: (turn: ChatTurn) => void
  onEdit: (turn: ChatTurn) => void
}) {
  const [open, setOpen] = useState<boolean | null>(null)
  const expanded = open ?? latest
  const r = turn.response
  const requestId = r?.request_id || turn.meta?.requestId || turn.error?.requestId || turn.clientRequestId
  const quota = rateLimitSummary(turn.meta?.rateLimit)

  return (
    <li className="space-y-3" data-testid={`turn-${turn.id}`} aria-label={`Interaction sent at ${clock(turn.sentAt)}`}>
      {/* user prompt */}
      <div className="flex justify-end gap-2">
        <div className="min-w-0 max-w-[85%] space-y-1">
          <div className="rounded-lg rounded-tr-sm bg-brand-600 px-3 py-2 text-sm text-white">
            <p className="whitespace-pre-wrap break-words" data-testid="prompt">{turn.query}</p>
          </div>
          <p className="text-right text-[11px] text-slate-400">
            Sent {clock(turn.sentAt)} · {turn.requestedModel ? <>override <span className="font-mono">{turn.requestedModel}</span></> : 'automatic routing'}
            {!turn.useCache && ' · cache skipped'}
          </p>
        </div>
        <div className="mt-0.5 hidden h-7 w-7 shrink-0 items-center justify-center rounded-full bg-brand-100 text-brand-700 sm:flex" aria-hidden="true">
          <User className="h-4 w-4" />
        </div>
      </div>

      {/* assistant side */}
      <div className="flex gap-2">
        <div className="mt-0.5 hidden h-7 w-7 shrink-0 items-center justify-center rounded-full bg-slate-100 text-slate-600 sm:flex" aria-hidden="true">
          <Bot className="h-4 w-4" />
        </div>
        <div className="min-w-0 flex-1 space-y-2">
          {turn.status === 'pending' && (
            <div role="status" aria-live="polite" className="flex items-center gap-2 rounded-lg border border-slate-200 bg-white px-3 py-2.5 text-sm text-slate-500">
              <Spinner size="sm" />
              Routing and generating… the backend classifies, selects a model and may fall back.
            </div>
          )}

          {turn.status === 'error' && turn.error && (
            <ChatErrorCard error={turn.error} receivedAt={turn.receivedAt} retryDisabled={busy}
              onRetry={() => onRetry(turn)} onEdit={() => onEdit(turn)} />
          )}

          {turn.status === 'success' && r && (
            <div className="rounded-lg rounded-tl-sm border border-slate-200 bg-white">
              <div className="flex flex-wrap items-center justify-between gap-2 border-b border-slate-100 px-3 py-2">
                <div className="flex min-w-0 flex-wrap items-center gap-1.5 text-xs">
                  <span className="font-mono font-medium text-slate-900">{r.model_used}</span>
                  <span className="text-slate-400">via {r.provider}</span>
                  {(r.fallback.used || r.selected_model !== r.model_used) && <Badge tone="warning">fallback</Badge>}
                  {isCacheHit(r.cache.status) && <Badge tone="success">{cacheLabel(r.cache.status)}</Badge>}
                  {r.mock_mode && <Badge tone="info">mock</Badge>}
                </div>
                <CopyButton text={r.answer} />
              </div>
              <div className="px-3 py-3"><FormattedAnswer text={r.answer} /></div>
              <div className="flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-slate-100 px-3 py-2 text-[11px] text-slate-500" data-testid="turn-summary">
                <span>{r.classification.complexity}</span>
                <span className="tabular-nums">{formatLatency(r.latency_ms)}</span>
                <span className="tabular-nums">{r.usage.total_tokens === null ? 'tokens unavailable' : `${formatInteger(r.usage.total_tokens)} tokens`}</span>
                <span className="tabular-nums">{r.cost.total_cost === null ? 'cost unavailable' : `${formatUsd(r.cost.total_cost)} est.`}</span>
                <button type="button" onClick={() => setOpen(!expanded)} aria-expanded={expanded}
                  className="ml-auto inline-flex items-center gap-1 rounded font-medium text-brand-700 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500">
                  {expanded ? 'Hide routing & usage' : 'Show routing & usage'}
                  <ChevronDown className={cn('h-3.5 w-3.5 transition-transform', expanded && 'rotate-180')} aria-hidden="true" />
                </button>
              </div>
              {expanded && <div className="border-t border-slate-100 bg-slate-50/60 px-3 py-3"><ResponseDetails response={r} rateLimit={turn.meta?.rateLimit ?? null} /></div>}
            </div>
          )}

          {turn.status !== 'pending' && (
            <p className="text-[11px] text-slate-400">
              {turn.status === 'success' ? 'Answered' : 'Failed'} {clock(turn.receivedAt)} · request{' '}
              <span className="font-mono" data-testid="request-id">{requestId}</span>
              {quota && <> · <span data-testid="rate-limit-quota">Rate limit: {quota}</span></>}
            </p>
          )}
        </div>
      </div>
    </li>
  )
}
