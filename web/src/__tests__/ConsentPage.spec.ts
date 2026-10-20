import { mount } from '@vue/test-utils'
import { describe, expect, it } from 'vitest'
import ConsentPage from '../components/ConsentPage.vue'

const settings = {
  memory_enabled: false,
  consent_version: null,
  consent_at: null,
  current_consent_version: '2026-10-v1',
}

describe('ConsentPage', () => {
  it('disables submit until checkbox is checked, then emits agree', async () => {
    const w = mount(ConsentPage, { props: { settings } })
    const btn = w.get('[data-testid="consent-submit"]')
    expect(btn.attributes('disabled')).toBeDefined()
    await w.get('[data-testid="consent-check"]').setValue(true)
    expect(btn.attributes('disabled')).toBeUndefined()
    await btn.trigger('click')
    expect(w.emitted('agree')).toHaveLength(1)
  })
})
