import { ChevronRight } from 'lucide-react'

import type { ModelOut } from '../../api/types'
import { cn } from '../../lib/cn'
import { formatInteger } from '../../lib/format'
import { formatPricePerMillion, formatSeconds, resolveFallbacks } from '../../lib/registry'
import { Badge } from '../ui/Badge'
import { FallbackStatusNote, PricingBadge, RegistryStatusBadge, RouterHealthBadge, TierBadge } from './badges'

interface Props {
  models: ModelOut[]
  all: readonly ModelOut[]
  selectedId: string | null
  onSelect: (id: string) => void
}

function Capabilities({ values }: { values: string[] }) {
  if (values.length === 0) return <span className="text-xs text-slate-400">none declared</span>
  return (
    <ul className="flex flex-wrap gap-1" aria-label="Capabilities">
      {values.map((c) => <li key={c}><Badge tone="neutral" className="font-mono text-[11px]">{c}</Badge></li>)}
    </ul>
  )
}

function Fallbacks({ model, all }: { model: ModelOut; all: readonly ModelOut[] }) {
  const list = resolveFallbacks(model, all)
  if (list.length === 0) return <span className="text-xs text-slate-400">none</span>
  return (
    <ol className="space-y-0.5 text-xs" aria-label="Fallback models">
      {list.map((f) => (
        <li key={f.id} className="whitespace-nowrap">
          <span className={cn('font-mono', f.status === 'enabled' ? 'text-slate-700' : 'text-slate-400 line-through')}>{f.id}</span>{' '}
          <FallbackStatusNote status={f.status} />
        </li>
      ))}
    </ol>
  )
}

const TH = 'whitespace-nowrap px-3 py-2 font-medium'

/** Full registry table (md and up). Scrolls horizontally inside its card; the Model ID column stays pinned. */
export function ModelTable({ models, all, selectedId, onSelect }: Props) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[1480px] text-left text-sm" aria-label="Registered models">
        <thead className="bg-slate-50 text-xs text-slate-500">
          <tr className="border-b border-slate-200">
            <th scope="col" className={cn(TH, 'sticky left-0 z-10 bg-slate-50')}>Model ID</th>
            <th scope="col" className={TH}>Provider</th>
            <th scope="col" className={TH}>Provider model</th>
            <th scope="col" className={TH}>Tier</th>
            <th scope="col" className={TH}>Status</th>
            <th scope="col" className={TH}>Router health</th>
            <th scope="col" className={TH}>Capabilities</th>
            <th scope="col" className={cn(TH, 'text-right')}>Input / 1M</th>
            <th scope="col" className={cn(TH, 'text-right')}>Output / 1M</th>
            <th scope="col" className={TH}>Pricing</th>
            <th scope="col" className={cn(TH, 'text-right')}>Context / max output</th>
            <th scope="col" className={cn(TH, 'text-right')}>Timeout</th>
            <th scope="col" className={cn(TH, 'text-right')}>Priority</th>
            <th scope="col" className={TH}>Fallbacks</th>
          </tr>
        </thead>
        <tbody>
          {models.map((m) => {
            const selected = m.id === selectedId
            return (
              <tr key={m.id} data-testid={`model-row-${m.id}`} data-selected={selected}
                className={cn('group cursor-pointer border-b border-slate-100 align-top last:border-0',
                  selected ? 'bg-brand-50' : 'hover:bg-slate-50', !m.enabled && 'text-slate-500')}
                onClick={() => onSelect(m.id)}>
                <th scope="row" className={cn('sticky left-0 z-10 px-3 py-2.5 text-left font-normal',
                  selected ? 'bg-brand-50' : 'bg-white group-hover:bg-slate-50')}>
                  <button type="button" onClick={(e) => { e.stopPropagation(); onSelect(m.id) }}
                    aria-label={`View details for ${m.id}`}
                    className="inline-flex items-center gap-1 whitespace-nowrap rounded font-mono text-xs font-medium text-brand-700 hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500">
                    {m.id}
                    <ChevronRight className="h-3 w-3" aria-hidden="true" />
                  </button>
                </th>
                <td className="px-3 py-2.5">{m.provider}</td>
                <td className="whitespace-nowrap px-3 py-2.5 font-mono text-xs">{m.provider_model}</td>
                <td className="px-3 py-2.5"><TierBadge tier={m.tier} /></td>
                <td className="px-3 py-2.5"><RegistryStatusBadge enabled={m.enabled} /></td>
                <td className="px-3 py-2.5"><RouterHealthBadge model={m} /></td>
                <td className="px-3 py-2.5"><Capabilities values={m.capabilities} /></td>
                <td className="px-3 py-2.5 text-right tabular-nums">{formatPricePerMillion(m.input_cost_per_million)}</td>
                <td className="px-3 py-2.5 text-right tabular-nums">{formatPricePerMillion(m.output_cost_per_million)}</td>
                <td className="px-3 py-2.5"><PricingBadge placeholder={m.pricing_is_placeholder} /></td>
                <td className="whitespace-nowrap px-3 py-2.5 text-right tabular-nums">
                  {formatInteger(m.max_context_tokens)} / {formatInteger(m.max_output_tokens)}
                </td>
                <td className="whitespace-nowrap px-3 py-2.5 text-right tabular-nums">{formatSeconds(m.timeout_seconds)}</td>
                <td className="px-3 py-2.5 text-right tabular-nums">{m.priority}</td>
                <td className="px-3 py-2.5"><Fallbacks model={m} all={all} /></td>
              </tr>
            )
          })}
        </tbody>
      </table>
    </div>
  )
}

/** Compact list for phones: key facts per model, tap for the full detail panel. */
export function ModelCardList({ models, selectedId, onSelect }: Omit<Props, 'all'>) {
  return (
    <ul className="divide-y divide-slate-100" aria-label="Registered models (compact)">
      {models.map((m) => (
        <li key={m.id}>
          <button type="button" onClick={() => onSelect(m.id)} aria-label={`View details for ${m.id}`}
            className={cn('flex w-full items-start gap-3 px-4 py-3 text-left focus:outline-none focus-visible:bg-slate-50',
              m.id === selectedId ? 'bg-brand-50' : 'hover:bg-slate-50')}>
            <div className="min-w-0 flex-1 space-y-1.5">
              <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
                <span className={cn('break-all font-mono text-xs font-medium', m.enabled ? 'text-slate-900' : 'text-slate-500')}>{m.id}</span>
                <TierBadge tier={m.tier} />
              </div>
              <p className="text-xs text-slate-500">{m.provider} · priority {m.priority}</p>
              <div className="flex flex-wrap gap-1.5">
                <RegistryStatusBadge enabled={m.enabled} />
                <RouterHealthBadge model={m} />
                {m.pricing_is_placeholder && <PricingBadge placeholder />}
              </div>
              <p className="text-xs tabular-nums text-slate-600">
                {formatPricePerMillion(m.input_cost_per_million)} in · {formatPricePerMillion(m.output_cost_per_million)} out
                <span className="text-slate-400"> per 1M tokens</span>
              </p>
            </div>
            <ChevronRight className="mt-0.5 h-4 w-4 shrink-0 text-slate-400" aria-hidden="true" />
          </button>
        </li>
      ))}
    </ul>
  )
}
