# -*- coding: utf-8 -*-
"""DraftJobQueue 正式状态机（规范 §5）。

    candidate ──► queued ──► generating ──► ready ──► reviewing ──► approved ──► sent
        │           │           │  ▲          │           │            │
        │           │           │  └──────────┘           │            │
        │           │           ▼      (需补信息)         │            │
        │           │      needs_input ──► queued         │            │
        │           ▼           │                          │            │
        │        failed ◄───────┴──────────────────────────┘            │
        │           │                                                   │
        │           └──► queued  （指数退避重试）                        │
        ▼                                                               ▼
     dismissed / expired                                            （终态）

设计原则：
  * 非法跳转**一律抛异常**，不做「静默修正」。状态机是安全边界的一部分：
    一旦允许 `generating → sent` 之类的近路，AI 自动发信就会绕开人类确认。
  * 每次转换都写 `job_events` 审计日志（谁、何时、从什么状态到什么状态）。
"""
from __future__ import annotations

from .. import util
from ..constants import (
    JOB_ACTIVE_STATUSES, JOB_APPROVED, JOB_CANDIDATE, JOB_DISMISSED, JOB_EXPIRED,
    JOB_FAILED, JOB_GENERATING, JOB_NEEDS_INPUT, JOB_QUEUED, JOB_READY,
    JOB_REVIEWING, JOB_SENT, JOB_STATUSES, JOB_TERMINAL_STATUSES,
)

TRANSITIONS = {
    JOB_CANDIDATE:   {JOB_QUEUED, JOB_DISMISSED, JOB_EXPIRED},
    JOB_QUEUED:      {JOB_GENERATING, JOB_FAILED, JOB_EXPIRED, JOB_DISMISSED},
    JOB_GENERATING:  {JOB_READY, JOB_FAILED, JOB_NEEDS_INPUT},
    JOB_NEEDS_INPUT: {JOB_QUEUED, JOB_FAILED, JOB_DISMISSED, JOB_EXPIRED},
    JOB_READY:       {JOB_REVIEWING, JOB_EXPIRED, JOB_DISMISSED},
    JOB_REVIEWING:   {JOB_APPROVED, JOB_READY, JOB_DISMISSED},
    JOB_APPROVED:    {JOB_SENT, JOB_FAILED, JOB_READY, JOB_REVIEWING},
    JOB_FAILED:      {JOB_QUEUED, JOB_EXPIRED, JOB_DISMISSED},
    JOB_SENT:        set(),
    JOB_DISMISSED:   set(),
    JOB_EXPIRED:     set(),
}

# 只有 approved 允许出站发送 —— 唯一的发送门禁定义点
SEND_ALLOWED_STATES = frozenset({JOB_APPROVED})

# 这些状态占用幂等键（同一 message 不允许并存的活跃任务）
ACTIVE_STATES = frozenset(JOB_ACTIVE_STATUSES)
TERMINAL_STATES = frozenset(JOB_TERMINAL_STATUSES)

# 「等待 WorkBuddy 处理」的状态
WORKER_PICKABLE = frozenset({JOB_QUEUED})
# 「等待人类」的状态
HUMAN_STATES = frozenset({JOB_NEEDS_INPUT, JOB_READY, JOB_REVIEWING, JOB_APPROVED})


class IllegalTransition(Exception):
    pass


def is_valid_state(state: str) -> bool:
    return state in JOB_STATUSES


def can_transition(from_state: str, to_state: str) -> bool:
    if to_state not in JOB_STATUSES:
        return False
    if from_state == to_state:
        return True  # 幂等重复调用允许
    return to_state in TRANSITIONS.get(from_state, set())


def assert_transition(from_state: str, to_state: str) -> None:
    if not can_transition(from_state, to_state):
        raise IllegalTransition(
            "非法状态跳转：%s -> %s（允许：%s）"
            % (from_state, to_state, sorted(TRANSITIONS.get(from_state, set())) or "无（终态）"))


def is_terminal(state: str) -> bool:
    return state in TERMINAL_STATES


def is_active(state: str) -> bool:
    return state in ACTIVE_STATES


def is_worker_pickable(state: str) -> bool:
    return state in WORKER_PICKABLE


def is_human_actionable(state: str) -> bool:
    return state in HUMAN_STATES


def can_send(state: str) -> bool:
    return state in SEND_ALLOWED_STATES


def transition(repo, job_id: str, to_state: str, actor: str = "system",
               note: str = "", patch: dict = None, allow_same: bool = True) -> dict:
    """执行状态转换（校验 + 落库 + 审计）。返回更新后的 job。"""
    job = repo.get_job(job_id)
    if not job:
        raise IllegalTransition("任务不存在：%s" % job_id)
    frm = job.get("status")
    if not is_valid_state(frm):
        raise IllegalTransition("任务 %s 处于未知状态 %r" % (job_id, frm))

    if frm == to_state:
        if not allow_same:
            raise IllegalTransition("任务 %s 已经是 %s" % (job_id, to_state))
        if patch:
            repo.update_job(job_id, patch)
        return repo.get_job(job_id)

    assert_transition(frm, to_state)
    data = dict(patch or {})
    data["status"] = to_state
    repo.update_job(job_id, data)
    repo.log_job_event(job_id, frm, to_state, actor=actor, note=note)
    return repo.get_job(job_id)


def describe() -> dict:
    """给 UI/文档用的状态机描述。"""
    return {k: sorted(v) for k, v in TRANSITIONS.items()}


def mermaid() -> str:
    lines = ["stateDiagram-v2", "    [*] --> candidate"]
    for src, dsts in TRANSITIONS.items():
        for d in sorted(dsts):
            lines.append("    %s --> %s" % (src, d))
    lines.append("    sent --> [*]")
    lines.append("    dismissed --> [*]")
    lines.append("    expired --> [*]")
    return "\n".join(lines)
