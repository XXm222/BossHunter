import { fireEvent, render, screen, cleanup } from '@testing-library/react'
import { afterEach, expect, it, vi } from 'vitest'
import { OutboundReview } from './OutboundReview'

afterEach(cleanup)
it('requires platform evidence and resolves without executing a send', () => {
  const act = vi.fn(async () => true)
  render(<OutboundReview kind="reply" id="draft" act={act} busy={false} />)
  expect((screen.getByRole('button', { name: '核实未发送' }) as HTMLButtonElement).disabled).toBe(true)
  expect(screen.getByText(/填写后下方按钮才可点击/)).toBeTruthy()
  fireEvent.change(screen.getByLabelText('平台核实依据'), { target: { value: '已核实了' } })
  expect((screen.getByRole('button', { name: '核实未发送' }) as HTMLButtonElement).disabled).toBe(true)
  fireEvent.change(screen.getByLabelText('平台核实依据'), { target: { value: '核对候选人及时间，没有该消息' } })
  fireEvent.click(screen.getByRole('button', { name: '核实未发送' }))
  expect(act).toHaveBeenCalledWith('outbound/resolve', { kind: 'reply', id: 'draft', outcome: 'not_sent', evidence: '核对候选人及时间，没有该消息' }, expect.any(String))
  expect(act).toHaveBeenCalledTimes(1)
})

it('explains when a pending operation prevents verification despite sufficient evidence', () => {
  render(<OutboundReview kind="greeting" id={1} act={vi.fn()} busy />)
  fireEvent.change(screen.getByLabelText('平台核实依据'), { target: { value: '已核对候选人的平台沟通记录' } })
  expect(screen.getByText(/正在处理其他操作/)).toBeTruthy()
  expect((screen.getByRole('button', { name: '核实已发送' }) as HTMLButtonElement).disabled).toBe(true)
})
