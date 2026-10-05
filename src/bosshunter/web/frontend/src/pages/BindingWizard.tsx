import { useState } from 'react'
import { Loader2, RefreshCw, ShieldCheck } from 'lucide-react'
import type { Act, State } from './RecruitingPage'

type Contact = { ident: string; name: string; position_title: string; last_ts: number | null }
type Preview = { account_uid: string; name: string; position_title: string; message_count: number; coverage: string }

export function BindingWizard({ data, act, busy }: { data: State; act: Act; busy: boolean }) {
  const [contacts, setContacts] = useState<Contact[]>([])
  const [loading, setLoading] = useState(false)
  const [loadError, setLoadError] = useState('')
  const [selected, setSelected] = useState<Contact | null>(null)
  const [positionTitle, setPositionTitle] = useState('')
  const [preview, setPreview] = useState<Preview | null>(null)
  const [checking, setChecking] = useState(false)
  const [previewError, setPreviewError] = useState('')

  const loadContacts = async () => {
    setLoading(true); setLoadError(''); setSelected(null); setPreview(null)
    try {
      const response = await fetch('/api/recruiting/contacts/list', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' })
      const value = await response.json()
      if (!response.ok) throw new Error(value.error || '联系人读取失败')
      setContacts(Array.isArray(value.result) ? value.result : [])
    } catch (e) {
      setLoadError(e instanceof Error ? e.message : '联系人读取失败')
    } finally {
      setLoading(false)
    }
  }

  const pick = (c: Contact) => {
    setSelected(c); setPositionTitle(c.position_title); setPreview(null); setPreviewError('')
  }

  const runPreview = async () => {
    if (!selected) return
    setChecking(true); setPreviewError(''); setPreview(null)
    try {
      const response = await fetch('/api/recruiting/binding/preview', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ conversation_id: selected.ident, name: selected.name, position_title: positionTitle.trim() }),
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
    <p className="rc-muted">从联系人列表选择一个候选人绑定。已绑定 <strong>{data.conversations.length}</strong> 个会话。本地模式只通过 Chrome 登录状态后台读取，不会操作标签页或发送消息。</p>

    {!data.connection.connected && <p className="rc-alert warning" role="status">{data.connection.message || '尚未核实本地 BOSS 登录状态'}</p>}

    <div className="rc-section-heading">
      <button type="button" disabled={busy || loading} onClick={loadContacts}>
        {loading ? <Loader2 size={14} className="animate-spin" /> : <RefreshCw size={14} />} 加载联系人列表
      </button>
    </div>

    {loadError && <p className="rc-alert error" role="alert">{loadError}</p>}

    {!selected && contacts.length > 0 && <div className="rc-panel">
      <p className="rc-caption">点击选择一个候选人：</p>
      {contacts.map(c => (
        <button key={c.ident} type="button" onClick={() => pick(c)} style={{ display: 'block', width: '100%', textAlign: 'left', marginBottom: 8, padding: 8 }}>
          <strong>{c.name}</strong> · {c.position_title || '岗位待补填'}
        </button>
      ))}
    </div>}

    {!selected && contacts.length === 0 && !loading && !loadError && <p className="rc-empty rc-muted">点上方「加载联系人列表」读取近 30 天联系人。</p>}

    {selected && <div className="rc-panel">
      <p>已选候选人：<strong>{selected.name}</strong>（会话 {selected.ident}）</p>
      <label htmlFor="binding-title">沟通岗位</label>
      <input id="binding-title" value={positionTitle} onChange={e => setPositionTitle(e.target.value)} placeholder="岗位名称（jobName 为空时需手动填）" />
      <div className="rc-section-heading">
        <button type="button" disabled={busy || checking || !positionTitle.trim()} onClick={runPreview}>
          {checking ? <Loader2 size={14} className="animate-spin" /> : null} 预览核实
        </button>
      </div>
    </div>}

    {previewError && <p className="rc-alert error" role="alert">{previewError}</p>}

    {preview && selected && <div className="rc-panel">
      <p>核实到招聘账号 uid：<strong>{preview.account_uid}</strong></p>
      <p>候选人：<strong>{preview.name}</strong> · {preview.position_title} · {preview.message_count} 条已读消息</p>
      <p className="rc-caption">{preview.coverage}</p>
      <button className="primary" disabled={busy} onClick={async () => { const ok = await act('binding/confirm', { conversation_id: selected.ident, name: selected.name, position_title: positionTitle.trim(), expected_account: preview.account_uid }, '会话已绑定'); if (ok) { setSelected(null); setPreview(null); setPositionTitle('') } }}>
        <ShieldCheck size={14} /> 确认绑定这个会话
      </button>
    </div>}
  </div>
}
