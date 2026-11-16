import { Loader2 } from 'lucide-react'

import { cn } from '../../lib/cn'

export function Spinner({ size = 'md', className }: { size?: 'sm' | 'md' | 'lg'; className?: string }) {
  const px = { sm: 'h-3.5 w-3.5', md: 'h-5 w-5', lg: 'h-8 w-8' }[size]
  return <Loader2 aria-hidden="true" className={cn('animate-spin', px, className)} />
}
