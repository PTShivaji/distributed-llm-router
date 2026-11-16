/**
 * Read-only Settings sections, rendered ONLY from the validated GET /api/v1/settings body (src/lib/settings.ts)
 * plus the browser-tab credential status (booleans from useCredentialStatus - never token values).
 *
 * Display rules: null renders as an explicit label ("No limit", "Not configured", "Not applicable"), never as
 * 0 or "Disabled"; nothing here is derived from hidden values (hosts, URLs, origins, client names, secrets).
 */

import type { ReactNode } from 'react'
import { Link } from 'react-router'
import {
  AlertTriangle, CircleCheck, FlaskConical, Info, KeyRound, Server, ShieldAlert, ShieldCheck, UserCog,
  XCircle,
} from 'lucide-react'

import type { SettingsResponse } from '../../api/types'
import { useCredentialStatus } from '../../auth/credentials'
import { cn } from '../../lib/cn'
import { formatBytes, formatInteger, formatPercent, formatUsd } from '../../lib/format'
import {
  credentialStatus, credentialSummary, CREDENTIAL_STATUS_TEXT, formatDuration, issueCounts, sortedIssues,
  type CredentialStatus,
} from '../../lib/settings'
import { Badge, type BadgeTone } from '../ui/Badge'
import { Card, CardBody, CardHeader } from '../ui/Card'
import { KpiCard } from '../ui/KpiCard'

type S = SettingsResponse

// --------------------------------------------------------------------------- primitives

function Section({ title, description, actions, children, testId }: {
  title: string; description?: ReactNode; actions?: ReactNode; children: ReactNode; testId: string
}) {
  return (
    <section aria-label={title} data-testid={testId}>
      <Card className="h-full">
        <CardHeader title={title} description={description} actions={actions} />
        <CardBody>{children}</CardBody>
      </Card>
    </section>
  )
}

/** An explicit "no value" label (null from the API). Never rendered as 0 / false. */
function Unset({ children }: { children: ReactNode }) {
  return <span className="font-normal italic text-slate-500" data-unset="true">{children}</span>
}

function Flag({ on, onText = 'Enabled', offText = 'Disabled', onTone = 'success', offTone = 'neutral' }: {
  on: boolean; onText?: string; offText?: string; onTone?: BadgeTone; offTone?: BadgeTone
}) {
  return <Badge tone={on ? onTone : offTone}>{on ? onText : offText}</Badge>
}

function Fields({ children, label }: { children: ReactNode; label: string }) {
  return <dl className="divide-y divide-slate-100" aria-label={label}>{children}</dl>
}

function Field({ label, hint, children, testId }: { label: string; hint?: string; children: ReactNode; testId?: string }) {
  return (
    <div className="flex flex-col gap-0.5 py-2 first:pt-0 last:pb-0 sm:flex-row sm:items-start sm:justify-between sm:gap-4" data-testid={testId}>
      <dt className="text-xs text-slate-500">
        {label}
        {hint && <span className="block text-[11px] text-slate-400">{hint}</span>}
      </dt>
      <dd className="min-w-0 break-words text-sm font-medium text-slate-900 sm:text-right">{children}</dd>
    </div>
  )
}

const mono = (text: string) => <span className="font-mono text-xs">{text}</span>
const seconds = (n: number) => `${n} s`
const orNoLimit = (n: number | null, fmt: (v: number) => string = formatInteger) => (n === null ? <Unset>No limit</Unset> : fmt(n))
const orNotConfigured = (n: number | null, fmt: (v: number) => string) => (n === null ? <Unset>Not configured</Unset> : fmt(n))

// --------------------------------------------------------------------------- summary strip

