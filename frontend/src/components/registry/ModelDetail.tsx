import type { ReactNode } from 'react'
import { AlertTriangle, Info } from 'lucide-react'

import type { ModelOut, ModelsResponse } from '../../api/types'
import { cn } from '../../lib/cn'
import { formatInteger } from '../../lib/format'
import { formatPricePerMillion, formatSeconds, resolveFallbacks, routerHealth, strategyDescription } from '../../lib/registry'
import { Badge } from '../ui/Badge'
import { FallbackStatusNote, PricingBadge, RegistryStatusBadge, RouterHealthBadge, TierBadge } from './badges'

function Section({ title, children }: { title: string; children: ReactNode }) {
  return (
    <section className="space-y-2">
      <h3 className="text-xs font-semibold uppercase tracking-wide text-slate-500">{title}</h3>
      {children}
    </section>
  )
}

function Fields({ rows }: { rows: [string, ReactNode][] }) {
  return (
    <dl className="divide-y divide-slate-100 rounded-md border border-slate-200 text-sm">
      {rows.map(([label, value]) => (
        <div key={label} className="flex flex-wrap items-start justify-between gap-x-4 gap-y-0.5 px-3 py-2">
          <dt className="text-slate-500">{label}</dt>
          <dd className="min-w-0 text-right text-slate-900">{value}</dd>
        </div>
      ))}
    </dl>
  )
}

function Note({ tone = 'info', children }: { tone?: 'info' | 'warning'; children: ReactNode }) {
  const Icon = tone === 'warning' ? AlertTriangle : Info
  return (
    <p className={cn('flex gap-2 rounded-md p-3 text-xs',
      tone === 'warning' ? 'bg-amber-50 text-amber-900' : 'bg-slate-50 text-slate-600')}>
      <Icon className={cn('mt-0.5 h-3.5 w-3.5 shrink-0', tone === 'warning' ? 'text-amber-600' : 'text-slate-400')} aria-hidden="true" />
      <span>{children}</span>
    </p>
  )
}

/** Full registry entry for one model, as returned by GET /api/v1/models (nothing added). */
export function ModelDetail({ model, registry, onSelect }: {
  model: ModelOut
  registry: ModelsResponse
  onSelect: (id: string) => void
}) {
  const health = routerHealth(model)
  const fallbacks = resolveFallbacks(model, registry.models)
  return (
    <div className="space-y-6" data-testid="model-detail">
      <Section title="Registry entry">
        {model.description && <p className="text-sm text-slate-700">{model.description}</p>}
        <Fields rows={[
          ['Model ID', <span className="break-all font-mono text-xs">{model.id}</span>],
          ['Provider', model.provider],
          ['Provider model', <span className="break-all font-mono text-xs">{model.provider_model}</span>],
          ['Tier', <TierBadge tier={model.tier} />],
          ['Status', <RegistryStatusBadge enabled={model.enabled} />],
        ]} />
        {model.enabled
          ? <Note>Enabled means this model is eligible for routing. It does not guarantee the provider is reachable.</Note>
          : <Note tone="warning">Disabled: the router never selects this model, it is skipped as a fallback, and an explicit
            override is refused with <code className="font-mono">model_disabled</code>.</Note>}
      </Section>

      <Section title="Capabilities">
        {model.capabilities.length === 0
          ? <p className="text-sm text-slate-500">No capabilities declared in the registry.</p>
          : (
            <ul className="flex flex-wrap gap-1.5" aria-label="Capabilities">
              {model.capabilities.map((c) => <li key={c}><Badge tone="neutral" className="font-mono">{c}</Badge></li>)}
            </ul>
          )}
      </Section>

      <Section title="Pricing (USD per 1M tokens)">
        <Fields rows={[
          ['Input', <span className="tabular-nums">{formatPricePerMillion(model.input_cost_per_million)}</span>],
          ['Output', <span className="tabular-nums">{formatPricePerMillion(model.output_cost_per_million)}</span>],
          ['Pricing source', <PricingBadge placeholder={model.pricing_is_placeholder} />],
        ]} />
        {model.pricing_is_placeholder
          ? <Note tone="warning">Placeholder pricing: these rates are not verified provider prices. Cost figures based on them are
            rough estimates only.</Note>
          : <Note>Configured in the registry. Costs shown elsewhere are estimates computed from these rates.</Note>}
      </Section>

      <Section title="Limits">
        <Fields rows={[
          ['Max context tokens', <span className="tabular-nums">{formatInteger(model.max_context_tokens)}</span>],
          ['Max output tokens', <span className="tabular-nums">{formatInteger(model.max_output_tokens)}</span>],
          ['Timeout', <span className="tabular-nums">{formatSeconds(model.timeout_seconds)}</span>],
        ]} />
        <p className="text-xs text-slate-500">Timeout is the effective value: the model's own setting or the backend default.</p>
      </Section>

      <Section title="Routing">
        <Fields rows={[
          ['Priority', <span className="tabular-nums">{model.priority}</span>],
          ['Strategy', <span className="font-mono text-xs">{registry.routing_strategy}</span>],
        ]} />
        <p className="text-xs text-slate-500">{strategyDescription(registry.routing_strategy)}</p>
        <div>
          <p className="mb-1 text-sm text-slate-500">Fallback models (in order)</p>
          {fallbacks.length === 0 ? <p className="text-sm text-slate-500">None configured.</p> : (
            <ol className="space-y-1" aria-label="Fallback models">
              {fallbacks.map((f, i) => (
                <li key={f.id} className="flex flex-wrap items-center gap-2 text-sm">
                  <span className="w-4 text-right text-xs tabular-nums text-slate-400">{i + 1}.</span>
                  {f.status === 'unregistered'
                    ? <span className="font-mono text-xs text-slate-400 line-through">{f.id}</span>
                    : (
                      <button type="button" onClick={() => onSelect(f.id)}
                        className={cn('rounded font-mono text-xs hover:underline focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500',
                          f.status === 'enabled' ? 'text-brand-700' : 'text-slate-400 line-through')}>
                        {f.id}
                      </button>
                    )}
                  <FallbackStatusNote status={f.status} />
                </li>
              ))}
            </ol>
          )}
        </div>
      </Section>

      <Section title="Router health">
        <Fields rows={[
          ['State', <RouterHealthBadge model={model} />],
          ['Consecutive failures', <span className="tabular-nums">{formatInteger(model.health.consecutive_failures)}</span>],
          ['Total failures', <span className="tabular-nums">{formatInteger(model.health.total_failures)}</span>],
          ['Total successes', <span className="tabular-nums">{formatInteger(model.health.total_successes)}</span>],
          ['Last error kind', model.health.last_error_kind
            ? <span className="font-mono text-xs">{model.health.last_error_kind}</span>
            : <span className="text-slate-400">none</span>],
        ]} />
        <Note>
          {health.note}{' '}
          Router health is failure tracking from recent real calls
          ({registry.health_backend === 'redis' ? 'shared across instances via Redis' : 'this backend instance only'}).
          It is not a live provider check.
          {!model.enabled && model.health.state !== 'healthy' && ` Tracked state: ${model.health.state}.`}
        </Note>
      </Section>
    </div>
  )
}
