/** Splits a model answer into prose and ``` fenced code blocks (display only; nothing is interpreted as HTML). */

export interface Block {
  kind: 'text' | 'code'
  content: string
  lang?: string
}

/** Splits ``` fenced code blocks from prose. An unterminated fence runs to the end. */
export function splitFences(text: string): Block[] {
  const blocks: Block[] = []
  const re = /```([\w.+-]*)[^\S\n]*\n?([\s\S]*?)(?:```|$)/g
  let last = 0
  for (const match of text.matchAll(re)) {
    if (match.index > last) blocks.push({ kind: 'text', content: text.slice(last, match.index) })
    blocks.push({ kind: 'code', content: match[2].replace(/\n$/, ''), lang: match[1] || undefined })
    last = match.index + match[0].length
    if (match[0].length === 0) break
  }
  if (last < text.length) blocks.push({ kind: 'text', content: text.slice(last) })
  return blocks.filter((b) => b.kind === 'code' || b.content.trim() !== '')
}
