# -*- coding: utf-8 -*-
"""DraftJobQueue —— 草稿任务队列（规范 §5 / §25 / §26 / §27 / §28）。

本模块是 V2 的**中枢**，负责：
  * 幂等建任务（同一 message 只允许一个活跃任务，绝不重复调用 WorkBuddy）
  * 原子 claim + 租约（两个 worker 不会同时处理同一个任务）
  * 超时 / 指数退避重试（禁止无限重试）
  * 状态机驱动的转换（全部经 workflow.state_machine 校验）
  * 人类确认与发送授权（approve_token 一次性、10 分钟过期）

三条不可退让的规则：
  1. `ensure_job` 幂等 —— 重复点击「AI 起草」返回既有任务。
  2. claim 必须在单个 SQLite 事务里完成（BEGIN IMMEDIATE + 条件 UPDATE）。
  3. 发送只能在 `approved` 且带有效 token 时发生；状态机里没有捷径。
"""
from __future__ import annotations

import sqlite3

from .. import util
from ..constants import (
    JOB_ACTIVE_STATUSES, JOB_APPROVED, JOB_CANDIDATE, JOB_DISMISSED, JOB_EXPIRED,
    JOB_FAILED, JOB_GENERATING, JOB_NEEDS_INPUT, JOB_QUEUED, JOB_READY, JOB_REVIEWING,
    JOB_SENT, MODE_NORMAL, MODE_QUICK, TRIGGER_MANUAL,
)
from ..workflow import state_machine as sm

TOKEN_TTL_SECONDS = 600


class QueueError(Exception):
    pass


# --------------------------------------------------------------------------
# 幂等键
# --------------------------------------------------------------------------
def idempotency_key(cfg: dict, message_id: str) -> str:
    """规范 §25 的形式：draft:{account}:{message_id}。"""
    return "draft:%s:%s" % (cfg.get("user") or "acct", message_id or "")


# --------------------------------------------------------------------------
# 建任务（幂等）
# --------------------------------------------------------------------------
def build_recipients(msg: dict, cfg: dict) -> list:
    """回复收件人：优先 Reply-To/发件人；系统类地址回退到原收件人。"""
    me = (cfg.get("user") or "").lower()
    frm = (msg.get("from_addr") or "").lower()
    if frm and frm != me and "@" in frm:
        return [frm]
    for a in util.parse_recipients(msg.get("to_addrs") or ""):
        if a != me:
            return [a]
    return [frm] if frm else []


def reply_subject(subject: str) -> str:
    s = util.clean(subject)
    low = s.lower()
    for p in ("re:", "re：", "回复:", "回复：", "答复:", "答复："):
        if low.startswith(p):
            return s
    return ("Re: " + s) if s else "Re: (无主题)"


def ensure_job(repo, cfg: dict, message_id: str, trigger_source: str = TRIGGER_MANUAL,
               draft_mode: str = None, priority: int = None,
               revision_of: str = None, instruction: str = "") -> dict:
    """幂等地把一个 Candidate/邮件提升为 DraftJob。

    若已有活跃任务（queued/generating/needs_input/ready/reviewing/approved）则
    直接返回它 —— 这就是「重复点击不重复调用 WorkBuddy」的实现点。
    """
    key = idempotency_key(cfg, message_id)
    existing = repo.get_active_job_by_idem(key)
    if existing:
        repo.inc_metric("draft_jobs_deduped")
        return existing

    msg = repo.get_message(message_id)
    if not msg:
        raise QueueError("找不到邮件：%s" % message_id)

    job = {
        "job_id": util.new_id("job"),
        "message_id": message_id,
        "thread_id": msg.get("thread_id"),
        "account": cfg.get("user") or msg.get("account") or "",
        "folder": msg.get("folder") or "",
        "sender": msg.get("from_addr") or "",
        "recipients": build_recipients(msg, cfg),
        "subject": reply_subject(msg.get("subject")),
        "created_at": util.now_iso(),
        "priority": int(priority if priority is not None else (msg.get("priority") or 50)),
        "trigger_source": trigger_source or TRIGGER_MANUAL,
        "draft_mode": draft_mode or cfg.get("default_draft_mode", MODE_NORMAL),
        "status": JOB_QUEUED,
        "retry_count": 0,
        "idempotency_key": key,
        "revision_of": revision_of,
        "revision_instruction": instruction or "",
        "next_attempt_at": util.now_iso(),
        "context_snapshot": None,
    }
    try:
        repo.insert_job(job)
    except sqlite3.IntegrityError:
        # 并发下被别人抢先建了 —— 返回既有的，不报错（幂等语义）
        again = repo.get_active_job_by_idem(key)
        if again:
            return again
        raise
    repo.log_job_event(job["job_id"], None, JOB_QUEUED, actor=trigger_source or "manual",
                       note="任务创建" + ("（由 %s 派生）" % revision_of if revision_of else ""))
    repo.inc_metric("draft_jobs")
    return repo.get_job(job["job_id"])


