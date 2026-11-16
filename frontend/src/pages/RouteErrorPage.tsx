import { AlertTriangle, Compass, RefreshCw } from 'lucide-react'
import { isRouteErrorResponse, Link, useRouteError } from 'react-router'

import { isChunkLoadError, pageReload } from '../lib/recovery'

/**
 * Router-level error page (errorElement of the root route). It replaces the whole layout, so it
 * is self-contained: a clear title, what happened, and two ways out (Overview, full reload).
 * Error details are summarised, never dumped: messages can contain user input.
 */
export function RouteErrorPage() {
  const error = useRouteError()
  const notFound = isRouteErrorResponse(error) && error.status === 404
  const chunk = isChunkLoadError(error)

  const title = notFound ? 'Page not found' : chunk ? 'This page could not be loaded' : 'Something went wrong'
  const text = notFound
    ? 'This page does not exist.'
    : chunk
      ? 'Its code could not be downloaded: a newer version of the app may have been deployed, or the connection dropped. Reload the page to get the current version.'
      : isRouteErrorResponse(error)
        ? `The page could not be shown (HTTP ${error.status}${error.statusText ? ` ${error.statusText}` : ''}).`
        : 'An unexpected error stopped this page from rendering. Reloading usually fixes it.'

  return (
    <main className="flex min-h-full items-center justify-center bg-slate-50 px-4 py-16">
      <div role="alert" className="w-full max-w-md rounded-lg border border-slate-200 bg-white p-6 text-center shadow-sm">
        {notFound
          ? <Compass className="mx-auto mb-3 h-8 w-8 text-slate-400" aria-hidden="true" />
          : <AlertTriangle className="mx-auto mb-3 h-8 w-8 text-red-500" aria-hidden="true" />}
        <h1 className="text-base font-semibold text-slate-900">{title}</h1>
        <p className="mt-1 text-sm text-slate-500">{text}</p>
        <div className="mt-4 flex justify-center gap-2">
          <Link to="/" className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
            Go to Overview
          </Link>
          {!notFound && (
            <button type="button" onClick={() => pageReload.reload()}
              className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
              <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
              Reload page
            </button>
          )}
        </div>
      </div>
    </main>
  )
}
