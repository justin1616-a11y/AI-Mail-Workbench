# -*- coding: utf-8 -*-
"""SnoozeManager —— 延后处理（规范 §16）。

**不使用 WorkBuddy automation**：写入本地数据库的 `wake_at`，
由本地 scheduler 到期恢复（`workflow/scheduler.py`）。
"""
from __future__ import annotations

import datetime

from .. import util
from ..constants import WF_SNOOZED, WF_WAITING_FOR_ME, TRIGGER_SNOOZE_WAKE

PRESETS = {
    "later_today": "今天稍后",
    "tomorrow": "明天",
    "next_week": "下周",
    "custom": "自定义时间",
}


def parse_when(spec: str, cfg: dict = None) -> str:
    """把预设或自定义写法解析成 ISO 时间。

    支持：later_today / tomorrow / next_week / +3h / +2d /
          2026-09-25 / 2026-09-25T14:30 / 14:30（今天该时刻）
    """
    cfg = cfg or {}
    hour = int(cfg.get("snooze_default_hour", 9))
    now = util.now()
    s = (spec or "").strip().lower()

    if s in ("later_today", "later", "今天稍后"):
        t = now.replace(hour=18, minute=0, second=0, microsecond=0)
        if t <= now:
            t = now + datetime.timedelta(hours=3)
        return t.replace(microsecond=0).isoformat()

    if s in ("tomorrow", "明天", "明"):
        t = (now + datetime.timedelta(days=1)).replace(hour=hour, minute=0, second=0, microsecond=0)
        return t.isoformat()

    if s in ("next_week", "下週", "下周"):
        days = 7 - now.weekday()  # 下周一
        t = (now + datetime.timedelta(days=days)).replace(
            hour=hour, minute=0, second=0, microsecond=0)
        return t.isoformat()

    if s.startswith("+"):
        m = s[1:]
        try:
            if m.endswith("h"):
                return util.add_seconds(util.now_iso(), float(m[:-1]) * 3600)
            if m.endswith("d"):
                return util.add_seconds(util.now_iso(), float(m[:-1]) * 86400)
            if m.endswith("m"):
                return util.add_seconds(util.now_iso(), float(m[:-1]) * 60)
        except ValueError:
            pass

    if len(s) == 5 and s[2] == ":":
        try:
            hh, mm = int(s[:2]), int(s[3:])
            t = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
            if t <= now:
                t += datetime.timedelta(days=1)
            return t.isoformat()
        except ValueError:
            pass

    dt = util.parse_iso(spec)
    if dt:
        return dt.replace(microsecond=0).isoformat()
    # 兜底：明天同一时刻
    return (now + datetime.timedelta(days=1)).replace(microsecond=0).isoformat()


class SnoozeManager:
    def __init__(self, repo, cfg: dict, log=None):
        self.repo = repo
        self.cfg = cfg
        self.log = log

    def snooze(self, message_id: str = None, thread_id: str = None,
               when: str = "tomorrow", note: str = "") -> dict:
        if not message_id and not thread_id:
            raise ValueError("snooze 需要 message_id 或 thread_id")
        msg = self.repo.get_message(message_id) if message_id else None
        if not thread_id and msg:
            thread_id = msg.get("thread_id")
        wake_at = parse_when(when, self.cfg)
        # 同一目标只保留一个活跃 snooze
        self.repo.cancel_snoozes_for(message_id=message_id, thread_id=thread_id)
        s = self.repo.create_snooze({
            "message_id": message_id, "thread_id": thread_id,
            "wake_at": wake_at, "note": note,
        })
        if message_id:
            self.repo.set_workflow_state(message_id, WF_SNOOZED, snooze_until=wake_at)
        if thread_id:
            self.repo.set_thread_workflow(thread_id, WF_SNOOZED)
        self.repo.inc_metric("snoozes_created")
        return s

    def cancel(self, message_id: str = None, thread_id: str = None) -> int:
        n = self.repo.cancel_snoozes_for(message_id=message_id, thread_id=thread_id)
        if message_id:
            self.repo.set_workflow_state(message_id, WF_WAITING_FOR_ME)
        if thread_id:
            self.repo.set_thread_workflow(thread_id, WF_WAITING_FOR_ME)
        return n

    def list_active(self, limit: int = 200) -> list:
        return self.repo.active_snoozes(limit=limit)

    def wake_due(self, now_iso: str = None, on_event=None) -> list:
        """到期唤醒：恢复为「等我处理」并通知 UI。"""
        due = self.repo.due_snoozes(now_iso)
        woken = []
        for s in due:
            self.repo.mark_snooze_woken(s["snooze_id"])
            if s.get("message_id"):
                self.repo.set_workflow_state(s["message_id"], WF_WAITING_FOR_ME,
                                             snooze_until=None)
            if s.get("thread_id"):
                self.repo.set_thread_workflow(s["thread_id"], WF_WAITING_FOR_ME)
            woken.append(s)
            self.repo.inc_metric("snoozes_woken")
            if on_event:
                try:
                    on_event({"type": TRIGGER_SNOOZE_WAKE, "snooze": s})
                except Exception:
                    pass
        return woken


def snooze(repo, cfg, **kw) -> dict:
    return SnoozeManager(repo, cfg).snooze(**kw)
