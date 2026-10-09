// Design-contract frames are deliberately normalized at the websocket edge.
// The server owns the contract; this module only keeps the UI tolerant of
// small protocol revisions while exposing a fixed component registry to the
// renderer.

export const CONTRACT_STATES = Object.freeze({
  DRAFT: 'draft',
  NEEDS_DECISION: 'needs_decision',
  READY: 'ready',
  REVISED: 'revised',
  LOCKED: 'locked',
})

export const CONTRACT_STATE_LABELS = Object.freeze({
  draft: 'Draft',
  needs_decision: 'Needs decision',
  ready: 'Ready',
  revised: 'Revised',
  locked: 'Locked',
})

const CONTRACT_BUILD_STATES = new Set(['queued', 'running', 'building', 'starting', 'active'])

const CONTRACT_FRAME_TYPES = new Set([
  'contract_state',
  'contract',
  'design_contract',
  'contract_upsert',
  'design_contract_upsert',
  'contract_patch',
  'design_contract_patch',
  'contract_remove',
  'design_contract_remove',
  'contract_clear',
  'design_contract_clear',
])

const FRAME_OPERATIONS = new Set(['upsert', 'patch', 'remove', 'clear'])

export const CONTRACT_SECTION_REGISTRY = Object.freeze([
  { key: 'interface', label: 'Interface', fields: ['top_module', 'clock', 'reset', 'ports'] },
  { key: 'data', label: 'Data semantics', fields: ['width', 'signed', 'latency_cycles', 'throughput_per_cycle', 'pipeline_stages', 'multiplier_strategy', 'memory_strategy'] },
  { key: 'timing', label: 'Timing', fields: ['clock_target_mhz', 'clock_period_ns', 'clock_uncertainty_ns'] },
  { key: 'target', label: 'Target', fields: ['target_type', 'technology', 'target'] },
  { key: 'verification', label: 'Verification', fields: ['testbench', 'acceptance', 'verification'] },
])

export const CONTRACT_FIELD_REGISTRY = Object.freeze({
  top_module: { label: 'Top module', input: 'text' },
  clock: { label: 'Clock', input: 'text' },
  reset: { label: 'Reset', input: 'text' },
  ports: { label: 'Ports', input: 'text' },
  width: { label: 'Data width', input: 'number' },
  signed: { label: 'Signed data', input: 'boolean' },
  latency_cycles: { label: 'Latency', unit: 'cycles', input: 'number' },
  throughput_per_cycle: { label: 'Throughput', unit: '/ cycle', input: 'number' },
  pipeline_stages: { label: 'Pipeline stages', input: 'number' },
  multiplier_strategy: { label: 'Multiplier', input: 'select', options: ['inferred', 'dsp', 'fabric', 'macro', 'none'] },
  memory_strategy: { label: 'Memory', input: 'select', options: ['registers', 'bram', 'sram', 'distributed_ram', 'external', 'none'] },
  clock_target_mhz: { label: 'Clock target', unit: 'MHz', input: 'number' },
  clock_period_ns: { label: 'Clock period', unit: 'ns', input: 'number' },
  clock_uncertainty_ns: { label: 'Uncertainty', unit: 'ns', input: 'number' },
  target_type: { label: 'Target type', input: 'select', options: ['fpga', 'asic'] },
  technology: { label: 'Technology', input: 'text' },
  target: { label: 'Build target', input: 'text' },
  testbench: { label: 'Testbench', input: 'text' },
  acceptance: { label: 'Acceptance', input: 'text' },
  verification: { label: 'Verification', input: 'text' },
})

function objectValue(value) {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {}
  return value
}

function decode(value) {
  if (value && typeof value === 'object') return value
  if (typeof value !== 'string') return null
  try {
    const parsed = JSON.parse(value)
    return parsed && typeof parsed === 'object' ? parsed : null
  } catch {
    return null
  }
}

function stateOf(value, decisions = []) {
  const raw = String(value ?? '').trim().toLowerCase().replace(/[ -]+/g, '_')
  if (CONTRACT_STATE_LABELS[raw]) return raw
  if (raw === 'needsdecision' || raw === 'needs_input' || raw === 'pending') return CONTRACT_STATES.NEEDS_DECISION
  // Older servers called the terminal approval state "approved". Treat it
  // as locked in the UI: approval authorizes the saved request and must not
  // invite the user to send the original prompt again.
  if (raw === 'approved' || raw === 'accepted' || raw === 'final') return CONTRACT_STATES.LOCKED
  if (raw === 'updated' || raw === 'modified' || raw === 'revision_requested') return CONTRACT_STATES.REVISED
  return decisions.some((decision) => decision.required !== false && decision.answer === undefined)
    ? CONTRACT_STATES.NEEDS_DECISION
    : CONTRACT_STATES.DRAFT
}

