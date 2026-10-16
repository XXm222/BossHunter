import { afterEach, expect, it, vi } from 'vitest'
import { cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react'
import { PacingSettings, type PacingState } from './PacingSettings'

const pacing: PacingState = {
  saved: { read_delay_min: 120, read_delay_max: 180, monitor_interval_seconds: 600 },
  active: { read_delay_min: 120, read_delay_max: 180, monitor_interval_seconds: 600 },
  fields: [{ key: 'read_delay_min', label: '读取操作间隔下限', unit: '秒', min: 1 },
    { key: 'read_delay_max', label: '读取操作间隔上限', unit: '秒', min: 1 },
    { key: 'monitor_interval_seconds', label: 'HTTP 轮询间隔', unit: '秒', min: 60 }],
  ranges: [['read_delay_min', 'read_delay_max']], restart_required: false, restart_mode: 'manual',
}
const input = () => screen.getByLabelText('读取操作间隔下限（秒）') as HTMLInputElement
const edit = (value = '90') => fireEvent.change(input(), { target: { value } })
const submit = () => fireEvent.submit(screen.getByRole('form', { name: '招聘端节流设置' }))
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks() })

it('cancelling confirmation discards edits without writing or restarting', () => {
  const fetcher = vi.fn()
  vi.stubGlobal('fetch', fetcher)
  const confirm = vi.spyOn(window, 'confirm').mockReturnValue(false)
  const refresh = vi.fn()
  render(<PacingSettings pacing={pacing} refresh={refresh} />)
  edit(); submit()
  expect(confirm).toHaveBeenCalledWith(expect.stringContaining('120 → 90 秒'))
  expect(input().value).toBe('120')
  expect(screen.getByText('已取消修改，参数未保存')).toBeTruthy()
  expect(fetcher).not.toHaveBeenCalled()
  expect(refresh).not.toHaveBeenCalled()
})

it('the cancel button restores saved values without asking for confirmation', () => {
  const confirm = vi.spyOn(window, 'confirm')
  render(<PacingSettings pacing={pacing} refresh={vi.fn()} />)
  edit(); fireEvent.click(screen.getByRole('button', { name: '取消修改' }))
  expect(input().value).toBe('120')
  expect(confirm).not.toHaveBeenCalled()
})

it('confirmed saving sends only pacing and displays persistent manual restart instructions', async () => {
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  const fetcher = vi.fn(async () => new Response(JSON.stringify({ ok: true })))
  vi.stubGlobal('fetch', fetcher)
  const refresh = vi.fn(async () => {})
  render(<PacingSettings pacing={pacing} refresh={refresh} />)
  edit(); submit()
  await waitFor(() => expect(refresh).toHaveBeenCalledTimes(1))
  expect(fetcher).toHaveBeenCalledTimes(1)
  expect(fetcher).toHaveBeenCalledWith('/api/recruiting/pacing', expect.objectContaining({
    body: JSON.stringify({ confirmed: true, expected: pacing.saved, values: { ...pacing.saved, read_delay_min: 90 } }),
  }))
  expect(screen.getByText(/未重启的进程继续使用原参数/)).toBeTruthy()
  expect(input().value).toBe('90')
  expect(screen.getByText('当前生效：120 秒')).toBeTruthy()
})

it('rejects inverted ranges and empty fields before asking or writing', () => {
  const fetcher = vi.fn()
  vi.stubGlobal('fetch', fetcher)
  const confirm = vi.spyOn(window, 'confirm')
  render(<PacingSettings pacing={pacing} refresh={vi.fn()} />)
  edit('200'); submit()
  expect(screen.getByRole('alert').textContent).toContain('不能大于上限')
  edit(''); submit()
  expect(screen.getByRole('alert').textContent).toContain('至少为 1')
  expect(fetcher).not.toHaveBeenCalled()
  expect(confirm).not.toHaveBeenCalled()
})

it('failed saving keeps the edit available and does not report a pending restart', async () => {
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ error: '文件不可写' }), { status: 500 })))
  const refresh = vi.fn()
  render(<PacingSettings pacing={pacing} refresh={refresh} />)
  edit(); submit()
  expect(await screen.findByRole('alert')).toHaveProperty('textContent', '文件不可写')
  expect(input().value).toBe('90')
  expect(screen.queryByText(/未重启的进程继续使用原参数/)).toBeNull()
  expect(refresh).not.toHaveBeenCalled()
})

it('a failed state refresh does not misreport a successful save as failed', async () => {
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ ok: true }))))
  render(<PacingSettings pacing={pacing} refresh={vi.fn(async () => { throw new Error('offline') })} />)
  edit(); submit()
  expect(await screen.findByText(/参数已保存，待重启生效；状态刷新失败/)).toBeTruthy()
  expect(input().value).toBe('90')
  expect(screen.queryByRole('alert')).toBeNull()
})

it('polling preserves edits and the original comparison values for conflict detection', async () => {
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  const fetcher = vi.fn(async (_url: string, _options?: RequestInit) => new Response(JSON.stringify({ error: '参数已变化' }), { status: 409 }))
  vi.stubGlobal('fetch', fetcher)
  const page = render(<PacingSettings pacing={pacing} refresh={vi.fn()} />)
  edit()
  page.rerender(<PacingSettings pacing={{ ...pacing, saved: { ...pacing.saved, read_delay_min: 100 } }} refresh={vi.fn()} />)
  expect(input().value).toBe('90')
  submit()
  await screen.findByRole('alert')
  expect(JSON.parse(String(fetcher.mock.calls[0][1]?.body)).expected).toEqual(pacing.saved)
})

it('shows the pending restart notice after reloading saved but inactive values', () => {
  render(<PacingSettings pacing={{ ...pacing, saved: { ...pacing.saved, read_delay_min: 90 }, restart_required: true }} refresh={vi.fn()} />)
  expect(input().value).toBe('90')
  expect(screen.getByText(/请退出 BossHunter 程序后重新打开/)).toBeTruthy()
})

it('clears the local restart reminder once restarted processes report the saved values', async () => {
  vi.spyOn(window, 'confirm').mockReturnValue(true)
  vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ ok: true }))))
  const page = render(<PacingSettings pacing={pacing} refresh={vi.fn(async () => {})} />)
  edit(); submit()
  await screen.findByText('参数已保存，待重启生效')
  const updated = { ...pacing.saved, read_delay_min: 90 }
  page.rerender(<PacingSettings pacing={{ ...pacing, saved: updated, active: updated, worker_active: updated }} refresh={vi.fn()} />)
  expect(screen.queryByText(/请退出 BossHunter 程序后重新打开/)).toBeNull()
})

it('distinguishes the old worker values when only the web service has restarted', () => {
  const updated = { ...pacing.saved, read_delay_min: 90 }
  render(<PacingSettings pacing={{ ...pacing, saved: updated, active: updated, worker_active: pacing.active, restart_required: true }} refresh={vi.fn()} />)
  expect(screen.getByText('当前生效：90 秒；后台监测：120 秒')).toBeTruthy()
  expect(screen.getByText(/待重启生效/)).toBeTruthy()
})
