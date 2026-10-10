import { afterEach, describe, expect, it, vi } from 'vitest'
import { act as reactAct, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import RecruitingPage from './RecruitingPage'
import { BindingWizard } from './BindingWizard'
import type { State } from './RecruitingPage'

const c = { id: '123456-0', name: '张三', position_id: 'p', updated_at: '2026-01-01', taken_over: 0, do_not_contact: 0, snapshot: { messages: [], coverage: '当前已加载消息' } }
const baseState = { positions: [{ id: 'p', title: '前端工程师', jd: '', enabled: 1, version: 1 }], conversations: [], documents: [], company: { text: '', version: 0, updated_at: null }, assessments: [], events: [], outbox: [], connection: { connected: true, message: '已连接' }, monitor: { running: false, error: '', last_success: null, interval_seconds: 120 }, model_ready: false }

function show(path: string) {
  render(<MemoryRouter initialEntries={[path]}><Routes><Route path="/recruiting" element={<RecruitingPage />} /><Route path="/recruiting/:section" element={<RecruitingPage />} /></Routes></MemoryRouter>)
}

const contacts = [{ ident: '123456-0', name: '张三', position_title: '前端工程师', last_ts: 1700000000000 }]
const preview = { account_uid: '987654', name: '张三', position_title: '前端工程师', message_count: 3, coverage: 'BOSS 历史消息接口当前可返回的全部记录' }

function mockFetch(fetcher: (url: string, init?: RequestInit) => Promise<Response>) {
  vi.stubGlobal('fetch', vi.fn(fetcher))
}

afterEach(() => { cleanup(); vi.unstubAllGlobals() })

describe('binding wizard', () => {
  it('offers explicit job linking for an existing placeholder without guessing a job', async () => {
    const value = { ...baseState, conversations: [{ ...c, position_id: 'context-placeholder' }],
      recruiting_jobs: { jobs: [{ id: 'boss-job', platform_id: 'job', title: '前端工程师' }] } } as unknown as State
    const act = vi.fn(async () => true)
    mockFetch(async url => new Response(JSON.stringify({ result: url.endsWith('/contacts/list') ? contacts : preview })))
    render(<BindingWizard data={value} act={act} busy={false} />)
    fireEvent.click(screen.getByRole('button', { name: '加载联系人列表' }))
    fireEvent.click(await screen.findByRole('button', { name: /张三/ }))
    fireEvent.click(screen.getByRole('button', { name: '预览核实' }))
    expect(await screen.findByRole('button', { name: '确认绑定这个会话' })).toBeTruthy()
    expect((screen.getByRole('button', { name: '确认关联岗位' }) as HTMLButtonElement).disabled).toBe(true)
    expect(act).not.toHaveBeenCalled()
    fireEvent.change(screen.getByLabelText('人工关联平台岗位'), { target: { value: 'job' } })
    fireEvent.click(screen.getByRole('button', { name: '确认关联岗位' }))
    await waitFor(() => expect(act).toHaveBeenCalledWith('conversation/link-position',
      { conversation_id: '123456-0', platform_id: 'job' }, '岗位已关联，请重新预览并确认绑定'))
    await waitFor(() => expect(screen.queryByRole('button', { name: '确认绑定这个会话' })).toBeNull())
    expect(act).toHaveBeenCalledTimes(1)
  })
  it('shows the load button when no conversation is bound', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(baseState))))
    show('/recruiting/binding')
    expect(await screen.findByRole('button', { name: '加载联系人列表' })).toBeTruthy()
    expect(screen.queryByLabelText('沟通岗位')).toBeNull()
  })

  it('shows the load button even when a conversation already exists', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ ...baseState, conversations: [c] }))))
    show('/recruiting/binding')
    expect(await screen.findByRole('button', { name: '加载联系人列表' })).toBeTruthy()
  })

  it('loads contacts and picks a candidate', async () => {
    mockFetch(async (url) => url.endsWith('/contacts/list') ? new Response(JSON.stringify({ ok: true, result: contacts })) : new Response(JSON.stringify(baseState)))
    show('/recruiting/binding')
    fireEvent.click(await screen.findByRole('button', { name: '加载联系人列表' }))
    fireEvent.click(await screen.findByRole('button', { name: /张三/ }))
    expect(await screen.findByText(/已选候选人/)).toBeTruthy()
    expect(screen.getByLabelText('沟通岗位')).toBeTruthy()
  })

  it('previews the picked candidate with its ident and job', async () => {
    const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/contacts/list')) return new Response(JSON.stringify({ ok: true, result: contacts }))
      if (url.endsWith('/binding/preview')) return new Response(JSON.stringify({ ok: true, result: preview }))
      return new Response(JSON.stringify(baseState))
    })
    vi.stubGlobal('fetch', fetcher)
    show('/recruiting/binding')
    fireEvent.click(await screen.findByRole('button', { name: '加载联系人列表' }))
    fireEvent.click(await screen.findByRole('button', { name: /张三/ }))
    fireEvent.click(await screen.findByRole('button', { name: '预览核实' }))
    expect(await screen.findByText('987654')).toBeTruthy()
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith('/api/recruiting/binding/preview', expect.objectContaining({ body: JSON.stringify({ conversation_id: '123456-0', name: '张三', position_title: '前端工程师' }) })))
  })

  it('confirms the binding with the previewed account', async () => {
    const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
      if (url.endsWith('/contacts/list')) return new Response(JSON.stringify({ ok: true, result: contacts }))
      if (url.endsWith('/binding/preview')) return new Response(JSON.stringify({ ok: true, result: preview }))
      if (url.endsWith('/binding/confirm')) return new Response(JSON.stringify({ ok: true }))
      return new Response(JSON.stringify(baseState))
    })
    vi.stubGlobal('fetch', fetcher)
    show('/recruiting/binding')
    fireEvent.click(await screen.findByRole('button', { name: '加载联系人列表' }))
    fireEvent.click(await screen.findByRole('button', { name: /张三/ }))
    fireEvent.click(await screen.findByRole('button', { name: '预览核实' }))
    fireEvent.click(await screen.findByRole('button', { name: /确认绑定这个会话/ }))
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith('/api/recruiting/binding/confirm', expect.objectContaining({ body: JSON.stringify({ conversation_id: '123456-0', name: '张三', position_title: '前端工程师', expected_account: '987654' }) })))
  })
})

