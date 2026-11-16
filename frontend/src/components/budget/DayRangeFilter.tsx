import { useState, type FormEvent } from 'react'
import { CalendarRange } from 'lucide-react'

import { cn } from '../../lib/cn'
import {
  addDays, BUDGET_RANGE_PRESETS, customDayRangeError, type BudgetRangeKey, type BudgetRangeSelection,
} from '../../lib/budgetDashboard'
import { Button } from '../ui/Button'

const INPUT = 'h-9 rounded-md border border-slate-300 bg-white px-2 text-sm text-slate-700 shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500'
const CHIP = 'h-8 rounded-md border px-3 text-xs font-medium transition-colors focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500 disabled:opacity-60'
const chip = (on: boolean) => (on ? 'border-brand-600 bg-brand-600 text-white' : 'border-slate-300 bg-white text-slate-700 hover:bg-slate-50')

/**
 * UTC-day range for GET /api/v1/budget/{spend,refusals} (start_date / end_date, inclusive). Presets apply at once;
 * a custom range only with Apply, after the same checks the backend makes (so invalid ranges are never sent).
 */
export function DayRangeFilter({ selection, onChange, today, disabled }: {
  selection: BudgetRangeSelection
  onChange: (sel: BudgetRangeSelection) => void
  /** Today's UTC day (YYYY-MM-DD). */
  today: string
  disabled?: boolean
}) {
  const [customOpen, setCustomOpen] = useState(selection.key === 'custom')
  const [start, setStart] = useState(selection.start ?? addDays(today, -6))
  const [end, setEnd] = useState(selection.end ?? today)
  const [touched, setTouched] = useState(false)
  const error = customDayRangeError(start, end, today)
  const active: BudgetRangeKey = customOpen ? 'custom' : selection.key

  function apply(e: FormEvent) {
    e.preventDefault()
    setTouched(true)
    if (!error) onChange({ key: 'custom', start, end })
  }

  return (
    <div className="space-y-3">
      <div role="radiogroup" aria-label="Date range" className="flex flex-wrap gap-1.5">
        {BUDGET_RANGE_PRESETS.map((p) => (
          <button key={p.key} type="button" role="radio" aria-checked={active === p.key} disabled={disabled}
            onClick={() => { setCustomOpen(false); onChange({ key: p.key }) }} className={cn(CHIP, chip(active === p.key))}>
            {p.label}
          </button>
        ))}
        <button type="button" role="radio" aria-checked={active === 'custom'} disabled={disabled} onClick={() => setCustomOpen(true)}
          className={cn(CHIP, 'inline-flex items-center gap-1.5', chip(active === 'custom'))}>
          <CalendarRange className="h-3.5 w-3.5" aria-hidden="true" />Custom
        </button>
      </div>
      {customOpen && (
        <form onSubmit={apply} className="flex flex-wrap items-end gap-3" aria-label="Custom date range" noValidate>
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-slate-600">Start day (UTC, inclusive)</span>
            <input type="date" value={start} onChange={(e) => setStart(e.target.value)} className={INPUT} max={end || today} />
          </label>
          <label className="block">
            <span className="mb-1 block text-xs font-medium text-slate-600">End day (UTC, inclusive)</span>
            <input type="date" value={end} onChange={(e) => setEnd(e.target.value)} className={INPUT} min={start || undefined} max={today} />
          </label>
          <Button type="submit" size="md" disabled={disabled}>Apply range</Button>
          {touched && error && <p role="alert" className="w-full text-xs font-medium text-red-600">{error}</p>}
        </form>
      )}
    </div>
  )
}