def transition_candidate(repo, candidate_id: str, job_id: str) -> dict:
    repo.set_candidate_status(candidate_id, "converted")
    return repo.get_job(job_id)


# --------------------------------------------------------------------------
# Claim / Lease（规范 §27）
# --------------------------------------------------------------------------
def claim_next(repo, cfg: dict, worker: str = "workbuddy") -> dict:
    """原子地认领下一个可处理任务。没有则返回 None。

    必须在单个事务里完成「挑一个 + 打上租约」，否则两个 worker 会撞车。
    """
    now = util.now_iso()
    lease_until = util.add_seconds(now, int(cfg.get("lease_seconds", 300)))
    with repo.db.tx() as cur:
        row = cur.execute(
            "SELECT job_id FROM draft_jobs "
            "WHERE status = ? AND (next_attempt_at IS NULL OR next_attempt_at <= ?) "
            "ORDER BY priority DESC, created_at ASC LIMIT 1",
            (JOB_QUEUED, now)).fetchone()
        if not row:
            return None
        job_id = row[0]
        upd = cur.execute(
            "UPDATE draft_jobs SET status = ?, claimed_by = ?, claim_time = ?, "
            "lease_until = ?, updated_at = ? "
            "WHERE job_id = ? AND status = ?",
            (JOB_GENERATING, worker, now, lease_until, now, job_id, JOB_QUEUED))
        if upd.rowcount != 1:
            return None
        cur.execute(
            "INSERT INTO job_events (job_id, ts, from_status, to_status, actor, note) "
            "VALUES (?,?,?,?,?,?)",
            (job_id, now, JOB_QUEUED, JOB_GENERATING, worker,
             "claim，租约至 %s" % lease_until))
    job = repo.get_job(job_id)
    repo.inc_metric("draft_claims")
    return job


def heartbeat(repo, cfg: dict, job_id: str, worker: str = None) -> dict:
    """续租。超时未续 -> 租约到期后由 Recovery 重投。"""
    now = util.now_iso()
    job = repo.get_job(job_id)
    if not job:
        raise QueueError("任务不存在：%s" % job_id)
    if worker and job.get("claimed_by") and job["claimed_by"] != worker:
        raise QueueError("任务 %s 被 %s 持有，无法续租" % (job_id, job["claimed_by"]))
    patch = {"lease_until": util.add_seconds(now, int(cfg.get("lease_seconds", 300)))}
    repo.update_job(job_id, patch)
    return repo.get_job(job_id)


def release(repo, cfg: dict, job_id: str, reason: str = "", retryable: bool = True,
            worker: str = None) -> dict:
    """worker 主动放弃（崩溃前的优雅路径 / 用户取消）。

    retryable=True 且未超上限 -> 指数退避后重回 queued；
    否则进入 failed 终态（等人工处理）。
    """
    job = repo.get_job(job_id)
    if not job:
        raise QueueError("任务不存在：%s" % job_id)
    if job["status"] != JOB_GENERATING:
        raise QueueError("只有 generating 状态可以 release，当前 %s" % job["status"])
    return fail(repo, cfg, job_id, error=reason or "worker released",
                retryable=retryable, worker=worker)


def _backoff_seconds(cfg: dict, retry_count: int) -> int:
    base = int(cfg.get("retry_base_seconds", 60))
    cap = int(cfg.get("retry_max_seconds", 1800))
    return min(base * (2 ** max(0, retry_count - 1)), cap)


