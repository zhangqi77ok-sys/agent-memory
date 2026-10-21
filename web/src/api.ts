import type { Job, MemoryItem, MemoryKind, MemoryPage, Settings } from './types'
import { getUserId } from './user'

export const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) || '/memory'

export class ApiError extends Error {
  status: number
  detail: unknown
  constructor(status: number, detail: unknown, message: string) {
    super(message)
    this.status = status
    this.detail = detail
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const userId = getUserId()
  if (!userId) throw new ApiError(422, null, '请先填写 user_id')
  const method = (init.method || 'GET').toUpperCase()
  let url = `${API_BASE}${path}`
  let payload = init.body
  if (method === 'GET' || method === 'DELETE') {
    url += `${path.includes('?') ? '&' : '?'}user_id=${encodeURIComponent(userId)}`
  } else {
    payload = JSON.stringify({ ...(payload ? JSON.parse(String(payload)) : {}), user_id: userId })
  }
  const headers = new Headers(init.headers)
  if (payload && !headers.has('Content-Type')) {
    headers.set('Content-Type', 'application/json')
  }
  const res = await fetch(url, { ...init, method, body: payload, headers })
  if (res.status === 204) return undefined as T
  const text = await res.text()
  let body: unknown = null
  if (text) {
    try {
      body = JSON.parse(text)
    } catch {
      body = text
    }
  }
  if (!res.ok) {
    const msg =
      typeof body === 'object' && body && 'detail' in body
        ? JSON.stringify((body as { detail: unknown }).detail)
        : `请求失败 ${res.status}`
    throw new ApiError(res.status, body, msg)
  }
  return body as T
}

export function getSettings(): Promise<Settings> {
  return request<Settings>('/v1/me/settings')
}

export function updateSettings(body: {
  memory_enabled: boolean
  consent_version?: string | null
}): Promise<Settings> {
  return request<Settings>('/v1/me/settings', { method: 'PUT', body: JSON.stringify(body) })
}

export function listMemories(opts: {
  kind?: MemoryKind
  offset?: number
  limit?: number
} = {}): Promise<MemoryPage> {
  const q = new URLSearchParams()
  if (opts.kind) q.set('kind', opts.kind)
  if (opts.offset != null) q.set('offset', String(opts.offset))
  if (opts.limit != null) q.set('limit', String(opts.limit))
  const qs = q.toString()
  return request<MemoryPage>(`/v1/me/memories${qs ? `?${qs}` : ''}`)
}

export function correctMemory(id: string, content: string): Promise<MemoryItem> {
  return request<MemoryItem>(`/v1/me/memories/${id}`, {
    method: 'PATCH',
    body: JSON.stringify({ content }),
  })
}

export function deleteMemory(id: string): Promise<void> {
  return request<void>(`/v1/me/memories/${id}`, { method: 'DELETE' })
}

export function clearAllMemories(): Promise<void> {
  return request<void>('/v1/me/memories', { method: 'DELETE' })
}

export async function exportMemories(): Promise<Blob> {
  const userId = getUserId()
  if (!userId) throw new ApiError(422, null, '请先填写 user_id')
  const res = await fetch(`${API_BASE}/v1/me/memories/export?user_id=${encodeURIComponent(userId)}`)
  if (!res.ok) {
    throw new ApiError(res.status, null, `导出失败 ${res.status}`)
  }
  return res.blob()
}

export function listJobs(limit = 20): Promise<Job[]> {
  return request<Job[]>(`/v1/me/jobs?limit=${limit}`)
}

/** 是否需要重新展示授权页：未同意，或同意版本落后。 */
export function needsConsent(s: Settings): boolean {
  return !s.consent_version || s.consent_version !== s.current_consent_version
}
