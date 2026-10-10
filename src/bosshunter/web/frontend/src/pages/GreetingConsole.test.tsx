import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { GreetingConsole } from './GreetingConsole'
import type { State } from './RecruitingPage'
import { RecruitingOverview } from './RecruitingOverview'

afterEach(cleanup)
it('shows actual daily job counts and includes older unresolved records only in the review count', () => {
  const value = { ...state, discovery: { running: true }, recruiting_jobs: { ...state.recruiting_jobs!, attempts: [
    { id: 1, job_id: 'j1', candidate_id: 'sent', status: 'sent', created_at: '2026-09-18T18:00:00Z' },
    { id: 2, job_id: 'j1', candidate_id: 'waiting', status: 'uncertain', day: '2026-09-19', created_at: '2026-09-19T02:00:00Z' },
    { id: 3, job_id: 'j1', candidate_id: 'old', status: 'uncertain', day: '2026-09-18', created_at: '2026-09-18T01:00:00Z' },
    { id: 4, job_id: 'other', candidate_id: 'other', status: 'sent', day: '2026-09-19', created_at: '2026-09-19T03:00:00Z' },
  ] } }
  show(value)
  const row = screen.getAllByText('财务主管')[0].closest('tr')!
  expect(within(row).getByText('1')).toBeTruthy()
  expect(within(row).getByText('今日已尝试 2 次 · 待核实 2 条（含跨日）')).toBeTruthy()
  expect(screen.queryByText('尚未开始处理')).toBeNull()
})
it('shows recorded job counts on the overview and does not claim a blocked job is ready', () => {
  render(<MemoryRouter><RecruitingOverview data={state} act={vi.fn(async () => true)} busy={false} /></MemoryRouter>)
  const row = screen.getAllByText('财务主管')[0].closest('tr')!
  expect(within(row).getByText('1')).toBeTruthy()
  expect(screen.getByText('需处理运行条件')).toBeTruthy()
  expect(screen.queryByText(/已选 1 个岗位 · 可执行/)).toBeNull()
  expect(screen.queryByText(/岗位招呼明细暂无数据/)).toBeNull()
})
it('allows stopping greeting discovery during another pending action', () => {
  const act = vi.fn(async () => true)
  render(<MemoryRouter><GreetingConsole data={{ ...state, discovery: { running: true } }} act={act} busy /></MemoryRouter>)
  fireEvent.click(screen.getByRole('button', { name: '停止' }))
  expect(act).toHaveBeenCalledWith('discover/stop', {}, expect.any(String))
})
it('provides a resolution form for a previous-day interrupted greeting', () => {
  const act = vi.fn(async () => true)
  const value = { ...state, recruiting_jobs: { ...state.recruiting_jobs!, attempts: [{ id: 10, job_id: 'j1', candidate_id: 'old', status: 'sending', day: '2000-01-01', created_at: '2000-01-01T01:00:00Z' }] } }
  render(<MemoryRouter><GreetingConsole data={value} act={act} /></MemoryRouter>)
  fireEvent.click(screen.getByRole('button', { name: '查看第 10 条触达记录' }))
  fireEvent.change(screen.getByRole('textbox', { name: '平台核实依据' }), { target: { value: '核对平台，没有此条招呼' } })
  fireEvent.click(screen.getByRole('button', { name: '核实未发送' }))
  expect(act).toHaveBeenCalledWith('outbound/resolve', expect.objectContaining({ id: 10, outcome: 'not_sent' }), expect.any(String))
})
const state: State = { positions: [], documents: [], outbox: [], assessments: [], events: [], connection: { connected: false, message: '' }, monitor: { running: false, error: '', last_success: null, interval_seconds: 120 }, model_ready: false, conversations: [], recruiting_jobs: { jobs: [{ id: 'j1', title: '财务主管', details: [], selected: 1, status: '开放中', platform_id: '1' }], sync: {}, budget: { mode: 'platform', limit: 100 }, daily: { sent: 1, attempted: 2, platform_remaining: null, custom_remaining: null, date: '2026-09-19' }, selected_count: 1, running: false, blockers: ['执行待接通'], attempts: [{ id: 1, job_id: 'j1', candidate_id: 'unknown', status: 'uncertain', created_at: '2026-09-19T01:00:00Z' }, { id: 2, job_id: 'j1', candidate_id: 'known', status: 'sent', created_at: '2026-09-19T01:02:00Z' }] } } as State
function show(value = state) { render(<MemoryRouter><GreetingConsole data={value} /></MemoryRouter>) }
it('does not present an executor or unknown platform quota as ready', () => {
  show()
  expect((screen.getByRole('button', { name: '开始执行' }) as HTMLButtonElement).disabled).toBe(true)
  expect(screen.getByText('未读取')).toBeTruthy()
  expect(screen.queryByRole('progressbar')).toBeNull()
})
it('filters uncertain results and never guesses a conversation association', () => {
  show({ ...state, conversations: [{ id: 'different', name: '不相关会话' }] } as State)
  fireEvent.click(screen.getByRole('button', { name: '待核实 1' }))
  expect(screen.queryByRole('button', { name: '查看第 2 条触达记录' })).toBeNull()
  fireEvent.click(screen.getByRole('button', { name: '查看第 1 条触达记录' }))
  expect(screen.queryByRole('link', { name: /进入候选人沟通/ })).toBeNull()
  expect(screen.getByText(/无法还原当时的匹配判断/)).toBeTruthy()
})
it('opens only the exactly linked conversation and clears stale details when filtering', () => {
  show({ ...state, conversations: [{ id: 'known', name: '测试候选人' }] } as State)
  fireEvent.click(screen.getByRole('button', { name: '查看第 2 条触达记录' }))
  expect(screen.getByRole('link', { name: /进入候选人沟通/ }).getAttribute('href')).toBe('/recruiting/candidates/known')
  fireEvent.change(screen.getByRole('textbox', { name: '搜索触达记录' }), { target: { value: '不存在' } })
  expect(screen.getByText('没有符合条件的触达记录')).toBeTruthy()
  expect(screen.queryByRole('link', { name: /进入候选人沟通/ })).toBeNull()
})
it('shows a setup action without selecting any job on behalf of the user', () => {
  show({ ...state, recruiting_jobs: { ...state.recruiting_jobs!, jobs: [] } })
  expect(screen.getByRole('link', { name: '选择招聘岗位' }).getAttribute('href')).toBe('/recruiting/positions')
  expect(screen.getByText('先选择需要 Agent 处理的岗位')).toBeTruthy()
})
it('allows starting a quota refresh without treating the cached balance as available', () => {
  const act = vi.fn(async () => true)
  const value = { ...state, recruiting_jobs: { ...state.recruiting_jobs!, can_start: true, blockers: ['平台剩余额度已过期，请先读取额度'], attempts: [] } }
  render(<MemoryRouter><GreetingConsole data={value} act={act} /></MemoryRouter>)
  fireEvent.click(screen.getByRole('button', { name: '开始执行' }))
  expect(act).toHaveBeenCalledWith('discover/run', {}, expect.any(String))
  expect(screen.getByText('未读取')).toBeTruthy()
})
