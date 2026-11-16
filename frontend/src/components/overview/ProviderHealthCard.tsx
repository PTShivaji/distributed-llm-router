import { Info } from 'lucide-react'

import type { ModelsResponse } from '../../api/types'
import { groupByProvider } from '../../lib/overview'
import { EmptyState } from '../feedback/States'
import { Badge, type BadgeTone } from '../ui/Badge'
import { Card, CardBody, CardHeader } from '../ui/Card'

const HEALTH_TONE: Record<string, BadgeTone> = { healthy: 'success', degraded: 'warning', unhealthy: 'danger' }

/**
 * Two separate facts per model:
 *   - Registry status: enabled/disabled in the model registry (configuration).
 *   - Router health: the circuit-breaker state from recent real calls. Not an active probe.
 */
export function ProviderHealthCard({ data }: { data: ModelsResponse }) {
  const groups = groupByProvider(data.models)
  return (
    <Card>
      <CardHeader title="Provider health" description="From GET /api/v1/models"
        actions={<Badge tone={data.mock_mode ? 'info' : 'neutral'}>{data.mock_mode ? 'Mock mode' : 'Real providers'}</Badge>} />
      <CardBody className="space-y-4">
        <p className="flex gap-2 rounded-md bg-slate-50 p-3 text-xs text-slate-600">
          <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-400" aria-hidden="true" />
          <span>
            <strong className="font-medium text-slate-700">Registry status</strong> is configuration (enabled or disabled).{' '}
            <strong className="font-medium text-slate-700">Router health</strong> is the circuit-breaker state from recent calls
            ({data.health_backend === 'redis' ? 'shared via Redis' : 'this instance only'}); it is not an availability probe.
            {data.mock_mode && ' In mock mode every call is served by the mock provider.'}
          </span>
        </p>
        {groups.length === 0 ? (
          <EmptyState title="No models registered" description="The model registry is empty." />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[520px] text-left text-sm">
              <thead className="text-xs text-slate-500">
                <tr className="border-b border-slate-100">
                  <th className="py-2 pr-3 font-medium">Model</th>
                  <th className="py-2 pr-3 font-medium">Provider</th>
                  <th className="py-2 pr-3 font-medium">Registry status</th>
                  <th className="py-2 pr-3 font-medium">Router health</th>
                  <th className="py-2 text-right font-medium">Failures (consecutive / total)</th>
                </tr>
              </thead>
              <tbody>
                {groups.flatMap((g) => g.models).map((m) => (
                  <tr key={m.id} className="border-b border-slate-50 last:border-0" data-testid={`health-${m.id}`}>
                    <td className="py-2 pr-3 font-mono text-xs text-slate-800">{m.id}</td>
                    <td className="py-2 pr-3 text-slate-600">{m.provider}</td>
                    <td className="py-2 pr-3">
                      <Badge tone={m.enabled ? 'success' : 'neutral'}>{m.enabled ? 'Enabled' : 'Disabled'}</Badge>
                    </td>
                    <td className="py-2 pr-3">
                      {m.enabled
                        ? <Badge tone={HEALTH_TONE[m.health.state] ?? 'neutral'}>{m.health.state}</Badge>
                        : <span className="text-xs text-slate-400">not routed</span>}
                      {m.health.last_error_kind && <span className="ml-2 text-xs text-slate-400">last error: {m.health.last_error_kind}</span>}
                    </td>
                    <td className="py-2 text-right text-xs tabular-nums text-slate-600">
                      {m.health.consecutive_failures} / {m.health.total_failures}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        <ul className="flex flex-wrap gap-2" aria-label="Providers">
          {groups.map((g) => (
            <li key={g.provider}><Badge tone="neutral">{g.provider}: {g.enabled}/{g.models.length} enabled</Badge></li>
          ))}
        </ul>
      </CardBody>
    </Card>
  )
}
