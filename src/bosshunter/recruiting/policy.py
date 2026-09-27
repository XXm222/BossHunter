"""Conservative restrictions for the user's one-sample pilot."""
import re


def check_reply(text):
    if not text.strip():
        raise ValueError("回复不能为空")
    # During this pilot, scheduling discussion stays local too. This intentionally
    # catches more than an invitation; it is not a general semantic classifier.
    if re.search(r"面试|邀约|约面|视频聊|电话聊|来公司|到公司|interview|meeting|meet\s+with", text, re.I):
        raise PermissionError("本次测试禁止通过普通回复发送面试／约见内容；请仅保存邀约草稿")
