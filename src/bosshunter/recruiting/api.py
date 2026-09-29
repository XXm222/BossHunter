"""Recruiting routes. Registered inside the existing local-only Bottle server."""
import json
from pathlib import Path
from threading import Lock

from bottle import request

from bosshunter.ai.credentials import AIRequestError
from .browser import BrowserError
from .service import RecruitingService


def register(app, data_dir, config, respond):
    services = {}
    service_lock = Lock()

    def service():
        path = Path(data_dir()).resolve() / "recruiting.db"
        with service_lock:
            if path not in services:
                services[path] = RecruitingService(path, config)
            return services[path]

    @app.get("/api/recruiting/state")
    def state():
        return respond(service().state())

    @app.post("/api/recruiting/<operation:path>")
    def action(operation):
        if request.content_type.split(";")[0] != "application/json":
            return respond({"error": "只接受 JSON 请求"}, 415)
        if request.content_length > 250000:
            return respond({"error": "请求过大"}, 413)
        payload = request.json
        if not isinstance(payload, dict):
            return respond({"error": "请求必须是 JSON 对象"}, 400)
        # Forbidden even before service creation or any browser operation.
        if operation in {"invitations/send", "invitation/send"}:
            return respond({"error": "本次测试禁止发送面试邀约", "code": "invitation_send_disabled"}, 403)
        try:
            s = service()
            actions = {
                "connect": lambda: s.connect(),
                "jobs/sync": lambda: s.sync_jobs(),
                "jobs/select": lambda: s.jobs.select(payload.get("ids")),
                "jobs/budget": lambda: s.jobs.save_budget(payload.get("mode"), payload.get("limit")),
                "import-current": lambda: s.import_current(),
                # 绑定向导：先预览核实身份（不写入），再确认后写入 pilot_conversation
                "contacts/list": lambda: s.list_contacts(),
                "quota/read": lambda: s.read_greeting_quota(),
                "binding/preview": lambda: s.preview_binding(payload["conversation_id"], payload["name"], payload["position_title"]),
                "binding/confirm": lambda: s.confirm_binding(payload["conversation_id"], payload["name"], payload["position_title"], payload["expected_account"]),
                "sync": lambda: s.sync(payload.get("conversation_id")),
                "discover": lambda: s.browser.discover(),
                "discover/verify": lambda: s.verify_discover(),
                "discover/greet": lambda: s.greet_discovered(payload["uid"]),
                "discover/run": lambda: s.start_discovery(),
                "discover/stop": lambda: s.stop_discovery(),
                "position": lambda: s.position(payload),
                "position/read": lambda: s.read_position(payload["id"]),
                "company": lambda: s.save_company(payload.get("text")),
                "reply/context": lambda: s.reply_context(payload["conversation_id"]),
                "resume/read": lambda: s.resume(payload["conversation_id"]),
                "resume/save": lambda: s.save_resume(payload),
                "assess": lambda: s.assess(payload["conversation_id"]),
                "reply/draft": lambda: s.prepare_reply(payload["conversation_id"], str(payload.get("text", "")), str(payload.get("question", ""))),
                "action/draft": lambda: s.prepare_action(payload["conversation_id"], payload["kind"]),
                "outbox/execute": lambda: s.execute(payload["id"]),
                "outbox/cancel": lambda: s.store.finish(payload["id"], "cancelled", "本地用户取消草稿") if s.store.row("outbox", payload["id"])["status"] == "draft" else (_ for _ in ()).throw(ValueError("只能取消未执行草稿")),
                "invitations/draft": lambda: s.invitation(payload),
                "conversation/select": lambda: s.store.select_conversation(payload["conversation_id"]),
                "conversation/control": lambda: s.control(payload["conversation_id"], payload["taken_over"], payload["do_not_contact"]),
                "monitor/once": lambda: s.monitor_once(),
                "monitor/start": lambda: s.start_monitor(),
                "monitor/stop": lambda: s.stop_monitor(),
            }
            if operation not in actions:
                return respond({"error": "未知招聘操作"}, 404)
            # Serialize browser use and local policy mutations; a stop can interrupt waits.
            if operation == "monitor/stop":
                result = actions[operation]()
            else:
                with s.lock:
                    result = actions[operation]()
            return respond({"ok": True, "result": result})
        except PermissionError as exc:
            return respond({"error": str(exc), "code": "action_disabled"}, 403)
        except (KeyError, TypeError, ValueError) as exc:
            return respond({"error": str(exc) if not isinstance(exc, KeyError) else "缺少必需字段"}, 400)
        except BrowserError as exc:
            return respond({"error": str(exc), "code": "browser_unavailable"}, 409)
        except AIRequestError as exc:
            return respond({"error": exc.user_message, "code": exc.kind}, 502)
        except Exception:
            # Avoid exposing candidate text, provider response or DB paths in errors.
            return respond({"error": "招聘操作未完成，请检查运行记录；不自动重试外发动作"}, 500)
