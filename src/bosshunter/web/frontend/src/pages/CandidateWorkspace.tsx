import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'
import { ChevronRight, MessageSquare, RefreshCw, Search, Settings2, Sparkles } from 'lucide-react'
import { RecruitIcon } from '../components/recruiting/RecruitIcon'
import { Drafts, InvitationForm, Monitor, ResumePanel, currentAssessment } from './CandidatePanels'
import type { Act, State } from './RecruitingPage'
import './candidate-workspace.css'

type Conversation = State['conversations'][number]
const url = (c: Conversation, panel = 'reply') => `/recruiting/candidates/${encodeURIComponent(c.id)}?panel=${panel}`
const lastMessage = (c: Conversation) => c.snapshot.messages[c.snapshot.messages.length - 1]
const pending = (s: State, c: Conversation) => s.outbox.some(d => d.conversation_id === c.id && ['uncertain', 'sending'].includes(d.status))
const label = (s: State, c: Conversation) => c.do_not_contact ? '已停止联系' : c.taken_over ? '人工接管中' : pending(s, c) ? '发送结果待核实' : lastMessage(c)?.direction === 'in' ? '待回复' : '等待回复'

export function CandidateWorkspace({ data, id, act, busy }: { data: State; id?: string; act: Act; busy: boolean }) {
  const location = useLocation()
  const [search, setSearch] = useState('')
  const [position, setPosition] = useState('')
  const [filter, setFilter] = useState('all')
  const [showMonitor, setShowMonitor] = useState(false)
  const [mobileView, setMobileView] = useState('chat')
  const c = id ? data.conversations.find(item => item.id === id) : data.conversations[0]
  useEffect(() => { setMobileView('chat') }, [location.pathname])
  useEffect(() => { if (['resume', 'interview'].includes(new URLSearchParams(location.search).get('panel') || '')) setMobileView('detail') }, [location.pathname, location.search])
  const counts = { all: data.conversations.length, reply: data.conversations.filter(item => lastMessage(item)?.direction === 'in' && !item.do_not_contact).length, human: data.conversations.filter(item => item.taken_over).length }
  const list = data.conversations.filter(item => (!position || item.position_id === position) && (!search.trim() || `${item.name} ${data.positions.find(p => p.id === item.position_id)?.title || ''}`.toLowerCase().includes(search.trim().toLowerCase())) && (filter === 'all' || filter === 'reply' && lastMessage(item)?.direction === 'in' && !item.do_not_contact || filter === 'human' && item.taken_over))
  return <>
    <div className="rd-page-heading"><div><h2>候选人沟通</h2><p>打开一个候选人，连续处理消息、简历评估和面试安排。</p></div><div className="cw-heading-actions"><Link className="rd-button" to="/recruiting/binding">绑定更多候选人</Link><span className={`rd-state ${data.monitor.running ? 'green' : ''}`}><i />{data.monitor.running ? '回复草稿监测中' : '回复建议模式'}</span><button className="rd-button" aria-expanded={showMonitor} onClick={() => setShowMonitor(!showMonitor)}><Settings2 size={15} />监测设置</button></div></div>
    {showMonitor && <div className="cw-monitor"><Monitor data={data} act={act} busy={busy} /></div>}
    <div className="cw-mobile-nav" role="group" aria-label="沟通工作区视图">{[['list', '候选人'], ['chat', '对话'], ['detail', '资料与助手']].map(([key, name]) => <button key={key} aria-pressed={mobileView === key} onClick={() => setMobileView(key)}>{name}</button>)}</div>
    <div className="cw-workspace" data-mobile-view={mobileView}>
      <aside className="cw-list" aria-label="候选人列表"><div className="cw-list-controls"><label className="sr-only" htmlFor="candidate-position">筛选沟通岗位</label><select id="candidate-position" value={position} onChange={e => setPosition(e.target.value)}><option value="">全部岗位</option>{data.positions.map(p => <option key={p.id} value={p.id}>{p.title}</option>)}</select><label className="rd-search"><Search size={15} /><input aria-label="搜索候选人" placeholder="搜索姓名或岗位" value={search} onChange={e => setSearch(e.target.value)} /></label><div className="cw-list-filters" role="group" aria-label="筛选候选人">{([['all', '全部'], ['reply', '待回复'], ['human', '人工']] as const).map(([key, name]) => <button key={key} aria-pressed={filter === key} onClick={() => setFilter(key)}>{name}<span>{counts[key]}</span></button>)}</div></div>
        <div className="cw-people">{list.map(item => <Link key={item.id} to={url(item)} onClick={() => { setMobileView('chat'); act('conversation/select', { conversation_id: item.id }) }} aria-current={c?.id === item.id ? 'page' : undefined} className="cw-person"><span className="rd-person-avatar">{item.name.slice(0, 1)}</span><div><strong>{item.name}</strong><small>{data.positions.find(p => p.id === item.position_id)?.title || '岗位待关联'}</small><p>{lastMessage(item)?.text || '暂无已加载消息'}</p><span className={`rd-badge ${pending(data, item) ? 'amber' : ''}`}>{label(data, item)}</span></div></Link>)}{!list.length && <div className="rd-empty"><RecruitIcon name="chat" size={32} /><p>{data.conversations.length ? '没有符合条件的候选人' : '尚未同步候选人会话'}</p>{data.conversations.length > 0 && <button className="rd-text-button" onClick={() => { setSearch(''); setPosition(''); setFilter('all') }}>清除筛选</button>}</div>}</div><p className="cw-list-note">共 {list.length} 位 · 当前已同步会话</p>
      </aside>
      {c ? <ConversationPanels key={c.id} data={data} c={c} act={act} busy={busy} onEditReply={() => setMobileView('chat')} /> : <section className="cw-no-selection"><RecruitIcon name="chat" size={48} /><h3>{id ? '没有找到该候选人，请返回工作台核对。' : '还没有可以打开的会话'}</h3><p>同步候选人后，在这里查看双方消息与简历。</p><Link className="rd-button" to="/recruiting">返回招聘工作台</Link></section>}
    </div>
    <p className="cw-footnote">当前为单候选人试运行 · 回复先核对再发送 · 面试邀约禁止发送</p>
  </>
}

