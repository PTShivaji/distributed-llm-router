/**
 * API types, aliased from the GENERATED schema (src/api/schema.gen.ts).
 * Regenerate after backend schema changes:  npm run gen:api
 * Do not hand-edit field names here; they must match the backend's Pydantic models.
 */

import type { components } from './schema.gen'

type Schemas = components['schemas']

export type HealthResponse = Schemas['HealthResponse']
export type ModelsResponse = Schemas['ModelsResponse']
export type ModelOut = Schemas['ModelOut']
export type ModelHealthOut = Schemas['ModelHealthOut']
export type ChatRequest = Schemas['ChatRequest']
export type ChatResponse = Schemas['ChatResponse']
export type ClassifyRequest = Schemas['ClassifyRequest']
export type ClassifyResponse = Schemas['ClassifyResponse']
export type AnalyticsSummaryResponse = Schemas['AnalyticsSummaryResponse']
export type CacheStatsResponse = Schemas['CacheStatsResponse']
export type CacheClearResponse = Schemas['CacheClearResponse']
export type DependenciesResponse = Schemas['DependenciesResponse']
export type ComplexityLevel = Schemas['ComplexityLevel']

// Step 10.4: read-only Settings and Budget endpoints (observability token).
export type SettingsResponse = Schemas['SettingsResponse']
export type ProviderInfo = Schemas['ProviderInfo']
export type ConfigIssueOut = Schemas['ConfigIssueOut']
export type BudgetStatusResponse = Schemas['BudgetStatusResponse']
export type BudgetSpendResponse = Schemas['BudgetSpendResponse']
export type SpendFigures = Schemas['SpendFigures']
export type SpendDay = Schemas['SpendDay']
export type SpendGroup = Schemas['SpendGroup']
export type BudgetRefusalsResponse = Schemas['BudgetRefusalsResponse']
export type RefusalEvent = Schemas['RefusalEvent']
export type ProcessCounterGroup = Schemas['ProcessCounterGroup']
