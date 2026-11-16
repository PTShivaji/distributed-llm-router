import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

import type { ComplexityRow } from '../../lib/analytics'
import { formatInteger, formatPercent } from '../../lib/format'
import { AXIS_TICK, CHART_COLORS, TOOLTIP_STYLE } from './theme'

/** Single series (one hue, no legend): requests per complexity tier, with a visible data table. */
export function ComplexityChart({ rows }: { rows: ComplexityRow[] }) {
  return (
    <figure aria-label="Requests by complexity">
      <div className="h-52 w-full">
        <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 480, height: 208 }}>
          <BarChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: -16 }} barCategoryGap="30%">
            <CartesianGrid vertical={false} stroke={CHART_COLORS.grid} />
            <XAxis dataKey="complexity" tick={AXIS_TICK} tickLine={false} axisLine={false} />
            <YAxis allowDecimals={false} tick={AXIS_TICK} tickLine={false} axisLine={false} width={48} />
            <Tooltip {...TOOLTIP_STYLE} cursor={{ fill: '#f1f5f9' }} formatter={(v) => [formatInteger(Number(v)), 'Requests']} />
            <Bar dataKey="requests" name="Requests" fill={CHART_COLORS.primary} radius={[4, 4, 0, 0]} maxBarSize={56} isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <table className="mt-3 w-full text-left text-xs" aria-label="Requests by complexity (table)">
        <thead className="text-slate-500">
          <tr className="border-b border-slate-100">
            <th className="py-1.5 pr-3 font-medium">Complexity</th>
            <th className="py-1.5 pr-3 text-right font-medium">Requests</th>
            <th className="py-1.5 text-right font-medium">Share</th>
          </tr>
        </thead>
        <tbody className="text-slate-700">
          {rows.map((r) => (
            <tr key={r.complexity} className="border-b border-slate-50 last:border-0">
              <td className="py-1.5 pr-3 font-mono">{r.complexity}</td>
              <td className="py-1.5 pr-3 text-right tabular-nums">{formatInteger(r.requests)}</td>
              <td className="py-1.5 text-right tabular-nums">{formatPercent(r.share)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </figure>
  )
}
