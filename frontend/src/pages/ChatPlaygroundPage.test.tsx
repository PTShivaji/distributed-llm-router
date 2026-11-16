import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { delay, http, HttpResponse } from 'msw'
import { afterEach, describe, expect, it, vi } from 'vitest'

import { sessionActivity } from '../activity/sessionActivity'
import type { ChatRequest, ChatResponse, ClassifyResponse } from '../api/types'
import { credentialStore } from '../auth/credentials'
import { chatSession } from '../chat/chatSession'
import { renderApp } from '../test/render'
import { errorResponse, MODELS_FIXTURE, server } from '../test/server'

/* ---------- test data (mocked API responses only; no backend or provider is contacted) ---------- */

const CLASSIFICATION: ClassifyResponse = {
  classifier_version: 'v-test', complexity: 'COMPLEX', complexity_score: 0.81, confidence: 0.74,
  explanation: 'Multiple design trade-offs requested.',
  features: { char_count: 120, code_line_count: 0, has_code: false, has_code_block: false, inline_code_count: 0, line_count: 1,
    list_item_count: 0, question_count: 0, sentence_count: 2, word_count: 20 },
  intent: { primary: 'system_design', confidence: 0.66, alternatives: [{ intent: 'analysis', score: 0.2 }] },
  reasoning: { estimated_steps: 4, score: 0.7, signals: [{ name: 'tradeoff_terms', detail: 'compare, trade-offs', weight: 0.3 }] },
  technical: { detected_terms: ['rate limiter'], domains: ['distributed_systems'], is_coding: false, is_technical: true, score: 0.8 },
  thresholds: { simple_max: 0.3, complex_min: 0.6 },
}

function chatResponse(overrides: Partial<ChatResponse> = {}): ChatResponse {
  return {
    answer: 'Paris is the capital of France.', request_id: 'req-test-1', mock_mode: true, latency_ms: 42.5,
    selected_model: 'mock-fast', model_used: 'mock-fast', provider: 'mock', provider_model: 'mock/fast-v1',
    provider_adapter: 'mock', provider_status: 'ok', finish_reason: 'stop',
    classification: { ...CLASSIFICATION, complexity: 'SIMPLE', complexity_score: 0.12, confidence: 0.9,
      intent: { primary: 'question_answering', confidence: 0.8, alternatives: [] } },
    routing: { selected_model: 'mock-fast', requested_tier: 'SIMPLE', routed_tier: 'SIMPLE', strategy: 'priority', override: false,
      reason: 'Query classified as SIMPLE (confidence 0.90). Selected mock-fast.', candidates: ['mock-fast', 'mock-balanced'],
      preferred_capabilities: [] },
    fallback: { used: false, chain: ['mock-fast', 'mock-balanced'], original_model: 'mock-fast', final_model: 'mock-fast', reason: null,
      attempts: [{ attempt_number: 1, model_id: 'mock-fast', provider: 'mock', outcome: 'success', error_kind: null, retryable: null, latency_ms: 40.1 }] },
    usage: { input_tokens: 12, output_tokens: 8, total_tokens: 20, source: 'provider_reported' },
    cost: { currency: 'USD', input_cost: 0.0000012, output_cost: 0.0000032, total_cost: 0.0000044, input_cost_per_million: 0.1,
      output_cost_per_million: 0.4, is_estimate: true, pricing_is_placeholder: true },
    cache: { status: 'miss', stored: true, lookup_ms: 1.2 },
    ...overrides,
  }
}

interface ChatCall { body: ChatRequest; auth: string | null; requestId: string | null; url: string }

function serveChat(respond: (body: ChatRequest) => Response | Promise<Response> = () => HttpResponse.json(chatResponse())) {
  const calls: ChatCall[] = []
  server.use(http.post('/api/v1/chat', async ({ request }) => {
    const body = (await request.json()) as ChatRequest
    calls.push({ body, auth: request.headers.get('authorization'), requestId: request.headers.get('x-request-id'), url: request.url })
    return respond(body)
  }))
  return calls
}

function serveClassify() {
  const calls: unknown[] = []
  server.use(http.post('/api/v1/classify', async ({ request }) => {
    calls.push(await request.json())
    return HttpResponse.json(CLASSIFICATION)
  }))
  return calls
}

async function openChat() {
  const user = userEvent.setup()
  const utils = renderApp('/chat')
  await screen.findByRole('heading', { level: 1, name: 'Chat Playground' })
  // model list loaded (override options present)
  await screen.findByRole('option', { name: /mock-fast · mock/ })
  return { user, ...utils }
}

