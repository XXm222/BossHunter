import { useState } from 'react'
import type { Act, State } from '../../pages/RecruitingPage'

export function PositionBinding({ data, conversation, act, busy }: { data: State; conversation: State['conversations'][number]; act: Act; busy: boolean }) {
  const [selected, setSelected] = useState('')
  const jobs = data.recruiting_jobs?.jobs ?? []
  const chosen = jobs.find(j => j.id === selected)
  const position = data.positions?.find(p => p.id === conversation.position_id)
  const bindingUnchanged = chosen && selected === conversation.position_id
    && chosen.title === position?.title && chosen.title === conversation.snapshot?.position_title
  return <details className="rc-context-details"><summary>核实关联岗位</summary>
    <p className="rc-caption">核对平台岗位后关联。不会复制旧岗位 JD，关联后需重新开启自动外发。</p>
    <label>平台岗位<select aria-label="关联平台岗位" value={selected} onChange={e => setSelected(e.target.value)}><option value="">请选择核实后的岗位</option>{jobs.map(j => <option key={j.id} value={j.id}>{j.title} · {j.details.join(' / ')} · {j.platform_id} · {j.status}</option>)}</select></label>
    <button disabled={busy || !chosen || !!bindingUnchanged} onClick={() => chosen && act('conversation/link-position', { conversation_id: conversation.id, platform_id: chosen.platform_id }, '岗位已关联，自动外发已关闭，请核对 JD')}>核实并关联岗位</button>
    {!jobs.length && <p className="rc-caption">请先同步已发布岗位。</p>}
  </details>
}
