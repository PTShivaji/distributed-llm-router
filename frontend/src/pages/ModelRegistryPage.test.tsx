import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { delay, http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'

import type { ModelsResponse } from '../api/types'
import { renderApp } from '../test/render'
import { errorResponse, modelFixture, server } from '../test/server'

/* A registry with every distinction the page must make (test data only). */
const REGISTRY: ModelsResponse = {
  mock_mode: true, routing_strategy: 'priority', health_backend: 'redis',
  models: [
    modelFixture('mock-fast', { fallbacks: ['mock-balanced'], priority: 10 }),
    modelFixture('mock-balanced', { tier: 'MEDIUM', fallbacks: ['mock-legacy', 'ghost-model'], priority: 10 }),
    modelFixture('gpt-x', {
      provider: 'openai', provider_model: 'gpt-x-2026-01', tier: 'COMPLEX', capabilities: ['chat', 'reasoning'],
      input_cost_per_million: 1.25, output_cost_per_million: 10, pricing_is_placeholder: false, max_context_tokens: 400_000,
      max_output_tokens: 32_000, timeout_seconds: 90, priority: 5, description: 'Large reasoning model.',
      health: { state: 'unhealthy', consecutive_failures: 5, total_failures: 7, total_successes: 42, last_error_kind: 'timeout' },
    }),
    modelFixture('claude-y', { provider: 'anthropic', provider_model: 'claude-y-latest', tier: 'MEDIUM', capabilities: [], priority: 20 }),
    modelFixture('mock-legacy', {
      enabled: false, priority: 1,
      health: { state: 'degraded', consecutive_failures: 2, total_failures: 2, total_successes: 0, last_error_kind: 'server_error' },
    }),
  ],
}

function serveRegistry(body: ModelsResponse = REGISTRY) {
  const calls: string[] = []
  server.use(http.get('/api/v1/models', ({ request }) => {
    calls.push(new URL(request.url).pathname)
    return HttpResponse.json(body)
  }))
  return calls
}

const table = () => screen.findByRole('table', { name: 'Registered models' })
const row = (id: string) => screen.getByTestId(`model-row-${id}`)
const visibleIds = async () => within(await table()).getAllByRole('rowheader').map((th) => th.textContent)
const kpi = (label: string) => screen.findByTestId(`kpi-${label}`)

afterEach(() => vi.restoreAllMocks())

describe('Model Registry: loading and counts', () => {
  it('shows skeletons (no numbers) while loading, then the real counts', async () => {
    server.use(http.get('/api/v1/models', async () => {
      await delay(50)
      return HttpResponse.json(REGISTRY)
    }))
    renderApp('/models')
    expect(await screen.findByRole('heading', { level: 1, name: 'Model Registry' })).toBeInTheDocument()
    expect(screen.getByLabelText('Registered models loading')).toBeInTheDocument()
    expect(screen.queryByTestId('kpi-Registered models')).not.toBeInTheDocument()
    expect(await kpi('Registered models')).toHaveTextContent('5')
    expect(await kpi('Enabled')).toHaveTextContent('4')
    expect(await kpi('Disabled')).toHaveTextContent('1')
    expect(await kpi('Providers')).toHaveTextContent('3')
    expect(screen.getByText('anthropic, mock, openai')).toBeInTheDocument()
    expect(screen.getByText('4 with placeholder pricing')).toBeInTheDocument()
    expect(screen.getByText(/^Updated /)).toBeInTheDocument()
    expect(screen.getByText('5 of 5 shown · select a model for its full registry entry')).toBeInTheDocument()
  })

  it('calls only GET /api/v1/models, once, and does not poll', async () => {
    const calls = serveRegistry()
    renderApp('/models')
    await table()
    vi.useFakeTimers()
    try {
      vi.advanceTimersByTime(10 * 60_000) // any interval/timeout-based polling would fire here
    } finally {
      vi.useRealTimers()
    }
    await new Promise((r) => setTimeout(r, 50)) // let any request it started reach the mock server
    expect(calls).toEqual(['/api/v1/models'])
  })

  it('reloads on Refresh', async () => {
    const user = userEvent.setup()
    const calls = serveRegistry()
    renderApp('/models')
    await table()
    await user.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(calls).toHaveLength(2))
  })

  it('cancels the in-flight request when navigating away', async () => {
    let signal: AbortSignal | null = null
    server.use(http.get('/api/v1/models', async ({ request }) => {
      signal = request.signal
      await delay('infinite')
      return HttpResponse.json(REGISTRY)
    }))
    const { router } = renderApp('/models')
    await waitFor(() => expect(signal).not.toBeNull())
    expect(signal!.aborted).toBe(false)
    await router.navigate('/settings')
    await waitFor(() => expect(signal!.aborted).toBe(true))
  })

  it('shows the routing context reported by the backend', async () => {
    serveRegistry()
    renderApp('/models')
    await table()
    expect(screen.getAllByText('Mock mode').length).toBeGreaterThan(0)
    expect(screen.getByText(/Lowest priority number is preferred within a tier/)).toBeInTheDocument()
    expect(screen.getByText(/not a live provider check/)).toBeInTheDocument()
    expect(screen.getByText(/shared via Redis/)).toBeInTheDocument()
  })
})

