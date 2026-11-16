import { useId, useState, type FormEvent } from 'react'
import { KeyRound } from 'lucide-react'

import { credentialStore, type CredentialKind } from '../../auth/credentials'
import { Button } from '../ui/Button'

const LABELS: Record<CredentialKind, { title: string; field: string; setting: string }> = {
  observability: { title: 'Observability token required', field: 'Observability token', setting: 'OBSERVABILITY_API_TOKEN' },
  chat: { title: 'Chat API key required', field: 'Chat API key', setting: 'CHAT_API_KEYS' },
}

/**
 * Inline credential entry shown on 401. The value goes to the runtime credential store:
 * memory only, unless the user explicitly ticks "keep for this tab" (sessionStorage).
 */
export function CredentialPrompt({ kind, onSaved, rejected = false }: { kind: CredentialKind; onSaved: () => void; rejected?: boolean }) {
  const id = useId()
  const [value, setValue] = useState('')
  const [keep, setKeep] = useState(credentialStore.isPersistent())
  const text = LABELS[kind]

  function submit(event: FormEvent) {
    event.preventDefault()
    if (!value.trim()) return
    credentialStore.setPersistent(keep)
    credentialStore.set(kind, value)
    setValue('')
    onSaved()
  }

  return (
    <form onSubmit={submit} className="rounded-md border border-amber-200 bg-amber-50/60 p-4" autoComplete="off" role="group"
      aria-label={text.title}>
      <div className="flex items-start gap-3">
        <KeyRound className="mt-0.5 h-4 w-4 shrink-0 text-amber-600" aria-hidden="true" />
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold text-slate-900">{text.title}</p>
          <p className="mt-0.5 text-xs text-slate-600">
            {rejected && credentialStore.has(kind) ? 'The backend rejected the stored credential. ' : ''}
            The backend protects this data with <code className="font-mono">{text.setting}</code>. The token is kept in memory
            for this session and is never written into the app build.
          </p>
          <div className="mt-3 flex flex-col gap-2 sm:flex-row sm:items-center">
            <label htmlFor={id} className="sr-only">{text.field}</label>
            <input id={id} type="password" autoComplete="off" spellCheck={false} value={value} placeholder={text.field}
              onChange={(e) => setValue(e.target.value)}
              className="h-9 min-w-0 flex-1 rounded-md border border-slate-300 bg-white px-3 font-mono text-sm shadow-sm" />
            <Button type="submit" size="md" disabled={!value.trim()}>Use token</Button>
          </div>
          <label className="mt-2 flex items-center gap-2 text-xs text-slate-600">
            <input type="checkbox" checked={keep} onChange={(e) => setKeep(e.target.checked)} />
            Keep for this browser tab (sessionStorage)
          </label>
        </div>
      </div>
    </form>
  )
}
