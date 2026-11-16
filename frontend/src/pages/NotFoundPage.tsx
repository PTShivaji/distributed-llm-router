import { Compass } from 'lucide-react'
import { Link } from 'react-router'

import { EmptyState } from '../components/feedback/States'

export function NotFoundPage() {
  return (
    <EmptyState icon={<Compass className="h-6 w-6" aria-hidden="true" />} title="Page not found"
      description="This page does not exist."
      action={<Link to="/" className="text-sm font-medium text-brand-700 hover:underline">Go to Overview</Link>} />
  )
}
