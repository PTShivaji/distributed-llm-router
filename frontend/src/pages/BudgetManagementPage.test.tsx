import { fireEvent, screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { delay, http, HttpResponse } from 'msw'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

import type { BudgetRefusalsResponse, BudgetSpendResponse, BudgetStatusResponse } from '../api/types'
import { addDays, utcToday } from '../lib/budgetDashboard'
import {
  CAPTURED, clone, event, refusalsFixture, spendFixture, statusFixture, statusNotConfiguredFixture, statusUnavailableFixture,
} from '../test/budgetFixtures'
import { renderApp } from '../test/render'
import { errorResponse, server } from '../test/server'

/* Mocked API only (MSW; unhandled requests fail the test). Base bodies are real captures from an isolated mock-mode backend. */
const TOKEN = 'obs-token-budget-tests-SENTINEL-0001'
const SECRET_LOOKING = 'sk-SENTINEL-budget-must-never-render-0001'
const UNAUTHORIZED = () => errorResponse(401, 'unauthorized', 'Missing or invalid observability token.')

type Reply = (n: number, url: URL, auth: string | null) => Response | Promise<Response>
const json = (body: unknown): Reply => () => HttpResponse.json(clone(body))

interface Calls { status: URL[]; spend: URL[]; refusals: URL[]; auth: (string | null)[]; methods: string[] }

function serve({ status = json(statusFixture()), spend = json(spendFixture()), refusals = json(refusalsFixture()) }:
  { status?: Reply; spend?: Reply; refusals?: Reply } = {}): Calls {
  const calls: Calls = { status: [], spend: [], refusals: [], auth: [], methods: [] }
  const route = (name: 'status' | 'spend' | 'refusals', reply: Reply) =>
    http.all(`/api/v1/budget/${name}`, ({ request }) => {
      const url = new URL(request.url)
      calls[name].push(url)
      calls.auth.push(request.headers.get('authorization'))
      calls.methods.push(request.method)
      return reply(calls[name].length, url, request.headers.get('authorization'))
    })
  server.use(route('status', status), route('spend', spend), route('refusals', refusals))
  return calls
}

async function openPage(path = '/budget') {
  const user = userEvent.setup()
  const utils = renderApp(path)
  await screen.findByRole('heading', { level: 1, name: 'Budget Management' })
  return { user, ...utils }
}

const region = (name: string) => screen.findByRole('region', { name })
const kpi = (label: string) => screen.getByTestId(`kpi-${label}`)
const hint = (label: string) => screen.getByRole('group', { name: label })

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
  expect(text).not.toContain(SECRET_LOOKING)
  vi.restoreAllMocks()
})

const SECRET_SHAPE = /(?<![A-Za-z0-9-])(sk-[A-Za-z0-9_-]{16,}|gsk_[A-Za-z0-9]{16,}|AIza[0-9A-Za-z_-]{20,})|Bearer\s/
function expectNoSecretsInDom() {
  const html = document.body.innerHTML
  expect(html).not.toContain(TOKEN)
  expect(html).not.toContain(SECRET_LOOKING)
  expect(html).not.toMatch(SECRET_SHAPE)
}

