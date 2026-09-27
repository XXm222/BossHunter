import { Fragment, useEffect, useRef, useState } from 'react'
import { Check, ChevronDown, ChevronRight, CircleAlert, Info, RefreshCw, Search } from 'lucide-react'
import type { GreetingAttempt } from './GreetingConsole'
import { GreetingQuota } from '../components/recruiting/GreetingQuota'
export type PublishedJobsState = {
  jobs: { id: string; title: string; details: string[]; status: string; selected: number; platform_id: string }[]
  sync: { synced_at?: string; attempted_at?: string; total?: number; error?: string }
  budget: { mode: string; limit: number }
  daily: { sent: number; attempted: number; platform_remaining: number | null; custom_remaining: number | null; date: string }
  attempts?: GreetingAttempt[]
  selected_count: number; running: boolean; blockers: string[]
}
type Act = (operation: string, payload?: object, success?: string) => Promise<boolean>
export function PublishedJobs({ data, act, busy }: { data: PublishedJobsState; act: Act; busy: boolean }) {
  const [draftIds, setDraftIds] = useState<string[] | null>(null)
  const [mode, setMode] = useState(data.budget.mode)
  const [limit, setLimit] = useState(String(data.budget.limit))
  const [tab, setTab] = useState('open')
  const [search, setSearch] = useState('')
  const [expanded, setExpanded] = useState<string | null>(null)
  const attempted = useRef(false)
  useEffect(() => {
    if (attempted.current || data.sync.attempted_at || busy) return
    attempted.current = true
    void act('jobs/sync', {}, '已读取平台岗位；新岗位默认未勾选')
  }, [data.sync.attempted_at, act, busy])
  const open = data.jobs.filter(j => j.status === '开放中')
  const others = data.jobs.filter(j => j.status !== '开放中')
  const saved = data.jobs.filter(j => j.selected).map(j => j.id)
  const ids = draftIds ?? saved
  const dirty = JSON.stringify([...ids].sort()) !== JSON.stringify([...saved].sort())
  const visible = (tab === 'open' ? open : others).filter(j => `${j.title} ${j.details.join(' ')}`.toLocaleLowerCase().includes(search.trim().toLocaleLowerCase()))
  const saveSelection = async () => { if (await act('jobs/select', { ids }, '岗位选择已保存；自动外发尚未开启')) setDraftIds(null) }
  const toggle = (id: string) => setDraftIds(ids.includes(id) ? ids.filter(i => i !== id) : [...ids, id])
  return <>
    <div className="rd-page-heading"><div><h2>选择自动处理的岗位</h2><p>勾选已发布岗位，为找人、打招呼和后续沟通确定处理范围。</p></div><div className="rd-sync-heading"><button className="rd-button" disabled={busy || dirty} onClick={() => act('jobs/sync', {}, '岗位已同步；新增岗位未自动勾选')}><RefreshCw size={15} className={busy ? 'animate-spin' : ''} />同步已发布岗位</button><small>{data.sync.synced_at ? `上次同步：${new Date(data.sync.synced_at).toLocaleString('zh-CN', { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', hour12: false })}` : '等待首次同步'}</small></div></div>
    {data.sync.error && <div className="rd-error" role="alert"><CircleAlert size={16} />{data.sync.error}</div>}
    <div className="rd-jobs-grid"><section className="rd-panel rd-jobs-panel"><div className="rd-job-tabs" role="tablist" aria-label="岗位状态" onKeyDown={e => {
        if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return
        e.preventDefault()
        const next = e.key === 'Home' ? 'open' : e.key === 'End' ? 'other' : tab === 'open' ? 'other' : 'open'
        setTab(next)
        e.currentTarget.querySelector<HTMLButtonElement>(`#jobs-tab-${next}`)?.focus()
      }}><button role="tab" tabIndex={tab === 'open' ? 0 : -1} aria-selected={tab === 'open'} aria-controls="published-job-list" id="jobs-tab-open" onClick={() => setTab('open')}>开放中 <span>{open.length}</span></button><button role="tab" tabIndex={tab === 'other' ? 0 : -1} aria-selected={tab === 'other'} aria-controls="published-job-list" id="jobs-tab-other" onClick={() => setTab('other')}>其他岗位 <span>{others.length}</span></button></div>
      <label className="rd-search"><Search size={16} /><input aria-label="搜索岗位" value={search} onChange={e => setSearch(e.target.value)} placeholder="搜索岗位名称、城市或关键词" /></label>
      <div id="published-job-list" role="tabpanel" aria-labelledby={`jobs-tab-${tab === 'open' ? 'open' : 'other'}`}><div className="rd-table-scroll"><table className="rd-table rd-job-table"><thead><tr><th className="rd-check-cell"><span className="sr-only">选择</span></th><th>岗位名称</th><th>城市</th><th>薪资</th><th>{tab === 'open' ? '自动处理范围' : '岗位状态'}</th><th>操作</th></tr></thead><tbody>{visible.map(job => {
        const selected = ids.includes(job.id)
        const salary = job.details.find(d => /\d.*(?:K|k|元|万|千)/.test(d)) || '—'
        const secondary = job.details.slice(1).filter(d => d !== salary).join(' · ')
        return <Fragment key={job.id}><tr className={selected ? 'is-selected' : ''}><td className="rd-check-cell">{job.status === '开放中' ? <input aria-label={`选择${job.title}`} type="checkbox" checked={selected} disabled={busy} onChange={() => toggle(job.id)} /> : <span className="rd-subtle">—</span>}</td><td><strong>{job.title}</strong><small className="rd-job-desktop-meta">{secondary}</small><small className="rd-job-mobile-meta">{job.details.join(' · ')}</small></td><td>{job.details[0] || '—'}</td><td className="rd-salary">{salary}</td><td>{job.status === '开放中' ? <div className="rd-scope-control"><button type="button" role="switch" className="rd-switch" aria-label={`${job.title}自动处理范围`} aria-checked={selected} disabled={busy} onClick={() => toggle(job.id)}><span /></button><span className={selected ? 'rd-selected-text' : 'rd-subtle'}>{selected ? '已勾选' : '未勾选'}</span></div> : <span className="rd-badge">{job.status}</span>}</td><td><button className="rd-text-button" onClick={() => setExpanded(expanded === job.id ? null : job.id)} aria-expanded={expanded === job.id} aria-label={`查看${job.title}详情`}>详情 {expanded === job.id ? <ChevronDown size={12} /> : <ChevronRight size={12} />}</button></td></tr>{expanded === job.id && <tr><td colSpan={6} className="rd-job-expanded"><strong>{job.title}</strong><p>{job.details.join(' · ')}</p><span>平台状态：{job.status}。{selected ? '已加入勾选范围，保存后生效；自动执行仍待接通。' : '当前未加入自动处理范围。'}</span></td></tr>}</Fragment>
      })}</tbody></table></div>{!visible.length && <div className="rd-empty"><Search size={24} /><p>{search ? '没有符合搜索条件的岗位' : data.sync.synced_at ? '这里暂时没有岗位' : '正在等待同步已发布岗位'}</p>{search && <button className="rd-text-button" onClick={() => setSearch('')}>清除搜索</button>}</div>}</div>
      <div className="rd-selection-footer"><div><strong>已选 <b>{ids.length}</b> 个岗位</strong><small>{dirty ? '有未保存修改' : '已保存'} · 自动外发尚未开启</small></div><button className="rd-button primary" aria-label="保存岗位选择" disabled={busy || !dirty} onClick={saveSelection}><Check size={15} />保存选择</button></div>
    </section><aside className="rd-jobs-aside"><form className="rd-panel rd-budget-form" onSubmit={e => { e.preventDefault(); void act('jobs/budget', { mode, limit: Number(limit) }, '每日招呼额度设置已保存；没有启动外发') }}><h3>每日招呼额度</h3><p className="rd-small">按账号设置，所有勾选岗位共用。</p><label className="rd-radio-option"><input type="radio" aria-label="用完平台当天可用额度" name="budget-mode" checked={mode === 'platform'} onChange={() => setMode('platform')} /><span><strong>用完平台当天可用额度</strong><small>以平台当天实际可用次数为准</small></span></label><label className="rd-radio-option"><input type="radio" name="budget-mode" checked={mode === 'custom'} onChange={() => setMode('custom')} /><span><strong>自定义每日上限</strong></span></label><div className="rd-limit-field"><label htmlFor="greeting-daily-limit" className="sr-only">每日最多招呼人数</label><input id="greeting-daily-limit" type="number" min={1} max={10000} step={1} required={mode === 'custom'} disabled={mode !== 'custom'} value={limit} onChange={e => setLimit(e.target.value)} /><span>人 / 天</span></div><p className="rd-budget-note">北京时间每日重算，不超过平台剩余额度。</p><GreetingQuota data={data} compact /><button className="rd-button primary rd-wide" aria-label="保存额度设置" disabled={busy || (mode === data.budget.mode && Number(limit) === data.budget.limit)}>保存额度</button></form>
      <div className="rd-tip"><Info size={17} /><div><strong>新同步岗位默认不启用</strong><p>只有你勾选并保存的岗位，才会加入自动处理范围。</p></div></div>
      <section className="rd-panel rd-readiness"><div className="rd-panel-heading"><h3>执行状态</h3><span className="rd-badge">尚未开启</span></div><ul>{data.blockers.map(b => <li key={b}>{b}</li>)}</ul><p className="rd-small">待接通后按所选额度执行。当前测试不发送面试邀约。</p></section>
    </aside></div>
  </>
}
