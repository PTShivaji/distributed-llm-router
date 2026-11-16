import { Suspense, useCallback, useEffect, useRef, useState } from 'react'
import { Outlet, useLocation } from 'react-router'

import { DESKTOP_QUERY, useMediaQuery } from '../../hooks/useMediaQuery'
import { ErrorBoundary } from '../feedback/ErrorBoundary'
import { LoadingState } from '../feedback/States'
import { Sidebar } from './Sidebar'
import { Topbar } from './Topbar'

export function AppShell() {
  const [navOpen, setNavOpen] = useState(false)
  // Where matchMedia is unavailable the sidebar is treated as the always-visible desktop variant.
  const desktop = useMediaQuery(DESKTOP_QUERY, true)
  const menuButton = useRef<HTMLButtonElement>(null)
  const close = useCallback(() => setNavOpen(false), [])
  const { pathname } = useLocation()

  // Mobile drawer: Escape closes it.
  useEffect(() => {
    if (!navOpen || desktop) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setNavOpen(false)
    }
    document.addEventListener('keydown', onKey)
    return () => document.removeEventListener('keydown', onKey)
  }, [navOpen, desktop])

  // Mobile drawer: when it closes, focus returns to the menu button (unless the user already
  // moved focus somewhere meaningful outside the drawer).
  const wasOpen = useRef(false)
  useEffect(() => {
    if (wasOpen.current && !navOpen && !desktop) {
      const active = document.activeElement
      const sidebar = document.getElementById('app-sidebar')
      if (!active || active === document.body || sidebar?.contains(active)) menuButton.current?.focus()
    }
    wasOpen.current = navOpen
  }, [navOpen, desktop])

  return (
    <div className="min-h-full bg-slate-50">
      <a href="#main" className="sr-only focus:not-sr-only focus:fixed focus:left-2 focus:top-2 focus:z-50 focus:rounded focus:bg-white focus:px-3 focus:py-2">
        Skip to content
      </a>
      <Sidebar open={navOpen} onClose={close} desktop={desktop} />
      <div className="lg:pl-64">
        <Topbar onOpenNav={() => setNavOpen(true)} navOpen={navOpen} menuButtonRef={menuButton} />
        <main id="main" className="mx-auto w-full max-w-7xl px-4 py-6 sm:px-6 lg:px-8">
          {/* resetKey: a failed view recovers when the user navigates elsewhere */}
          <ErrorBoundary resetKey={pathname}>
            <Suspense fallback={<LoadingState label="Loading page…" />}>
              <Outlet />
            </Suspense>
          </ErrorBoundary>
        </main>
      </div>
    </div>
  )
}
