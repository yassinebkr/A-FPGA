// Normalize optional structured run events from the websocket into the small
// public shape the timeline renders. The backend may send an object directly
// or JSON in `content`; older servers do not send these events at all.

const STRUCTURED_TYPES = new Set([
  'progress',
  'decision',
  'phase',
  'timeline',
  'run_progress',
  'run_status',
  'structured_progress',
  'structured_status',
  'decision_status',
])

const PRIVATE_KEYS = new Set([
  'analysis',
  'chain_of_thought',
  'cot',
  'inner_monologue',
  'thought',
  'thoughts',
])

const asText = (value, max = 220) => {
  if (value === null || value === undefined) return ''
  if (typeof value === 'string') return value.replace(/\s+/g, ' ').trim().slice(0, max)
  if (typeof value === 'number' || typeof value === 'boolean') return String(value)
  if (Array.isArray(value)) return value.map((item) => asText(item, max)).filter(Boolean).join(' · ').slice(0, max)
  if (typeof value === 'object') {
    return Object.entries(value)
      .filter(([key]) => !PRIVATE_KEYS.has(key))
      .map(([key, item]) => {
        const text = asText(item, max)
        return text ? `${key}: ${text}` : ''
      })
      .filter(Boolean).join(' · ').slice(0, max)
  }
  return ''
}

function decodeContent(content) {
  if (content && typeof content === 'object') return content
  if (typeof content !== 'string') return null
  try {
    const decoded = JSON.parse(content)
    return decoded && typeof decoded === 'object' ? decoded : null
  } catch {
    return null
  }
}

function firstText(source, keys) {
  for (const key of keys) {
    if (PRIVATE_KEYS.has(key)) continue
    const value = asText(source?.[key])
    if (value) return value
  }
  return ''
}


const hasOwn = (value, key) => Boolean(value && typeof value === 'object' &&
  Object.prototype.hasOwnProperty.call(value, key))

function firstValue(source, keys) {
  for (const key of keys) {
    if (hasOwn(source, key) && source[key] !== null && source[key] !== undefined) {
      return source[key]
    }
  }
  return undefined
}

function finiteNumber(value) {
  if (typeof value === 'number' && Number.isFinite(value)) return value
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value.replace(/,/g, ''))
    return Number.isFinite(parsed) ? parsed : undefined
  }
  return undefined
}

function normalizeAttempt(source) {
  const raw = firstValue(source, ['attempt', 'attempts', 'revision_attempt'])
  const attempt = raw && typeof raw === 'object' ? { ...raw } : {}
  const current = finiteNumber(firstValue(source, [
    'attempt_number', 'attempt_no', 'attempt_index', 'current_attempt', 'attempt_count',
  ])) ?? finiteNumber(typeof raw === 'number' ? raw : attempt.current ?? attempt.number ?? attempt.index)
  const limit = finiteNumber(firstValue(source, [
    'attempt_limit', 'max_attempts', 'attempts_limit', 'retry_limit',
  ])) ?? finiteNumber(attempt.limit ?? attempt.max ?? attempt.total)
  if (current !== undefined) attempt.current = current
  if (limit !== undefined) attempt.limit = limit
  return Object.keys(attempt).length ? attempt : null
}

function normalizeBudgetEntry(raw) {
  if (raw === null || raw === undefined) return null
  const source = raw && typeof raw === 'object' ? raw : { used: raw }
  const used = finiteNumber(firstValue(source, ['used', 'consumed', 'count', 'current', 'spent']))
  const limit = finiteNumber(firstValue(source, ['limit', 'budget', 'max', 'capacity', 'total']))
  const remaining = finiteNumber(firstValue(source, ['remaining', 'left', 'available']))
  if (used === undefined && limit === undefined && remaining === undefined) return null
  return {
    ...(used === undefined ? {} : { used }),
    ...(limit === undefined ? {} : { limit }),
    ...(remaining === undefined ? {} : { remaining }),
  }
}


