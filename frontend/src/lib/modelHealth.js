// Short suffix for model <option> labels (native options can't render a dot element).
export function healthSuffix(model) {
  if (!model.available) return ' (api key not set)'
  if (model.health === 'auth_failed') return ' (auth failed)'
  if (model.health === 'unreachable') return ' (unreachable)'
  return ''
}
