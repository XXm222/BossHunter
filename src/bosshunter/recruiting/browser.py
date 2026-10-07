"""Employer DOM adapter using the project's built-in Browser Runtime.

No raw browser/JS endpoint is exposed by the recruiting API. Every write is an
explicit action with a current conversation check. Invitation submission does
not exist in this adapter.
"""
import json
import time
from urllib.parse import urlsplit
from bosshunter.browser.client import RuntimeClient
from bosshunter.browser.runtime import ensure_runtime
from .policy import check_reply


class BrowserError(RuntimeError):
    pass


class AccountPauseError(BrowserError):
    """账号需要人工处理（验证码、登录失效、身份不一致），应暂停自动任务而非自动重试。"""
    pass


READ_CHAT = r"""
const selected = document.querySelector('.geek-item.selected');
const editor = document.querySelector('#boss-chat-editor-input');
if(location.hostname !== 'www.zhipin.com' || location.pathname !== '/web/chat/index')
    throw Error('请先打开 BOSS 招聘端沟通页面');
if(!selected || !editor) throw Error('请在招聘端选择一个会话');
const messages = [...document.querySelectorAll('.message-item')].map(e => {
    const text = e.querySelector('.text-content');
    const card = e.querySelector('.message-card-wrap');
    return {direction:e.querySelector('.item-myself')?'out':e.querySelector('.item-friend')?'in':'system',
        kind:text?'text':card?'card':'system',
        text:(text?.innerText || card?.innerText || e.innerText).trim(),
        time:e.querySelector('.message-time')?.innerText.trim() || ''};
}).filter(e=>e.text);
const snapshot = {
    id:selected.getAttribute('data-id'), name:selected.querySelector('.geek-name')?.innerText.trim() || '',
    position_title:document.querySelector('.job-content .position-name')?.innerText.trim() || '',
    messages, editor_empty:!editor.innerText.trim(),
    coverage:'当前会话已加载消息；列表只展示近30天联系人',
    stable_message_ids:false
};
if(!snapshot.id || !snapshot.position_title) throw Error('无法核实会话或关联岗位');
"""


READ_CONTACT_LIST = r"""
const contacts = [...document.querySelectorAll('.geek-item')].map(item => {
    const ident = item.getAttribute('data-id') || '';
    const name = (item.querySelector('.geek-name')?.innerText || item.querySelector('.geek-name')?.getAttribute('title') || '').trim();
    const position_title = (item.querySelector('.source-job')?.innerText || item.querySelector('.source-job')?.getAttribute('title') || '').trim();
    return {ident, name, position_title};
}).filter(c => c.ident && c.name);
"""


# 把左侧联系人列表最后一个条目滚进可视区，触发懒加载下一页；返回当前已加载的条目数。
SCROLL_CONTACT_LIST = r"""
const items = document.querySelectorAll('.geek-item');
if (items.length) { items[items.length - 1].scrollIntoView(); }
return JSON.stringify({count: items.length});
"""


