# -*- coding: utf-8 -*-
"""Candidate Detector —— Candidate 与 DraftJob 必须分离（规范 §6）。

这是 V2 最重要的设计之一：

    收到邮件 -> Candidate Detector -> Reply Candidate
                                        │
                          （只有下面两种情况才继续）
                                        ▼
                                   DraftJob -> WorkBuddy

Candidate 的含义只是「**这封邮件可能需要用户回复**」，
它**不代表必须调用 AI**。这样能防止「所有新邮件都调用 LLM」，
把 token 花在真正需要语言理解的地方（P0-3 AI On Demand）。

只有：
  1. 用户点击「AI 起草」
  2. 或规则明确允许自动预备草稿（`auto_prepare_drafts=true` 且该邮件是 IMPORTANT）
才把 Candidate 提升为 DraftJob。
"""
from __future__ import annotations

from .. import util
from ..constants import (
    CLASS_DISMISS, CLASS_IMPORTANT, CLASS_PRIORITY, CLASS_READ, CLASS_REPLY,
    CLASS_SYSTEM, TRIGGER_RECOVERY, TRIGGER_RULE, WF_ARCHIVED, WF_DONE, WF_IGNORED,
)
from . import state_machine as sm


class CandidateDetector:
    def __init__(self, repo, cfg: dict, log=None):
        self.repo = repo
        self.cfg = cfg
        self.log = log
        self.self_set = {a.lower() for a in (cfg.get("self_addresses") or [])}

    # ------------------------------------------------------------------
    def is_inbound(self, msg: dict) -> bool:
        return (msg.get("from_addr") or "").lower() not in self.self_set

    def should_consider(self, msg: dict) -> tuple:
        """返回 (bool, reason)。全部是确定性判断，无 AI。"""
        if not msg:
            return False, "邮件不存在"
        if not self.is_inbound(msg):
            return False, "我方发出的邮件"
        if msg.get("deleted"):
            return False, "已标记删除"
        wf = msg.get("workflow_state") or "NEW"
        if wf in (WF_DONE, WF_IGNORED, WF_ARCHIVED):
            return False, "工作流已终结（%s）" % wf
        cls = msg.get("classification")
        if cls is None:
            return False, "尚未分类"
        if cls == CLASS_SYSTEM:
            return False, "系统类邮件：应去对应系统处理，不需要回邮件"
        if cls in (CLASS_READ, CLASS_DISMISS):
            return False, "分类为 %s，不进候选" % cls
        if cls not in (CLASS_IMPORTANT, CLASS_REPLY):
            return False, "分类 %s 不产生候选" % cls
        if msg.get("answered"):
            return False, "服务器已标记 \\Answered"
        # 我方已经在这个线程里回过、且回得比对方新 -> 不算待回
        tid = msg.get("thread_id")
        if tid:
            thread = self.repo.get_thread(tid)
            if thread and thread.get("waiting_for_other"):
                return False, "最新一封是我方发出，等对方回复"
        return True, "分类=%s，来自 %s" % (cls, util.redact(msg.get("from_addr")))

    # ------------------------------------------------------------------
    def consider(self, message_id: str, trigger_source: str = TRIGGER_RULE) -> dict:
        """评估一封邮件是否需要回复。可能返回：
            {"kind":"skip",   "reason":...}
            {"kind":"existing_job", "job":...}     已有活跃草稿任务
            {"kind":"job",    "job":...}           规则允许自动预备，已建任务
            {"kind":"candidate","candidate":...}   只建候选，不调 AI
        """
        msg = self.repo.get_message(message_id)
        if not msg:
            return {"kind": "skip", "reason": "邮件不在本地库"}

        # 1) 已有活跃 DraftJob -> 幂等返回，绝不重复调用 WorkBuddy（§25）
        job = self.repo.find_job_by_message(message_id, active_only=True)
        if job:
            return {"kind": "existing_job", "job": job,
                    "reason": "已有活跃草稿任务 %s（%s）" % (job["job_id"], job["status"])}

        ok, reason = self.should_consider(msg)
        if not ok:
            return {"kind": "skip", "reason": reason}

        # 2) 已有候选 -> 复用
        cand, created = self.repo.create_candidate({
            "message_id": message_id,
            "thread_id": msg.get("thread_id"),
            "account": msg.get("account"),
            "folder": msg.get("folder"),
            "sender": msg.get("from_addr"),
            "recipients": [self.cfg.get("user")],
            "subject": msg.get("subject"),
            "priority": CLASS_PRIORITY.get(msg.get("classification"), 50),
            "trigger_source": trigger_source,
            "reason": reason,
        })
        self.repo.inc_metric("reply_candidates")
        self._touch_contact(msg)

        # 3) 规则允许时才自动升级为 DraftJob
        if self.auto_prepare_allowed(msg):
            from ..draft import queue as dq
            job = dq.ensure_job(self.repo, self.cfg, message_id,
                                trigger_source="rule_auto",
                                draft_mode=self.cfg.get("default_draft_mode", "normal"))
            if job and job.get("job_id"):
                job = dq.transition_candidate(repo=self.repo, candidate_id=cand["candidate_id"],
                                              job_id=job["job_id"])
                return {"kind": "job", "job": job,
                        "candidate": cand,
                        "reason": "规则允许自动预备草稿，" + reason}
        return {"kind": "candidate", "candidate": cand,
                "created": created, "reason": reason}

    def auto_prepare_allowed(self, msg: dict) -> bool:
        """默认关闭：Candidate 只标记，不自动调 AI。"""
        if not self.cfg.get("auto_prepare_drafts", False):
            return False
        return msg.get("classification") == CLASS_IMPORTANT

    def _touch_contact(self, msg: dict) -> None:
        try:
            self.repo.touch_contact(msg.get("from_addr"), msg.get("from_name"),
                                    when_iso=msg.get("date_iso"))
        except Exception:
            pass

    # ------------------------------------------------------------------
    def scan(self, limit: int = 200, trigger_source: str = TRIGGER_RULE,
             since_days: int = None) -> dict:
        """对最近入库的邮件跑一遍候选检测（同步结束后调用，纯本地）。

        `since_days` 默认取 cfg.action_window_days：候选的含义是「这封邮件可能
        需要回复」，两年前的旧邮件不该产生候选 —— 否则首次全量同步会一次
        造出上千个永远无人问津的候选（实测 1422 个）。设 0 表示不限制。
        """
        if since_days is None:
            try:
                since_days = int(self.cfg.get("action_window_days", 30))
            except (TypeError, ValueError):
                since_days = 30
        sql = ("SELECT message_id FROM messages WHERE source='imap' AND deleted=0 ")
        params = []
        if since_days and since_days > 0:
            cutoff = util.add_seconds(util.now_iso(), -since_days * 86400)
            sql += "AND (unread=1 OR internal_ts >= ?) "
            params.append(util.to_epoch(cutoff))
        sql += "ORDER BY internal_ts DESC LIMIT ?"
        params.append(int(limit))
        rows = self.repo.db.query(sql, params)
        out = {"scanned": 0, "candidates": 0, "jobs": 0, "skipped": 0, "reused": 0}
        for r in rows:
            out["scanned"] += 1
            res = self.consider(r["message_id"], trigger_source=trigger_source)
            k = res.get("kind")
            if k == "candidate":
                out["candidates"] += 1
            elif k == "job":
                out["jobs"] += 1
            elif k == "existing_job":
                out["reused"] += 1
            else:
                out["skipped"] += 1
        return out

    def dismiss(self, candidate_id: str, reason: str = "") -> bool:
        cand = self.repo.get_candidate(candidate_id)
        if not cand:
            return False
        self.repo.set_candidate_status(candidate_id, "dismissed")
        return True

    def expire_stale(self, older_than_hours: int = 24 * 14) -> int:
        """归档候选，避免候选表无限膨胀。

        两条判据，满足任一即归档（有活跃草稿任务的永远保留）：
          1. 候选本身已创建超过 older_than_hours
          2. 对应邮件已超出工作集时间窗（已读 + 早于 action_window_days）

        判据 2 是必需的：`created_at` 只能挡住「慢慢变旧的候选」，
        挡不住「历史邮件一次性灌进来」——那批候选的 created_at 都是当下，
        但底层邮件是几年前的，同样应该退出。
        """
        active = ("candidate", "queued", "generating", "needs_input",
                  "ready", "reviewing", "approved")
        ph = ",".join("?" * len(active))
        created_cutoff = util.add_seconds(util.now_iso(), -older_than_hours * 3600)
        try:
            days = int(self.cfg.get("action_window_days", 30))
        except (TypeError, ValueError):
            days = 30
        args = [util.now_iso(), created_cutoff]
        window_clause = ""
        if days and days > 0:
            window_clause = (
                " OR message_id IN (SELECT message_id FROM messages "
                "   WHERE unread = 0 AND internal_ts < ?)")
            args.append(util.to_epoch(util.add_seconds(util.now_iso(), -days * 86400)))
        args.extend(active)
        cur = self.repo.db.execute(
            "UPDATE candidates SET status='expired', updated_at=? "
            "WHERE status='new' AND (created_at < ?" + window_clause + ") "
            "AND message_id NOT IN (SELECT message_id FROM draft_jobs "
            "                       WHERE status IN (%s) AND message_id IS NOT NULL)" % ph,
            tuple(args))
        return cur.rowcount or 0
