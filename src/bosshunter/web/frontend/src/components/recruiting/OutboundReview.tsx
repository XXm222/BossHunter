import { useId, useState } from 'react'
import type { Act } from '../../pages/RecruitingPage'

export function OutboundReview({ kind, id, act, busy }: { kind: 'reply' | 'greeting'; id: string | number; act?: Act; busy: boolean }) {
  const [evidence, setEvidence] = useState('')
  const hintId = useId()
  const length = evidence.trim().length
  const disabled = busy || !act || evidence.trim().length < 5
  const hint = busy ? '正在处理其他操作，请完成后再核实。' : !act ? '当前页面无法提交核实结果，请重新打开招聘端。' : length < 5 ? '请先填写至少 5 个字的平台核实依据，填写后下方按钮才可点击。' : '核实依据已填写，可以选择对应的发送结果。'
  const resolve = (outcome: 'sent' | 'not_sent') => act?.('outbound/resolve', { kind, id, outcome, evidence: evidence.trim() }, '平台结果已记录，不会自动重发')
  return <div className="rc-form">
    <p className="rc-caption">先在 BOSS 核实对应候选人及发送结果。记录核实结果后解除阻塞，不会重发。</p>
    <label>平台核实依据（至少 5 个字）<textarea aria-label="平台核实依据" aria-describedby={hintId} value={evidence} maxLength={1000} onChange={e => setEvidence(e.target.value)} placeholder="请按实际结果填写，例如：已核对该候选人的沟通记录，确认招呼已发送。" /></label>
    <p id={hintId} className="rc-caption" role="status">{hint} 已填写 {length} / 1000 字。</p>
    <div className="rc-actions"><button disabled={disabled} onClick={() => resolve('sent')}>核实已发送</button><button disabled={disabled} onClick={() => resolve('not_sent')}>核实未发送</button></div>
  </div>
}
