/**
 * Recent activity of THIS browser session only (requests the user sends from the Chat
 * Playground). In memory, never persisted, cleared on reload. No prompt or answer text is
 * kept: metadata only. The backend has no "recent requests" endpoint, so nothing else may
 * be shown as history.
 */

import { useSyncExternalStore } from 'react'

export interface ActivityEntry {
  requestId: string | null
  at: string // ISO time the response arrived
  outcome: 'success' | 'error'
  selectedModel: string | null
  modelUsed: string | null
  provider: string | null
  complexity: string | null
  cacheStatus: string | null
  latencyMs: number | null
  totalCost: number | null
  fallbackUsed: boolean
  errorCode: string | null
}

export const MAX_ENTRIES = 20

type Listener = () => void

export class SessionActivity {
  private entries: readonly ActivityEntry[] = []
  private readonly listeners = new Set<Listener>()

  record(entry: ActivityEntry): void {
    this.entries = [entry, ...this.entries].slice(0, MAX_ENTRIES)
    for (const l of this.listeners) l()
  }

  clear(): void {
    this.entries = []
    for (const l of this.listeners) l()
  }

  list = (): readonly ActivityEntry[] => this.entries

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }
}

export const sessionActivity = new SessionActivity()

export function useSessionActivity(store: SessionActivity = sessionActivity): readonly ActivityEntry[] {
  return useSyncExternalStore(store.subscribe, store.list, store.list)
}
