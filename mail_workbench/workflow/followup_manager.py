# -*- coding: utf-8 -*-
"""FollowUpManager —— 发出重要邮件后的跟进（规范 §17）。

**禁止为每封邮件创建 WorkBuddy automation**：
所有 follow-up 共用一个本地 scheduler，到期统一提醒。
"""
from __future__ import annotations

from .. import util
from ..constants import TRIGGER_FOLLOWUP, WF_WAITING_FOR_OTHER


class FollowUpManager:
    def __init__(self, repo, cfg: dict, log=None):
        self.repo = repo
        self.cfg = cfg
        self.log = log

    # ------------------------------------------------------------------
    def schedule(self, thread_id: str, message_id: str = None,
                 days: float = None, sent_message_id: str = "", note: str = "") -> dict:
        if not thread_id:
            raise ValueError("followup 需要 thread_id")
        days = float(days if days is not None else self.cfg.get("followup_default_days", 3))
        if days <= 0:
            raise ValueError("跟进天数必须为正")
        fu = self.repo.create_followup({
            "thread_id": thread_id,
            "message_id": message_id,
            "sent_message_id": sent_message_id,
            "due_at": util.add_days(util.now_iso(), days),
            "note": note or "发送后 %.0f 天未见回复则提醒跟进" % days,
        })
        self.repo.set_thread_workflow(thread_id, WF_WAITING_FOR_OTHER)
        self.repo.inc_metric("followups_scheduled")
        return fu

    def list_open(self, limit: int = 200) -> list:
        return self.repo.list_followups(status="scheduled", limit=limit)

    def list_due(self, limit: int = 200) -> list:
        return self.repo.list_followups(status="due", limit=limit)

    def cancel(self, followup_id: str, status: str = "cancelled") -> bool:
        fu = self.repo.db.query_one(
            "SELECT * FROM followups WHERE followup_id = ?", (followup_id,))
        if not fu:
            return False
        self.repo.mark_followup(followup_id, status)
        return True

    def close_on_reply(self, thread_id: str) -> int:
        """对方回信后自动关闭该线程的跟进（避免多余提醒）。"""
        n = self.repo.close_followups_for_thread(thread_id, status="replied")
        if n:
            self.repo.inc_metric("followups_closed_by_reply", n)
        return n

    # ------------------------------------------------------------------
    def mark_due(self, now_iso: str = None, on_event=None) -> list:
        """到期 -> 状态 scheduled→due，并在 UI 的「待我处理」桶里显示提醒。"""
        due = self.repo.due_followups(now_iso)
        out = []
        for fu in due:
            tid = fu.get("thread_id")
            # 若对方已经回信，直接关掉，不打扰用户
            if tid:
                thread = self.repo.get_thread(tid)
                if thread and thread.get("waiting_for_me"):
                    self.repo.mark_followup(fu["followup_id"], "replied")
                    self.repo.inc_metric("followups_closed_by_reply")
                    continue
            self.repo.mark_followup(fu["followup_id"], "due")
            out.append(fu)
            self.repo.inc_metric("followups_due")
            if on_event:
                try:
                    on_event({"type": TRIGGER_FOLLOWUP, "followup": fu,
                              "thread_id": tid,
                              "note": fu.get("note") or "该线程已发出多日仍未回复"})
                except Exception:
                    pass
        return out

    def snapshot(self) -> dict:
        open_n = len(self.repo.list_followups(status="scheduled", limit=1000))
        due_n = len(self.repo.list_followups(status="due", limit=1000))
        return {"scheduled": open_n, "due": due_n}


def schedule(repo, cfg, **kw) -> dict:
    return FollowUpManager(repo, cfg).schedule(**kw)
