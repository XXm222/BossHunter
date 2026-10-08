import { afterEach, expect, it, vi } from 'vitest'
import { act, cleanup, render, screen } from '@testing-library/react'
import { ReplyProgress } from './ReplyProgress'
import type { State } from './RecruitingPage'

const c = { id: 'c', position_id: 'p', auto_send: 1, taken_over: 0, do_not_contact: 0 } as State['conversations'][number]
const base = { positions: [{ id: 'p', enabled: 1 }], outbox: [], worker: { alive: true, monitor_enabled: true } } as unknown as State
afterEach(() => { cleanup(); vi.useRealTimers() })

it('counts down locally without fetching the platform and never claims to have sent', async () => {
  vi.useFakeTimers(); vi.setSystemTime(1000000)
  const fetcher = vi.spyOn(globalThis, 'fetch')
  render(<ReplyProgress data={{ ...base, reply_progress: { c: { stage: 'waiting_throttle', message: '下一步：核实发送账号', active: true, wait_until: 1120 } } }} conversation={c} />)
  expect(screen.getByText(/约 2 分 0 秒后可继续/)).toBeTruthy()
  await act(async () => { vi.advanceTimersByTime(120000) })
  expect(screen.getByText(/预计等待时间已到，等待后台更新状态/)).toBeTruthy()
  expect(screen.queryByText('已确认发送')).toBeNull()
  expect(fetcher).not.toHaveBeenCalled(); fetcher.mockRestore()
})

it('explains missing browser selection and keeps the auto send switch visible', () => {
  render(<ReplyProgress data={{ ...base, reply_progress: { c: { stage: 'waiting_browser', message: '请在 BOSS 手动打开该候选人的对话', active: false } } }} conversation={c} />)
  expect(screen.getByText('等待打开 BOSS 对话')).toBeTruthy()
  expect(screen.getByText('自动外发：开启')).toBeTruthy()
})

it('marks stopped workers and exhausted budget instead of showing a live countdown', () => {
  render(<ReplyProgress data={{ ...base, worker: { alive: false, monitor_enabled: true }, request_budget: { count: 200, remaining: 0, daily_limit: 200, by_kind: {} }, reply_progress: { c: { stage: 'waiting_throttle', active: true, wait_until: Date.now() / 1000 + 120 } } }} conversation={c} />)
  expect(screen.getByText(/worker 未运行/)).toBeTruthy()
  expect(screen.getByText(/今日请求预算已耗尽/)).toBeTruthy()
  expect(screen.queryByText(/秒后可继续/)).toBeNull()
})

it('uses confirmed outbox results over stale stopped progress', () => {
  render(<ReplyProgress data={{ ...base, outbox: [{ id: 'd', conversation_id: 'c', kind: 'reply', status: 'sent', content: '合成回复', result: '', refs: {} }], reply_progress: { c: { stage: 'checking_browser', active: false } } } as State} conversation={c} />)
  expect(screen.getByText('已确认发送')).toBeTruthy()
})