describe('Budget: overview', () => {
  it('renders every section from the validated responses, one GET request each, no token in URLs', async () => {
    const calls = serve()
    await openPage()
    for (const name of ['Budget overview', 'Daily spending', 'Spending breakdown', 'Budget refusals',
      'Embedding costs and process counters', 'Budget limitations and information']) {
      expect(await region(name)).toBeInTheDocument()
    }
    await waitFor(() => expect(screen.getByTestId('refresh-status')).toHaveTextContent(/^Updated /))
    expect([calls.status.length, calls.spend.length, calls.refusals.length]).toEqual([1, 1, 1])
    expect(new Set(calls.methods)).toEqual(new Set(['GET']))
    for (const u of [...calls.status, ...calls.spend, ...calls.refusals]) {
      expect(u.href).not.toMatch(/token|key|auth/i)
    }
  })

  it('shows limit, counted spend, remaining, utilization, enforcement, period and monthly "Not supported"', async () => {
    serve()
    await openPage()
    await region('Budget overview')
    await waitFor(() => expect(kpi('Daily limit')).toHaveTextContent('$5.00'))
    expect(kpi('Counted today')).toHaveTextContent('$0.00') // a real zero reported by the backend
    expect(kpi('Remaining today')).toHaveTextContent('$5.00')
    expect(kpi('Utilization')).toHaveTextContent('0.0%')
    expect(kpi('Daily enforcement')).toHaveTextContent('Configured · inactive')
    expect(kpi('Monthly limit')).toHaveTextContent('Not supported')
    expect(hint('Monthly limit')).toHaveTextContent('daily limits only')
    expect(screen.getByRole('meter', { name: 'Daily budget utilization' })).toHaveAttribute('aria-valuenow', '0')
    expect(screen.getByTestId('fact-Budget period')).toHaveTextContent('2026-11-11 · 2026-11-11 00:00 UTC → 2026-11-12 00:00 UTC')
    expect(screen.getByTestId('fact-Daily limit per client')).toHaveTextContent('$1.00')
    expect(screen.getByTestId('fact-Daily tokens per client')).toHaveTextContent('Not configured')
    expect(screen.getByTestId('fact-Counter store')).toHaveTextContent('In-memory · this API process only')
    expect(screen.getByTestId('fact-Provider mode')).toHaveTextContent('Mock')
    expect(screen.getByTestId('fact-Monthly enforcement')).toHaveTextContent('Not supported')
    // mock calls do not count: the backend's own statement is shown
    expect(screen.getByTestId('applies-to')).toHaveTextContent('mock calls never reserve or count spend')
  })

  it('unknown values are labelled "Not available" with the backend reason, never $0.00', async () => {
    serve({ status: json(statusUnavailableFixture()) })
    await openPage()
    await waitFor(() => expect(kpi('Counted today')).toHaveTextContent('Not available'))
    expect(kpi('Remaining today')).toHaveTextContent('Not available')
    expect(kpi('Utilization')).toHaveTextContent('Not available')
    expect(hint('Counted today')).toHaveTextContent('the budget store (Redis) is unreachable')
    expect(hint('Utilization')).toHaveTextContent("Today's spend is unknown")
    for (const label of ['Counted today', 'Remaining today', 'Utilization']) expect(kpi(label)).not.toHaveTextContent('$0')
    expect(screen.queryByRole('meter')).not.toBeInTheDocument()
  })

  it('no limits configured: "Not configured", no invented limit or remaining amount', async () => {
    serve({ status: json(statusNotConfiguredFixture()) })
    await openPage()
    await waitFor(() => expect(kpi('Daily limit')).toHaveTextContent('Not configured'))
    expect(kpi('Remaining today')).toHaveTextContent('Not configured')
    expect(kpi('Daily enforcement')).toHaveTextContent('Not configured')
    expect(hint('Utilization')).toHaveTextContent('No global daily limit is configured')
    expect(screen.getByTestId('fact-Max cost per request')).toHaveTextContent('Not configured')
    expect(screen.getByTestId('fact-Counter store')).toHaveTextContent('None')
  })

  it('active enforcement with counted spend shows the utilization meter', async () => {
    const s = statusFixture()
    s.enforcement = { ...s.enforcement, currently_applicable: true, mock_mode: false, real_provider_calls_enabled: true }
    s.today = { ...s.today, counted_usd: 4, remaining_usd: 1 }
    serve({ status: json(s) })
    await openPage()
    await waitFor(() => expect(kpi('Utilization')).toHaveTextContent('80.0%'))
    expect(kpi('Daily enforcement')).toHaveTextContent('Active')
    expect(screen.getByRole('meter', { name: 'Daily budget utilization' })).toHaveAttribute('aria-valuetext', '80.0%')
  })
})

