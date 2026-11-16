import '@testing-library/jest-dom/vitest'
import { cleanup, configure } from '@testing-library/react'
import { afterAll, afterEach, beforeAll } from 'vitest'

import { sessionActivity } from '../activity/sessionActivity'
import { credentialStore } from '../auth/credentials'
import { chatSession } from '../chat/chatSession'
import { resetBackendHealth } from '../hooks/useBackendHealth'
import { server } from './server'

// findBy*/waitFor budget: the default 1 s is too tight for a lazy page's first render on a busy machine.
// This only changes how long to WAIT for an expected state, never what is asserted.
configure({ asyncUtilTimeout: 3000 })

// jsdom has no ResizeObserver (used by Recharts' ResponsiveContainer); charts use initialDimension.
class ResizeObserverStub {
  observe() {}
  unobserve() {}
  disconnect() {}
}
globalThis.ResizeObserver ??= ResizeObserverStub as unknown as typeof ResizeObserver

// jsdom does no layout: every element measures 0x0, so Recharts' ResponsiveContainer re-measures itself
// to 0x0 right after mount and logs "width(0) and height(0) of chart should be greater than 0". Give
// chart containers (and ONLY them) the size a laid-out page would have; every other element is unchanged.
const CHART_RECT = { x: 0, y: 0, top: 0, left: 0, width: 640, height: 256, right: 640, bottom: 256 }
const realGetBoundingClientRect = HTMLElement.prototype.getBoundingClientRect
HTMLElement.prototype.getBoundingClientRect = function getBoundingClientRect(this: HTMLElement): DOMRect {
  if (this.classList.contains('recharts-responsive-container')) return { ...CHART_RECT, toJSON: () => CHART_RECT } as DOMRect
  return realGetBoundingClientRect.call(this)
}

beforeAll(() => {
  server.listen({ onUnhandledRequest: 'error' })
  // Lazy pages are preloaded only by files that render the app (see render.tsx), not by every test file.
})

afterEach(() => {
  cleanup()
  server.resetHandlers()
  credentialStore.setPersistent(false)
  credentialStore.clear()
  sessionActivity.clear()
  chatSession.clear()
  resetBackendHealth()
  window.sessionStorage.clear()
  window.localStorage.clear()
})

afterAll(() => server.close())
