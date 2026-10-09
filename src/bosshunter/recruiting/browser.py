"""Employer DOM adapter using the project's built-in Browser Runtime.

No raw browser/JS endpoint is exposed by the recruiting API. Every write is an
explicit action with a current conversation check. Invitation submission does
not exist in this adapter.
"""
import json
import re
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, parse_qs
from bosshunter.browser.client import RuntimeClient
from bosshunter.browser.runtime import ensure_runtime
from .policy import check_reply


class BrowserError(RuntimeError):
    pass


class TaskCancelled(BrowserError):
    """The current task was stopped before its next operation."""


class ConversationNotSelected(BrowserError):
    """Sending requires the user to open the bound conversation."""


class AccountPauseError(BrowserError):
    """账号需要人工处理（验证码、登录失效、身份不一致），应暂停自动任务而非自动重试。"""
    pass


def platform_security_url(url):
    """Known BOSS verification/restriction destinations; not a ban-code decoder."""
    parsed = urlsplit(str(url))
    host = parsed.hostname or ''
    if not (host == 'zhipin.com' or host.endswith('.zhipin.com')):
        return False
    return (parsed.path in {'/web/passport/zp/verify.html', '/web/passport/zp/403.html'}
            or (parsed.path in {'', '/'} and '_security_check' in parse_qs(parsed.query, keep_blank_values=True)))


def refusal_cooldown(retry_after):
    """At least 30 minutes; honor a longer Retry-After in either HTTP format."""
    try:
        extra = int(retry_after) if retry_after.isdigit() else (parsedate_to_datetime(retry_after) - datetime.now(timezone.utc)).total_seconds()
        return max(1800, extra)
    except (TypeError, ValueError, OverflowError):
        return 1800


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
SCROLL_CONTACT_LIST = READ_CONTACT_LIST + r"""
const items = document.querySelectorAll('.geek-item');
if (items.length) { items[items.length - 1].scrollIntoView(); }
return JSON.stringify({count: items.length, contacts});
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

    def verify_account(self, ident, expected_account, on_refusal=None):
        match = re.fullmatch(r'([1-9][0-9]*)-([01])', ident)
        if not match or not expected_account:
            raise AccountPauseError('发送前缺少已核实的招聘账号身份，请先绑定并同步会话')
        gid, source = match.groups()
        args = json.dumps({'gid': gid, 'src': source, 'account': str(expected_account)})
        value = self.evaluate('return (async()=>{try{const expected=' + args + r""";
            if(location.pathname!=='/web/chat/index' || document.querySelector('.geek-item.selected')?.getAttribute('data-id')!==expected.gid+'-'+expected.src)
                return JSON.stringify({account_verified:false});
            const params=new URLSearchParams({gid:expected.gid,src:expected.src,maxMsgId:'0',c:'1',page:'1'});
            const response=await fetch('/wapi/zpchat/boss/historyMsg?'+params,{credentials:'same-origin',redirect:'error'});
            if(!response.ok) return JSON.stringify({account_verified:false,refusal_status:response.status,retry_after:response.headers.get('Retry-After')||''});
            const body=await response.json();
            const messages=body?.zpData?.messages;
            if(body.code!==0 || !Array.isArray(messages) || !messages.length) return JSON.stringify({account_verified:false});
            const valid=messages.every(m=>{
                const incoming=String(m.from?.uid)===expected.gid, outgoing=String(m.to?.uid)===expected.gid;
                const peer=incoming?m.from:m.to, account=incoming?m.to:m.from;
                return incoming!==outgoing && String(peer?.source)===expected.src && String(account?.uid)===expected.account;
            });
            return JSON.stringify({account_verified:valid});
        }catch(e){return JSON.stringify({account_verified:false});}})();
        """)
        if value.get('refusal_status') in {403, 429} and on_refusal:
            on_refusal(refusal_cooldown(value.get('retry_after', '')))
        if value.get('account_verified') is not True:
            raise AccountPauseError('浏览器当前招聘账号与已绑定账号不一致或无法核实，停止外发')

    def open_conversation(self, ident):
        # Never navigate or switch the user's selected conversation as a side effect.
        previous = self.read_current()
        if previous.get("id") != ident:
            raise ConversationNotSelected("当前页面不是绑定会话，请在 Chrome 手动打开该会话；未切换页面")
        time.sleep(.35)
        snapshot = self.read_current()
        if snapshot != previous:
            raise BrowserError("当前会话仍在变化，请核对后重试；未执行发送")
        return snapshot

    def read_contact_list(self, load_all=False, before_load=None):
        """读取聊天页左侧联系人列表（含岗位名）；只读，不点选、不导航、不发消息。

        load_all=True 时先反复滚动到底触发懒加载，直到没有新增联系人，再一次性读取全部；
        联系人列表是滚动加载的，只读当前 DOM 会漏掉未加载的人。
        """
        collected = {}
        reason = 'visible_dom'
        if load_all:
            last = -1
            stable = 0
            for step in range(200):
                if before_load:
                    before_load(step == 0)
                value = self.evaluate(SCROLL_CONTACT_LIST)
                for contact in value.get('contacts', []):
                    if contact.get('ident'):
                        collected[contact['ident']] = contact
                count = len(collected) if collected else value.get('count', 0)
                if count == last:
                    stable += 1
                    if stable >= 3:
                        reason = 'loaded_list_stable'
                        break
                else:
                    stable = 0
                    last = count
                time.sleep(0.8)
            else:
                reason = 'scroll_limit'
        value = self.evaluate(READ_CONTACT_LIST + "return JSON.stringify({contacts});")
        for contact in value.get('contacts', []):
            if contact.get('ident'):
                collected[contact['ident']] = contact
        self.contact_coverage = {'complete': False, 'scope': 'loaded_browser_contacts',
                                 'reason': reason, 'loaded': len(collected),
                                 'note': '仅包含当前页面可加载的联系人，未证明覆盖全账号历史联系人'}
        return list(collected.values())

    def read_position(self, expected_title, expected_job_id):
        return self.evaluate(r"""
            if(location.hostname!=='www.zhipin.com'||location.pathname!=='/web/chat/job/edit') throw Error('请在 Chrome 职位管理中打开目标岗位；只读取，不保存平台表单');
            const f=[...document.querySelectorAll('iframe')].find(e=>new URL(e.src,location.href).pathname==='/web/frame/job/edit');
            const d=f?.contentDocument;
            const title=d?.querySelector('input[placeholder="请填写职位名称，如“销售专员”"]')?.value.trim();
            const jd=d?.querySelector('textarea')?.value.trim();
            const urls=[new URL(location.href),new URL(f?.src || location.href,location.href)];
            const ids=urls.flatMap(u=>['encryptJobId','jobId','jobid','id'].map(k=>u.searchParams.get(k))).filter(Boolean);
            if(!ids.includes(EXPECTED_ID)) throw Error('当前岗位平台 ID 未能核实，停止读取 JD');
            if(title!==EXPECTED || !jd) throw Error('当前平台岗位与绑定会话不一致，已停止读取');
            return JSON.stringify({title,jd,source:'boss_job_form'});
        """.replace('EXPECTED_ID', json.dumps(expected_job_id)).replace("EXPECTED", json.dumps(expected_title, ensure_ascii=False)))

    def execute(self, kind, before, content="", preflight=None):
        if preflight:
            preflight()
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
            if preflight:
                preflight()
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
            if preflight:
                preflight()
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
