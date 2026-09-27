import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { PublishedJobs, PublishedJobsState } from './PublishedJobs'
const data: PublishedJobsState = { jobs: [{ id: 'boss-one', platform_id: 'one', title: '开放岗位', status: '开放中', details: ['金华', '7-12K'], selected: 0 }, { id: 'boss-two', platform_id: 'two', title: '关闭岗位', status: '已关闭', details: [], selected: 0 }], sync: { attempted_at: '2026-01-01', synced_at: '2026-01-01' }, budget: { mode: 'platform', limit: 100 }, daily: { sent: 0, attempted: 0, date: '2026-01-01', platform_remaining: null, custom_remaining: null }, selected_count: 0, running: false, blockers: ['请先勾选岗位'] }
afterEach(cleanup)
describe('published employer jobs', () => {
  it('requires explicit selection and saves only selected open IDs', async () => {
    const act = vi.fn(async (_operation: string, _payload?: object, _success?: string) => true)
    render(<PublishedJobs data={data} act={act} busy={false} />)
    const checkbox = screen.getByRole('checkbox') as HTMLInputElement
    expect(checkbox.checked).toBe(false)
    expect(screen.getAllByRole('checkbox')).toHaveLength(1)
    fireEvent.click(checkbox)
    expect(act).not.toHaveBeenCalled()
    fireEvent.click(screen.getByRole('button', { name: '保存岗位选择' }))
    await waitFor(() => expect(act).toHaveBeenCalledWith('jobs/select', { ids: ['boss-one'] }, expect.any(String)))
    expect(act.mock.calls.some(call => String(call[0]).includes('start'))).toBe(false)
  })
  it('persists a custom account-wide daily cap separately', async () => {
    const act = vi.fn(async (_operation: string, _payload?: object, _success?: string) => true)
    render(<PublishedJobs data={data} act={act} busy={false} />)
    expect((screen.getByLabelText('用完平台当天可用额度') as HTMLInputElement).checked).toBe(true)
    fireEvent.click(screen.getByLabelText('自定义每日上限'))
    fireEvent.change(screen.getByLabelText('每日最多招呼人数'), { target: { value: '25' } })
    fireEvent.click(screen.getByRole('button', { name: '保存额度设置' }))
    await waitFor(() => expect(act).toHaveBeenCalledWith('jobs/budget', { mode: 'custom', limit: 25 }, expect.any(String)))
    expect(screen.getByText('未读取')).toBeTruthy()
  })
  it('keeps an unsaved selection through filtering and server refresh', async () => {
    const act = vi.fn(async (_operation: string, _payload?: object, _success?: string) => true)
    const view = render(<PublishedJobs data={data} act={act} busy={false} />)
    fireEvent.click(screen.getByRole('checkbox', { name: '选择开放岗位' }))
    fireEvent.change(screen.getByLabelText('搜索岗位'), { target: { value: '不匹配' } })
    expect(screen.queryByRole('checkbox')).toBeNull()
    view.rerender(<PublishedJobs data={{ ...data, jobs: data.jobs.map(j => ({ ...j })) }} act={act} busy={false} />)
    fireEvent.click(screen.getByRole('button', { name: '清除搜索' }))
    expect((screen.getByRole('checkbox') as HTMLInputElement).checked).toBe(true)
    expect(screen.getByRole('switch').getAttribute('aria-checked')).toBe('true')
    fireEvent.click(screen.getByRole('button', { name: '保存岗位选择' }))
    await waitFor(() => expect(act).toHaveBeenCalledWith('jobs/select', { ids: ['boss-one'] }, expect.any(String)))
  })
  it('supports keyboard tabs and keeps closed jobs unselectable', () => {
    render(<PublishedJobs data={data} act={vi.fn(async () => true)} busy={false} />)
    fireEvent.keyDown(screen.getByRole('tab', { name: /开放中/ }), { key: 'ArrowRight' })
    expect(screen.getByRole('tab', { name: /其他岗位/ }).getAttribute('aria-selected')).toBe('true')
    expect(screen.getByText('关闭岗位')).toBeTruthy()
    expect(screen.queryByRole('checkbox')).toBeNull()
    expect(screen.queryByRole('switch')).toBeNull()
  })
  it('automatically reads jobs once when there has been no sync attempt', async () => {
    const act = vi.fn(async (_operation: string, _payload?: object, _success?: string) => true)
    const value = { ...data, jobs: [], sync: {} }
    const view = render(<PublishedJobs data={value} act={act} busy={false} />)
    await waitFor(() => expect(act).toHaveBeenCalledTimes(1))
    expect(act).toHaveBeenCalledWith('jobs/sync', {}, expect.any(String))
    view.rerender(<PublishedJobs data={value} act={act} busy={false} />)
    expect(act).toHaveBeenCalledTimes(1)
  })
})
