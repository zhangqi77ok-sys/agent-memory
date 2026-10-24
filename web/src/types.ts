export type MemoryKind = 'fact' | 'preference'

export interface MemoryItem {
  id: string
  kind: MemoryKind
  subject: string
  content: string
  version: number
  valid_from: string
  source_session_id?: string | null
  // 第十七轮：佐证置信度（被独立来源重复证实的程度）
  confidence?: number
  support_count?: number
}

export interface MemoryPage {
  items: MemoryItem[]
  total: number
  offset: number
  limit: number
}

export interface Settings {
  memory_enabled: boolean
  consent_version: string | null
  consent_at: string | null
  current_consent_version: string
}

export interface Job {
  job_id: string
  session_id: string
  status: string
  attempts: number
  last_error: string | null
  created_at: string
}

export interface ApiError {
  status: number
  detail: unknown
  message: string
}

/** 经验条目（GET /v1/me/insights）：第十六/十七轮的信任字段都在这里 */
export interface InsightItem {
  id: string
  kind: string // lesson | strategy | pitfall | cost_saver
  subject: string
  content: string
  applies_when: string
  project: string | null
  confidence: number
  use_count: number // 曝光（被简报递出）
  applied_count: number // 应用（被后续任务真正用上，回流记账）
  success_count: number
  feedback_pos: number
  feedback_neg: number
  auto_pos: number
  auto_neg: number
  verified: boolean // 提炼证据校验（第十六轮）
  pinned: boolean
  muted: boolean
  version: number
  source_episode_id: string | null
  valid_from: string
  last_used_at: string | null
}

/** 前瞻记忆（第十七轮）：当 condition 发生时提醒做 action */
export interface Reminder {
  reminder_id: string
  condition: string
  action: string
  project: string | null
  once: boolean
  status: string // armed | fired | dismissed
  fire_count: number
}

/** 管理端（第十七轮）：跨用户总览，只读 */
export interface AdminUserRow {
  user_id: string
  facts: number
  preferences: number
  insights: number
  episodes: number
  reminders_armed: number
}

export interface AdminUserMemories {
  user_id: string
  facts: Array<Record<string, unknown>>
  preferences: Array<Record<string, unknown>>
  insights: Array<Record<string, unknown>>
  episodes: Array<Record<string, unknown>>
  reminders: Array<Record<string, unknown>>
}
