// Internal source references used only in compact model context/tool frames.
// They are not executable RTL and must never replace the Code-tab snapshot.
const ARTIFACT_HANDLE_RE = /^\[artifact [A-Za-z][A-Za-z0-9 _-]*:[0-9a-fA-F]{16,64} omitted from protocol tail; see CURRENT ARTIFACTS\]$/

export function isArtifactHandle(value) {
  return typeof value === 'string' && ARTIFACT_HANDLE_RE.test(value)
}