describe('Budget: daily spending and breakdown', () => {
  it('chart, totals and a table of days with requests; simulated and estimated costs are kept apart', async () => {
    serve()
    await openPage()
    const spend = await region('Daily spending')
    await waitFor(() => expect(kpi('Requests')).toHaveTextContent('6'))
    expect(within(spend).getByRole('figure', { name: 'Cost per day (UTC)' })).toBeInTheDocument()
    expect(within(spend).getByText(/never added together/)).toBeInTheDocument()
    expect(kpi('Total tokens')).toHaveTextContent('260')
    expect(kpi('Estimated cost (real calls)')).toHaveTextContent('$0.0500')
    expect(hint('Estimated cost (real calls)')).toHaveTextContent('estimate, not a provider invoice')
    expect(kpi('Simulated cost (mock calls)')).toHaveTextContent('$0.0050')
    expect(hint('Simulated cost (mock calls)')).toHaveTextContent('no money spent')
    expect(within(spend).getByText(/Mixed: simulated mock cost and estimated real cost/)).toBeInTheDocument()
    const rows = within(within(spend).getByRole('table', { name: 'Daily spending (days with requests)' })).getAllByRole('row')
    expect(rows.slice(1).map((r) => within(r).getByRole('rowheader').textContent)).toEqual(['2026-11-01', '2026-11-02'])
    expect(within(spend).getByText(/days with persisted requests \(2 of 3\)/)).toBeInTheDocument()
  })

  it('placeholder pricing is flagged; mock-only history is labelled simulated, never as a charge', async () => {
    const s = spendFixture()
    s.days = s.days.map((d) => ({ ...d, requests: d.mock_requests, successful_requests: Math.min(d.successful_requests, d.mock_requests),
      real_requests: 0, real_estimated_cost_usd: 0, cost_unknown_requests: 0 }))
    s.totals = { ...s.totals, requests: 3, successful_requests: 3, real_requests: 0, real_estimated_cost_usd: 0, cost_unknown_requests: 0 }
    serve({ spend: json(s) })
    await openPage()
    const spend = await region('Daily spending')
    expect(await within(spend).findByText('Simulated cost (mock mode, placeholder prices; no money spent)')).toBeInTheDocument()
    expect(within(spend).getByTestId('placeholder-pricing')).toHaveTextContent('2 of 3 requests were costed with placeholder prices')
    expect(kpi('Placeholder-priced requests')).toHaveTextContent('2')
    // UI-authored cost labels never call an estimate a charge (a backend note in Limitations may say what a provider "may have billed")
    for (const name of ['Budget overview', 'Daily spending', 'Spending breakdown']) {
      expect(screen.getByRole('region', { name }).textContent).not.toMatch(/\b(billed|charged|actual cost|invoice total)\b/i)
    }
  })

  it('breakdown by provider and model shows only returned groups, with a truncation notice', async () => {
    const s = spendFixture()
    s.by_model_truncated = true
    serve({ spend: json(s) })
    await openPage()
    const breakdown = await region('Spending breakdown')
    const providers = within(breakdown).getByRole('table', { name: 'Spending by provider' })
    expect(within(providers).getAllByRole('rowheader').map((c) => c.textContent)).toEqual(['openai', 'mock', 'anthropic'])
    const openai = within(providers).getByRole('rowheader', { name: 'openai' }).closest('tr')!
    expect(openai).toHaveTextContent('$0.0200')
    const models = within(breakdown).getByRole('table', { name: 'Spending by model' })
    expect(within(models).getAllByRole('rowheader')).toHaveLength(3)
    expect(within(breakdown).getByTestId('truncated-model')).toBeInTheDocument()
    expect(within(breakdown).queryByTestId('truncated-provider')).not.toBeInTheDocument()
  })

  it('empty history: explicit empty states, no chart', async () => {
    serve({ spend: json(CAPTURED.spend), refusals: json(CAPTURED.refusals) })
    await openPage()
    const spend = await region('Daily spending')
    expect(await within(spend).findByText('No persisted requests in this range')).toBeInTheDocument()
    expect(within(spend).queryByRole('figure')).not.toBeInTheDocument()
    expect(within(await region('Spending breakdown')).getByText('No persisted requests in this range.')).toBeInTheDocument()
    expect(within(await region('Budget refusals')).getByText('No budget refusals in this range')).toBeInTheDocument()
    expect(kpi('Refused attempts')).toHaveTextContent('0')
  })
})

