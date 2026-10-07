import type { PublishedJobsState } from '../pages/PublishedJobs'

// The server day uses Beijing time; legacy records may only include a timestamp.
export function jobGreetingStats(data: PublishedJobsState | undefined, jobId: string) {
  if (!data?.attempts) return null
  const records = data.attempts.filter(record => record.job_id === jobId)
  const today = records.filter(record => {
    if (record.day) return record.day === data.daily.date
    const date = new Date(record.created_at)
    if (Number.isNaN(date.getTime())) return false
    const parts = new Intl.DateTimeFormat('en', { timeZone: 'Asia/Shanghai', year: 'numeric', month: '2-digit', day: '2-digit' }).formatToParts(date)
    const part = (type: string) => parts.find(p => p.type === type)?.value
    return `${part('year')}-${part('month')}-${part('day')}` === data.daily.date
  })
  return {
    sent: today.filter(record => record.status === 'sent').length,
    attempted: today.length,
    review: records.filter(record => ['uncertain', 'sending'].includes(record.status)).length,
  }
}
