import { lazy } from 'react'

/**
 * Code-split pages: page code (and chart libraries such as Recharts) is only downloaded when
 * the page is opened. AppShell renders them inside <Suspense> with a loading state.
 */
export const OverviewPage = lazy(() => import('./OverviewPage').then((m) => ({ default: m.OverviewPage })))
export const ModelRegistryPage = lazy(() => import('./ModelRegistryPage').then((m) => ({ default: m.ModelRegistryPage })))
export const ChatPlaygroundPage = lazy(() => import('./ChatPlaygroundPage').then((m) => ({ default: m.ChatPlaygroundPage })))
export const RoutingAnalyticsPage = lazy(() => import('./RoutingAnalyticsPage').then((m) => ({ default: m.RoutingAnalyticsPage })))
export const CacheManagementPage = lazy(() => import('./CacheManagementPage').then((m) => ({ default: m.CacheManagementPage })))
export const ObservabilityPage = lazy(() => import('./ObservabilityPage').then((m) => ({ default: m.ObservabilityPage })))
export const BudgetManagementPage = lazy(() => import('./BudgetManagementPage').then((m) => ({ default: m.BudgetManagementPage })))
export const SettingsPage = lazy(() => import('./SettingsPage').then((m) => ({ default: m.SettingsPage })))
