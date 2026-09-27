"""Read-only employer data using domain-scoped local Chrome cookies.

No navigation, cookie export, session persistence or platform write endpoints.
"""
from http.cookiejar import CookieJar
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo
from urllib.parse import urlparse, parse_qs
from io import BytesIO
from hashlib import sha256
import re
import time
import httpx
from .browser import BrowserError


class LocalBossSession:
    def __init__(self, cookie_loader=None, transport=None):
        self.cookie_loader = cookie_loader or self.load_cookies
        self.transport = transport

    def read_conversation(self, ident, name, position_title, expected_account=None, *, include_attachments=False):
        """Read one already-bound conversation. Never mark read or operate a tab.

        The job title comes from the existing verified binding, not a name search.
        All returned messages must belong to this exact peer and one employer.
        Unknown payloads remain explicit system records, not guessed dialogue.
        """
        match = re.fullmatch(r'([1-9][0-9]*)-([01])', ident)
        if not match:
            raise BrowserError('绑定会话的标识不支持后台读取，请先核对会话身份')
        gid, source = match.groups()
        messages, seen, account_ids, names = [], set(), set(), set()
        attachments = []
        cursor = 0
        with httpx.Client(cookies=self.cookie_loader(), transport=self.transport, trust_env=False,
                          timeout=20, follow_redirects=False,
                          headers={'Referer': 'https://www.zhipin.com/web/chat/index'}) as client:
            for page in range(1, 11):
                try:
                    response = client.get('https://www.zhipin.com/wapi/zpchat/boss/historyMsg',
                                          params={'src': int(source), 'gid': gid, 'maxMsgId': cursor, 'c': 20, 'page': page})
                    if response.is_redirect and urlparse(response.headers.get('location', '')).path == '/web/passport/zp/verify.html':
                        raise BrowserError('BOSS 要求账号验证，请在现有 Chrome 招聘端页面完成验证后重新读取；已有记录已保留')
                    response.raise_for_status()
                    body = response.json()
                except (httpx.HTTPError, ValueError):
                    raise BrowserError('后台会话读取未完成；保留原记录，不自动重试') from None
                if not isinstance(body, dict) or body.get('code') != 0:
                    raise BrowserError('BOSS 未接受会话读取请求，请核实登录状态；未操作标签页')
                data = body.get('zpData')
                if not isinstance(data, dict) or not isinstance(data.get('messages'), list) or type(data.get('hasMore')) is not bool:
                    raise BrowserError('会话数据结构变化，保留原记录')
                for item in data['messages']:
                    if not isinstance(item, dict):
                        raise BrowserError('会话记录结构不完整')
                    sender, receiver = item.get('from'), item.get('to')
                    if not isinstance(sender, dict) or not isinstance(receiver, dict):
                        raise BrowserError('消息收发身份缺失，停止同步')
                    incoming = str(sender.get('uid')) == gid
                    outgoing = str(receiver.get('uid')) == gid
                    if incoming == outgoing:
                        raise BrowserError('消息不属于绑定候选人，停止同步')
                    peer, employer = (sender, receiver) if incoming else (receiver, sender)
                    if str(peer.get('source')) != source or not employer.get('uid'):
                        raise BrowserError('消息来源或招聘账号身份不一致，停止同步')
                    account_ids.add(str(employer['uid']))
                    if peer.get('name'):
                        names.add(peer['name'])
                    mid, stamp = item.get('mid'), item.get('time')
                    if type(mid) is not int or mid <= 0 or type(stamp) is not int or stamp <= 0:
                        raise BrowserError('消息唯一标识或时间缺失，停止同步')
                    if mid in seen:
                        raise BrowserError('消息分页重复，保留原记录')
                    seen.add(mid)
                    text, kind, system = self.message_content(item)
                    if incoming:
                        link = item.get('body', {}).get('hyperLink', {})
                        wrapper = urlparse(link.get('url', ''))
                        query = parse_qs(wrapper.query)
                        if link.get('hyperLinkType') == 1 and wrapper.hostname == 'bosszhipin.app' and 'encryptId' in query and 'url' in query:
                            attachments.append({'url': query['url'][0], 'name': str(link.get('text', '附件简历'))[:200],
                                                'message_id': str(mid), 'timestamp': stamp})
                    messages.append({'id': str(mid), 'direction': 'system' if system else 'in' if incoming else 'out',
                                     'kind': kind, 'text': text, 'timestamp': stamp,
                                     'time': datetime.fromtimestamp(stamp / 1000, ZoneInfo('Asia/Shanghai')).strftime('%m-%d %H:%M')})
                if data['hasMore'] is False:
                    break
                next_cursor = data.get('minMsgId')
                if not data['messages'] or type(next_cursor) is not int or next_cursor <= 0 or next_cursor == cursor:
                    raise BrowserError('消息分页游标异常，保留原记录')
                cursor = next_cursor
                time.sleep(.5)
            else:
                raise BrowserError('单会话超过最小样本读取上限，保留原记录')
        if not messages or len(account_ids) != 1 or names != {name}:
            raise BrowserError('候选人或招聘账号未能准确核实，保留原记录')
        account = next(iter(account_ids))
        if expected_account and account != expected_account:
            raise BrowserError('当前登录招聘账号与绑定会话不一致，停止同步')
        messages.sort(key=lambda m: (m['timestamp'], int(m['id'])))
        result = {'id': ident, 'name': name, 'position_title': position_title, 'messages': messages,
                'account_uid': account, 'editor_empty': False, 'stable_message_ids': True,
                'source': 'boss_local_cookie_http', 'history_complete': False,
                'coverage': 'BOSS 历史消息接口当前可返回的全部记录；更早或已删除的历史无法保证完整'}
        attachments.sort(key=lambda a: (a['timestamp'], int(a['message_id'])))
        result['received_resume_message_id'] = attachments[-1]['message_id'] if attachments else None
        if include_attachments:
            # Signed links are internal-only: resume reads consume them in memory.
            result['_attachments'] = sorted(attachments, key=lambda a: (a['timestamp'], int(a['message_id'])))
        return result

    def read_resume(self, ident, name, position_title, expected_account=None):
        snapshot = self.read_conversation(ident, name, position_title, expected_account, include_attachments=True)
        attachments = snapshot.pop('_attachments')
        if not attachments:
            raise BrowserError('当前会话没有已收到的附件简历；不会自动索取或接收')
        attachment = attachments[-1]
        url = urlparse(attachment['url'])
        if (url.scheme != 'https' or url.hostname != 'm.zhipin.com' or url.port not in (None, 443)
                or url.username or url.password or url.fragment or url.path != '/wflow/zpgeek/download/preview4boss'
                or not parse_qs(url.query).get('encryptParam')):
            raise BrowserError('附件地址不属于已验证的 BOSS 简历预览接口，停止读取')
        limit = 20 * 1024 * 1024
        content = bytearray()
        try:
            with httpx.Client(cookies=self.cookie_loader(), transport=self.transport, trust_env=False,
                              timeout=30, follow_redirects=False,
                              headers={'Referer': 'https://www.zhipin.com/web/chat/index'}) as client:
                with client.stream('GET', attachment['url']) as response:
                    response.raise_for_status()
                    if response.is_redirect:
                        raise BrowserError('附件地址发生跳转，未继续下载；请核实附件权限')
                    for chunk in response.iter_bytes():
                        content.extend(chunk)
                        if len(content) > limit:
                            raise BrowserError('简历附件超过 20 MB，保留已有简历，请人工核对')
        except httpx.HTTPError:
            raise BrowserError('BOSS 简历附件下载未完成或链接已失效；保留已有简历，未自动重试') from None
        result = self.extract_pdf(bytes(content))
        result['meta'].update({'filename': attachment['name'], 'message_id': attachment['message_id'],
                               'sha256': sha256(content).hexdigest(), 'bytes': len(content)})
        return result

    @staticmethod
    def extract_pdf(content):
        if not content.startswith(b'%PDF-'):
            raise BrowserError('附件未返回有效 PDF，可能权限已失效或格式暂不支持；保留已有简历')
        try:
            from pypdf import PdfReader
            reader = PdfReader(BytesIO(content))
            if reader.is_encrypted and not reader.decrypt(''):
                raise BrowserError('简历 PDF 已加密，需解密后再读取')
            count = len(reader.pages)
            if not 1 <= count <= 30:
                raise BrowserError('简历页数超过读取范围，请人工核对')
            pages = []
            for index, page in enumerate(reader.pages, 1):
                value = (page.extract_text() or '').strip()
                pages.append({'page': index, 'text': value})
            text = '\n\n'.join(f"第 {page['page']} / {count} 页\n{page['text'] or '（本页没有可提取文字，需核对图片或扫描内容）'}" for page in pages)
            if len(text) > 100000:
                raise BrowserError('简历文字超过读取上限，保留原记录，不能静默截断')
            if not any(page['text'] for page in pages):
                raise BrowserError('该 PDF 没有可提取的文字层，需 OCR 或人工补全；保留已有简历')
            empty = [page['page'] for page in pages if not page['text']]
            note = f'已读取 PDF 全部 {count} 页的文字层；图片、表格及文字顺序仍需核对。'
            if empty:
                note += ' 第 ' + '、'.join(map(str, empty)) + ' 页没有可提取文字。'
            return {'source': 'boss_attachment_pdf_http', 'text': text, 'complete': False,
                    'meta': {'page_count': count, 'pages': [p['page'] for p in pages],
                             'page_characters': [len(p['text']) for p in pages], 'empty_pages': empty,
                             'all_pages_read': True, 'note': note}}
        except BrowserError:
            raise
        except Exception:
            raise BrowserError('PDF 解析未完成，保留已有简历，请核对附件文件') from None

    @staticmethod
    def message_content(item):
        body = item.get('body')
        if not isinstance(body, dict):
            raise BrowserError('消息正文结构不完整')
        kind = body.get('type')
        text = body.get('text')
        if kind == 1 and isinstance(text, str) and text.strip():
            return text.strip(), 'text', item.get('type') == 4
        if kind == 12 and isinstance(body.get('hyperLink'), dict):
            text = body['hyperLink'].get('text')
            if isinstance(text, str) and text.strip():
                return text.strip(), 'card', item.get('type') == 4
        if kind == 7 and isinstance(body.get('dialog'), dict):
            dialog = body['dialog']
            text = '\n'.join(dict.fromkeys(v for k in ('title', 'content', 'text') if isinstance(v := dialog.get(k), str) and v.strip()))
            return text or '平台交互卡片（未执行任何操作）', 'system', True
        if kind == 9 and isinstance(body.get('resume'), dict):
            resume = body['resume']
            # Only job-relevant display fields; no secret IDs, demographic fields or URLs.
            text = '\n'.join(f'{label}：{resume[key]}' for key, label in [('position', '求职方向'), ('workYear', '工作经验'), ('description', '公开介绍')]
                             if isinstance(resume.get(key), str) and resume[key].strip())
            return '候选人资料卡片（非完整简历）' + ('\n' + text if text else ''), 'card', False
        return f'平台消息（正文类型 {kind}，尚未解析，不作为候选人回答）', 'system', True

    @staticmethod
    def load_cookies():
        try:
            import browser_cookie3
        except ImportError as exc:
            raise BrowserError('请安装招聘依赖：pip install -e ".[recruiting]"') from exc
        root = Path.home() / 'Library/Application Support/Google/Chrome/Default'
        cookie_file = next((p for p in [root / 'Cookies', root / 'Network/Cookies'] if p.is_file()), None)
        if cookie_file is None:
            raise BrowserError('未找到本地 Chrome 默认配置的 Cookie 文件')
        try:
            raw = browser_cookie3.chrome(cookie_file=str(cookie_file), domain_name='.zhipin.com')
        except Exception:
            raise BrowserError('无法读取 Chrome 中的 BOSS 登录状态，请检查钥匙串访问权限') from None
        scoped = CookieJar()
        for cookie in raw:
            if cookie.domain.lstrip('.') in {'zhipin.com', 'www.zhipin.com'} and not cookie.is_expired():
                scoped.set_cookie(cookie)
        if not any(c.name in {'wt2', 'zp_at'} for c in scoped):
            raise BrowserError('本地未找到有效期内的 BOSS 登录 Cookie，请在 Chrome 中登录招聘端')
        return scoped

    def read_jobs(self):
        # Each explicit sync refreshes the local login state; secrets stay in memory.
        cookies = self.cookie_loader()
        with httpx.Client(cookies=cookies, transport=self.transport, trust_env=False,
                          timeout=20, follow_redirects=False,
                          headers={'Referer': 'https://www.zhipin.com/web/chat/job/list'}) as client:
            jobs, seen, total = [], set(), None
            for page in range(1, 101):
                try:
                    response = client.get('https://www.zhipin.com/wapi/zpjob/job/data/list',
                                          params={'page': page, 'pageSize': 20})
                    response.raise_for_status()
                    body = response.json()
                except (httpx.HTTPError, ValueError):
                    raise BrowserError('后台岗位读取未完成；保留已有岗位，不自动重试或操作标签页') from None
                if not isinstance(body, dict) or body.get('code') != 0:
                    raise BrowserError('BOSS 未接受本地登录状态或需要验证，请在 Chrome 中核实；没有操作标签页')
                data = body.get('zpData')
                if not isinstance(data, dict) or not isinstance(data.get('data'), list):
                    raise BrowserError('平台岗位数据结构变化，停止本次同步')
                size = data.get('totalSize')
                if type(size) is not int or size < 0 or data.get('page') != page or (total is not None and total != size):
                    raise BrowserError('岗位分页或总数发生变化，请稍后重新同步')
                total = size
                for item in data['data']:
                    ident, title = item.get('encryptJobId'), item.get('jobName')
                    if not isinstance(ident, str) or not ident or not isinstance(title, str) or not title or ident in seen:
                        raise BrowserError('岗位身份缺失或分页重复，本次不更新岗位范围')
                    seen.add(ident)
                    # Values verified against the visible employer list. Unknown values
                    # stay unselectable instead of being treated as open.
                    status = {0: '开放中', 1: '已关闭', 3: '待开放'}.get(item.get('jobStatus'), '状态待核实')
                    if status == '开放中' and item.get('jobAuditStatus') != 3:
                        status = '审核状态待核实'
                    jobs.append({'platform_id': ident, 'title': title, 'status': status,
                                 'details': [str(item[k]) for k in ('locationName', 'experienceName', 'degreeName', 'salaryDesc', 'jobTypeName') if item.get(k)]})
                if data.get('hasMore') is False:
                    if len(jobs) != total:
                        raise BrowserError('岗位列表未完整读取，本次不更新岗位范围')
                    return {'jobs': jobs, 'total': total, 'complete': True}
                if data.get('hasMore') is not True or not data['data'] or len(jobs) >= total:
                    raise BrowserError('平台分页信息不一致，本次不更新岗位范围')
                time.sleep(.5)
        raise BrowserError('岗位列表超出读取范围')
