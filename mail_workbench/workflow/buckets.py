# -*- coding: utf-8 -*-
"""工作桶计算（规范 §13）。

首页**不主要展示「全部邮件数量」**，而是展示五个工作桶：

    ACTION REQUIRED     等我处理
    WAITING FOR REPLY   我已经回复，对方尚未回复
    AI DRAFT READY      AI 已准备草稿
    SNOOZED             延后处理
    DONE TODAY          今天处理完成

桶的判定完全来自本地库（threads + draft_jobs + snoozes + messages），
不需要连云、不需要 LLM，因此首页可以做到 §38 要求的 < 200ms。

两个容易踩的坑（这里已按正确语义处理）：

1. **计数与列表必须分开**
   `ACTION REQUIRED` 的数字必须是全量统计。若用带 LIMIT 的查询结果去 len()，
   得到的只是切片大小 —— 曾经因此把 2883 报成 544。

2. **工作桶必须有「工作集时间窗」**
   邮箱里有几千封几年历史的邮件。若「待我处理」等于「所有最新一封是来信的线程」，
   这个桶会永远收敛不了，§30 的 TIME TO INBOX ZERO 也就失去意义。
   因此：**未读**的永远在桶里；已读的只在最近 N 天（`action_window_days`）内
   仍算待处理，更早的视为历史、退出工作集。
   这不是丢弃 —— 分类（classification）与线程（thread）都完整保留，
   搜索和线程视图仍可随时找回，`older_actionable` 也如实报出被移出工作集的数量。

3. **「等待对方回复」也必须有时间窗，而且它比「待我处理」更需要**
   见 constants.DEFAULT_WAITING_WINDOW_DAYS 的注释。一句话：
   「待我处理」涨了我可以行动，「等对方」涨了我一点办法都没有 ——
   一个只增不减、又不由我控制的计数器，是纯粹的焦虑源，不是工作视图。
   规则：只有我方最后一封（`last_outbound_ts`）在 `waiting_window_days` 天内的
   才算「活跃等待」；更早的进 `BUCKET_STALE_WAIT` 沉淀区（不删除、不隐藏、可搜）。
   例外：**有活跃 followup 的线程永远留在桶里** —— 那是用户主动说「这件事我要盯」，
   系统不该替他放弃。这样「等不等」的控制权回到用户手里，而不是由时间替他决定。
"""
from __future__ import annotations

from datetime import datetime

from .. import util
from ..constants import (
    BUCKET_ACTION_REQUIRED, BUCKET_BROADCAST, BUCKET_DONE_TODAY, BUCKET_DRAFT_READY,
    BUCKET_HINT, BUCKET_LABEL, BUCKET_SNOOZED, BUCKET_STALE_WAIT,
    BUCKET_WAITING_FOR_REPLY, CLASS_DISMISS, CLASS_IMPORTANT, CLASS_LABEL, CLASS_READ,
    CLASS_REPLY, CLASS_SYSTEM, DEFAULT_WAITING_WINDOW_DAYS, JOB_LABEL,
    WF_ARCHIVED, WF_DONE, WF_IGNORED, WF_SNOOZED,
)

# 工作流终态：不再进任何「待办」桶
_CLOSED = (WF_DONE, WF_IGNORED, WF_ARCHIVED)

# 需要「我」花时间处理的分类。
#   IMPORTANT ★重点  -> 要处理
#   REPLY     ✉要回  -> 要回复
#   READ      ○看一眼 -> 要过目（轻量，但仍是我的事）
# 显式排除：
#   SYSTEM  ⚙系统  -> 应去对应业务系统办理，不是「回邮件」
#   DISMISS ×可忽略 -> 用户已判定无需理会
_ACTIONABLE_CLASSES = (CLASS_IMPORTANT, CLASS_REPLY, CLASS_READ)

DEFAULT_ACTION_WINDOW_DAYS = 30