export function SettingsKpis({ settings, loading }: { settings: S | null; loading: boolean }) {
  if (!settings) {
    return loading ? (
      <div className="grid grid-cols-2 gap-3 lg:grid-cols-4" data-testid="settings-kpis-loading">
        {['Environment', 'Provider mode', 'Provider credentials', 'Configuration checks'].map((label) => (
          <KpiCard key={label} label={label} value="" icon={<Server className="h-4 w-4" />} loading />
        ))}
      </div>
    ) : null
  }
  const creds = credentialSummary(settings.providers)
  const issues = issueCounts(settings.production_checks.issues)
  const prod = settings.app.environment === 'production'
  return (
    <div className="grid grid-cols-2 gap-3 lg:grid-cols-4" data-testid="settings-kpis">
      <KpiCard label="Environment" value={settings.app.environment} icon={<Server className="h-4 w-4" />} tone={prod ? 'brand' : 'neutral'}
        hint={`v${settings.app.version}${settings.app.debug ? ' · debug on' : ''}`} />
      <KpiCard label="Provider mode" value={settings.mode.mock_mode ? 'Mock' : settings.mode.real_provider_calls_enabled ? 'Real calls' : 'Real calls off'}
        icon={<FlaskConical className="h-4 w-4" />} tone={settings.mode.real_provider_calls_enabled ? 'warning' : 'neutral'}
        hint={settings.mode.mock_mode ? 'Simulated responses; no provider is called' : 'Paid provider calls are possible'} />
      <KpiCard label="Provider credentials" icon={<KeyRound className="h-4 w-4" />}
        value={creds.required ? `${creds.configured} of ${creds.required}` : <Unset>None required</Unset>}
        hint="Providers that need an API key" />
      <KpiCard label="Configuration checks" icon={<ShieldAlert className="h-4 w-4" />}
        tone={issues.errors ? 'danger' : issues.warnings ? 'warning' : 'success'}
        value={issues.errors + issues.warnings === 0 ? 'No issues' : `${issues.errors} error${issues.errors === 1 ? '' : 's'}`}
        hint={`${issues.warnings} warning${issues.warnings === 1 ? '' : 's'} · production rules`} />
    </div>
  )
}

// --------------------------------------------------------------------------- A. runtime

export function RuntimeSection({ settings }: { settings: S }) {
  const { app, mode } = settings
  return (
    <Section title="Runtime information" testId="section-runtime"
      description="Reported by the API instance that answered (GET /api/v1/settings).">
      <Fields label="Runtime information">
        <Field label="Application">{app.name}</Field>
        <Field label="Version">{mono(`v${app.version}`)}</Field>
        <Field label="Environment"><Badge tone={app.environment === 'production' ? 'info' : 'neutral'}>{app.environment}</Badge></Field>
        <Field label="Debug mode"><Flag on={app.debug} onText="On" offText="Off" onTone="warning" /></Field>
        <Field label="Uptime" hint="This instance only; resets on restart">{formatDuration(app.uptime_seconds)}</Field>
        <Field label="Mock mode" hint="Deterministic simulated responses">
          <Flag on={mode.mock_mode} onText="On" offText="Off" onTone="demo" offTone="warning" />
        </Field>
        <Field label="Real provider calls" hint="Need LLM_MOCK_MODE=false and ALLOW_REAL_PROVIDER_CALLS=true">
          <Flag on={mode.real_provider_calls_enabled} onText="Enabled (paid)" offText="Disabled" onTone="warning" />
        </Field>
        <Field label="ALLOW_REAL_PROVIDER_CALLS"><Flag on={mode.allow_real_provider_calls} onText="true" offText="false" onTone="warning" /></Field>
      </Fields>
    </Section>
  )
}

// --------------------------------------------------------------------------- C. authentication

const TAB_TEXT = (held: boolean) => <Flag on={held} onText="Entered in this tab" offText="Not entered" onTone="info" />

