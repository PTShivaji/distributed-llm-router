import { screen, waitFor, within } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { http, HttpResponse } from 'msw'
import { describe, expect, it } from 'vitest'

import { NAV_ITEMS } from '../components/layout/navigation'
import { renderApp } from '../test/render'
import { server } from '../test/server'

const EXPECTED_ORDER = ['Overview', 'Chat Playground', 'Model Registry', 'Routing Analytics', 'Budget Management',
  'Cache Management', 'Observability', 'Settings']

describe('application shell and navigation', () => {
  it('shows the product title and the eight sidebar items in the specified order', async () => {
    renderApp('/')
    const nav = screen.getByRole('navigation')
    expect(within(screen.getByRole('complementary', { name: 'Main navigation' })).getByText('Distributed AI Router')).toBeInTheDocument()
    expect(within(nav).getAllByRole('link').map((a) => a.textContent)).toEqual(EXPECTED_ORDER)
    expect(NAV_ITEMS.map((n) => n.label)).toEqual(EXPECTED_ORDER)
    // top bar + Overview header (lazy-loaded) share ONE /health request
    await screen.findByRole('heading', { level: 1, name: 'Overview' })
    await waitFor(() => expect(screen.getAllByText(/Connected · v0\.1\.0 · test/)).toHaveLength(2))
  })

  it.each(NAV_ITEMS.map((n) => [n.label, n.path]))('navigates to %s (%s) and marks it active', async (label, path) => {
    const user = userEvent.setup()
    const { router } = renderApp(path === '/' ? '/settings' : '/')
    const link = within(screen.getByRole('navigation')).getByRole('link', { name: label })
    await user.click(link)
    // Overview is lazy-loaded: navigation commits (in a transition) once its chunk has loaded
    expect(await screen.findByRole('heading', { level: 1, name: label })).toBeInTheDocument()
    expect(router.state.location.pathname).toBe(path)
    expect(link).toHaveAttribute('aria-current', 'page')
  })

  it('no module is a placeholder any more and none shows demo data', async () => {
    renderApp('/budget')
    expect(await screen.findByRole('heading', { level: 1, name: 'Budget Management' })).toBeInTheDocument()
    expect(await screen.findByRole('region', { name: 'Budget overview' })).toBeInTheDocument()
    expect(screen.queryByText('Not connected yet')).not.toBeInTheDocument()
    expect(screen.queryByText('Demo data')).not.toBeInTheDocument()
  })

  it('shows a not-found page for unknown routes', () => {
    renderApp('/does-not-exist')
    expect(screen.getByText('Page not found')).toBeInTheDocument()
    expect(screen.getByRole('link', { name: 'Go to Overview' })).toHaveAttribute('href', '/')
  })

  it('opens and closes the mobile navigation drawer', async () => {
    const user = userEvent.setup()
    renderApp('/')
    const sidebar = screen.getByRole('complementary', { name: 'Main navigation' })
    const toggle = screen.getByRole('button', { name: 'Open navigation' })
    expect(sidebar).toHaveAttribute('data-open', 'false')
    expect(sidebar.className).toContain('-translate-x-full')
    expect(sidebar.className).toContain('lg:translate-x-0') // always visible on desktop
    await user.click(toggle)
    expect(sidebar).toHaveAttribute('data-open', 'true')
    expect(toggle).toHaveAttribute('aria-expanded', 'true')
    await user.click(within(sidebar).getByRole('link', { name: 'Model Registry' })) // navigating closes it
    expect(sidebar).toHaveAttribute('data-open', 'false')
    await user.click(toggle)
    await user.click(screen.getByRole('button', { name: 'Close navigation' }))
    expect(sidebar).toHaveAttribute('data-open', 'false')
  })

  it('shows "Backend unreachable" when /health fails', async () => {
    server.use(http.get('/health', () => HttpResponse.error()))
    renderApp('/settings')
    // the top bar (app shell); the Settings page header shows the same shared status as well
    await waitFor(() => expect(within(screen.getByRole('banner')).getByText('Backend unreachable')).toBeInTheDocument())
  })
})
