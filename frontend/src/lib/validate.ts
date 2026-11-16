/**
 * Minimal runtime shape validation for API responses (no dependency).
 *
 * A Check returns null when the value is valid, otherwise a short problem description that names the
 * field PATH only - response values are never copied into messages (they end up in errors and logs).
 *
 * obj() checks EXACT keys: the backend models use extra="forbid", so a missing or unexpected field
 * means contract drift and the body is rejected rather than partially rendered. Its `shape` argument is
 * keyed by the generated type, so TypeScript also fails when a field is added to or removed from the
 * OpenAPI schema without updating the validator.
 */

export type Check = (value: unknown, path: string) => string | null

const describe = (path: string, expected: string) => `${path}: expected ${expected}`

export const str: Check = (v, p) => (typeof v === 'string' ? null : describe(p, 'string'))
export const bool: Check = (v, p) => (typeof v === 'boolean' ? null : describe(p, 'boolean'))
export const num: Check = (v, p) => (typeof v === 'number' && Number.isFinite(v) ? null : describe(p, 'finite number'))
export const nonNeg: Check = (v, p) => (typeof v === 'number' && Number.isFinite(v) && v >= 0 ? null
  : describe(p, 'number >= 0'))
export const count: Check = (v, p) => (typeof v === 'number' && Number.isInteger(v) && v >= 0 ? null
  : describe(p, 'integer >= 0'))

const DATE = /^\d{4}-\d{2}-\d{2}$/

/** YYYY-MM-DD that is a real calendar date. */
export function isIsoDate(v: unknown): v is string {
  if (typeof v !== 'string' || !DATE.test(v)) return false
  const d = new Date(`${v}T00:00:00Z`)
  return !Number.isNaN(d.getTime()) && d.toISOString().slice(0, 10) === v
}

export const isoDate: Check = (v, p) => (isIsoDate(v) ? null : describe(p, 'date YYYY-MM-DD'))
export const isoDateTime: Check = (v, p) => (typeof v === 'string' && /^\d{4}-\d{2}-\d{2}T/.test(v)
  && !Number.isNaN(Date.parse(v)) ? null : describe(p, 'ISO date-time'))

export function lit(...allowed: readonly (string | number | boolean | null)[]): Check {
  return (v, p) => (allowed.includes(v as never) ? null : describe(p, 'one of the documented values'))
}

export function nullable(check: Check): Check {
  return (v, p) => (v === null ? null : check(v, p))
}

export function arr(item: Check): Check {
  return (v, p) => {
    if (!Array.isArray(v)) return describe(p, 'array')
    for (let i = 0; i < v.length; i++) {
      const problem = item(v[i], `${p}[${i}]`)
      if (problem) return problem
    }
    return null
  }
}

export function isPlainObject(v: unknown): v is Record<string, unknown> {
  return typeof v === 'object' && v !== null && !Array.isArray(v)
}

/** Object with arbitrary keys whose values all pass `value`. */
export function dict(value: Check): Check {
  return (v, p) => {
    if (!isPlainObject(v)) return describe(p, 'object')
    for (const [k, item] of Object.entries(v)) {
      const problem = value(item, `${p}[${JSON.stringify(k).slice(0, 40)}]`)
      if (problem) return problem
    }
    return null
  }
}

/** Exact-key object. `extra` runs after the field checks (cross-field rules). */
export function obj<T>(shape: { [K in keyof T]-?: Check }, extra?: (o: Record<string, unknown>, path: string) => string | null): Check {
  const keys = Object.keys(shape) as (keyof T & string)[]
  return (v, p) => {
    if (!isPlainObject(v)) return describe(p, 'object')
    for (const k of keys) {
      if (!(k in v)) return `${p}.${k}: missing`
      const problem = (shape[k] as Check)(v[k], `${p}.${k}`)
      if (problem) return problem
    }
    const unexpected = Object.keys(v).find((k) => !(k in shape))
    if (unexpected !== undefined) return `${p}: unexpected field ${JSON.stringify(unexpected).slice(0, 40)}`
    return extra ? extra(v, p) : null
  }
}

/** Problem description for `value`, or null when it matches. */
export function explain(check: Check, value: unknown, root = 'body'): string | null {
  return check(value, root)
}
