import type { RouteObject } from 'react-router'

import { AppShell } from './components/layout/AppShell'
import {
  BudgetManagementPage, CacheManagementPage, ChatPlaygroundPage, ModelRegistryPage, ObservabilityPage, OverviewPage, RoutingAnalyticsPage,
  SettingsPage,
} from './pages/lazyPages'
import { NotFoundPage } from './pages/NotFoundPage'
import { RouteErrorPage } from './pages/RouteErrorPage'

/** Route table, shared by the browser router (main.tsx) and memory routers in tests. */
export const routes: RouteObject[] = [
  {
    path: '/',
    element: <AppShell />,
    // Errors thrown outside the page boundary (layout, loaders, router itself). Page render errors
    // and failed lazy chunks are caught closer, by AppShell's ErrorBoundary, keeping the layout.
    errorElement: <RouteErrorPage />,
    children: [
      { index: true, element: <OverviewPage /> },
      { path: 'chat', element: <ChatPlaygroundPage /> },
      { path: 'models', element: <ModelRegistryPage /> },
      { path: 'analytics', element: <RoutingAnalyticsPage /> },
      { path: 'budget', element: <BudgetManagementPage /> },
      { path: 'cache', element: <CacheManagementPage /> },
      { path: 'observability', element: <ObservabilityPage /> },
      { path: 'settings', element: <SettingsPage /> },
      { path: '*', element: <NotFoundPage /> },
    ],
  },
]