function normalizeBudgets(source) {
  const root = firstValue(source, ['budgets', 'budget', 'resource_budgets', 'limits'])
  const parent = root && typeof root === 'object' ? root : {}
  const entries = {}
  const aliases = {
    tokens: ['tokens', 'token', 'token_budget', 'token_budget_tokens'],
    tools: ['tools', 'tool', 'tool_budget', 'tool_calls'],
    context: ['context', 'context_budget', 'context_budget_tokens', 'prompt', 'prompt_budget'],
    rounds: ['rounds', 'round', 'round_budget', 'model_rounds'],
  }
  const flat = {
    tokens: {
      used: ['tokens_used', 'token_usage', 'token_count'],
      limit: ['token_limit', 'max_tokens', 'chat_token_budget'],
      remaining: ['tokens_remaining', 'token_remaining'],
    },
    tools: {
      used: ['tools_used', 'tool_calls_used', 'tool_count'],
      limit: ['tool_limit', 'max_tool_calls', 'tool_budget'],
      remaining: ['tools_remaining', 'tool_calls_remaining'],
    },
    context: {
      used: ['context_used', 'context_tokens_used', 'prompt_tokens'],
      limit: ['context_limit', 'context_tokens_limit', 'context_budget', 'max_context_tokens'],
      remaining: ['context_remaining', 'context_tokens_remaining'],
    },
    rounds: {
      used: ['rounds_used', 'round_count', 'model_rounds_used'],
      limit: ['round_limit', 'max_rounds', 'model_round_limit'],
      remaining: ['rounds_remaining'],
    },
  }
  for (const [name, keys] of Object.entries(aliases)) {
    const direct = firstValue(parent, keys)
    const fields = flat[name]
    const flattened = {
      used: firstValue(source, fields.used),
      limit: firstValue(source, fields.limit),
      remaining: firstValue(source, fields.remaining),
    }
    const value = direct === undefined ? flattened : direct
    const normalized = normalizeBudgetEntry(value)
    if (normalized) entries[name] = normalized
  }
  return Object.keys(entries).length ? entries : null
}

function normalizeMetricDeltas(source) {
  const raw = firstValue(source, ['metric_deltas', 'metrics_delta', 'metricDelta', 'metric_changes', 'deltas'])
  if (raw === null || raw === undefined) return []
  const items = Array.isArray(raw)
    ? raw
    : Object.entries(raw).map(([name, value]) =>
      value && typeof value === 'object' ? { name, ...value } : { name, delta: value })
  return items.map((item) => {
    if (!item || typeof item !== 'object') return null
    const name = asText(item.name ?? item.metric ?? item.key, 48)
    if (!name) return null
    // Timing slack is evidence, not a generic PPA delta. It is rendered in
    // the event evidence/status and should not get a misleading green/red
    // change chip here.
    if (/(?:slack|wns|timing_slack)/i.test(name)) return null
    const delta = item.delta ?? item.change ?? item.diff ??
      (item.before !== undefined && item.after !== undefined
        ? Number(item.after) - Number(item.before)
        : item.value)
    const explicitImproved = typeof item.improved === 'boolean'
      ? item.improved
      : typeof item.improved === 'string'
        ? item.improved.toLowerCase() === 'true'
        : item.improved === 1
          ? true
          : item.improved === 0
            ? false
            : undefined
    return {
      name,
      ...(delta === undefined ? {} : { delta }),
      ...(item.before === undefined ? {} : { before: item.before }),
      ...(item.after === undefined ? {} : { after: item.after }),
      ...(explicitImproved === undefined ? {} : { improved: explicitImproved }),
      ...(item.unit === undefined ? {} : { unit: asText(item.unit, 16) }),
    }
  }).filter(Boolean).slice(0, 8)
}

