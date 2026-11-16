/**
 * Runtime credential store for the two credentials the UI may hold between requests:
 *
 *   chat           -> CHAT_API_KEYS bearer key for POST /api/v1/chat
 *   observability  -> OBSERVABILITY_API_TOKEN for /metrics, analytics, cache stats, dependencies
 *
 * Default: memory only (gone on reload). sessionStorage is used ONLY after an explicit
 * opt-in (`setPersistent(true)`), is scoped to the browser tab, and is wiped on opt-out.
 * localStorage is never used. The ADMIN token is deliberately NOT a credential kind:
 * it is entered in the cache-clear confirmation, used for that one request and discarded.
 */

import { useSyncExternalStore } from 'react'

export type CredentialKind = 'chat' | 'observability'
export const CREDENTIAL_KINDS: readonly CredentialKind[] = ['chat', 'observability']

const KEY_PREFIX = 'dar.credential.'
const PERSIST_FLAG = 'dar.credential.persist'

type Listener = () => void

function safeSessionStorage(): Storage | null {
  try {
    return typeof window !== 'undefined' ? window.sessionStorage : null
  } catch {
    return null // storage blocked (privacy mode / sandboxed iframe)
  }
}

export class CredentialStore {
  private readonly tokens = new Map<CredentialKind, string>()
  private readonly listeners = new Set<Listener>()
  private persistent = false
  private version = 0

  constructor(private readonly storage: Storage | null = safeSessionStorage()) {
    // Restore only if the user opted in earlier in THIS tab.
    if (this.read(PERSIST_FLAG) === '1') {
      this.persistent = true
      for (const kind of CREDENTIAL_KINDS) {
        const value = this.read(KEY_PREFIX + kind)
        if (value) this.tokens.set(kind, value)
      }
    }
  }

  get(kind: CredentialKind): string | null {
    return this.tokens.get(kind) ?? null
  }

  has(kind: CredentialKind): boolean {
    return this.tokens.has(kind)
  }

  /** Empty or whitespace-only clears the credential. */
  set(kind: CredentialKind, value: string): void {
    const token = value.trim()
    if (!token) return this.clear(kind)
    this.tokens.set(kind, token)
    if (this.persistent) this.write(KEY_PREFIX + kind, token)
    this.emit()
  }

  /** Clears one credential, or all of them. */
  clear(kind?: CredentialKind): void {
    const kinds = kind ? [kind] : CREDENTIAL_KINDS
    for (const k of kinds) {
      this.tokens.delete(k)
      this.remove(KEY_PREFIX + k)
    }
    this.emit()
  }

  isPersistent(): boolean {
    return this.persistent
  }

  /** Explicit opt-in to keep credentials in sessionStorage for this tab; opt-out wipes them. */
  setPersistent(on: boolean): void {
    this.persistent = on && this.storage !== null
    if (this.persistent) {
      this.write(PERSIST_FLAG, '1')
      for (const [kind, token] of this.tokens) this.write(KEY_PREFIX + kind, token)
    } else {
      this.remove(PERSIST_FLAG)
      for (const kind of CREDENTIAL_KINDS) this.remove(KEY_PREFIX + kind)
    }
    this.emit()
  }

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  /** Changes on every update; used by useSyncExternalStore. Never exposes token values. */
  getVersion = (): number => this.version

  private emit(): void {
    this.version += 1
    for (const listener of this.listeners) listener()
  }

  private read(key: string): string | null {
    try {
      return this.storage?.getItem(key) ?? null
    } catch {
      return null
    }
  }

  private write(key: string, value: string): void {
    try {
      this.storage?.setItem(key, value)
    } catch {
      /* quota / blocked: stay memory-only */
    }
  }

  private remove(key: string): void {
    try {
      this.storage?.removeItem(key)
    } catch {
      /* ignore */
    }
  }
}

export const credentialStore = new CredentialStore()

/** Re-renders when credentials change. Returns whether each credential is set, never the value. */
export function useCredentialStatus(store: CredentialStore = credentialStore) {
  useSyncExternalStore(store.subscribe, store.getVersion, store.getVersion)
  return {
    chat: store.has('chat'),
    observability: store.has('observability'),
    persistent: store.isPersistent(),
  }
}
