import {
  Activity, BarChart3, Boxes, Database, LayoutDashboard, MessageSquareText, Settings, Wallet, type LucideIcon,
} from 'lucide-react'

export interface NavItem {
  path: string
  label: string
  icon: LucideIcon
  description: string
}

/** Sidebar order is part of the product spec (Step 10.3). */
export const NAV_ITEMS: readonly NavItem[] = [
  { path: '/', label: 'Overview', icon: LayoutDashboard, description: 'Requests, latency, cost, cache and provider health at a glance.' },
  { path: '/chat', label: 'Chat Playground', icon: MessageSquareText, description: 'Send a prompt through the router and inspect classification, routing, usage and cost.' },
  { path: '/models', label: 'Model Registry', icon: Boxes, description: 'Registered models, providers, pricing, status and routing priority.' },
  { path: '/analytics', label: 'Routing Analytics', icon: BarChart3, description: 'Request counts, provider attempts, retries, fallbacks, latency and tokens.' },
  { path: '/budget', label: 'Budget Management', icon: Wallet, description: 'Chat and embedding spend and budget refusal events (enforced by the backend).' },
  { path: '/cache', label: 'Cache Management', icon: Database, description: 'Exact and semantic cache statistics and admin-only cache clearing.' },
  { path: '/observability', label: 'Observability', icon: Activity, description: 'Prometheus metrics, dependency health, errors and tracing status.' },
  { path: '/settings', label: 'Settings', icon: Settings, description: 'API connection, runtime credentials and UI preferences.' },
]

/** Exact match (no page has nested routes): unknown paths such as /models/x or /chatter belong to no item. */
export function navItemFor(pathname: string): NavItem | undefined {
  const path = pathname.length > 1 ? pathname.replace(/\/+$/, '') : pathname
  return NAV_ITEMS.find((item) => item.path === path)
}
