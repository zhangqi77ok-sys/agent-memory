import { expect, test, type Page } from '@playwright/test'

const SETTINGS_OFF = {
  memory_enabled: false,
  consent_version: null,
  consent_at: null,
  current_consent_version: '2026-10-v1',
}

const SETTINGS_ON = {
  memory_enabled: true,
  consent_version: '2026-10-v1',
  consent_at: '2026-10-20T12:00:00Z',
  current_consent_version: '2026-10-v1',
}

const MEMORIES = {
  items: [
    {
      id: 'f1',
      kind: 'fact',
      subject: 'drink',
      content: '每天早上喝一杯咖啡',
      version: 1,
      valid_from: '2026-10-20T08:00:00Z',
      source_session_id: 's-9',
    },
    {
      id: 'p1',
      kind: 'preference',
      subject: 'lang',
      content: 'zh',
      version: 1,
      valid_from: '2026-10-20T08:01:00Z',
      source_session_id: null,
    },
  ],
  total: 2,
  offset: 0,
  limit: 100,
}

async function mockApi(page: Page) {
  let settings = { ...SETTINGS_OFF }
  let memories = structuredClone(MEMORIES)

  await page.route('**/memory/v1/me/**', async (route) => {
    const req = route.request()
    const url = new URL(req.url())
    const path = url.pathname
    const method = req.method()
    if (method === 'GET' || method === 'DELETE') {
      expect(url.searchParams.get('user_id')).toBe('alice')
    } else if (req.postData()) {
      expect((req.postDataJSON() as { user_id?: string }).user_id).toBe('alice')
    }
    expect(req.headers()['authorization']).toBeUndefined()

    if (path.endsWith('/v1/me/settings') && method === 'GET') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(settings) })
    }
    if (path.endsWith('/v1/me/settings') && method === 'PUT') {
      const body = req.postDataJSON() as { memory_enabled: boolean; consent_version?: string }
      if (body.memory_enabled && body.consent_version !== '2026-10-v1') {
        return route.fulfill({
          status: 409,
          contentType: 'application/json',
          body: JSON.stringify({ detail: { code: 'consent_required' } }),
        })
      }
      settings = {
        memory_enabled: body.memory_enabled,
        consent_version: body.memory_enabled ? '2026-10-v1' : settings.consent_version,
        consent_at: body.memory_enabled ? '2026-10-21T00:00:00Z' : settings.consent_at,
        current_consent_version: '2026-10-v1',
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(settings) })
    }
    if (path.endsWith('/v1/me/memories') && method === 'GET') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(memories) })
    }
    if (path.endsWith('/v1/me/memories') && method === 'DELETE') {
      memories = { items: [], total: 0, offset: 0, limit: 100 }
      return route.fulfill({ status: 204, body: '' })
    }
    if (/\/v1\/me\/memories\/[^/]+$/.test(path) && method === 'PATCH') {
      const id = path.split('/').pop()!
      const body = req.postDataJSON() as { content: string }
      memories.items = memories.items.map((i) =>
        i.id === id ? { ...i, content: body.content, version: i.version + 1 } : i,
      )
      const item = memories.items.find((i) => i.id === id)!
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(item) })
    }
    if (/\/v1\/me\/memories\/[^/]+$/.test(path) && method === 'DELETE') {
      const id = path.split('/').pop()!
      memories.items = memories.items.filter((i) => i.id !== id)
      memories.total = memories.items.length
      return route.fulfill({ status: 204, body: '' })
    }
    if (path.endsWith('/v1/me/memories/export') && method === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify({ exported_at: '2026-10-21T00:00:00Z', facts: [], preferences: [] }),
      })
    }
    if (path.includes('/v1/me/jobs') && method === 'GET') {
      return route.fulfill({
        status: 200,
        contentType: 'application/json',
        body: JSON.stringify([
          {
            job_id: 'j1',
            session_id: 's-9',
            status: 'done',
            attempts: 1,
            last_error: null,
            created_at: '2026-10-20T08:00:00Z',
          },
        ]),
      })
    }
    return route.fulfill({ status: 404, body: 'unmocked ' + method + ' ' + path })
  })
}

test('happy path: consent → list → correct → delete → clear', async ({ page }) => {
  await mockApi(page)
  await page.goto('?user_id=alice')

  await expect(page.getByTestId('consent-page')).toBeVisible()
  await page.getByTestId('consent-check').check()
  await page.getByTestId('consent-submit').click()

  await expect(page.getByTestId('memory-panel')).toBeVisible()
  await expect(page.getByTestId('memory-f1')).toContainText('每天早上喝一杯咖啡')
  await expect(page.getByTestId('jobs-list')).toContainText('完成')

  await page.getByTestId('memory-f1').getByTestId('edit-btn').click()
  await page.getByTestId('edit-input').fill('改成每天喝绿茶')
  await page.getByTestId('edit-save').click()
  await expect(page.getByTestId('notice')).toContainText('已纠正')
  await expect(page.getByTestId('memory-f1')).toContainText('改成每天喝绿茶')

  await page.getByTestId('memory-p1').getByTestId('delete-btn').click()
  await expect(page.getByTestId('notice')).toContainText('已删除')
  await expect(page.getByTestId('memory-p1')).toHaveCount(0)

  await page.getByTestId('clear-btn').click()
  await expect(page.getByTestId('confirm-dialog')).toBeVisible()
  await page.getByTestId('confirm-ok').click()
  await expect(page.getByTestId('empty-list')).toBeVisible()
})

async function enterPanel(page: Page) {
  await mockApi(page)
  await page.goto('?user_id=alice')
  await page.getByTestId('consent-check').check()
  await page.getByTestId('consent-submit').click()
  await expect(page.getByTestId('memory-panel')).toBeVisible()
}

test('export downloads a JSON file', async ({ page }) => {
  await enterPanel(page)
  const [download] = await Promise.all([
    page.waitForEvent('download'),
    page.getByTestId('export-btn').click(),
  ])
  expect(download.suggestedFilename()).toMatch(/^my-memories-.*\.json$/)
  await expect(page.getByTestId('notice')).toContainText('导出')
})

test('typing user_id and clicking 使用 opens the panel', async ({ page }) => {
  await mockApi(page)
  await page.goto('/')
  await expect(page.getByTestId('error')).toContainText('请填写 user_id')
  await page.getByTestId('user-id').fill('alice')
  await page.getByTestId('use-user').click()
  await expect(page.getByTestId('consent-page')).toBeVisible()
  await page.getByTestId('logout').click()
  await expect(page.getByTestId('consent-page')).toHaveCount(0)
  await expect(page.getByTestId('error')).toContainText('请填写 user_id')
})

test('turning off master toggle shows 不再写入 notice', async ({ page }) => {
  await enterPanel(page)
  const toggle = page.getByTestId('master-toggle')
  await expect(toggle).toHaveAttribute('aria-pressed', 'true')
  await toggle.click()
  await expect(page.getByTestId('notice')).toContainText('不再写入')
  await expect(toggle).toHaveAttribute('aria-pressed', 'false')
})
