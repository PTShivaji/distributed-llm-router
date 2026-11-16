import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { delay, http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { SettingsResponse } from '../api/types'
import { credentialStore } from '../auth/credentials'
import { settingsFixture } from '../test/budgetFixtures'
import { renderApp } from '../test/render'
import { DEPENDENCIES_FIXTURE, errorResponse, server } from '../test/server'

/* Mocked API only: settingsFixture() is a real body captured from an isolated, mock-mode backend. */
const TOKEN = 'obs-token-settings-tests-SENTINEL-0001'
const CHAT_KEY = 'chat-key-settings-tests-SENTINEL-0001'
const UNAUTHORIZED = () => errorResponse(401, 'unauthorized', 'Missing or invalid observability token.')

type Reply = (n: number, auth: string | null) => Response | Promise<Response>

function serve({ settings = () => HttpResponse.json(settingsFixture()), deps = () => HttpResponse.json(DEPENDENCIES_FIXTURE) }:
  { settings?: Reply; deps?: Reply } = {}) {
  const calls = { settings: [] as (string | null)[], deps: [] as (string | null)[], urls: [] as string[] }
  server.use(
    http.get('/api/v1/settings', ({ request }) => {
      calls.settings.push(request.headers.get('authorization'))
      calls.urls.push(request.url)
      return settings(calls.settings.length, request.headers.get('authorization'))
    }),
    http.get('/api/v1/health/dependencies', ({ request }) => {
      calls.deps.push(request.headers.get('authorization'))
      calls.urls.push(request.url)
      return deps(calls.deps.length, request.headers.get('authorization'))
    }),
  )
  return calls
}

function variant(change: (s: SettingsResponse) => void): () => Response {
  return () => {
    const s = settingsFixture()
    change(s)
    return HttpResponse.json(s)
  }
}

async function openPage() {
  const user = userEvent.setup()
  const utils = renderApp('/settings')
  await screen.findByRole('heading', { level: 1, name: 'Settings' })
  return { user, ...utils }
}

const page = () => screen.getByTestId('settings-page')
const region = (name: string) => screen.findByRole('region', { name })

let consoleCalls: unknown[][]
beforeEach(() => {
  consoleCalls = []
  for (const m of ['log', 'info', 'warn', 'error', 'debug'] as const) {
    vi.spyOn(console, m).mockImplementation((...args: unknown[]) => { consoleCalls.push(args) })
  }
})
afterEach(() => {
  const text = JSON.stringify(consoleCalls)
  expect(text).not.toContain(TOKEN)
  expect(text).not.toContain(CHAT_KEY)
  vi.restoreAllMocks()
})

// Key-shaped strings (word boundary: "lucide-flask-conical" is an icon class, not a key) and bearer headers.
const SECRET_SHAPE = /(?<![A-Za-z0-9-])(sk-[A-Za-z0-9_-]{16,}|gsk_[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_-]{20,})|Bearer\s/

function expectNoSecretsInDom() {
  const html = document.body.innerHTML
  for (const secret of [TOKEN, CHAT_KEY]) expect(html).not.toContain(secret)
  expect(html).not.toMatch(SECRET_SHAPE)
}

it('the secret-shape detector catches keys and ignores icon class names (control)', () => {
  for (const key of ['sk-abcdefghijklmnop1234', 'x sk-ant-api03-abcdefghij1234', 'gsk_abcdefghijklmnop1234', 'Bearer abc']) {
    expect(key).toMatch(SECRET_SHAPE)
  }
  expect('lucide lucide-flask-conical h-4 w-4').not.toMatch(SECRET_SHAPE)
})

describe('Settings: successful load', () => {
  it('renders every section from the validated API body, with one request each', async () => {
    const calls = serve()
    await openPage()
    for (const name of ['Runtime information', 'Administrative capabilities', 'Authentication', 'Provider credentials',
      'Production configuration checks', 'Effective configuration']) {
      expect(await region(name)).toBeInTheDocument()
    }
    expect(screen.getByRole('group', { name: 'Environment' })).toHaveTextContent('test')
    expect(screen.getByRole('group', { name: 'Provider mode' })).toHaveTextContent('Mock')
    expect(screen.getByRole('group', { name: 'Configuration checks' })).toHaveTextContent('5 errors')
    const runtime = await region('Runtime information')
    expect(within(runtime).getByText('Distributed LLM Router')).toBeInTheDocument()
    expect(within(runtime).getByText('v0.1.0')).toBeInTheDocument()
    expect(screen.getByTestId('refresh-status')).toHaveTextContent(/^Updated /)
    await waitFor(() => expect(screen.getByTestId('dep-redis')).toBeInTheDocument()) // health section (B)
    expect(screen.getByTestId('dep-database')).toHaveTextContent('Connection check succeeded.')
    expect(calls.settings).toHaveLength(1)
    expect(calls.deps).toHaveLength(1)
  })

  it('is read-only: the only control in the page body is Refresh (plus a navigation link)', async () => {
    serve()
    await openPage()
    await region('Effective configuration')
    expect(within(page()).getAllByRole('button').map((b) => b.textContent)).toEqual(['Refresh'])
    expect(within(page()).queryByRole('textbox')).not.toBeInTheDocument()
    expect(within(page()).queryByRole('checkbox')).not.toBeInTheDocument()
    expect(within(page()).queryByRole('link')).not.toBeInTheDocument() // admin disabled in the fixture: no cache link
  })

  it('links to the existing cache clear action only when admin endpoints are enabled', async () => {
    serve({ settings: variant((s) => { s.auth.admin_endpoints_enabled = true }) })
    await openPage()
    const admin = await region('Administrative capabilities')
    expect(within(admin).getByRole('link', { name: 'Available on Cache Management' })).toHaveAttribute('href', '/cache')
    expect(within(admin).getByTestId('admin-runtime-config')).toHaveTextContent('Not supported')
  })
})

describe('Settings: loading, errors and retry', () => {
  it('shows loading placeholders until the response arrives', async () => {
    serve({ settings: async () => { await delay('infinite'); return HttpResponse.json({}) } })
    await openPage()
    expect(await screen.findByTestId('settings-loading')).toHaveAttribute('aria-busy', 'true')
    expect(screen.getByTestId('settings-kpis-loading')).toBeInTheDocument()
    expect(screen.getByTestId('refresh-status')).toHaveTextContent('Loading…')
    expect(screen.queryByRole('region', { name: 'Runtime information' })).not.toBeInTheDocument()
  })

  it('401: one token prompt; the token goes in the header only and is never shown', async () => {
    const ok = (auth: string | null) => auth === `Bearer ${TOKEN}`
    const calls = serve({
      settings: (_n, auth) => (ok(auth) ? HttpResponse.json(settingsFixture()) : UNAUTHORIZED()),
      deps: (_n, auth) => (ok(auth) ? HttpResponse.json(DEPENDENCIES_FIXTURE) : UNAUTHORIZED()),
    })
    const { user } = await openPage()
    await screen.findByTestId('token-elsewhere')
    const prompts = screen.getAllByRole('group', { name: 'Observability token required' })
    expect(prompts).toHaveLength(1)
    await user.type(within(prompts[0]).getByPlaceholderText('Observability token'), TOKEN)
    await user.click(within(prompts[0]).getByRole('button', { name: 'Use token' }))
    expect(await region('Runtime information')).toBeInTheDocument()
    expect(calls.settings.at(-1)).toBe(`Bearer ${TOKEN}`)
    expect(calls.deps.at(-1)).toBe(`Bearer ${TOKEN}`)
    for (const url of calls.urls) expect(url).not.toContain(TOKEN)
    expect(within(await region('Authentication')).getByTestId('tab-observability')).toHaveTextContent('Entered in this tab')
    expectNoSecretsInDom()
    expect(JSON.stringify({ ...window.localStorage })).not.toContain(TOKEN)
  })

  it('401 on settings only: the prompt is shown in the settings area', async () => {
    serve({ settings: UNAUTHORIZED })
    await openPage()
    const failure = await screen.findByTestId('settings-failure')
    expect(within(failure).getByRole('group', { name: 'Observability token required' })).toBeInTheDocument()
  })

  it('403 is shown as an error with Retry', async () => {
    serve({ settings: () => errorResponse(403, 'forbidden', 'Access was refused.') })
    await openPage()
    const failure = await screen.findByTestId('settings-failure')
    expect(within(failure).getByText('Access was refused.')).toBeInTheDocument()
    expect(within(failure).getByRole('button', { name: /retry/i })).toBeInTheDocument()
  })

  it('503: shows the backend error, then Retry loads the settings', async () => {
    const calls = serve({ settings: (n) => (n === 1 ? errorResponse(503, 'service_unavailable', 'The service is temporarily unavailable.')
      : HttpResponse.json(settingsFixture())) })
    const { user } = await openPage()
    const failure = await screen.findByTestId('settings-failure')
    expect(within(failure).getByText('The service is temporarily unavailable.')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Runtime information' })).not.toBeInTheDocument()
    await user.click(within(failure).getByRole('button', { name: /retry/i }))
    expect(await region('Runtime information')).toBeInTheDocument()
    expect(calls.settings).toHaveLength(2)
    expect(screen.queryByTestId('settings-failure')).not.toBeInTheDocument()
  })

  it('network error: shows the unreachable state, Retry recovers', async () => {
    serve({ settings: (n) => (n === 1 ? HttpResponse.error() : HttpResponse.json(settingsFixture())) })
    const { user } = await openPage()
    const failure = await screen.findByTestId('settings-failure')
    expect(within(failure).getByText(/Cannot reach the backend API/)).toBeInTheDocument()
    await user.click(within(failure).getByRole('button', { name: /retry/i }))
    expect(await region('Provider credentials')).toBeInTheDocument()
  })

  it('malformed body: reported as such, nothing is rendered from it', async () => {
    serve({ settings: () => HttpResponse.json({ ...settingsFixture(), unexpected_field: true }) })
    await openPage()
    const malformed = await screen.findByTestId('malformed')
    expect(malformed).toHaveTextContent('GET /api/v1/settings returned an unexpected body')
    expect(screen.queryByRole('region', { name: 'Runtime information' })).not.toBeInTheDocument()
    expect(screen.queryByTestId('settings-kpis')).not.toBeInTheDocument()
  })

  it('a failed refresh keeps the earlier data, marked stale', async () => {
    serve({ settings: (n) => (n === 1 ? HttpResponse.json(settingsFixture()) : errorResponse(503, 'service_unavailable', 'Down.')) })
    const { user } = await openPage()
    await region('Runtime information')
    await user.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByTestId('stale-notice')).toHaveTextContent('Showing earlier settings')
    expect(screen.getByRole('region', { name: 'Runtime information' })).toBeInTheDocument()
    expect(screen.getByTestId('refresh-status')).toHaveTextContent('Refresh failed · showing earlier data')
  })
})

describe('Settings: values', () => {
  it('null values render as explicit labels, never as 0, $0 or Disabled', async () => {
    serve({ settings: variant((s) => {
      s.rate_limits.per_minute = null
      s.rate_limits.per_hour = null
      s.budget = { ...s.budget, configured: false, daily_usd: null, daily_usd_per_client: null, daily_tokens_per_client: null,
        max_cost_per_request_usd: null }
      s.http.hsts_enabled = false
      s.http.hsts_max_age_seconds = null
      s.cache.embedding_model = null
    }) })
    await openPage()
    const rate = await screen.findByTestId('config-Rate limiting')
    expect(within(rate).getAllByText('No limit')).toHaveLength(2)
    const budget = screen.getByTestId('config-Budget limits')
    expect(within(budget).getAllByText('Not configured')).toHaveLength(4)
    expect(budget).not.toHaveTextContent('$0')
    expect(within(budget).getByText('Not supported (daily limits only)')).toBeInTheDocument()
    expect(within(screen.getByTestId('config-HTTP security')).getByText('Not applicable (HSTS off)')).toBeInTheDocument()
    expect(within(screen.getByTestId('config-Response cache')).getByText('Not applicable (local embedder)')).toBeInTheDocument()
    for (const el of document.querySelectorAll('[data-unset="true"]')) expect(el.textContent).not.toMatch(/^(0|\$0\.00|Disabled)$/)
  })

  it('configured values are shown as reported', async () => {
    serve({ settings: variant((s) => {
      s.budget = { ...s.budget, configured: true, daily_usd: 5, daily_usd_per_client: 1.5, daily_tokens_per_client: 20000,
        max_cost_per_request_usd: 0.25 }
      s.rate_limits.model_limits = { 'mock-advanced': 7 }
      s.http.cors_enabled = true
      s.http.cors_origins_configured = 2
    }) })
    await openPage()
    const budget = await screen.findByTestId('config-Budget limits')
    for (const text of ['$5.00', '$1.50', '20,000', '$0.2500']) expect(budget).toHaveTextContent(text)
    expect(screen.getByTestId('config-Rate limiting')).toHaveTextContent('mock-advanced 7/min')
    expect(screen.getByTestId('config-HTTP security')).toHaveTextContent('2 origins allowed')
  })

  it('provider credentials: configured, not configured and not required, never a key', async () => {
    serve({ settings: variant((s) => {
      for (const p of s.providers) if (p.credential_required) p.credential_configured = p.provider === 'anthropic'
    }) })
    await openPage()
    const providers = await region('Provider credentials')
    const status = (name: string) => within(providers).getByTestId(`provider-${name}`)
    expect(status('anthropic')).toHaveAttribute('data-status', 'configured')
    expect(within(status('anthropic')).getByText('Configured')).toBeInTheDocument()
    expect(status('openai')).toHaveAttribute('data-status', 'not_configured')
    expect(within(status('openai')).getByText('Not configured')).toBeInTheDocument()
    expect(within(status('openai')).getByText('OPENAI_API_KEY')).toBeInTheDocument() // the setting NAME only
    expect(status('mock')).toHaveAttribute('data-status', 'not_required')
    expect(within(status('mock')).getByText('mock-fast')).toBeInTheDocument()
    expect(within(providers).getByText('1 of 5 configured')).toBeInTheDocument()
    expect(within(providers).getByTestId('providers-mock-note')).toBeInTheDocument()
  })

  it('production warnings: errors first, severity shown, informational outside production', async () => {
    serve()
    await openPage()
    const checks = await region('Production configuration checks')
    expect(within(checks).getByText('Informational')).toBeInTheDocument()
    const issues = within(checks).getAllByTestId('config-issue')
    const severities = issues.map((i) => i.getAttribute('data-severity'))
    expect(severities).toEqual([...severities].sort()) // 'error' < 'warning'
    expect(severities.filter((s) => s === 'error')).toHaveLength(5)
    const hsts = issues.find((i) => i.textContent?.includes('HSTS_ENABLED'))!
    expect(hsts).toHaveAttribute('data-severity', 'warning')
    expect(hsts).toHaveTextContent('enable once the API is served over HTTPS')
    expect(within(checks).getByTestId('checks-summary')).toHaveTextContent('5 errors · 3 warnings')
  })

  it('production: checks marked enforced; an empty list says so', async () => {
    serve({ settings: variant((s) => {
      s.app.environment = 'production'
      s.production_checks = { enforced: true, issues: [] }
    }) })
    await openPage()
    const checks = await region('Production configuration checks')
    expect(within(checks).getByText('Enforced')).toBeInTheDocument()
    expect(within(checks).getByTestId('checks-clean')).toHaveTextContent('No configuration issues reported.')
    expect(screen.getByRole('group', { name: 'Configuration checks' })).toHaveTextContent('No issues')
  })

  it('authentication: backend requirements and this tab, booleans only', async () => {
    credentialStore.set('observability', TOKEN)
    credentialStore.set('chat', CHAT_KEY)
    serve({ settings: variant((s) => {
      s.auth = { chat_auth_mode: 'api_key', chat_clients_configured: 2, observability_token_required: true,
        admin_endpoints_enabled: false, client_id_hashing: 'hmac' }
    }) })
    await openPage()
    const auth = await region('Authentication')
    expect(within(auth).getByTestId('auth-chat')).toHaveTextContent('API key required')
    expect(within(auth).getByTestId('auth-chat')).toHaveTextContent('2 clients configured')
    expect(within(auth).getByTestId('auth-observability')).toHaveTextContent('Token required')
    expect(within(auth).getByTestId('auth-admin')).toHaveTextContent('Disabled')
    expect(within(auth).getByTestId('auth-hashing')).toHaveTextContent('HMAC (keyed)')
    expect(within(auth).getByTestId('tab-chat')).toHaveTextContent('Entered in this tab')
    expect(within(auth).getByTestId('tab-storage')).toHaveTextContent('Memory only')
    expectNoSecretsInDom()
  })
})

describe('Settings: accessibility and layout', () => {
  it('uses one h1, labelled regions with h2 titles, and responsive grids without fixed widths', async () => {
    serve()
    await openPage()
    await region('Effective configuration')
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
    for (const r of within(page()).getAllByRole('region')) {
      expect(r).toHaveAccessibleName()
    }
    const config = screen.getByTestId('section-config')
    expect(config.querySelector('.grid')?.className).toMatch(/grid-cols-1 .*md:grid-cols-2/)
    expect(screen.getByTestId('settings-kpis').className).toMatch(/grid-cols-2 .*lg:grid-cols-4/)
    expect(page().innerHTML).not.toMatch(/overflow-y-(auto|scroll)|max-h-/) // no nested scrolling areas
    expect(screen.getByTestId('refresh-status')).toHaveAttribute('aria-live', 'polite')
  })
})
