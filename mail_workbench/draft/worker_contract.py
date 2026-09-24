# -*- coding: utf-8 -*-
"""WorkBuddy Worker Contract（规范 §26）。

WorkBuddy **不直接操作内部文件结构**，只通过下面这组稳定接口交互：

    GET  /api/draft-jobs/next            认领（claim）
    GET  /api/draft-jobs/{id}/context    取最小充分上下文
    POST /api/draft-jobs/{id}/plan       提交 ReplyPlan
    POST /api/draft-jobs/{id}/draft      提交草稿正文
    POST /api/draft-jobs/{id}/fail       报告失败（可重试）
    POST /api/draft-jobs/{id}/heartbeat  续租

对应流程：claim → context → plan → draft → complete
状态由 Mail Workbench 负责，WorkBuddy 只负责理解与写作。

契约里**没有 send 端点** —— 这是故意的（规范 §33）。
"""
from __future__ import annotations

from .. import util
from ..intelligence import reply_planner as rp
from ..thread import context_builder as cbm
from ..workflow import state_machine as sm
from . import queue as dq


class ContractError(Exception):
    pass


# --------------------------------------------------------------------------
# 1) claim
# --------------------------------------------------------------------------
def claim(repo, cfg: dict, worker: str = "workbuddy") -> dict:
    job = dq.claim_next(repo, cfg, worker=worker)
    if not job:
        return {"job": None, "message": "没有待处理任务"}
    return {"job": dq.to_public(job, include_context=False), "worker": worker,
            "lease_until": job.get("lease_until")}


# --------------------------------------------------------------------------
# 2) context
# --------------------------------------------------------------------------
def get_context(repo, cfg: dict, job_id: str, worker: str = None,
                rebuild: bool = False, save: bool = True) -> dict:
    job = repo.get_job(job_id)
    if not job:
        raise ContractError("任务不存在：%s" % job_id)
    if worker and job.get("claimed_by") and job["claimed_by"] != worker:
        raise ContractError("任务被 %s 持有" % job["claimed_by"])
    if job.get("context_snapshot") and not rebuild:
        pkg = job["context_snapshot"]
    else:
        pkg = cbm.MailContextBuilder(repo, cfg).build(
            message_id=job["message_id"], thread_id=job.get("thread_id"),
            draft_mode=job.get("draft_mode"),
            user_input=job.get("user_input_json") or {},
            extra_instruction=job.get("revision_instruction") or "")
        if save and job["status"] == dq.JOB_GENERATING:
            try:
                dq.set_context(repo, job_id, pkg)
            except dq.QueueError:
                pass
    brief = rp.build_brief(pkg, stage="plan")
    brief["job"] = dq.to_public(job, include_context=False)
    brief["gate"] = {
        "suggested_missing_information": rp.suggest_missing_information(pkg),
        "note": "这些类别在上下文中没有可靠依据；请务必列入 missing_information 或由用户补全。",
    }
    return brief


# --------------------------------------------------------------------------
# 3) plan
# --------------------------------------------------------------------------
def submit_plan(repo, cfg: dict, job_id: str, plan: dict, worker: str = None) -> dict:
    job = repo.get_job(job_id)
    if not job:
        raise ContractError("任务不存在：%s" % job_id)
    if job["status"] != dq.JOB_GENERATING:
        raise ContractError("状态 %s 不接受 plan" % job["status"])
    if worker and job.get("claimed_by") and job["claimed_by"] != worker:
        raise ContractError("任务被 %s 持有" % job["claimed_by"])

    ok, errors, normalized = rp.validate_plan(plan or {})
    if not ok:
        raise ContractError("plan 校验失败：%s" % "; ".join(errors))

    pkg = job.get("context_snapshot") or cbm.MailContextBuilder(repo, cfg).build(
        message_id=job["message_id"], thread_id=job.get("thread_id"),
        draft_mode=job.get("draft_mode"), user_input=job.get("user_input_json") or {})
    normalized, enforced = rp.enforce_gate(pkg, normalized)

    updated, needs_input = dq.set_plan(repo, job_id, normalized)
    return {
        "job": dq.to_public(updated, include_plan=True),
        "status": updated["status"],
        "needs_input": needs_input,
        "missing_information": normalized.get("missing_information") or [],
        "enforced_by_gate": enforced,
        "next": ("等待用户补齐信息；补齐后重新 claim 继续起草"
                 if needs_input else "继续调用 /draft 提交正文"),
    }


