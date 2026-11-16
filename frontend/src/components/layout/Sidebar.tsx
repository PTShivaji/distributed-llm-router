import { useEffect, useRef } from 'react'
import { Network, X } from 'lucide-react'
import { NavLink } from 'react-router'

import { APP_TITLE } from '../../config'
import { cn } from '../../lib/cn'
import { NAV_ITEMS } from './navigation'

/**
 * Dark navy sidebar. Desktop (lg+): always visible. Smaller screens: off-canvas drawer
 * opened from the top bar; closes on navigation, on the backdrop, with the close button and
 * with Escape (handled by AppShell). While the drawer is closed on a small screen it is `inert`:
 * its links can be neither focused nor read by assistive technology. When it opens, focus moves
 * to its close button (AppShell returns focus to the menu button when it closes).
 */
export function Sidebar({ open, onClose, desktop }: { open: boolean; onClose: () => void; desktop: boolean }) {
  const closeButton = useRef<HTMLButtonElement>(null)
  const drawer = !desktop
  const hidden = drawer && !open

  useEffect(() => {
    if (drawer && open) closeButton.current?.focus()
  }, [drawer, open])

  return (
    <>
      <div
        aria-hidden="true"
        onClick={onClose}
        className={cn('fixed inset-0 z-30 bg-slate-900/50 lg:hidden', open ? 'block' : 'hidden')}
      />
      <aside
        id="app-sidebar"
        aria-label="Main navigation"
        data-open={open}
        inert={hidden}
        className={cn(
          'fixed inset-y-0 left-0 z-40 flex w-64 flex-col border-r border-sidebar-border bg-sidebar text-slate-300',
          'transition-transform duration-200 lg:translate-x-0',
          open ? 'translate-x-0' : '-translate-x-full',
        )}
      >
        <div className="flex h-16 items-center justify-between gap-2 border-b border-sidebar-border px-5">
          <div className="flex items-center gap-2.5">
            <span className="flex h-8 w-8 items-center justify-center rounded-md bg-brand-600/20 text-brand-500">
              <Network className="h-4.5 w-4.5" aria-hidden="true" />
            </span>
            <span className="text-sm font-semibold tracking-tight text-white">{APP_TITLE}</span>
          </div>
          <button ref={closeButton} type="button" onClick={onClose} aria-label="Close navigation"
            className="rounded p-1 text-slate-400 hover:bg-sidebar-hover hover:text-white lg:hidden">
            <X className="h-4 w-4" aria-hidden="true" />
          </button>
        </div>
        <nav className="flex-1 overflow-y-auto px-3 py-4">
          <ul className="space-y-0.5">
            {NAV_ITEMS.map(({ path, label, icon: Icon }) => (
              <li key={path}>
                <NavLink
                  to={path}
                  end
                  onClick={onClose}
                  className={({ isActive }) => cn(
                    'flex items-center gap-3 rounded-md px-3 py-2 text-sm font-medium transition-colors',
                    isActive ? 'bg-sidebar-active text-white' : 'text-slate-400 hover:bg-sidebar-hover hover:text-slate-100',
                  )}
                >
                  <Icon className="h-4 w-4 shrink-0" aria-hidden="true" />
                  {label}
                </NavLink>
              </li>
            ))}
          </ul>
        </nav>
        <div className="border-t border-sidebar-border px-5 py-3 text-[11px] text-slate-500">
          Backend enforces auth, rate limits and budgets.
        </div>
      </aside>
    </>
  )
}
