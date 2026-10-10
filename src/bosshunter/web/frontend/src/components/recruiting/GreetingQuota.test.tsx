import { afterEach, expect, it } from 'vitest'
import { cleanup, render, screen } from '@testing-library/react'
import { GreetingQuota } from './GreetingQuota'
import type { PublishedJobsState } from '../../pages/PublishedJobs'

const base = { budget: { mode: 'platform', limit: 100 }, daily: { sent: 0, attempted: 0, platform_remaining: null, custom_remaining: null, date: '2026-10-08' } } as PublishedJobsState
afterEach(cleanup)

it('distinguishes no prior read from an expired saved reading', () => {
  const view = render(<GreetingQuota data={{ ...base, daily: { ...base.daily, quota_status: 'unread' } }} />)
  expect(screen.getByText('未读取')).toBeTruthy()
  expect(screen.queryByText(/上次读取/)).toBeNull()
  view.rerender(<GreetingQuota data={{ ...base, daily: { ...base.daily, quota_status: 'expired', quota_last_remaining: 200, quota_updated_at: '2026-10-08T05:07:15Z' } }} />)
  expect(screen.getByText('已过期')).toBeTruthy()
  expect(screen.getByText(/当时剩余 200.*仅供参考/)).toBeTruthy()
  expect(screen.queryByText('未读取')).toBeNull()
})

it('shows a fresh unlimited quota as unlimited and flags expired platform quota in custom mode', () => {
  const view = render(<GreetingQuota data={{ ...base, daily: { ...base.daily, quota_status: 'fresh', quota_unlimited: true } }} />)
  expect(screen.getByText('不限')).toBeTruthy()
  view.rerender(<GreetingQuota compact data={{ ...base, budget: { mode: 'custom', limit: 100 }, daily: { ...base.daily, custom_remaining: 80, quota_status: 'expired' } }} />)
  expect(screen.getByText('80')).toBeTruthy()
  expect(screen.getByText('平台额度：已过期，请重新读取后再使用')).toBeTruthy()
})
