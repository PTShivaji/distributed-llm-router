/** Pure data shaping for the Overview page (no fabrication: only reorders/joins API fields). */

import type { AnalyticsSummaryResponse, ModelOut, ModelsResponse } from '../api/types'

export interface ModelUsageRow {
  model: string
  provider: string | null
  requests: number
  totalTokens: number
  estimatedCost: number
  averageLatencyMs: number | null
}

/** analytics `requests_by_model`, sorted by requests, with the provider joined from the registry (null if unknown). */
export function modelUsageRows(byModel: AnalyticsSummaryResponse['requests_by_model'], models: ModelsResponse | null): ModelUsageRow[] {
  const provider = new Map((models?.models ?? []).map((m) => [m.id, m.provider]))
  return [...byModel]
    .sort((a, b) => b.requests - a.requests)
    .map((m) => ({
      model: m.model, provider: provider.get(m.model) ?? null, requests: m.requests, totalTokens: m.total_tokens,
      estimatedCost: m.estimated_cost, averageLatencyMs: m.average_latency_ms ?? null,
    }))
}

export interface ProviderGroup {
  provider: string
  models: ModelOut[]
  enabled: number
}

export function groupByProvider(models: ModelOut[]): ProviderGroup[] {
  const groups = new Map<string, ProviderGroup>()
  for (const m of models) {
    const g = groups.get(m.provider) ?? { provider: m.provider, models: [], enabled: 0 }
    g.models.push(m)
    if (m.enabled) g.enabled += 1
    groups.set(m.provider, g)
  }
  return [...groups.values()].sort((a, b) => a.provider.localeCompare(b.provider))
}