const prompt = () => screen.getByLabelText('Prompt')
const lastTurn = () => {
  const items = within(screen.getByRole('list', { name: 'Conversation' })).getAllByRole('listitem', { name: /^Interaction sent at/ })
  return within(items[items.length - 1])
}

afterEach(() => vi.restoreAllMocks())

/* ---------------------------------------------------------------------------------------------- */

describe('Chat Playground: empty state and composer', () => {
  it('shows the empty state with example prompts that only fill the input', async () => {
    const calls = serveChat()
    const { user } = await openChat()
    expect(screen.getByText('Start a conversation')).toBeInTheDocument()
    const examples = within(screen.getByRole('list', { name: 'Example prompts' })).getAllByRole('button')
    expect(examples).toHaveLength(4)
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    await user.click(examples[0])
    expect(prompt()).toHaveValue('What is the capital of France?')
    expect(prompt()).toHaveFocus()
    expect(calls).toHaveLength(0)
    expect(screen.getByRole('button', { name: 'Clear conversation' })).toBeDisabled()
  })

  it('submits the prompt once with automatic routing and shows the answer, request id and timestamps', async () => {
    const calls = serveChat()
    const { user } = await openChat()
    await user.type(prompt(), 'What is the capital of France?')
    await user.click(screen.getByRole('button', { name: 'Send' }))
    expect(await screen.findByText('Paris is the capital of France.')).toBeInTheDocument()
    expect(calls).toHaveLength(1)
    expect(calls[0].body).toEqual({ query: 'What is the capital of France?', use_cache: true })
    expect(calls[0].requestId).toMatch(/^ui-[A-Za-z0-9]+$/)
    expect(prompt()).toHaveValue('')
    const turn = lastTurn()
    expect(turn.getByTestId('prompt')).toHaveTextContent('What is the capital of France?')
    expect(turn.getByTestId('request-id')).toHaveTextContent('req-test-1')
    expect(turn.getByText(/^Sent .* automatic routing/)).toBeInTheDocument()
    expect(turn.getByText(/^Answered /)).toBeInTheDocument()
  })

  it('sends on Enter and inserts a new line on Shift+Enter', async () => {
    const calls = serveChat()
    const { user } = await openChat()
    await user.type(prompt(), 'line one{Shift>}{Enter}{/Shift}line two')
    expect(prompt()).toHaveValue('line one\nline two')
    expect(calls).toHaveLength(0)
    await user.keyboard('{Enter}')
    await screen.findByText('Paris is the capital of France.')
    expect(calls.map((c) => c.body.query)).toEqual(['line one\nline two'])
  })

  it('does not send blank or over-long prompts', async () => {
    const calls = serveChat()
    const { user } = await openChat()
    await user.type(prompt(), '   {Enter}')
    expect(calls).toHaveLength(0)
    await user.clear(prompt())
    await user.click(prompt())
    await user.paste('x'.repeat(10_001))
    expect(screen.getByText(/too long to send/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: 'Send' })).toBeDisabled()
    await user.keyboard('{Enter}')
    expect(calls).toHaveLength(0)
  })

  it('shows a loading state and blocks a second send while a request is in flight', async () => {
    let release!: () => void
    const held = new Promise<void>((r) => { release = r })
    const calls = serveChat(async () => {
      await held
      return HttpResponse.json(chatResponse())
    })
    const { user } = await openChat()
    await user.type(prompt(), 'first{Enter}')
    expect(await screen.findByText(/Routing and generating/)).toBeInTheDocument()
    expect(screen.getByRole('button', { name: /Sending/ })).toBeDisabled()
    await user.type(prompt(), 'second{Enter}')
    expect(prompt()).toHaveValue('second') // kept, not sent
    expect(calls).toHaveLength(1)
    release()
    await screen.findByText('Paris is the capital of France.')
    expect(calls).toHaveLength(1)
    expect(screen.queryByText(/Routing and generating/)).not.toBeInTheDocument()
  })

  it('renders code fences as code blocks and never interprets HTML in answers', async () => {
    serveChat(() => HttpResponse.json(chatResponse({ answer: 'Here:\n```python\ndef f():\n    return 1\n```\n<b>not bold</b><script>x()</script>' })))
    const { user } = await openChat()
    await user.type(prompt(), 'code please{Enter}')
    const answer = await screen.findByTestId('answer')
    expect(answer.querySelector('pre code')).toHaveTextContent('def f(): return 1')
    expect(within(answer).getByText('python')).toBeInTheDocument()
    expect(answer.querySelector('b, script')).toBeNull()
    expect(answer).toHaveTextContent('<b>not bold</b><script>x()</script>')
  })

  it('copies the response to the clipboard', async () => {
    serveChat()
    const { user } = await openChat()
    await user.type(prompt(), 'hi{Enter}')
    await screen.findByText('Paris is the capital of France.')
    await user.click(screen.getByRole('button', { name: 'Copy response' }))
    expect(await navigator.clipboard.readText()).toBe('Paris is the capital of France.')
    expect(screen.getByText('Copied')).toBeInTheDocument()
  })

  it('clears the conversation only after confirmation', async () => {
    serveChat()
    const { user } = await openChat()
    await user.type(prompt(), 'hi{Enter}')
    await screen.findByText('Paris is the capital of France.')
    await user.click(screen.getByRole('button', { name: 'Clear conversation' }))
    const dialog = screen.getByRole('dialog', { name: 'Clear conversation?' })
    await user.click(within(dialog).getByRole('button', { name: 'Cancel' }))
    expect(screen.getByText('Paris is the capital of France.')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Clear conversation' }))
    await user.click(within(screen.getByRole('dialog')).getByRole('button', { name: 'Clear conversation' }))
    expect(screen.queryByText('Paris is the capital of France.')).not.toBeInTheDocument()
    expect(screen.getByText('Start a conversation')).toBeInTheDocument()
  })
})

