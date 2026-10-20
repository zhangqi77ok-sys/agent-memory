import { describe, expect, it } from 'vitest'
import { needsConsent } from '../api'
import type { Settings } from '../types'

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
