import { Bar, BarChart, CartesianGrid, Legend, ResponsiveContainer, Tooltip, XAxis, YAxis } from 'recharts'

import type { HourlyPoint } from '../../lib/hourly'
import { AXIS_TICK, CHART_COLORS, TOOLTIP_STYLE } from './theme'

/** Stacked successful/failed requests per hour. Includes an accessible data table. */
export function RequestActivityChart({ points }: { points: HourlyPoint[] }) {
  return (
    <figure aria-label="Requests per hour (last 24 hours)">
      <div className="h-64 w-full">
        <ResponsiveContainer width="100%" height="100%" initialDimension={{ width: 640, height: 256 }}>
          <BarChart data={points} margin={{ top: 8, right: 8, bottom: 0, left: -16 }} barCategoryGap="20%">
            <CartesianGrid vertical={false} stroke={CHART_COLORS.grid} />
            <XAxis dataKey="label" tick={AXIS_TICK} tickLine={false} axisLine={false} interval="preserveStartEnd" minTickGap={16} />
            <YAxis allowDecimals={false} tick={AXIS_TICK} tickLine={false} axisLine={false} width={48} />
            <Tooltip {...TOOLTIP_STYLE} cursor={{ fill: '#f1f5f9' }} />
            <Legend iconType="circle" iconSize={8} wrapperStyle={{ fontSize: 12 }} />
            <Bar dataKey="successful" name="Successful" stackId="r" fill={CHART_COLORS.success} isAnimationActive={false} />
            <Bar dataKey="failed" name="Failed" stackId="r" fill={CHART_COLORS.failed} radius={[3, 3, 0, 0]} isAnimationActive={false} />
          </BarChart>
        </ResponsiveContainer>
      </div>
      <table className="sr-only">
        <caption>Requests per hour</caption>
        <thead><tr><th>Hour</th><th>Total</th><th>Successful</th><th>Failed</th></tr></thead>
        <tbody>
          {points.map((p) => (
            <tr key={p.start}><td>{p.label}</td><td>{p.total}</td><td>{p.successful}</td><td>{p.failed}</td></tr>
          ))}
        </tbody>
      </table>
    </figure>
  )
}
