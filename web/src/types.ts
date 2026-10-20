export type MemoryKind = 'fact' | 'preference'

export interface MemoryItem {
  id: string
  kind: MemoryKind
  subject: string
  content: string
  version: number
  valid_from: string
  source_session_id?: string | null
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
