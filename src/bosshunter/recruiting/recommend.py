"""Patchright-backed candidate verification on the BOSS recommend page.

Connects to the user's running Chrome over CDP — reusing their real login and
fingerprint — to lower platform risk, then reloads the recommend page and reads
the fresh first candidate card. BOSS only refreshes the recommend list on a real
browser reload, not on in-app page switches, so the reload is mandatory.

Safety: never creates, activates, closes, or switches tabs. It only reloads a
page the user already opened on the recommend URL, and it never sends a message.
"""
import json
import time

import httpx
from patchright.sync_api import sync_playwright

from .browser import BrowserError

RECOMMEND_URL_PREFIX = "https://www.zhipin.com/web/chat/recommend"
RECOMMEND_FRAME_PATH = "/web/frame/recommend/"
DEFAULT_CHROME_PORTS = [9222, 9229, 9333]

# 真实卡片结构（对照用户提供的推荐页 HTML）：
#   li.card-item > .candidate-card-wrap > .card-inner（data-geek / data-geekid 为
#   稳定唯一 ID）> ... 姓名在 span.name（img.avatar 的 alt 兜底）。
# 列表里混有非候选卡片（如 .quick-top-item「职位升级曝光」），因此用 .btn-greet
# 锁定「有打招呼按钮的才是候选卡」。

# 点击指定 geekid 卡片的「打招呼」按钮。BOSS 会自动发默认招呼语，随后按钮变为
# 「继续沟通」，所以这里只需点一次，不需要填任何文字。
CLICK_GREET = r"""
(() => {
    const uid = __UID__;
    for (const card of document.querySelectorAll('li.card-item')) {
        const inner = card.querySelector('.card-inner');
        const geekid = (inner?.getAttribute('data-geekid') || inner?.getAttribute('data-geek') || '').trim();
        if (geekid !== uid) continue;
        const btn = card.querySelector('.btn-greet');
        if (!btn) return 'already';
        btn.click();
        return 'clicked';
    }
    return 'missing';
})()
"""

# 读取指定 geekid 卡片的打招呼按钮文案，用于确认是否已变成「继续沟通」。
READ_GREET_TEXT = r"""
(() => {
    const uid = __UID__;
    for (const card of document.querySelectorAll('li.card-item')) {
        const inner = card.querySelector('.card-inner');
        const geekid = (inner?.getAttribute('data-geekid') || inner?.getAttribute('data-geek') || '').trim();
        if (geekid !== uid) continue;
        const btn = card.querySelector('.btn-greet');
        return btn ? (btn.innerText || '').trim() : '';
    }
    return null;
})()
"""


# 读取推荐页顶部职位下拉框当前选中的岗位 ID。选中项是 li.job-item.curr，其 value
# 属性即岗位的 encryptJobId（与 published_jobs 的 platform_id 同一 ID 空间）。
READ_JOB_ID = r"""
(() => {
    const item = document.querySelector('.job-selecter-options .job-item.curr') || document.querySelector('.job-list .job-item.curr');
    return item ? item.getAttribute('value') : null;
})()
"""

# 在职位下拉框里选中指定岗位。先点开下拉框标签，再点对应 job-item；切换岗位会
# 刷新候选人列表。
SELECT_JOB = r"""
(() => {
    const jobId = __JOB_ID__;
    const label = document.querySelector('.job-selecter-wrap .ui-dropmenu-label');
    if (!label) return 'no_label';
    label.click();
    const item = document.querySelector('.job-selecter-options .job-item[value="' + jobId + '"]');
    if (!item) return 'missing';
    item.click();
    return 'selected';
})()
"""

# 读取推荐页所有候选卡：name + data-geekid + 是否还有「打招呼」按钮（greetable）。
READ_CANDIDATES = r"""
(() => {
    const out = [];
    for (const card of document.querySelectorAll('li.card-item')) {
        const inner = card.querySelector('.card-inner');
        const uid = (inner?.getAttribute('data-geekid') || inner?.getAttribute('data-geek') || '').trim();
        if (!uid) continue;
        const name = (card.querySelector('.name')?.innerText || card.querySelector('.avatar')?.getAttribute('alt') || '').trim();
        out.push({ name, uid, greetable: !!card.querySelector('.btn-greet') });
    }
    return out;
})()
"""


