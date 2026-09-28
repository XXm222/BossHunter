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
import html
from bosshunter.throttle import PageThrottle


class LocalBossSession:
    def __init__(self, cookie_loader=None, transport=None, throttle=None):
        self.cookie_loader = cookie_loader or self.load_cookies
        self.transport = transport
        # 后台读取节流：真实网络下每个操作间隔 2-5 秒降低风控；测试用 MockTransport 时不加延迟
        if throttle is not None:
            self.throttle = throttle
        elif transport is None:
            self.throttle = PageThrottle(delay_min=2.0, delay_max=5.0)
        else:
            self.throttle = PageThrottle(delay_min=0.0, delay_max=0.0)

    def read_conversation(self, ident, name, position_title, expected_account=None, *, include_attachments=False):
        """Read one already-bound conversation. Never mark read or operate a tab.

        The job title comes from the existing verified binding, not a name search.
        All returned messages must belong to this exact peer and one employer.
        Unknown payloads remain explicit system records, not guessed dialogue.
        """
        self.throttle.wait()
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
        self.throttle.wait()
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
                # 没有文字层时（扫描版简历）尝试 OCR 兜底；失败或未安装则抛错。
                ocr_used = False
                if not any(page['text'] for page in pages):
                    _ocr_pdf_or_raise(content, pages)
                    ocr_used = True

                text = '\n\n'.join(
                    f"第 {page['page']} / {count} 页\n{page['text'] or '（本页没有可提取文字，需核对图片或扫描内容）'}" for
                    page in pages)
                if len(text) > 100000:
                    raise BrowserError('简历文字超过读取上限，保留原记录，不能静默截断')

                empty = [page['page'] for page in pages if not page['text']]
                if ocr_used:
                    note = f'已通过 OCR 识别 PDF 全部 {count} 页；识别结果与文字顺序需人工核对。'
                else:
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
            # BOSS 用 &lt;copy&gt;/&lt;phone&gt; 包装联系信息，解码实体并去掉标签，只留内容
            clean = re.sub(r'</?(?:copy|phone)\s*>', '', html.unescape(text))
            return clean.strip(), 'text', item.get('type') == 4
        if kind == 2 and isinstance(body.get('sound'), dict):
            # 语音消息：不转录，只标注时长；不作为候选人的口头回答
            duration = body['sound'].get('duration')
            return (f'语音消息（{duration} 秒）' if isinstance(duration, int) else '语音消息'), 'system', True
        if kind == 3 and isinstance(body.get('image'), dict):
            # 图片消息：不识别内容，仅标注；不作为候选人的口头回答
            return '图片消息', 'system', True
        if kind == 4 and isinstance(body.get('action'), dict):
            # 动作/操作记录（交换微信、接受简历等系统动作），不作为候选人的口头回答
            return '系统操作记录', 'system', True
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
        self.throttle.wait()
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

    def list_contacts(self):
        """读取招聘账号的联系人列表（近 30 天有会话的候选人）。

        返回 [{ident, name, position_title, last_ts}]，按最后消息时间倒序。
        filterByLabel 只返回 uid 和姓名、不返回岗位名，position_title 留空由绑定时手动填写。
        """
        self.throttle.wait()
        cookies = self.cookie_loader()
        with httpx.Client(cookies=cookies, transport=self.transport, trust_env=False,
                          timeout=20, follow_redirects=False,
                          headers={'Referer': 'https://www.zhipin.com/web/chat/index'}) as client:
            try:
                response = client.post('https://www.zhipin.com/wapi/zprelation/friend/filterByLabel',
                                       data={'labelId': '0', 'encJobId': '', 'sort': '', 'scene': '0'})
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, ValueError):
                raise BrowserError('后台联系人读取未完成；保留原记录，不自动重试') from None
        if not isinstance(body, dict) or body.get('code') != 0:
            raise BrowserError('BOSS 未接受联系人读取请求，请核实登录状态；未操作标签页')
        data = body.get('zpData')
        if not isinstance(data, dict) or not isinstance(data.get('result'), list):
            raise BrowserError('联系人数据结构变化，停止本次同步')
        contacts = []
        for item in data['result']:
            if not isinstance(item, dict):
                continue
            uid = item.get('friendId')
            name = item.get('name')
            if not uid or not isinstance(name, str) or not name.strip():
                continue
            contacts.append({
                'ident': f'{uid}-0',
                'name': name.strip(),
                'position_title': '',  # 岗位名需绑定时手动填写
                'last_ts': item.get('updateTime'),
            })
        contacts.sort(key=lambda c: c.get('last_ts') or 0, reverse=True)
        return contacts

    def read_greeting_quota(self):
        """读取招聘账号今日剩余打招呼权益（主动沟通额度）。

        返回 {'limit': 总限额, 'used': 已用, 'remaining': 剩余}；
        无限制（limitCount 为 -1）或结构异常时 limit/remaining 为 None。
        """
        self.throttle.wait()
        cookies = self.cookie_loader()
        with httpx.Client(cookies=cookies, transport=self.transport, trust_env=False,
                          timeout=20, follow_redirects=False,
                          headers={'Referer': 'https://www.zhipin.com/web/chat/index'}) as client:
            try:
                response = client.get('https://www.zhipin.com/wapi/zpboss/h5/weeklyReportV3/recruitDataCenter/get.json',
                                      params={'jobId': '0', 'platform': '1', 'date': ''})
                response.raise_for_status()
                body = response.json()
            except (httpx.HTTPError, ValueError):
                raise BrowserError('后台额度读取未完成；保留原记录，不自动重试') from None
        if not isinstance(body, dict) or body.get('code') != 0:
            raise BrowserError('BOSS 未接受额度读取请求，请核实登录状态；未操作标签页')
        data = body.get('zpData')
        if not isinstance(data, dict):
            raise BrowserError('额度数据结构变化，停止本次同步')
        chat_state = data.get('dailyRightStates')
        if not isinstance(chat_state, dict) or not isinstance(chat_state.get('chatRightState'), dict):
            raise BrowserError('额度数据结构变化，停止本次同步')
        bars = chat_state['chatRightState'].get('progressBarList')
        if not isinstance(bars, list) or not bars or not isinstance(bars[0], dict):
            raise BrowserError('额度数据结构变化，停止本次同步')
        bar = bars[0]
        limit = bar.get('limitCount')
        used = bar.get('usedCount')
        if isinstance(limit, int) and isinstance(used, int) and limit >= 0:
            return {'limit': limit, 'used': used, 'remaining': max(0, limit - used)}
        # 无限制（limitCount 为 -1）或字段异常：无可用额度上限
        return {'limit': None, 'used': used if isinstance(used, int) else 0, 'remaining': None}


