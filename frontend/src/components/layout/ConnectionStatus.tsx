import { useBackendHealth } from '../../hooks/useBackendHealth'
import { Badge } from '../ui/Badge'

/** Real liveness check against GET /health (public, touches neither Redis nor PostgreSQL). Shared state. */
export function ConnectionStatus() {
  const { status, data } = useBackendHealth()
  if (status === 'idle' || status === 'checking') return <Badge tone="neutral">Checking backend…</Badge>
  if (status === 'unreachable' || !data) return <Badge tone="danger">Backend unreachable</Badge>
  return (
    <Badge tone="success">
      <span className="h-1.5 w-1.5 rounded-full bg-emerald-500" aria-hidden="true" />
      Connected · v{data.version} · {data.environment}
    </Badge>
  )
}
