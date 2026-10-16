import { useEffect, useState } from 'react'
import './pacing-settings.css'

type Values = Record<string, number>
export type PacingState = {
  saved: Values
  active: Values
  worker_active?: Values | null
  fields: { key: string; label: string; unit: string; min: number; integer?: boolean }[]
  ranges: [string, string][]
  restart_required: boolean
  restart_mode: 'manual'
}
const strings = (values: Values) => Object.fromEntries(Object.entries(values).map(([key, value]) => [key, String(value)]))

export function PacingSettings({ pacing, refresh }: { pacing: PacingState; refresh: () => Promise<void> }) {
  const [saved, setSaved] = useState(pacing.saved)
  const [edit, setEdit] = useState<{ base: Values; draft: Record<string, string> } | null>(null)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [pendingRestart, setPendingRestart] = useState(false)
  const savedKey = JSON.stringify(pacing.saved)
  useEffect(() => { setSaved(JSON.parse(savedKey)) }, [savedKey])
  const draft = edit?.draft || strings(saved)
  const dirty = pacing.fields.some(({ key }) => draft[key]?.trim() === '' || Number(draft[key]) !== saved[key])
  const restartRequired = pacing.restart_required || (pendingRestart && pacing.fields.some(({ key }) => saved[key] !== pacing.active[key]))

  const cancel = () => { setEdit(null); setError(''); setNotice('已取消修改，参数未保存') }
  const submit = async (event: React.FormEvent) => {
    event.preventDefault()
    if (saving || !dirty || !edit) return
    setError(''); setNotice('')
    const values: Values = {}
    for (const field of pacing.fields) {
      const value = Number(draft[field.key])
      if (!draft[field.key]?.trim() || !Number.isFinite(value) || value < field.min || (field.integer && !Number.isInteger(value))) {
        setError(`${field.label}至少为 ${field.min} ${field.unit}${field.integer ? '，且必须为整数' : ''}`)
        return
      }
      values[field.key] = value
    }
    for (const [low, high] of pacing.ranges) {
      if (values[low] > values[high]) {
        setError(`${pacing.fields.find(field => field.key === low)?.label}不能大于上限`)
        return
      }
    }
    const changes = pacing.fields.filter(({ key }) => values[key] !== edit.base[key])
      .map(({ key, label, unit }) => `${label}：${edit.base[key]} → ${values[key]} ${unit}`).join('\n')
    if (!window.confirm(`确认修改以下节流参数？\n\n${changes}\n\n确认后保存，重启整个程序后生效。当前不支持自动重启，请退出 BossHunter 后重新打开；终端启动时请同时重启 Web 和监测进程。\n取消将放弃本次修改。`)) {
      cancel()
      return
    }
    setSaving(true)
    try {
      const response = await fetch('/api/recruiting/pacing', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirmed: true, expected: edit.base, values }),
      })
      const result = await response.json()
      if (!response.ok || !result.ok) throw new Error(result.error || '保存失败')
      setSaved(values); setEdit(null); setPendingRestart(true)
      setNotice('参数已保存，待重启生效')
      try { await refresh() } catch { setNotice('参数已保存，待重启生效；状态刷新失败，请重新读取页面') }
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : '保存失败，请重试')
    } finally { setSaving(false) }
  }

  return <form className="rc-panel rc-form rc-pacing" onSubmit={submit} aria-label="招聘端节流设置">
    <h3>招聘端节流设置</h3>
    <p className="rc-muted">读取、分页和主动招呼分别设置随机间隔；下限和上限相同则使用固定间隔。HTTP 轮询间隔用于 WS 断线后的会话检查。</p>
    {restartRequired && <div className="rc-alert warning" role="status">节流参数已保存，待重启生效。请退出 BossHunter 程序后重新打开并启动系统；终端启动时请同时停止并重启 Web 服务和监测进程。未重启的进程继续使用原参数。</div>}
    <div className="rc-pacing-fields">{pacing.fields.map(field => <label key={field.key} htmlFor={`pacing-${field.key}`}>
      <span>{field.label}（{field.unit}）</span>
      <input id={`pacing-${field.key}`} aria-label={`${field.label}（${field.unit}）`} type="number" min={field.min} step={field.integer ? 1 : 'any'} required
        value={draft[field.key] ?? ''} disabled={saving}
        onChange={event => { setEdit(previous => ({ base: previous?.base || { ...saved }, draft: { ...(previous?.draft || strings(saved)), [field.key]: event.target.value } })); setError(''); setNotice('') }} />
      <small className="rc-caption">当前生效：{pacing.active[field.key]} {field.unit}{pacing.worker_active && pacing.worker_active[field.key] !== pacing.active[field.key] && `；后台监测：${pacing.worker_active[field.key]} ${field.unit}`}</small>
    </label>)}</div>
    {error && <p className="rc-alert error" role="alert">{error}</p>}
    {notice && <p className="rc-caption" role="status">{notice}</p>}
    <div className="rc-actions"><button type="submit" className="primary" disabled={saving || !dirty}>{saving ? '正在保存…' : '保存节流参数'}</button><button type="button" disabled={saving || !edit} onClick={cancel}>取消修改</button></div>
  </form>
}
