import type { PublishedJobsState } from '../../pages/PublishedJobs'
export function GreetingQuota({ data, compact = false }: { data?: PublishedJobsState; compact?: boolean }) {
  const sent = data?.daily.sent ?? 0
  const attempted = data?.daily.attempted ?? 0
  const custom = data?.budget.mode === 'custom'
  const total = custom ? data.budget.limit : null
  const remaining = custom ? data.daily.custom_remaining : data?.daily.platform_remaining
  const progress = total ? Math.min(100, Math.max(0, attempted / total * 100)) : null
  return <div className={`rd-quota${compact ? ' compact' : ''}`}><div className="rd-quota-title">{compact ? '今日使用情况' : '今日主动招呼'}</div><div className="rd-quota-value"><strong>{sent}</strong><span>/ {total ?? '—'}</span></div><div className="rd-progress" role={progress !== null ? 'progressbar' : undefined} aria-label="今日招呼额度使用情况" aria-valuemin={progress !== null ? 0 : undefined} aria-valuemax={total ?? undefined} aria-valuenow={progress !== null ? Math.min(attempted, total!) : undefined}><span style={{ width: `${progress ?? 0}%` }} /></div><div className="rd-quota-remaining"><span>{compact && !custom ? '平台剩余' : '剩余'}</span><strong>{remaining ?? '未读取'}</strong></div>{!compact && <span className="rd-quota-scope">账号总额度 · 各岗位共用</span>}{compact && <p className="rd-small">{custom ? '自定义上限仍受平台剩余额度限制' : '平台额度读取后显示使用进度'}</p>}</div>
}
