import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { MemoryRouter } from 'react-router'
import { describe, expect, it, vi } from 'vitest'

import { ApiError } from '../../api/errors'
import { ErrorBoundary } from './ErrorBoundary'
import { EmptyState, ErrorState, LoadingState } from './States'

function Boom({ fail }: { fail: boolean }) {
  if (fail) throw new Error('render failure')
  return <p>recovered</p>
}

describe('ErrorBoundary', () => {
  it('contains render errors and can be reset', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const user = userEvent.setup()
    let fail = true
    const { rerender } = render(<ErrorBoundary><Boom fail={fail} /></ErrorBoundary>)
    expect(screen.getByRole('alert')).toHaveTextContent('This view failed to render')
    fail = false
    rerender(<ErrorBoundary><Boom fail={fail} /></ErrorBoundary>)
    await user.click(screen.getByRole('button', { name: 'Try again' }))
    expect(screen.getByText('recovered')).toBeInTheDocument()
  })

  it('resets automatically when resetKey changes (navigation)', () => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
    const { rerender } = render(<ErrorBoundary resetKey="/a"><Boom fail /></ErrorBoundary>)
    expect(screen.getByRole('alert')).toBeInTheDocument()
    rerender(<ErrorBoundary resetKey="/b"><Boom fail={false} /></ErrorBoundary>)
    expect(screen.getByText('recovered')).toBeInTheDocument()
  })
})

describe('loading, empty and error states', () => {
  it('announces loading politely', () => {
    render(<LoadingState label="Loading models…" />)
    expect(screen.getByRole('status')).toHaveTextContent('Loading models…')
  })

  it('renders an empty state', () => {
    render(<EmptyState title="No models" description="The registry is empty." />)
    expect(screen.getByText('No models')).toBeInTheDocument()
  })

  it('401: points to Settings instead of offering a retry', () => {
    const error = new ApiError({ message: 'Missing or invalid observability token.', status: 401, code: 'unauthorized',
      kind: 'unauthorized', requestId: 'srv-9' })
    render(<MemoryRouter><ErrorState error={error} onRetry={() => {}} /></MemoryRouter>)
    expect(screen.getByRole('alert')).toHaveTextContent('Authentication required')
    expect(screen.getByRole('link', { name: 'Open Settings' })).toHaveAttribute('href', '/settings')
    expect(screen.queryByRole('button', { name: 'Retry' })).not.toBeInTheDocument()
    expect(screen.getByText(/HTTP 401 · unauthorized · request srv-9/)).toBeInTheDocument()
  })

  it('429: shows the Retry-After wait and a manual retry button', async () => {
    const onRetry = vi.fn()
    const error = new ApiError({ message: 'Rate limit exceeded.', status: 429, code: 'rate_limit_exceeded',
      kind: 'rate_limited', retryAfterSeconds: 30 })
    render(<MemoryRouter><ErrorState error={error} onRetry={onRetry} /></MemoryRouter>)
    expect(screen.getByText('Try again in 30s.')).toBeInTheDocument()
    await userEvent.setup().click(screen.getByRole('button', { name: 'Retry' }))
    expect(onRetry).toHaveBeenCalledOnce()
  })

  it.each([
    [503, 'budget_unavailable', 'unavailable', 'Service unavailable'],
    [429, 'budget_exceeded', 'budget_exceeded', 'Spend limit reached'],
    [404, 'not_found', 'not_found', 'Not found'],
    [422, 'validation_error', 'validation', 'Invalid request'],
  ] as const)('%i %s shows "%s"', (status, code, kind, title) => {
    render(<MemoryRouter><ErrorState error={new ApiError({ message: 'm', status, code, kind })} /></MemoryRouter>)
    expect(screen.getByRole('alert')).toHaveTextContent(title)
  })
})
