import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

import type { AttemptTotals, ProviderAttemptRow } from '../../lib/analytics'
import { formatInteger, formatPercent } from '../../lib/format'
import { AXIS_TICK, CHART_COLORS, TOOLTIP_STYLE } from './theme'

const SEGMENT = { stroke: CHART_COLORS.surface, strokeWidth: 2, isAnimationActive: false, barSize: 18 } as const

/**
 * Provider attempts per provider, stacked by outcome (status colours + legend + visible table,
 * so outcome is never conveyed by colour alone).
 */
export function ProviderAttemptsChart({ rows, totals }: { rows: ProviderAttemptRow[]; totals: AttemptTotals }) {
  const height = Math.max(140, rows.length * 40 + 64)
  return (
    <figure aria-label="Provider attempts by outcome">
      <div className="w-full" style={{ height }}>
        <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 480, height }}>
          <BarChart data={rows} layout="vertical" margin={{ top: 4, right: 16, bottom: 4, left: 8 }}>
            <CartesianGrid horizontal={false} stroke={CHART_COLORS.grid} />
            <XAxis type="number" allowDecimals={false} tick={AXIS_TICK} tickLine={false} axisLine={false} />
            <YAxis type="category" dataKey="provider" width={96} tick={AXIS_TICK} tickLine={false} axisLine={false} />
            <Tooltip {...TOOLTIP_STYLE} cursor={{ fill: '#f1f5f9' }} />
            <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 12 }} itemSorter={null} /* keep stack order */ />
            <Bar dataKey="successes" name="Succeeded" stackId="a" fill={CHART_COLORS.success} {...SEGMENT} />
            <Bar dataKey="failures" name="Failed" stackId="a" fill={CHART_COLORS.failed} {...SEGMENT} />
            <Bar dataKey="skipped" name="Skipped" stackId="a" fill={CHART_COLORS.skipped} radius={[0, 4, 4, 0]} {...SEGMENT} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <div className="mt-3 overflow-x-auto">
        <table className="w-full min-w-[420px] text-left text-xs" aria-label="Provider attempts (table)">
          <thead className="text-slate-500">
            <tr className="border-b border-slate-100">
              <th className="py-1.5 pr-3 font-medium">Provider</th>
              <th className="py-1.5 pr-3 text-right font-medium">Succeeded</th>
              <th className="py-1.5 pr-3 text-right font-medium">Failed</th>
              <th className="py-1.5 pr-3 text-right font-medium">Skipped</th>
              <th className="py-1.5 pr-3 text-right font-medium">Total</th>
              <th className="py-1.5 text-right font-medium" title="Succeeded ÷ (succeeded + failed); skipped candidates were never called">Call success</th>
            </tr>
          </thead>
          <tbody className="text-slate-700">
            {rows.map((r) => (
              <tr key={r.provider} className="border-b border-slate-50">
                <td className="py-1.5 pr-3">{r.provider}</td>
                <td className="py-1.5 pr-3 text-right tabular-nums">{formatInteger(r.successes)}</td>
                <td className="py-1.5 pr-3 text-right tabular-nums">{formatInteger(r.failures)}</td>
                <td className="py-1.5 pr-3 text-right tabular-nums">{formatInteger(r.skipped)}</td>
                <td className="py-1.5 pr-3 text-right tabular-nums">{formatInteger(r.total)}</td>
                <td className="py-1.5 text-right tabular-nums">{formatPercent(r.successRate)}</td>
              </tr>
            ))}
          </tbody>
          {rows.length > 1 && (
            <tfoot className="font-medium text-slate-900">
              <tr>
                <td className="py-1.5 pr-3">All providers</td>
                <td className="py-1.5 pr-3 text-right tabular-nums">{formatInteger(totals.successes)}</td>
                <td className="py-1.5 pr-3 text-right tabular-nums">{formatInteger(totals.failures)}</td>
                <td className="py-1.5 pr-3 text-right tabular-nums">{formatInteger(totals.skipped)}</td>
                <td className="py-1.5 pr-3 text-right tabular-nums">{formatInteger(totals.total)}</td>
                <td className="py-1.5 text-right tabular-nums">{formatPercent(totals.successes + totals.failures > 0 ? totals.successes / (totals.successes + totals.failures) : null)}</td>
              </tr>
            </tfoot>
          )}
        </table>
      </div>
    </figure>
  )
}
