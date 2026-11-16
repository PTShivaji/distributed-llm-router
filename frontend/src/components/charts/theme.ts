/** Shared chart styling (Recharts). */
export const CHART_COLORS = {
  success: '#10b981',
  failed: '#ef4444',
  /** Validated with the dataviz palette script alongside success/failed (contrast WARN -> charts ship a visible table). */
  skipped: '#f59e0b',
  surface: '#ffffff',
  primary: '#0284c7',
  /** Mock-mode (simulated) cost series; validated with `primary` as a 2-slot categorical pair (all checks pass, CVD ΔE 9.2). */
  mock: '#7c3aed',
  grid: '#e2e8f0',
  axis: '#64748b',
} as const

/**
 * Cache lookup split (exact hit / semantic hit / miss): categorical slots 1-3 in fixed order,
 * validated on the white card surface (all checks pass; CVD ΔE 9.2). Aqua is below 3:1 contrast,
 * so the bar always sits with visible figures (the KPI tiles) and a full accessible name.
 */
export const CACHE_SPLIT_COLORS = {
  exact: '#2a78d6',
  semantic: '#eb6834',
  miss: '#1baf7a',
} as const

export const AXIS_TICK = { fill: CHART_COLORS.axis, fontSize: 11 }

export const TOOLTIP_STYLE = {
  contentStyle: { borderRadius: 8, borderColor: '#e2e8f0', fontSize: 12, boxShadow: '0 4px 12px rgb(15 23 42 / 0.08)' },
  labelStyle: { color: '#0f172a', fontWeight: 600 },
}
