import { beforeEach, describe, expect, it } from 'vitest'
import { clearUserId, getUserId, setUserId } from '../user'

describe('user id', () => {
  beforeEach(() => {
    localStorage.clear()
    window.history.replaceState({}, '', '/memory/ui/')
  })
  it('prefills from ?user_id=', () => {
    window.history.replaceState({}, '', '/memory/ui/?user_id=alice')
    expect(getUserId()).toBe('alice')
  })
  it('persists and clears', () => {
    setUserId('bob')
    expect(getUserId()).toBe('bob')
    clearUserId()
    expect(getUserId()).toBeNull()
  })
})