# 渲染分辨率（DPI）：中文简历识别精度与耗时/内存之间的平衡点；需要更高精度可调到 300，但会更慢、更占内存。
_OCR_RENDER_DPI = 200


def _ocr_available():
    """OCR 依赖（PyMuPDF + RapidOCR）是否可导入。

    只做 import 探测，不初始化引擎——引擎初始化失败会单独在
    _ocr_pdf_pages 里抛错，由调用方统一转成用户可读的提示。
    """
    try:
        import fitz  # noqa: F401  PyMuPDF
        import rapidocr_onnxruntime  # noqa: F401
        return True
    except ImportError:
        return False


def _ocr_pdf_pages(content: bytes):
    """把 PDF 每页渲染成图片并 OCR，返回每页识别出的文字。

    返回的列表长度等于 PDF 页数、顺序与页序一致，便于调用方逐页回填。
    依赖在函数内懒加载：未安装 `ocr` extra 时招聘端其余功能不受影响。
    """
    # 懒加载，避免把重依赖（PyMuPDF / RapidOCR）压到模块导入期。
    import fitz
    from rapidocr_onnxruntime import RapidOCR

    engine = RapidOCR()  # 首次初始化会下载模型权重到本地缓存
    pages: list[str] = []
    # 直接从内存字节打开 PDF，不落盘，避免残留中间图片文件。
    with fitz.open(stream=content, filetype="pdf") as doc:
        for page in doc:
            # get_pixmap 把 PDF 页栅格化；OCR 输入用 PNG 字节，不依赖 numpy。
            png = page.get_pixmap(dpi=_OCR_RENDER_DPI).tobytes("png")
            pages.append(_ocr_image_text(engine, png))
    return pages


def _ocr_image_text(engine, png: bytes):
    """对单页渲染图跑 OCR，按行拼接识别出的文字。

    行与行之间用换行连接，与 pypdf 提取文字层时保持同一拼接口径。
    """
    result = engine(png)
    # rapidocr_onnxruntime 1.x 返回 (result, elapse)；result 才是识别结果。
    if isinstance(result, tuple):
        result = result[0]
    if not result:  # 空页，或未识别出任何文字
        return ""
    lines = []
    for item in result:
        text = _ocr_entry_text(item)
        if text:
            lines.append(text)
    return "\n".join(lines)


def _ocr_entry_text(item):
    """从单个 OCR 结果条目里取出识别文字。

    兼容多种返回形态：1.x 的 [box, text, score] 列表，以及带 .text
    属性的对象；取不到时返回空串，由上层统一过滤。
    """
    if isinstance(item, str):
        return item.strip()
    text = getattr(item, "text", None)
    if isinstance(text, str):
        return text.strip()
    # 1.x 列表形态：[box(4 点坐标), text(识别文字), score(置信度)]
    if isinstance(item, (list, tuple)) and len(item) >= 2 and isinstance(item[1], str):
        return item[1].strip()
    return ""


def _ocr_pdf_or_raise(content: bytes, pages: list[dict]):
    """没有文字层时尝试 OCR，并把识别文字回填进 pages（原地修改）。

    未安装 OCR 依赖或识别失败时抛 BrowserError，绝不静默吞掉——
    保持与「文字层为空时明确失败」相同的安全边界，只是多一条兜底路径。
    """
    if not _ocr_available():
        # 依赖未安装：给出可执行的安装提示，而不是含糊的「请先 OCR」。
        raise BrowserError('该 PDF 没有可提取的文字层，且未安装 OCR 依赖；'
                           '请用 `pip install -e ".[ocr]"` 安装后重试，或人工补全；保留已有简历')
    try:
        ocr_texts = _ocr_pdf_pages(content)
    except Exception as exc:
        # 识别失败不静默继续，保留已有简历由人工核对。
        raise BrowserError('该 PDF 没有可提取的文字层，OCR 识别失败；保留已有简历，请人工核对') from exc
    if len(ocr_texts) != len(pages):
        raise BrowserError('OCR 识别出的页数与 PDF 页数不一致；保留已有简历，请人工核对')
    # 逐页回填；识别为空的行后续仍会进入 empty_pages 供人工核对。
    for entry, ocr_text in zip(pages, ocr_texts):
        entry['text'] = ocr_text.strip()
