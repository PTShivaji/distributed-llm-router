/**
 * Pure data shaping for the Model Registry page.
 * Everything here only counts, filters, sorts or joins fields returned by GET /api/v1/models;
 * nothing is inferred (no capabilities, prices or availability are invented).
 */

import type { ComplexityLevel, ModelOut, ModelsResponse } from '../api/types'
import type { BadgeTone } from '../components/ui/Badge'

export const TIER_ORDER: readonly ComplexityLevel[] = ['SIMPLE', 'MEDIUM', 'COMPLEX']

export interface RegistrySummary {
  total: number
  enabled: number
  disabled: number
  providers: string[]
  placeholderPricing: number
}

export function summarizeRegistry(models: readonly ModelOut[]): RegistrySummary {
  const enabled = models.filter((m) => m.enabled).length
  return {
    total: models.length,
    enabled,
    disabled: models.length - enabled,
    providers: providersOf(models),
    placeholderPricing: models.filter((m) => m.pricing_is_placeholder).length,
  }
}

export function providersOf(models: readonly ModelOut[]): string[] {
  return [...new Set(models.map((m) => m.provider))].sort((a, b) => a.localeCompare(b))
}

/** Tiers present in the data, in complexity order (unknown tiers last, alphabetically). */
export function tiersOf(models: readonly ModelOut[]): string[] {
  const present = new Set<string>(models.map((m) => m.tier))
  const known = TIER_ORDER.filter((t) => present.has(t))
  const other = [...present].filter((t) => !(TIER_ORDER as readonly string[]).includes(t)).sort()
  return [...known, ...other]
}

export type StatusFilter = 'all' | 'enabled' | 'disabled'

export interface RegistryFilters {
  query: string
  /** '' = all providers */
  provider: string
  status: StatusFilter
  /** '' = all tiers */
  tier: string
}

export const NO_FILTERS: RegistryFilters = { query: '', provider: '', status: 'all', tier: '' }

export function hasActiveFilters(f: RegistryFilters): boolean {
  return f.query.trim() !== '' || f.provider !== '' || f.status !== 'all' || f.tier !== ''
}

/** Search matches the model id, provider or provider model identifier (case-insensitive substring). */
export function filterModels(models: readonly ModelOut[], f: RegistryFilters): ModelOut[] {
  const q = f.query.trim().toLowerCase()
  return models.filter((m) =>
    (q === '' || m.id.toLowerCase().includes(q) || m.provider.toLowerCase().includes(q) || m.provider_model.toLowerCase().includes(q))
    && (f.provider === '' || m.provider === f.provider)
    && (f.status === 'all' || (f.status === 'enabled') === m.enabled)
    && (f.tier === '' || m.tier === f.tier))
}

/** Stable display order: tier (SIMPLE → COMPLEX), then routing priority (lower = preferred), then id. */
export function sortModels(models: readonly ModelOut[]): ModelOut[] {
  const rank = (t: string) => {
    const i = (TIER_ORDER as readonly string[]).indexOf(t)
    return i === -1 ? TIER_ORDER.length : i
  }
  return [...models].sort((a, b) => rank(a.tier) - rank(b.tier) || a.priority - b.priority || a.id.localeCompare(b.id))
}

export type FallbackStatus = 'enabled' | 'disabled' | 'unregistered'

export interface ResolvedFallback {
  id: string
  status: FallbackStatus
}

/**
 * The model's configured fallback ids, in order, joined with the registry.
 * The backend's fallback chain skips disabled and unregistered ids (RoutingPolicy.fallback_chain).
 */
export function resolveFallbacks(model: ModelOut, all: readonly ModelOut[]): ResolvedFallback[] {
  const byId = new Map(all.map((m) => [m.id, m]))
  return model.fallbacks.map((id) => {
    const target = byId.get(id)
    return { id, status: !target ? 'unregistered' : target.enabled ? 'enabled' : 'disabled' }
  })
}

export interface HealthView {
  label: string
  tone: BadgeTone
  /** Short explanation of what the state means for routing. */
  note: string
}

/**
 * Router health is the backend's circuit-breaker state from recent real calls, NOT a live provider probe.
 * A disabled model is never shown as available, whatever its tracked state.
 */
export function routerHealth(model: ModelOut): HealthView {
  if (!model.enabled) return { label: 'Not routed', tone: 'neutral', note: 'Disabled in the registry, so it is never selected.' }
  switch (model.health.state) {
    case 'healthy':
      return { label: 'healthy', tone: 'success', note: 'No recent consecutive failures.' }
    case 'degraded':
      return { label: 'degraded', tone: 'warning', note: 'Some consecutive failures; still routable.' }
    case 'unhealthy':
      return { label: 'unhealthy', tone: 'danger', note: 'Circuit open: skipped by routing until the cooldown ends.' }
    default:
      return { label: model.health.state, tone: 'neutral', note: 'State reported by the backend.' }
  }
}

export function strategyDescription(strategy: ModelsResponse['routing_strategy']): string {
  if (strategy === 'priority') return 'Lowest priority number is preferred within a tier.'
  if (strategy === 'lowest_cost') return 'Lowest blended price (input + output) is preferred; priority breaks ties.'
  return 'Strategy reported by the backend.'
}

const price = new Intl.NumberFormat('en-US', { style: 'currency', currency: 'USD', minimumFractionDigits: 2, maximumFractionDigits: 4 })

/** Registry price in USD per 1M tokens, exactly as configured (no rounding to zero for tiny prices). */
export function formatPricePerMillion(usd: number): string {
  if (!Number.isFinite(usd)) return '—'
  if (usd > 0 && usd < 0.0001) return `$${usd.toPrecision(2)}`
  return price.format(usd)
}

export function formatSeconds(seconds: number): string {
  if (!Number.isFinite(seconds)) return '—'
  return `${Number.isInteger(seconds) ? seconds : seconds.toFixed(1)} s`
}
