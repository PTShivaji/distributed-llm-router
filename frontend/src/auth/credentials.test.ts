import { act, renderHook } from '@testing-library/react'
import { beforeEach, describe, expect, it, vi } from 'vitest'

import { CredentialStore, useCredentialStatus } from './credentials'

const KEY = 'chat-key-for-tests-000000000001'
const OBS = 'observability-token-for-tests-01'

beforeEach(() => {
  sessionStorage.clear()
  localStorage.clear()
})

function storageDump(storage: Storage): string {
  return Array.from({ length: storage.length }, (_, i) => `${storage.key(i)}=${storage.getItem(storage.key(i)!)}`).join(';')
}

describe('CredentialStore', () => {
  it('keeps credentials in memory only by default', () => {
    const store = new CredentialStore(sessionStorage)
    store.set('chat', KEY)
    expect(store.get('chat')).toBe(KEY)
    expect(store.isPersistent()).toBe(false)
    expect(sessionStorage.length).toBe(0)
    expect(localStorage.length).toBe(0)
    expect(new CredentialStore(sessionStorage).get('chat')).toBeNull() // a reload loses it
  })

  it('trims values and treats empty input as clearing', () => {
    const store = new CredentialStore(null)
    store.set('chat', `  ${KEY}  `)
    expect(store.get('chat')).toBe(KEY)
    store.set('chat', '   ')
    expect(store.has('chat')).toBe(false)
  })

  it('persists to sessionStorage only after an explicit opt-in, and restores in the same tab', () => {
    const store = new CredentialStore(sessionStorage)
    store.set('chat', KEY)
    store.setPersistent(true)
    store.set('observability', OBS)
    expect(storageDump(sessionStorage)).toContain(KEY)
    expect(storageDump(sessionStorage)).toContain(OBS)
    const reloaded = new CredentialStore(sessionStorage)
    expect(reloaded.isPersistent()).toBe(true)
    expect(reloaded.get('chat')).toBe(KEY)
    expect(reloaded.get('observability')).toBe(OBS)
    expect(localStorage.length).toBe(0) // never localStorage
  })

  it('wipes sessionStorage on opt-out but keeps the in-memory values', () => {
    const store = new CredentialStore(sessionStorage)
    store.setPersistent(true)
    store.set('chat', KEY)
    store.setPersistent(false)
    expect(sessionStorage.length).toBe(0)
    expect(store.get('chat')).toBe(KEY)
    expect(new CredentialStore(sessionStorage).get('chat')).toBeNull()
  })

  it('clear() removes values from memory and storage', () => {
    const store = new CredentialStore(sessionStorage)
    store.setPersistent(true)
    store.set('chat', KEY)
    store.set('observability', OBS)
    store.clear('chat')
    expect(store.has('chat')).toBe(false)
    expect(storageDump(sessionStorage)).not.toContain(KEY)
    store.clear()
    expect(store.has('observability')).toBe(false)
    expect(storageDump(sessionStorage)).not.toContain(OBS)
  })

  it('stays memory-only when storage is unavailable or throws', () => {
    const noStorage = new CredentialStore(null)
    noStorage.setPersistent(true)
    expect(noStorage.isPersistent()).toBe(false)

    const throwing = { getItem: () => { throw new Error('blocked') }, setItem: () => { throw new Error('blocked') },
      removeItem: () => { throw new Error('blocked') } } as unknown as Storage
    const store = new CredentialStore(throwing)
    store.setPersistent(true)
    expect(() => store.set('chat', KEY)).not.toThrow()
    expect(store.get('chat')).toBe(KEY)
  })

  it('has no admin credential kind', () => {
    const store = new CredentialStore(null)
    // @ts-expect-error admin tokens are deliberately not storable
    store.set('admin', 'x')
  })

  it('notifies subscribers on change', () => {
    const store = new CredentialStore(null)
    const listener = vi.fn()
    const unsubscribe = store.subscribe(listener)
    store.set('chat', KEY)
    store.clear('chat')
    unsubscribe()
    store.set('chat', KEY)
    expect(listener).toHaveBeenCalledTimes(2)
  })
})

describe('useCredentialStatus', () => {
  it('reports presence (never values) and re-renders on change', () => {
    const store = new CredentialStore(null)
    const { result } = renderHook(() => useCredentialStatus(store))
    expect(result.current).toEqual({ chat: false, observability: false, persistent: false })
    act(() => store.set('observability', OBS))
    expect(result.current).toEqual({ chat: false, observability: true, persistent: false })
    expect(JSON.stringify(result.current)).not.toContain(OBS)
  })
})
