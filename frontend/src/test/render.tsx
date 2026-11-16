import { render } from '@testing-library/react'
import { createMemoryRouter, RouterProvider } from 'react-router'
import { beforeAll } from 'vitest'

import { routes } from '../router'

/** One-time module import; generous because a cold transform of the chart library can be slow on a busy machine. */
export const PRELOAD_TIMEOUT_MS = 60_000

/**
 * Warms the module cache for the lazy (code-split) pages so React.lazy resolves immediately in tests.
 * Registered by importing this module, i.e. only in test files that render the app (helper/unit tests skip the cost).
 */
export function preloadPages() {
  return Promise.all([import('../pages/OverviewPage'), import('../pages/ModelRegistryPage'), import('../pages/ChatPlaygroundPage'),
    import('../pages/RoutingAnalyticsPage'), import('../pages/CacheManagementPage'), import('../pages/ObservabilityPage'),
    import('../pages/SettingsPage'), import('../pages/BudgetManagementPage')])
}

beforeAll(preloadPages, PRELOAD_TIMEOUT_MS)

/** Renders the real route table at `path` inside a memory router. */
export function renderApp(path = '/') {
  const router = createMemoryRouter(routes, { initialEntries: [path] })
  const utils = render(<RouterProvider router={router} />)
  return { router, ...utils }
}