describe('Chat Playground: model selection', () => {
  it('offers automatic routing plus enabled models only; disabled models cannot be selected', async () => {
    await openChat()
    const select = screen.getByLabelText('Model') as HTMLSelectElement
    expect(select).toHaveValue('')
    expect(within(select).getByRole('option', { name: 'Automatic routing (recommended)' })).toBeInTheDocument()
    expect(within(select).getByRole('option', { name: 'mock-fast · mock' })).toBeEnabled()
    expect(within(select).getByRole('option', { name: 'mock-advanced · mock' })).toBeEnabled()
    expect(within(select).getByRole('option', { name: 'mock-legacy · mock (disabled)' })).toBeDisabled()
  })

  it('sends the chosen override, shows its provider, status and placeholder pricing, and can go back to automatic', async () => {
    const calls = serveChat()
    const { user } = await openChat()
    await user.selectOptions(screen.getByLabelText('Model'), 'mock-advanced')
    const info = screen.getByTestId('override-info')
    expect(info).toHaveTextContent('mock-advanced')
    expect(info).toHaveTextContent('Enabled')
    expect(info).toHaveTextContent('mock/mock-advanced · COMPLEX tier')
    expect(info).toHaveTextContent(/Placeholder pricing/)
    await user.type(prompt(), 'one{Enter}')
    await screen.findByText('Paris is the capital of France.')
    expect(calls[0].body).toEqual({ query: 'one', use_cache: true, model: 'mock-advanced' })
    expect(lastTurn().getByText(/override/)).toHaveTextContent('override mock-advanced')
    await user.selectOptions(screen.getByLabelText('Model'), '')
    await user.click(screen.getByRole('checkbox', { name: /Use response cache/ }))
    await user.type(prompt(), 'two{Enter}')
    await waitFor(() => expect(calls).toHaveLength(2))
    expect(calls[1].body).toEqual({ query: 'two', use_cache: false })
  })

  it('falls back to automatic routing only when the model list cannot be loaded', async () => {
    server.use(http.get('/api/v1/models', () => errorResponse(503, 'service_unavailable', 'registry down')))
    const calls = serveChat()
    const user = userEvent.setup()
    renderApp('/chat')
    expect(await screen.findByText(/Model list unavailable/)).toBeInTheDocument()
    expect(within(screen.getByLabelText('Model')).getAllByRole('option')).toHaveLength(1)
    await user.type(prompt(), 'hi{Enter}')
    await screen.findByText('Paris is the capital of France.')
    expect(calls[0].body).not.toHaveProperty('model')
  })
})

