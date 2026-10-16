def fresh_quota(service, remaining=100):
    from bosshunter.recruiting.jobs import day_key
    from bosshunter.recruiting.store import now
    service.store.set_setting('greeting_quota', {'date': day_key(), 'updated_at': now(),
                                               'remaining': remaining, 'unlimited': False,
                                               'local_used_at_read': 0})


def authorize(service, job_id='test-job', title='测试岗位'):
    service.jobs.import_snapshot({'complete': True, 'total': 1, 'jobs': [
        {'platform_id': job_id, 'title': title, 'status': '开放中', 'details': []}]})
    service.jobs.select(['boss-' + job_id])
    if not service.store.setting('greeting_quota'):
        fresh_quota(service)
