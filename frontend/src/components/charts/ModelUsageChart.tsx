import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

import { formatInteger, formatLatency, formatUsd } from '../../lib/format'
import type { ModelUsageRow } from '../../lib/overview'
import { AXIS_TICK, CHART_COLORS, TOOLTIP_STYLE } from './theme'

/** Horizontal bars: requests per model (from analytics `requests_by_model`). Includes a data table. */
export function ModelUsageChart({ rows }: { rows: ModelUsageRow[] }) {
  const height = Math.max(160, rows.length * 36 + 40)
  return (
    <figure aria-label="Requests by model">
      <div className="w-full" style={{ height }}>
        <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 480, height }}>
          <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 16, bottom: 4, left: 8 }}>
            <CartesianGrid horizontal={false} stroke={CHART_COLORS.grid} />
            <XAxis type="number" allowDecimals={false} tick={AXIS_TICK} tickLine={false} axisLine={false} />
            <YAxis type="category" dataKey="model" width={120} tick={AXIS_TICK} tickLine={false} axisLine={false} />
            <Tooltip {...TOOLTIP_STYLE} cursor={{ fill: '#f1f5f9' }} />
            <Bar dataKey="requests" name="Requests" fill={CHART_COLORS.primary} radius={[0, 3, 3, 0]} barSize={18} isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full min-w-[480px] text-left text-xs">
          <thead className="text-slate-500">
            <tr className="border-b border-slate-100">
              <th className="py-2 pr-3 font-medium">Model</th>
              <th className="py-2 pr-3 font-medium">Provider</th>
              <th className="py-2 pr-3 text-right font-medium">Requests</th>
              <th className="py-2 pr-3 text-right font-medium">Tokens</th>
              <th className="py-2 pr-3 text-right font-medium">Est. cost</th>
              <th className="py-2 text-right font-medium">Avg latency</th>
            </tr>
          </thead>
          <tbody className="text-slate-700">
            {rows.map((r) => (
              <tr key={r.model} className="border-b border-slate-50 last:border-0">
                <td className="py-2 pr-3 font-mono">{r.model}</td>
                <td className="py-2 pr-3">{r.provider ?? <span className="text-slate-400">not in registry</span>}</td>
                <td className="py-2 pr-3 text-right tabular-nums">{formatInteger(r.requests)}</td>
                <td className="py-2 pr-3 text-right tabular-nums">{formatInteger(r.totalTokens)}</td>
                <td className="py-2 pr-3 text-right tabular-nums">{formatUsd(r.estimatedCost)}</td>
                <td className="py-2 text-right tabular-nums">{formatLatency(r.averageLatencyMs)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </figure>
  )
}