describe('Chat Playground: classification preview', () => {
  it('classifies only on request, is labelled as a preview, and is not sent with the chat request', async () => {
    const classify = serveClassify()
    const calls = serveChat()
    const { user } = await openChat()
    await user.type(prompt(), 'Design a distributed rate limiter')
    expect(classify).toHaveLength(0)
    await user.click(screen.getByRole('button', { name: 'Preview classification' }))
    const preview = await screen.findByTestId('classification-preview')
    await within(preview).findByText('COMPLEX')
    expect(classify).toEqual([{ query: 'Design a distributed rate limiter' }])
    expect(preview).toHaveTextContent('system design')
    expect(preview).toHaveTextContent('74.0%')
    expect(preview).toHaveTextContent(/Preview only\. The backend classifies the prompt again/)
    expect(calls).toHaveLength(0) // previewing never sends the chat request
    await user.type(prompt(), ' now')
    expect(within(preview).getByText('The prompt has changed since this preview.')).toBeInTheDocument()
    await user.keyboard('{Enter}')
    await screen.findByText('Paris is the capital of France.')
    expect(screen.queryByTestId('classification-preview')).not.toBeInTheDocument() // closed once its prompt is sent
    expect(calls[0].body).toEqual({ query: 'Design a distributed rate limiter now', use_cache: true })
    expect(classify).toHaveLength(1) // no automatic classification before chat
  })

  it('renders repeated classifier signal names (as the backend can return) without React key errors', async () => {
    const consoleError = vi.spyOn(console, 'error')
    server.use(http.post('/api/v1/classify', () => HttpResponse.json({ ...CLASSIFICATION, reasoning: { ...CLASSIFICATION.reasoning,
      signals: [{ name: 'reasoning_marker', detail: 'why', weight: 0.1 }, { name: 'reasoning_marker', detail: 'because', weight: 0.1 }] } })))
    const { user } = await openChat()
    await user.type(prompt(), 'why and because')
    await user.click(screen.getByRole('button', { name: 'Preview classification' }))
    const signals = await screen.findByRole('list', { name: 'Classifier signals' })
    expect(within(signals).getAllByRole('listitem')).toHaveLength(2)
    expect(JSON.stringify(consoleError.mock.calls)).not.toMatch(/same key/)
  })

  it('shows a classification error without blocking chat', async () => {
    server.use(http.post('/api/v1/classify', () => errorResponse(429, 'rate_limit_exceeded', 'slow', { headers: { 'Retry-After': '3' } })))
    const { user } = await openChat()
    await user.type(prompt(), 'x')
    await user.click(screen.getByRole('button', { name: 'Preview classification' }))
    expect(await within(screen.getByTestId('classification-preview')).findByText('Rate limit reached')).toBeInTheDocument()
    await user.click(screen.getByRole('button', { name: 'Close classification preview' }))
    expect(screen.queryByTestId('classification-preview')).not.toBeInTheDocument()
  })
})