describe('Model Registry: table', () => {
  it('renders the real API fields for each model, in tier/priority order', async () => {
    serveRegistry()
    renderApp('/models')
    expect(await visibleIds()).toEqual(['mock-legacy', 'mock-fast', 'mock-balanced', 'claude-y', 'gpt-x'])
    const gpt = within(row('gpt-x'))
    for (const text of ['openai', 'gpt-x-2026-01', 'COMPLEX', 'Enabled', 'chat', 'reasoning', '$1.25', '$10.00', 'Configured',
      '400,000 / 32,000', '90 s', '5', 'unhealthy']) {
      expect(gpt.getByText(text)).toBeInTheDocument()
    }
    expect(gpt.getByText('none')).toBeInTheDocument() // no fallbacks configured
  })

  it('marks disabled and unregistered fallbacks as skipped', async () => {
    serveRegistry()
    renderApp('/models')
    await table()
    const balanced = within(row('mock-balanced'))
    expect(balanced.getByText('mock-legacy')).toBeInTheDocument()
    expect(balanced.getByText('(disabled, skipped)')).toBeInTheDocument()
    expect(balanced.getByText('ghost-model')).toBeInTheDocument()
    expect(balanced.getByText('(not registered, skipped)')).toBeInTheDocument()
    expect(within(row('mock-fast')).queryByText(/skipped/)).not.toBeInTheDocument()
  })

  it('never shows a disabled model as available', async () => {
    serveRegistry()
    renderApp('/models')
    await table()
    const legacy = within(row('mock-legacy'))
    expect(legacy.getByText('Disabled')).toBeInTheDocument()
    expect(legacy.getByText('Not routed')).toBeInTheDocument()
    expect(legacy.queryByText('Enabled')).not.toBeInTheDocument()
    expect(legacy.queryByText('healthy')).not.toBeInTheDocument()
    expect(legacy.queryByText('degraded')).not.toBeInTheDocument() // tracked state is not presented as routable health
  })

  it('labels placeholder pricing on exactly the placeholder models', async () => {
    serveRegistry()
    renderApp('/models')
    await table()
    for (const id of ['mock-fast', 'mock-balanced', 'claude-y', 'mock-legacy']) {
      expect(within(row(id)).getByText('Placeholder pricing')).toBeInTheDocument()
    }
    expect(within(row('gpt-x')).queryByText('Placeholder pricing')).not.toBeInTheDocument()
  })

  it('does not fabricate data: empty capabilities stay empty, prices are exact, only API models are listed', async () => {
    serveRegistry()
    renderApp('/models')
    expect((await visibleIds()).sort()).toEqual(REGISTRY.models.map((m) => m.id).sort())
    expect(within(row('claude-y')).getByText('none declared')).toBeInTheDocument()
    expect(within(row('claude-y')).queryByText('chat')).not.toBeInTheDocument()
    expect(within(row('mock-fast')).getByText('$0.10')).toBeInTheDocument()
    expect(within(row('mock-fast')).getByText('$0.40')).toBeInTheDocument()
    expect(screen.queryByText('Demo data')).not.toBeInTheDocument()
  })

  it('renders a compact list for phones with the same models', async () => {
    serveRegistry()
    renderApp('/models')
    await table()
    const compact = screen.getByRole('list', { name: 'Registered models (compact)' })
    expect(within(compact).getAllByRole('button').map((b) => b.getAttribute('aria-label')))
      .toEqual(['mock-legacy', 'mock-fast', 'mock-balanced', 'claude-y', 'gpt-x'].map((id) => `View details for ${id}`))
  })
})