function normalizeDecision(raw, index) {
  const source = objectValue(raw)
  if (typeof raw === 'string') return { id: `decision-${index + 1}`, prompt: raw, options: [], required: true }
  const options = Array.isArray(source.options)
    ? source.options.map((option) => option && typeof option === 'object' ? { ...option, label: String(option.label ?? option.value ?? '') } : { value: option, label: String(option) })
    : []
  return {
    ...source,
    id: String(source.id ?? source.key ?? `decision-${index + 1}`),
    prompt: String(source.prompt ?? source.question ?? source.label ?? source.title ?? 'Choose an implementation detail'),
    options,
    required: source.required !== false,
  }
}

function normalizeContract(raw) {
  const source = objectValue(raw)
  const nested = objectValue(source.contract)
  const spec = objectValue(nested.spec ?? source.spec)
  // The backend envelope owns revision/digest/status while DesignSpec owns
  // the engineering fields. Flatten them for the fixed React renderer.
  const architecture = objectValue(spec.metadata?.architecture_contract)
  const timingGoals = objectValue(spec.timing_goals)
  const envelope = Object.keys(nested).length ? nested : source
  const value = Object.keys(nested).length
    ? { ...source, ...nested, ...spec, ...architecture, ...timingGoals }
    : { ...source, ...spec, ...architecture, ...timingGoals }
  const decisionsRaw = value.decisions ?? value.decision_prompts ?? value.prompts ?? []
  const decisions = Array.isArray(decisionsRaw) ? decisionsRaw.map(normalizeDecision) : []
  const sections = objectValue(value.sections)
  const sectionValues = {}
  for (const section of CONTRACT_SECTION_REGISTRY) {
    sectionValues[section.key] = {
      ...objectValue(value[section.key]),
      ...objectValue(value[`${section.key}_semantics`]),
      ...objectValue(sections[section.key]),
    }
  }
  // The architecture contract is flat. Copy its known fields into their
  // display groups without losing any server-provided section metadata.
  const flatFields = Object.keys(CONTRACT_FIELD_REGISTRY)
  for (const field of flatFields) {
    for (const section of CONTRACT_SECTION_REGISTRY) {
      if (section.fields.includes(field) && sectionValues[section.key][field] === undefined && value[field] !== undefined) {
        sectionValues[section.key][field] = value[field]
      }
    }
  }
  return {
    ...value,
    id: String(value.id ?? value.contract_id ?? value.surface_id ?? 'design-contract'),
    // The DesignSpec has its own engineering revision. Approval concurrency
    // must always use the outer DesignContract revision/digest/status.
    revision: envelope.revision ?? envelope.revision_id ?? envelope.version ?? value.revision ?? null,
    digest: envelope.digest ?? source.digest ?? value.digest ?? null,
    status: stateOf(envelope.status ?? envelope.state ?? envelope.verdict ?? value.status ?? value.state ?? value.verdict, decisions),
    backendStatus: String(envelope.status ?? envelope.state ?? envelope.verdict ?? value.status ?? value.state ?? value.verdict ?? '').toLowerCase() || null,
    pausedReason: String(value.paused_reason ?? value.pausedReason ?? value.block_reason ?? '').trim(),
    pendingRequest: value.pending_request ?? value.pendingRequest ?? value.original_request ?? value.request ?? null,
    buildStatus: String(value.build_status ?? value.buildStatus ?? value.execution?.status ?? '').toLowerCase() || null,
    summary: value.summary ?? value.description ?? value.rationale ?? '',
    revisionDiff: value.revisionDiff ?? value.revision_diff ?? [],
    sections: sectionValues,
    decisions,
    inferredDefaults: (() => {
      const defaults = value.inferredDefaults ?? value.inferred_defaults ?? value.metadata?.inferred_defaults ?? value.defaults
      return Array.isArray(defaults) ? defaults : defaults ? [defaults] : []
    })(),
  }
}

function payloadOf(frame) {
  const decoded = decode(frame)
  if (!decoded) return null
  if (String(decoded.type || '').toLowerCase() === 'contract_state') return decoded
  const payload = decoded.payload ?? decoded.data ?? decoded.content
  return decode(payload) ?? payload ?? decoded.contract ?? decoded
}

export function isDesignContractFrame(frame) {
  if (!frame || typeof frame !== 'object') return false
  const type = String(frame.type ?? '').toLowerCase()
  if (CONTRACT_FRAME_TYPES.has(type)) return true
  return type === 'ui_surface' && (frame.surface === 'design_contract' || frame.component === 'design_contract')
}

export function normalizeDesignContractFrame(frame) {
  if (!isDesignContractFrame(frame)) return null
  const type = String(frame.type ?? '').toLowerCase()
  const payload = payloadOf(frame)
  const payloadSource = objectValue(payload)
  const explicit = String(frame.op ?? frame.operation ?? payloadSource.op ?? payloadSource.operation ?? '').toLowerCase()
  let operation = explicit
  if (!FRAME_OPERATIONS.has(operation)) {
    if (type.endsWith('_patch')) operation = 'patch'
    else if (type.endsWith('_remove')) operation = 'remove'
    else if (type.endsWith('_clear')) operation = 'clear'
    else operation = 'upsert'
  }
  const source = objectValue(payload)
  const candidate = source.contract ?? source.surface ?? source.data ?? source.props
  const contract = candidate && typeof candidate === 'object' ? candidate : source
  return {
    operation,
    id: frame.contract_id ?? frame.surface_id ?? source.contract_id ?? source.id ?? contract.id,
    patch: frame.patch ?? source.patch ?? (operation === 'patch' ? contract : null),
    contract: operation === 'clear' || operation === 'remove' ? null : normalizeContract(contract),
  }
}