describe('Chat Playground: routing, fallback, usage and cache details', () => {
  it('shows the routing intelligence from the response', async () => {
    serveChat()
    const { user } = await openChat()
    await user.type(prompt(), 'q{Enter}')
    const routing = await screen.findByTestId('routing-panel')
    for (const text of ['SIMPLE', 'score 0.12', '90.0%', 'question answering', 'SIMPLE → SIMPLE', 'priority', 'mock/fast-v1', '43 ms', 'Cache miss',
      'Provider status (this call)ok']) {
      expect(routing).toHaveTextContent(text)
    }
    expect(routing).toHaveTextContent('Routing reason: Query classified as SIMPLE (confidence 0.90). Selected mock-fast.')
    expect(within(routing).getByRole('list', { name: 'Candidate models' })).toHaveTextContent('mock-fastmock-balanced')
    expect(within(screen.getByTestId('model-path')).getByText('No fallback')).toBeInTheDocument()
    const attempts = within(routing).getByRole('table', { name: 'Provider attempts' })
    expect(within(attempts).getAllByRole('row')).toHaveLength(2)
    expect(attempts).toHaveTextContent('success')
  })

  it('distinguishes the initially selected model from the model that actually answered (fallback)', async () => {
    serveChat(() => HttpResponse.json(chatResponse({
      selected_model: 'mock-advanced', model_used: 'mock-balanced', provider_model: 'mock/balanced-v1',
      fallback: { used: true, chain: ['mock-advanced', 'mock-balanced'], original_model: 'mock-advanced', final_model: 'mock-balanced',
        reason: 'mock-advanced failed: unavailable',
        attempts: [
          { attempt_number: 1, model_id: 'mock-advanced', provider: 'mock', outcome: 'error', error_kind: 'unavailable', retryable: true, latency_ms: 5 },
          { attempt_number: 2, model_id: 'mock-balanced', provider: 'mock', outcome: 'success', error_kind: null, retryable: null, latency_ms: 30 },
        ] },
    })))
    const { user } = await openChat()
    await user.type(prompt(), 'q{Enter}')
    const path = await screen.findByTestId('model-path')
    expect(path).toHaveTextContent('Initially selectedmock-advanced')
    expect(path).toHaveTextContent('Actually answeredmock-balanced')
    expect(within(path).getByText('Fallback used')).toBeInTheDocument()
    const fb = screen.getByTestId('fallback-info')
    expect(fb).toHaveTextContent('Fallback chain (used)')
    expect(fb).toHaveTextContent('Fallback reason: mock-advanced failed: unavailable')
    const attempts = screen.getByRole('table', { name: 'Provider attempts' })
    expect(attempts).toHaveTextContent('unavailable (retryable)')
    expect(lastTurn().getByText('fallback')).toBeInTheDocument()
  })

  it('shows tokens, estimated cost, placeholder pricing and mock mode from the response', async () => {
    serveChat()
    const { user } = await openChat()
    await user.type(prompt(), 'q{Enter}')
    const usage = await screen.findByTestId('usage-panel')
    expect(within(usage).getByLabelText('Token usage')).toHaveTextContent('Input tokens12Output tokens8Total tokens20Token sourceReported by the provider')
    expect(within(usage).getByLabelText('Estimated cost')).toHaveTextContent('Estimated total$0.000004')
    expect(within(usage).getByLabelText('Estimated cost')).toHaveTextContent('$0.10 / $0.40')
    expect(usage).toHaveTextContent('Mock mode: no paid provider call')
    expect(usage).toHaveTextContent(/Placeholder pricing: the cost is computed from unverified rates/)
    expect(screen.getByTestId('turn-summary')).toHaveTextContent('20 tokens')
  })

  it('shows missing usage and cost as unavailable, never as zero, and labels real-provider mode', async () => {
    serveChat(() => HttpResponse.json(chatResponse({
      mock_mode: false,
      usage: { input_tokens: null, output_tokens: null, total_tokens: null, source: 'unavailable' },
      cost: { currency: 'USD', input_cost: null, output_cost: null, total_cost: null, input_cost_per_million: 1, output_cost_per_million: 2,
        is_estimate: true, pricing_is_placeholder: false },
    })))
    const { user } = await openChat()
    await user.type(prompt(), 'q{Enter}')
    const usage = await screen.findByTestId('usage-panel')
    expect(within(within(usage).getByLabelText('Token usage')).getAllByText('Unavailable')).toHaveLength(3)
    expect(within(within(usage).getByLabelText('Estimated cost')).getAllByText('Unavailable')).toHaveLength(3)
    expect(within(usage).getByLabelText('Token usage')).not.toHaveTextContent(/\b0\b/)
    expect(usage).toHaveTextContent('Real provider: may incur cost')
    expect(usage).not.toHaveTextContent('Placeholder pricing')
    expect(screen.getByTestId('turn-summary')).toHaveTextContent('tokens unavailable')
    expect(screen.getByTestId('turn-summary')).toHaveTextContent('cost unavailable')
  })

  it('shows cache hit information', async () => {
    serveChat(() => HttpResponse.json(chatResponse({
      cache: { status: 'hit_semantic', stored: false, similarity: 0.951, threshold: 0.92, saved_tokens: 20, saved_cost: 0.0000044,
        age_seconds: 12.4, source_request_id: 'req-original', lookup_ms: 3 },
      usage: { input_tokens: 12, output_tokens: 8, total_tokens: 20, source: 'cache_hit' },
      fallback: { used: false, chain: [], original_model: 'mock-fast', final_model: 'mock-fast', reason: null, attempts: [] },
    })))
    const { user } = await openChat()
    await user.type(prompt(), 'q{Enter}')
    const cache = await screen.findByLabelText('Cache')
    expect(cache).toHaveTextContent('Semantic cache hit')
    expect(cache).toHaveTextContent('0.951 / 0.92')
    expect(cache).toHaveTextContent('Saved tokens20')
    expect(cache).toHaveTextContent('req-original')
    expect(screen.getByText(/no provider was called for this answer/)).toBeInTheDocument()
    expect(screen.getByText('None: served from the cache.')).toBeInTheDocument()
    expect(lastTurn().getAllByText('Semantic cache hit').length).toBeGreaterThan(0)
  })
})