describe('Budget: refusals and pagination', () => {
  it('lists refusals with UTC time, reason, provider/model and outcome; no ids or prompts', async () => {
    serve()
    await openPage()
    const section = await region('Budget refusals')
    await waitFor(() => expect(kpi('Refused attempts')).toHaveTextContent('3'))
    expect(kpi('Requests with a refusal')).toHaveTextContent('2')
    const table = within(section).getByRole('table', { name: 'Refusal history' })
    const rows = within(table).getAllByRole('row').slice(1)
    expect(rows).toHaveLength(2)
    expect(rows[0]).toHaveTextContent('2026-11-03 10:00 UTC')
    expect(rows[0]).toHaveTextContent('Global daily budget')
    expect(rows[0]).toHaveTextContent('openai')
    expect(rows[0]).toHaveTextContent('gpt-x')
    expect(rows[0]).toHaveTextContent('Request refused')
    expect(rows[1]).toHaveTextContent('Served by another model')
    expect(within(section).getByLabelText('By reason')).toHaveTextContent('Per-request cost limit')
    expect(section).not.toHaveTextContent(/request[_ ]id|client[_ ]id|prompt/i)
  })

  it('missing provider/model are labelled, not blank', async () => {
    const r = refusalsFixture()
    r.events = [event({ provider: null, model: null, mock_mode: true })]
    serve({ refusals: json(r) })
    await openPage()
    const table = await screen.findByRole('table', { name: 'Refusal history' })
    expect(within(table).getAllByText('Not recorded')).toHaveLength(2)
    expect(within(table).getByText('Mock')).toBeInTheDocument()
  })

  it('Next / Previous request pages with the API parameters; controls reflect has_more', async () => {
    const page2 = refusalsFixture()
    page2.events = [event({ time: '2026-11-02T08:30:00Z', reason: 'client_daily_budget', category: 'client_daily' })]
    page2.pagination = { page: 2, page_size: 2, total: 3, has_more: false }
    const calls = serve({ refusals: (_n, url) => HttpResponse.json(url.searchParams.get('page') === '2' ? clone(page2) : refusalsFixture()) })
    const { user } = await openPage()
    const section = await region('Budget refusals')
    const nav = await within(section).findByRole('navigation', { name: 'Refusal pages' })
    expect(within(nav).getByTestId('page-status')).toHaveTextContent('Page 1 of 2 · 1–2 of 3')
    expect(within(nav).getByRole('button', { name: 'Previous' })).toBeDisabled()
    await user.click(within(nav).getByRole('button', { name: 'Next' }))
    await waitFor(() => expect(within(nav).getByTestId('page-status')).toHaveTextContent('Page 2 of 2 · 3–3 of 3'))
    expect(within(section).getByText('2026-11-02 08:30 UTC')).toBeInTheDocument()
    expect(within(nav).getByRole('button', { name: 'Next' })).toBeDisabled()
    expect(calls.refusals.at(-1)!.searchParams.get('page')).toBe('2')
    expect(calls.refusals.at(-1)!.searchParams.get('page_size')).toBe('20')
    expect(calls.spend).toHaveLength(1) // paging does not reload spend
    await user.click(within(nav).getByRole('button', { name: 'Previous' }))
    await waitFor(() => expect(within(nav).getByTestId('page-status')).toHaveTextContent('Page 1 of 2'))
    expect(calls.refusals.at(-1)!.searchParams.get('page')).toBe('1')
  })

  it('a failed page load shows the error (not the previous page as if current)', async () => {
    serve({ refusals: (_n, url) => (url.searchParams.get('page') === '2' ? errorResponse(503, 'database_unavailable', 'Database query failed.')
      : HttpResponse.json(refusalsFixture())) })
    const { user } = await openPage()
    const section = await region('Budget refusals')
    await user.click(await within(section).findByRole('button', { name: 'Next' }))
    expect(await within(section).findByText('Database query failed.')).toBeInTheDocument()
    expect(within(section).queryByRole('table', { name: 'Refusal history' })).not.toBeInTheDocument()
  })
})

