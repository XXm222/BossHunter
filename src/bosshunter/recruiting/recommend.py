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
# 锁定「有打招呼按钮的才是候选卡」。任一字段读取不到就判验证失败（fail-closed）。
READ_FIRST_CANDIDATE = r"""
() => {
    const btn = [...document.querySelectorAll('.btn-greet')].find(e => e.getClientRects().length > 0);
    if (!btn) return null;
    const card = btn.closest('li.card-item');
    if (!card) return null;
    const inner = card.querySelector('.card-inner');
    const name = (card.querySelector('.name')?.innerText || card.querySelector('.avatar')?.getAttribute('alt') || '').trim();
    const uid = (inner?.getAttribute('data-geekid') || inner?.getAttribute('data-geek') || '').trim();
    return { name, uid };
}
"""

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

    @staticmethod
    def _recommend_page(context):
        for page in context.pages:
            if page.url and page.url.startswith(RECOMMEND_URL_PREFIX):
                return page
        raise BrowserError("请先在 Chrome 打开 BOSS 牛人推荐页（/web/chat/recommend）")

    @staticmethod
    def _recommend_frame(page):
        for frame in page.frames:
            if frame.url and RECOMMEND_FRAME_PATH in frame.url:
                return frame
        return None

    @classmethod
    def _read_first_candidate(cls, page):
        frame = cls._recommend_frame(page)
        if not frame:
            return None
        value = frame.evaluate(READ_FIRST_CANDIDATE)
        if not isinstance(value, dict):
            return None
        return {"name": str(value.get("name") or "").strip(), "uid": str(value.get("uid") or "").strip()}

    def _wait_for_candidate(self, page):
        deadline = time.time() + self._wait_timeout
        while time.time() < deadline:
            candidate = self._read_first_candidate(page)
            if candidate and candidate["name"] and candidate["uid"]:
                return candidate
            time.sleep(0.5)
        return None

    def verify(self):
        cdp_url = self._find_cdp_url()
        pw = None
        try:
            pw = sync_playwright().start()
            browser = pw.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0]
            page = self._recommend_page(context)
            page.reload(wait_until="domcontentloaded")
            candidate = self._wait_for_candidate(page)
            if not candidate:
                return {"verified": False, "name": None, "uid": None,
                        "reason": "刷新后未能读取到完整候选人身份（姓名或稳定 ID 缺失），不会自动开聊"}
            return {"verified": True, "name": candidate["name"], "uid": candidate["uid"],
                    "reason": "已通过刷新拿到当前推荐候选人并读取到完整身份"}
        finally:
            if pw is not None:
                pw.stop()

    @staticmethod
    def _job_id_from_frame(frame):
        # 选中岗位在推荐页 iframe 顶部的职位下拉框里（li.job-item.curr 的 value 属性），
        # 不在 iframe URL 的 jobid 参数里（该参数加载后为 null）。
        value = frame.evaluate(READ_JOB_ID)
        if isinstance(value, str) and value.strip():
            return value.strip()
        return None

    def _wait_greet_text(self, frame, uid):
        deadline = time.time() + self._wait_timeout
        while time.time() < deadline:
            text = frame.evaluate(READ_GREET_TEXT.replace("__UID__", json.dumps(uid)))
            if isinstance(text, str) and bool(text) and "打招呼" not in text:
                return text
            time.sleep(0.5)
        return frame.evaluate(READ_GREET_TEXT.replace("__UID__", json.dumps(uid)))

    def greet(self, uid):
        """对已通过跨刷新验证的候选人点「打招呼」，BOSS 自动发默认招呼语。

        返回 {sent, job_id, reason}。先读 jobid（读不到就不点，避免发了却无法记录），
        再定位该 geekid 的卡片点击按钮，最后确认按钮文案不再是「打招呼」。
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
            state = frame.evaluate(CLICK_GREET.replace("__UID__", json.dumps(uid)))
            if state == "missing":
                raise BrowserError("未找到该候选人的卡片，页面可能已刷新，请重新验证")
            if state == "already":
                return {"sent": True, "job_id": job_id, "reason": "该候选人已打过招呼（按钮已不是「打招呼」）"}
            text = self._wait_greet_text(frame, uid)
            sent = isinstance(text, str) and bool(text) and "打招呼" not in text
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