describe('Chat Playground: credentials', () => {
  it('asks for the chat key on 401, never resends automatically, and sends it only as a bearer header', async () => {
    const consoleSpies = (['log', 'info', 'warn', 'error', 'debug'] as const).map((m) => vi.spyOn(console, m))
    const calls = serveChat((body) => (calls.length === 1
      ? errorResponse(401, 'unauthorized', 'Missing or invalid chat API key')
      : HttpResponse.json(chatResponse({ answer: `ok ${body.query}` }))))
    const { user } = await openChat()
    await user.type(prompt(), 'secret question{Enter}')
    const error = await screen.findByTestId('chat-error')
    expect(error).toHaveTextContent('Chat API key required')
    const key = 'chat-key-ABCDEFGH-12345'
    await user.type(within(error).getByPlaceholderText('Chat API key'), key)
    await user.click(within(error).getByRole('button', { name: 'Use token' }))
    expect(within(error).getByText(/Chat API key saved\. Press Retry/)).toBeInTheDocument()
    await new Promise((r) => setTimeout(r, 100))
    expect(calls).toHaveLength(1) // saving the key does NOT resend
    await user.click(within(error).getByRole('button', { name: 'Retry' }))
    await screen.findByText('ok secret question')
    expect(calls).toHaveLength(2)
    expect(calls[0].auth).toBeNull()
    expect(calls[1].auth).toBe(`Bearer ${key}`)
    for (const c of calls) {
      expect(JSON.stringify(c.body)).not.toContain(key)
      expect(c.url).not.toContain(key)
    }
    expect(document.body.textContent).not.toContain(key)
    for (const spy of consoleSpies) expect(JSON.stringify(spy.mock.calls)).not.toContain(key)
    expect(window.localStorage.length).toBe(0)
    expect(window.sessionStorage.length).toBe(0) // memory only unless the user opts in
  })

  it('shows whether a chat key is set (never its value) and can clear it', async () => {
    credentialStore.set('chat', 'preset-chat-key-123456')
    const { user } = await openChat()
    const status = screen.getByTestId('chat-key-status')
    expect(status).toHaveTextContent('set (memory only)')
    expect(status).not.toHaveTextContent('preset-chat-key')
    await user.click(within(status).getByRole('button', { name: 'Clear' }))
    expect(status).toHaveTextContent('not set')
    expect(credentialStore.has('chat')).toBe(false)
  })
})