export function AuthenticationSection({ settings }: { settings: S }) {
  const { auth } = settings
  const tab = useCredentialStatus() // booleans only; token values never reach this component
  return (
    <Section title="Authentication" testId="section-auth"
      description="Which credentials the backend requires, and which ones this browser tab holds. Values are never shown.">
      <div className="grid grid-cols-1 gap-6 md:grid-cols-2">
        <div>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">Backend requirements</h3>
          <Fields label="Backend authentication requirements">
            <Field label="Chat (POST /api/v1/chat)" testId="auth-chat">
              {auth.chat_auth_mode === 'api_key'
                ? <span className="flex flex-col items-start gap-0.5 sm:items-end"><Badge tone="success">API key required</Badge>
                  <span className="text-xs font-normal text-slate-500">{formatInteger(auth.chat_clients_configured)} client{auth.chat_clients_configured === 1 ? '' : 's'} configured</span></span>
                : auth.chat_auth_mode === 'none' ? <Badge tone="warning">Open: no key required</Badge> : mono(auth.chat_auth_mode)}
            </Field>
            <Field label="Observability and settings endpoints" testId="auth-observability">
              <Flag on={auth.observability_token_required} onText="Token required" offText="Open: no token configured" offTone="warning" />
            </Field>
            <Field label="Admin endpoints" testId="auth-admin">
              <Flag on={auth.admin_endpoints_enabled} onText="Enabled (token per action)" offText="Disabled" onTone="info" />
            </Field>
            <Field label="Client identifiers in logs and limits" hint="How client ids are hashed before storage" testId="auth-hashing">
              {auth.client_id_hashing === 'hmac' ? <Badge tone="success">HMAC (keyed)</Badge> : <Badge tone="warning">SHA-256 (unkeyed)</Badge>}
            </Field>
          </Fields>
        </div>
        <div>
          <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">This browser tab</h3>
          <Fields label="Credentials held by this browser tab">
            <Field label="Observability token" testId="tab-observability">{TAB_TEXT(tab.observability)}</Field>
            <Field label="Chat API key" testId="tab-chat">{TAB_TEXT(tab.chat)}</Field>
            <Field label="Storage" testId="tab-storage">
              {tab.persistent ? 'This tab only (sessionStorage, opted in)' : 'Memory only (cleared on reload)'}
            </Field>
            <Field label="Admin token">Never stored (asked for each admin action)</Field>
          </Fields>
          <p className="mt-3 flex gap-1.5 text-[11px] text-slate-500">
            <Info className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
            Credentials are entered when a page needs them and are never part of the app build.
          </p>
        </div>
      </div>
    </Section>
  )
}

// --------------------------------------------------------------------------- D. providers

const CRED_TONE: Record<CredentialStatus, BadgeTone> = { configured: 'success', not_configured: 'neutral', not_required: 'neutral' }
const CRED_ICON: Record<CredentialStatus, ReactNode> = {
  configured: <CircleCheck className="h-3 w-3" aria-hidden="true" />,
  not_configured: <XCircle className="h-3 w-3" aria-hidden="true" />,
  not_required: null,
}

export function ProvidersSection({ settings }: { settings: S }) {
  const summary = credentialSummary(settings.providers)
  return (
    <Section title="Provider credentials" testId="section-providers"
      description="Whether each provider's API key is set on the server. Key values are never sent to the browser."
      actions={<Badge tone="neutral">{summary.configured} of {summary.required} configured</Badge>}>
      {settings.mode.mock_mode && (
        <p className="mb-3 rounded-md bg-violet-50 px-3 py-2 text-xs text-violet-800" data-testid="providers-mock-note">
          Mock mode is on: provider keys are not used and no provider is called.
        </p>
      )}
      {settings.providers.length === 0 ? <p className="text-sm text-slate-500">No providers reported.</p> : (
        <ul className="divide-y divide-slate-100 rounded-md border border-slate-200" aria-label="Providers">
          {settings.providers.map((p) => {
            const status = credentialStatus(p)
            return (
              <li key={p.provider} className="flex flex-col gap-2 px-3 py-2.5 sm:flex-row sm:items-start sm:justify-between sm:gap-4"
                data-testid={`provider-${p.provider}`} data-status={status}>
                <div className="min-w-0">
                  <p className="text-sm font-medium text-slate-900">{p.provider}</p>
                  <p className="text-[11px] text-slate-500">
                    {p.supported_for_real_calls ? 'Supported for real calls' : 'Not a real provider (mock / unsupported)'}
                    {p.credential_setting && <> · key setting {mono(p.credential_setting)}</>}
                  </p>
                  <p className="mt-1 flex flex-wrap gap-1">
                    {p.models.length
                      ? p.models.map((m) => <span key={m} className="rounded bg-slate-100 px-1.5 py-0.5 font-mono text-[11px] text-slate-700">{m}</span>)
                      : <span className="text-[11px] text-slate-400">No registered models</span>}
                  </p>
                </div>
                <div className="shrink-0"><Badge tone={CRED_TONE[status]} icon={CRED_ICON[status]}>{CREDENTIAL_STATUS_TEXT[status]}</Badge></div>
              </li>
            )
          })}
        </ul>
      )}
    </Section>
  )
}