describe('request and preview regressions', () => {
const data = { conversations: [], connection: { connected: true, message: '' } } as unknown as State
const contacts = [{ ident: '123-0', name: '合成候选人', position_title: '岗位甲' }]
const preview = { account_uid: '456', name: '合成候选人', position_title: '岗位甲', message_count: 1, coverage: '合成记录' }
const reply = (result: unknown) => new Response(JSON.stringify({ result }))
async function selectContact() {
  fireEvent.click(screen.getByRole('button', { name: '加载联系人列表' }))
  fireEvent.click(await screen.findByRole('button', { name: '合成候选人 · 岗位甲' }))
}

it('invalidates a completed preview when the job title changes', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => reply(url.endsWith('contacts/list') ? contacts : preview)))
  render(<BindingWizard data={data} act={vi.fn()} busy={false} />)
  await selectContact()
  fireEvent.click(screen.getByRole('button', { name: '预览核实' }))
  expect(await screen.findByRole('button', { name: '确认绑定这个会话' })).toBeTruthy()
  fireEvent.change(screen.getByLabelText('沟通岗位'), { target: { value: '岗位乙' } })
  expect(screen.queryByRole('button', { name: '确认绑定这个会话' })).toBeNull()
})

it('prevents concurrent wizard requests and discards an outdated preview', async () => {
  let release!: (value: Response) => void
  const pending = new Promise<Response>(resolve => { release = resolve })
  const fetcher = vi.fn(async (url: string) => url.endsWith('contacts/list') ? reply(contacts) : pending)
  vi.stubGlobal('fetch', fetcher)
  render(<BindingWizard data={data} act={vi.fn()} busy={false} />)
  await selectContact()
  fireEvent.click(screen.getByRole('button', { name: '预览核实' }))
  fireEvent.click(screen.getByRole('button', { name: '预览核实' }))
  expect((screen.getByRole('button', { name: '加载联系人列表' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.click(screen.getByRole('button', { name: '加载联系人列表' }))
  fireEvent.change(screen.getByLabelText('沟通岗位'), { target: { value: '岗位乙' } })
  await reactAct(async () => { release(reply(preview)); await pending })
  expect(fetcher).toHaveBeenCalledTimes(2)
  expect(screen.queryByRole('button', { name: '确认绑定这个会话' })).toBeNull()
})

})
