import { Component, type ErrorInfo, type ReactNode } from 'react'
import { AlertTriangle, RefreshCw } from 'lucide-react'

import { isChunkLoadError, pageReload } from '../../lib/recovery'

interface Props {
  children: ReactNode
  /** Changing this value resets the boundary (e.g. the current route path). */
  resetKey?: unknown
  fallback?: (reset: () => void) => ReactNode
}

interface State {
  failed: boolean
  /** The page's code could not be downloaded: only a full reload can recover. */
  chunkError: boolean
  lastResetKey: unknown
}

/** Catches render errors so one broken view never blanks the whole application. */
export class ErrorBoundary extends Component<Props, State> {
  override state: State = { failed: false, chunkError: false, lastResetKey: this.props.resetKey }

  static getDerivedStateFromError(error: unknown): Partial<State> {
    return { failed: true, chunkError: isChunkLoadError(error) }
  }

  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    return props.resetKey !== state.lastResetKey ? { failed: false, chunkError: false, lastResetKey: props.resetKey } : null
  }

  override componentDidCatch(error: Error, info: ErrorInfo): void {
    // Error name only: messages/stacks may contain user input; never log credentials.
    console.error('UI error boundary caught', error.name, info.componentStack ? '(component stack available)' : '')
  }

  reset = (): void => this.setState({ failed: false, chunkError: false })

  override render(): ReactNode {
    if (!this.state.failed) return this.props.children
    if (this.props.fallback) return this.props.fallback(this.reset)
    const chunk = this.state.chunkError
    return (
      <div role="alert" className="mx-auto my-16 max-w-md rounded-lg border border-red-200 bg-white p-6 text-center shadow-sm">
        <AlertTriangle className="mx-auto mb-3 h-8 w-8 text-red-500" aria-hidden="true" />
        <h2 className="text-base font-semibold text-slate-900">{chunk ? 'This page could not be loaded' : 'This view failed to render'}</h2>
        <p className="mt-1 text-sm text-slate-500">
          {chunk
            ? 'Its code could not be downloaded: a newer version of the app may have been deployed, or the connection dropped. Reload the page to get the current version.'
            : 'The rest of the application still works. Try again, reload the page, or pick another page.'}
        </p>
        <div className="mt-4 flex justify-center gap-2">
          {/* A failed lazy import is cached by React: "Try again" cannot recover it, so it is not offered. */}
          {!chunk && (
            <button type="button" onClick={this.reset}
              className="rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
              Try again
            </button>
          )}
          <button type="button" onClick={() => pageReload.reload()}
            className="inline-flex items-center gap-1.5 rounded-md border border-slate-300 px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50">
            <RefreshCw className="h-3.5 w-3.5" aria-hidden="true" />
            Reload page
          </button>
        </div>
      </div>
    )
  }
}
