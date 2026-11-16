import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

import type { DailyCostRow } from '../../lib/budgetDashboard'
import { formatUsd } from '../../lib/format'
import { AXIS_TICK, CHART_COLORS, TOOLTIP_STYLE } from './theme'

export const REAL_SERIES = 'Estimated cost · real calls'
export const MOCK_SERIES = 'Simulated cost · mock calls'

const axisUsd = (v: number) => (v === 0 ? '$0' : formatUsd(v))

/**
 * Cost per UTC day. Real-call estimates and mock-mode simulated costs are drawn side by side, never stacked:
 * they must not be read as one sum. Only series with a non-zero value in the range are drawn.
 */
export function DailySpendChart({ rows }: { rows: DailyCostRow[] }) {
  const hasReal = rows.some((r) => r.real > 0)
  const hasMock = rows.some((r) => r.mock > 0)
  return (
    <figure aria-label="Cost per day (UTC)">
      <div className="h-64 w-full">
        <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 640, height: 256 }}>
          <BarChart data={rows} margin={{ top: 8, right: 8, bottom: 0, left: 0 }} barCategoryGap="20%" barGap={2}>
            <CartesianGrid vertical={false} stroke={CHART_COLORS.grid} />
            <XAxis dataKey="label" tick={AXIS_TICK} tickLine={false} axisLine={false} interval="preserveStartEnd" minTickGap={16} />
            <YAxis tick={AXIS_TICK} tickLine={false} axisLine={false} width={64} tickFormatter={axisUsd} />
            <Tooltip {...TOOLTIP_STYLE} cursor={{ fill: '#f1f5f9' }} formatter={(v) => formatUsd(Number(v))}
              labelFormatter={(_l, payload) => (payload?.[0]?.payload as DailyCostRow | undefined)?.date ?? ''} />
            <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 12 }} />
            {hasReal && <Bar dataKey="real" name={REAL_SERIES} fill={CHART_COLORS.primary} radius={[3, 3, 0, 0]} isAnimationActive={false} />}
            {hasMock && <Bar dataKey="mock" name={MOCK_SERIES} fill={CHART_COLORS.mock} radius={[3, 3, 0, 0]} isAnimationActive={false} />}
          </BarChart>
        </ResponsiveContainer>
      </div>
      <figcaption className="mt-1 text-[11px] text-slate-500">
        UTC days. Bars are side by side, not stacked: simulated mock costs and estimated real costs are never added together.
      </figcaption>
    </figure>
  )
}