describe('Chat Playground: errors (never retried automatically)', () => {
  it.each([
    [400, 'unknown_model', 'Model not registered'],
    [400, 'model_disabled', 'Model disabled'],
    [413, 'request_too_large', 'Request too large'],
    [422, 'request_rejected_by_provider', 'Rejected by the provider'],
    [503, 'budget_unavailable', 'Budget store unavailable'],
    [503, 'no_model_available', 'No model available'],
  ])('HTTP %s %s shows "%s"', async (status, code, title) => {
    const calls = serveChat(() => errorResponse(status, code, `backend: ${code}`))
    const { user } = await openChat()
    await user.type(prompt(), 'q{Enter}')
    const error = await screen.findByTestId('chat-error')
    expect(error).toHaveTextContent(title)
    expect(error).toHaveTextContent(`backend: ${code}`)
    expect(error).toHaveTextContent(`HTTP ${status} · ${code}`)
    expect(lastTurn().getByTestId('request-id').textContent).toMatch(/^ui-/)
    await new Promise((r) => setTimeout(r, 100))
    expect(calls).toHaveLength(1)
  })

  it('422 validation errors list the field messages', async () => {
    serveChat(() => errorResponse(422, 'validation_error', 'Request validation failed',
      { details: [{ type: 'less_than_equal', loc: ['body', 'temperature'], msg: 'Input should be less than or equal to 2' }] }))
    const { user } = await openChat()
    await user.type(prompt(), 'q{Enter}')
    const error = await screen.findByTestId('chat-error')
    expect(error).toHaveTextContent('Request failed validation')
    expect(within(error).getByRole('list', { name: 'Validation errors' })).toHaveTextContent('temperature: Input should be less than or equal to 2')
  })

  it('a model error refreshes the model list so the override can be corrected', async () => {
    let modelCalls = 0
    server.use(http.get('/api/v1/models', () => {
      modelCalls += 1
      return HttpResponse.json(MODELS_FIXTURE)
    }))
    serveChat(() => errorResponse(400, 'model_disabled', "Model 'mock-advanced' is disabled."))
    const { user } = await openChat()
    expect(modelCalls).toBe(1)
    await user.selectOptions(screen.getByLabelText('Model'), 'mock-advanced')
    await user.type(prompt(), 'q{Enter}')
    await screen.findByTestId('chat-error')
    await waitFor(() => expect(modelCalls).toBe(2))
  })

  it('429 rate limit shows Retry-After and keeps Retry disabled until it has passed', async () => {
    const calls = serveChat(() => errorResponse(429, 'rate_limit_exceeded', 'Rate limit exceeded', {
      headers: { 'Retry-After': '30' }, details: { limit: 10, window: '1m', retry_after_seconds: 30 } }))
    const { user } = await openChat()
    await user.type(prompt(), 'q{Enter}')
    const error = await screen.findByTestId('chat-error')
    expect(error).toHaveTextContent('Rate limit reached')
    expect(within(error).getByTestId('retry-after')).toHaveTextContent(/Retry-After: 30s \(\d+s left\)/)
    expect(within(error).getByRole('button', { name: /Retry in \d+s/ })).toBeDisabled()
    expect(error).not.toHaveTextContent('Spend limit')
    expect(calls).toHaveLength(1)
  })

  it('429 budget exceeded is shown as a spend limit, not a rate limit', async () => {
    serveChat(() => errorResponse(429, 'budget_exceeded', 'Spend limit reached for this request; no provider was called.', {
      details: { reasons: ['daily_budget'], attempts: [{ model: 'mock-fast', reason: 'daily_budget' }] } }))
    const { user } = await openChat()
    await user.type(prompt(), 'q{Enter}')
    const error = await screen.findByTestId('chat-error')
    expect(error).toHaveTextContent('Spend limit reached')
    expect(error).toHaveTextContent('This is not a rate limit')
    expect(error).toHaveTextContent('Budget reasons: daily_budget')
    expect(error).not.toHaveTextContent('Rate limit reached')
    expect(within(error).queryByTestId('retry-after')).not.toBeInTheDocument()
  })

  it('503 all models failed lists the attempts', async () => {
    serveChat(() => errorResponse(503, 'all_models_failed', 'All candidate models failed to produce a response.', {
      details: { selected_model: 'mock-advanced', attempts: [{ model: 'mock-advanced', outcome: 'error', error_kind: 'unavailable' },
        { model: 'mock-balanced', outcome: 'error', error_kind: 'timeout' }] } }))
    const { user } = await openChat()
    await user.type(prompt(), 'q{Enter}')
    const error = await screen.findByTestId('chat-error')
    expect(error).toHaveTextContent('All models failed')
    expect(within(error).getByRole('list', { name: 'Failed attempts' })).toHaveTextContent('mock-advanced · error · unavailablemock-balanced · error · timeout')
  })

  it('shows "Backend unreachable" for network failures', async () => {
    serveChat(() => HttpResponse.error())
    const { user } = await openChat()
    await user.type(prompt(), 'q{Enter}')
    expect(await screen.findByTestId('chat-error')).toHaveTextContent('Backend unreachable')
  })

  it('never resubmits after an error or after Retry-After passes; Retry and Edit are explicit', async () => {
    const calls = serveChat(() => (calls.length === 1
      ? errorResponse(429, 'rate_limit_exceeded', 'Rate limit exceeded', { headers: { 'Retry-After': '1' } })
      : HttpResponse.json(chatResponse())))
    const { user } = await openChat()
    await user.type(prompt(), 'please answer{Enter}')
    const error = await screen.findByTestId('chat-error')
    await new Promise((r) => setTimeout(r, 1500)) // Retry-After elapses
    expect(calls).toHaveLength(1)
    const retry = within(error).getByRole('button', { name: 'Retry' })
    await waitFor(() => expect(retry).toBeEnabled())
    await user.click(within(error).getByRole('button', { name: 'Edit prompt' }))
    expect(prompt()).toHaveValue('please answer')
    expect(calls).toHaveLength(1)
    await user.click(retry)
    await screen.findByText('Paris is the capital of France.')
    expect(calls).toHaveLength(2)
    expect(calls[1].body.query).toBe('please answer')
  })
})

