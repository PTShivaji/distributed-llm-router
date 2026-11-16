/**
 * Recovery helpers for failures that only a full page load can fix.
 *
 * Pages are code-split (React.lazy). If a page's JS chunk cannot be fetched (typically after a
 * redeploy replaced the hashed file names, or a dropped connection), React caches the rejected
 * import: re-rendering ("Try again") fails the same way forever. Reloading fetches the current
 * index.html and chunk names.
 */

const CHUNK_ERROR_PATTERNS = [
  /Failed to fetch dynamically imported module/i, // Chromium
  /error loading dynamically imported module/i, // Firefox
  /Importing a module script failed/i, // Safari
  /Unable to preload CSS/i, // Vite preload helper
  /Loading (CSS )?chunk [\w-]+ failed/i, // webpack-style wording
]

export function isChunkLoadError(error: unknown): boolean {
  if (!(error instanceof Error)) return false
  if (error.name === 'ChunkLoadError') return true
  return CHUNK_ERROR_PATTERNS.some((p) => p.test(error.message))
}

/** Indirection so tests can observe reloads (jsdom does not implement location.reload). */
export const pageReload = {
  reload(): void {
    window.location.reload()
  },
}
