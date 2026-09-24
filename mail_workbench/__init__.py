# -*- coding: utf-8 -*-
"""Mail Workbench —— Human-in-the-loop AI Email Workbench.

分层（自下而上）：
    storage        SQLite（WAL，versioned migration）
    mail           IMAP / SMTP / IMAP IDLE 同步 / Foxmail 历史索引
    thread         线程聚合 + MailContextBuilder
    intelligence   规则引擎（确定性）+ Reply Planner（AI 侧契约）
    workflow       Candidate / DraftJob 状态机 / Snooze / FollowUp / 本地调度
    draft          DraftJobQueue / WorkBuddy Worker Contract / Recovery
    ui + server    本地 Web Workbench

设计红线（不可违反）：
    P0-1 事件驱动 —— 不靠 automation 轮询邮件（IMAP IDLE 为主，NOOP 降级）
    P0-2 Local First —— 确定性工作全部本地完成，不浪费 LLM 调用
    P0-3 AI On Demand —— 只有真正需要语言理解时才交给 WorkBuddy
    P0-4 Human In The Loop —— AI 永不发送邮件，发送需人类显式确认
"""

__version__ = "2.0.0"
__all__ = ["__version__"]
