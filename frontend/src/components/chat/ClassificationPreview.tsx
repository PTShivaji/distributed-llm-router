import { Info, X } from 'lucide-react'

import type { ApiError } from '../../api/errors'
import type { ClassifyResponse } from '../../api/types'
import { intentLabel } from '../../lib/chat'
import { formatPercent } from '../../lib/format'
import { ErrorState } from '../feedback/States'
import { Badge } from '../ui/Badge'
import { Spinner } from '../ui/Spinner'

export interface PreviewState {
  query: string
  loading: boolean
  result: ClassifyResponse | null
  error: ApiError | null
}

/**
 * Result of an explicit POST /api/v1/classify. Informational only: it is NOT sent with the
 * chat request; the backend classifies again and makes its own routing decision.
 */
export function ClassificationPreview({ state, currentQuery, onClose, onRetry }: {
  state: PreviewState
  currentQuery: string
  onClose: () => void
  onRetry: () => void
}) {
  const stale = state.query !== currentQuery
  const c = state.result
  return (
    <section className="rounded-md border border-brand-100 bg-brand-50/40 p-3 text-sm" aria-label="Classification preview" data-testid="classification-preview">
      <div className="mb-2 flex items-start justify-between gap-2">
        <div>
          <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-600">Classification preview</h3>
          <p className="flex items-start gap-1 text-[11px] text-slate-500">
            <Info className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
            Preview only. The backend classifies the prompt again when you send it and decides the routing itself.
          </p>
        </div>
        <button type="button" onClick={onClose} aria-label="Close classification preview"
          className="rounded p-1 text-slate-400 hover:bg-white hover:text-slate-600"><X className="h-3.5 w-3.5" aria-hidden="true" /></button>
      </div>
      {state.loading ? (
        <p role="status" className="flex items-center gap-2 text-xs text-slate-500"><Spinner size="sm" /> Classifying…</p>
      ) : state.error ? (
        <ErrorState error={state.error} onRetry={onRetry} compact />
      ) : c ? (
        <div className="space-y-2">
          {stale && <p className="text-[11px] font-medium text-amber-700">The prompt has changed since this preview.</p>}
          <dl className="grid grid-cols-2 gap-x-4 gap-y-1.5 text-xs sm:grid-cols-4">
            <div><dt className="text-slate-500">Complexity</dt><dd><Badge tone="info" className="font-mono text-[11px]">{c.complexity}</Badge></dd></div>
            <div><dt className="text-slate-500">Score</dt><dd className="tabular-nums text-slate-900">{c.complexity_score.toFixed(2)}</dd></div>
            <div><dt className="text-slate-500">Confidence</dt><dd className="tabular-nums text-slate-900">{formatPercent(c.confidence)}</dd></div>
            <div><dt className="text-slate-500">Intent</dt><dd className="text-slate-900">{intentLabel(c.intent.primary)} <span className="tabular-nums text-slate-500">{formatPercent(c.intent.confidence)}</span></dd></div>
          </dl>
          <p className="text-xs text-slate-600">{c.explanation}</p>
          {c.reasoning.signals.length > 0 && (
            <ul className="flex flex-wrap gap-1" aria-label="Classifier signals">
              {c.reasoning.signals.map((s, i) => (
                <li key={`${s.name}-${i}`} title={s.detail}><Badge tone="neutral" className="font-mono text-[11px]">{s.name} {s.weight >= 0 ? '+' : ''}{s.weight.toFixed(2)}</Badge></li>
              ))}
            </ul>
          )}
          <p className="text-[11px] text-slate-400">Classifier {c.classifier_version} · POST /api/v1/classify</p>
        </div>
      ) : null}
    </section>
  )
}
