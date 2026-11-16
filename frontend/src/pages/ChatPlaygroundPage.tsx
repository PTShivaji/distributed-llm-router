import { useEffect, useRef, useState } from 'react'
import { MessageSquareText, Trash2 } from 'lucide-react'

import { api } from '../api/endpoints'
import { normalizeError } from '../api/errors'
import { chatSession, sendChat, useChatTurns, type ChatTurn } from '../chat/chatSession'
import { ChatComposer } from '../components/chat/ChatComposer'
import { ChatTurnView } from '../components/chat/ChatTurnView'
import { ClassificationPreview, type PreviewState } from '../components/chat/ClassificationPreview'
import { RoutingOptions } from '../components/chat/RoutingOptions'
import { EmptyState } from '../components/feedback/States'
import { ConnectionStatus } from '../components/layout/ConnectionStatus'
import { Button } from '../components/ui/Button'
import { Card, CardHeader } from '../components/ui/Card'
import { Dialog } from '../components/ui/Dialog'
import { PageHeader } from '../components/ui/PageHeader'
import { useApiQuery } from '../hooks/useApiQuery'
import { describeChatError, EXAMPLE_PROMPTS, queryProblem, validOverride } from '../lib/chat'

/**
 * Chat Playground: POST /api/v1/chat (one request per send, never retried automatically),
 * optional POST /api/v1/classify preview, GET /api/v1/models for the override list.
 * The conversation lives in this tab's memory only (chatSession); nothing is persisted.
 */
