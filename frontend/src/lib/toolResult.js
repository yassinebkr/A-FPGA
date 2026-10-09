// Shared parsing for agent tool results. Tool results may append token metadata
// after a leading JSON object, so JSON.parse(content) alone is insufficient.
export function parseToolResult(content) {
  const text = String(content ?? '')
  const start = text.indexOf('{')
  if (start < 0) return null
  let depth = 0
  let quoted = false
  let escaped = false
  for (let i = start; i < text.length; i += 1) {
    const ch = text[i]
    if (quoted) {
      if (escaped) escaped = false
      else if (ch === '\\') escaped = true
      else if (ch === '"') quoted = false
      continue
    }
    if (ch === '"') quoted = true
    else if (ch === '{') depth += 1
    else if (ch === '}' && --depth === 0) {
      try { return JSON.parse(text.slice(start, i + 1)) } catch { return null }
    }
  }
  return null
}

export function toolResultOk(content) {
  const text = String(content ?? '')
  const parsed = parseToolResult(text)
  if (typeof parsed?.ok === 'boolean') return parsed.ok
  if (parsed) {
    if (Number(parsed.errors_total) > 0) return false
    if (parsed.verdict && /syntax_error|elaboration_error|failed|failure/i.test(parsed.verdict)) return false
    if (parsed.verdict === 'ok' || parsed.verdict === 'lint_warning') return true
  }
  const slack = text.match(/(?:setup\s*slack|setupslack)\s*:\s*(-?[0-9.eE+-]+)/i)
  if (slack && Number(slack[1]) < 0) return false
  return !/simulation\s+failed|synthesis\s+failed|timing\s+failed|\btool\s+error\b/i.test(text)
}
