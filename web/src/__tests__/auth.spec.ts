import { beforeEach, describe, expect, it } from 'vitest'
import { clearToken, getStoredToken, resolveToken } from '../auth'

describe('resolveToken', () => {
  beforeEach(() => {
    localStorage.clear()
    window.history.replaceState({}, '', '/memory/ui/')
  })

  it('reads ?token= and persists to localStorage, then strips query', () => {
    window.history.replaceState({}, '', '/memory/ui/?token=abc.jwt&x=1')
    expect(resolveToken()).toBe('abc.jwt')
    expect(getStoredToken()).toBe('abc.jwt')
    expect(window.location.search).toBe('?x=1')
  })

  it('falls back to localStorage', () => {
    localStorage.setItem('memsvc_token', 'stored')
    expect(resolveToken()).toBe('stored')
  })

  it('clearToken removes storage', () => {
    localStorage.setItem('memsvc_token', 'x')
    clearToken()
    expect(getStoredToken()).toBeNull()
  })
})
