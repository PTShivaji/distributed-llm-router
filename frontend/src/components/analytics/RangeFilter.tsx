import { useState, type FormEvent } from 'react'
import { CalendarRange } from 'lucide-react'

import { cn } from '../../lib/cn'
import {
  customRangeError, fromLocalInput, RANGE_PRESETS, toLocalInput, type RangeKey, type RangeSelection,
} from '../../lib/analytics'
import { Button } from '../ui/Button'

const INPUT = 'h-9 rounded-md border border-slate-300 bg-white px-2 text-sm text-slate-700 shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500'

/**
 * Preset ranges apply immediately (one request each). A custom range is applied only with the
 * Apply button, so editing the inputs never fires requests. Inputs are in the browser's local time.
 */
export function RangeFilter({ selection, onChange, disabled }: {
  selection: RangeSelection
  onChange: (sel: RangeSelection) => void
  disabled?: boolean
}) {
  const [customOpen, setCustomOpen] = useState(selection.key === 'custom')
  const [start, setStart] = useState(() => toLocalInput(selection.start ? new Date(selection.start) : new Date(Date.now() - 24 * 3_600_000)))
  const [end, setEnd] = useState(() => toLocalInput(selection.end ? new Date(selection.end) : new Date()))
  const [touched, setTouched] = useState(false)
  const error = customRangeError(start, end)
  const active: RangeKey = customOpen ? 'custom' : selection.key

  function choose(key: Exclude<RangeKey, 'custom'>) {
    setCustomOpen(false)
    onChange({ key })
  }

  function apply(e: FormEvent) {
    e.preventDefault()
    setTouched(true)
    if (error) return
    onChange({ key: 'custom', start: fromLocalInput(start)!.toISOString(), end: fromLocalInput(end)!.toISOString() })
  }

  return (
    <div className="space-y-3">
      <div role="radiogroup" aria-label="Date range" className="flex flex-wrap gap-1.5">
        {RANGE_PRESETS.map((p) => (
          <button key={p.key} type="button" role="radio" aria-checked={active === p.key} disabled={disabled} onClick={() => choose(p.key)}
            className={cn('h-8 rounded-md border px-3 text-xs font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 disabled:opacity-60',
              active === p.key ? 'border-brand-600 bg-brand-600 text-white' : 'border-slate-300 bg-white text-slate-700 hover:bg-slate-50')}>
            {p.label}
          </button>
        ))}
        <button type="button" role="radio" aria-checked={active === 'custom'} disabled={disabled} onClick={() => setCustomOpen(true)}
          className={cn('inline-flex h-8 items-center gap-1.5 rounded-md border px-3 text-xs font-medium focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 disabled:opacity-60',
            active === 'custom' ? 'border-brand-600 bg-brand-600 text-white' : 'border-slate-300 bg-white text-slate-700 hover:bg-slate-50')}>
          <CalendarRange className="h-3.5 w-3.5" aria-hidden="true" />Custom
        </button>
      </div>
      {customOpen && (
        <form onSubmit={apply} className="flex flex-wrap items-end gap-3" aria-label="Custom date range" noValidate>
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-slate-600">Start (inclusive)</span>
            <input type="datetime-local" value={start} onChange={(e) => setStart(e.target.value)} className={INPUT} max={end || undefined} />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-slate-600">End (exclusive)</span>
            <input type="datetime-local" value={end} onChange={(e) => setEnd(e.target.value)} className={INPUT} min={start || undefined} />
          </label>
          <Button type="submit" size="md" disabled={disabled}>Apply range</Button>
          {(touched || selection.key === 'custom') && error && <p role="alert" className="w-full text-xs font-medium text-red-600">{error}</p>}
        </form>
      )}
    </div>
  )
}