# --------------------------------------------------------------------------
# 4) draft
# --------------------------------------------------------------------------
def submit_draft(repo, cfg: dict, job_id: str, draft_text: str,
                 draft_mode: str = None, variant: int = None,
                 worker: str = None) -> dict:
    job = repo.get_job(job_id)
    if job and job["status"] == dq.JOB_NEEDS_INPUT:
        raise ContractError("该任务缺信息（needs_input），请等用户补全后再起草")
    try:
        updated = dq.set_draft(repo, job_id, draft_text,
                               draft_mode=rp.normalize_mode(draft_mode) if draft_mode else None,
                               variant=variant)
    except dq.QueueError as e:
        raise ContractError(str(e))
    return {"job": dq.to_public(updated),
            "status": updated["status"],
            "next": "等待人类审核：/api/drafts/{id}/approve -> /send"}


# --------------------------------------------------------------------------
# 5) fail / heartbeat
# --------------------------------------------------------------------------
def submit_fail(repo, cfg: dict, job_id: str, error: str, retryable: bool = True,
                worker: str = None) -> dict:
    try:
        updated = dq.fail(repo, cfg, job_id, error=error, retryable=retryable, worker=worker)
    except dq.QueueError as e:
        raise ContractError(str(e))
    return {"job": dq.to_public(updated), "status": updated["status"],
            "retry_count": updated.get("retry_count"),
            "next_attempt_at": updated.get("next_attempt_at")}


def heartbeat(repo, cfg: dict, job_id: str, worker: str = None) -> dict:
    try:
        updated = dq.heartbeat(repo, cfg, job_id, worker=worker)
    except dq.QueueError as e:
        raise ContractError(str(e))
    return {"job_id": job_id, "lease_until": updated.get("lease_until")}


# --------------------------------------------------------------------------
# 契约文档（供 SKILL.md / README / /api/contract 使用）
# --------------------------------------------------------------------------
CONTRACT_DOC = {
    "version": "2.0",
    "principle": "AI NEVER SENDS EMAIL WITHOUT EXPLICIT HUMAN CONFIRMATION.",
    "flow": ["claim", "context", "plan", "draft", "complete"],
    "endpoints": [
        {"method": "GET", "path": "/api/draft-jobs/next",
         "desc": "原子认领一个 queued 任务（带租约），没有则返回 job=null"},
        {"method": "GET", "path": "/api/draft-jobs/{id}/context",
         "desc": "取 MailContextPackage（最小充分上下文）+ 规划说明"},
        {"method": "POST", "path": "/api/draft-jobs/{id}/plan",
         "desc": "提交 ReplyPlan；若声明缺信息则任务转 needs_input"},
        {"method": "POST", "path": "/api/draft-jobs/{id}/draft",
         "desc": "提交草稿正文；任务转 ready 等人类审核"},
        {"method": "POST", "path": "/api/draft-jobs/{id}/fail",
         "desc": "报告失败；retryable=true 时按指数退避重回 queued"},
        {"method": "POST", "path": "/api/draft-jobs/{id}/heartbeat",
         "desc": "续租，避免长任务被 Recovery 误判"},
    ],
    "forbidden_for_ai": [
        "send email", "delete permanently", "forward externally",
        "change account credentials", "call /api/drafts/{id}/send",
    ],
    "human_only": [
        "POST /api/drafts/{id}/approve （生成一次性确认令牌）",
        "POST /api/drafts/{id}/send    （带令牌才真正投递）",
    ],
    "state_machine": sm.describe(),
}


def contract_doc() -> dict:
    return CONTRACT_DOC


def stats(repo) -> dict:
    return {"jobs": repo.job_counts(), "generated_at": util.now_iso()}
