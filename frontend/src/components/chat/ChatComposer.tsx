import type { KeyboardEvent, Ref } from 'react'
import { ScanSearch, Send } from 'lucide-react'

import { MAX_QUERY_LENGTH, queryProblem } from '../../lib/chat'
import { cn } from '../../lib/cn'
import { formatInteger } from '../../lib/format'
import { Button } from '../ui/Button'

/** Multiline prompt input. Enter sends, Shift+Enter inserts a new line (IME composition is respected). */
export function ChatComposer({ ref, value, onChange, onSend, onPreview, sending, previewing }: {
  ref?: Ref<HTMLTextAreaElement>
  value: string
  onChange: (v: string) => void
  onSend: () => void
  onPreview: () => void
  sending: boolean
  previewing: boolean
}) {
  const problem = queryProblem(value)
  const canSend = problem === null && !sending

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key !== 'Enter' || e.shiftKey || e.nativeEvent.isComposing) return
    e.preventDefault()
    if (canSend) onSend()
  }

  return (
    <form className="space-y-2" aria-label="Send a prompt" onSubmit={(e) => { e.preventDefault(); if (canSend) onSend() }}>
      <label htmlFor="chat-prompt" className="sr-only">Prompt</label>
      <textarea id="chat-prompt" ref={ref} value={value} onChange={(e) => onChange(e.target.value)} onKeyDown={onKeyDown}
        rows={3} placeholder="Ask anything… (Enter to send, Shift+Enter for a new line)" spellCheck
        aria-describedby="chat-prompt-help" aria-invalid={problem === 'too_long' || undefined}
        className={cn('block max-h-64 min-h-[5.5rem] w-full resize-y rounded-md border bg-white px-3 py-2 text-sm text-slate-900 shadow-sm placeholder:text-slate-400 focus:outline-none focus:ring-1',
          problem === 'too_long' ? 'border-red-400 focus:border-red-500 focus:ring-red-500' : 'border-slate-300 focus:border-brand-500 focus:ring-brand-500')} />
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p id="chat-prompt-help" className={cn('text-[11px] tabular-nums', problem === 'too_long' ? 'font-medium text-red-600' : 'text-slate-400')}>
          {formatInteger(value.length)} / {formatInteger(MAX_QUERY_LENGTH)} characters
          {problem === 'too_long' && ' (too long to send)'}
          <span className="hidden sm:inline"> · Enter to send · Shift+Enter for a new line</span>
        </p>
        <div className="flex gap-2">
          <Button type="button" variant="secondary" size="sm" onClick={onPreview} disabled={problem !== null} loading={previewing}
            icon={<ScanSearch className="h-3.5 w-3.5" aria-hidden="true" />}>Preview classification</Button>
          <Button type="submit" size="sm" disabled={!canSend} loading={sending}
            icon={<Send className="h-3.5 w-3.5" aria-hidden="true" />}>{sending ? 'Sending…' : 'Send'}</Button>
        </div>
      </div>
    </form>
  )
}
