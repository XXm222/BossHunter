import { useState } from 'react'
import { Link } from 'react-router-dom'
import { ArrowRight, Ban, ChevronLeft, ChevronRight, Clock3, Info, Play, Search, Square, Target } from 'lucide-react'
import { RecruitIcon } from '../components/recruiting/RecruitIcon'
import type { Act, State } from './RecruitingPage'
import './greeting-console.css'

export type GreetingAttempt = { id: number; job_id: string; candidate_id: string; name?: string; status: string; created_at: string }
const filters = [{ id: 'all', label: '全部' }, { id: 'sent', label: '已招呼' }, { id: 'skipped', label: '已跳过' }, { id: 'review', label: '待核实' }]
const statusLabel = (status: string) => ({ sent: '已确认招呼', skipped: '已跳过', uncertain: '结果待核实', sending: '结果待核实', failed: '发送失败' }[status] || '状态待核实')
const matches = (status: string, filter: string) => filter === 'all' || (filter === 'review' ? !['sent', 'skipped'].includes(status) : status === filter)
const time = (value: string) => new Date(value).toLocaleString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false })

export function GreetingConsole({ data, act, busy = false }: { data: State; act?: Act; busy?: boolean }) {
  const [filter, setFilter] = useState('all')
  const [query, setQuery] = useState('')
  const [page, setPage] = useState(1)
  const [selectedId, setSelectedId] = useState<number | null>(null)
  const jobs = data.recruiting_jobs
  const running = data.discovery?.running ?? false
  const blockers = jobs?.blockers ?? []
  const ready = !running && blockers.length === 0
  const attempts = jobs?.attempts ?? []
  const selectedJobs = jobs?.jobs.filter(job => job.selected) ?? []
  const jobMap = new Map(jobs?.jobs.map(job => [job.id, job]))
  // Only use an exact conversation ID match; names or row order cannot identify a person.
  const conversations = new Map(data.conversations.map(c => [c.id, c]))
  const candidateName = (record: GreetingAttempt) => record.name || conversations.get(record.candidate_id)?.name || '候选人资料未关联'
  const jobTitle = (record: GreetingAttempt) => jobMap.get(record.job_id)?.title || '岗位资料未关联'
  const visible = attempts.filter(record => matches(record.status, filter) && `${candidateName(record)} ${jobTitle(record)}`.toLowerCase().includes(query.trim().toLowerCase()))
  const pages = Math.max(1, Math.ceil(visible.length / 6))
  const currentPage = Math.min(page, pages)
  const rows = visible.slice((currentPage - 1) * 6, currentPage * 6)
  const current = visible.find(record => record.id === selectedId) ?? null
  const conversation = current ? conversations.get(current.candidate_id) : undefined
  const custom = jobs?.budget.mode === 'custom'
  const limit = custom ? jobs.budget.limit : null
  const attempted = jobs?.daily.attempted ?? 0
  const percent = limit ? Math.min(100, attempted / limit * 100) : null
  return <>
    <div className="rd-page-heading gc-heading"><div><h2>主动打招呼</h2><p>按已选岗位持续找人，把每天的可用招呼额度用在合适的人身上。</p></div><div className="gc-heading-controls"><span className="rd-state"><i />{running ? '执行中' : ready ? '可执行' : '待接通'}</span><button className="rd-button" disabled={busy || (!ready && !running)} aria-describedby="greeting-readiness" onClick={() => running ? act?.('discover/stop', {}, '已请求停止主动打招呼') : act?.('discover/run', {}, '主动打招呼循环已启动')}>{running ? <Square size={15} /> : <Play size={15} />}{running ? '停止' : '开始执行'}</button><Link className="rd-text-button" to="/recruiting/positions">管理岗位与额度 <ChevronRight size={14} /></Link></div></div>

    <section className="rd-panel gc-target" aria-label="今日招呼额度">
      <div className="gc-target-label"><Target size={30} /><div><h3>今日目标</h3><p>{custom ? `每日最多 ${limit} 人` : '用完平台可用额度'}</p><Link to="/recruiting/positions">调整额度 <ChevronRight size={12} /></Link></div></div>
      <div className="gc-progress-block"><div className="gc-progress-label"><span>已确认 <strong>{jobs?.daily.sent ?? 0}</strong> / {limit ?? '—'}</span><span>{percent === null ? '平台总额度未读取' : `已尝试 ${attempted} 次`}</span></div><div className="rd-progress" role={percent !== null ? 'progressbar' : undefined} aria-label="今日招呼尝试次数" aria-valuemin={percent !== null ? 0 : undefined} aria-valuemax={limit ?? undefined} aria-valuenow={limit !== null ? Math.min(attempted, limit) : undefined}><span style={{ width: `${percent ?? 0}%` }} /></div><p className="rd-state"><i />{selectedJobs.length ? `已选 ${selectedJobs.length} 个岗位${running ? '，执行中' : ready ? '，可执行' : ''}` : '尚未选择自动处理的岗位'}</p></div>
      <div className="gc-remaining"><strong>平台剩余 <b>{jobs?.daily.platform_remaining ?? '未读取'}</b></strong><p>账号额度，所有岗位共用</p>{custom && <small>自定义剩余 {jobs?.daily.custom_remaining ?? '—'} 次</small>}<button className="rd-text-button" disabled={busy || !act} onClick={() => act?.('quota/read', {}, '额度已读取')}>读取额度</button></div>
      <div className="gc-cadence"><Clock3 size={20} /><span>低频执行<br />按岗位依次处理</span></div>
    </section>

    <section className="rd-panel"><div className="rd-panel-heading"><h3>岗位执行 <span className="rd-count">{selectedJobs.length}</span></h3><Link to="/recruiting/positions">调整已选岗位 <ChevronRight size={13} /></Link></div>
      <div className="rd-table-scroll"><table className="rd-table gc-jobs"><thead><tr><th>岗位</th><th>执行状态</th><th>今日已招呼</th><th>本轮进展</th><th>操作</th></tr></thead><tbody>{selectedJobs.map(job => <tr key={job.id}><td><strong>{job.title}</strong><small>{job.details.slice(0, 2).join(' · ')}</small></td><td><span className="rd-badge amber">{running ? '执行中' : ready ? '可执行' : '待接通'}</span></td><td>{attempts.filter(record => record.job_id === job.id && record.status === 'sent').length}</td><td>尚未开始处理</td><td><Link to="/recruiting/positions">管理岗位 <ChevronRight size={12} /></Link></td></tr>)}</tbody></table></div>
      {!selectedJobs.length && <div className="rd-empty gc-job-empty"><RecruitIcon name="jobs" size={34} /><strong>{jobs ? '先选择需要 Agent 处理的岗位' : '岗位数据暂不可用'}</strong><p>{jobs ? '已发布岗位不会自动启用。勾选并保存后，会出现在这里。' : '请前往岗位与额度查看同步状态。'}</p><Link className="rd-button" to="/recruiting/positions">选择招聘岗位 <ArrowRight size={14} /></Link></div>}
    </section>

    <div className="gc-record-grid"><section className="rd-panel gc-records"><div className="rd-panel-heading"><h3>今日触达记录</h3><span className="rd-small">{jobs?.daily.date ?? '今日'} · 北京时间</span></div>
      <label className="rd-search"><Search size={15} /><input aria-label="搜索触达记录" placeholder="搜索候选人或岗位" value={query} onChange={event => { setQuery(event.target.value); setPage(1); setSelectedId(null) }} /></label>
      <div className="gc-filters" role="group" aria-label="筛选触达结果">{filters.map(item => <button key={item.id} aria-pressed={filter === item.id} onClick={() => { setFilter(item.id); setPage(1); setSelectedId(null) }}>{item.label}<span>{attempts.filter(record => matches(record.status, item.id)).length}</span></button>)}</div>
      <div className="rd-table-scroll"><table className="rd-table gc-record-table"><thead><tr><th>时间</th><th>候选人 / 岗位</th><th>结果</th><th>操作</th></tr></thead><tbody>{rows.map(record => <tr key={record.id} className={current?.id === record.id ? 'is-current' : ''}><td><time dateTime={record.created_at}>{new Date(record.created_at).toLocaleTimeString('zh-CN', { timeZone: 'Asia/Shanghai', hour12: false, hour: '2-digit', minute: '2-digit' })}</time></td><td><strong>{candidateName(record)}</strong><small>{jobTitle(record)}</small></td><td><span className={`rd-badge ${record.status === 'sent' ? 'gc-good' : record.status !== 'skipped' ? 'amber' : ''}`}>{statusLabel(record.status)}</span></td><td><button className="rd-text-button" aria-label={`查看第 ${record.id} 条触达记录`} aria-pressed={current?.id === record.id} onClick={() => setSelectedId(record.id)}>详情 <ChevronRight size={12} /></button></td></tr>)}</tbody></table></div>
      {!rows.length && <div className="rd-empty gc-record-empty"><RecruitIcon name="search" size={40} /><strong>{query || filter !== 'all' ? '没有符合条件的触达记录' : '今天还没有主动招呼记录'}</strong><p>{query || filter !== 'all' ? '试试其他关键词或切换筛选条件。' : '执行后，在这里查看联系结果；发送结果未核实时不会计为成功。'}</p>{(query || filter !== 'all') && <button className="rd-text-button" onClick={() => { setQuery(''); setFilter('all'); setPage(1) }}>清除筛选</button>}</div>}
      <div className="gc-pagination"><span>共 {visible.length} 条记录</span><div><button className="rd-button" aria-label="上一页" disabled={currentPage <= 1} onClick={() => setPage(currentPage - 1)}><ChevronLeft size={14} /></button><span>{currentPage} / {pages}</span><button className="rd-button" aria-label="下一页" disabled={currentPage >= pages} onClick={() => setPage(currentPage + 1)}><ChevronRight size={14} /></button></div></div>
    </section>

    <aside className="rd-panel gc-detail" aria-label="触达详情"><h3>为什么联系这位候选人</h3>{current ? <><div className="gc-person"><span className="rd-person-avatar">{(current.name || conversation?.name || '?').slice(0, 1)}</span><div><strong>{candidateName(current)}</strong><p>{jobTitle(current)}</p></div></div><div className="gc-evidence"><Info size={17} /><p>这条历史记录未保存联系依据，无法还原当时的匹配判断。</p></div><p className="rd-small">完整简历和评分请在已关联的候选人沟通中查看。</p><div className="gc-result"><h4>执行结果</h4><span className={`rd-badge ${current.status === 'sent' ? 'gc-good' : 'amber'}`}>{statusLabel(current.status)}</span><p>时间：{time(current.created_at)}</p>{current.status !== 'sent' && current.status !== 'skipped' && <p>请先核实平台结果，避免重复联系。</p>}</div>{conversation ? <Link className="rd-button rd-wide" to={`/recruiting/candidates/${encodeURIComponent(conversation.id)}`}><RecruitIcon name="chat" size={21} />进入候选人沟通 <ChevronRight size={14} /></Link> : <p className="rd-small">尚未关联会话，不能直接进入该候选人的沟通。</p>}</> : <div className="gc-detail-empty"><RecruitIcon name="resume" size={46} /><strong>选一条记录，查看触达详情</strong><p>核对联系结果和岗位，再进入候选人沟通，连续查看对话、简历与评估。</p><span>没有已记录的依据时，不生成推测结论。</span><Link className="rd-button" to="/recruiting/candidates"><RecruitIcon name="chat" size={20} />打开候选人沟通 <ChevronRight size={14} /></Link></div>}</aside></div>
    <section className="gc-readiness" id="greeting-readiness"><Info size={18} /><div><strong>{running ? '主动打招呼执行中' : ready ? '可开始主动打招呼' : '自动打招呼尚未就绪'}</strong><p>{blockers.join('；') || (running ? '循环已启动，按岗位依次处理，每个动作间隔随机 10–30 秒。' : '已选岗位和额度就绪，点击「开始执行」按额度持续招呼。')}</p></div><span><Ban size={15} />面试邀约发送已禁用</span></section>
  </>
}
