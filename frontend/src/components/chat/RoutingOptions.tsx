import { useState } from 'react'
import { AlertTriangle, KeyRound } from 'lucide-react'

import type { ApiError } from '../../api/errors'
import type { ModelsResponse } from '../../api/types'
import { credentialStore, useCredentialStatus } from '../../auth/credentials'
import { formatPricePerMillion, routerHealth, tiersOf } from '../../lib/registry'
import { CredentialPrompt } from '../auth/CredentialPrompt'
import { Badge } from '../ui/Badge'
import { Button } from '../ui/Button'
import { Card, CardBody, CardHeader } from '../ui/Card'

const AUTO = ''

/**
 * Model override + request options. Only registered, ENABLED models can be chosen; disabled
 * models are listed as unselectable so their status is visible. The backend still validates
 * the override (400 unknown_model / model_disabled) and makes the final routing decision.
 */
export function RoutingOptions({ registry, registryError, onReloadRegistry, override, onOverride, useCache, onUseCache, disabled }: {
  registry: ModelsResponse | null
  registryError: ApiError | null
  onReloadRegistry: () => void
  override: string | null
  onOverride: (id: string | null) => void
  useCache: boolean
  onUseCache: (v: boolean) => void
  disabled: boolean
}) {
  const models = registry?.models ?? []
  const selected = override ? models.find((m) => m.id === override) ?? null : null
  const placeholderEnabled = models.some((m) => m.enabled && m.pricing_is_placeholder)

  return (
    <Card>
      <CardHeader title="Routing options"
        actions={registry ? (registry.mock_mode
          ? <Badge tone="info">Mock mode</Badge>
          : <Badge tone="warning">Real providers</Badge>) : undefined} />
      <CardBody className="space-y-4">
        {registry && !registry.mock_mode && (
          <p className="flex gap-2 rounded-md bg-amber-50 p-2.5 text-xs text-amber-900">
            <AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0 text-amber-600" aria-hidden="true" />
            Real provider calls are enabled on the backend: each request may incur cost (budgets are enforced by the backend).
          </p>
        )}
        <label className="block">
          <span className="mb-1 block text-xs font-medium text-slate-600">Model</span>
          <select value={selected ? selected.id : AUTO} disabled={disabled} onChange={(e) => onOverride(e.target.value === AUTO ? null : e.target.value)}
            className="h-9 w-full rounded-md border border-slate-300 bg-white px-2 text-sm text-slate-700 shadow-sm focus:border-brand-500 focus:outline-none focus:ring-1 focus:ring-brand-500">
            <option value={AUTO}>Automatic routing (recommended)</option>
            {tiersOf(models).map((tier) => (
              <optgroup key={tier} label={`${tier} tier`}>
                {models.filter((m) => m.tier === tier).map((m) => (
                  <option key={m.id} value={m.id} disabled={!m.enabled}>
                    {m.id} · {m.provider}{m.enabled ? '' : ' (disabled)'}
                  </option>
                ))}
              </optgroup>
            ))}
          </select>
        </label>

        {registryError && (
          <div className="rounded-md border border-slate-200 bg-slate-50 p-2.5 text-xs text-slate-600" role="status">
            Model list unavailable ({registryError.message}). Only automatic routing can be used.{' '}
            <button type="button" onClick={onReloadRegistry} className="font-medium text-brand-700 hover:underline">Reload models</button>
          </div>
        )}

        {selected ? (
          <div className="space-y-2 rounded-md border border-slate-200 p-3 text-xs" data-testid="override-info">
            <div className="flex flex-wrap items-center gap-1.5">
              <span className="font-mono text-sm font-medium text-slate-900">{selected.id}</span>
              <Badge tone="success">Enabled</Badge>
              <Badge tone={routerHealth(selected).tone}>{routerHealth(selected).label}</Badge>
            </div>
            <p className="text-slate-600">{selected.provider} · <span className="font-mono">{selected.provider_model}</span> · {selected.tier} tier</p>
            <p className="tabular-nums text-slate-600">
              {formatPricePerMillion(selected.input_cost_per_million)} in / {formatPricePerMillion(selected.output_cost_per_million)} out per 1M tokens
            </p>
            {selected.pricing_is_placeholder && (
              <p className="flex gap-1.5 text-amber-800"><AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                Placeholder pricing: cost estimates for this model are not based on verified prices.</p>
            )}
            <p className="text-slate-500">Explicit override: the backend skips tier routing but still validates the model, and may fall back if it fails.</p>
          </div>
        ) : (
          <div className="space-y-1.5 text-xs text-slate-500">
            <p>The backend classifies each prompt and selects an enabled, healthy model for its complexity tier.</p>
            {placeholderEnabled && (
              <p className="flex gap-1.5 text-amber-800"><AlertTriangle className="mt-0.5 h-3.5 w-3.5 shrink-0" aria-hidden="true" />
                Some enabled models use placeholder pricing; cost figures are rough estimates.</p>
            )}
          </div>
        )}

        <label className="flex items-start gap-2 text-sm text-slate-700">
          <input type="checkbox" className="mt-0.5" checked={useCache} disabled={disabled} onChange={(e) => onUseCache(e.target.checked)} />
          <span>Use response cache<span className="block text-xs text-slate-500">Off sends <code className="font-mono">use_cache: false</code> for this request.</span></span>
        </label>

        <ChatKeyStatus />
      </CardBody>
    </Card>
  )
}

/** Shows whether a chat API key is held (never its value). Only needed when CHAT_AUTH_MODE=api_key. */
function ChatKeyStatus() {
  const status = useCredentialStatus()
  const [editing, setEditing] = useState(false)
  return (
    <div className="space-y-2 border-t border-slate-100 pt-3 text-xs" data-testid="chat-key-status">
      <div className="flex flex-wrap items-center justify-between gap-2">
        <span className="flex items-center gap-1.5 text-slate-600">
          <KeyRound className="h-3.5 w-3.5 text-slate-400" aria-hidden="true" />
          Chat API key: {status.chat
            ? <Badge tone="success">set ({status.persistent ? 'this tab' : 'memory only'})</Badge>
            : <Badge tone="neutral">not set</Badge>}
        </span>
        <span className="flex gap-1">
          {!editing && <Button variant="ghost" size="sm" onClick={() => setEditing(true)}>{status.chat ? 'Replace' : 'Set key'}</Button>}
          {status.chat && <Button variant="ghost" size="sm" onClick={() => credentialStore.clear('chat')}>Clear</Button>}
        </span>
      </div>
      <p className="text-slate-500">Only needed when the backend runs with <code className="font-mono">CHAT_AUTH_MODE=api_key</code>. Sent as a bearer header, never in the prompt or URL.</p>
      {editing && <CredentialPrompt kind="chat" onSaved={() => setEditing(false)} />}
    </div>
  )
}
