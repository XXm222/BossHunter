import { afterEach, expect, it } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { MemoryRouter } from 'react-router-dom'
import { GreetingConsole } from './GreetingConsole'
import type { State } from './RecruitingPage'

afterEach(cleanup)
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
