import { useState } from 'react'
import { Info, RefreshCw } from 'lucide-react'

import { api } from '../api/endpoints'
import { ApiError } from '../api/errors'
import type { DependenciesResponse, SettingsResponse } from '../api/types'
import { SkeletonRows } from '../components/feedback/States'
import { ConnectionStatus } from '../components/layout/ConnectionStatus'
import { HealthCard } from '../components/observability/HealthCard'
import { SectionFailure } from '../components/observability/StaleNotice'
import {
  AdministrationSection, AuthenticationSection, ConfigurationSection, ProductionChecksSection, ProvidersSection, RuntimeSection,
  SettingsKpis,
} from '../components/settings/SettingsSections'
import { Badge } from '../components/ui/Badge'
import { Button } from '../components/ui/Button'
import { Card, CardBody } from '../components/ui/Card'
import { PageHeader } from '../components/ui/PageHeader'
import { useApiQuery } from '../hooks/useApiQuery'
import { refreshBackendHealth, useBackendHealth } from '../hooks/useBackendHealth'
import { formatClock } from '../lib/format'
import { isDependenciesResponse } from '../lib/observability'
import { loadSettings } from '../lib/settings'

interface Loaded<T> { value: T; at: number }

/**
 * Settings (read-only): GET /api/v1/settings through the validated loader (src/lib/settings.ts), plus the
 * same health sources as Observability (GET /health, GET /api/v1/health/dependencies). Both protected
 * endpoints use the observability token. Loaded on open and on Refresh only: never polled, never retried
 * automatically. A failed refresh keeps the earlier data, marked stale. Nothing here changes the backend.
 */
export function SettingsPage() {
  const [refreshKey, setRefreshKey] = useState(0)
  const health = useBackendHealth()

  const settings = useApiQuery<Loaded<SettingsResponse>>(async (signal) => {
    const { data, meta } = await loadSettings(api, { signal })
    return { data: { value: data, at: Date.now() }, meta }
  }, refreshKey)

  const deps = useApiQuery<Loaded<DependenciesResponse>>(async (signal) => {
    const { data, meta } = await api.dependencies({ signal })
    if (!isDependenciesResponse(data)) {
      throw new ApiError({ message: 'GET /api/v1/health/dependencies returned a body that does not match the dependency schema.',
        status: meta.status, code: 'malformed_response', kind: 'unknown' })
    }
    return { data: { value: data, at: Date.now() }, meta }
  }, refreshKey)

  const refresh = () => {
    setRefreshKey((k) => k + 1)
    void refreshBackendHealth()
  }

  const loading = settings.loading || deps.loading
  const s = settings.data?.value ?? null
  const status = loading ? 'Loading…'
    : settings.error ? (s ? 'Refresh failed · showing earlier data' : 'Not loaded')
      : settings.data ? `Updated ${formatClock(new Date(settings.data.at))}` : 'Loading…'
  // The dependency section above owns the token prompt; never show two prompts for the same credential.
  const promptHere = deps.error?.kind !== 'unauthorized'

  return (
    <div className="space-y-6" data-testid="settings-page">
      <PageHeader
        title="Settings"
        description="Read-only view of the backend's effective configuration, credentials status and health."
        actions={(
          <div className="flex flex-wrap items-center justify-end gap-2">
            <span className="hidden sm:inline-flex"><ConnectionStatus /></span>
            <Badge tone="neutral">Read-only</Badge>
            <span className="text-xs text-slate-500" aria-live="polite" data-testid="refresh-status">{status}</span>
            <Button variant="secondary" size="sm" onClick={refresh} loading={loading}
              icon={<RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />}>Refresh</Button>
          </div>
        )}
      />

      <p className="flex gap-2 rounded-lg border border-slate-200 bg-white p-3 text-xs text-slate-600 shadow-sm" role="note" data-testid="settings-note">
        <Info className="mt-0.5 h-3.5 w-3.5 shrink-0 text-slate-400" aria-hidden="true" />
        <span>
          <strong className="font-medium text-slate-700">Nothing on this page changes the backend.</strong> Configuration comes from
          environment variables on the server and changes need a restart. The backend sends an allow-list of safe values only: keys, tokens,
          passwords, hosts, URLs, CORS origins and client names never reach the browser. With several API instances, the one that
          answered is shown.
        </span>
      </p>

      <SettingsKpis settings={s} loading={settings.loading && !s} />

      <HealthCard liveness={health.status} health={health.data} healthError={health.error}
        deps={deps.data?.value ?? null} depsAt={deps.data?.at ?? null} depsError={deps.error}
        depsLoading={deps.loading} onRetry={refresh} />

      {settings.error && (
        <section aria-label="Settings request status" data-testid="settings-failure">
          <SectionFailure error={settings.error} hasData={s !== null} at={settings.data?.at ?? null} what="settings"
            onRetry={refresh} prompt={promptHere} />
        </section>
      )}

      {!s && settings.loading && !settings.error && (
        <Card data-testid="settings-loading" aria-busy="true"><CardBody><SkeletonRows rows={6} /></CardBody></Card>
      )}

      {s && (
        <>
          <div className="grid grid-cols-1 gap-6 xl:grid-cols-2">
            <RuntimeSection settings={s} />
            <AdministrationSection settings={s} />
          </div>
          <AuthenticationSection settings={s} />
          <ProvidersSection settings={s} />
          <ProductionChecksSection settings={s} />
          <ConfigurationSection settings={s} />
        </>
      )}
    </div>
  )
}