describe('Budget: date range', () => {
  it('defaults to the last 30 UTC days; presets send both dates and reset paging', async () => {
    const calls = serve()
    const { user, router } = await openPage()
    await waitFor(() => expect(calls.spend).toHaveLength(1))
    const today = utcToday(Date.now())
    expect(calls.spend[0].searchParams.get('end_date')).toBe(today)
    expect(calls.spend[0].searchParams.get('start_date')).toBe(addDays(today, -29))
    expect(calls.refusals[0].searchParams.get('start_date')).toBe(addDays(today, -29))
    expect(calls.refusals[0].searchParams.get('page')).toBe('1')
    const nav = await screen.findByRole('navigation', { name: 'Refusal pages' })
    await user.click(within(nav).getByRole('button', { name: 'Next' }))
    await waitFor(() => expect(calls.refusals.at(-1)!.searchParams.get('page')).toBe('2'))

    await user.click(screen.getByRole('radio', { name: 'Last 7 days' }))
    await waitFor(() => expect(calls.spend).toHaveLength(2))
    expect(calls.spend[1].searchParams.get('start_date')).toBe(addDays(today, -6))
    await waitFor(() => expect(calls.refusals.at(-1)!.searchParams.get('start_date')).toBe(addDays(today, -6)))
    expect(calls.refusals.at(-1)!.searchParams.get('page')).toBe('1')
    expect(router.state.location.search).toBe('?range=7d')
    expect(screen.getByRole('radio', { name: 'Last 7 days' })).toHaveAttribute('aria-checked', 'true')
    expect(calls.status).toHaveLength(1) // the status is not ranged
  })

  it('custom range: invalid input is explained and never sent; a valid one is applied and kept in the URL', async () => {
    const calls = serve()
    const { user, router } = await openPage()
    await waitFor(() => expect(calls.spend).toHaveLength(1))
    await user.click(screen.getByRole('radio', { name: 'Custom' }))
    const form = screen.getByRole('form', { name: 'Custom date range' })
    const start = within(form).getByLabelText('Start day (UTC, inclusive)')
    const end = within(form).getByLabelText('End day (UTC, inclusive)')
    fireEvent.change(start, { target: { value: '2026-11-05' } })
    fireEvent.change(end, { target: { value: '2026-11-01' } })
    await user.click(within(form).getByRole('button', { name: 'Apply range' }))
    expect(within(form).getByRole('alert')).toHaveTextContent('The start date must not be after the end date.')
    fireEvent.change(start, { target: { value: '2026-11-01' } })
    fireEvent.change(end, { target: { value: '2999-01-01' } })
    expect(within(form).getByRole('alert')).toHaveTextContent('must not be in the future')
    expect(calls.spend).toHaveLength(1)
    fireEvent.change(end, { target: { value: '2026-11-03' } })
    await user.click(within(form).getByRole('button', { name: 'Apply range' }))
    await waitFor(() => expect(calls.spend).toHaveLength(2))
    expect(calls.spend[1].searchParams.get('start_date')).toBe('2026-11-01')
    expect(calls.spend[1].searchParams.get('end_date')).toBe('2026-11-03')
    expect(router.state.location.search).toBe('?range=custom&start=2026-11-01&end=2026-11-03')
    expect(await screen.findByTestId('range-window')).toHaveTextContent('2026-11-01 → 2026-11-03 · UTC days, inclusive')
  })

  it('a range in the URL is used on direct load; an invalid one falls back to the default', async () => {
    const calls = serve()
    await openPage('/budget?range=custom&start=2026-10-01&end=2026-10-10')
    await waitFor(() => expect(calls.spend).toHaveLength(1))
    expect(calls.spend[0].searchParams.get('start_date')).toBe('2026-10-01')
    expect(calls.refusals[0].searchParams.get('end_date')).toBe('2026-10-10')
  })

  it('an invalid URL range falls back to the last 30 days', async () => {
    const calls = serve()
    await openPage('/budget?range=custom&start=2026-01-01&end=2026-12-31')
    await waitFor(() => expect(calls.spend).toHaveLength(1))
    expect(calls.spend[0].searchParams.get('start_date')).toBe(addDays(utcToday(Date.now()), -29))
    expect(screen.getByRole('radio', { name: 'Last 30 days' })).toHaveAttribute('aria-checked', 'true')
  })
})

