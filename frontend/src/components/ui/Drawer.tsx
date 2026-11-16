import { useEffect, useId, useRef, type ReactNode } from 'react'
import { X } from 'lucide-react'

import { trapTab } from '../../lib/focusTrap'

/**
 * Right-hand detail panel: role="dialog" + aria-modal, Escape / backdrop / close button close it,
 * focus moves to the close button, Tab stays inside the panel, and focus returns to the previously
 * focused element on close.
 * Full width on phones, fixed width from `sm` up.
 */
export function Drawer({ open, title, subtitle, onClose, children }: {
  open: boolean
  title: ReactNode
  subtitle?: ReactNode
  onClose: () => void
  children: ReactNode
}) {
  const titleId = useId()
  const closeButton = useRef<HTMLButtonElement>(null)
  const panel = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (!open) return
    const previous = document.activeElement as HTMLElement | null
    closeButton.current?.focus()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
      else trapTab(e, panel.current)
    }
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('keydown', onKey)
      if (previous?.isConnected) previous.focus()
    }
  }, [open, onClose])

  if (!open) return null
  return (
    <div className="fixed inset-0 z-50 flex justify-end">
      <div className="absolute inset-0 bg-slate-900/40" aria-hidden="true" onClick={onClose} data-testid="drawer-backdrop" />
      <div ref={panel} role="dialog" aria-modal="true" aria-labelledby={titleId}
        className="relative flex h-full w-full flex-col bg-white shadow-xl sm:max-w-lg sm:border-l sm:border-slate-200">
        <div className="flex items-start justify-between gap-4 border-b border-slate-100 px-5 py-4">
          <div className="min-w-0">
            <h2 id={titleId} className="break-all font-mono text-base font-semibold text-slate-900">{title}</h2>
            {subtitle && <div className="mt-1 text-sm text-slate-500">{subtitle}</div>}
          </div>
          <button ref={closeButton} type="button" onClick={onClose} aria-label="Close details"
            className="rounded p-1 text-slate-400 hover:bg-slate-100 hover:text-slate-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500">
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-y-auto px-5 py-4">{children}</div>
      </div>
    </div>
  )
}
