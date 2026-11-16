import type { ReactNode } from 'react'

import { cn } from '../../lib/cn'

export type BadgeTone = 'neutral' | 'success' | 'warning' | 'danger' | 'info' | 'demo'

const TONES: Record<BadgeTone, string> = {
  neutral: 'bg-slate-100 text-slate-700 ring-slate-200',
  success: 'bg-emerald-50 text-emerald-700 ring-emerald-200',
  warning: 'bg-amber-50 text-amber-800 ring-amber-200',
  danger: 'bg-red-50 text-red-700 ring-red-200',
  info: 'bg-brand-50 text-brand-700 ring-brand-100',
  demo: 'bg-violet-50 text-violet-700 ring-violet-200',
}

export function Badge({ tone = 'neutral', icon, children, className }: {
  tone?: BadgeTone; icon?: ReactNode; children: ReactNode; className?: string
}) {
  return (
    <span className={cn('inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-xs font-medium ring-1 ring-inset',
      TONES[tone], className)}>
      {icon}
      {children}
    </span>
  )
}

/** Mandatory label for any illustrative (non-live) data. */
export function DemoBadge({ label = 'Demo data' }: { label?: string }) {
  return <Badge tone="demo">{label}</Badge>
}
