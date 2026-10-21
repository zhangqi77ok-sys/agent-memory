const KEY = 'memsvc_user_id'

export function getUserId(): string | null {
  if (typeof window === 'undefined') return null
  const q = new URLSearchParams(window.location.search).get('user_id')
  if (q) localStorage.setItem(KEY, q)
  return q || localStorage.getItem(KEY)
}

export function setUserId(id: string): void {
  localStorage.setItem(KEY, id.trim())
}

export function clearUserId(): void {
  localStorage.removeItem(KEY)
}