describe('Budget: embedding and limitations', () => {
  it('embedding metrics are labelled process-local, not history', async () => {
    const s = statusFixture()
    s.embedding = { ...s.embedding, provider: 'litellm', paid: true, budgeted: true, process_estimated_cost_usd: 0.0012,
      process_events: [{ labels: { outcome: 'ok', provider: 'litellm' }, value: 4 }] }
    serve({ status: json(s) })
    await openPage()
    const section = await region('Embedding costs and process counters')
    await waitFor(() => expect(within(section).getByTestId('fact-Estimated embedding cost (this process)')).toHaveTextContent('$0.0012'))
    expect(within(section).getByText('Process-local')).toBeInTheDocument()
    expect(within(section).getByTestId('fact-Durable history')).toHaveTextContent('Not persisted')
    expect(within(section).getByText('outcome=ok, provider=litellm')).toBeInTheDocument()
    expect(section).toHaveTextContent('must not be read as daily or monthly history')
  })

  it('process counters disabled: "Not available", never zero', async () => {
    const s = statusFixture()
    s.embedding = { ...s.embedding, process_counters_available: false, process_estimated_cost_usd: null, process_events: null }
    const r = refusalsFixture()
    r.process_counters_available = false
    r.process_chat_refusals = null
    r.process_embedding_refusals = null
    serve({ status: json(s), refusals: json(r) })
    await openPage()
    const section = await region('Embedding costs and process counters')
    await waitFor(() => expect(within(section).getByTestId('fact-Estimated embedding cost (this process)')).toHaveTextContent('Not available'))
    expect(within(section).getByText(/Process counters are not available/)).toBeInTheDocument()
    expect(within(section).getByText(/Process refusal counters are not available/)).toBeInTheDocument()
  })

  it('limitations state daily-only enforcement, mock exclusion, placeholder pricing and the backend notes', async () => {
    serve()
    await openPage()
    const section = await region('Budget limitations and information')
    await waitFor(() => expect(section).toHaveTextContent('2 of 6 requests in the selected range used placeholder prices'))
    expect(section).toHaveTextContent('Monthly budgetNot supported.')
    expect(section).toHaveTextContent('mock calls never reserve or count spend')
    expect(section).toHaveTextContent('Process-local Prometheus counters')
    const notes = within(section).getByTestId('backend-notes')
    expect(notes).toHaveTextContent('Costs are per-request estimates from persisted history, never provider invoices.')
    expect(new Set(within(notes).getAllByRole('listitem').map((li) => li.textContent)).size).toBe(within(notes).getAllByRole('listitem').length)
  })
})

