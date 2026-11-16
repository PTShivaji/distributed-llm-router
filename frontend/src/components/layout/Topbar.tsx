import type { Ref } from 'react'
import { Menu } from 'lucide-react'
import { useLocation } from 'react-router'

import { ConnectionStatus } from './ConnectionStatus'
import { navItemFor } from './navigation'

export function Topbar({ onOpenNav, navOpen, menuButtonRef }: {
  onOpenNav: () => void
  navOpen: boolean
  /** AppShell returns focus here when the mobile drawer closes. */
  menuButtonRef?: Ref<HTMLButtonElement>
}) {
  const { pathname } = useLocation()
  const current = navItemFor(pathname)
  return (
    <header className="sticky top-0 z-20 flex h-16 items-center gap-3 border-b border-slate-200 bg-white/90 px-4 backdrop-blur sm:px-6">
      <button ref={menuButtonRef} type="button" onClick={onOpenNav} aria-label="Open navigation" aria-controls="app-sidebar"
        aria-expanded={navOpen} className="rounded-md p-2 text-slate-600 hover:bg-slate-100 lg:hidden">
        <Menu className="h-5 w-5" aria-hidden="true" />
      </button>
      <div className="min-w-0 flex-1">
        <p className="truncate text-sm font-medium text-slate-900">{current?.label ?? 'Not found'}</p>
      </div>
      <ConnectionStatus />
    </header>
  )
}