describe('Chat Playground: privacy and layout', () => {
  it('keeps the conversation in memory only (no localStorage/sessionStorage) and records metadata-only activity', async () => {
    serveChat()
    const { user, router } = await openChat()
    await user.type(prompt(), 'very private prompt{Enter}')
    await screen.findByText('Paris is the capital of France.')
    expect(window.localStorage.length).toBe(0)
    for (let i = 0; i < window.sessionStorage.length; i++) {
      expect(window.sessionStorage.getItem(window.sessionStorage.key(i)!)).not.toContain('very private prompt')
    }
    const activity = sessionActivity.list()
    expect(activity).toHaveLength(1)
    expect(activity[0]).toMatchObject({ outcome: 'success', modelUsed: 'mock-fast', requestId: 'req-test-1' })
    expect(JSON.stringify(activity)).not.toContain('very private prompt')
    expect(JSON.stringify(activity)).not.toContain('Paris')
    // survives switching pages in the same tab (memory), gone after a reload (module state reset)
    await router.navigate('/models')
    await router.navigate('/chat')
    expect(await screen.findByText('Paris is the capital of France.')).toBeInTheDocument()
    chatSession.clear()
    expect(await screen.findByText('Start a conversation')).toBeInTheDocument()
  })

  it('does not abort an in-flight chat request when navigating away (its result is kept)', async () => {
    let signal: AbortSignal | null = null
    serveChat(async () => {
      await delay(100)
      return HttpResponse.json(chatResponse())
    })
    server.events.on('request:start', ({ request }) => {
      if (request.url.endsWith('/api/v1/chat')) signal = request.signal
    })
    const { user, router } = await openChat()
    await user.type(prompt(), 'q{Enter}')
    await router.navigate('/models')
    await waitFor(() => expect(chatSession.list()[0]?.status).toBe('success'))
    expect(signal!.aborted).toBe(false)
    server.events.removeAllListeners()
  })

  it('uses a responsive layout: options above the conversation on small screens, side column on xl', async () => {
    await openChat()
    const layout = screen.getByTestId('chat-layout')
    expect(layout.className).toContain('grid-cols-1')
    expect(layout.className).toContain('xl:grid-cols-[minmax(0,1fr)_340px]')
    const [aside, card] = Array.from(layout.children)
    expect(aside).toHaveAttribute('aria-label', 'Routing options')
    expect(aside.className).toContain('xl:order-2')
    expect(card.className).toContain('xl:order-1')
    expect(within(card as HTMLElement).getByLabelText('Prompt')).toBeInTheDocument()
  })
})

describe('Chat Playground: backend busy (503) and rate-limit quota (Phase B P1/P2)', () => {
  it('503 rate_limiter_unavailable is shown as "busy, retry shortly" with its Retry-After countdown', async () => {
    const calls = serveChat(() => errorResponse(503, 'rate_limiter_unavailable', 'Rate limiter busy; retry shortly.', {
      headers: { 'Retry-After': '20' } }))
    const { user } = await openChat()
    await user.type(prompt(), 'q{Enter}')
    const error = await screen.findByTestId('chat-error')
    expect(error).toHaveTextContent('Backend busy, retry shortly')
    expect(error).toHaveTextContent('Rate limiter busy; retry shortly.')
    expect(error).not.toHaveTextContent('could not check')
    expect(within(error).getByTestId('retry-after')).toHaveTextContent(/Retry-After: 20s \(\d+s left\)/)
    expect(within(error).getByRole('button', { name: /Retry in \d+s/ })).toBeDisabled()
    expect(error).toHaveTextContent('HTTP 503 · rate_limiter_unavailable')
    expect(calls).toHaveLength(1) // never retried automatically
  })

  it('shows the X-RateLimit quota of a successful response in the turn footer and the details', async () => {
    serveChat(() => HttpResponse.json(chatResponse(), { headers: {
      'X-RateLimit-Limit': '60', 'X-RateLimit-Remaining': '57', 'X-RateLimit-Reset': '42', 'X-RateLimit-Window': 'minute' } }))
    const { user } = await openChat()
    await user.type(prompt(), 'q{Enter}')
    expect(await lastTurn().findByTestId('rate-limit-quota')).toHaveTextContent('Rate limit: 57 of 60 requests left this minute, resets in 42s')
    const details = screen.getByLabelText('Rate limit')
    expect(details).toHaveTextContent('60 per minute')
    expect(within(details).getByTestId('rate-limit-remaining')).toHaveTextContent('57')
    expect(details).toHaveTextContent('42 s')
  })

  it('shows no quota when the backend sends no rate-limit headers (limits disabled)', async () => {
    serveChat()
    const { user } = await openChat()
    await user.type(prompt(), 'q{Enter}')
    await screen.findByTestId('routing-panel')
    expect(screen.queryByTestId('rate-limit-quota')).not.toBeInTheDocument()
    expect(screen.queryByLabelText('Rate limit')).not.toBeInTheDocument()
  })
})
