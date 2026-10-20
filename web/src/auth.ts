const STORAGE_KEY = 'memsvc_token'

/** 从 URL ?token= 读取并写入 localStorage；之后用 localStorage。 */
export function resolveToken(): string | null {
  if (typeof window === 'undefined') return null
  const params = new URLSearchParams(window.location.search)
  const fromQuery = params.get('token')
  if (fromQuery) {
    localStorage.setItem(STORAGE_KEY, fromQuery)
    // 去掉 URL 里的 token，避免分享链接泄漏
    params.delete('token')
    const qs = params.toString()
    const next = `${window.location.pathname}${qs ? `?${qs}` : ''}${window.location.hash}`
    window.history.replaceState({}, '', next)
    return fromQuery
  }
  return localStorage.getItem(STORAGE_KEY)
}

export function clearToken(): void {
  localStorage.removeItem(STORAGE_KEY)
}

export function getStoredToken(): string | null {
  return localStorage.getItem(STORAGE_KEY)
}
