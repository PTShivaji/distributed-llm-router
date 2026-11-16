import type { CacheStatsResponse } from '../../api/types'
import { lookupSplit, type LookupPart } from '../../lib/cache'
import { formatInteger, formatPercent } from '../../lib/format'
import { CACHE_SPLIT_COLORS } from '../charts/theme'

/**
 * Thin segmented bar: how the measured lookups split into exact hits, semantic hits and misses.
 * It sits under the KPI tiles that already print every figure, so the bar carries no text; the
 * legend names the parts in block order, and the accessible name / hover title give each part's
 * count and share. Shown only for real measurements (cache enabled and Redis read); a part at
 * zero draws nothing. Cumulative counters only: the backend reports no history, so no trend.
 */
export function CacheLookupSplit({ stats }: { stats: CacheStatsResponse }) {
  const split = lookupSplit(stats)
  if (!split) return null
  const { parts, total } = split
  if (total === 0) {
    return <p className="text-xs text-slate-500" data-testid="lookup-split-empty">No cache lookups yet: the hit/miss split appears after the first chat request.</p>
  }
  const describe = (p: LookupPart) => `${p.label} ${formatInteger(p.value)} (${formatPercent(p.value / total)})`
  const visible = parts.filter((p) => p.value > 0)
  return (
    <figure className="space-y-2" data-testid="lookup-split">
      <figcaption className="text-xs font-medium text-slate-600">
        Lookup split <span className="font-normal text-slate-500">· {formatInteger(total)} lookups</span>
      </figcaption>
      <div role="img" aria-label={`Cache lookups: ${parts.map(describe).join(', ')}, of ${formatInteger(total)} lookups`}
        className="flex h-2 w-full gap-0.5 overflow-hidden rounded-full bg-white">
        {visible.map((p) => (
          <span key={p.key} title={describe(p)} data-testid={`lookup-split-${p.key}`}
            className="h-full first:rounded-l-full last:rounded-r-full"
            style={{ flexGrow: p.value, flexBasis: 0, backgroundColor: CACHE_SPLIT_COLORS[p.key] }} />
        ))}
      </div>
      <ul className="flex flex-wrap gap-x-4 gap-y-1 text-xs text-slate-600" aria-label="Legend">
        {parts.map((p) => (
          <li key={p.key} className="flex items-center gap-1.5">
            <span className="h-2 w-2 rounded-full" style={{ backgroundColor: CACHE_SPLIT_COLORS[p.key] }} aria-hidden="true" />
            {p.label}
          </li>
        ))}
      </ul>
    </figure>
  )
}