def fail(repo, cfg: dict, job_id: str, error: str, retryable: bool = True,
         worker: str = None) -> dict:
    """失败处理 + 指数退避重试（规范 §28，禁止无限 retry）。"""
    now = util.now_iso()
    max_retry = int(cfg.get("max_retry", 3))
    job = repo.get_job(job_id)
    if not job:
        raise QueueError("任务不存在：%s" % job_id)
    if job["status"] not in (JOB_GENERATING, JOB_QUEUED, JOB_NEEDS_INPUT, JOB_APPROVED):
        raise QueueError("状态 %s 不允许 fail" % job["status"])

    retry_count = int(job.get("retry_count") or 0) + 1
    from_status = job["status"]
    can_retry = bool(retryable) and retry_count <= max_retry

    with repo.db.tx() as cur:
        cur.execute(
            "UPDATE draft_jobs SET status=?, retry_count=?, last_error=?, updated_at=?, "
            "claimed_by=NULL, lease_until=NULL WHERE job_id=?",
            (JOB_FAILED, retry_count, (error or "")[:2000], now, job_id))
        cur.execute(
            "INSERT INTO job_events (job_id, ts, from_status, to_status, actor, note) "
            "VALUES (?,?,?,?,?,?)",
            (job_id, now, from_status, JOB_FAILED, worker or "system",
             "第 %d 次失败：%s" % (retry_count, (error or "")[:300])))
        if can_retry:
            delay = _backoff_seconds(cfg, retry_count)
            next_at = util.add_seconds(now, delay)
            cur.execute(
                "UPDATE draft_jobs SET status=?, next_attempt_at=?, updated_at=? WHERE job_id=?",
                (JOB_QUEUED, next_at, now, job_id))
            cur.execute(
                "INSERT INTO job_events (job_id, ts, from_status, to_status, actor, note) "
                "VALUES (?,?,?,?,?,?)",
                (job_id, util.now_iso(), JOB_FAILED, JOB_QUEUED, "backoff",
                 "%d 秒后重试（%d/%d）" % (delay, retry_count, max_retry)))
        else:
            cur.execute("UPDATE draft_jobs SET next_attempt_at=NULL WHERE job_id=?", (job_id,))

    repo.inc_metric("draft_failure")
    if not can_retry:
        repo.set_metric("draft_retry_exhausted", int(repo.all_metrics().get(
            "draft_retry_exhausted", 0)) + 1)
    return repo.get_job(job_id)


# --------------------------------------------------------------------------
# Worker 契约写回（规范 §26）
# --------------------------------------------------------------------------
def set_context(repo, job_id: str, package: dict) -> dict:
    job = repo.get_job(job_id)
    if not job:
        raise QueueError("任务不存在：%s" % job_id)
    if job["status"] != JOB_GENERATING:
        raise QueueError("状态 %s 不接受 context 写回" % job["status"])
    return repo.update_job(job_id, {
        "context_snapshot": package,
        "context_hash": package.get("context_hash"),
    }) or repo.get_job(job_id)


def set_plan(repo, job_id: str, plan: dict) -> tuple:
    """写入 ReplyPlan。若 plan 声明缺信息 -> 状态转 needs_input（规范 §10 门禁）。"""
    job = repo.get_job(job_id)
    if not job:
        raise QueueError("任务不存在：%s" % job_id)
    if job["status"] != JOB_GENERATING:
        raise QueueError("状态 %s 不接受 plan 写回" % job["status"])

    missing = plan.get("missing_information") or []
    if plan.get("status") == "NEEDS_INPUT" or missing:
        repo.update_job(job_id, {"plan_json": plan, "missing_info_json": missing})
        updated = sm.transition(repo, job_id, JOB_NEEDS_INPUT, actor="workbuddy",
                                note="缺 %d 项信息，等待用户补充" % len(missing))
        return updated, True
    repo.update_job(job_id, {"plan_json": plan, "missing_info_json": []})
    return repo.get_job(job_id), False


