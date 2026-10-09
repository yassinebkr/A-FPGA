import { cn } from '@/lib/utils'

// Health status of a registry model, as reported by GET /api/models.
const DOT_STYLE = {
  ok: 'bg-emerald-400',
  auth_failed: 'bg-red-500',
  unreachable: 'bg-orange-400',
  unknown: 'bg-amber-400',
  checking: 'bg-amber-400 animate-pulse',
  no_key: 'bg-gray-600',
}

const DOT_TITLE = {
  ok: 'api key verified',
  auth_failed: 'api key rejected (401/403)',
  unreachable: 'provider unreachable',
  unknown: 'key present, not verifiable for free',
  checking: 'checking…',
  no_key: 'api key not set',
}

export default function ModelHealthDot({ health, className }) {
  return (
    <span
      title={DOT_TITLE[health] ?? DOT_TITLE.checking}
      className={cn(
        'inline-block w-1.5 h-1.5 rounded-full shrink-0',
        DOT_STYLE[health] ?? DOT_STYLE.checking,
        className,
      )}
    />
  )
}