function mergeContract(current, patch) {
  if (!current) return normalizeContract(patch)
  const next = { ...current, ...objectValue(patch) }
  next.sections = { ...current.sections }
  for (const section of CONTRACT_SECTION_REGISTRY) {
    if (patch?.sections?.[section.key] || patch?.[section.key]) {
      next.sections[section.key] = {
        ...current.sections?.[section.key],
        ...objectValue(patch.sections?.[section.key]),
        ...objectValue(patch[section.key]),
      }
    }
  }
  if (patch?.decisions) next.decisions = Array.isArray(patch.decisions) ? patch.decisions.map(normalizeDecision) : []
  if (patch?.inferred_defaults || patch?.inferredDefaults || patch?.defaults) {
    next.inferredDefaults = patch.inferredDefaults ?? patch.inferred_defaults ?? patch.defaults
  }
  return normalizeContract(next)
}

export function applyDesignContractFrame(current, frame) {
  const normalized = normalizeDesignContractFrame(frame)
  if (!normalized) return current
  if (normalized.operation === 'clear') return null
  if (normalized.operation === 'remove') {
    if (!normalized.id || !current || normalized.id === current.id) return null
    return current
  }
  if (normalized.operation === 'patch') return mergeContract(current, normalized.patch ?? normalized.contract ?? {})

  // A project join can deliver a persisted snapshot and then replay buffered
  // contract frames. The replay may contain an older snapshot than the one
  // already rendered. Never let that late frame move the UI backwards: the
  // backend validates actions against the authoritative current revision, so
  // displaying a lower revision makes a valid click fail with stale_revision.
  const incomingRevision = Number(normalized.contract?.revision)
  const currentRevision = Number(current?.revision)
  if (Number.isFinite(incomingRevision) && Number.isFinite(currentRevision) && incomingRevision < currentRevision) {
    return current
  }
  if (current && Number.isFinite(incomingRevision) && Number.isFinite(currentRevision) && incomingRevision > currentRevision) {
    const changes = []
    for (const section of CONTRACT_SECTION_REGISTRY) {
      for (const field of section.fields) {
        const previous = contractFieldValue(current, section.key, field)
        const next = contractFieldValue(normalized.contract, section.key, field)
        if (previous === undefined || next === undefined || JSON.stringify(previous) === JSON.stringify(next)) continue
        changes.push({ section: section.label, field, label: CONTRACT_FIELD_REGISTRY[field]?.label || field, previous, next })
      }
    }
    if (changes.length) normalized.contract.revisionDiff = changes
  }
  return normalized.contract
}

export function contractFieldValue(contract, sectionKey, field) {
  if (!contract) return undefined
  const section = contract.sections?.[sectionKey]
  if (section && section[field] !== undefined) return section[field]
  return contract[field]
}



/**
 * Calculate UI gating from the last authoritative contract snapshot.
 * This is intentionally pure: it never promotes a draft locally and never
 * mutates the contract while a typed action is in flight.
 */
export function contractGate(contract, { busy = false, isRunning = false } = {}) {
  if (!contract) {
    return { hasContract: false, blocked: false, reason: '', building: false, locked: false, canApprove: false }
  }
  const status = contract.status || CONTRACT_STATES.DRAFT
  const answers = objectValue(contract.answers)
  const unresolved = contract.decisions?.some((decision) => (
    decision.required !== false &&
    decision.answer === undefined &&
    answers[decision.id] === undefined
  )) ?? false
  const locked = status === CONTRACT_STATES.LOCKED
  const building = CONTRACT_BUILD_STATES.has(contract.buildStatus) || (locked && (busy || isRunning))
  const canApprove = status === CONTRACT_STATES.READY && !unresolved
  let reason = contract.pausedReason
  if (!reason && unresolved) reason = 'Answer the highlighted design decision before building.'
  if (!reason && status === CONTRACT_STATES.DRAFT) reason = 'Review the design contract before building.'
  if (!reason && status === CONTRACT_STATES.REVISED) reason = 'Review the revised contract before building.'
  if (!reason && status === CONTRACT_STATES.READY) reason = 'Approve the contract to start the saved request.'
  if (!reason && building) reason = 'Build is running from the approved contract.'
  return {
    hasContract: true,
    status,
    unresolved,
    blocked: !locked || building,
    building,
    locked,
    reason,
    canApprove,
    awaitingApproval: status === CONTRACT_STATES.READY && !unresolved,
  }
}
