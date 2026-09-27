import { afterEach, describe, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor, within } from '@testing-library/react'
import { MemoryRouter, Route, Routes } from 'react-router-dom'
import RecruitingPage from './RecruitingPage'
import { Sidebar } from '../components/layout/Sidebar'
const c = { id: 'sample', name: '测试候选人', position_id: 'p', updated_at: '2026-01-01', taken_over: 0, do_not_contact: 0, snapshot: { messages: [{ direction: 'out', kind: 'text', text: '你有电商经验吗？', time: '' }, { direction: 'in', kind: 'text', text: '有的，做过三年。', time: '' }], coverage: '当前已加载消息' } }
const state = { positions: [{ id: 'p', title: '财务', jd: '电商财务经验', enabled: 1, version: 1 }], conversations: [c], documents: [], company: { text: '公司说明原文', version: 1, updated_at: null }, assessments: [], events: [], outbox: [], connection: { connected: false, message: '尚未检查浏览器连接' }, monitor: { running: false, error: '', last_success: null, interval_seconds: 120 }, model_ready: false }
function show(path: string) { render(<MemoryRouter initialEntries={[path]}><Sidebar /><Routes><Route path="/recruiting" element={<RecruitingPage />} /><Route path="/recruiting/:section" element={<RecruitingPage />} /><Route path="/recruiting/candidates/:id" element={<RecruitingPage />} /></Routes></MemoryRouter>) }
afterEach(() => { cleanup(); vi.unstubAllGlobals() })
const seed = (value = state) => vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify(value))))
describe('recruiting workspace', () => {
  it('shows automatic assessment waiting for a model without inventing a score', async () => {
    seed({ ...state, resume_processing: { sample: { status: 'waiting_model', message: '简历已读取，等待配置模型；配置后下次监测自动评分' } } } as typeof state)
    show('/recruiting/candidates/sample?panel=resume')
    expect(await screen.findByText('简历已读取，等待配置模型；配置后下次监测自动评分')).toBeTruthy()
    expect((screen.getByRole('button', { name: '立即评分' }) as HTMLButtonElement).disabled).toBe(true)
  })
  it('offers an explicit score retry while preserving the received resume', async () => {
    const value = { ...state, model_ready: true, documents: [{ id: 'doc', conversation_id: 'sample', text: '真实简历保留', complete: 0, source: 'pdf' }], resume_processing: { sample: { status: 'failed', message: '自动评分未完成，简历已保留；请点击重试评分' } } }
    const fetcher = vi.fn(async (_url: string, init?: RequestInit) => new Response(JSON.stringify(init ? { ok: true } : value)))
    vi.stubGlobal('fetch', fetcher)
    show('/recruiting/candidates/sample?panel=resume')
    fireEvent.click(await screen.findByRole('button', { name: '重试评分' }))
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith('/api/recruiting/assess', expect.objectContaining({ body: JSON.stringify({ conversation_id: 'sample' }) })))
    expect(screen.getByText('真实简历保留')).toBeTruthy()
  })
  it('allows resume monitoring before model configuration', async () => {
    seed(); show('/recruiting/candidates/sample')
    fireEvent.click(await screen.findByRole('button', { name: '监测设置' }))
    expect((screen.getByRole('button', { name: '开启消息与简历监测' }) as HTMLButtonElement).disabled).toBe(false)
  })
  it('returns to the composer when adopting a draft from the mobile assistant', async () => {
    seed({ ...state, outbox: [{ id: 'd', conversation_id: 'sample', kind: 'reply', content: '可核对的草稿', status: 'draft', refs: {}, result: '' }] } as typeof state)
    show('/recruiting/candidates')
    fireEvent.click(await screen.findByRole('button', { name: '资料与助手' }))
    fireEvent.click(screen.getByRole('button', { name: '编辑这段回复' }))
    expect(screen.getByRole('button', { name: '对话' }).getAttribute('aria-pressed')).toBe('true')
    expect((screen.getByLabelText('回复内容') as HTMLTextAreaElement).value).toBe('可核对的草稿')
  })
  it('opens the first conversation directly without triggering browser operations', async () => {
    seed(); show('/recruiting/candidates')
    expect(await screen.findByRole('region', { name: '当前候选人对话' })).toBeTruthy()
    expect(screen.getByRole('complementary', { name: '候选人资料与回复助手' })).toBeTruthy()
    expect(screen.queryByRole('link', { name: '进入沟通' })).toBeNull()
    expect(vi.mocked(fetch).mock.calls.every(call => !call[1])).toBe(true)
  })
  it('filters the list without silently replacing the open conversation', async () => {
    seed(); show('/recruiting/candidates')
    fireEvent.change(await screen.findByLabelText('搜索候选人'), { target: { value: '不存在' } })
    expect(screen.getByText('没有符合条件的候选人')).toBeTruthy()
    expect(within(screen.getByRole('region', { name: '当前候选人对话' })).getByText('你有电商经验吗？')).toBeTruthy()
  })
  it('blocks new reply preparation when a previous result is uncertain', async () => {
    seed({ ...state, model_ready: true, outbox: [{ id: 'd', conversation_id: 'sample', kind: 'reply', content: '待核实内容', status: 'uncertain', refs: {}, result: '' }] } as typeof state)
    show('/recruiting/candidates')
    expect((await screen.findByRole('button', { name: '生成上下文回复' }) as HTMLButtonElement).disabled).toBe(true)
    fireEvent.change(screen.getByLabelText('回复内容'), { target: { value: '新的回复' } })
    expect((screen.getByRole('button', { name: '保存这段回复' }) as HTMLButtonElement).disabled).toBe(true)
  })
  it('replaces the standalone invitation page with the candidate workflow', async () => {
    seed(); show('/recruiting/invitations')
    expect(await screen.findByText('打开一个候选人，连续处理消息、简历评估和面试安排。')).toBeTruthy()
    expect(screen.queryByRole('link', { name: '邀约草稿' })).toBeNull()
    expect(screen.queryByRole('button', { name: '保存本地安排' })).toBeNull()
  })
  it('keeps interview sending disabled inside a candidate', async () => {
    seed({ ...state, outbox: [{ id: 'd', conversation_id: 'sample', kind: 'invitation', content: '{"mode":"online"}', status: 'draft', refs: {}, result: '' }] } as typeof state)
    show('/recruiting/candidates/sample?panel=interview')
    expect((await screen.findByRole('button', { name: '面试邀约发送已禁用' }) as HTMLButtonElement).disabled).toBe(true)
  })
  it('keeps company information to one text field plus the job description', async () => {
    seed(); show('/recruiting/company')
    expect(await screen.findByLabelText('公司说明')).toBeTruthy()
    expect(screen.getByLabelText('财务 · 岗位 JD')).toBeTruthy()
    expect(screen.getAllByRole('textbox')).toHaveLength(2)
    expect(screen.queryByText('关键词')).toBeNull()
    expect(screen.queryByRole('checkbox')).toBeNull()
  })
  it('saves the company text without keyword, source or approval fields', async () => {
    const fetcher = vi.fn(async (_url: string, init?: RequestInit) => new Response(JSON.stringify(init ? { ok: true } : state)))
    vi.stubGlobal('fetch', fetcher); show('/recruiting/company')
    fireEvent.change(await screen.findByLabelText('公司说明'), { target: { value: '新公司说明' } })
    fireEvent.click(screen.getByRole('button', { name: '保存公司说明' }))
    await waitFor(() => expect(fetcher).toHaveBeenCalledWith('/api/recruiting/company', expect.objectContaining({ body: JSON.stringify({ text: '新公司说明' }) })))
  })
  it('shows both sides and the facts used by the reply agent', async () => {
    seed(); show('/recruiting/candidates/sample')
    expect(await screen.findByText('你有电商经验吗？')).toBeTruthy()
    expect(within(screen.getByRole('region', { name: '当前候选人对话' })).getByText('有的，做过三年。')).toBeTruthy()
    expect(screen.getByText('公司说明原文')).toBeTruthy()
    expect(screen.getByText('电商财务经验')).toBeTruthy()
    expect(screen.getByText(/2 条对话消息/)).toBeTruthy()
  })
  it('does not silently show another candidate for an unknown ID', async () => {
    seed(); show('/recruiting/candidates/missing')
    expect(await screen.findByText('没有找到该候选人，请返回工作台核对。')).toBeTruthy()
    expect(screen.queryByRole('button', { name: '同步会话' })).toBeNull()
  })
  it('shows failed loading distinctly from an empty data set', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ error: '本地服务暂不可用' }), { status: 503 })))
    show('/recruiting'); expect(await screen.findByRole('alert')).toBeTruthy()
    expect(screen.getByText('本地服务暂不可用')).toBeTruthy()
    expect(screen.queryByText('还没有导入样本。请先选择 Chrome 中的招聘会话。')).toBeNull()
  })
})