// --------------------------------------------------------------------------- E. effective configuration

function Group({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div className="min-w-0 rounded-md border border-slate-200 p-3" data-testid={`config-${title}`}>
      <h3 className="mb-2 text-xs font-semibold uppercase tracking-wide text-slate-500">{title}</h3>
      <Fields label={title}>{children}</Fields>
    </div>
  )
}

export function ConfigurationSection({ settings }: { settings: S }) {
  const { routing: r, rate_limits: rl, cache: c, budget: b, persistence: p, http: h, observability: o } = settings
  const modelLimits = Object.entries(rl.model_limits)
  return (
    <Section title="Effective configuration" testId="section-config"
      description="Safe values as the backend runs them. Hosts, URLs, origins and secrets are not exposed. Changes need environment variables and a restart.">
      <div className="grid grid-cols-1 gap-4 md:grid-cols-2 2xl:grid-cols-3">
        <Group title="Routing and fallback">
          <Field label="Strategy">{mono(r.strategy)}</Field>
          <Field label="Model registry" hint="The file path is not exposed">{r.model_registry === 'built_in' ? 'Built-in' : 'Custom file'}</Field>
          <Field label="Tier escalation"><Flag on={r.tier_escalation} /></Field>
          <Field label="Fallback"><Flag on={r.fallback_enabled} /></Field>
          <Field label="Fallback across tiers"><Flag on={r.fallback_auto_tier} /></Field>
          <Field label="Fallback on explicit model"><Flag on={r.fallback_on_override} /></Field>
          <Field label="Max fallback models">{formatInteger(r.max_fallback_models)}</Field>
          <Field label="Provider timeout">{seconds(r.provider_timeout_seconds)}</Field>
          <Field label="Retries per model">{formatInteger(r.provider_max_retries)} · backoff {seconds(r.provider_retry_backoff_seconds)}</Field>
          <Field label="Max output tokens">{formatInteger(r.default_max_output_tokens)} default · {formatInteger(r.real_max_output_tokens)} real cap</Field>
          <Field label="Provider health store">{mono(r.health_backend)}</Field>
          <Field label="Unhealthy after">{formatInteger(r.health_failure_threshold)} failures · cooldown {seconds(r.health_cooldown_seconds)}</Field>
        </Group>
        <Group title="Rate limiting">
          <Field label="Enabled"><Flag on={rl.enabled} /></Field>
          <Field label="Per client, per minute">{orNoLimit(rl.per_minute)}</Field>
          <Field label="Per client, per hour">{orNoLimit(rl.per_hour)}</Field>
          <Field label="If Redis is unreachable">
            <Flag on={!rl.fail_open} onText="Reject (fail closed)" offText="Allow (fail open)" offTone="warning" />
          </Field>
          <Field label="Per-model limits">
            {modelLimits.length ? (
              <span className="flex flex-col gap-0.5 sm:items-end">
                {modelLimits.map(([m, n]) => <span key={m}>{mono(m)} {formatInteger(n)}/min</span>)}
              </span>
            ) : <Unset>None</Unset>}
          </Field>
          <Field label="Trust X-Forwarded-For"><Flag on={rl.trust_proxy_headers} onText="Yes" offText="No" onTone="info" /></Field>
          <Field label="Trusted proxies" hint="Addresses are not shown">
            {rl.trusted_proxies_configured ? `${formatInteger(rl.trusted_proxies_configured)} configured` : <Unset>None</Unset>}
          </Field>
        </Group>
        <Group title="Response cache">
          <Field label="Enabled"><Flag on={c.enabled} /></Field>
          <Field label="Semantic (approximate) hits"><Flag on={c.semantic_enabled} onTone="info" /></Field>
          <Field label="Lexical semantic matching" hint="Unsafe: can serve wrong answers">
            <Flag on={c.semantic_allow_lexical} onText="Allowed" offText="Not allowed" onTone="danger" offTone="success" />
          </Field>
          <Field label="Entry lifetime (TTL)">{seconds(c.ttl_seconds)}</Field>
          <Field label="Similarity threshold">{c.similarity_threshold}</Field>
          <Field label="Max entries per scope">{formatInteger(c.max_entries_per_scope)}</Field>
          <Field label="Embedding provider">{mono(c.embedding_provider)}</Field>
          <Field label="Embedding model">{c.embedding_model === null ? <Unset>Not applicable (local embedder)</Unset> : mono(c.embedding_model)}</Field>
          <Field label="Embedding pricing">{c.embedding_pricing_is_placeholder ? 'Placeholder' : 'Verified'}</Field>
        </Group>
        <Group title="Budget limits">
          <Field label="Configured"><Flag on={b.configured} onText="Yes" offText="No limits set" offTone="neutral" /></Field>
          <Field label="Global daily limit">{orNotConfigured(b.daily_usd, formatUsd)}</Field>
          <Field label="Per client, daily">{orNotConfigured(b.daily_usd_per_client, formatUsd)}</Field>
          <Field label="Per client, daily tokens">{orNotConfigured(b.daily_tokens_per_client, formatInteger)}</Field>
          <Field label="Per request (worst case)">{orNotConfigured(b.max_cost_per_request_usd, formatUsd)}</Field>
          <Field label="If Redis is unreachable">
            <Flag on={b.fail_closed} onText="Refuse paid calls (fail closed)" offText="Allow (fail open)" offTone="warning" />
          </Field>
          <Field label="Monthly limit"><Unset>Not supported (daily limits only)</Unset></Field>
          <Field label="Applies to"><span className="text-xs font-normal text-slate-600">{b.applies_to}</span></Field>
        </Group>
        <Group title="Persistence">
          <Field label="Redis"><Flag on={p.redis_enabled} /></Field>
          <Field label="PostgreSQL request log"><Flag on={p.database_enabled} /></Field>
          <Field label="Automatic migrations"><Flag on={p.database_auto_migrate} /></Field>
          <Field label="Store prompt text"><Flag on={p.persist_prompts} onText="Yes" offText="No" onTone="warning" offTone="success" /></Field>
          <Field label="Store response text"><Flag on={p.persist_responses} onText="Yes" offText="No" onTone="warning" offTone="success" /></Field>
        </Group>
        <Group title="HTTP security">
          <Field label="Interactive API docs"><Flag on={h.docs_enabled} onText="Exposed" offText="Off" onTone="warning" offTone="success" /></Field>
          <Field label="Security headers"><Flag on={h.security_headers_enabled} offTone="warning" /></Field>
          <Field label="HSTS"><Flag on={h.hsts_enabled} offText="Off" /></Field>
          <Field label="HSTS max-age">{h.hsts_max_age_seconds === null ? <Unset>Not applicable (HSTS off)</Unset> : seconds(h.hsts_max_age_seconds)}</Field>
          <Field label="Max request body">{formatBytes(h.max_request_body_bytes)}</Field>
          <Field label="CORS" hint="Origin values are not shown">
            {h.cors_enabled ? `${formatInteger(h.cors_origins_configured)} origin${h.cors_origins_configured === 1 ? '' : 's'} allowed` : <Unset>Off (same-origin only)</Unset>}
          </Field>
        </Group>
        <Group title="Observability">
          <Field label="Prometheus metrics"><Flag on={o.metrics_enabled} /></Field>
          <Field label="Access log"><Flag on={o.access_log_enabled} /></Field>
          <Field label="Log level / format">{mono(`${o.log_level} · ${o.log_format}`)}</Field>
          <Field label="Tracing"><Flag on={o.tracing_enabled} /></Field>
          <Field label="Trace exporter">{mono(o.tracing_exporter)}</Field>
          <Field label="Trace sampling">{formatPercent(o.tracing_sample_ratio, 0)}</Field>
          <Field label="Trust incoming traceparent"><Flag on={o.tracing_trust_incoming} onText="Yes" offText="No" onTone="warning" /></Field>
        </Group>
      </div>
    </Section>
  )
}