def set_draft(repo, job_id: str, draft_text: str, draft_mode: str = None,
              variant: int = None) -> dict:
    job = repo.get_job(job_id)
    if not job:
        raise QueueError("任务不存在：%s" % job_id)
    if job["status"] not in (JOB_GENERATING, JOB_NEEDS_INPUT, JOB_REVIEWING, JOB_READY):
        raise QueueError("状态 %s 不接受 draft 写回" % job["status"])
    if not (draft_text or "").strip():
        raise QueueError("草稿正文为空，拒绝")
    patch = {
        "draft_text": draft_text,
        "original_draft_text": job.get("original_draft_text") or draft_text,
        "claimed_by": None,
        "lease_until": None,
        "last_error": None,
    }
    if draft_mode:
        patch["draft_mode"] = draft_mode
    if variant is not None:
        patch["variant"] = int(variant)
    repo.update_job(job_id, patch)
    if job["status"] != JOB_READY:
        out = sm.transition(repo, job_id, JOB_READY, actor="workbuddy", note="草稿生成完成")
    else:
        out = repo.get_job(job_id)
    repo.inc_metric("draft_success")
    # 记录起草耗时（从创建到就绪）
    lat = util.hours_between(job.get("created_at"), util.now_iso()) * 3600.0
    if lat > 0:
        repo.record_metric_event("draft_latency_seconds", lat)
    return out


def set_fast_draft(repo, cfg: dict, message_id: str, draft_text: str,
                   draft_mode: str = None, trigger: str = "fast_template") -> dict:
    """本地模板草稿：内容已在手上，直接写成 ready，不需要 Worker 认领。

    **为什么值得为它单开一条路**：
        常规路径是 `queued →（等有人认领）→ generating → ready`，
        而模板草稿的正文**在这一刻就已经算出来了**，不存在「等某个 Worker 来写」。
        硬让它先排队等着，代价是把「快」做没了 —— 而「快」正是这个功能的全部意义。
        实测 claim → ready 要 22 秒，但那之外还有一段更长的等待（等人来认领），
        对「邮件已收到，谢谢」这类事务性回复，这段等待纯属浪费。

    **没有绕开任何门禁**：这两步转换仍照走（`queued → generating → ready`），
        只是由本地代理（actor="fast-template"）立刻完成，job_events 一样完整。
        发送只允许从 `approved` 出发（state_machine.SEND_ALLOWED_STATES），
        与这里毫无关系 —— 模板草稿同样要人点「确认 → 发送」。

    已有活跃任务时就地填上：用户点「快速草稿」即表示放弃等 AI 那一版。
    """
    if not (draft_text or "").strip():
        raise QueueError("草稿正文为空，拒绝")

    job = ensure_job(repo, cfg, message_id, trigger_source=trigger,
                     draft_mode=draft_mode or MODE_QUICK)
    jid = job["job_id"]
    st = job["status"]

    # 已经在等人处理的状态：只换正文，不动状态 ——
    # 别因为「换了个来源」就把用户已经开始的审核流程降级。
    if st in (JOB_READY, JOB_REVIEWING, JOB_APPROVED):
        repo.update_job(jid, {
            "draft_text": draft_text,
            "original_draft_text": job.get("original_draft_text") or draft_text,
            "draft_mode": draft_mode or job.get("draft_mode") or MODE_QUICK,
        })
        return repo.get_job(jid)

    if st == JOB_NEEDS_INPUT:
        # 模板草稿不写事实，所以原来缺的那些信息它也不需要
        sm.transition(repo, jid, JOB_QUEUED, actor="fast-template",
                      note="改用本地模板草稿，无需补充信息")
        st = JOB_QUEUED

    if st == JOB_QUEUED:
        repo.update_job(jid, {"claimed_by": "fast-template", "lease_until": None})
        sm.transition(repo, jid, JOB_GENERATING, actor="fast-template",
                      note="本地模板草稿（未调用模型）")

    repo.update_job(jid, {
        "draft_text": draft_text,
        "original_draft_text": job.get("original_draft_text") or draft_text,
        "draft_mode": draft_mode or job.get("draft_mode") or MODE_QUICK,
        "claimed_by": None,
        "lease_until": None,
        "last_error": None,
        "missing_info_json": [],
    })
    out = sm.transition(repo, jid, JOB_READY, actor="fast-template",
                        note="本地模板草稿就绪（未调用模型）")
    repo.inc_metric("draft_success")
    repo.inc_metric("draft_fast_template")
    return out


