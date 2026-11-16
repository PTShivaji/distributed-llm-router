import type { ReactNode } from 'react'

import { cn } from '../../lib/cn'

export type KpiTone = 'neutral' | 'success' | 'danger' | 'brand' | 'warning'

const ICON_TONES: Record<KpiTone, string> = {
  neutral: 'bg-slate-100 text-slate-600',
  success: 'bg-emerald-50 text-emerald-600',
  danger: 'bg-red-50 text-red-600',
  brand: 'bg-brand-50 text-brand-600',
  warning: 'bg-amber-50 text-amber-600',
}

/** Compact KPI tile. While loading it shows a skeleton, never a placeholder number. */
export function KpiCard({ label, value, hint, icon, tone = 'neutral', loading = false }: {
  label: string
  value: ReactNode
  hint?: ReactNode
  icon: ReactNode
  tone?: KpiTone
  loading?: boolean
}) {
  return (
    <div className="flex min-w-0 items-start gap-3 rounded-lg border border-slate-200 bg-white p-3 shadow-sm sm:p-4" role="group" aria-label={label}>
      {/* decorative icon hidden on phones so two cards fit per row */}
      <div className={cn('hidden h-9 w-9 shrink-0 items-center justify-center rounded-md sm:flex', ICON_TONES[tone])} aria-hidden="true">{icon}</div>
      <div className="min-w-0">
        <p className="truncate text-xs font-medium text-slate-500">{label}</p>
        {loading ? (
          <div className="mt-1.5 h-6 w-20 animate-pulse rounded bg-slate-100" aria-label={`${label} loading`} />
        ) : (
          <p className="mt-0.5 truncate text-xl font-semibold tracking-tight text-slate-900 tabular-nums" data-testid={`kpi-${label}`}>{value}</p>
        )}
        {hint && !loading && <p className="mt-0.5 text-[11px] leading-snug text-slate-500">{hint}</p>}
      </div>
    </div>
  )
}