describe('Model Registry: filters', () => {
  it('filters by provider', async () => {
    const user = userEvent.setup()
    serveRegistry()
    const { router } = renderApp('/models')
    await table()
    const provider = screen.getByLabelText('Provider')
    expect(within(provider).getAllByRole('option').map((o) => o.textContent)).toEqual(['All providers', 'anthropic', 'mock', 'openai'])
    await user.selectOptions(provider, 'openai')
    expect(await visibleIds()).toEqual(['gpt-x'])
    expect(screen.getByText(/^1 of 5 shown/)).toBeInTheDocument()
    expect(router.state.location.search).toBe('?provider=openai')
    await user.selectOptions(provider, 'mock')
    expect(await visibleIds()).toEqual(['mock-legacy', 'mock-fast', 'mock-balanced'])
  })

  it('searches by model id, provider and provider model', async () => {
    const user = userEvent.setup()
    serveRegistry()
    renderApp('/models')
    await table()
    const search = screen.getByRole('searchbox')
    // typing is covered once; the remaining queries are pasted (one input event each) to keep the test fast under load
    await user.type(search, 'CLAUDE')
    expect(await visibleIds()).toEqual(['claude-y'])
    await user.clear(search)
    await user.click(search)
    await user.paste('anthropic')
    expect(await visibleIds()).toEqual(['claude-y'])
    await user.clear(search)
    await user.click(search)
    await user.paste('gpt-x-2026')
    expect(await visibleIds()).toEqual(['gpt-x'])
    await user.clear(search)
    await user.click(search)
    await user.paste('no-such-model')
    expect(screen.getByText('No models match these filters')).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    await user.click(screen.getAllByRole('button', { name: 'Clear filters' })[1])
    expect(await visibleIds()).toHaveLength(5)
    expect(search).toHaveValue('')
  })

  it('filters by enabled/disabled status', async () => {
    const user = userEvent.setup()
    serveRegistry()
    renderApp('/models')
    await table()
    await user.selectOptions(screen.getByLabelText('Status'), 'disabled')
    expect(await visibleIds()).toEqual(['mock-legacy'])
    await user.selectOptions(screen.getByLabelText('Status'), 'enabled')
    expect(await visibleIds()).toEqual(['mock-fast', 'mock-balanced', 'claude-y', 'gpt-x'])
  })

  it('filters by tier, combined with other filters, and restores filters from the URL', async () => {
    const user = userEvent.setup()
    serveRegistry()
    renderApp('/models?tier=MEDIUM&provider=mock')
    expect(await visibleIds()).toEqual(['mock-balanced'])
    expect(within(screen.getByLabelText('Tier')).getAllByRole('option').map((o) => o.textContent))
      .toEqual(['All tiers', 'SIMPLE', 'MEDIUM', 'COMPLEX'])
    await user.selectOptions(screen.getByLabelText('Provider'), '')
    expect(await visibleIds()).toEqual(['mock-balanced', 'claude-y'])
  })
})

