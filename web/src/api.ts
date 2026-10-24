import type {
  AdminUserMemories,
  AdminUserRow,
  GraphStats,
  InsightItem,
  Job,
  MemoryItem,
  MemoryKind,
  MemoryPage,
  Reminder,
  Settings,
} from './types'
import { getUserId } from './user'

export const API_BASE = (import.meta.env.VITE_API_BASE as string | undefined) || '/memory'

// ---------- 服务密钥（第十九轮落地）：面板不再裸奔 ----------
const SERVICE_KEY_STORE = 'memsvc_service_key'

export function getServiceKey(): string {
  return sessionStorage.getItem(SERVICE_KEY_STORE) || ''
}

export function setServiceKey(k: string): void {
  if (k) sessionStorage.setItem(SERVICE_KEY_STORE, k)
  else sessionStorage.removeItem(SERVICE_KEY_STORE)
}

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
  const sk = getServiceKey()
  if (sk && !headers.has('X-Service-Key')) headers.set('X-Service-Key', sk)
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
    if (res.status === 401) {
      throw new ApiError(401, body, '服务密钥无效或未填写，请在右上角填写服务密钥')
    }
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
  const sk = getServiceKey()
  const res = await fetch(`${API_BASE}/v1/me/memories/export?user_id=${encodeURIComponent(userId)}`, {
    headers: sk ? { 'X-Service-Key': sk } : {},
  })
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

// ---------- 经验库（第十七轮前端同步） ----------

export function listInsights(): Promise<InsightItem[]> {
  return request<InsightItem[]>('/v1/me/insights')
}

/** 网结构统计（第二十轮）：节点/边/密度/孤点/枢纽/社群/桥/矛盾张力 */
export function graphStats(): Promise<GraphStats> {
  return request<GraphStats>('/v1/me/graph/stats')
}

export async function patchInsight(
  id: string,
  body: { pinned?: boolean; muted?: boolean },
): Promise<InsightItem> {
  // 该端点的 user_id 走查询参数（与其它 /me 接口的约定不同），单独发请求
  const userId = getUserId()
  if (!userId) throw new ApiError(422, null, '请先填写 user_id')
  const sk = getServiceKey()
  const res = await fetch(`${API_BASE}/v1/me/insights/${id}?user_id=${encodeURIComponent(userId)}`, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...(sk ? { 'X-Service-Key': sk } : {}) },
    body: JSON.stringify(body),
  })
  if (!res.ok) throw new ApiError(res.status, null, `更新失败 ${res.status}`)
  return (await res.json()) as InsightItem
}

// ---------- 前瞻记忆（第十七轮） ----------

export function listReminders(includeClosed = true): Promise<Reminder[]> {
  return request<Reminder[]>(`/v1/reminders?include_closed=${includeClosed}`)
}

export function createReminder(body: {
  condition: string
  action: string
  project?: string | null
  once?: boolean
}): Promise<Reminder> {
  return request<Reminder>('/v1/reminders', { method: 'POST', body: JSON.stringify(body) })
}

export function dismissReminder(id: string): Promise<Reminder> {
  return request<Reminder>(`/v1/reminders/${id}/dismiss`, { method: 'POST', body: '{}' })
}

// ---------- 管理端（第十七轮）：密钥只存 sessionStorage，请求直带 X-Admin-Key ----------

const ADMIN_KEY_STORE = 'memsvc_admin_key'

export function getAdminKey(): string {
  return sessionStorage.getItem(ADMIN_KEY_STORE) || ''
}

export function setAdminKey(k: string): void {
  if (k) sessionStorage.setItem(ADMIN_KEY_STORE, k)
  else sessionStorage.removeItem(ADMIN_KEY_STORE)
}

async function adminRequest<T>(path: string): Promise<T> {
  const key = getAdminKey()
  if (!key) throw new ApiError(403, null, '请先填写管理密钥')
  const res = await fetch(`${API_BASE}${path}`, { headers: { 'X-Admin-Key': key } })
  if (!res.ok) {
    throw new ApiError(res.status, null, res.status === 404 ? '管理端未启用（服务端未配置 ADMIN_KEYS）' : `管理端请求失败 ${res.status}`)
  }
  return (await res.json()) as T
}

export function adminOverview(): Promise<{ users: AdminUserRow[] }> {
  return adminRequest<{ users: AdminUserRow[] }>('/v1/admin/overview')
}

export interface AdminAuditEntry {
  at: string | null
  admin_key_hash: string
  endpoint: string
  target_user: string | null
}

export function adminAuditLog(limit = 100): Promise<AdminAuditEntry[]> {
  return adminRequest<{ entries: AdminAuditEntry[] }>(`/audit?limit=${limit}`).then((d) => d.entries)
}

export function adminUserMemories(userId: string): Promise<AdminUserMemories> {
  return adminRequest<AdminUserMemories>(`/v1/admin/users/${encodeURIComponent(userId)}/memories`)
}