def provide_input(repo, job_id: str, user_input: dict) -> dict:
    """用户补齐 Missing Information 后恢复起草（needs_input -> queued）。"""
    job = repo.get_job(job_id)
    if not job:
        raise QueueError("任务不存在：%s" % job_id)
    if job["status"] != JOB_NEEDS_INPUT:
        raise QueueError("只有 needs_input 可以补信息，当前 %s" % job["status"])
    merged = dict(job.get("user_input_json") or {})
    merged.update(user_input or {})
    # 必须同时作废 context_snapshot：它是补信息之前构建的，user_notes 里没有
    # 这次的 user_input。若不清空，/context 与 /plan 都会命中旧快照
    # （worker_contract 用 `job.get("context_snapshot") or build(...)` 短路），
    # 于是 gate 每轮都用空 user_notes 重新拦同一项 —— 任务会在
    # needs_input <-> generating 之间死循环，永远出不来。
    repo.update_job(job_id, {"user_input_json": merged, "next_attempt_at": util.now_iso(),
                             "context_snapshot": None})
    return sm.transition(repo, job_id, JOB_QUEUED, actor="user",
                         note="用户补充了 %d 项信息，恢复起草" % len(user_input or {}))


# --------------------------------------------------------------------------
# 人类审核 / 发送授权（规范 §33）
# --------------------------------------------------------------------------
def begin_review(repo, job_id: str, actor: str = "user") -> dict:
    job = repo.get_job(job_id)
    if not job:
        raise QueueError("任务不存在：%s" % job_id)
    if job["status"] == JOB_REVIEWING:
        return job
    if job["status"] == JOB_READY:
        return sm.transition(repo, job_id, JOB_REVIEWING, actor=actor, note="开始人工审核")
    if job["status"] == JOB_NEEDS_INPUT:
        return job
    return job


def save_edit(repo, job_id: str, draft_text: str, actor: str = "user") -> dict:
    """用户手工编辑草稿。同时更新 Edit Ratio（§30 的 user_edit_ratio）。"""
    job = repo.get_job(job_id)
    if not job:
        raise QueueError("任务不存在：%s" % job_id)
    if job["status"] not in (JOB_READY, JOB_REVIEWING, JOB_APPROVED):
        raise QueueError("状态 %s 不允许编辑" % job["status"])
    original = job.get("original_draft_text") or job.get("draft_text") or ""
    ratio = edit_ratio(original, draft_text)
    patch = {"draft_text": draft_text, "edit_ratio": ratio}
    if job["status"] == JOB_APPROVED:
        patch["approve_token"] = None
        patch["approve_token_expires"] = None
        patch["approved_at"] = None
    repo.update_job(job_id, patch)
    if ratio is not None:
        repo.record_metric_event("user_edit_ratio", ratio)
    if job["status"] == JOB_APPROVED:
        return sm.transition(repo, job_id, JOB_REVIEWING, actor=actor,
                             note="发送前内容被修改，需重新确认")
    if job["status"] == JOB_READY:
        return sm.transition(repo, job_id, JOB_REVIEWING, actor=actor, note="进入人工审核")
    return repo.get_job(job_id)


def edit_ratio(original: str, edited: str):
    """0 = 一字未改，1 = 完全重写。用 difflib 计算相似度。"""
    a = (original or "").strip()
    b = (edited or "").strip()
    if not a and not b:
        return 0.0
    if not a or not b:
        return 1.0
    import difflib
    return round(1.0 - difflib.SequenceMatcher(None, a, b).ratio(), 4)