// --------------------------------------------------------------------------- F. production checks

export function ProductionChecksSection({ settings }: { settings: S }) {
  const { enforced, issues } = settings.production_checks
  const counts = issueCounts(issues)
  return (
    <Section title="Production configuration checks" testId="section-checks"
      description={enforced
        ? 'Enforced: in production errors stop the API from starting; staging logs them.'
        : `Informational in ${settings.app.environment}: what would block or weaken a production start. Values are never shown.`}
      actions={<Badge tone={enforced ? 'info' : 'neutral'}>{enforced ? 'Enforced' : 'Informational'}</Badge>}>
      {issues.length === 0 ? (
        <p className="flex items-center gap-2 text-sm text-emerald-700" data-testid="checks-clean">
          <ShieldCheck className="h-4 w-4" aria-hidden="true" /> No configuration issues reported.
        </p>
      ) : (
        <>
          <p className="mb-3 text-xs text-slate-500" data-testid="checks-summary">
            {counts.errors} error{counts.errors === 1 ? '' : 's'} · {counts.warnings} warning{counts.warnings === 1 ? '' : 's'}
          </p>
          <ul className="grid grid-cols-1 gap-2 lg:grid-cols-2" aria-label="Configuration issues">
            {sortedIssues(issues).map((issue) => {
              const error = issue.severity === 'error'
              return (
                <li key={`${issue.severity}-${issue.setting}-${issue.message}`} data-testid="config-issue" data-severity={issue.severity}
                  className={cn('flex gap-2.5 rounded-md border border-l-4 p-3', error ? 'border-red-200 border-l-red-500 bg-red-50/50' : 'border-amber-200 border-l-amber-500 bg-amber-50/50')}>
                  {error ? <XCircle className="mt-0.5 h-4 w-4 shrink-0 text-red-600" aria-hidden="true" />
                    : <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" aria-hidden="true" />}
                  <div className="min-w-0">
                    <p className="flex flex-wrap items-center gap-2">
                      <span className="break-all font-mono text-xs font-semibold text-slate-900">{issue.setting}</span>
                      <Badge tone={error ? 'danger' : 'warning'}>{error ? 'Error' : 'Warning'}</Badge>
                    </p>
                    <p className="mt-1 text-sm text-slate-700">{issue.message}</p>
                  </div>
                </li>
              )
            })}
          </ul>
        </>
      )}
    </Section>
  )
}

// --------------------------------------------------------------------------- G. administration

export function AdministrationSection({ settings }: { settings: S }) {
  const admin = settings.auth.admin_endpoints_enabled
  return (
    <Section title="Administrative capabilities" testId="section-admin"
      description="Informational only: this page has no controls that change the backend.">
      <Fields label="Administrative capabilities">
        <Field label="Admin endpoints" testId="admin-endpoints">
          <Flag on={admin} onText="Enabled" offText="Disabled (ADMIN_API_TOKEN not set)" onTone="info" />
        </Field>
        <Field label="Clear response cache" hint="DELETE /api/v1/cache · admin token asked for each time" testId="admin-clear-cache">
          {admin ? <Link to="/cache" className="text-brand-700 underline-offset-2 hover:underline">Available on Cache Management</Link>
            : <Unset>Unavailable while admin endpoints are disabled</Unset>}
        </Field>
        <Field label="Change configuration at runtime" testId="admin-runtime-config">
          <Unset>Not supported: set environment variables and restart the API</Unset>
        </Field>
        <Field label="Interactive API docs">
          <Flag on={settings.http.docs_enabled} onText="Exposed" offText="Off" onTone="warning" offTone="success" />
        </Field>
      </Fields>
      <p className="mt-3 flex gap-1.5 text-[11px] text-slate-500">
        <UserCog className="mt-0.5 h-3 w-3 shrink-0" aria-hidden="true" />
        Admin tokens are typed per action and never stored by this app.
      </p>
    </Section>
  )
}