describe('Budget: loading, errors, retry and stale data', () => {
  it('loading: skeletons and "Loading…", never placeholder numbers', async () => {
    const forever = async () => { await delay('infinite'); return HttpResponse.json({}) }
    serve({ status: forever, spend: forever, refusals: forever })
    await openPage()
    expect(await screen.findByLabelText('Daily limit loading')).toBeInTheDocument()
    expect(screen.getByLabelText('Requests loading')).toBeInTheDocument()
    expect(screen.getByLabelText('Refused attempts loading')).toBeInTheDocument()
    expect(screen.getByTestId('refresh-status')).toHaveTextContent('Loading…')
    expect(screen.queryByTestId('kpi-Daily limit')).not.toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Spending breakdown' })).not.toBeInTheDocument()
  })

  it('401 everywhere: exactly one token prompt; the token goes in the Authorization header only', async () => {
    const ok = (auth: string | null) => auth === `Bearer ${TOKEN}`
    const guard = (body: () => unknown): Reply => (_n, _u, auth) => (ok(auth) ? HttpResponse.json(body()) : UNAUTHORIZED())
    const calls = serve({ status: guard(statusFixture), spend: guard(spendFixture), refusals: guard(refusalsFixture) })
    const { user } = await openPage()
    await waitFor(() => expect(screen.getAllByTestId('token-elsewhere')).toHaveLength(2))
    const prompts = screen.getAllByRole('group', { name: 'Observability token required' })
    expect(prompts).toHaveLength(1)
    expect(within(await region('Budget overview')).getByRole('group', { name: 'Observability token required' })).toBe(prompts[0])
    expect(screen.getAllByTestId('token-elsewhere')[0]).toHaveTextContent('Enter it in the Budget overview section above')
    await user.type(within(prompts[0]).getByPlaceholderText('Observability token'), TOKEN)
    await user.click(within(prompts[0]).getByRole('button', { name: 'Use token' }))
    await waitFor(() => expect(kpi('Daily limit')).toHaveTextContent('$5.00'))
    await waitFor(() => expect(kpi('Refused attempts')).toHaveTextContent('3'))
    expect(calls.auth.at(-1)).toBe(`Bearer ${TOKEN}`)
    for (const u of [...calls.status, ...calls.spend, ...calls.refusals]) expect(u.href).not.toContain(TOKEN)
    expectNoSecretsInDom()
    expect(JSON.stringify({ ...window.localStorage })).not.toContain(TOKEN)
    expect(JSON.stringify({ ...window.sessionStorage })).not.toContain(TOKEN)
  })

  it('403 is shown as "Not allowed" with Retry', async () => {
    serve({ status: () => errorResponse(403, 'forbidden', 'Access was refused.') })
    await openPage()
    const section = await region('Budget overview')
    expect(await within(section).findByText('Not allowed')).toBeInTheDocument()
    expect(within(section).getByText('Access was refused.')).toBeInTheDocument()
    expect(within(section).getByRole('button', { name: /retry/i })).toBeInTheDocument()
  })

  it('503: the backend error is shown, then Retry loads the data', async () => {
    const calls = serve({ spend: (n) => (n === 1 ? errorResponse(503, 'database_unavailable', 'Database is disabled or unreachable.')
      : HttpResponse.json(spendFixture())) })
    const { user } = await openPage()
    const section = await region('Daily spending')
    expect(await within(section).findByText('Service unavailable')).toBeInTheDocument()
    expect(within(section).getByText('Database is disabled or unreachable.')).toBeInTheDocument()
    expect(screen.queryByRole('region', { name: 'Spending breakdown' })).not.toBeInTheDocument()
    await waitFor(() => expect(screen.getByTestId('refresh-status')).toHaveTextContent('Some sections failed to load'))
    await user.click(within(section).getByRole('button', { name: /retry/i }))
    expect(await region('Spending breakdown')).toBeInTheDocument()
    expect(calls.spend).toHaveLength(2)
    await waitFor(() => expect(within(section).queryByText('Service unavailable')).not.toBeInTheDocument())
  })

  it('network error: "Backend unreachable", Retry recovers', async () => {
    serve({ refusals: (n) => (n === 1 ? HttpResponse.error() : HttpResponse.json(refusalsFixture())) })
    const { user } = await openPage()
    const section = await region('Budget refusals')
    expect(await within(section).findByText('Backend unreachable')).toBeInTheDocument()
    await user.click(within(section).getByRole('button', { name: /retry/i }))
    expect(await within(section).findByRole('table', { name: 'Refusal history' })).toBeInTheDocument()
  })

  it('malformed bodies are reported and nothing is rendered from them (extra secret-looking field never shown)', async () => {
    serve({
      status: () => HttpResponse.json({ ...statusFixture(), debug_token: SECRET_LOOKING }),
      spend: () => {
        const s = spendFixture() as unknown as Record<string, unknown>
        ;(s.days as unknown[]).pop() // a missing day would look like "no spend": rejected
        return HttpResponse.json(s)
      },
    })
    await openPage()
    const overview = await region('Budget overview')
    expect(await within(overview).findByTestId('malformed')).toHaveTextContent('GET /api/v1/budget/status returned an unexpected body')
    expect(within(overview).queryByTestId('kpi-Daily limit')).not.toBeInTheDocument()
    const spend = await region('Daily spending')
    expect(await within(spend).findByTestId('malformed')).toHaveTextContent('one entry per day')
    expect(within(spend).queryByRole('figure')).not.toBeInTheDocument()
    expectNoSecretsInDom()
  })

  it('a failed refresh keeps the earlier data, clearly marked stale', async () => {
    const down = () => errorResponse(503, 'service_unavailable', 'Down.')
    serve({
      status: (n) => (n === 1 ? HttpResponse.json(statusFixture()) : down()),
      spend: (n) => (n === 1 ? HttpResponse.json(spendFixture()) : down()),
      refusals: (n) => (n === 1 ? HttpResponse.json(refusalsFixture()) : down()),
    })
    const { user } = await openPage()
    await waitFor(() => expect(kpi('Refused attempts')).toHaveTextContent('3'))
    await user.click(screen.getByRole('button', { name: 'Refresh' }))
    await waitFor(() => expect(screen.getAllByTestId('stale-notice')).toHaveLength(3))
    const text = screen.getAllByTestId('stale-notice').map((n) => n.textContent).join(' | ')
    for (const what of ['budget status', 'spending', 'refusals']) expect(text).toContain(`Showing earlier ${what}`)
    expect(kpi('Daily limit')).toHaveTextContent('$5.00')
    expect(screen.getByRole('table', { name: 'Refusal history' })).toBeInTheDocument()
    expect(screen.getByTestId('refresh-status')).toHaveTextContent('Refresh failed · showing earlier data')
  })
})

