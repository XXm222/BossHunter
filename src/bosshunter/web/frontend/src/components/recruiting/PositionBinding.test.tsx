import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen } from '@testing-library/react'
import { PositionBinding } from './PositionBinding'
import type { State } from '../../pages/RecruitingPage'

afterEach(cleanup)
it.each(['old-position-and-snapshot', 'old-snapshot'])('allows confirming the same platform id after a job rename: %s', mode => {
  const act = vi.fn(async () => true)
  const data = { positions: [{ id: 'boss-a', title: mode === 'old-snapshot' ? '新岗位名' : '旧岗位名' }], recruiting_jobs: { jobs: [
    { id: 'boss-a', platform_id: 'a', title: '新岗位名', status: '开放中', details: [] },
  ] } } as unknown as State
  const conversation = { id: 'candidate', position_id: 'boss-a', snapshot: { position_title: '旧岗位名' } } as unknown as State['conversations'][number]
  render(<PositionBinding data={data} conversation={conversation} act={act} busy={false} />)
  fireEvent.change(screen.getByLabelText('关联平台岗位'), { target: { value: 'boss-a' } })
  const button = screen.getByRole('button', { name: '核实并关联岗位', hidden: true }) as HTMLButtonElement
  expect(button.disabled).toBe(false)
  fireEvent.click(button)
  expect(act).toHaveBeenCalledWith('conversation/link-position', { conversation_id: 'candidate', platform_id: 'a' }, expect.any(String))
})

it('keeps an unchanged binding disabled', () => {
  const data = { positions: [{ id: 'boss-a', title: '岗位名' }], recruiting_jobs: { jobs: [
    { id: 'boss-a', platform_id: 'a', title: '岗位名', status: '开放中', details: [] },
  ] } } as unknown as State
  const conversation = { id: 'candidate', position_id: 'boss-a', snapshot: { position_title: '岗位名' } } as unknown as State['conversations'][number]
  render(<PositionBinding data={data} conversation={conversation} act={vi.fn()} busy={false} />)
  fireEvent.change(screen.getByLabelText('关联平台岗位'), { target: { value: 'boss-a' } })
  expect((screen.getByRole('button', { name: '核实并关联岗位', hidden: true }) as HTMLButtonElement).disabled).toBe(true)
})

it('distinguishes identically named jobs by platform id when explicitly associating', () => {
  const act = vi.fn(async () => true)
  const data = { recruiting_jobs: { jobs: [
    { id: 'boss-a', platform_id: 'a', title: '工程师', status: '开放中', details: ['上海'] },
    { id: 'boss-b', platform_id: 'b', title: '工程师', status: '开放中', details: ['北京'] },
  ] } } as State
  const conversation = { id: 'candidate', position_id: 'context-old' } as State['conversations'][number]
  render(<PositionBinding data={data} conversation={conversation} act={act} busy={false} />)
  fireEvent.change(screen.getByLabelText('关联平台岗位'), { target: { value: 'boss-b' } })
  fireEvent.click(screen.getByRole('button', { name: '核实并关联岗位', hidden: true }))
  expect(act).toHaveBeenCalledWith('conversation/link-position', { conversation_id: 'candidate', platform_id: 'b' }, expect.any(String))
})