function ConversationPanels({ data, c, act, busy, onEditReply }: { data: State; c: Conversation; act: Act; busy: boolean; onEditReply: () => void }) {
  const location = useLocation()
  const requestedPanel = new URLSearchParams(location.search).get('panel') || 'reply'
  const panel = ['reply', 'resume', 'interview'].includes(requestedPanel) ? requestedPanel : 'reply'
  const [text, setText] = useState('')
  const composer = useRef<HTMLTextAreaElement>(null)
  const messagePane = useRef<HTMLDivElement>(null)
  const followLatest = useRef(true)
  useLayoutEffect(() => {
    const pane = messagePane.current
    if (pane && followLatest.current) pane.scrollTop = pane.scrollHeight
  }, [c.snapshot.messages.length])
  const doc = data.documents.find(item => item.conversation_id === c.id)
  const job = data.positions.find(item => item.id === c.position_id)
  const score = currentAssessment(data, c)
  const company = data.company?.text || ''
  const drafts = data.outbox.filter(d => d.conversation_id === c.id && d.kind !== 'invitation')
  const blocked = !!c.do_not_contact || pending(data, c)
  return <>
    <section className="cw-chat" aria-label="当前候选人对话"><header className="cw-chat-header"><div><h3>{c.name}</h3><p>{job?.title || '岗位待关联'}</p><span className={`rd-badge ${pending(data, c) ? 'amber' : ''}`}>{label(data, c)}</span></div><div><button className="rd-text-button" disabled={busy} onClick={() => act('sync', {}, '最新对话已同步')}><RefreshCw size={13} />同步会话</button><button className="rd-button" disabled={busy} onClick={() => act('conversation/control', { conversation_id: c.id, taken_over: !c.taken_over, do_not_contact: !!c.do_not_contact }, '会话处理方式已更新')}>{c.taken_over ? '交回 Agent' : '人工接管'}</button></div></header>
      <div className="cw-messages" ref={messagePane} onScroll={e => { const pane = e.currentTarget; followLatest.current = pane.scrollHeight - pane.scrollTop - pane.clientHeight < 60 }} aria-label="双方沟通记录"><p className="cw-history-note">{c.snapshot.messages.length} 条已加载消息 · 更早历史可能未加载</p>{c.snapshot.messages.map((message, i) => <div key={i} className={`cw-message ${message.direction === 'out' ? 'ours' : ''}`}><span className="cw-message-avatar">{message.direction === 'out' ? '我' : message.direction === 'in' ? c.name.slice(0, 1) : '·'}</span><div><small>{message.direction === 'in' ? c.name : message.direction === 'out' ? '招聘方' : '系统'} {message.time}</small><p>{message.text}</p></div></div>)}{!c.snapshot.messages.length && <div className="rd-empty">暂无已加载的消息</div>}</div>
      <div className="cw-composer"><label htmlFor="reply-text">回复内容</label><textarea ref={composer} id="reply-text" rows={4} maxLength={500} value={text} onChange={e => setText(e.target.value)} placeholder="输入回复，或采用右侧的回复建议…" /><div className="cw-compose-actions"><small>{text.length}/500 字</small><button className="rd-button" disabled={busy || !text.trim() || blocked} onClick={() => act('reply/draft', { conversation_id: c.id, text }, '回复已保存，尚未发送')}>保存这段回复</button></div><p>{blocked ? '存在待核实结果或已停止联系，先核对后继续。' : '保存为草稿，核对后再发送。'}</p></div>
    </section>
    <aside className="cw-assistant" aria-label="候选人资料与回复助手"><nav className="cw-panel-tabs" aria-label="候选人操作">{[['reply', '回复助手'], ['resume', '简历评分'], ['interview', '面试安排']].map(([key, name]) => <Link key={key} aria-current={panel === key ? 'page' : undefined} to={url(c, key)}>{name}</Link>)}</nav><div className="cw-assistant-body">
      {panel === 'reply' && <><div className="cw-assistant-title"><RecruitIcon name="chat" size={32} /><div><h3>带着上下文回复</h3><p>结合双方消息、公司说明和岗位信息。</p></div></div><div className="cw-context-facts"><span><MessageSquare size={14} />双方消息 <b>{c.snapshot.messages.length} 条</b></span><span><RecruitIcon name="company" size={17} />公司说明 <b>{company ? '已加载' : '待填写'}</b></span><span><RecruitIcon name="jobs" size={17} />岗位 JD <b>{job?.jd ? '已加载' : '待填写'}</b></span><span><RecruitIcon name="resume" size={17} />已有简历 <b>{doc ? '1 份' : '未收到'}</b></span></div><p className="rc-caption">{c.snapshot.messages.length} 条对话消息全部带入；{c.snapshot.coverage}，更早历史可能未加载。</p>
        <details className="rc-context-details"><summary>查看 Agent 将使用的上下文</summary><p className="rc-caption">生成前重新同步双方消息；只依据已加载内容，不推测未提供的事实。</p><h4>公司说明</h4><p className="rc-pre">{company || '未提供，Agent 不得编造制度'}</p><h4>岗位 JD</h4><p className="rc-pre">{job?.jd || '未提供'}</p><p className="rc-caption">简历：{doc ? `${doc.text.length} 字，${doc.complete ? '已核对完整' : '完整性待核对'}` : '未提供'}；评估：{score ? '带入当前有效评估' : '暂无有效评估'}。</p></details>
        <button className="rd-button primary rd-wide" disabled={busy || !data.model_ready || !!c.taken_over || blocked} onClick={() => act('reply/draft', { conversation_id: c.id }, '已结合最新上下文生成回复，请核对下方内容')}><Sparkles size={15} />生成上下文回复</button>{!data.model_ready && <p className="rc-caption">模型未配置，<Link to="/recruiting/settings">先接通模型</Link>。</p>}
        <Drafts drafts={drafts} act={act} busy={busy || blocked} onEdit={value => { setText(value); onEditReply(); requestAnimationFrame(() => composer.current?.focus()) }} inline />
        <details className="rc-context-details"><summary>简历请求与联系设置</summary><div className="rc-actions"><button disabled={busy || !!doc || blocked} onClick={() => act('action/draft', { conversation_id: c.id, kind: 'request_resume' }, '已准备简历请求，尚未发送')}>{doc ? '已有简历资料' : '准备索取简历'}</button><button disabled={busy || blocked} onClick={() => act('action/draft', { conversation_id: c.id, kind: 'accept_resume' }, '已准备接收申请，尚未执行')}>准备接收简历</button><button disabled={busy} onClick={() => act('conversation/control', { conversation_id: c.id, taken_over: !!c.taken_over, do_not_contact: !c.do_not_contact }, '联系设置已更新')}>{c.do_not_contact ? '恢复联系' : '停止联系'}</button><button disabled={busy} onClick={() => act('conversation/auto-send', { conversation_id: c.id, enabled: !c.auto_send }, '自动外发设置已更新')}>{c.auto_send ? '关闭自动外发' : '开启自动外发'}</button></div></details>
      </>}
      {panel === 'resume' && <ResumePanel data={data} c={c} act={act} busy={busy} />}
      {panel === 'interview' && <><InvitationForm c={c} act={act} busy={busy} /><Drafts drafts={data.outbox.filter(d => d.conversation_id === c.id && d.kind === 'invitation')} act={act} busy={busy} inline /></>}
      <div className="cw-assistant-note"><RecruitIcon name="resume" size={20} /><span>{score?.result.score != null ? `当前岗位匹配分 ${score.result.score}` : doc ? '简历已收到，完整性和岗位匹配待核对' : '收到简历后，可按岗位 JD 评估'}<Link to={url(c, 'resume')}>查看简历与评分 <ChevronRight size={11} /></Link></span></div>
    </div></aside>
  </>
}
