import { History } from 'lucide-react'
import { Link } from 'react-router'

import { useSessionActivity } from '../../activity/sessionActivity'
import { formatLatency, formatUsd } from '../../lib/format'
import { EmptyState } from '../feedback/States'
import { Badge } from '../ui/Badge'
import { Card, CardBody, CardHeader } from '../ui/Card'

/** Only requests sent from this browser session (the backend has no recent-requests endpoint). */
export function RecentActivityCard() {
  const entries = useSessionActivity()
  return (
    <Card role="region" aria-label="Recent activity">
      <CardHeader title="Recent activity" description="Requests sent from this browser session only. Not stored; cleared on reload." />
      <CardBody>
        {entries.length === 0 ? (
          <EmptyState icon={<History className="h-6 w-6" aria-hidden="true" />} title="No activity in this session"
            description="Requests you send from the Chat Playground appear here. Request history from other clients is not exposed by the API."
            action={<Link to="/chat" className="text-sm font-medium text-brand-700 hover:underline">Open Chat Playground</Link>} />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full min-w-[560px] text-left text-sm">
              <thead className="text-xs text-slate-500">
                <tr className="border-b border-slate-100">
                  <th className="py-2 pr-3 font-medium">Time</th>
                  <th className="py-2 pr-3 font-medium">Result</th>
                  <th className="py-2 pr-3 font-medium">Model</th>
                  <th className="py-2 pr-3 font-medium">Cache</th>
                  <th className="py-2 pr-3 text-right font-medium">Latency</th>
                  <th className="py-2 text-right font-medium">Est. cost</th>
                </tr>
              </thead>
              <tbody>
                {entries.map((e, i) => (
                  <tr key={e.requestId ?? `${e.at}-${i}`} className="border-b border-slate-50 last:border-0">
                    <td className="py-2 pr-3 text-xs text-slate-500">{new Date(e.at).toLocaleTimeString()}</td>
                    <td className="py-2 pr-3">
                      <Badge tone={e.outcome === 'success' ? 'success' : 'danger'}>{e.outcome === 'success' ? 'OK' : e.errorCode ?? 'error'}</Badge>
                      {e.fallbackUsed && <Badge tone="warning" className="ml-1">fallback</Badge>}
                    </td>
                    <td className="py-2 pr-3 font-mono text-xs">{e.modelUsed ?? e.selectedModel ?? '—'}</td>
                    <td className="py-2 pr-3 text-xs">{e.cacheStatus ?? '—'}</td>
                    <td className="py-2 pr-3 text-right text-xs tabular-nums">{formatLatency(e.latencyMs)}</td>
                    <td className="py-2 text-right text-xs tabular-nums">{formatUsd(e.totalCost)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </CardBody>
    </Card>
  )
}
