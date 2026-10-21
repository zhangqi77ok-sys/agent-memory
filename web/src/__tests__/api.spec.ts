import { describe, expect, it, vi } from 'vitest'
import { getSettings, needsConsent, updateSettings } from '../api'
import type { Settings } from '../types'
import { setUserId } from '../user'

describe('needsConsent', () => {
  it('true when never consented', () => {
    const s: Settings = {
      memory_enabled: false,
      consent_version: null,
      consent_at: null,
      current_consent_version: '2026-10-v1',
    }
    expect(needsConsent(s)).toBe(true)
  })

  it('true when consent version is stale', () => {
    const s: Settings = {
      memory_enabled: true,
      consent_version: 'old',
      consent_at: '2026-01-01T00:00:00Z',
      current_consent_version: '2026-10-v1',
    }
    expect(needsConsent(s)).toBe(true)
  })

  it('false when versions match', () => {
    const s: Settings = {
      memory_enabled: false,
      consent_version: '2026-10-v1',
      consent_at: '2026-10-01T00:00:00Z',
      current_consent_version: '2026-10-v1',
    }
    expect(needsConsent(s)).toBe(false)
  })
})

it('sends user_id as query on GET and in body on PUT, never Authorization', async () => {
  setUserId('alice')
  const calls: [string, RequestInit][] = []
  vi.stubGlobal('fetch', vi.fn(async (u: string, i: RequestInit) => { calls.push([u, i]); return new Response('{}') }))
  await getSettings()
  await updateSettings({ memory_enabled: false })
  expect(calls[0][0]).toContain('/v1/me/settings?user_id=alice')
  expect(new Headers(calls[0][1].headers).has('Authorization')).toBe(false)
  expect(JSON.parse(String(calls[1][1].body))).toMatchObject({ user_id: 'alice', memory_enabled: false })
})
