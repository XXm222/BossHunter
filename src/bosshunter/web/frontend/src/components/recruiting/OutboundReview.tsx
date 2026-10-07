import { useState } from 'react'
import type { Act } from '../../pages/RecruitingPage'

export function OutboundReview({ kind, id, act, busy }: { kind: 'reply' | 'greeting'; id: string | number; act?: Act; busy: boolean }) {
  const [evidence, setEvidence] = useState('')
  const disabled = busy || !act || evidence.trim().length < 5
  const resolve = (outcome: 'sent' | 'not_sent') => act?.('outbound/resolve', { kind, id, outcome, evidence: evidence.trim() }, '平台结果已记录，不会自动重发')
  return <div className="rc-form">
    <p className="rc-caption">先在 BOSS 核实对应候选人及发送结果。记录核实结果后解除阻塞，不会重发。</p>
    <label>平台核实依据<textarea aria-label="平台核实依据" value={evidence} maxLength={1000} onChange={e => setEvidence(e.target.value)} placeholder="例如：核对候选人、消息时间及正文，平台没有这条消息。" /></label>
    <div className="rc-actions"><button disabled={disabled} onClick={() => resolve('sent')}>核实已发送</button><button disabled={disabled} onClick={() => resolve('not_sent')}>核实未发送</button></div>
  </div>
}