def approve(repo, cfg: dict, job_id: str, actor: str = "user") -> dict:
    """人类点「确认发送」—— 产生一次性 approve_token。

    若任务还在 ready，会先补一次 ready -> reviewing（审核），再 reviewing -> approved，
    保证状态机的每一步都是合法转换。
    """
    job = repo.get_job(job_id)
    if not job:
        raise QueueError("任务不存在：%s" % job_id)
    if not (job.get("draft_text") or "").strip():
        raise QueueError("草稿为空，无法确认")
    if not job.get("recipients"):
        raise QueueError("收件人为空，无法确认")

    if job["status"] == JOB_READY:
        job = sm.transition(repo, job_id, JOB_REVIEWING, actor=actor, note="自动进入审核")
    if job["status"] == JOB_REVIEWING:
        token = util.new_token(24)
        job = sm.transition(
            repo, job_id, JOB_APPROVED, actor=actor, note="人工确认发送",
            patch={"approve_token": token,
                   "approve_token_expires": util.add_seconds(util.now_iso(), TOKEN_TTL_SECONDS),
                   "approved_at": util.now_iso()})
        repo.inc_metric("drafts_approved")
        return {"job": job, "approve_token": token,
                "expires_at": job.get("approve_token_expires"),
                "confirm_hint": "调用 /api/drafts/%s/send 并带上 token 才会真正发送" % job_id}
    raise QueueError("只有 ready/reviewing 可以确认，当前 %s" % job["status"])


def authorize_send(repo, job_id: str, token: str) -> tuple:
    """校验发送授权。返回 (ok, reason, job)。"""
    job = repo.get_job(job_id)
    if not job:
        return False, "任务不存在", None
    if job["status"] != JOB_APPROVED:
        return False, "任务状态为 %s，只有 approved 能发送（AI 永不自动发送）" % job["status"], job
    stored = job.get("approve_token")
    if not stored:
        return False, "没有确认令牌，请先在界面点「确认发送」", job
    if token != stored:
        return False, "确认令牌不匹配", job
    exp = util.parse_iso(job.get("approve_token_expires"))
    if exp and util.now() > exp:
        return False, "确认令牌已过期（%s）" % job.get("approve_token_expires"), job
    return True, "ok", job


def mark_sent(repo, cfg: dict, job_id: str, sent_message_id: str = "",
              followup_days: int = None, actor: str = "user") -> dict:
    job = repo.get_job(job_id)
    if not job:
        raise QueueError("任务不存在：%s" % job_id)
    out = sm.transition(repo, job_id, JOB_SENT, actor=actor, note="已投递并写入已发送",
                        patch={"sent_at": util.now_iso(),
                               "sent_message_id": sent_message_id,
                               "approve_token": None,
                               "approve_token_expires": None,
                               "claimed_by": None, "lease_until": None})
    repo.inc_metric("drafts_sent")
    # 发出后自动安排跟进（本地 scheduler，不为每封邮件建 automation）
    days = followup_days if followup_days is not None else int(cfg.get("followup_default_days", 3))
    if days and days > 0:
        repo.create_followup({
            "thread_id": job.get("thread_id"),
            "message_id": job.get("message_id"),
            "sent_message_id": sent_message_id,
            "due_at": util.add_days(util.now_iso(), days),
            "note": "发送后 %d 天未见回复则提醒跟进" % days,
        })
    return out


def revoke_approval(repo, job_id: str, actor: str = "user") -> dict:
    """撤回确认（approved -> reviewing），并作废令牌。"""
    job = repo.get_job(job_id)
    if not job:
        raise QueueError("任务不存在：%s" % job_id)
    if job["status"] != JOB_APPROVED:
        raise QueueError("只有 approved 可以撤回，当前 %s" % job["status"])
    return sm.transition(repo, job_id, JOB_REVIEWING, actor=actor, note="撤回发送确认",
                         patch={"approve_token": None, "approve_token_expires": None})


def dismiss(repo, job_id: str, actor: str = "user", note: str = "") -> dict:
    job = repo.get_job(job_id)
    if not job:
        raise QueueError("任务不存在：%s" % job_id)
    if sm.is_terminal(job["status"]):
        return job
    return sm.transition(repo, job_id, JOB_DISMISSED, actor=actor, note=note or "用户放弃",
                         patch={"claimed_by": None, "lease_until": None,
                                "approve_token": None, "approve_token_expires": None})