def _days_since(ts) -> int:
    """距今多少天（整天，向下取整）。时间戳无效返回 -1，UI 好据此不显示年龄。"""
    try:
        ts = float(ts or 0)
    except (TypeError, ValueError):
        return -1
    if ts <= 0:
        return -1
    try:
        secs = datetime.fromisoformat(util.now_iso()).timestamp() - ts
        # 时间戳落在未来（刚发出的信写回本地时会有几毫秒误差）要按 0 天算。
        # 不能让它变成负数：-1 是本函数表示「无效」的哨兵值，
        # 撞上了就会让 UI 以为「这封没有时间信息」而什么都不显示。
        return max(0, int(secs // 86400))
    except Exception:
        return -1


def _row(thread: dict, extra: dict = None) -> dict:
    out = {
        "thread_id": thread.get("thread_id"),
        "message_id": thread.get("latest_message_id"),
        "subject": thread.get("subject") or thread.get("last_subject"),
        "from_name": thread.get("last_from_name") or "",
        "from_addr": thread.get("last_from_addr") or "",
        "snippet": thread.get("last_snippet") or "",
        "date_iso": None,
        "latest_ts": thread.get("latest_ts"),
        "last_outbound_ts": thread.get("last_outbound_ts") or 0,
        # 我方最后一封发出距今多少天 —— 「等待」「沉底」两类都用它说话
        "waiting_days": _days_since(thread.get("last_outbound_ts")),
        "message_count": thread.get("message_count"),
        "classification": thread.get("classification"),
        "classification_label": CLASS_LABEL.get(thread.get("classification") or "", ""),
        "workflow_state": thread.get("workflow_state"),
        "priority": thread.get("priority"),
        "has_attachments": bool(thread.get("has_attachments")),
        "unread": bool(thread.get("last_unread")),
        "waiting_for_me": bool(thread.get("waiting_for_me")),
        "waiting_for_other": bool(thread.get("waiting_for_other")),
        "source": thread.get("last_source") or "imap",
        "broadcast": bool(thread.get("last_broadcast")),
    }
    if thread.get("latest_ts"):
        out["date_iso"] = datetime.fromtimestamp(
            thread["latest_ts"], tz=util.TZ_CST).replace(microsecond=0).isoformat()
    if extra:
        out.update(extra)
    return out


def _action_window_days(cfg: dict) -> int:
    try:
        v = int((cfg or {}).get("action_window_days", DEFAULT_ACTION_WINDOW_DAYS))
    except (TypeError, ValueError):
        v = DEFAULT_ACTION_WINDOW_DAYS
    # 0 或负数 -> 不设窗口（全部纳入工作集）。给「我就要看全量」留出口。
    return v


def _waiting_window_days(cfg: dict) -> int:
    """「等待对方回复」的活跃窗口。

    同样是 0/负数 = 不设窗口（退回旧行为，全部算活跃等待）。
    这个出口是刻意留的：如果哪天用户觉得「我就是要看到全部等待」，
    改一个数字就能回去，不需要改代码 —— 也就不用担心这次改动是单向的。
    """
    try:
        v = int((cfg or {}).get("waiting_window_days", DEFAULT_WAITING_WINDOW_DAYS))
    except (TypeError, ValueError):
        v = DEFAULT_WAITING_WINDOW_DAYS
    return v


def _informational_classes(cfg: dict) -> set:
    """cfg 里声明为「仅知会」的分类 —— 不算「待我处理」。

    默认空集合（= 重要/要回/看一眼 都算我的事），因为「看一眼的邮件算不算我的事」
    是个因人而异的政策判断，不该由程序替用户定。
    设成 ["READ"] 就能让「待我处理」只留真正要办要回的邮件。
    """
    raw = (cfg or {}).get("inbox_informational_classes") or []
    if isinstance(raw, str):
        raw = [x.strip() for x in raw.split(",") if x.strip()]
    return {str(x).strip().upper() for x in raw}


def compute(repo, cfg: dict = None, limit: int = 100, include_dismissed: bool = False) -> dict:
    """返回五个桶 + 计数。首页一次请求拿全部。

    limit 只截断**返回的列表**，不影响 counts（counts 永远是全量）。
    """
    cfg = cfg or {}
    rows = repo.list_threads(limit=0)          # 0 = 不限制；计数必须全量
    today_start = util.parse_iso(util.now_iso()).replace(
        hour=0, minute=0, second=0, microsecond=0).isoformat()

    window_days = _action_window_days(cfg)
    informational = _informational_classes(cfg)
    cutoff_ts = 0.0
    if window_days > 0:
        cutoff_ts = datetime.fromisoformat(today_start).timestamp() - window_days * 86400.0

    jobs_ready = repo.list_jobs(statuses=("ready", "reviewing"), limit=200)
    jobs_needs_input = repo.list_jobs(statuses=("needs_input",), limit=200)
    jobs_approved = repo.list_jobs(statuses=("approved",), limit=200)
    jobs_generating = repo.list_jobs(statuses=("queued", "generating"), limit=200)
    ready_by_thread = {}
    for j in jobs_ready + jobs_needs_input + jobs_approved + jobs_generating:
        if j.get("thread_id"):
            ready_by_thread[j["thread_id"]] = j

    snooze_by_thread = {}
    for s in repo.active_snoozes(limit=500):
        if s.get("thread_id"):
            snooze_by_thread[s["thread_id"]] = s

    # 有活跃 followup 的线程：用户主动按下了「这件事我要盯」。
    # 这类线程**不受等待窗口约束** —— 窗口是系统的默认值，用户的显式选择优先。
    followup_threads = set()
    for st in ("scheduled", "due"):
        try:
            for fu in repo.list_followups(status=st, limit=1000):
                if fu.get("thread_id"):
                    followup_threads.add(fu["thread_id"])
        except Exception:
            pass

    # 等待窗口的截止时间戳，以「我方最后一封发出时间」为准。
    # 用 last_outbound_ts 而不是 latest_ts：对方回了就自动离开这个桶了，
    # 所以这里的计时起点天然就是我发出那一刻，语义刚好对上。
    waiting_days = _waiting_window_days(cfg)
    waiting_cutoff = 0.0
    if waiting_days > 0:
        waiting_cutoff = (datetime.fromisoformat(today_start).timestamp()
                          - waiting_days * 86400.0)

    buckets = {
        BUCKET_ACTION_REQUIRED: [],
        BUCKET_WAITING_FOR_REPLY: [],
        BUCKET_DRAFT_READY: [],
        BUCKET_SNOOZED: [],
        BUCKET_DONE_TODAY: [],
        BUCKET_BROADCAST: [],
        BUCKET_STALE_WAIT: [],
    }
    # 被时间窗移出工作集的数量，如实上报（不静默丢弃）
    older_actionable = 0
    skipped_system = 0
    broadcast_in_window = 0
    stale_waiting = 0

    for t in rows:
        tid = t["thread_id"]
        wf = t.get("workflow_state") or "NEW"
        job = ready_by_thread.get(tid)
        snz = snooze_by_thread.get(tid)

        # AI 草稿就绪（含需要补信息的，这两类都要用户看）
        #
        # queued / generating **刻意不在这里**，它们属于「待我处理」——
        # 那时候球在我这边（等我去触发认领，或等它写完）。
        # 曾试图把它们也收进来「好让用户看得见在等认领」，结果把
        # test_revision_is_claimable_and_thread_stays_visible 弄挂了：
        # 那个测试明确要求「正在重新生成的线程仍应在首页（ACTION_REQUIRED）可见」。
        # 真正该修的不是归属，而是**那一行上的文案**：
        # 原来显示英文 "queued"，看不出在等什么 —— 现在显示「等待认领」。
        if job and job.get("status") in ("ready", "reviewing", "needs_input", "approved"):
            buckets[BUCKET_DRAFT_READY].append(_row(t, {
                "job_id": job["job_id"], "job_status": job["status"],
                "job_status_label": JOB_LABEL.get(job["status"], job["status"]),
                "has_draft": bool(job.get("draft_text")),
                "draft_mode": job.get("draft_mode"),
                "missing_count": len(job.get("missing_info_json") or []),
            }))
            continue

        # 延后
        if wf == WF_SNOOZED or (snz and not job):
            buckets[BUCKET_SNOOZED].append(_row(t, {
                "wake_at": (snz or {}).get("wake_at"),
                "snooze_note": (snz or {}).get("note"),
            }))
            continue

        if wf in _CLOSED:
            if wf == WF_DONE and (t.get("workflow_changed_at") or "") >= today_start:
                buckets[BUCKET_DONE_TODAY].append(_row(t, {
                    "done_at": t.get("workflow_changed_at")}))
            continue

        if t.get("waiting_for_other"):
            # 用户主动 follow up 过的 -> 无条件留下（他的显式意图优先于系统默认窗口）
            if tid in followup_threads:
                buckets[BUCKET_WAITING_FOR_REPLY].append(_row(t, {"followup": True}))
                continue
            out_ts = float(t.get("last_outbound_ts") or 0)
            # 超期 -> 沉底。注意「沉底」不等于「了结」：
            # 线程状态一个字没改，搜索、线程视图、归档都还在，
            # 对方哪天真回了也会照常回到「待我处理」。
            # 这里只是不再让它占用我的工作计数。
            if waiting_cutoff and out_ts and out_ts < waiting_cutoff:
                stale_waiting += 1
                buckets[BUCKET_STALE_WAIT].append(_row(t, {"stale": True}))
                continue
            buckets[BUCKET_WAITING_FOR_REPLY].append(_row(t))
            continue

        if not t.get("waiting_for_me"):
            continue

        cls = t.get("classification")
        # ⚙系统类：应去对应业务系统办理，不属于「回邮件」，不进工作桶
        if cls == CLASS_SYSTEM:
            skipped_system += 1
            continue
        if cls == CLASS_DISMISS and not include_dismissed:
            continue
        if cls not in _ACTIONABLE_CLASSES and cls != CLASS_DISMISS:
            # 尚未分类的（None）保守地放进桶里，避免漏掉新到的邮件
            pass

        # 工作集时间窗：未读的永远在；已读的只在窗口内算待处理
        over_window = bool(cutoff_ts) and not t.get("last_unread") \
            and (t.get("latest_ts") or 0) < cutoff_ts
        if over_window:
            older_actionable += 1
            continue

        # 群发通知 / 配置为「仅知会」的分类：不占用「待我处理」。
        # 注意这不是「丢掉」—— 它们有自己的列表（BROADCAST），计数如实给出，
        # 搜索也照常能搜到。只有真的事务才留在「待我处理」里。
        if t.get("last_broadcast") or (cls and cls in informational):
            broadcast_in_window += 1
            buckets[BUCKET_BROADCAST].append(_row(t, {
                "informational_reason": ("群发通知" if t.get("last_broadcast")
                                         else "按配置归为仅知会（%s）" % cls),
            }))
            continue

        buckets[BUCKET_ACTION_REQUIRED].append(_row(t, {
            "job_id": (job or {}).get("job_id"),
            "job_status": (job or {}).get("status"),
            "job_status_label": JOB_LABEL.get((job or {}).get("status"),
                                              (job or {}).get("status")),
        }))

    for k in buckets:
        buckets[k].sort(key=lambda r: (-(r.get("priority") or 0), -(r.get("latest_ts") or 0)))

    counts = {k: len(v) for k, v in buckets.items()}
    return {
        "generated_at": util.now_iso(),
        "counts": counts,
        "labels": BUCKET_LABEL,
        "hints": BUCKET_HINT,
        "buckets": {k: (v[:limit] if limit and limit > 0 else v) for k, v in buckets.items()},
        "pipeline": {
            "queued": len(repo.list_jobs(statuses=("queued",), limit=500)),
            "generating": len(repo.list_jobs(statuses=("generating",), limit=500)),
            "needs_input": len(jobs_needs_input),
            "ready": len(jobs_ready),
            "approved": len(jobs_approved),
        },
        "workingset": {
            "action_window_days": window_days,
            "waiting_window_days": waiting_days,
            "older_actionable": older_actionable,
            "skipped_system": skipped_system,
            "broadcast": counts[BUCKET_BROADCAST],
            "stale_waiting": stale_waiting,
            "note": ("older_actionable = 已读且超出时间窗、已退出工作集的线程数（仍可搜索到）；"
                     "broadcast = 判定为群发通知的线程数（在「群发通知」里可见，不占用待我处理）；"
                     "stale_waiting = 我方发出超过 waiting_window_days 天仍无回音的线程数"
                     "（已退出「等待对方回复」，在沉底区可见，不删除）"),
        },
        "totals": {
            "threads": len(rows),
            "action_required": counts[BUCKET_ACTION_REQUIRED],
            "inbox_messages": repo.count_messages(cfg.get("folder_inbox", "INBOX")),
        },
    }


def counts_only(repo, cfg: dict = None) -> dict:
    data = compute(repo, cfg, limit=0)
    return {"counts": data["counts"], "pipeline": data["pipeline"],
            "workingset": data.get("workingset"),
            "generated_at": data["generated_at"]}
