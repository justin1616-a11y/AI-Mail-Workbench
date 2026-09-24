# -*- coding: utf-8 -*-
"""Recovery —— 队列自愈（规范 §4 / §27 / §28）。

这是唯一允许用 automation 触发的邮件处理动作（`Mail Draft Recovery`，
每 2~3 小时一次，**没有异常就静默退出**）。本地 scheduler 也会周期性调用它，
所以即使 automation 被关掉，队列也不会卡死。

它负责发现并修复：
    * queued 过久（超过 queued_timeout_seconds）
    * generating 超时 / worker 崩溃留下的过期租约
    * 孤儿任务（claimed_by 有值但状态不是 generating）
    * 卡死的 stale lock
    * failed 且仍可重试的任务
    * ready 太久没人审核（超过 ready_expire_hours）
    * 长期无人处理的 candidate
"""
from __future__ import annotations

from .. import util
from ..constants import (
    JOB_APPROVED, JOB_EXPIRED, JOB_FAILED, JOB_GENERATING, JOB_NEEDS_INPUT,
    JOB_QUEUED, JOB_READY, JOB_REVIEWING, TRIGGER_RECOVERY,
)
from ..workflow import state_machine as sm


def run_recovery(repo, cfg: dict, log=None, dry_run: bool = False) -> dict:
    """跑一遍自愈。无异常时返回 actions=0（调用方应静默退出）。"""
    now = util.now_iso()
    out = {
        "at": now,
        "expired_leases": 0,
        "orphan_claims": 0,
        "stale_queued": 0,
        "retryable_failed": 0,
        "expired_ready": 0,
        "budget_exhausted": 0,
        "expired_candidates": 0,
        "reconciled": 0,
        "actions": 0,
    }
    max_retry = int(cfg.get("max_retry", 3))

    # --- 1) 过期租约：worker 崩溃 / 生成超时 --------------------------------
    rows = repo.db.query(
        "SELECT * FROM draft_jobs WHERE status = ? AND lease_until IS NOT NULL AND lease_until < ?",
        (JOB_GENERATING, now))
    for j in rows:
        out["expired_leases"] += 1
        if dry_run:
            continue
        retry = int(j.get("retry_count") or 0) + 1
        if retry > max_retry:
            sm.transition(repo, j["job_id"], JOB_FAILED, actor="recovery",
                          note="租约过期，重试预算已用尽（%d/%d）" % (retry - 1, max_retry))
            repo.update_job(j["job_id"], {"claimed_by": None, "lease_until": None})
            out["budget_exhausted"] += 1
        else:
            with repo.db.tx() as cur:
                cur.execute(
                    "UPDATE draft_jobs SET status=?, retry_count=?, last_error=?, claimed_by=NULL, "
                    "lease_until=NULL, next_attempt_at=?, updated_at=? WHERE job_id=?",
                    (JOB_QUEUED, retry, "租约过期（worker 可能已崩溃）", now, now, j["job_id"]))
                cur.execute(
                    "INSERT INTO job_events (job_id, ts, from_status, to_status, actor, note) "
                    "VALUES (?,?,?,?,?,?)",
                    (j["job_id"], now, JOB_GENERATING, JOB_QUEUED, "recovery",
                     "租约 %s 过期，重新入队（第 %d 次）" % (j.get("lease_until"), retry)))
        out["actions"] += 1

    # --- 2) 孤儿 claim：claimed_by 有值但状态不是 generating ---------------
    orphans = repo.db.query(
        "SELECT job_id, status, claimed_by FROM draft_jobs "
        "WHERE claimed_by IS NOT NULL AND status NOT IN (?, ?)",
        (JOB_GENERATING, JOB_APPROVED))
    for o in orphans:
        out["orphan_claims"] += 1
        if not dry_run:
            repo.update_job(o["job_id"], {"claimed_by": None, "lease_until": None})
        out["actions"] += 1

    # --- 3) queued 过久 ---------------------------------------------------
    cutoff = util.add_seconds(now, -int(cfg.get("queued_timeout_seconds", 7200)))
    stale = repo.db.query(
        "SELECT job_id, retry_count, created_at FROM draft_jobs "
        "WHERE status = ? AND created_at < ?", (JOB_QUEUED, cutoff))
    for s in stale:
        out["stale_queued"] += 1
        if not dry_run:
            # 抬高优先级并重置 next_attempt_at，让它尽快被处理（不直接丢弃用户请求）
            repo.update_job(s["job_id"], {
                "priority": 99, "next_attempt_at": now,
                "last_error": "排队超过 %ss，已提升优先级重投" % cfg.get("queued_timeout_seconds"),
            })
            repo.log_job_event(s["job_id"], JOB_QUEUED, JOB_QUEUED, actor="recovery",
                               note="排队过久，提升优先级")
        out["actions"] += 1

    # --- 4) failed 但仍可重试 ---------------------------------------------
    failed = repo.db.query(
        "SELECT job_id, retry_count FROM draft_jobs WHERE status = ?", (JOB_FAILED,))
    for f in failed:
        if int(f.get("retry_count") or 0) <= max_retry:
            out["retryable_failed"] += 1
            if not dry_run:
                with repo.db.tx() as cur:
                    cur.execute(
                        "UPDATE draft_jobs SET status=?, next_attempt_at=?, updated_at=? "
                        "WHERE job_id=? AND status=?", (JOB_QUEUED, now, now, f["job_id"], JOB_FAILED))
            out["actions"] += 1

    # --- 5) ready / reviewing 长期无人审核 --------------------------------
    ready_cutoff = util.add_seconds(now, -int(cfg.get("ready_expire_hours", 72)) * 3600)
    stale_ready = repo.db.query(
        "SELECT job_id, created_at FROM draft_jobs WHERE status IN (?, ?) AND updated_at < ?",
        (JOB_READY, JOB_REVIEWING, ready_cutoff))
    for s in stale_ready:
        out["expired_ready"] += 1
        if not dry_run:
            sm.transition(repo, s["job_id"], JOB_EXPIRED, actor="recovery",
                          note="草稿超过 %s 小时无人审核" % cfg.get("ready_expire_hours"))
        out["actions"] += 1

    # --- 6) needs_input 长期未补 ------------------------------------------
    ni_cutoff = util.add_seconds(now, -int(cfg.get("ready_expire_hours", 72)) * 3600)
    stale_ni = repo.db.query(
        "SELECT job_id FROM draft_jobs WHERE status = ? AND updated_at < ?",
        (JOB_NEEDS_INPUT, ni_cutoff))
    for s in stale_ni:
        out["actions"] += 1
        if not dry_run:
            sm.transition(repo, s["job_id"], JOB_EXPIRED, actor="recovery",
                          note="等待用户补信息超时")

    # --- 7) 候选过期 ------------------------------------------------------
    try:
        from ..workflow.candidate_detector import CandidateDetector
        n = 0 if dry_run else CandidateDetector(repo, cfg).expire_stale()
        out["expired_candidates"] = n
        out["actions"] += n
    except Exception:
        pass

    # --- 8) 未知状态归一（版本升级遗留） ----------------------------------
    try:
        n = 0 if dry_run else repo.reconcile_job_statuses(
            lambda s: s if sm.is_valid_state(s) else JOB_FAILED)
        out["reconciled"] = n
        out["actions"] += n
    except Exception:
        pass

    if not dry_run and out["actions"]:
        repo.inc_metric("recovery_actions", out["actions"])
        repo.kv_set("last_recovery", out)

    if log and out["actions"]:
        log("Recovery: %d 项动作 %s" % (out["actions"], out))
    return out


def should_run(repo, cfg: dict, min_interval_seconds: int = 1800) -> bool:
    """给 automation 用的节流：距上次不足 min_interval 就静默退出。"""
    last = repo.kv_get("last_recovery")
    if not last or not last.get("at"):
        return True
    return util.hours_between(last["at"], util.now_iso()) * 3600 >= min_interval_seconds


def summary(repo) -> dict:
    return {"last_recovery": repo.kv_get("last_recovery"),
            "job_counts": repo.job_counts()}