describe('Budget: security, accessibility and layout', () => {
  it('read-only: no inputs outside the custom range form, only GET requests, no secrets anywhere', async () => {
    const r = refusalsFixture() as BudgetRefusalsResponse
    const calls = serve({ refusals: json(r) })
    await openPage()
    await region('Budget refusals')
    await waitFor(() => expect(kpi('Refused attempts')).toHaveTextContent('3'))
    const page = screen.getByTestId('budget-page')
    expect(within(page).queryByRole('textbox')).not.toBeInTheDocument()
    expect(within(page).queryByRole('checkbox')).not.toBeInTheDocument()
    expect(within(page).getAllByRole('button').map((b) => b.textContent)).toEqual(['Refresh', 'Previous', 'Next'])
    expect(within(page).getAllByRole('radio').map((b) => b.textContent)).toEqual(['Last 7 days', 'Last 30 days', 'Last 90 days', 'Custom'])
    expect(new Set(calls.methods)).toEqual(new Set(['GET']))
    expect(calls.auth.every((a) => a === null)).toBe(true) // no token configured: none invented
    expectNoSecretsInDom()
  })

  it('accessible structure: one h1, labelled regions, radiogroup, meter, labelled tables and responsive grids', async () => {
    serve()
    await openPage()
    await region('Budget refusals')
    await waitFor(() => expect(kpi('Requests')).toHaveTextContent('6'))
    expect(screen.getAllByRole('heading', { level: 1 })).toHaveLength(1)
    expect(screen.getByRole('radiogroup', { name: 'Date range' })).toBeInTheDocument()
    expect(screen.getByRole('meter', { name: 'Daily budget utilization' })).toBeInTheDocument()
    for (const name of ['Daily spending (days with requests)', 'Spending by provider', 'Spending by model', 'Refusal history']) {
      const table = screen.getByRole('table', { name })
      expect(within(table).getAllByRole('columnheader').length).toBeGreaterThan(1)
      expect(table.parentElement!.className).toContain('overflow-x-auto') // wide tables scroll sideways on phones, not the page
    }
    const kpiGrid = screen.getByRole('group', { name: 'Daily limit' }).parentElement!
    expect(kpiGrid.className).toContain('grid-cols-2')
    expect(kpiGrid.className).toContain('md:grid-cols-3')
    expect(screen.getByTestId('budget-breakdown').querySelector('.xl\\:grid-cols-2')).not.toBeNull()
    expect(screen.getByTestId('budget-page').querySelector('[class*="overflow-y"]')).toBeNull() // no nested vertical scrolling
  })
})

// Type-only guard: the fixtures used above match the generated API types.
const _typed: [BudgetStatusResponse, BudgetSpendResponse] = [statusFixture(), spendFixture()]
void _typed
