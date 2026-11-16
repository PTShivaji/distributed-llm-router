import { normalizeError } from '../../api/errors'
import type { CredentialKind } from '../../auth/credentials'
import { CredentialPrompt } from '../auth/CredentialPrompt'
import { ErrorState } from './States'

/** Section-level error: 401 on a protected endpoint offers inline credential entry; others show ErrorState. */
export function SectionError({ error, credential, onRetry }: { error: unknown; credential?: CredentialKind; onRetry: () => void }) {
  const e = normalizeError(error)
  if (e.kind === 'unauthorized' && credential) return <CredentialPrompt kind={credential} onSaved={onRetry} rejected />
  return <ErrorState error={e} onRetry={onRetry} compact />
}
