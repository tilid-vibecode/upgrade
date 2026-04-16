import { getWorkflowStatusLabel } from '../workflow'

type StatusChipProps = {
  status: string
}

function normalizeChipClass(status: string) {
  switch (status) {
    case 'completed':
    case 'approved':
    case 'published':
    case 'healthy':
    case 'parsed':
      return 'is-completed'
    case 'ready':
      return 'is-ready'
    case 'draft':
    case 'generated':
    case 'attached':
    case 'uploaded':
      return 'is-neutral'
    case 'pending':
      return 'is-ready'
    case 'running':
    case 'opened':
    case 'parsing':
    case 'processing':
      return 'is-running'
    case 'blocked':
    case 'degraded':
    case 'archived':
      return 'is-blocked'
    case 'action_required':
    case 'partial_failed':
      return 'is-action'
    case 'superseded':
    case 'failed':
    case 'unhealthy':
    case 'invalid':
      return 'is-failed'
    case 'submitted':
      return 'is-submitted'
    case 'not_started':
      return 'is-neutral'
    default:
      return 'is-neutral'
  }
}

export default function StatusChip({ status }: StatusChipProps) {
  return (
    <span className={`status-chip ${normalizeChipClass(status)}`}>
      {getWorkflowStatusLabel(status)}
    </span>
  )
}
