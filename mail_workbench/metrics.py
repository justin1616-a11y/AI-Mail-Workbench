# -*- coding: utf-8 -*-
"""Metrics —— 本地统计（规范 §30）。

**不建复杂 telemetry**：所有数据只留在本机 SQLite 里，不上传任何地方。
重点跟踪两个真正有意义的指标：

    TIME TO INBOX ZERO      邮件到达 -> 处理完毕 的平均耗时（小时）
    DRAFT ACCEPTANCE RATE   我确认发送的草稿 / 全部产出的草稿（避免 AI 白干活）
"""
from __future__ import annotations

from . import util

TTIZ_EVENT = "time_to_inbox_zero_hours"

# 需要对外的计数器（其余内部计数器也会一并展示，但这两个是重点）
KEY_METRICS = {
    "emails_received": "收到邮件",
    "emails_archived": "归档邮件",
    "emails_trashed": "删除邮件",
    "emails_starred": "星标操作",
    "reply_candidates": "产生的回复候选",
    "draft_jobs": "创建的草稿任务",
    "draft_jobs_deduped": "因幂等被合并的重复请求",
    "draft_success": "草稿生成成功",
    "draft_failure": "草稿生成失败",
    "drafts_approved": "人类确认发送",
    "drafts_sent": "实际发送",
    "snoozes_created": "延后处理",
    "snoozes_woken": "延后到期恢复",
    "followups_scheduled": "安排跟进",
    "followups_due": "跟进到期",
    "followups_closed_by_reply": "因对方回信自动关闭的跟进",
    "sync_runs": "同步次数",
    "sync_errors": "同步失败次数",
    "recovery_actions": "自愈动作",
}


def snapshot(repo) -> dict:
    """汇总一份指标快照。"""
    raw = repo.all_metrics()

    received = raw.get("emails_received", 0) or 0
    sent = raw.get("drafts_sent", 0) or 0
    dismissed = int(repo.db.scalar(
        "SELECT COUNT(*) FROM draft_jobs WHERE status IN ('dismissed','expired')", default=0) or 0)
    jobs_total = raw.get("draft_jobs", 0) or 0

    # 接受率：确认发送 / (发送 + 被放弃/过期)。分母为 0 时给 None，避免假 0%.
    denom = sent + dismissed
    acceptance = round(sent / denom, 4) if denom else None

    avg_latency = repo.metric_average("draft_latency_seconds", limit=200)
    avg_edit = repo.metric_average("user_edit_ratio", limit=200)
    ttiz = avg_ttiz(repo)

    return {
        "generated_at": util.now_iso(),
        "counters": {k: raw.get(k, 0) for k in KEY_METRICS},
        "counter_labels": KEY_METRICS,
        "other_counters": {k: v for k, v in raw.items() if k not in KEY_METRICS},
        "kpi": {
            "time_to_inbox_zero_hours": ttiz,
            "draft_acceptance_rate": acceptance,
            "average_draft_latency_seconds": round(avg_latency, 1),
            "average_user_edit_ratio": round(avg_edit, 4),
            "draft_failure_rate": round(
                (raw.get("draft_failure", 0) or 0) / jobs_total, 4) if jobs_total else None,
            "total_draft_jobs": jobs_total,
            "drafts_sent": sent,
            "drafts_discarded": dismissed,
            "emails_received": received,
        },
        "series": {
            "draft_latency": repo.metric_series("draft_latency_seconds", limit=50),
            "user_edit_ratio": repo.metric_series("user_edit_ratio", limit=50),
            "time_to_inbox_zero": repo.metric_series(TTIZ_EVENT, limit=50),
        },
        "local_only": True,
        "note": "全部指标仅保存在本机 SQLite，不上传任何外部服务。",
    }


def record_ttiz_for_today(repo) -> dict:
    """计算「今天处理的邮件」的平均到达->完成耗时，并记录。

    度量的是：一封邮件从 date_iso（到达）到 workflow_changed_at（被标记 DONE/ARCHIVED）
    之间的时长。这就是「TIME TO INBOX ZERO」的可操作近似。
    """
    rows = repo.db.query(
        "SELECT message_id, date_iso, workflow_changed_at FROM messages "
        "WHERE workflow_state IN ('DONE','ARCHIVED') AND workflow_changed_at IS NOT NULL "
        "AND date_iso IS NOT NULL AND date_iso != '' "
        "ORDER BY workflow_changed_at DESC LIMIT 200")
    vals = []
    for r in rows:
        h = util.hours_between(r["date_iso"], r["workflow_changed_at"])
        if h and h > 0:
            vals.append(h)
    if not vals:
        return {"count": 0, "average_hours": None}
    avg = sum(vals) / len(vals)
    repo.record_metric_event(TTIZ_EVENT, round(avg, 3))
    repo.set_metric("time_to_inbox_zero_hours", round(avg, 3))
    repo.set_metric("inbox_zero_samples", len(vals))
    return {"count": len(vals), "average_hours": round(avg, 3),
            "fastest_hours": round(min(vals), 3), "slowest_hours": round(max(vals), 3)}


def avg_ttiz(repo, limit: int = 200):
    v = repo.metric_average(TTIZ_EVENT, limit=limit)
    if v:
        return round(v, 3)
    stored = repo.all_metrics().get("time_to_inbox_zero_hours")
    return round(float(stored), 3) if stored else None


def window_stats(repo, days: int = 7) -> dict:
    """最近 N 天的作业量，供 UI 画简易趋势。"""
    since = util.add_seconds(util.now_iso(), -days * 86400)
    return {
        "days": days,
        "messages_received": int(repo.db.scalar(
            "SELECT COUNT(*) FROM messages WHERE ingested_at >= ?", (since,), 0) or 0),
        "candidates": int(repo.db.scalar(
            "SELECT COUNT(*) FROM candidates WHERE created_at >= ?", (since,), 0) or 0),
        "jobs": int(repo.db.scalar(
            "SELECT COUNT(*) FROM draft_jobs WHERE created_at >= ?", (since,), 0) or 0),
        "sent": int(repo.db.scalar(
            "SELECT COUNT(*) FROM draft_jobs WHERE sent_at >= ?", (since,), 0) or 0),
        "done": int(repo.db.scalar(
            "SELECT COUNT(*) FROM messages WHERE workflow_state IN ('DONE','ARCHIVED') "
            "AND workflow_changed_at >= ?", (since,), 0) or 0),
    }
