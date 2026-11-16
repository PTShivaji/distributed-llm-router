import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { MemoryRouter } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import { credentialStore } from '../../auth/credentials'
import { errorResponse, server } from '../../test/server'
import { ClearCacheDialog } from './ClearCacheDialog'

const ADMIN = 'admin-token-for-dialog-tests-00001'

function setup(props: Partial<Parameters<typeof ClearCacheDialog>[0]> = {}) {
  const onClose = vi.fn()
  const onCleared = vi.fn()
  const utils = render(<MemoryRouter><ClearCacheDialog open onClose={onClose} onCleared={onCleared} {...props} /></MemoryRouter>)
  return { onClose, onCleared, user: userEvent.setup(), ...utils }
}

/** Records each DELETE and the token field's value at the moment the request arrives. */
function serveClear(respond: () => Response = () => HttpResponse.json({ cleared: true, deleted_keys: 5, reset_stats: false })) {
  const calls: { auth: string | null; resetStats: string | null; fieldValueDuringRequest: string | undefined }[] = []
  server.use(http.delete('/api/v1/cache', ({ request }) => {
    calls.push({
      auth: request.headers.get('authorization'),
      resetStats: new URL(request.url).searchParams.get('reset_stats'),
      fieldValueDuringRequest: (document.getElementById('admin-token') as HTMLInputElement | null)?.value,
    })
    return respond()
  }))
  return calls
}

async function fillAndSubmit(user: ReturnType<typeof userEvent.setup>, opts: { reset?: boolean } = {}) {
  await user.type(screen.getByLabelText('Admin token'), ADMIN)
  if (opts.reset) await user.click(screen.getByLabelText(/Also reset hit\/miss statistics/))
  await user.click(screen.getByLabelText(/I understand/))
  await user.click(screen.getByRole('button', { name: 'Clear cache' }))
}

/** The token must not survive anywhere the app could read it later. */
function expectTokenGone() {
  expect((document.getElementById('admin-token') as HTMLInputElement | null)?.value ?? '').toBe('')
  expect(document.documentElement.outerHTML).not.toContain(ADMIN)
  expect(window.sessionStorage.length + window.localStorage.length).toBe(0)
  expect(credentialStore.has('chat') || credentialStore.has('observability')).toBe(false)
}

describe('ClearCacheDialog', () => {
  it('requires both the admin token and an explicit confirmation, and warns that the cache is shared', async () => {
    const { user } = setup()
    const submit = screen.getByRole('button', { name: 'Clear cache' })
    expect(submit).toBeDisabled()
    await user.type(screen.getByLabelText('Admin token'), ADMIN)
    expect(submit).toBeDisabled()
    await user.click(screen.getByLabelText(/I understand/))
    expect(submit).toBeEnabled()
    expect(screen.getByLabelText('Admin token')).toHaveAttribute('type', 'password')
    expect(screen.getByLabelText('Admin token')).toHaveAttribute('autocomplete', 'off')
    expect(screen.getByRole('note')).toHaveTextContent(/shared.*every user, client and backend instance/)
  })

  it('sends the token once as a bearer header, empties the field BEFORE the request, reset_stats=false by default', async () => {
    const calls = serveClear()
    const { user, onCleared, onClose } = setup()
    await fillAndSubmit(user)
    expect(calls).toEqual([{ auth: `Bearer ${ADMIN}`, resetStats: 'false', fieldValueDuringRequest: '' }])
    expect(onCleared).toHaveBeenCalledWith({ cleared: true, deleted_keys: 5, reset_stats: false })
    expect(onClose).toHaveBeenCalled()
    expectTokenGone()
  })

  it('sends reset_stats=true only when chosen', async () => {
    const calls = serveClear(() => HttpResponse.json({ cleared: true, deleted_keys: 9, reset_stats: true }))
    const { user, onCleared } = setup()
    await fillAndSubmit(user, { reset: true })
    expect(calls[0].resetStats).toBe('true')
    expect(onCleared).toHaveBeenCalledWith({ cleared: true, deleted_keys: 9, reset_stats: true })
  })

  it('on failure: shows the error, discards the token, does not retry and requires re-entry', async () => {
    const calls = serveClear(() => errorResponse(401, 'unauthorized', 'Missing or invalid admin token.'))
    const { user, onCleared } = setup()
    await fillAndSubmit(user)
    const alert = await screen.findByTestId('clear-error')
    expect(alert).toHaveTextContent('Admin token rejected')
    expect(alert).toHaveTextContent('Missing or invalid admin token.')
    expect(alert).toHaveTextContent('HTTP 401 · unauthorized')
    expect(alert).not.toHaveTextContent(/Settings/) // the admin token is never a stored credential
    expect(onCleared).not.toHaveBeenCalled()
    expectTokenGone()
    expect(screen.getByRole('button', { name: 'Clear cache' })).toBeDisabled()
    await new Promise((r) => setTimeout(r, 100))
    expect(calls).toHaveLength(1)
  })

  it.each([
    [403, 'admin_disabled', 'Admin endpoints are disabled', /no ADMIN_API_TOKEN configured/, {}],
    [503, 'cache_unavailable', 'Cache unavailable', /nothing was cleared/, {}],
    [429, 'rate_limit_exceeded', 'Rate limit reached', /Try again in 20s/, { 'Retry-After': '20' }],
  ])('explains HTTP %s %s', async (status, code, title, explanation, headers) => {
    const calls = serveClear(() => errorResponse(status, code, `backend: ${code}`, { headers }))
    const { user } = setup()
    await fillAndSubmit(user)
    const alert = await screen.findByTestId('clear-error')
    expect(alert).toHaveTextContent(title)
    expect(alert).toHaveTextContent(explanation)
    expectTokenGone()
    expect(calls).toHaveLength(1)
  })

  it('network failure says the outcome is unknown and is not retried', async () => {
    const calls = serveClear(() => HttpResponse.error())
    const { user } = setup()
    await fillAndSubmit(user)
    expect(await screen.findByTestId('clear-error')).toHaveTextContent('Backend unreachable')
    expectTokenGone()
    expect(calls).toHaveLength(1)
  })

  it('wipes the typed token when cancelled or closed with Escape (nothing is sent)', async () => {
    const calls = serveClear()
    const { user, onClose } = setup()
    await user.type(screen.getByLabelText('Admin token'), ADMIN)
    await user.click(screen.getByRole('button', { name: 'Cancel' }))
    expect(onClose).toHaveBeenCalledTimes(1)
    expectTokenGone() // still mounted (open prop unchanged)
    await user.type(screen.getByLabelText('Admin token'), ADMIN)
    await user.keyboard('{Escape}')
    expect(onClose).toHaveBeenCalledTimes(2)
    expectTokenGone()
    expect(calls).toHaveLength(0)
  })
})