# --------------------------------------------------------------------------
# Reply Controls（规范 §12）—— 一键改写，不要求用户重写 prompt
# --------------------------------------------------------------------------
def revise(repo, cfg: dict, job_id: str, instruction: str,
           draft_mode: str = None, actor: str = "user") -> dict:
    """基于既有草稿派生新版本。

    关键：先把父任务移出活跃集合（ready/reviewing -> expired，note=superseded），
    这样复用同一个 idempotency_key 也不会撞唯一索引，
    从而既满足「同一 message 只有一个活跃任务」，又不破坏状态机合法性。
    """
    job = repo.get_job(job_id)
    if not job:
        raise QueueError("任务不存在：%s" % job_id)
    if sm.is_terminal(job["status"]):
        raise QueueError("任务已终结（%s），无法改写" % job["status"])

    parent_status = job["status"]
    if parent_status == JOB_APPROVED:
        job = revoke_approval(repo, job_id, actor=actor)
        parent_status = JOB_REVIEWING
    if parent_status in (JOB_READY, JOB_REVIEWING, JOB_NEEDS_INPUT, JOB_QUEUED):
        sm.transition(repo, job_id, JOB_EXPIRED, actor=actor,
                      note="被新版本取代（superseded）")
    elif parent_status == JOB_GENERATING:
        raise QueueError("正在起草中，请等这一版出来再改写")

    new_job = ensure_job(repo, cfg, job["message_id"], trigger_source="revise",
                         draft_mode=draft_mode or job.get("draft_mode"),
                         priority=job.get("priority"),
                         revision_of=job_id, instruction=instruction)
    if new_job["status"] != JOB_QUEUED:
        # 极端情况：并发下被别的请求抢先
        from ..workflow import state_machine as _sm
        if new_job["status"] == JOB_EXPIRED:
            raise QueueError("派生失败，请重试")
    return new_job


# --------------------------------------------------------------------------
# 展示
# --------------------------------------------------------------------------
def to_public(job: dict, include_context: bool = False, include_plan: bool = True) -> dict:
    if not job:
        return {}
    out = {
        "job_id": job.get("job_id"),
        "message_id": job.get("message_id"),
        "thread_id": job.get("thread_id"),
        "sender": job.get("sender"),
        "recipients": job.get("recipients") or [],
        "subject": job.get("subject"),
        "created_at": job.get("created_at"),
        "updated_at": job.get("updated_at"),
        "priority": job.get("priority"),
        "trigger_source": job.get("trigger_source"),
        "draft_mode": job.get("draft_mode"),
        "status": job.get("status"),
        "status_label": _status_label(job.get("status")),
        "retry_count": job.get("retry_count"),
        "last_error": job.get("last_error"),
        "draft_text": job.get("draft_text") or "",
        "has_draft": bool(job.get("draft_text")),
        "missing_information": job.get("missing_info_json") or [],
        "user_input": job.get("user_input_json") or {},
        "revision_of": job.get("revision_of"),
        "revision_instruction": job.get("revision_instruction"),
        "claimed_by": job.get("claimed_by"),
        "lease_until": job.get("lease_until"),
        "approved_at": job.get("approved_at"),
        "sent_at": job.get("sent_at"),
        "expire_at": job.get("expire_at"),
        "next_attempt_at": job.get("next_attempt_at"),
        "edit_ratio": job.get("edit_ratio"),
        "can": _capabilities(job),
    }
    if include_plan:
        out["plan"] = job.get("plan_json")
    if include_context:
        out["context"] = job.get("context_snapshot")
    return out


def _status_label(status: str) -> str:
    from ..constants import JOB_LABEL
    return JOB_LABEL.get(status, status or "")


def _capabilities(job: dict) -> dict:
    st = job.get("status")
    return {
        "can_claim": st == JOB_QUEUED,
        "can_draft": st in (JOB_GENERATING, JOB_NEEDS_INPUT, JOB_REVIEWING, JOB_READY),
        "can_edit": st in (JOB_READY, JOB_REVIEWING, JOB_APPROVED),
        "can_revise": st in (JOB_READY, JOB_REVIEWING, JOB_APPROVED),
        "can_provide_input": st == JOB_NEEDS_INPUT,
        "can_approve": st in (JOB_READY, JOB_REVIEWING),
        "can_send": st == JOB_APPROVED,
        "can_revoke": st == JOB_APPROVED,
        "can_dismiss": not sm.is_terminal(st or ""),
    }


def counts(repo) -> dict:
    return repo.job_counts()