function normalizeOne(frame, payload, index = 0) {
  const base = payload && typeof payload === 'object' ? payload : {}
  // S3 run snapshots carry the authoritative lifecycle fields in a compact
  // JSON `evidence` string. Decode it for the UI, while retaining the public
  // envelope as the fallback for older servers.
  const snapshot = typeof base.evidence === 'string' ? decodeContent(base.evidence) : null
  const merged = snapshot && typeof snapshot === 'object' ? { ...base, ...snapshot } : base
  const telemetry = merged.stats || merged.telemetry || merged.run_stats
  const source = telemetry && typeof telemetry === 'object' ? { ...merged, ...telemetry } : merged
  const type = String(frame.type || source.type || 'progress').toLowerCase()
  const phase = firstText(source, ['phase', 'stage', 'step', 'area'])
  const action = firstText(source, ['action', 'title', 'summary', 'message', 'label', 'event'])
  const reason = firstText(source, ['reason', 'rationale', 'why', 'terminal_reason'])
  const evidence = firstText(source, ['evidence', 'result', 'metric', 'artifact', 'source'])
  const status = firstText(source, ['status', 'state', 'outcome']) ||
    (type === 'decision' ? 'decision' : 'running')

  const attempt = normalizeAttempt(source)
  const budgets = normalizeBudgets(source)
  const metricDeltas = normalizeMetricDeltas(source)

  // Do not create an empty card for malformed/unknown payloads. This also
  // prevents a plain heartbeat string from becoming a fake decision entry.
  if (!phase && !action && !reason && !evidence && !attempt && !budgets && !metricDeltas.length) return null

  const eventId = firstText(source, ['id', 'event_id', 'eventId', 'seq']) ||
    firstText(frame, ['id', 'event_id', 'eventId', 'seq'])
  return {
    id: eventId || `${type}-${Date.now()}-${index}`,
    kind: type === 'decision' ? 'decision' : 'progress',
    phase: phase || 'Run',
    action: action || (type === 'decision' ? 'Decision recorded' : 'Progress update'),
    status: status.toLowerCase(),
    reason,
    evidence,
    attempt,
    budgets,
    metricDeltas,
    revision: firstValue(source, ['revision', 'revision_id', 'checkpoint']),
    timestamp: source.timestamp || source.created_at || frame.timestamp || new Date().toISOString(),
  }
}

export function isStructuredTimelineFrame(frame) {
  if (!frame) return false
  const type = String(frame.type || '').toLowerCase()
  if (STRUCTURED_TYPES.has(type)) return true
  // Preserve legacy string heartbeats while accepting a future structured
  // status payload such as { type: 'status', content: { phase, action } }.
  if (type !== 'status') return false
  const payload = decodeContent(frame.content)
  const telemetry = payload?.stats || payload?.telemetry || payload?.run_stats
  const candidate = telemetry && typeof telemetry === 'object' ? { ...payload, ...telemetry } : payload
  return Boolean(candidate && typeof candidate === 'object' && (
    candidate.phase || candidate.stage || candidate.action || candidate.summary || candidate.message || candidate.reason || candidate.evidence ||
    candidate.attempt || candidate.attempts || candidate.attempt_number || candidate.budgets || candidate.budget ||
    candidate.metric_deltas || candidate.metrics_delta || candidate.metric_changes || candidate.deltas ||
    candidate.rounds_used || candidate.round_count || candidate.context_used || candidate.context_tokens_used
  ))
}

export function normalizeTimelineFrame(frame) {
  if (!isStructuredTimelineFrame(frame)) return []
  const decoded = decodeContent(frame.content)
  // Merge envelope fields with content so producers can keep phase/status in
  // the frame while putting action/reason/evidence in JSON content.
  const payload = decoded && !Array.isArray(decoded) ? { ...frame, ...decoded } : (decoded || frame)
  const entries = Array.isArray(payload) ? payload :
    Array.isArray(payload.events) ? payload.events :
      Array.isArray(payload.items) ? payload.items : [payload]
  return entries.map((entry, index) => normalizeOne(frame, entry, index)).filter(Boolean)
}
