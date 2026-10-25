import { describe, expect, it, vi } from 'vitest'
import { episodeDetail, getSettings, identity, listInsights, needsConsent, updateSettings } from '../api'
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

it('sends X-Service-Key when saved, and reports 401 in plain words', async () => {
  const { setServiceKey, getServiceKey } = await import('../api')
  setServiceKey('k-123')
  expect(getServiceKey()).toBe('k-123')
  const calls: [string, RequestInit][] = []
  vi.stubGlobal('fetch', vi.fn(async (u: string, i: RequestInit) => {
    calls.push([u, i])
    return new Response(JSON.stringify({ detail: 'Unauthorized' }), { status: 401 })
  }))
  await expect(getSettings()).rejects.toThrow('服务密钥无效或未填写')
  expect(new Headers(calls[0][1].headers).get('X-Service-Key')).toBe('k-123')
  setServiceKey('')
  expect(getServiceKey()).toBe('')
})

it('listInsights pages and returns total (no silent 100 cap)', async () => {
  setUserId('alice')
  const calls: [string, RequestInit][] = []
  vi.stubGlobal('fetch', vi.fn(async (u: string, i: RequestInit) => {
    calls.push([u, i])
    return new Response(JSON.stringify({ items: [], total: 250, offset: 100, limit: 100 }))
  }))
  const page = await listInsights({ offset: 100, limit: 100 })
  expect(calls[0][0]).toContain('/v1/me/insights?offset=100&limit=100')
  expect(page.total).toBe(250)
  expect(page.items).toEqual([])
})

it('identity and episode detail hit the v1/me endpoints', async () => {
  setUserId('alice')
  const calls: [string, RequestInit][] = []
  vi.stubGlobal('fetch', vi.fn(async (u: string, i: RequestInit) => {
    calls.push([u, i])
    if (u.includes('/v1/me/identity')) return new Response(JSON.stringify({ app_id: 'tiancode', user_id: 'alice', bound: false }))
    return new Response(JSON.stringify({
      id: 'ep1', title: '部署服务', source: 'mcp', outcome: 'success', project: null,
      steps: [], cost: {}, distill_status: 'done', salience: 1, skeletonized: false,
      created_at: '2026-10-25T00:00:00Z',
    }))
  }))
  const id = await identity()
  expect(calls[0][0]).toContain('/v1/me/identity?user_id=alice')
  expect(id.bound).toBe(false)
  const ep = await episodeDetail('ep1')
  expect(calls[1][0]).toContain('/v1/me/episodes/ep1?user_id=alice')
  expect(ep.title).toBe('部署服务')
})