describe('Model Registry: detail panel', () => {
  it('opens the full registry entry for the selected model and closes with Escape', async () => {
    const user = userEvent.setup()
    serveRegistry()
    const { router } = renderApp('/models')
    await table()
    await user.click(within(row('gpt-x')).getByRole('button', { name: 'View details for gpt-x' }))
    const dialog = await screen.findByRole('dialog', { name: 'gpt-x' })
    const d = within(dialog)
    expect(router.state.location.search).toBe('?model=gpt-x')
    expect(d.getByText('Large reasoning model.')).toBeInTheDocument()
    expect(d.getByText('openai · COMPLEX tier')).toBeInTheDocument()
    expect(d.getByText('gpt-x-2026-01')).toBeInTheDocument()
    expect(d.getByText('400,000')).toBeInTheDocument()
    expect(d.getByText('32,000')).toBeInTheDocument()
    expect(d.getByText('90 s')).toBeInTheDocument()
    expect(d.getByText('$1.25')).toBeInTheDocument()
    expect(d.getByText('$10.00')).toBeInTheDocument()
    expect(d.getByText('Configured')).toBeInTheDocument()
    expect(d.getByText('reasoning')).toBeInTheDocument()
    expect(d.getByText('timeout')).toBeInTheDocument()
    expect(d.getByText('42')).toBeInTheDocument()
    expect(d.getByText(/Circuit open: skipped by routing/)).toBeInTheDocument()
    expect(d.getByText(/It is not a live provider check/)).toBeInTheDocument()
    expect(d.getByText('None configured.')).toBeInTheDocument()
    expect(d.getByRole('button', { name: 'Close details' })).toHaveFocus()
    await user.keyboard('{Escape}')
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
    expect(router.state.location.search).toBe('')
  })

  it('labels placeholder pricing and disabled status in the panel, and links to fallbacks', async () => {
    const user = userEvent.setup()
    serveRegistry()
    renderApp('/models?model=mock-balanced')
    const dialog = await screen.findByRole('dialog', { name: 'mock-balanced' })
    expect(within(dialog).getByText(/Placeholder pricing: these rates are not verified provider prices/)).toBeInTheDocument()
    expect(within(dialog).getByText('(not registered, skipped)')).toBeInTheDocument()
    await user.click(within(dialog).getByRole('button', { name: 'mock-legacy' }))
    const legacy = await screen.findByRole('dialog', { name: 'mock-legacy' })
    expect(within(legacy).getByText(/Disabled: the router never selects this model/)).toBeInTheDocument()
    expect(within(legacy).getByText('Not routed')).toBeInTheDocument()
    expect(within(legacy).getByText(/Tracked state: degraded/)).toBeInTheDocument()
    await user.click(within(legacy).getByRole('button', { name: 'Close details' }))
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('shows "No capabilities declared" rather than inventing any', async () => {
    serveRegistry()
    renderApp('/models?model=claude-y')
    const dialog = await screen.findByRole('dialog', { name: 'claude-y' })
    expect(within(dialog).getByText('No capabilities declared in the registry.')).toBeInTheDocument()
  })

  it('opens nothing for an unknown ?model= id', async () => {
    serveRegistry()
    renderApp('/models?model=not-a-model')
    await table()
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument()
  })

  it('opens from the compact (phone) list too', async () => {
    const user = userEvent.setup()
    serveRegistry()
    renderApp('/models')
    await table()
    const compact = screen.getByRole('list', { name: 'Registered models (compact)' })
    await user.click(within(compact).getByRole('button', { name: 'View details for claude-y' }))
    expect(await screen.findByRole('dialog', { name: 'claude-y' })).toBeInTheDocument()
  })
})

describe('Model Registry: empty and error states', () => {
  it('shows an empty state with zero counts for an empty registry', async () => {
    serveRegistry({ ...REGISTRY, models: [] })
    renderApp('/models')
    expect(await screen.findByText('No models registered')).toBeInTheDocument()
    expect(await kpi('Registered models')).toHaveTextContent('0')
    expect(await kpi('Providers')).toHaveTextContent('0')
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })

  it.each([
    [401, 'unauthorized', 'Authentication required'],
    [403, 'forbidden', 'Not allowed'],
    [503, 'service_unavailable', 'Service unavailable'],
  ])('HTTP %s shows "%s" without any model data', async (status, code, title) => {
    server.use(http.get('/api/v1/models', () => errorResponse(status, code, `backend says ${code}`)))
    renderApp('/models')
    expect(await screen.findByText(title)).toBeInTheDocument()
    expect(screen.getByText(`backend says ${code}`)).toBeInTheDocument()
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
    expect(screen.getByText('Not loaded')).toBeInTheDocument()
    for (const label of ['Registered models', 'Enabled', 'Disabled', 'Providers']) expect(await kpi(label)).toHaveTextContent('—')
  })

  it('HTTP 429 shows the Retry-After hint; Retry loads the registry', async () => {
    const user = userEvent.setup()
    let fail = true
    server.use(http.get('/api/v1/models', () => (fail
      ? errorResponse(429, 'rate_limit_exceeded', 'Slow down', { headers: { 'Retry-After': '7' } })
      : HttpResponse.json(REGISTRY))))
    renderApp('/models')
    expect(await screen.findByText('Rate limit reached')).toBeInTheDocument()
    expect(screen.getByText('Try again in 7s.')).toBeInTheDocument()
    fail = false
    await user.click(screen.getByRole('button', { name: 'Retry' }))
    expect(await visibleIds()).toHaveLength(5)
    expect(screen.queryByText('Rate limit reached')).not.toBeInTheDocument()
  })

  it('keeps earlier data visible (and says so) when a refresh fails', async () => {
    const user = userEvent.setup()
    let fail = false
    server.use(http.get('/api/v1/models', () => (fail ? errorResponse(503, 'service_unavailable', 'down') : HttpResponse.json(REGISTRY))))
    renderApp('/models')
    await table()
    fail = true
    await user.click(screen.getByRole('button', { name: 'Refresh' }))
    expect(await screen.findByText('Service unavailable')).toBeInTheDocument()
    expect(screen.getByText('Refresh failed · showing earlier data')).toBeInTheDocument()
    expect(await visibleIds()).toHaveLength(5)
  })

  it('shows "Backend unreachable" when the request fails at the network level', async () => {
    server.use(http.get('/api/v1/models', () => HttpResponse.error()))
    renderApp('/models')
    expect(await screen.findAllByText('Backend unreachable')).not.toHaveLength(0)
    expect(screen.queryByRole('table')).not.toBeInTheDocument()
  })
})