export function ChatPlaygroundPage() {
  const models = useApiQuery((signal) => api.models({ signal }))
  const turns = useChatTurns()
  const [draft, setDraft] = useState('')
  const [override, setOverride] = useState<string | null>(null)
  const [useCache, setUseCache] = useState(true)
  const [preview, setPreview] = useState<PreviewState | null>(null)
  const [confirmClear, setConfirmClear] = useState(false)
  const input = useRef<HTMLTextAreaElement>(null)
  const listEnd = useRef<HTMLDivElement>(null)
  const classifyAbort = useRef<AbortController | null>(null)

  const busy = turns.some((t) => t.status === 'pending')
  // An override that is no longer registered + enabled silently falls back to automatic routing.
  const effectiveOverride = validOverride(override, models.data?.models ?? null)

  useEffect(() => () => classifyAbort.current?.abort(), [])
  useEffect(() => {
    listEnd.current?.scrollIntoView?.({ block: 'end', behavior: 'smooth' })
  }, [turns.length])

  async function send(query: string, model: string | null) {
    const id = await sendChat({ query, model, useCache }, { api })
    const turn = chatSession.list().find((t) => t.id === id)
    // The registry may have changed (model removed/disabled): refresh the override list.
    if (turn?.error && describeChatError(turn.error).modelProblem) models.reload()
  }

  function submitDraft() {
    if (busy || queryProblem(draft) !== null) return
    const query = draft
    setDraft('')
    closePreview() // the preview described this draft; the backend now classifies it authoritatively
    void send(query, effectiveOverride)
  }

  /** Explicit user retry: same prompt, CURRENT routing options (e.g. after choosing another model or setting a key). */
  function retry(turn: ChatTurn) {
    if (busy) return
    void send(turn.query, effectiveOverride)
  }

  function edit(turn: ChatTurn) {
    setDraft(turn.query)
    input.current?.focus()
  }

  async function runPreview() {
    const query = draft
    if (queryProblem(query) !== null) return
    classifyAbort.current?.abort()
    const controller = new AbortController()
    classifyAbort.current = controller
    setPreview({ query, loading: true, result: null, error: null })
    try {
      const { data } = await api.classify({ query }, { signal: controller.signal })
      if (!controller.signal.aborted) setPreview({ query, loading: false, result: data, error: null })
    } catch (err) {
      const error = normalizeError(err)
      if (!controller.signal.aborted && error.kind !== 'cancelled') setPreview({ query, loading: false, result: null, error })
    }
  }

  function closePreview() {
    classifyAbort.current?.abort()
    setPreview(null)
  }

  function applyExample(prompt: string) {
    setDraft(prompt)
    input.current?.focus()
  }

  return (
    <div className="space-y-6">
      <PageHeader
        title="Chat Playground"
        description="Send a prompt through the router and see how it was classified, routed, answered, and what it cost. The backend makes every routing, fallback and budget decision."
        actions={<span className="hidden sm:inline-flex"><ConnectionStatus /></span>}
      />

      <div className="grid grid-cols-1 gap-6 xl:grid-cols-[minmax(0,1fr)_340px]" data-testid="chat-layout">
        <aside className="min-w-0 space-y-6 xl:order-2" aria-label="Routing options">
          <RoutingOptions registry={models.data} registryError={models.data ? null : models.error} onReloadRegistry={models.reload}
            override={effectiveOverride} onOverride={setOverride} useCache={useCache} onUseCache={setUseCache} disabled={busy} />
        </aside>

        <Card className="flex min-w-0 flex-col xl:order-1">
          <CardHeader title="Conversation"
            description="Kept in this browser tab's memory only: not stored, not sent anywhere except the chat request. Cleared on reload."
            actions={(
              <Button variant="ghost" size="sm" onClick={() => setConfirmClear(true)} disabled={turns.length === 0 || busy}
                icon={<Trash2 className="h-3.5 w-3.5" aria-hidden="true" />}>Clear conversation</Button>
            )} />
          <div className="min-h-[16rem] flex-1 px-4 py-4 xl:max-h-[calc(100vh-20rem)] xl:overflow-y-auto" data-testid="conversation">
            {turns.length === 0 ? (
              <EmptyState icon={<MessageSquareText className="h-6 w-6" aria-hidden="true" />} title="Start a conversation"
                description="Type a prompt below or pick an example. Each send is one POST /api/v1/chat; the response shows the routing, fallback, cache, token and cost details."
                action={(
                  <ul className="grid grid-cols-1 gap-2 sm:grid-cols-2" aria-label="Example prompts">
                    {EXAMPLE_PROMPTS.map((e) => (
                      <li key={e.label}>
                        <button type="button" onClick={() => applyExample(e.prompt)}
                          className="h-full w-full rounded-md border border-slate-200 bg-white p-3 text-left text-xs shadow-sm hover:border-brand-300 hover:bg-brand-50/40 focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500">
                          <span className="block font-semibold text-slate-800">{e.label}</span>
                          <span className="mt-0.5 block text-slate-500">{e.prompt}</span>
                        </button>
                      </li>
                    ))}
                  </ul>
                )} />
            ) : (
              <ol className="space-y-6" aria-label="Conversation">
                {turns.map((t, i) => (
                  <ChatTurnView key={t.id} turn={t} latest={i === turns.length - 1} busy={busy} onRetry={retry} onEdit={edit} />
                ))}
              </ol>
            )}
            <div ref={listEnd} />
          </div>
          <div className="space-y-3 border-t border-slate-100 px-4 py-3">
            {preview && <ClassificationPreview state={preview} currentQuery={draft} onClose={closePreview} onRetry={runPreview} />}
            <ChatComposer ref={input} value={draft} onChange={setDraft} onSend={submitDraft} onPreview={runPreview}
              sending={busy} previewing={preview?.loading ?? false} />
          </div>
        </Card>
      </div>

      <Dialog open={confirmClear} title="Clear conversation?" onClose={() => setConfirmClear(false)}
        description="All prompts and responses in this tab are removed. They are not stored anywhere else, so this cannot be undone."
        footer={(
          <>
            <Button variant="secondary" size="sm" onClick={() => setConfirmClear(false)}>Cancel</Button>
            <Button variant="danger" size="sm" onClick={() => { chatSession.clear(); setConfirmClear(false) }}>Clear conversation</Button>
          </>
        )} />
    </div>
  )
}
