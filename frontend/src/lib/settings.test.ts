import { describe, expect, it } from 'vitest'

import type { ProviderInfo } from '../api/types'
import { CAPTURED, settingsFixture } from '../test/budgetFixtures'
import {
  CREDENTIAL_STATUS_TEXT, credentialStatus, credentialSummary, formatDuration, isSettingsResponse, issueCounts, settingsCheck, sortedIssues,
} from './settings'
import { bool, explain, obj, str } from './validate'

const SECRET_LOOKING = 'sk-SENTINEL-must-never-be-echoed-0002'

describe('settings validation', () => {
  it('accepts the real backend body', () => {
    expect(explain(settingsCheck, CAPTURED.settings)).toBeNull()
    expect(CAPTURED.settings.providers.length).toBeGreaterThan(0)
  })

  it('accepts optional values as null', () => {
    const s = settingsFixture()
    s.rate_limits.per_minute = null
    s.http.hsts_max_age_seconds = null
    s.cache.embedding_model = null
    s.budget.daily_usd = null
    expect(isSettingsResponse(s)).toBe(true)
  })

  it.each([
    ['missing section', (s: Record<string, unknown>) => { delete s.auth }, 'body.auth: missing'],
    ['a secret-looking extra field', (s: Record<string, unknown>) => {
      (s.auth as Record<string, unknown>).admin_api_token = SECRET_LOOKING
    }, 'unexpected field'],
    ['a credential value instead of a boolean', (s: Record<string, unknown>) => {
      ((s.providers as Record<string, unknown>[]).find((p) => p.credential_required === true) as Record<string, unknown>)
        .credential_configured = SECRET_LOOKING
    }, 'credential_configured'],
    ['"not configured" for a provider without a credential', (s: Record<string, unknown>) => {
      ((s.providers as Record<string, unknown>[]).find((p) => p.provider === 'mock') as Record<string, unknown>)
        .credential_configured = false
    }, 'must be null when no credential is required'],
    ['monthly limit claimed', (s: Record<string, unknown>) => {
      (s.budget as Record<string, unknown>).monthly_limit_supported = true
    }, 'monthly_limit_supported'],
    ['bad issue severity', (s: Record<string, unknown>) => {
      ((s.production_checks as Record<string, unknown>).issues as unknown[]).push({ setting: 'X', severity: 'fatal', message: 'm' })
    }, 'severity'],
    ['negative count', (s: Record<string, unknown>) => { (s.auth as Record<string, unknown>).chat_clients_configured = -1 },
      'integer >= 0'],
  ])('rejects %s without echoing values', (_name, mutate, expected) => {
    const body = settingsFixture() as unknown as Record<string, unknown>
    mutate(body)
    const problem = explain(settingsCheck, body)
    expect(problem).toContain(expected)
    expect(problem).not.toContain(SECRET_LOOKING)
  })

  it('maps provider credential status to configured / not configured / not required only', () => {
    const p = (credential_required: boolean, credential_configured: boolean | null) =>
      ({ credential_required, credential_configured }) as Pick<ProviderInfo, 'credential_required' | 'credential_configured'>
    expect(credentialStatus(p(true, true))).toBe('configured')
    expect(credentialStatus(p(true, false))).toBe('not_configured')
    expect(credentialStatus(p(false, null))).toBe('not_required')
    expect(CREDENTIAL_STATUS_TEXT).toEqual({ configured: 'Configured', not_configured: 'Not configured', not_required: 'Not required' })
    for (const provider of CAPTURED.settings.providers) {
      expect(['configured', 'not_configured', 'not_required']).toContain(credentialStatus(provider))
    }
  })

  it('obj() validators are tied to the generated types (compile-time completeness)', () => {
    // @ts-expect-error - every field of the generated type must have a check
    obj<ProviderInfo>({ provider: str })
    // @ts-expect-error - fields that are not in the generated type are rejected
    obj<{ a: boolean }>({ a: bool, b: bool })
    expect(true).toBe(true)
  })
})

describe('settings display helpers', () => {
  it('counts only providers that require a credential', () => {
    const providers = settingsFixture().providers.map((p) => (p.credential_required
      ? { ...p, credential_configured: p.provider === 'anthropic' } : p))
    expect(credentialSummary(providers)).toEqual({ required: 5, configured: 1 })
    expect(credentialSummary([])).toEqual({ required: 0, configured: 0 })
  })

  it('formats uptime', () => {
    expect(formatDuration(0)).toBe('0m 0s')
    expect(formatDuration(65.9)).toBe('1m 5s')
    expect(formatDuration(3 * 3600 + 120)).toBe('3h 2m')
    expect(formatDuration(2 * 86_400 + 3600)).toBe('2d 1h 0m')
    expect(formatDuration(-5)).toBe('0m 0s')
  })

  it('counts and orders issues: errors first, backend order kept', () => {
    const issues = [
      { setting: 'A', severity: 'warning' as const, message: 'a' },
      { setting: 'B', severity: 'error' as const, message: 'b' },
      { setting: 'C', severity: 'warning' as const, message: 'c' },
      { setting: 'D', severity: 'error' as const, message: 'd' },
    ]
    expect(issueCounts(issues)).toEqual({ errors: 2, warnings: 2 })
    expect(sortedIssues(issues).map((i) => i.setting)).toEqual(['B', 'D', 'A', 'C'])
    expect(issues.map((i) => i.setting)).toEqual(['A', 'B', 'C', 'D']) // input untouched
  })
})
