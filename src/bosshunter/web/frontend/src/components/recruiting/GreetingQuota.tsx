import type { PublishedJobsState } from '../../pages/PublishedJobs'
export function GreetingQuota({ data, compact = false }: { data?: PublishedJobsState; compact?: boolean }) {
  const sent = data?.daily.sent ?? 0
  const attempted = data?.daily.attempted ?? 0
  const custom = data?.budget.mode === 'custom'
  const total = custom ? data.budget.limit : null
  const remaining = custom ? data.daily.custom_remaining : data?.daily.platform_remaining
  const progress = total ? Math.min(100, Math.max(0, attempted / total * 100)) : null
  const status = data?.daily.quota_status || (data?.daily.platform_remaining != null ? 'fresh' : 'unread')
  const labels = { unread: '未读取', expired: '已过期', unknown: '额度未知', fresh: '已读取' }
  const platformValue = status === 'fresh' ? data?.daily.quota_unlimited ? '不限' : remaining : labels[status]
  const readTime = data?.daily.quota_updated_at ? new Date(data.daily.quota_updated_at) : null
  const reference = data?.daily.quota_unlimited ? '不限' : data?.daily.quota_last_remaining
  return <div className={`rd-quota${compact ? ' compact' : ''}`}><div className="rd-quota-title">{compact ? '今日使用情况' : '今日主动招呼'}</div><div className="rd-quota-value"><strong>{sent}</strong><span>/ {total ?? '—'}</span></div><div className="rd-progress" role={progress !== null ? 'progressbar' : undefined} aria-label="今日招呼额度使用情况" aria-valuemin={progress !== null ? 0 : undefined} aria-valuemax={total ?? undefined} aria-valuenow={progress !== null ? Math.min(attempted, total!) : undefined}><span style={{ width: `${progress ?? 0}%` }} /></div><div className="rd-quota-remaining"><span>{compact && !custom ? '平台剩余' : '剩余'}</span><strong>{custom ? remaining ?? '额度未知' : platformValue ?? '额度未知'}</strong></div>{!compact && <span className="rd-quota-scope">账号总额度 · 各岗位共用</span>}
    <p className="rd-small">平台额度：{labels[status]}{status === 'expired' ? '，请重新读取后再使用' : status === 'fresh' ? '（有效期 10 分钟）' : ''}</p>
    {readTime && !Number.isNaN(readTime.getTime()) && <p className="rd-small">上次读取：{readTime.toLocaleString('zh-CN', { hour12: false })}{reference != null ? `；当时剩余 ${reference}` : ''}{status !== 'fresh' ? '（仅供参考）' : ''}</p>}
    {compact && <p className="rd-small">{custom ? '自定义上限仍受平台剩余额度限制' : '平台额度读取后显示使用进度'}</p>}</div>
}
