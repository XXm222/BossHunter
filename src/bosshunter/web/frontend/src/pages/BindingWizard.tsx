import { useState } from 'react'
import { Link } from 'react-router-dom'
import { Loader2, ShieldCheck } from 'lucide-react'
import type { Act, State } from './RecruitingPage'

type Preview = { account_uid: string; name: string; position_title: string; message_count: number; coverage: string }

export function BindingWizard({ data, act, busy }: { data: State; act: Act; busy: boolean }) {
  const [conversationId, setConversationId] = useState('')
  const [name, setName] = useState('')
  const [positionTitle, setPositionTitle] = useState('')
  const [preview, setPreview] = useState<Preview | null>(null)
  const [checking, setChecking] = useState(false)
  const [previewError, setPreviewError] = useState('')

  // 试点只绑一个会话：conversations 非空即已绑定，不允许重复绑定。
  if (data.conversations.length > 0) {
    const c = data.conversations[0]
    return <div className="rc-panel rc-empty"><p>已绑定候选人：<strong>{c.name}</strong></p><Link to="/recruiting/candidates">进入候选人沟通</Link></div>
  }

  const formReady = conversationId.trim() && name.trim() && positionTitle.trim()

  // 预览不走 act（act 不返回结果），直接 fetch 拿 account_uid 等核实信息。
  const runPreview = async () => {
    setChecking(true); setPreviewError(''); setPreview(null)
    try {
      const response = await fetch('/api/recruiting/binding/preview', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ conversation_id: conversationId.trim(), name: name.trim(), position_title: positionTitle.trim() }),
      })
      const value = await response.json()
      if (!response.ok) throw new Error(value.error || '预览未完成')
      setPreview(value.result)
    } catch (e) {
      setPreviewError(e instanceof Error ? e.message : '预览未完成')
    } finally {
      setChecking(false)
    }
  }

  return <div className="rc-panel rc-form">
    <h3>绑定候选人会话</h3>
    <p className="rc-muted">首次使用需先绑定一个候选人会话。本地模式只通过 Chrome 登录状态后台读取，不会操作标签页或发送消息。</p>

    {!data.connection.connected && <p className="rc-alert warning" role="status">{data.connection.message || '尚未核实本地 BOSS 登录状态'}</p>}

    <label htmlFor="binding-cid">会话标识（gid-来源）</label>
    <input id="binding-cid" value={conversationId} onChange={e => setConversationId(e.target.value)} placeholder="形如 123456-0，从 BOSS 会话地址或调试信息里取" />

    <label htmlFor="binding-name">候选人姓名</label>
    <input id="binding-name" value={name} onChange={e => setName(e.target.value)} placeholder="与平台显示一致，用于核实身份" />

    <label htmlFor="binding-title">沟通岗位</label>
    <input id="binding-title" value={positionTitle} onChange={e => setPositionTitle(e.target.value)} placeholder="该候选人关联的岗位名称" />

    <div className="rc-section-heading">
      <button type="button" disabled={busy || checking || !formReady} onClick={runPreview}>
        {checking ? <Loader2 size={14} className="animate-spin" /> : null} 预览核实
      </button>
    </div>

    {previewError && <p className="rc-alert error" role="alert">{previewError}</p>}

    {preview && <div className="rc-panel">
      <p>核实到招聘账号 uid：<strong>{preview.account_uid}</strong></p>
      <p>候选人：<strong>{preview.name}</strong> · {preview.position_title} · {preview.message_count} 条已读消息</p>
      <p className="rc-caption">{preview.coverage}</p>
      <button className="primary" disabled={busy} onClick={() => act('binding/confirm', { conversation_id: conversationId.trim(), name: name.trim(), position_title: positionTitle.trim(), expected_account: preview.account_uid }, '会话已绑定')}>
        <ShieldCheck size={14} /> 确认绑定这个会话
      </button>
    </div>}
  </div>
}