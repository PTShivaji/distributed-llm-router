import { useEffect, useState } from 'react'
import { Check, Copy } from 'lucide-react'

import { cn } from '../../lib/cn'
import { splitFences } from '../../lib/fences'

/**
 * Readable rendering of a model answer. Plain text only: React escapes everything, no HTML or
 * Markdown is interpreted (an answer can never inject markup or scripts).
 */
export function FormattedAnswer({ text }: { text: string }) {
  if (text === '') return <p className="text-sm italic text-slate-400">The model returned an empty answer.</p>
  return (
    <div className="space-y-3 text-sm leading-relaxed text-slate-800" data-testid="answer">
      {splitFences(text).map((b, i) => (b.kind === 'code' ? (
        <div key={i} className="overflow-hidden rounded-md border border-slate-200 bg-slate-950">
          {b.lang && <div className="border-b border-slate-800 px-3 py-1 font-mono text-[11px] text-slate-400">{b.lang}</div>}
          <pre className="overflow-x-auto p-3 text-xs leading-relaxed text-slate-100"><code>{b.content}</code></pre>
        </div>
      ) : (
        <p key={i} className="whitespace-pre-wrap break-words">{b.content.replace(/^\n+|\n+$/g, '')}</p>
      )))}
    </div>
  )
}

export function CopyButton({ text, label = 'Copy response' }: { text: string; label?: string }) {
  const [state, setState] = useState<'idle' | 'copied' | 'failed'>('idle')
  useEffect(() => {
    if (state === 'idle') return
    const t = window.setTimeout(() => setState('idle'), 2000)
    return () => window.clearTimeout(t)
  }, [state])

  async function copy() {
    try {
      await navigator.clipboard.writeText(text)
      setState('copied')
    } catch {
      setState('failed')
    }
  }

  return (
    <button type="button" onClick={copy} aria-label={label}
      className={cn('inline-flex items-center gap-1 rounded px-1.5 py-1 text-xs font-medium focus:outline-none focus-visible:ring-2 focus-visible:ring-brand-500',
        state === 'failed' ? 'text-red-600' : 'text-slate-500 hover:bg-slate-100 hover:text-slate-700')}>
      {state === 'copied' ? <Check className="h-3.5 w-3.5 text-emerald-600" aria-hidden="true" /> : <Copy className="h-3.5 w-3.5" aria-hidden="true" />}
      <span aria-live="polite">{state === 'copied' ? 'Copied' : state === 'failed' ? 'Copy failed' : 'Copy'}</span>
    </button>
  )
}
