import type { ModelOut } from '../../api/types'
import { routerHealth, type FallbackStatus } from '../../lib/registry'
import { Badge } from '../ui/Badge'

/** Enabled = eligible for routing (configuration), not a statement about provider availability. */
export function RegistryStatusBadge({ enabled }: { enabled: boolean }) {
  return enabled
    ? <Badge tone="success">Enabled</Badge>
    : <Badge tone="neutral" className="line-through decoration-slate-400/60">Disabled</Badge>
}

export function PricingBadge({ placeholder }: { placeholder: boolean }) {
  return placeholder
    ? <Badge tone="warning" className="whitespace-nowrap">Placeholder pricing</Badge>
    : <Badge tone="neutral" className="whitespace-nowrap">Configured</Badge>
}

export function RouterHealthBadge({ model }: { model: ModelOut }) {
  const h = routerHealth(model)
  return <span title={h.note}><Badge tone={h.tone} className="whitespace-nowrap">{h.label}</Badge></span>
}

export function TierBadge({ tier }: { tier: string }) {
  return <Badge tone="info" className="font-mono text-[11px]">{tier}</Badge>
}

const FALLBACK_LABEL: Record<FallbackStatus, string | null> = { enabled: null, disabled: 'disabled, skipped', unregistered: 'not registered, skipped' }

export function FallbackStatusNote({ status }: { status: FallbackStatus }) {
  const label = FALLBACK_LABEL[status]
  return label ? <span className="text-[11px] text-amber-700">({label})</span> : null
}