class BossBrowser:
    def __init__(self, runtime=None, target_id=None):
        self.runtime = runtime or RuntimeClient()
        self.target_id = target_id

    @staticmethod
    def recruiter_target(target):
        if not isinstance(target, dict) or target.get("type") != "page" or not target.get("targetId"):
            return False
        try:
            url = urlsplit(target.get("url", ""))
            return (url.scheme == "https" and url.hostname == "www.zhipin.com"
                    and url.port in (None, 443) and not url.username and not url.password
                    and url.path.startswith("/web/chat/"))
        except (TypeError, ValueError):
            return False

    def bound_target(self):
        ensure_runtime()  # 确保原项目 Node Browser Runtime 已启动（发送消息走它）
        health = self.runtime.health()
        if not isinstance(health, dict) or health.get("runtime") != "bosshunter":
            raise BrowserError("BossHunter Browser Runtime 未连接，请检查原项目浏览器服务及 Chrome 调试连接")
        # /targets reconnects the runtime's CDP session without creating or activating tabs.
        targets = [t for t in self.runtime.targets() if self.recruiter_target(t)]
        if self.target_id:
            if not any(t['targetId'] == self.target_id for t in targets):
                raise BrowserError("绑定的招聘标签页已关闭或离开招聘端，停止操作；不会切换到其他标签页")
        elif len(targets) == 1:
            self.target_id = targets[0]['targetId']
        else:
            raise BrowserError("无法唯一确定招聘标签页，请核对已打开的 BOSS 招聘页面；不会自动选择或新建标签页")
        return self.target_id

    def evaluate(self, body):
        target_id = self.bound_target()
        # Guard again inside the selected page in case it navigated after /targets.
        code = "(()=>{try{if(location.origin!=='https://www.zhipin.com'||!location.pathname.startsWith('/web/chat/')) throw Error('绑定页面已离开招聘端');" + body + "}catch(e){return JSON.stringify({adapter_error:String(e.message)});}})()"
        value = self.runtime.evaluate(target_id, code, timeout=25)
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        if isinstance(value, dict) and value.get("adapter_error"):
            raise BrowserError(str(value["adapter_error"])[:300])
        if not isinstance(value, dict) or not value or value.get("error"):
            raise BrowserError("Browser Runtime 未返回有效页面结果，请核对绑定页面；不会自动重试操作")
        return value

    def status(self):
        return self.evaluate("return JSON.stringify({connected:true,host:location.hostname,path:location.pathname,recruiter:!!document.querySelector('a[href=\"/web/chat/job/list\"]')});")

    def read_current(self):
        return self.evaluate(READ_CHAT + "return JSON.stringify(snapshot);")

    def open_conversation(self, ident):
        # Never navigate or switch the user's selected conversation as a side effect.
        previous = self.read_current()
        if previous.get("id") != ident:
            raise BrowserError("当前页面不是绑定会话，请在 Chrome 手动打开该会话；未切换页面")
        time.sleep(.35)
        snapshot = self.read_current()
        if snapshot != previous:
            raise BrowserError("当前会话仍在变化，请核对后重试；未执行发送")
        return snapshot

    def read_contact_list(self, load_all=False):
        """读取聊天页左侧联系人列表（含岗位名）；只读，不点选、不导航、不发消息。

        load_all=True 时先反复滚动到底触发懒加载，直到没有新增联系人，再一次性读取全部；
        联系人列表是滚动加载的，只读当前 DOM 会漏掉未加载的人。
        """
        if load_all:
            last = -1
            stable = 0
            for _ in range(200):
                value = self.evaluate(SCROLL_CONTACT_LIST)
                count = value.get('count', 0) if isinstance(value, dict) else 0
                if count == last:
                    stable += 1
                    if stable >= 3:
                        break
                else:
                    stable = 0
                    last = count
                time.sleep(0.8)
        value = self.evaluate(READ_CONTACT_LIST + "return JSON.stringify({contacts});")
        return value.get("contacts") if isinstance(value, dict) else []

    def read_position(self, expected_title):
        return self.evaluate(r"""
            if(location.hostname!=='www.zhipin.com'||location.pathname!=='/web/chat/job/edit') throw Error('请在 Chrome 职位管理中打开目标岗位；只读取，不保存平台表单');
            const f=[...document.querySelectorAll('iframe')].find(e=>new URL(e.src,location.href).pathname==='/web/frame/job/edit');
            const d=f?.contentDocument;
            const title=d?.querySelector('input[placeholder="请填写职位名称，如“销售专员”"]')?.value.trim();
            const jd=d?.querySelector('textarea')?.value.trim();
            if(title!==EXPECTED || !jd) throw Error('当前平台岗位与绑定会话不一致，已停止读取');
            return JSON.stringify({title,jd,source:'boss_job_form'});
        """.replace("EXPECTED", json.dumps(expected_title, ensure_ascii=False)))

    def execute(self, kind, before, content=""):
        if kind == "invitation":
            raise BrowserError("本次测试禁止发送面试邀约")
        if kind not in {"reply", "request_resume", "accept_resume"}:
            raise BrowserError("动作不在执行白名单内")
        if kind == "reply":
            check_reply(content)
        expected = json.dumps({k: before[k] for k in ("id", "position_title", "messages")}, ensure_ascii=False)
        guard = READ_CHAT + """
            const expected=EXPECTED;
            if(snapshot.id!==expected.id || snapshot.position_title!==expected.position_title || JSON.stringify(snapshot.messages)!==JSON.stringify(expected.messages)) throw Error('会话或消息已变化，已停止发送');
            if(!snapshot.editor_empty) throw Error('编辑器中有人工输入，已停止自动操作');
            const visible=e=>!!e && e.getClientRects().length>0 && getComputedStyle(e).visibility!=='hidden';
            if([...document.querySelectorAll('.dialog-wrap.active')].some(visible)) throw Error('请先关闭当前弹窗');
        """.replace("EXPECTED", expected)
        if kind == "reply":
            self.evaluate(guard + """
                const send=document.querySelector('.submit-content .submit');
                if(!visible(send)) throw Error('找不到可见发送控件');
                editor.focus(); editor.textContent=CONTENT;
                editor.dispatchEvent(new InputEvent('input',{bubbles:true,inputType:'insertText',data:CONTENT}));
                return JSON.stringify({prepared:true});
            """.replace("CONTENT", json.dumps(content, ensure_ascii=False)))
            # Separate browser turns allow the framework to update send-button state.
            # Revalidate context and our exact text, allowing no intervening human edit.
            typed_guard = guard.replace(
                "if(!snapshot.editor_empty) throw Error('编辑器中有人工输入，已停止自动操作');",
                "if(editor.innerText!==" + json.dumps(content, ensure_ascii=False) + ") throw Error('回复草稿已被修改，停止发送');",
            )
            return self.evaluate(typed_guard + """
                const send=document.querySelector('.submit-content .submit.active');
                if(!visible(send)) throw Error('发送按钮尚未就绪；保留待核实状态');
                send.click();
                return JSON.stringify({attempted:true});
            """)
        if kind == "request_resume":
            self.evaluate(guard + """
                if(snapshot.messages.some(m=>/简历请求已发送|索取.*简历/.test(m.text))) throw Error('已有简历请求记录，请人工核对，避免重复索取');
                if([...document.querySelectorAll('span.card-btn')].some(e=>e.innerText.includes('点击预览附件简历'))) throw Error('当前已收到附件，无需再次索取');
                const b=[...document.querySelectorAll('span.operate-btn')].find(e=>e.innerText.trim()==='求简历');
                if(!visible(b)) throw Error('找不到求简历入口');
                b.click(); return JSON.stringify({opened:true});
            """)
            return self.evaluate(guard + """
                const dialog=[...document.querySelectorAll('.exchange-tooltip')].find(e=>visible(e)&&e.innerText.includes('确定向牛人索取简历吗'));
                const b=dialog?.querySelector('.boss-btn-primary');
                if(!visible(b)) throw Error('求简历确认框不匹配');
                b.click(); return JSON.stringify({attempted:true});
            """)
        return self.evaluate(guard + """
            const cards=[...document.querySelectorAll('.message-card-wrap')].filter(e=>e.innerText.includes('对方想发送附件简历给您'));
            const b=cards.flatMap(e=>[...e.querySelectorAll('.card-btn')]).find(e=>e.innerText.trim()==='同意'&&!e.classList.contains('disabled'));
            if(!visible(b)) throw Error('没有可接收的新简历申请卡');
            b.click(); return JSON.stringify({attempted:true});
        """)
