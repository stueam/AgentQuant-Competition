export function readLocal(key: string) {
  try {
    return localStorage.getItem(`agentquant.${key}`)
  } catch {
    return null
  }
}

export function writeLocal(key: string, value: string | null) {
  try {
    if (value === null) localStorage.removeItem(`agentquant.${key}`)
    else localStorage.setItem(`agentquant.${key}`, value)
  } catch {
    // Core API flows remain available when browser storage is blocked.
  }
}

export function validationStorageKey(scope: string, key: string) {
  return scope === 'demo' ? key : `${scope}.${key}`
}
