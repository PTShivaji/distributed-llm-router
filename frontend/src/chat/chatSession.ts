/**
 * Chat Playground conversation, held in THIS tab's memory only.
 *
 * - Never written to localStorage/sessionStorage and never sent anywhere except the one
 *   POST /api/v1/chat per turn. Cleared on reload or with "Clear conversation".
 * - Lives outside the page component, so the conversation survives switching pages and a
 *   request that is in flight when the user navigates away still lands here (it is NOT
 *   aborted: the backend would process and bill it anyway).
 * - Each send is exactly one request. There is no automatic retry anywhere; a retry is a new,
 *   explicit user action that creates a new turn.
 */

import { useSyncExternalStore } from 'react'

import { newRequestId, type ResponseMeta } from '../api/client'
import type { Endpoints } from '../api/endpoints'
import { normalizeError, type ApiError } from '../api/errors'
import type { ChatResponse } from '../api/types'
import { sessionActivity, type SessionActivity } from '../activity/sessionActivity'
import { buildChatRequest } from '../lib/chat'

export interface ChatTurn {
  id: string
  query: string
  /** Model override sent with the request; null = automatic routing. */
  requestedModel: string | null
  useCache: boolean
  /** X-Request-ID sent with the request. */
  clientRequestId: string
  sentAt: string
  status: 'pending' | 'success' | 'error'
  receivedAt: string | null
  response: ChatResponse | null
  meta: ResponseMeta | null
  error: ApiError | null
}

/** Oldest turns are dropped beyond this (memory bound for long sessions). */
export const MAX_TURNS = 50

type Listener = () => void

export class ChatSession {
  private turns: readonly ChatTurn[] = []
  private readonly listeners = new Set<Listener>()
  private counter = 0

  list = (): readonly ChatTurn[] => this.turns

  subscribe = (listener: Listener): (() => void) => {
    this.listeners.add(listener)
    return () => this.listeners.delete(listener)
  }

  isPending(): boolean {
    return this.turns.some((t) => t.status === 'pending')
  }

  clear(): void {
    this.turns = []
    this.emit()
  }

  nextId(): string {
    this.counter += 1
    return `turn-${this.counter}`
  }

  add(turn: ChatTurn): void {
    this.turns = [...this.turns, turn].slice(-MAX_TURNS)
    this.emit()
  }

  /** No-op if the turn was cleared meanwhile. */
  update(id: string, patch: Partial<ChatTurn>): void {
    if (!this.turns.some((t) => t.id === id)) return
    this.turns = this.turns.map((t) => (t.id === id ? { ...t, ...patch } : t))
    this.emit()
  }

  private emit(): void {
    for (const l of this.listeners) l()
  }
}

export const chatSession = new ChatSession()

export function useChatTurns(store: ChatSession = chatSession): readonly ChatTurn[] {
  return useSyncExternalStore(store.subscribe, store.list, store.list)
}

export interface SendInput {
  query: string
  model: string | null
  useCache: boolean
}

/**
 * Sends ONE chat request and records the outcome. Refuses to start while another turn is
 * pending (no double submission). Returns the turn id, or null if nothing was sent.
 */
export async function sendChat(input: SendInput, deps: { api: Pick<Endpoints, 'chat'>; session?: ChatSession; activity?: SessionActivity }):
  Promise<string | null> {
  const session = deps.session ?? chatSession
  const activity = deps.activity ?? sessionActivity
  if (session.isPending()) return null
  const id = session.nextId()
  const clientRequestId = newRequestId()
  session.add({
    id, query: input.query, requestedModel: input.model, useCache: input.useCache, clientRequestId,
    sentAt: new Date().toISOString(), status: 'pending', receivedAt: null, response: null, meta: null, error: null,
  })
  try {
    const { data, meta } = await deps.api.chat(buildChatRequest(input), { requestId: clientRequestId })
    const receivedAt = new Date().toISOString()
    session.update(id, { status: 'success', response: data, meta, receivedAt })
    activity.record({
      requestId: data.request_id || meta.requestId || clientRequestId, at: receivedAt, outcome: 'success',
      selectedModel: data.selected_model, modelUsed: data.model_used, provider: data.provider,
      complexity: data.classification.complexity, cacheStatus: data.cache.status, latencyMs: data.latency_ms,
      totalCost: data.cost.total_cost, fallbackUsed: data.fallback.used, errorCode: null,
    })
  } catch (err) {
    const error = normalizeError(err)
    const receivedAt = new Date().toISOString()
    session.update(id, { status: 'error', error, receivedAt })
    activity.record({
      requestId: error.requestId ?? clientRequestId, at: receivedAt, outcome: 'error', selectedModel: input.model,
      modelUsed: null, provider: null, complexity: null, cacheStatus: null, latencyMs: null, totalCost: null,
      fallbackUsed: false, errorCode: error.code,
    })
  }
  return id
}
