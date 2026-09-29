import { useCallback, useEffect, useRef, useState } from 'react'
import { Link, Navigate, useLocation, useParams } from 'react-router-dom'
import { Loader2 } from 'lucide-react'
import './recruiting.css'
import './recruiting-design.css'
import { Monitor, Drafts } from './CandidatePanels'
import { CandidateWorkspace } from './CandidateWorkspace'
import { GreetingConsole } from './GreetingConsole'
import { RecruitingOverview } from './RecruitingOverview'
import { PublishedJobs, PublishedJobsState } from './PublishedJobs'
import { BindingWizard } from './BindingWizard'

type Message = { direction: string; kind: string; text: string; time: string }
type Position = { id: string; title: string; jd: string; enabled: number; version: number; source: string }
type Conversation = { id: string; name: string; position_id: string; updated_at: string; taken_over: number; do_not_contact: number; auto_send?: number; snapshot: { messages: Message[]; coverage: string } }
type Resume = { id: string; conversation_id: string; text: string; complete: number; source: string; meta?: { page_count?: number; note?: string; filename?: string } }
type Company = { text: string; version: number; updated_at: string | null }
type Draft = { id: string; conversation_id: string; kind: string; content: string; status: string; result: string; refs: { source?: string; needs_human?: boolean; message_count?: number; basis?: string[]; missing?: string[] } }
type Assessment = { id: string; conversation_id: string; result: { score: number | null; earned: number; assessed_weight: number; coverage: number; document_id: string; position_version: number; questions: string[]; components: Record<string, { score: number | null; reason: string; quotes: string[] }> } }
export type State = { resume_processing?: Record<string, { status: string; message: string }>; recruiting_jobs?: PublishedJobsState; positions: Position[]; conversations: Conversation[]; documents: Resume[]; company?: Company; outbox: Draft[]; assessments: Assessment[]; events: { id: number; detail: string; kind: string; created_at: string }[]; connection: { connected: boolean; message: string }; monitor: { running: boolean; error: string; last_success: string | null; interval_seconds: number }; discovery?: { running: boolean }; auto_send?: { daily_limit: number; sent_today: number }; model_ready: boolean }
export type Act = (operation: string, payload?: object, success?: string) => Promise<boolean>
const EMPTY_COMPANY: Company = { text: '', version: 0, updated_at: null }
const fmt = (value?: string | null) => value ? new Date(value).toLocaleString('zh-CN', { hour12: false }) : '尚未同步'
export default function RecruitingPage() {
  const [data, setData] = useState<State | null>(null)
  const [error, setError] = useState('')
  const [notice, setNotice] = useState('')
  const [busy, setBusy] = useState(false)
  const inFlight = useRef(false)
  const mounted = useRef(true)
  const location = useLocation()
  const { id } = useParams()
  const section = location.pathname.split('/')[2] || 'overview'
  const load = useCallback(async () => {
    const response = await fetch('/api/recruiting/state')
    const value = await response.json()
    if (!response.ok) throw new Error(value.error || '无法读取招聘数据')
    if (mounted.current) setData(value)
  }, [])
  useEffect(() => {
    mounted.current = true
    const refresh = () => { if (!inFlight.current) load().catch(e => mounted.current && setError(e.message)) }
    refresh()
    const timer = setInterval(refresh, 15000)
    return () => { mounted.current = false; clearInterval(timer) }
  }, [load])
  const act: Act = async (operation, payload = {}, success = '已保存') => {
    if (inFlight.current) return false
    inFlight.current = true; setBusy(true); setError(''); setNotice('')
    try {
      const response = await fetch(`/api/recruiting/${operation}`, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(payload) })
      const value = await response.json()
      if (!response.ok) throw new Error(value.error || '操作未完成')
      await load(); setNotice(success); return true
    } catch (e) { setError(e instanceof Error ? e.message : '操作失败'); await load().catch(() => {}); return false }
    finally { inFlight.current = false; setBusy(false) }
  }
  if (section === 'invitations') return <Navigate to="/recruiting/candidates" replace />
  if (section === 'knowledge') return <Navigate to="/recruiting/company" replace />
  return <div className={`recruiting${['overview', 'positions', 'discover', 'candidates'].includes(section) ? ' recruiting-designed' : ''}`}>
    {error && <div className="rc-alert error" role="alert">{error}<button onClick={() => { setError(''); load().catch(e => setError(e.message)) }}>重新读取</button></div>}
    {notice && <div className="rc-alert success" role="status">{notice}</div>}
    {busy && <div className="rc-working" role="status"><Loader2 size={16} className="animate-spin" /> 正在处理…</div>}
    {!data ? <div className="rc-panel rc-empty">正在读取招聘数据…</div> : <>
      {section === 'overview' && <RecruitingOverview data={data} act={act} busy={busy} />}
      {section === 'positions' && data.recruiting_jobs && <PublishedJobs data={data.recruiting_jobs} act={act} busy={busy} />}
      {section === 'candidates' && <CandidateWorkspace data={data} id={id} act={act} busy={busy} />}
      {section === 'company' && <><PageHeading title="公司说明与岗位 JD" description="只维护这两份内容，Agent 回复时会直接结合使用。" /><CompanyForm company={data.company || EMPTY_COMPANY} act={act} busy={busy} />{data.positions.length ? data.positions.map(p => <PositionForm key={p.id} p={p} act={act} busy={busy} />) : <div className="rc-panel rc-muted">导入招聘会话后，在这里填写该岗位 JD。</div>}</>}
      {section === 'binding' && <BindingWizard data={data} act={act} busy={busy} />}
      {section === 'discover' && <GreetingConsole data={data} act={act} busy={busy} />}
      {section === 'monitor' && <><PageHeading title="运行记录" description="查看监测状态、回复结果和需要人工处理的异常。" /><Monitor data={data} act={act} busy={busy} /><Drafts drafts={data.outbox} act={act} busy={busy} /><section className="rc-panel"><h3>最近操作</h3>{data.events.map(e => <div className="rc-event" key={e.id}><time>{fmt(e.created_at)}</time><span>{e.detail || e.kind}</span></div>)}</section></>}
      {!['overview', 'positions', 'discover', 'candidates'].includes(section) && <p className="rc-footnote">当前为单候选人试运行 · 回复先核对再发送 · 面试邀约禁止发送</p>}
    </>}
  </div>
}
function PageHeading({ title, description }: { title: string; description: string }) { return <div className="rc-page-heading"><div><h2>{title}</h2><p className="rc-muted">{description}</p></div></div> }
function Empty({ text }: { text: string }) { return <div className="rc-panel rc-empty"><p>{text}</p><Link to="/recruiting">返回招聘工作台</Link></div> }
function CompanyForm({ company, act, busy }: { company: Company; act: Act; busy: boolean }) {
  const [text, setText] = useState(company.text)
  const dirty = text !== company.text
  useEffect(() => { const warn = (e: BeforeUnloadEvent) => { if (dirty) e.preventDefault() }; window.addEventListener('beforeunload', warn); return () => window.removeEventListener('beforeunload', warn) }, [dirty])
  return <form className="rc-panel rc-form" onSubmit={e => { e.preventDefault(); act('company', { text }, '公司说明已保存，后续回复会结合这份内容') }}><label htmlFor="company-brief">公司说明</label><p className="rc-muted">把可向候选人说明的公司介绍、工作地点、作息、薪酬福利等放在这里即可。</p><textarea id="company-brief" rows={12} maxLength={30000} value={text} onChange={e => setText(e.target.value)} placeholder="直接粘贴公司说明，不需要拆分条目或设置标签。" /><div className="rc-section-heading"><span className="rc-caption">{dirty ? '有未保存修改' : company.updated_at ? `已保存 · ${fmt(company.updated_at)}` : '还没有公司说明'} · {text.length}/30000 字</span><button className="primary" disabled={busy || !dirty}>保存公司说明</button></div></form>
}
function PositionForm({ p, act, busy }: { p: Position; act: Act; busy: boolean }) {
  const [jd, setJd] = useState(p.jd)
  const dirty = jd !== p.jd
  return <form className="rc-panel rc-form" onSubmit={e => { e.preventDefault(); act('position', { id: p.id, jd, enabled: !!p.enabled }, '岗位 JD 已保存') }}><label htmlFor={`jd-${p.id}`}>{p.title} · 岗位 JD</label><textarea id={`jd-${p.id}`} rows={8} maxLength={30000} value={jd} onChange={e => setJd(e.target.value)} placeholder="岗位职责、必须条件、优先条件和待遇说明。" /><div className="rc-section-heading"><span className="rc-caption">回复与简历评分使用候选人对应的这份 JD。{dirty ? ' 有未保存修改' : ''}</span><button className="primary" disabled={busy || !dirty}>保存岗位 JD</button></div></form>
}