class RecommendVerifier:
    def __init__(self, config_provider, cdp_url=None, wait_timeout=15.0):
        self.config_provider = config_provider
        self._cdp_url = cdp_url
        self._wait_timeout = wait_timeout

    def _chrome_ports(self):
        config = self.config_provider() or {}
        browser = config.get("browser", {}) if isinstance(config, dict) else {}
        ports = browser.get("chrome_ports", DEFAULT_CHROME_PORTS)
        if not isinstance(ports, list) or not ports:
            return DEFAULT_CHROME_PORTS
        return [int(p) for p in ports]

    def _find_cdp_url(self):
        if self._cdp_url:
            return self._cdp_url
        for port in self._chrome_ports():
            url = f"http://127.0.0.1:{port}"
            try:
                response = httpx.get(f"{url}/json/version", timeout=2, trust_env=False)
                if response.status_code == 200 and response.json().get("webSocketDebuggerUrl"):
                    return url
            except (httpx.HTTPError, ValueError):
                continue
        raise BrowserError("未找到可连接的 Chrome 调试端口，请确认 Chrome 已用调试端口启动")

    def _bound_target_id(self):
        config = self.config_provider() or {}
        browser = config.get("browser", {}) if isinstance(config, dict) else {}
        return browser.get("recruiting_target_id") or None

    def _cdp_page_targets(self):
        """通过 CDP /json/list 读取当前调试实例的所有 page target（含 id 与 url）。"""
        cdp_url = self._find_cdp_url()
        try:
            response = httpx.get(f"{cdp_url}/json/list", timeout=2, trust_env=False)
            response.raise_for_status()
            return [t for t in response.json() if isinstance(t, dict) and t.get("type") == "page"]
        except (httpx.HTTPError, ValueError):
            return []

    def _recommend_page(self, context):
        """确定唯一要操作的推荐页：优先用绑定的 recruiting_target_id，否则要求唯一。

        多窗口/多推荐页/多账号时不再「取第一个」，避免连错页面；目标失效或出现
        多个可选页面就停止，不自动换另一页。
        """
        pages = [p for p in context.pages if p.url and p.url.startswith(RECOMMEND_URL_PREFIX)]
        target_id = self._bound_target_id()
        if target_id:
            target = next((t for t in self._cdp_page_targets() if t.get("id") == target_id), None)
            if not target or not str(target.get("url", "")).startswith(RECOMMEND_URL_PREFIX):
                raise BrowserError("绑定的招聘标签页不是推荐页或已关闭，停止操作；不会切换到其他标签页")
            page = next((p for p in context.pages if p.url == target["url"]), None)
            if not page:
                raise BrowserError("绑定的推荐页当前不可用，请刷新后重试；不会切换到其他标签页")
            return page
        if len(pages) == 1:
            return pages[0]
        if not pages:
            raise BrowserError("请先在 Chrome 打开 BOSS 牛人推荐页（/web/chat/recommend）")
        raise BrowserError("发现多个推荐页，无法唯一确定；请关闭多余页面或配置 browser.recruiting_target_id")

    @staticmethod
    def _recommend_frame(page):
        for frame in page.frames:
            if frame.url and RECOMMEND_FRAME_PATH in frame.url:
                return frame
        return None

    @staticmethod
    def _job_id_from_frame(frame):
        # 选中岗位在推荐页 iframe 顶部的职位下拉框里（li.job-item.curr 的 value 属性），
        # 不在 iframe URL 的 jobid 参数里（该参数加载后为 null）。
        value = frame.evaluate(READ_JOB_ID)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def _wait_greet_text(self, frame, uid):
        """等待按钮文案明确变为「继续沟通」，才视为发送成功。"""
        deadline = time.time() + self._wait_timeout
        while time.time() < deadline:
            text = frame.evaluate(READ_GREET_TEXT.replace("__UID__", json.dumps(uid)))
            if isinstance(text, str) and "继续沟通" in text:
                return text
            time.sleep(0.5)
        return frame.evaluate(READ_GREET_TEXT.replace("__UID__", json.dumps(uid)))

    def greet(self, uid, expected_job_id=None):
        """对已通过跨刷新验证的候选人点「打招呼」，BOSS 自动发默认招呼语。

        返回 {sent, job_id, reason}。先读 jobid（读不到就不点，避免发了却无法记录），
        若传了 expected_job_id 则点击前核对其与页面当前岗位一致（不一致就不点，避免
        误点错误岗位的候选人），再点击，最后确认按钮文案明确变为「继续沟通」。
        """
        cdp_url = self._find_cdp_url()
        pw = None
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0]
            page = self._recommend_page(context)
            frame = self._recommend_frame(page)
            if not frame:
                raise BrowserError("未找到推荐页 iframe，请刷新推荐页后重试")
            job_id = self._job_id_from_frame(frame)
            if not job_id:
                raise BrowserError("推荐页未选择岗位，无法记录招呼；请先在下拉框选择岗位")
            if expected_job_id and str(job_id) != str(expected_job_id):
                raise BrowserError("推荐页当前岗位与任务岗位不一致，已停止招呼")
            state = frame.evaluate(CLICK_GREET.replace("__UID__", json.dumps(uid)))
            if state == "missing":
                raise BrowserError("未找到该候选人的卡片，页面可能已刷新，请重新验证")
            if state == "already":
                return {"sent": True, "job_id": job_id, "reason": "该候选人已打过招呼（按钮已不是「打招呼」）"}
            text = self._wait_greet_text(frame, uid)
            sent = isinstance(text, str) and "继续沟通" in text
            return {"sent": sent, "job_id": job_id,
                    "reason": "已发出招呼，按钮变为「继续沟通」" if sent else "点击后未确认按钮变化，结果待核实"}
        finally:
            if pw is not None:
                pw.stop()

    def select_job(self, job_id):
        """在推荐页下拉框选中指定岗位；切换岗位会刷新候选人列表。"""
        cdp_url = self._find_cdp_url()
        pw = None
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0]
            page = self._recommend_page(context)
            frame = self._recommend_frame(page)
            if not frame:
                raise BrowserError("未找到推荐页 iframe，请刷新推荐页后重试")
            result = frame.evaluate(SELECT_JOB.replace("__JOB_ID__", json.dumps(job_id)))
            if result != "selected":
                raise BrowserError("未能在下拉框选中该岗位，请确认该岗位仍处于开放状态")
            time.sleep(2)  # 切换岗位后等候选人列表刷新
            return True
        finally:
            if pw is not None:
                pw.stop()

    def read_candidates(self):
        """读取推荐页当前展示的全部候选人（name + uid + 是否可招呼）。"""
        cdp_url = self._find_cdp_url()
        pw = None
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0]
            page = self._recommend_page(context)
            frame = self._recommend_frame(page)
            if not frame:
                raise BrowserError("未找到推荐页 iframe，请刷新推荐页后重试")
            value = frame.evaluate(READ_CANDIDATES)
            if not isinstance(value, list):
                return []
            return [{"name": str(c.get("name") or "").strip(), "uid": str(c.get("uid") or "").strip(),
                     "greetable": bool(c.get("greetable"))} for c in value if isinstance(c, dict) and c.get("uid")]
        finally:
            if pw is not None:
                pw.stop()
