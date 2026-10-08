import { useEffect, useState } from 'react'
import type { State } from './RecruitingPage'

const titles: Record<string, string> = {
  taken_over: '人工接管中', queued: '已发现新消息', syncing: '正在同步会话', sync_before_generation: '生成前同步',
  generating: '模型生成中', sync_after_generation: '生成后核对', drafted: '草稿已保存',
  checking_browser: '检查 BOSS 对话', preflight: '发送前检查', verifying_account: '核实发送账号',
  waiting_throttle: '等待节流', waiting_browser: '等待打开 BOSS 对话', waiting_model: '等待配置模型',
  sending: '正在发送', sent: '已确认发送', uncertain: '发送结果待核实',
  needs_attention: '需要人工处理', blocked: '自动处理被阻止', paused: '已暂停',
  failed: '本轮处理失败', idle: '等待下一轮检查',
}

export function ReplyProgress({ data, conversation }: { data: State; conversation: State['conversations'][number] }) {
  const [clock, setClock] = useState(Date.now())
  useEffect(() => {
    const timer = setInterval(() => setClock(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [])
  const progress = data.reply_progress?.[conversation.id]
  const work = data.reply_work?.[conversation.id]
  const draft = data.outbox.find(d => d.conversation_id === conversation.id && d.kind === 'reply')
  const alive = data.worker?.alive ?? false
  let stage = progress?.stage || (work?.status === 'waiting' ? 'queued' : work?.status === 'needs_attention' ? 'needs_attention' : draft?.status || 'idle')
  let message = progress?.message || work?.reason || (draft?.status === 'sent' ? '页面已确认出现对应的本人消息' : '等待下一轮检查；不会自动回复绑定前的历史消息')
  if (!progress?.active && draft && ['sending', 'uncertain'].includes(draft.status)) {
    stage = 'uncertain'; message = '发送结果尚未确认，请先核实 BOSS，禁止重复发送'
  } else if (!progress?.active && draft?.status === 'sent' && work?.status !== 'waiting') {
    stage = 'sent'; message = '页面已确认出现对应的本人消息'
  }
  if (conversation.taken_over && !progress?.active && stage !== 'uncertain') {
    stage = 'taken_over'; message = '消息同步保留，模型评分、回复生成和自动外发暂停'
  }
  const until = progress?.active && alive ? progress.wait_until : undefined
  const seconds = until ? Math.max(0, Math.ceil(until - clock / 1000)) : 0
  return <section className="cw-reply-progress" aria-label="自动回复进度" role="status">
    <div><strong>{titles[stage] || '等待处理'}</strong><span>自动外发：{conversation.auto_send ? '开启' : '关闭'}</span></div>
    <p>{message}</p>
    {until && <p>{seconds > 0 ? `约 ${Math.floor(seconds / 60)} 分 ${seconds % 60} 秒后可继续` : '预计等待时间已到，等待后台更新状态'} · 最早 {new Date(until * 1000).toLocaleTimeString('zh-CN', { hour12: false })}</p>}
    {!!conversation.do_not_contact ? <p>已停止联系，禁止发送。</p> : conversation.taken_over ? <p>继续同步消息和读取简历；暂停模型评分、回复生成和自动外发，仍可人工确认普通回复。</p> : !data.positions.find(p => p.id === conversation.position_id)?.enabled ? <p>岗位自动任务已暂停，仍可人工确认普通回复。</p> : !alive ? <p>worker 未运行，自动处理无法继续。以上为最后记录的状态。</p> : !data.worker?.monitor_enabled ? <p>监测已停止，任务和草稿保留。</p> : null}
    {data.request_budget && data.request_budget.remaining <= 0 && <p>今日请求预算已耗尽，请先处理预算限制。</p>}
    {progress?.next_check_at && !progress.active && alive && data.worker?.monitor_enabled && !['sent', 'uncertain'].includes(stage) && <p>下一轮最早 {new Date(progress.next_check_at * 1000).toLocaleTimeString('zh-CN', { hour12: false })} 检查；多会话依次轮询。</p>}
    {progress?.updated_at && <small>状态更新于 {new Date(progress.updated_at).toLocaleTimeString('zh-CN', { hour12: false })} · 页面约每 15 秒读取本地状态，倒计时不是发送保证</small>}
  </section>
}
