# -*- coding: utf-8 -*-
"""Mail Workbench V2 测试套件。

覆盖三类：
  A. V2 新机制：状态机合法性、候选/任务分离、幂等、租约、退避重试、
     Missing Information Gate、上下文最小性、线程聚合、工作桶、发送门禁。
  B. V1 功能回归（规范 §37）：文件夹、未读、正文解析、附件、搜索、星标、
     删除/撤销、SMTP 已发送副本逻辑。
  C. 性能（规范 §38）：常见 UI 操作在 5000 封规模下 < 200ms。

全部测试**不联网**：IMAP/SMTP 一律用桩替换。
运行：
    python -m unittest discover -s mail_workbench/tests -v
"""
from __future__ import annotations

import json
import os
import socket
import struct
import sys
import tempfile
import time
import unittest
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.dirname(HERE)          # mail_workbench/
PROJ_ROOT = os.path.dirname(PKG_ROOT)     # Mails/
if PROJ_ROOT not in sys.path:
    sys.path.insert(0, PROJ_ROOT)

from mail_workbench import config as cfgmod            # noqa: E402
from mail_workbench import metrics as metricsmod       # noqa: E402
from mail_workbench import util                        # noqa: E402
from mail_workbench.constants import (                 # noqa: E402
    BUCKET_STALE_WAIT, BUCKETS, BUCKETS_ALL, CLASS_DISMISS, CLASS_IMPORTANT,
    CLASS_READ, CLASS_REPLY, CLASS_SYSTEM,
    JOB_APPROVED, JOB_DISMISSED, JOB_EXPIRED, JOB_FAILED, JOB_GENERATING,
    JOB_NEEDS_INPUT, JOB_QUEUED, JOB_READY, JOB_REVIEWING, JOB_SENT, JOB_STATUSES,
    WF_ARCHIVED, WF_DONE, WF_IGNORED, WF_WAITING_FOR_ME, WF_WAITING_FOR_OTHER,
)
from mail_workbench.draft import queue as dq           # noqa: E402
from mail_workbench.draft import recovery as rcv       # noqa: E402
from mail_workbench.draft import templates as dtpl     # noqa: E402
from mail_workbench.draft import worker_contract as wc  # noqa: E402
from mail_workbench.intelligence import fact_extractor as fx   # noqa: E402
from mail_workbench.intelligence import reply_planner as rp    # noqa: E402
from mail_workbench.intelligence import rule_engine    # noqa: E402
from mail_workbench.mail import attachment_store as atstore  # noqa: E402
from mail_workbench.mail import foxmail_index          # noqa: E402
from mail_workbench.mail import imap_client            # noqa: E402
from mail_workbench.mail import parser as mparser      # noqa: E402
from mail_workbench.mail import smtp_client            # noqa: E402
from mail_workbench.storage import search as searchmod  # noqa: E402
from mail_workbench.storage.database import open_db    # noqa: E402
from mail_workbench.storage.repo import Repo           # noqa: E402
from mail_workbench.thread import aggregator as agg    # noqa: E402
from mail_workbench.thread import context_builder as cbm  # noqa: E402
from mail_workbench.workflow import actions as actionsmod  # noqa: E402
from mail_workbench.workflow import brief as briefmod  # noqa: E402
from mail_workbench.workflow import buckets as bucketmod   # noqa: E402
from mail_workbench.workflow import followup_manager as fum  # noqa: E402
from mail_workbench.workflow import snooze_manager as snzm   # noqa: E402
from mail_workbench.workflow import state_machine as sm     # noqa: E402
from mail_workbench.workflow.candidate_detector import CandidateDetector  # noqa: E402

SELF = "me@example.edu"

# 真实实现必须在**导入时**抓一份，不能等到 setUp 里去抓。
#
# 踩过的坑（本文件自己造成的）：unittest 的执行顺序是
#     setUp -> test -> tearDown -> addCleanup
# 于是「在测试里注册的 cleanup」会**晚于** tearDown 执行。
# 若某个测试在 setup 阶段先装了打桩、再把当前值当成「原始值」存下来，
# 它的 cleanup 就会在 tearDown 已经把真身装回去之后，**再盖上一个打桩函数**，
# 后面所有测试都在「假装成功」的环境里跑 ——
# 表现为「单跑通过、全量跑却失败」，极难查。
_REAL_IMAP_STORE = actionsmod._imap_store
_REAL_IMAP_MOVE = actionsmod._imap_move_to_archive


def make_cfg(db_path: str, **over) -> dict:
    cfg = dict(cfgmod.DEFAULT_CONFIG)
    cfg.update({
        "user": SELF,
        "pass": "test-pass",
        "self_addresses": [SELF],
        "db_path": db_path,
        "logs_dir": os.path.join(os.path.dirname(db_path), "logs"),
        "data_dir": os.path.dirname(db_path),
        # ⚠️ attachments_dir 必须跟 db 一起落在临时目录里。
        # 不设它的后果是悄悄往**项目根的 attachments/** 写东西（attachment_store 会
        # 回落到 <project_root>/attachments）：跑一遍测试，测试固件（7 字节的
        # "PDFDATA"）就躺在 attachments/ 里了。发布目录里跑过一次测试、接着打包，
        # 这些垃圾就跟着发出去 —— `.gitignore` 管得住 git，管不住打包/上传那一步。
        # 已实测：在 mail-workbench-release 里跑完 unittest，
        # attachments/<sha16>_简历.pdf 与 _report.pdf 各一份。
        "attachments_dir": os.path.join(os.path.dirname(db_path), "attachments"),
        "ui_dir": os.path.join(PKG_ROOT, "ui"),
        "project_root": PROJ_ROOT,
        "workspace_root": os.path.dirname(PROJ_ROOT),
        "foxmail_enabled": False,
        "auto_prepare_drafts": False,
    })
    cfg.update(over)
    return cfg


def mk_msg(message_id, subject, from_addr, from_name="张三", to=None,
           date_iso="2026-09-18T10:00:00+08:00", body="你好", classification=None,
           uid=None, folder="INBOX", flags=None):
    flags = flags or {}
    return {
        "message_id": message_id,
        "account": SELF,
        "folder": folder,
        "uid": uid,
        "subject": subject,
        "from_addr": from_addr,
        "from_name": from_name,
        "to_addrs": to or [SELF],
        "cc_addrs": [],
        "date_iso": date_iso,
        "internal_ts": util.to_epoch(date_iso),
        "unread": flags.get("unread", True),
        "answered": flags.get("answered", False),
        "flagged": flags.get("flagged", False),
        "deleted": flags.get("deleted", False),
        "has_attachments": flags.get("has_attachments", False),
        "attachment_names": flags.get("attachment_names", []),
        "body_text": body,
        "classification": classification,
        # 群发通知标记（规则引擎产出）。测试里可直接指定，省去造规则文件。
        "is_broadcast": flags.get("broadcast", False),
        "source": "imap",
        "in_reply_to": "",
        "references_ids": [],
    }


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmp.name, "t.db")
        self.db = open_db(self.db_path)
        self.repo = Repo(self.db)
        self.cfg = make_cfg(self.db_path)
        # 默认把 IMAP 写操作打桩，避免测试联网。
        # 还原时用**模块级**的真身（见文件顶部的说明），不要用 self.* 存下来的值。
        actionsmod._imap_store = lambda *a, **k: {"ok": True}
        actionsmod._imap_move_to_archive = lambda *a, **k: {"ok": True, "moved_to": "Archive"}

    def tearDown(self):
        actionsmod._imap_store = _REAL_IMAP_STORE
        actionsmod._imap_move_to_archive = _REAL_IMAP_MOVE
        try:
            self.db.close()
        except Exception:
            pass
        self.tmp.cleanup()

    # ---------------- helpers ----------------
    def add(self, msg, thread=True):
        if thread:
            res = agg.attach_message(self.repo, msg, self.cfg)
            return res["thread_id"]
        self.repo.upsert_message(msg)
        return None

    def add_reply_thread(self, n=3):
        """构造一个「对方来信 -> 我方回信 -> 对方再来信」的线程。"""
        t1 = self.add(mk_msg("<m1@x>", "关于产学研申报", "office@example.edu",
                             "科研办", date_iso="2026-09-15T09:00:00+08:00",
                             body="请于 9/23 12:00 前提交材料。需要你确认是否申报。",
                             classification=CLASS_IMPORTANT))
        self.add(mk_msg("<m2@x>", "Re: 关于产学研申报", SELF, "本人",
                        to=["office@example.edu"],
                        date_iso="2026-09-16T09:00:00+08:00",
                        body="我会在 9/20 前反馈。", classification=None))
        self.add(mk_msg("<m3@x>", "Re: 关于产学研申报", "office@example.edu",
                        "科研办", date_iso="2026-09-17T09:00:00+08:00",
                        body="补充：院系限报 1 项，请确认申报方向？",
                        classification=CLASS_IMPORTANT))
        return t1


# ==========================================================================
# A1. 状态机
# ==========================================================================
class TestStateMachine(Base):
    def test_spec_legal_transitions(self):
        legal = [
            (JOB_QUEUED, JOB_GENERATING), (JOB_GENERATING, JOB_READY),
            (JOB_READY, JOB_REVIEWING), (JOB_REVIEWING, JOB_APPROVED),
            (JOB_APPROVED, JOB_SENT),
            ("candidate", JOB_QUEUED), ("candidate", JOB_DISMISSED),
            ("candidate", JOB_EXPIRED),
            (JOB_QUEUED, JOB_FAILED), (JOB_GENERATING, JOB_FAILED),
            (JOB_FAILED, JOB_QUEUED), (JOB_READY, JOB_EXPIRED),
            (JOB_GENERATING, JOB_NEEDS_INPUT), (JOB_NEEDS_INPUT, JOB_QUEUED),
        ]
        for a, b in legal:
            self.assertTrue(sm.can_transition(a, b), "%s -> %s 应为合法" % (a, b))

    def test_illegal_transitions_rejected(self):
        illegal = [
            (JOB_GENERATING, JOB_SENT),      # AI 不能直达发送
            (JOB_QUEUED, JOB_SENT),
            (JOB_QUEUED, JOB_APPROVED),
            (JOB_READY, JOB_SENT),           # 必须先 approved
            (JOB_SENT, JOB_QUEUED),          # 终态
            (JOB_DISMISSED, JOB_QUEUED),
            (JOB_EXPIRED, JOB_READY),
            (JOB_NEEDS_INPUT, JOB_READY),    # 必须先回 queued
            (JOB_GENERATING, JOB_APPROVED),
        ]
        for a, b in illegal:
            self.assertFalse(sm.can_transition(a, b), "%s -> %s 应非法" % (a, b))
            with self.assertRaises(sm.IllegalTransition):
                sm.assert_transition(a, b)

    def test_only_approved_can_send(self):
        self.assertEqual(set(sm.SEND_ALLOWED_STATES), {JOB_APPROVED})
        for st in JOB_STATUSES:
            self.assertEqual(sm.can_send(st), st == JOB_APPROVED)

    def test_terminal_states_locked(self):
        for st in (JOB_SENT, JOB_DISMISSED, JOB_EXPIRED):
            self.assertTrue(sm.is_terminal(st))
            self.assertEqual(sm.TRANSITIONS[st], set())

    def test_transition_writes_audit(self):
        self.add(mk_msg("<a1@x>", "s", "a@b.com", classification=CLASS_REPLY))
        job = dq.ensure_job(self.repo, self.cfg, self.repo.db.query_one(
            "SELECT message_id FROM messages LIMIT 1")["message_id"])
        dq.claim_next(self.repo, self.cfg, worker="w1")
        dq.set_draft(self.repo, job["job_id"], "你好")
        events = self.repo.job_events(job["job_id"])
        chain = [(e["from_status"], e["to_status"]) for e in events]
        self.assertIn((JOB_QUEUED, JOB_GENERATING), chain)
        self.assertIn((JOB_GENERATING, JOB_READY), chain)


# ==========================================================================
# A2. Candidate 与 DraftJob 分离
# ==========================================================================
class TestCandidateSeparation(Base):
    def test_reply_mail_creates_candidate_not_job(self):
        self.add(mk_msg("<c1@x>", "导师双选申请", "student@example.com", "王同学",
                        classification=CLASS_REPLY))
        det = CandidateDetector(self.repo, self.cfg)
        res = det.consider(self.repo.db.query_one(
            "SELECT message_id FROM messages LIMIT 1")["message_id"])
        self.assertEqual(res["kind"], "candidate")
        self.assertEqual(self.repo.job_counts().get(JOB_QUEUED, 0), 0,
                         "Candidate 不应该自动创建 DraftJob（不能白白调用 LLM）")
        self.assertEqual(len(self.repo.list_candidates(status="new")), 1)

    def test_system_and_dismiss_not_candidates(self):
        self.add(mk_msg("<s1@x>", "待办提醒", "application.infoplus@sjtu.edu.cn",
                        "交我办", classification=CLASS_SYSTEM))
        self.add(mk_msg("<s2@x>", "营销", "promo@spam.top", "营销", classification=CLASS_DISMISS))
        self.add(mk_msg("<s3@x>", "期刊约稿", "news@mdpi.com", "MDPI", classification=CLASS_READ))
        det = CandidateDetector(self.repo, self.cfg)
        kinds = {det.consider(r["message_id"])["kind"]
                 for r in self.repo.db.query("SELECT message_id FROM messages")}
        self.assertEqual(kinds, {"skip"})

    def test_outbound_never_candidate(self):
        self.add(mk_msg("<o1@x>", "我发出去的", SELF, "本人",
                        to=["a@b.com"], classification=CLASS_REPLY))
        det = CandidateDetector(self.repo, self.cfg)
        res = det.consider(self.repo.db.query_one(
            "SELECT message_id FROM messages LIMIT 1")["message_id"])
        self.assertEqual(res["kind"], "skip")
        self.assertIn("我方", res["reason"])

    def test_auto_prepare_rule_can_escalate(self):
        cfg = dict(self.cfg, auto_prepare_drafts=True)
        self.add(mk_msg("<c2@x>", "重要通知", "office@example.edu", "科研办",
                        classification=CLASS_IMPORTANT))
        det = CandidateDetector(self.repo, cfg)
        res = det.consider(self.repo.db.query_one(
            "SELECT message_id FROM messages LIMIT 1")["message_id"])
        self.assertEqual(res["kind"], "job")
        self.assertEqual(self.repo.job_counts().get(JOB_QUEUED, 0), 1)
        self.assertEqual(len(self.repo.list_candidates(status="converted")), 1)

    def test_scan_respects_working_set_window(self):
        """首次全量同步不得为历史邮件批量造候选（否则一次多出上千条）。"""
        self.cfg["action_window_days"] = 30
        # 两年前的已读邮件：不该产生候选
        self.add(mk_msg("<h1@x>", "两年前的旧信", "a@b.com", "张三",
                        classification=CLASS_REPLY,
                        date_iso="2024-01-01T09:00:00+08:00",
                        flags={"unread": False}))
        # 两年前的未读邮件：未读就是还没处理，仍要进候选
        self.add(mk_msg("<h2@x>", "两年前的未读", "b@b.com", "李四",
                        classification=CLASS_REPLY,
                        date_iso="2024-01-02T09:00:00+08:00",
                        flags={"unread": True}))
        # 最近的要回邮件
        self.add(mk_msg("<h3@x>", "最近的", "c@b.com", "王五",
                        classification=CLASS_REPLY,
                        date_iso="2026-09-21T09:00:00+08:00"))
        det = CandidateDetector(self.repo, self.cfg)
        out = det.scan(limit=500)
        self.assertEqual(out["scanned"], 2, "超出窗口的已读邮件不该进扫描")
        self.assertEqual(out["candidates"], 2)
        mids = {c["message_id"] for c in self.repo.list_candidates(status="new")}
        self.assertNotIn("<h1@x>", mids)

    def test_expire_stale_releases_out_of_window_candidates(self):
        """历史邮件灌进来的候选（created_at 是当下但邮件很旧）必须能被归档。"""
        self.cfg["action_window_days"] = 30
        self.add(mk_msg("<e1@x>", "旧信", "a@b.com", classification=CLASS_REPLY,
                        date_iso="2024-06-01T09:00:00+08:00",
                        flags={"unread": False}))
        self.add(mk_msg("<e2@x>", "新信", "b@b.com", classification=CLASS_REPLY,
                        date_iso="2026-09-21T09:00:00+08:00"))
        det = CandidateDetector(self.repo, self.cfg)
        # 绕过 scan 的窗口过滤，直接造出「旧邮件也有候选」的坏状态
        for mid in ("<e1@x>", "<e2@x>"):
            det.repo.create_candidate({"message_id": mid, "sender": "a@b.com"})
        self.assertEqual(
            det.repo.db.scalar("SELECT COUNT(*) FROM candidates WHERE status='new'"), 2)
        n = det.expire_stale()
        self.assertEqual(n, 1, "只应归档那封超出窗口的旧信候选")
        left = self.repo.list_candidates(status="new")
        self.assertEqual([c["message_id"] for c in left], ["<e2@x>"])

    def test_expire_stale_never_touches_active_job_candidates(self):
        """有活跃草稿任务的候选永远保留（用户正在处理它）。"""
        self.cfg["action_window_days"] = 30
        self.add(mk_msg("<aj@x>", "旧信但已有任务", "a@b.com", classification=CLASS_REPLY,
                        date_iso="2024-06-01T09:00:00+08:00",
                        flags={"unread": False}))
        det = CandidateDetector(self.repo, self.cfg)
        cand, _ = self.repo.create_candidate({"message_id": "<aj@x>", "sender": "a@b.com"})
        dq.ensure_job(self.repo, self.cfg, "<aj@x>")
        det.expire_stale()
        self.assertIsNotNone(self.repo.get_candidate(cand["candidate_id"]))
        self.assertEqual(self.repo.get_candidate(cand["candidate_id"])["status"], "new")

    def test_already_replied_thread_not_candidate(self):
        tid = self.add_reply_thread()
        # 我方又回了一封（走 add() 以正确挂到同一线程上）-> 最新一封是我方发出
        self.add(mk_msg("<m4@x>", "Re: 关于产学研申报", SELF, "本人",
                        to=["office@example.edu"],
                        date_iso="2026-09-18T09:00:00+08:00",
                        body="已提交。"))
        latest = self.repo.latest_in_thread(tid)
        self.assertEqual(latest["message_id"], "<m4@x>")
        self.assertTrue(self.repo.get_thread(tid)["waiting_for_other"])
        det = CandidateDetector(self.repo, self.cfg)
        res = det.consider(latest["message_id"])
        self.assertEqual(res["kind"], "skip")
        self.assertIn("我方", res["reason"])


# ==========================================================================
# A3. 幂等 / 重复草稿保护（§25）
# ==========================================================================
class TestIdempotency(Base):
    def setUp(self):
        super().setUp()
        self.add(mk_msg("<i1@x>", "需要回复的邮件", "a@b.com", classification=CLASS_REPLY))
        self.mid = self.repo.db.query_one("SELECT message_id FROM messages LIMIT 1")["message_id"]

    def test_ensure_job_same_job_id(self):
        j1 = dq.ensure_job(self.repo, self.cfg, self.mid, trigger_source="manual")
        j2 = dq.ensure_job(self.repo, self.cfg, self.mid, trigger_source="manual")
        j3 = dq.ensure_job(self.repo, self.cfg, self.mid, trigger_source="manual")
        self.assertEqual(j1["job_id"], j2["job_id"])
        self.assertEqual(j2["job_id"], j3["job_id"])
        self.assertEqual(self.repo.job_counts().get(JOB_QUEUED, 0), 1)
        self.assertGreaterEqual(self.repo.all_metrics().get("draft_jobs_deduped", 0), 2)

    def test_consider_returns_existing_job(self):
        j = dq.ensure_job(self.repo, self.cfg, self.mid)
        res = CandidateDetector(self.repo, self.cfg).consider(self.mid)
        self.assertEqual(res["kind"], "existing_job")
        self.assertEqual(res["job"]["job_id"], j["job_id"])

    def test_idempotency_key_shape(self):
        self.assertEqual(dq.idempotency_key(self.cfg, "<i1@x>"),
                         "draft:%s:<i1@x>" % SELF)

    def test_terminal_job_allows_new_one(self):
        j = dq.ensure_job(self.repo, self.cfg, self.mid)
        dq.dismiss(self.repo, j["job_id"])
        j2 = dq.ensure_job(self.repo, self.cfg, self.mid)
        self.assertNotEqual(j["job_id"], j2["job_id"])


# ==========================================================================
# A4. 租约 / claim / 超时恢复（§27）
# ==========================================================================
class TestLeaseAndClaim(Base):
    def setUp(self):
        super().setUp()
        self.add(mk_msg("<l1@x>", "任务A", "a@b.com", classification=CLASS_REPLY))
        self.mid = self.repo.db.query_one("SELECT message_id FROM messages LIMIT 1")["message_id"]
        self.job = dq.ensure_job(self.repo, self.cfg, self.mid)

    def test_claim_is_exclusive(self):
        j1 = dq.claim_next(self.repo, self.cfg, worker="w1")
        self.assertIsNotNone(j1)
        self.assertEqual(j1["status"], JOB_GENERATING)
        self.assertEqual(j1["claimed_by"], "w1")
        j2 = dq.claim_next(self.repo, self.cfg, worker="w2")
        self.assertIsNone(j2, "同一任务不能被两个 worker 同时认领")

    def test_claim_returns_none_when_empty(self):
        dq.claim_next(self.repo, self.cfg, worker="w1")
        dq.set_draft(self.repo, self.job["job_id"], "draft")
        self.assertIsNone(dq.claim_next(self.repo, self.cfg, worker="w1"))

    def test_heartbeat_extends_lease(self):
        j = dq.claim_next(self.repo, self.cfg, worker="w1")
        before = j["lease_until"]
        time.sleep(0.05)
        after = dq.heartbeat(self.repo, self.cfg, j["job_id"], worker="w1")["lease_until"]
        self.assertGreaterEqual(after, before)

    def test_heartbeat_rejects_other_worker(self):
        j = dq.claim_next(self.repo, self.cfg, worker="w1")
        with self.assertRaises(dq.QueueError):
            dq.heartbeat(self.repo, self.cfg, j["job_id"], worker="w2")

    def test_expired_lease_requeued_by_recovery(self):
        j = dq.claim_next(self.repo, self.cfg, worker="crash")
        # 手动把租约调到过去，模拟 worker 崩溃
        self.repo.update_job(j["job_id"], {"lease_until": util.add_seconds(util.now_iso(), -60)})
        res = rcv.run_recovery(self.repo, self.cfg)
        self.assertEqual(res["expired_leases"], 1)
        again = self.repo.get_job(j["job_id"])
        self.assertEqual(again["status"], JOB_QUEUED)
        self.assertIsNone(again["claimed_by"])
        self.assertEqual(again["retry_count"], 1)
        self.assertIsNotNone(dq.claim_next(self.repo, self.cfg, worker="w2"))

    def test_expired_lease_exhausts_budget(self):
        j = dq.claim_next(self.repo, self.cfg, worker="crash")
        self.repo.update_job(j["job_id"], {
            "lease_until": util.add_seconds(util.now_iso(), -60),
            "retry_count": 99})
        res = rcv.run_recovery(self.repo, self.cfg)
        self.assertEqual(res["budget_exhausted"], 1)
        self.assertEqual(self.repo.get_job(j["job_id"])["status"], JOB_FAILED)

    def test_orphan_claim_cleanup(self):
        j = dq.claim_next(self.repo, self.cfg, worker="w1")
        # 状态被改成 ready 但 claimed_by 残留 -> 孤儿
        self.repo.update_job(j["job_id"], {"status": JOB_READY})
        res = rcv.run_recovery(self.repo, self.cfg)
        self.assertGreaterEqual(res["orphan_claims"], 1)
        self.assertIsNone(self.repo.get_job(j["job_id"])["claimed_by"])


# ==========================================================================
# A5. 重试与指数退避（§28）
# ==========================================================================
class TestRetryBackoff(Base):
    def setUp(self):
        super().setUp()
        self.add(mk_msg("<r1@x>", "失败重试", "a@b.com", classification=CLASS_REPLY))
        self.mid = self.repo.db.query_one("SELECT message_id FROM messages LIMIT 1")["message_id"]
        self.job = dq.ensure_job(self.repo, self.cfg, self.mid)

    def test_retryable_failure_requeues_with_backoff(self):
        dq.claim_next(self.repo, self.cfg, worker="w1")
        j = dq.fail(self.repo, self.cfg, self.job["job_id"], "模型超时", retryable=True)
        self.assertEqual(j["status"], JOB_QUEUED)
        self.assertEqual(j["retry_count"], 1)
        self.assertGreater(util.to_epoch(j["next_attempt_at"]), util.to_epoch(util.now_iso()))
        # 退避期内不可认领
        self.assertIsNone(dq.claim_next(self.repo, self.cfg, worker="w1"))

    def test_backoff_grows(self):
        delays = []
        for _ in range(3):
            self.repo.update_job(self.job["job_id"], {
                "status": JOB_GENERATING, "next_attempt_at": util.now_iso()})
            j = dq.fail(self.repo, self.cfg, self.job["job_id"], "x", retryable=True)
            delays.append(util.hours_between(util.now_iso(), j["next_attempt_at"]) * 3600)
        self.assertLess(delays[0], delays[1])
        self.assertLess(delays[1], delays[2])

    def test_max_retry_stops(self):
        max_retry = int(self.cfg["max_retry"])
        for i in range(max_retry):
            self.repo.update_job(self.job["job_id"], {
                "status": JOB_GENERATING, "next_attempt_at": util.now_iso()})
            dq.fail(self.repo, self.cfg, self.job["job_id"], "fail %d" % i, retryable=True)
        self.repo.update_job(self.job["job_id"], {"status": JOB_GENERATING})
        j = dq.fail(self.repo, self.cfg, self.job["job_id"], "final", retryable=True)
        self.assertEqual(j["status"], JOB_FAILED, "超过 max_retry 必须停下，禁止无限重试")
        self.assertEqual(j["retry_count"], max_retry + 1)

    def test_non_retryable_stops_immediately(self):
        dq.claim_next(self.repo, self.cfg, worker="w1")
        j = dq.fail(self.repo, self.cfg, self.job["job_id"], "永久错误", retryable=False)
        self.assertEqual(j["status"], JOB_FAILED)
        self.assertIsNone(j["next_attempt_at"])

    def test_recovery_requeues_retryable_failed(self):
        dq.claim_next(self.repo, self.cfg, worker="w1")
        dq.fail(self.repo, self.cfg, self.job["job_id"], "x", retryable=False)
        res = rcv.run_recovery(self.repo, self.cfg)
        self.assertGreaterEqual(res["retryable_failed"], 1)
        self.assertEqual(self.repo.get_job(self.job["job_id"])["status"], JOB_QUEUED)


# ==========================================================================
# A6. Missing Information Gate（§10）
# ==========================================================================
class TestMissingInfoGate(Base):
    def _job_for(self, body):
        self.add(mk_msg("<g1@x>", "需要信息的邮件", "a@b.com", classification=CLASS_REPLY,
                        body=body))
        mid = self.repo.db.query_one("SELECT message_id FROM messages LIMIT 1")["message_id"]
        return dq.ensure_job(self.repo, self.cfg, mid)

    def test_plan_with_missing_info_goes_needs_input(self):
        job = self._job_for("请确认是否合作。")
        dq.claim_next(self.repo, self.cfg, worker="w1")
        res = wc.submit_plan(self.repo, self.cfg, job["job_id"], {
            "intent": "clarify",
            "questions_to_answer": ["是否合作"],
            "facts_to_include": [],
            "missing_information": [{"key": "decision", "label": "是否同意合作",
                                     "type": "decision", "required": True}],
            "tone": "professional", "language": "zh", "attachment_required": False,
        }, worker="w1")
        self.assertTrue(res["needs_input"])
        self.assertEqual(res["status"], JOB_NEEDS_INPUT)

    def test_gate_forces_missing_for_risky_category(self):
        """涉及「是否同意合作」但 plan 没声明缺项 -> 门禁强制补齐。"""
        job = self._job_for("请问你是否同意接收该学生？")
        dq.claim_next(self.repo, self.cfg, worker="w1")
        res = wc.submit_plan(self.repo, self.cfg, job["job_id"], {
            "intent": "confirm", "questions_to_answer": ["是否接收"],
            "facts_to_include": [], "missing_information": [],
            "tone": "professional", "language": "zh",
        }, worker="w1")
        self.assertTrue(res["needs_input"], "风险类别未声明缺项时必须被门禁拦下")
        self.assertTrue(res["enforced_by_gate"])
        self.assertEqual(res["status"], JOB_NEEDS_INPUT)

    def test_needs_input_blocks_drafting(self):
        job = self._job_for("请确认是否合作。")
        dq.claim_next(self.repo, self.cfg, worker="w1")
        dq.set_plan(self.repo, job["job_id"], {
            "intent": "clarify", "missing_information": [{"key": "decision", "type": "decision"}]})
        with self.assertRaises(wc.ContractError):
            wc.submit_draft(self.repo, self.cfg, job["job_id"], "好的", worker="w1")

    def test_user_input_resumes_and_unblocks(self):
        job = self._job_for("请确认是否合作。")
        dq.claim_next(self.repo, self.cfg, worker="w1")
        dq.set_plan(self.repo, job["job_id"], {
            "intent": "clarify", "missing_information": [{"key": "decision", "type": "decision"}]})
        dq.provide_input(self.repo, job["job_id"], {"risk_decision": "同意合作"})
        j = self.repo.get_job(job["job_id"])
        self.assertEqual(j["status"], JOB_QUEUED)
        self.assertIn("risk_decision", j["user_input_json"])
        claimed = dq.claim_next(self.repo, self.cfg, worker="w1")
        self.assertIsNotNone(claimed)
        dq.set_draft(self.repo, job["job_id"], "已确认合作。")
        self.assertEqual(self.repo.get_job(job["job_id"])["status"], JOB_READY)

    def test_provide_input_invalidates_context_snapshot(self):
        """回归：补信息后旧 context_snapshot 必须作废。

        worker_contract 里是 `job.get("context_snapshot") or build(...)` —— 快照
        一旦缓存就短路，补录的 user_input 永远进不去。gate 于是每轮都拿空
        user_notes 重新拦同一项，任务在 needs_input <-> generating 之间死循环。
        """
        job = self._job_for("请提供相关附件材料。")
        dq.claim_next(self.repo, self.cfg, worker="w1")
        wc.get_context(self.repo, self.cfg, job["job_id"], worker="w1")  # 落快照
        self.assertTrue(self.repo.get_job(job["job_id"])["context_snapshot"])

        r1 = wc.submit_plan(self.repo, self.cfg, job["job_id"], {
            "intent": "inform", "facts_to_include": [], "missing_information": [],
            "tone": "professional", "language": "zh"}, worker="w1")
        self.assertTrue(r1["needs_input"], "attachment 类别应被门禁拦下")

        dq.provide_input(self.repo, job["job_id"], {"risk_attachment": "不需要附件"})
        # 注意：写下去是 NULL，_loads 会把它读成 []（repo._loads 的 default 语义），
        # 所以这里断言 falsy 而不是 is None —— 关键是让 `snapshot or build(...)`
        # 的短路失效。
        self.assertFalse(self.repo.get_job(job["job_id"])["context_snapshot"],
                         "补信息必须作废过期快照，否则 gate 会反复拦截")

        dq.claim_next(self.repo, self.cfg, worker="w1")
        r2 = wc.submit_plan(self.repo, self.cfg, job["job_id"], {
            "intent": "inform", "facts_to_include": [], "missing_information": [],
            "tone": "professional", "language": "zh"}, worker="w1")
        self.assertFalse(r2["needs_input"], "补了 attachment 信息后不应再被拦")
        self.assertEqual(r2["status"], JOB_GENERATING)

    def test_detect_risk_categories(self):
        self.assertIn("decision", [r["type"] for r in
                                  rp.detect_risk_categories("请问你是否同意参与该项目？")])
        self.assertIn("date", [r["type"] for r in
                               rp.detect_risk_categories("你哪天方便开会？")])
        self.assertIn("attachment", [r["type"] for r in
                                     rp.detect_risk_categories("请提供相关附件材料")])
        self.assertEqual(rp.detect_risk_categories("谢谢，收到。"), [])

    def test_plan_validation(self):
        ok, errs, norm = rp.validate_plan({"intent": "bogus"})
        self.assertFalse(ok)
        ok2, errs2, norm2 = rp.validate_plan({"intent": "confirm"})
        self.assertTrue(ok2)
        self.assertEqual(norm2["status"], "READY")
        self.assertEqual(norm2["tone"], "professional")


# ==========================================================================
# A7. 上下文构造（§8 最小充分）
# ==========================================================================
class TestContextBuilder(Base):
    def test_minimum_sufficient_context(self):
        tid = self.add_reply_thread()
        # 再堆 10 封，验证 recent_messages 被裁剪
        for i in range(10):
            self.add(mk_msg("<x%d@x>" % i, "Re: 关于产学研申报", "office@example.edu",
                            "科研办", date_iso="2026-09-18T%02d:00:00+08:00" % (i % 24),
                            body="第 %d 封补充说明。" % i, classification=CLASS_IMPORTANT))
        latest = self.repo.latest_in_thread(tid)
        cfg = dict(self.cfg, draft_context_recent_messages=6, draft_context_max_messages=10)
        pkg = cbm.MailContextBuilder(self.repo, cfg).build(message_id=latest["message_id"])
        self.assertEqual(pkg["schema"], "MailContextPackage/1")
        self.assertLessEqual(len(pkg["recent_messages"]), 6)
        self.assertEqual(pkg["current_message"]["message_id"], latest["message_id"])
        for key in ("thread_summary", "participants", "attachments", "previous_commitments",
                    "open_questions", "known_deadlines", "user_notes", "hard_constraints"):
            self.assertIn(key, pkg)
        self.assertTrue(pkg["thread_summary"])
        self.assertTrue(pkg["hard_constraints"])
        self.assertIn("context_hash", pkg)

    def test_body_truncated(self):
        self.add(mk_msg("<big@x>", "长正文", "a@b.com", classification=CLASS_REPLY,
                        body="很长" * 2000))
        mid = self.repo.db.query_one("SELECT message_id FROM messages LIMIT 1")["message_id"]
        pkg = cbm.MailContextBuilder(self.repo, self.cfg).build(message_id=mid)
        self.assertLessEqual(len(pkg["current_message"]["body_text"]), 1500)
        self.assertTrue(pkg["current_message"]["body_truncated"])

    def test_commitments_and_questions_extracted(self):
        tid = self.add_reply_thread()
        latest = self.repo.latest_in_thread(tid)
        pkg = cbm.MailContextBuilder(self.repo, self.cfg).build(message_id=latest["message_id"])
        self.assertTrue(pkg["open_questions"], "对方的问题应被抽出")
        self.assertTrue(pkg["previous_commitments"], "我方的历史承诺应被抽出")
        self.assertTrue(any("9" in (d.get("text") or "") or d.get("dates")
                            for d in pkg["known_deadlines"]))

    def test_no_whole_mailbox(self):
        """上下文里不应出现与本线程无关的邮件。"""
        self.add_reply_thread()
        self.add(mk_msg("<unrel@x>", "完全无关的营销", "promo@x.top", "广告",
                        classification=CLASS_DISMISS, body="买它"))
        mid = self.repo.db.query_one(
            "SELECT message_id FROM messages WHERE message_id='<m3@x>'")["message_id"]
        pkg = cbm.MailContextBuilder(self.repo, self.cfg).build(message_id=mid)
        ids = [m["message_id"] for m in pkg["recent_messages"]]
        self.assertNotIn("<unrel@x>", ids)
        self.assertNotIn("买它", json.dumps(pkg, ensure_ascii=False))


# ==========================================================================
# A8. 线程聚合（§7 thread-first）
# ==========================================================================
class TestThreadAggregation(Base):
    def test_thread_from_references(self):
        self.add(mk_msg("<t1@x>", "项目讨论", "a@b.com", classification=CLASS_REPLY))
        msg2 = mk_msg("<t2@x>", "Re: 项目讨论", "a@b.com", classification=CLASS_REPLY)
        msg2["references_ids"] = ["<t1@x>"]
        msg2["in_reply_to"] = "<t1@x>"
        tid2 = self.add(msg2)
        tids = self.repo.thread_ids()
        self.assertEqual(len(tids), 1)
        self.assertEqual(tids[0], tid2)
        t = self.repo.get_thread(tids[0])
        self.assertEqual(t["message_count"], 2)

    def test_waiting_for_me_then_other(self):
        tid = self.add_reply_thread()
        t = self.repo.get_thread(tid)
        self.assertTrue(t["waiting_for_me"])
        self.assertFalse(t["waiting_for_other"])
        self.assertTrue(t["needs_reply"])
        self.assertGreater(t["last_outbound_ts"], 0)
        self.assertGreater(t["last_inbound_ts"], 0)

        self.add(mk_msg("<m5@x>", "Re: 关于产学研申报", SELF, "本人",
                        to=["office@example.edu"],
                        date_iso="2026-09-19T09:00:00+08:00", body="已提交。"))
        t = self.repo.get_thread(tid)
        self.assertTrue(t["waiting_for_other"])
        self.assertFalse(t["waiting_for_me"])

    def test_subject_normalization_merges(self):
        self.add(mk_msg("<n1@x>", "会议安排", "a@b.com", classification=CLASS_REPLY))
        self.add(mk_msg("<n2@x>", "Re: 会议安排", "a@b.com", classification=CLASS_REPLY))
        self.add(mk_msg("<n3@x>", "答复：Re: 会议安排", "a@b.com", classification=CLASS_REPLY))
        self.assertEqual(len(self.repo.thread_ids()), 1)

    def test_participants_collected(self):
        tid = self.add_reply_thread()
        t = self.repo.get_thread(tid)
        self.assertIn(SELF, t["participants"])
        self.assertIn("office@example.edu", t["participants"])

    def test_thread_view_directions(self):
        tid = self.add_reply_thread()
        view = agg.thread_view(self.repo, tid, self.cfg)
        dirs = [m["direction"] for m in view["messages"]]
        self.assertEqual(dirs, ["in", "out", "in"])

    def test_latest_prefers_imap_over_history_stub_at_same_timestamp(self):
        """同一时刻有 IMAP 邮件与 Foxmail 存根时，「最新一封」必须是 IMAP 那封。

        真实踩到的坑：Foxmail 历史记录是同一封邮件的副本，时间戳完全相同。
        只按时间排序时取到哪条不确定 —— 取到存根（状态 ARCHIVED）就会
        把这封真实邮件从工作桶里挤掉，界面上还看不到。本机实测 2742 个
        多封线程里有 2569 个取错了。
        """
        mid = mk_msg("<twin@x>", "同一封邮件", "a@b.com", "张三",
                     date_iso="2026-09-20T09:00:00+08:00", classification=CLASS_REPLY)
        tid = self.add(mid)
        # 模拟历史存根：同主题、同时间戳、source=foxmail、状态 ARCHIVED
        stub = mk_msg("<twin@x>", "同一封邮件", "a@b.com", "张三",
                      date_iso="2026-09-20T09:00:00+08:00")
        stub["message_id"] = "<twin-fx@mail-workbench.local>"
        stub["source"] = "foxmail"
        stub["classification"] = None
        stub["thread_id"] = tid
        self.repo.upsert_message(stub)
        self.repo.set_workflow_state(stub["message_id"], WF_ARCHIVED)
        agg.aggregate(self.repo, tid, self.cfg)

        latest = self.repo.latest_in_thread(tid)
        self.assertEqual(latest["message_id"], "<twin@x>",
                         "同分时必须优先 IMAP 真实邮件")
        t = self.repo.get_thread(tid)
        self.assertTrue(t["waiting_for_me"],
                        "线程状态必须由真实邮件决定，不能被 ARCHIVED 存根带偏")
        self.assertEqual(t["classification"], CLASS_REPLY)

    def test_backfill_history_skips_duplicate_of_existing_mail(self):
        """邮箱里本来就还存在的邮件，不需要再补一份历史存根当上下文。"""
        tid = self.add(mk_msg("<bf@x>", "重复主题", "a@b.com", "张三",
                              date_iso="2026-09-20T09:00:00+08:00",
                              classification=CLASS_REPLY))
        # 两条历史记录：一条与现有邮件重复，一条是邮箱里没有的旧邮件
        for mid, ts in (("<bf-fx1@mail-workbench.local>", "2026-09-20T09:00:00"),
                        ("<bf-fx2@mail-workbench.local>", "2024-03-01T09:00:00")):
            row = mk_msg(mid, "重复主题", "a@b.com", "张三", date_iso=ts)
            row["source"] = "foxmail"
            row["classification"] = None
            self.repo.upsert_message(row)
        moved = agg.backfill_history(self.repo, util.normalize_subject("重复主题"), tid)
        self.assertEqual(moved, 1, "只该并入「邮箱里没有的」那一封")
        rows = self.repo.db.query(
            "SELECT message_id FROM messages WHERE thread_id=? AND source='foxmail'", (tid,))
        self.assertEqual([r["message_id"] for r in rows],
                         ["<bf-fx2@mail-workbench.local>"])

    def test_prune_redundant_history_detaches_only_duplicates(self):
        """清理存量重复：只摘线程归属，不删数据，且幂等。"""
        tid = self.add(mk_msg("<pr1@x>", "清理主题", "a@b.com", "张三",
                              date_iso="2026-09-20T09:00:00+08:00",
                              classification=CLASS_REPLY))
        # 重复存根（同主题同时间）
        dup = mk_msg("<pr-dup@mail-workbench.local>", "清理主题", "a@b.com", "张三",
                     date_iso="2026-09-20T09:00:00+08:00")
        dup["source"] = "foxmail"
        dup["classification"] = None
        dup["thread_id"] = tid
        self.repo.upsert_message(dup)
        # 非重复的历史邮件（邮箱里没有的旧邮件）—— 必须留下
        old = mk_msg("<pr-old@mail-workbench.local>", "清理主题", "a@b.com", "张三",
                     date_iso="2023-05-05T09:00:00+08:00")
        old["source"] = "foxmail"
        old["classification"] = None
        old["thread_id"] = tid
        self.repo.upsert_message(old)

        out = agg.prune_redundant_history(self.repo)
        self.assertEqual(out["detached"], 1)
        rows = self.repo.db.query(
            "SELECT message_id FROM messages WHERE thread_id=? AND source='foxmail'", (tid,))
        self.assertEqual([r["message_id"] for r in rows],
                         ["<pr-old@mail-workbench.local>"],
                         "邮箱里没有的旧邮件必须保留为上下文")
        # 数据没被删
        self.assertIsNotNone(self.repo.get_message("<pr-dup@mail-workbench.local>"))
        # 幂等
        self.assertEqual(agg.prune_redundant_history(self.repo)["detached"], 0)


# ==========================================================================
# A9. 工作桶（§13）
# ==========================================================================
class TestBuckets(Base):
    def test_bucket_assignment(self):
        tid = self.add_reply_thread()                      # -> ACTION_REQUIRED
        self.add(mk_msg("<w2@x>", "等对方", SELF, "本人", to=["a@b.com"],
                        date_iso="2026-09-18T09:00:00+08:00", body="等你回复"))
        # 让第二个线程处于 waiting_for_other
        self.add(mk_msg("<w1@x>", "等对方", "a@b.com", classification=CLASS_REPLY,
                        date_iso="2026-09-17T09:00:00+08:00"))

        self.add(mk_msg("<snz@x>", "延后处理", "a@b.com", classification=CLASS_REPLY,
                        date_iso="2026-09-16T09:00:00+08:00"))
        snz_mid = self.repo.db.query_one(
            "SELECT message_id FROM messages WHERE message_id='<snz@x>'")["message_id"]
        snzm.SnoozeManager(self.repo, self.cfg).snooze(message_id=snz_mid, when="tomorrow")

        self.add(mk_msg("<done@x>", "已完成", "a@b.com", classification=CLASS_REPLY,
                        date_iso="2026-09-15T09:00:00+08:00"))
        done_mid = self.repo.db.query_one(
            "SELECT message_id FROM messages WHERE message_id='<done@x>'")["message_id"]
        actionsmod.apply(self.repo, self.cfg, "done", message_id=done_mid)

        data = bucketmod.compute(self.repo, self.cfg)
        c = data["counts"]
        self.assertGreaterEqual(c["ACTION_REQUIRED"], 1)
        self.assertGreaterEqual(c["SNOOZED"], 1)
        self.assertGreaterEqual(c["DONE_TODAY"], 1)
        self.assertEqual(data["labels"]["ACTION_REQUIRED"], "待我处理")

    def test_draft_ready_bucket(self):
        self.add(mk_msg("<dr@x>", "草稿就绪", "a@b.com", classification=CLASS_REPLY))
        mid = self.repo.db.query_one("SELECT message_id FROM messages LIMIT 1")["message_id"]
        job = dq.ensure_job(self.repo, self.cfg, mid)
        dq.claim_next(self.repo, self.cfg, worker="w")
        dq.set_draft(self.repo, job["job_id"], "草稿内容")
        data = bucketmod.compute(self.repo, self.cfg)
        items = data["buckets"]["AI_DRAFT_READY"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["job_id"], job["job_id"])
        self.assertEqual(data["pipeline"]["ready"], 1)

    def test_counts_are_full_even_when_lists_are_limited(self):
        """计数必须全量。用带 LIMIT 的结果 len() 会低估（曾经 2883 报成 544）。"""
        for i in range(25):
            self.add(mk_msg("<c%d@x>" % i, "计数测试 %d" % i, "a@b.com", "张三",
                            classification=CLASS_REPLY,
                            date_iso="2026-09-20T09:00:00+08:00"))
        data = bucketmod.compute(self.repo, self.cfg, limit=5)
        self.assertEqual(data["counts"]["ACTION_REQUIRED"], 25)
        self.assertEqual(len(data["buckets"]["ACTION_REQUIRED"]), 5,
                         "列表按 limit 截断")
        # limit=0 时列表也不截断
        data2 = bucketmod.compute(self.repo, self.cfg, limit=0)
        self.assertEqual(len(data2["buckets"]["ACTION_REQUIRED"]), 25)

    def test_broadcast_notice_does_not_occupy_action_required(self):
        """群发通知只知会，不该占用「待我处理」（用户明确要求）。

        但要**可见**：它有自己的一块列表，计数如实给出，不是被藏起来。
        """
        self.add(mk_msg("<bc1@x>", "关于XX重点专项2026年度项目申报指南征求意见的通知",
                        "office@example.edu", "科研办", classification=CLASS_READ,
                        date_iso="2026-09-20T09:00:00+08:00",
                        flags={"broadcast": True}))
        self.add(mk_msg("<bc2@x>", "关于征集2027年联合基金项目指南建议的通知",
                        "office@example.edu", "科研办", classification=CLASS_READ,
                        date_iso="2026-09-20T10:00:00+08:00",
                        flags={"broadcast": True}))
        # 同样来自科研办，但这是真事务（不是群发通知）
        self.add(mk_msg("<real1@x>", "2026年度国自然申请书人员信息核查通知",
                        "office@example.edu", "科研办", classification=CLASS_READ,
                        date_iso="2026-09-20T11:00:00+08:00"))
        data = bucketmod.compute(self.repo, self.cfg, limit=0)
        self.assertEqual(data["counts"]["BROADCAST"], 2)
        self.assertEqual(data["counts"]["ACTION_REQUIRED"], 1,
                         "只有真事务留在待我处理")
        subs = [i["subject"] for i in data["buckets"]["ACTION_REQUIRED"]]
        self.assertEqual(subs, ["2026年度国自然申请书人员信息核查通知"])
        # 可见性：群发通知在 BROADCAST 里能点进去，且带标记
        bc = data["buckets"]["BROADCAST"]
        self.assertEqual(len(bc), 2)
        self.assertTrue(all(i["broadcast"] for i in bc))
        self.assertEqual(data["workingset"]["broadcast"], 2)

    def test_informational_classes_config_is_opt_in(self):
        """`inbox_informational_classes` 是政策开关：默认不启用，开启后移到「仅知会」。

        「看一眼的邮件算不算我的事」因人而异，不该由程序替用户定。
        所以默认保持原行为，用户想收紧工作集时改一行配置即可。
        """
        self.add(mk_msg("<inf1@x>", "要回的", "a@b.com", "张三",
                        classification=CLASS_REPLY,
                        date_iso="2026-09-20T09:00:00+08:00"))
        self.add(mk_msg("<inf2@x>", "看一眼的", "b@b.com", "李四",
                        classification=CLASS_READ,
                        date_iso="2026-09-20T10:00:00+08:00"))
        # 默认：READ 仍在「待我处理」
        d1 = bucketmod.compute(self.repo, self.cfg, limit=0)
        self.assertEqual(d1["counts"]["ACTION_REQUIRED"], 2)
        self.assertEqual(d1["counts"]["BROADCAST"], 0)

        # 开启后：READ 移到「仅知会」，且**没有消失**（可点进去）
        self.cfg["inbox_informational_classes"] = ["READ"]
        d2 = bucketmod.compute(self.repo, self.cfg, limit=0)
        self.assertEqual(d2["counts"]["ACTION_REQUIRED"], 1)
        self.assertEqual(d2["counts"]["BROADCAST"], 1)
        self.assertEqual([i["subject"] for i in d2["buckets"]["ACTION_REQUIRED"]],
                         ["要回的"])
        moved = d2["buckets"]["BROADCAST"][0]
        self.assertEqual(moved["subject"], "看一眼的")
        self.assertIn("仅知会", moved["informational_reason"])

        # 字符串写法也支持（配置文件里手写更省事）
        self.cfg["inbox_informational_classes"] = "READ"
        self.assertEqual(bucketmod.compute(self.repo, self.cfg, limit=0)["counts"]["BROADCAST"], 1)

    def test_broadcast_still_respects_workingset_window(self):
        """群发通知也要受时间窗约束，否则旧通知会永久堆在信息区。"""
        self.cfg["action_window_days"] = 30
        self.add(mk_msg("<bcold@x>", "去年的申报指南征求意见通知",
                        "office@example.edu", "科研办", classification=CLASS_READ,
                        date_iso="2025-01-01T09:00:00+08:00",
                        flags={"broadcast": True, "unread": False}))
        self.add(mk_msg("<bcnew@x>", "今年的申报指南征求意见通知",
                        "office@example.edu", "科研办", classification=CLASS_READ,
                        date_iso="2026-09-20T09:00:00+08:00",
                        flags={"broadcast": True, "unread": False}))
        data = bucketmod.compute(self.repo, self.cfg, limit=0)
        self.assertEqual(data["counts"]["BROADCAST"], 1)
        self.assertEqual(data["buckets"]["BROADCAST"][0]["subject"],
                         "今年的申报指南征求意见通知")

    def test_system_class_goes_to_no_work_bucket(self):
        """⚙系统类邮件应去对应业务系统办理，不该占「待我处理」（规范 §13/§14）。"""
        self.add(mk_msg("<sys@x>", "系统通知", "noreply@sjtu.edu.cn", "系统",
                        classification=CLASS_SYSTEM,
                        date_iso="2026-09-20T09:00:00+08:00"))
        self.add(mk_msg("<rep@x>", "要回复的", "a@b.com", "张三",
                        classification=CLASS_REPLY,
                        date_iso="2026-09-20T10:00:00+08:00"))
        data = bucketmod.compute(self.repo, self.cfg)
        self.assertEqual(data["counts"]["ACTION_REQUIRED"], 1)
        subs = [i["subject"] for i in data["buckets"]["ACTION_REQUIRED"]]
        self.assertEqual(subs, ["要回复的"])
        self.assertEqual(data["workingset"]["skipped_system"], 1)

    def test_old_read_mail_leaves_workingset_but_unread_stays(self):
        """工作集时间窗：未读永远在；已读且超出窗口的退出（否则永远收敛不了）。"""
        self.cfg["action_window_days"] = 30
        # 已读 + 1 年前 -> 退出工作集
        self.add(mk_msg("<old_read@x>", "去年的已读邮件", "a@b.com", "张三",
                        classification=CLASS_REPLY,
                        date_iso="2025-01-01T09:00:00+08:00",
                        flags={"unread": False}))
        # 未读 + 1 年前 -> 仍在工作集（未读就是还没处理）
        self.add(mk_msg("<old_unread@x>", "去年的未读邮件", "b@b.com", "李四",
                        classification=CLASS_REPLY,
                        date_iso="2025-01-02T09:00:00+08:00",
                        flags={"unread": True}))
        # 已读 + 昨天 -> 仍在工作集
        self.add(mk_msg("<new_read@x>", "昨天的已读邮件", "c@b.com", "王五",
                        classification=CLASS_REPLY,
                        date_iso="2026-09-21T09:00:00+08:00",
                        flags={"unread": False}))
        data = bucketmod.compute(self.repo, self.cfg)
        subs = sorted(i["subject"] for i in data["buckets"]["ACTION_REQUIRED"])
        self.assertEqual(subs, ["去年的未读邮件", "昨天的已读邮件"])
        self.assertEqual(data["counts"]["ACTION_REQUIRED"], 2)
        self.assertEqual(data["workingset"]["older_actionable"], 1)
        # 退出工作集不等于删除：线程仍在库里、仍可被检索
        self.assertIsNotNone(self.repo.db.query_one(
            "SELECT thread_id FROM messages WHERE message_id='<old_read@x>'"))
        # 关掉窗口 -> 全部纳入
        self.cfg["action_window_days"] = 0
        data2 = bucketmod.compute(self.repo, self.cfg)
        self.assertEqual(data2["counts"]["ACTION_REQUIRED"], 3)
        self.assertEqual(data2["workingset"]["older_actionable"], 0)

    def test_classification_workflow_separation(self):
        """classification 与 workflow_state 必须能自由组合（§14）。

        message 上的 workflow_state 记录「人类对这封做了什么」（默认 NEW），
        线程上的 workflow_state / waiting_for_* 是聚合出来的派生状态。
        """
        tid = self.add_reply_thread()
        m = self.repo.db.query_one("SELECT * FROM messages WHERE message_id='<m3@x>'")
        self.assertEqual(m["classification"], CLASS_IMPORTANT, "分类来自规则引擎")
        self.assertEqual(m["workflow_state"], "NEW", "尚未有人类动作")
        t = self.repo.get_thread(tid)
        self.assertTrue(t["waiting_for_me"])
        self.assertEqual(t["workflow_state"], WF_WAITING_FOR_ME)

        actionsmod.apply(self.repo, self.cfg, "waiting", message_id=m["message_id"])
        m2 = self.repo.get_message("<m3@x>")
        self.assertEqual(m2["classification"], CLASS_IMPORTANT, "工作流动作不得改动分类")
        self.assertEqual(m2["workflow_state"], WF_WAITING_FOR_OTHER)
        t2 = self.repo.get_thread(tid)
        self.assertTrue(t2["waiting_for_other"])
        self.assertEqual(t2["classification"], CLASS_IMPORTANT)


# ==========================================================================
# A9b. 「等待对方回复」的时间窗与沉底
#
# 用户反馈的原话：「会随着时间的累积数量一直持续增加，很多邮件对方是不会回复的，
# 有问题，让人产生焦虑感」。实测那个库里 145 个 waiting_for_other，128 个超过
# 90 天、102 个超过一年，最早的是 2023 年的「打印机权限设置」。
#
# 这个桶和「待我处理」的语义**正相反**：球在对方那边，我做什么都不会让它变小。
# 一个只增不减、又不由我控制的计数器，是焦虑源而不是工作视图 ——
# 所以它必须自带收敛机制。下面这几条把这个机制钉死。
# ==========================================================================
class TestLocalActionsSurviveNoUid(Base):
    """没有服务器 UID 的邮件（Foxmail 历史索引里的老邮件）也要能「完成」。

    用户实测：「左侧『等待对方回复』我已经点了完成，但该邮件没有服务器 UID
    （可能是 Foxmail 历史记录），无法改服务器状态」——
    对人的感受就是「这个按钮坏了，点了没用」。

    而「完成 / 忽略 / 等待」本质是**本地**工作流决定（把线程从工作桶里收起来），
    标已读 / \\Answered 只是顺手同步给服务器。历史邮件根本没有服务器副本，
    同步不上不该拦住主操作 —— 否则这类邮件被永久卡住，永远处理不掉。
    """

    def test_done_works_without_server_uid(self):
        # 夹具默认把 _imap_store 打桩成「永远成功」，那正好掩盖了这条路径。
        # 换回真身：它对没有 uid 的邮件会在**连服务器之前**就返回失败，不会联网。
        actionsmod._imap_store = _REAL_IMAP_STORE

        self.add(mk_msg("<noun1@x>", "Foxmail 历史里的老邮件", "old@b.com"))
        mid = "<noun1@x>"
        self.assertFalse(self.repo.get_message(mid).get("uid"),
                         "前置条件：这封本来就没有 UID")

        r = actionsmod.apply(self.repo, self.cfg, "done", message_id=mid)
        self.assertTrue(r.get("ok"), "无 UID 也必须能完成，实际：%s" % r)
        self.assertEqual(r.get("workflow_state"), WF_DONE)
        self.assertTrue(r.get("imap"), "要如实说明服务器标志没同步")
        self.assertIn("已完成", r.get("message") or "")
        self.assertEqual(self.repo.get_message(mid)["workflow_state"], WF_DONE,
                         "本地状态必须真的落库")

    def test_done_says_nothing_about_imap_when_uid_exists(self):
        """有 UID 且服务器标志下发成功时，不该多嘴提「没同步」。"""
        self.add(mk_msg("<noun2@x>", "正常邮件", "a@b.com", uid=12345))
        mid = "<noun2@x>"
        # 打桩：只替换服务器那一步，其余走真逻辑
        real = actionsmod._imap_store
        actionsmod._imap_store = lambda *a, **k: {"ok": True}
        try:
            r = actionsmod.apply(self.repo, self.cfg, "done", message_id=mid)
        finally:
            actionsmod._imap_store = real
        self.assertTrue(r.get("ok"))
        self.assertEqual(r.get("imap"), "", "不该有无谓的提示")
        self.assertEqual(r.get("message"), "已完成")

    def test_waiting_and_ignore_also_survive(self):
        # 同样要用真身，否则打桩的成功会掩盖「无 UID」这条路径
        actionsmod._imap_store = _REAL_IMAP_STORE

        self.add(mk_msg("<noun3@x>", "待忽略的老邮件", "c@b.com"))
        mid = "<noun3@x>"
        r1 = actionsmod.apply(self.repo, self.cfg, "ignore", message_id=mid)
        self.assertTrue(r1.get("ok"), "忽略也应该能落地：%s" % r1)
        self.assertEqual(self.repo.get_message(mid)["workflow_state"], WF_IGNORED)

        self.add(mk_msg("<noun4@x>", "等待类老邮件", "d@b.com"))
        mid2 = "<noun4@x>"
        r2 = actionsmod.apply(self.repo, self.cfg, "waiting", message_id=mid2)
        self.assertTrue(r2.get("ok"), "等待也应该能落地：%s" % r2)
        self.assertEqual(self.repo.get_message(mid2)["workflow_state"],
                         WF_WAITING_FOR_OTHER)

    def test_done_moves_thread_out_of_action_required(self):
        """完成的真正意义：线程从「待我处理」里出去。"""
        self.add(mk_msg("<noun5@x>", "需要处理的老邮件", "e@b.com",
                        classification=CLASS_REPLY))
        mid = "<noun5@x>"
        tid = self.repo.get_message(mid)["thread_id"]
        before = bucketmod.compute(self.repo, self.cfg)
        self.assertTrue([r for r in before["buckets"]["ACTION_REQUIRED"]
                         if r.get("thread_id") == tid], "前置：它本来在待我处理")

        actionsmod.apply(self.repo, self.cfg, "done", message_id=mid)

        after = bucketmod.compute(self.repo, self.cfg)
        self.assertEqual([r for r in after["buckets"]["ACTION_REQUIRED"]
                          if r.get("thread_id") == tid], [],
                         "完成后不该还在「待我处理」")


class TestFastDraftTemplate(Base):
    """本地模板「快速草稿」：即时出稿、不调模型，且**绝不写事实**。

    存在意义：实测 AI 草稿 claim → ready 要 22 秒，认领之前的等待更久且不可控；
    而日常大量回复属于「收到确认 / 婉拒 / 索取材料 / 稍后答复」，
    用模板直接出稿可以把这段等待整个去掉。
    """

    def test_ack_template_gives_ready_job_without_worker(self):
        """通知类邮件 -> 收到确认模板 -> 一步到 ready，不需要任何 Worker。"""
        self.add(mk_msg("<fast1@x>", "关于实验室安全检查的通知", "bwc@x.com", "保卫处",
                        classification=CLASS_READ))
        mid = "<fast1@x>"

        pkg = dtpl.build_fast_draft(self.repo, self.cfg, mid)
        self.assertTrue(pkg["ok"])
        self.assertEqual(pkg["kind"], "ack", "通知类应该匹配到「收到确认」")
        self.assertEqual(pkg["missing_slots"], [], "ack 不该有占位符，应当可直接发")
        self.assertIn("来信收到", pkg["draft_text"])

        job = dq.set_fast_draft(self.repo, self.cfg, mid, pkg["draft_text"])
        self.assertEqual(job["status"], JOB_READY, "快速草稿应当直接就绪")
        self.assertEqual(job["trigger_source"], "fast_template")
        self.assertTrue(job["claimed_by"] is None, "不该留下租约")

        # 界面上要立刻看得见
        data = bucketmod.compute(self.repo, self.cfg)
        self.assertEqual(len(data["buckets"]["AI_DRAFT_READY"]), 1)

    def test_decline_template_leaves_placeholder(self):
        """涉及事实（原因）的地方必须留占位符 —— 模板不许替用户编。"""
        # 主题里保留「增补名额」这个关键词 —— decline 模板靠它命中
        self.add(mk_msg("<fast2@x>", "2027级增补名额申请", "stu@x.com", "某同学",
                        classification=CLASS_REPLY))
        pkg = dtpl.build_fast_draft(self.repo, self.cfg, "<fast2@x>")
        self.assertEqual(pkg["kind"], "decline")
        self.assertTrue(pkg["missing_slots"], "婉拒模板必须留出「原因」让用户填")
        self.assertIn(dtpl.PLACEHOLDER_OPEN, pkg["draft_text"])

    def test_unknown_subject_falls_back_to_safe_template(self):
        """认不出类型时用「稍后答复」—— 只确认收到，不做任何承诺。"""
        self.add(mk_msg("<fast3@x>", "一个看不出类别的主题", "x@x.com",
                        classification=CLASS_REPLY))
        pkg = dtpl.build_fast_draft(self.repo, self.cfg, "<fast3@x>")
        self.assertEqual(pkg["kind"], dtpl.DEFAULT_KIND)
        self.assertTrue(pkg["auto"], "应当是推断出来的")
        self.assertNotIn("承诺", pkg["draft_text"])

    def test_salutation_falls_back_and_strips_notes(self):
        """没有姓名时用地址前缀；带括号备注要清掉（别出现「教务处（本科）：」）。"""
        cfg = {"from_name": "本人"}
        t1 = dtpl.render("ack", cfg, from_name="", from_addr="zhangsan@x.com")
        self.assertTrue(t1.startswith("zhangsan："), t1[:20])
        t2 = dtpl.render("ack", cfg, from_name="教务处（本科）")
        self.assertTrue(t2.startswith("教务处："), t2[:20])

    def test_switching_template_reuses_same_job(self):
        """换模板不该派生新任务：同一封邮件只应有一个活跃任务。"""
        self.add(mk_msg("<fast4@x>", "关于报销的通知", "caiwu@x.com",
                        classification=CLASS_READ))
        mid = "<fast4@x>"
        j1 = dq.set_fast_draft(self.repo, self.cfg, mid,
                               dtpl.render("ack", self.cfg, "财务处"))
        j2 = dq.set_fast_draft(self.repo, self.cfg, mid,
                               dtpl.render("thanks", self.cfg, "财务处"))
        self.assertEqual(j1["job_id"], j2["job_id"], "换模板应当就地改，不派生新任务")
        self.assertIn("感谢", self.repo.get_job(j1["job_id"])["draft_text"])

    def test_fast_draft_respects_sent_terminal_state(self):
        """已发出的任务不能被模板草稿复活，而应派生一个新的。"""
        self.add(mk_msg("<fast5@x>", "通知", "a@x.com", classification=CLASS_READ))
        mid = "<fast5@x>"
        j = dq.ensure_job(self.repo, self.cfg, mid)
        dq.claim_next(self.repo, self.cfg, worker="w")
        dq.set_draft(self.repo, j["job_id"], "第一版")
        # 走正规门禁：ready -> reviewing -> approved -> sent
        # （不能从 ready 直接跳 sent，这正是「AI 永不自动发送」的实现点）
        dq.approve(self.repo, self.cfg, j["job_id"], actor="user")
        dq.mark_sent(self.repo, self.cfg, j["job_id"], sent_message_id="<out@x>",
                     actor="user")
        j2 = dq.set_fast_draft(self.repo, self.cfg, mid, "第二版（模板）")
        self.assertNotEqual(j2["job_id"], j["job_id"], "不该复活已发送的任务")
        self.assertEqual(j2["status"], JOB_READY)


class TestSentCopyFlipsThread(Base):
    """发出回复后，线程方向要**立刻**翻转 —— 不必等 IMAP 把 Sent 副本同步回来。

    用户实测反馈：「我已完成某封邮件的处理了，但左侧『待我处理』里还是有该邮件，
    且后面的数量也没自动变化，是等我点『完成了』才变化的。」
    根因：发出的信没有写回本地库，线程的「最新一封」还停在对方那封来信上，
    于是 waiting_for_me 仍为真；只有下次同步拉回 Sent 才会纠正。
    """

    def test_sent_copy_flips_thread_direction(self):
        # 对方来信 -> 球在我这边，应在「待我处理」
        self.add(mk_msg("<flip-in@x>", "名额申请", "stu@example.com", "学生",
                        to=[SELF], classification=CLASS_REPLY))
        tid = self.repo.get_message("<flip-in@x>")["thread_id"]
        t = self.repo.get_thread(tid)
        self.assertTrue(t["waiting_for_me"], "来信后球应该在我这边")
        self.assertFalse(t["waiting_for_other"])

        # 模拟「草稿已发送」：把刚发出的信写回本地
        from mail_workbench import server as mwserver
        job = {"subject": "Re: 名额申请",
               "recipients": ["stu@example.com"],
               "draft_text": "同学你好，很抱歉，课题组今年没有增补名额。"}
        origin = self.repo.get_message("<flip-in@x>")
        ok = mwserver.remember_sent_copy(
            self.repo, self.cfg, job, origin, "<flip-out@x>",
            in_reply="<flip-in@x>", refs=["<flip-in@x>"])
        self.assertTrue(ok, "写回应该成功")
        self.assertIsNotNone(self.repo.get_message("<flip-out@x>"),
                             "发出的信应该已经进了本地库")

        t2 = self.repo.get_thread(tid)
        self.assertTrue(t2["waiting_for_other"], "发出回复后球应该在对方那边")
        self.assertFalse(t2["waiting_for_me"], "不该继续挂在「待我处理」")

        # 关键：桶也要跟着动 —— 这才是用户看得见的东西
        data = bucketmod.compute(self.repo, self.cfg)
        ar = [r for r in data["buckets"]["ACTION_REQUIRED"]
              if r.get("thread_id") == tid]
        wr = [r for r in data["buckets"]["WAITING_FOR_REPLY"]
              if r.get("thread_id") == tid]
        self.assertEqual(len(ar), 0, "已回复的线程不该还留在「待我处理」")
        self.assertEqual(len(wr), 1, "已回复的线程应出现在「等待对方回复」")

    def test_sent_copy_skipped_without_message_id(self):
        """取不到 Message-ID 时只记录跳过，不能抛异常影响发送结果。"""
        from mail_workbench import server as mwserver
        logs = []
        ok = mwserver.remember_sent_copy(self.repo, self.cfg, {}, {}, "",
                                         "", [], log=logs.append)
        self.assertFalse(ok)
        self.assertTrue(logs and "跳过写回" in logs[0], "应该说明为什么跳过")

    def test_sent_copy_never_raises_on_bad_payload(self):
        """写回失败也要静默返回 False —— 发出去的事实不能被它带崩。"""
        from mail_workbench import server as mwserver
        logs = []
        # job 缺 recipients/subject 也不会炸
        ok = mwserver.remember_sent_copy(self.repo, {}, {"recipients": None},
                                         {}, "bad-payload-never-fails", "", [],
                                         log=logs.append)
        self.assertIn(ok, (True, False))


class TestWaitingWindow(Base):
    def _waiting_thread(self, mid, days_ago, subject=None, classification=CLASS_REPLY):
        """造「对方来信 -> 我方回信」的线程，我方回信发生在 days_ago 天前。"""
        subject = subject or ("等待测试 " + mid)
        self.add(mk_msg("<%s-in@x>" % mid, subject, "a@b.com", "对方",
                        date_iso=util.add_days(util.now_iso(), -(days_ago + 1)),
                        classification=classification))
        self.add(mk_msg("<%s-out@x>" % mid, "Re: " + subject, SELF, "本人",
                        to=["a@b.com"],
                        date_iso=util.add_days(util.now_iso(), -days_ago),
                        body="请确认。", classification=None))
        row = self.repo.db.query_one(
            "SELECT thread_id FROM messages WHERE message_id=?", ("<%s-out@x>" % mid,))
        return row["thread_id"]

    def test_fresh_waiting_counts_as_active(self):
        self._waiting_thread("fresh", 1)
        data = bucketmod.compute(self.repo, self.cfg)
        self.assertEqual(data["counts"]["WAITING_FOR_REPLY"], 1)
        self.assertEqual(data["counts"]["STALE_WAIT"], 0)
        self.assertEqual(data["workingset"]["stale_waiting"], 0)
        self.assertEqual(data["workingset"]["waiting_window_days"], 14)

    def test_stale_waiting_sinks_instead_of_piling_up(self):
        """核心断言：超期不再占用工作计数 —— 这就是「不焦虑」的技术含义。"""
        self._waiting_thread("old", 120)
        data = bucketmod.compute(self.repo, self.cfg)
        self.assertEqual(data["counts"]["WAITING_FOR_REPLY"], 0,
                         "超期的不该再占用工作计数")
        self.assertEqual(data["counts"]["STALE_WAIT"], 1)
        self.assertEqual(data["workingset"]["stale_waiting"], 1)
        # 沉底 != 删除：线程还在、能点开、可搜索
        items = data["buckets"]["STALE_WAIT"]
        self.assertEqual(len(items), 1)
        self.assertTrue(items[0]["stale"])
        self.assertGreaterEqual(items[0]["waiting_days"], 119)
        self.assertIsNotNone(self.repo.get_thread(items[0]["thread_id"]))

    def test_window_is_configurable_and_zero_restores_old_behavior(self):
        self._waiting_thread("cfg", 40)
        self.assertEqual(bucketmod.compute(self.repo, self.cfg)["counts"]["STALE_WAIT"], 1)
        cfg = dict(self.cfg)
        cfg["waiting_window_days"] = 60
        d2 = bucketmod.compute(self.repo, cfg)
        self.assertEqual(d2["counts"]["WAITING_FOR_REPLY"], 1, "放宽窗口应把它捞回来")
        self.assertEqual(d2["counts"]["STALE_WAIT"], 0)
        cfg0 = dict(self.cfg)
        cfg0["waiting_window_days"] = 0
        self.assertEqual(
            bucketmod.compute(self.repo, cfg0)["counts"]["WAITING_FOR_REPLY"], 1,
            "0 = 不设窗口，可一键退回旧行为（改动不是单向的）")

    def test_explicit_followup_overrides_window(self):
        """用户主动 follow up 的线程不受窗口约束 —— 显式意图优先于系统默认。"""
        tid = self._waiting_thread("followed", 200)
        fum.FollowUpManager(self.repo, self.cfg).schedule(tid, days=3)
        data = bucketmod.compute(self.repo, self.cfg)
        self.assertEqual(data["counts"]["WAITING_FOR_REPLY"], 1)
        self.assertEqual(data["counts"]["STALE_WAIT"], 0)
        self.assertTrue(data["buckets"]["WAITING_FOR_REPLY"][0]["followup"])

    def test_reply_from_other_leaves_the_bucket(self):
        """对方回信 -> 自动离开。这是该桶唯一自然的出口，不能被窗口逻辑弄坏。"""
        self._waiting_thread("replied", 100)
        self.add(mk_msg("<replied-re@x>", "Re: 等待测试 replied", "a@b.com", "对方",
                        date_iso=util.now_iso(), classification=CLASS_REPLY))
        data = bucketmod.compute(self.repo, self.cfg)
        self.assertEqual(data["counts"]["WAITING_FOR_REPLY"], 0)
        self.assertEqual(data["counts"]["STALE_WAIT"], 0)
        self.assertGreaterEqual(data["counts"]["ACTION_REQUIRED"], 1,
                                "对方回信后应回到「待我处理」")

    def test_stale_area_is_not_a_work_bucket(self):
        """沉底区不能进 BUCKETS/侧边栏工作区 —— 否则只是给焦虑换个地方长。"""
        self._waiting_thread("isolated", 300)
        data = bucketmod.compute(self.repo, self.cfg)
        self.assertNotIn(BUCKET_STALE_WAIT, BUCKETS)
        self.assertNotIn(BUCKET_STALE_WAIT, BUCKETS_ALL)
        self.assertIn(BUCKET_STALE_WAIT, data["buckets"], "但仍必须可见可点开")
        self.assertEqual(data["counts"]["ACTION_REQUIRED"], 0)


# ==========================================================================
# A10. 发送门禁（§33）
# ==========================================================================
class TestSendGate(Base):
    def setUp(self):
        super().setUp()
        self.add(mk_msg("<send@x>", "需要回复", "a@b.com", classification=CLASS_REPLY))
        self.mid = self.repo.db.query_one("SELECT message_id FROM messages LIMIT 1")["message_id"]
        self.job = dq.ensure_job(self.repo, self.cfg, self.mid)
        dq.claim_next(self.repo, self.cfg, worker="w")
        dq.set_draft(self.repo, self.job["job_id"], "这是草稿正文。")

    def test_send_denied_without_token(self):
        ok, reason, _ = dq.authorize_send(self.repo, self.job["job_id"], "bogus")
        self.assertFalse(ok)
        self.assertIn("approved", reason)

    def test_send_denied_in_ready_state(self):
        j = self.repo.get_job(self.job["job_id"])
        self.assertEqual(j["status"], JOB_READY)
        ok, reason, _ = dq.authorize_send(self.repo, self.job["job_id"], "")
        self.assertFalse(ok)

    def test_approve_then_authorized(self):
        res = dq.approve(self.repo, self.cfg, self.job["job_id"])
        token = res["approve_token"]
        self.assertEqual(res["job"]["status"], JOB_APPROVED)
        ok, reason, job = dq.authorize_send(self.repo, self.job["job_id"], token)
        self.assertTrue(ok, reason)
        ok2, _, _ = dq.authorize_send(self.repo, self.job["job_id"], token + "x")
        self.assertFalse(ok2)

    def test_approve_from_ready_keeps_machine_legal(self):
        dq.approve(self.repo, self.cfg, self.job["job_id"])
        chain = [(e["from_status"], e["to_status"])
                 for e in self.repo.job_events(self.job["job_id"])]
        self.assertIn((JOB_READY, JOB_REVIEWING), chain)
        self.assertIn((JOB_REVIEWING, JOB_APPROVED), chain)

    def test_token_expiry(self):
        res = dq.approve(self.repo, self.cfg, self.job["job_id"])
        self.repo.update_job(self.job["job_id"],
                             {"approve_token_expires": util.add_seconds(util.now_iso(), -10)})
        ok, reason, _ = dq.authorize_send(self.repo, self.job["job_id"], res["approve_token"])
        self.assertFalse(ok)
        self.assertIn("过期", reason)

    def test_edit_after_approve_invalidates_token(self):
        dq.approve(self.repo, self.cfg, self.job["job_id"])
        j = dq.save_edit(self.repo, self.job["job_id"], "我改成别的内容了")
        self.assertEqual(j["status"], JOB_REVIEWING)
        self.assertIsNone(j.get("approve_token"))

    def test_revoke(self):
        dq.approve(self.repo, self.cfg, self.job["job_id"])
        j = dq.revoke_approval(self.repo, self.job["job_id"])
        self.assertEqual(j["status"], JOB_REVIEWING)
        ok, _, _ = dq.authorize_send(self.repo, self.job["job_id"], "x")
        self.assertFalse(ok)

    def test_contract_has_no_send_for_ai(self):
        doc = wc.contract_doc()
        paths = [e["path"] for e in doc["endpoints"]]
        self.assertNotIn("/api/drafts/{id}/send", paths)
        self.assertIn("send email", doc["forbidden_for_ai"])
        self.assertIn("AI NEVER SENDS EMAIL", doc["principle"])

    def _make_app(self):
        """构造 App 并屏蔽所有网络：IMAP 连接直接抛错（写入已发送是 best-effort）。

        注意：App 会在同一个 sqlite 文件上再开连接，**必须在 tearDown 之前**关掉，
        否则 Windows 上临时目录删不掉（unittest 的 addCleanup 跑在 tearDown 之后）。
        因此这里把清理动作返回给调用方，用 try/finally 显式收尾。
        """
        from mail_workbench.server import App
        from mail_workbench.mail import imap_client
        self._orig_connect = imap_client.ImapClient.connect

        def _no_net(self, *a, **k):
            raise imap_client.ImapError("测试环境不联网")

        imap_client.ImapClient.connect = _no_net
        app = App(self.cfg)
        return app

    def _close_app(self, app):
        from mail_workbench.mail import imap_client
        try:
            app.repo.db.close()
        finally:
            imap_client.ImapClient.connect = self._orig_connect

    def test_app_send_requires_confirm_flag(self):
        """App.send_draft 必须要求 confirm=SEND，否则不发（此处不发信）。"""
        app = self._make_app()
        try:
            res = app.send_draft(self.job["job_id"], "wrong-token", confirm="")
            self.assertFalse(res["ok"])
            self.assertEqual(res["http"], 403)
            dq.approve(self.repo, self.cfg, self.job["job_id"])
            res2 = app.send_draft(self.job["job_id"], "wrong", confirm="SEND")
            self.assertFalse(res2["ok"])
            self.assertEqual(res2["http"], 403)
        finally:
            self._close_app(app)

    def test_app_send_happy_path_marks_sent_and_schedules_followup(self):
        app = self._make_app()
        sent_calls = []
        orig = smtp_client.send

        def _stub_send(cfg, raw, recips, dry_run=False):
            sent_calls.append(list(recips))
            return {"ok": True, "recipients": list(recips)}

        try:
            token = dq.approve(self.repo, self.cfg, self.job["job_id"])["approve_token"]
            smtp_client.send = _stub_send
            try:
                res = app.send_draft(self.job["job_id"], token, confirm="SEND")
            finally:
                smtp_client.send = orig
            self.assertTrue(res["ok"], res)
            self.assertEqual(len(sent_calls), 1)
            self.assertEqual(sent_calls[0], ["a@b.com"])
            self.assertFalse(res["appended_to_sent"], "测试环境没有 IMAP，应如实报告未写入")
            j = self.repo.get_job(self.job["job_id"])
            self.assertEqual(j["status"], JOB_SENT)
            self.assertIsNone(j["approve_token"])
            self.assertEqual(len(self.repo.list_followups(status="scheduled")), 1)
        finally:
            self._close_app(app)

    def test_crlf_normalization(self):
        """RFC 5322 要求 CRLF；V1 的坑：用 LF 写草稿会让 Foxmail 段落间多出空行。"""
        raw = smtp_client.build_message(self.cfg, "a@b.com", "主题", "第一行\n第二行")
        from email import message_from_bytes
        payload = message_from_bytes(raw).get_payload(decode=True).decode("utf-8")
        self.assertIn("第一行\r\n第二行", payload)
        self.assertEqual(payload.count("\n"), payload.count("\r\n"),
                         "不应存在裸 LF（会被 Foxmail 当成段落分隔）")
        self.assertNotIn("\r\r", smtp_client.crlf("a\r\nb"))


# ==========================================================================
# A11. Reply Controls（§12）
# ==========================================================================
class TestReplyControls(Base):
    def test_control_catalog_present(self):
        for k in ("shorter", "more_formal", "more_friendly", "more_direct",
                  "chinese", "english", "bilingual", "regenerate"):
            self.assertIn(k, rp.__dict__ and __import__(
                "mail_workbench.constants", fromlist=["REPLY_CONTROLS"]).REPLY_CONTROLS)

    def test_revise_supersedes_parent_and_keeps_single_active(self):
        self.add(mk_msg("<rv@x>", "改写测试", "a@b.com", classification=CLASS_REPLY))
        mid = self.repo.db.query_one("SELECT message_id FROM messages LIMIT 1")["message_id"]
        j1 = dq.ensure_job(self.repo, self.cfg, mid)
        dq.claim_next(self.repo, self.cfg, worker="w")
        dq.set_draft(self.repo, j1["job_id"], "第一版")
        j2 = dq.revise(self.repo, self.cfg, j1["job_id"], "改为更正式")
        self.assertNotEqual(j1["job_id"], j2["job_id"])
        self.assertEqual(j2["revision_of"], j1["job_id"])
        self.assertEqual(self.repo.get_job(j1["job_id"])["status"], JOB_EXPIRED,
                         "旧版本必须离开活跃集合")
        active = [j for j in self.repo.list_jobs(statuses=(JOB_QUEUED, JOB_GENERATING, JOB_READY))
                  if j["message_id"] == mid]
        self.assertEqual(len(active), 1, "同一 message 只能有一个活跃任务")

    def test_revise_keeps_old_draft_for_history(self):
        self.add(mk_msg("<rv2@x>", "改写测试2", "a@b.com", classification=CLASS_REPLY))
        mid = self.repo.db.query_one("SELECT message_id FROM messages WHERE message_id='<rv2@x>'"
                                     )["message_id"]
        j1 = dq.ensure_job(self.repo, self.cfg, mid)
        dq.claim_next(self.repo, self.cfg, worker="w")
        dq.set_draft(self.repo, j1["job_id"], "旧草稿内容")
        dq.revise(self.repo, self.cfg, j1["job_id"], "shorter")
        self.assertEqual(self.repo.get_job(j1["job_id"])["draft_text"], "旧草稿内容")

    def test_revision_is_claimable_and_thread_stays_visible(self):
        """改写后：新版本必须能被 worker 认领，线程也不能从首页消失。

        这是端到端验证时踩到的坑 —— 用户点了「更短」，如果新版本认领不到、
        或线程从「待我处理」里消失，用户会以为草稿丢了。
        """
        self.add(mk_msg("<rv3@x>", "改写可认领", "a@b.com", classification=CLASS_REPLY))
        mid = "<rv3@x>"
        j1 = dq.ensure_job(self.repo, self.cfg, mid)
        dq.claim_next(self.repo, self.cfg, worker="w")
        dq.set_draft(self.repo, j1["job_id"], "第一版草稿")

        j2 = dq.revise(self.repo, self.cfg, j1["job_id"], "shorter")
        self.assertEqual(j2["status"], JOB_QUEUED)

        # 新版本必须能被 worker 领到（否则「一键改写」点了没反应）
        got = dq.claim_next(self.repo, self.cfg, worker="w2")
        self.assertIsNotNone(got, "改写后的新版本必须可被认领")
        self.assertEqual(got["job_id"], j2["job_id"])
        self.assertEqual(got["claimed_by"], "w2")

        # 生成中：线程回到「待我处理」，并带上任务状态，UI 才知道在重新生成
        data = bucketmod.compute(self.repo, self.cfg, limit=0)
        rows = [i for i in data["buckets"]["ACTION_REQUIRED"]
                if i.get("thread_id") == self.repo.get_message(mid)["thread_id"]]
        self.assertEqual(len(rows), 1, "正在重新生成的线程仍应在首页可见")
        self.assertEqual(rows[0]["job_status"], JOB_GENERATING)

        # 新版本出来后又回到 AI_DRAFT_READY，且草稿是新内容
        dq.set_draft(self.repo, j2["job_id"], "第二版（更短）")
        data2 = bucketmod.compute(self.repo, self.cfg, limit=0)
        ready = [i for i in data2["buckets"]["AI_DRAFT_READY"]
                 if i.get("thread_id") == self.repo.get_message(mid)["thread_id"]]
        self.assertEqual(len(ready), 1)
        self.assertEqual(ready[0]["job_id"], j2["job_id"],
                         "AI_DRAFT_READY 必须指向新版本，而不是被取代的旧版本")

    def test_draft_mode_switch(self):
        self.add(mk_msg("<md@x>", "模式", "a@b.com", classification=CLASS_REPLY))
        mid = self.repo.db.query_one("SELECT message_id FROM messages LIMIT 1")["message_id"]
        j = dq.ensure_job(self.repo, self.cfg, mid, draft_mode="academic")
        self.assertEqual(j["draft_mode"], "academic")


# ==========================================================================
# A12. Snooze / FollowUp（§16 / §17）
# ==========================================================================
class TestSnoozeFollowup(Base):
    def test_parse_when_presets(self):
        for spec in ("later_today", "tomorrow", "next_week", "+3d", "+6h", "14:30",
                     "2026-12-01", "2026-12-01T09:00"):
            iso = snzm.parse_when(spec, self.cfg)
            self.assertTrue(util.parse_iso(iso), spec)
            self.assertGreater(util.to_epoch(iso), util.to_epoch(util.now_iso()) - 10, spec)

    def test_snooze_and_wake(self):
        self.add(mk_msg("<z1@x>", "延后我", "a@b.com", classification=CLASS_REPLY))
        mid = self.repo.db.query_one("SELECT message_id FROM messages LIMIT 1")["message_id"]
        mgr = snzm.SnoozeManager(self.repo, self.cfg)
        s = mgr.snooze(message_id=mid, when="tomorrow")
        self.assertEqual(self.repo.get_message(mid)["workflow_state"], "SNOOZED")
        self.assertEqual(len(mgr.list_active()), 1)
        # 手动把 wake_at 调到过去
        self.repo.db.execute("UPDATE snoozes SET wake_at=? WHERE snooze_id=?",
                             (util.add_seconds(util.now_iso(), -60), s["snooze_id"]))
        woken = mgr.wake_due()
        self.assertEqual(len(woken), 1)
        self.assertEqual(self.repo.get_message(mid)["workflow_state"], WF_WAITING_FOR_ME)

    def test_snooze_no_automation_needed(self):
        """snooze 只落本地库，调度由本地 scheduler 承担。"""
        self.add(mk_msg("<z2@x>", "延后我2", "a@b.com", classification=CLASS_REPLY))
        mid = self.repo.db.query_one("SELECT message_id FROM messages WHERE message_id='<z2@x>'"
                                     )["message_id"]
        snzm.SnoozeManager(self.repo, self.cfg).snooze(message_id=mid, when="+2d")
        row = self.repo.db.query_one("SELECT * FROM snoozes WHERE message_id=?", (mid,))
        self.assertEqual(row["status"], "active")
        self.assertTrue(row["wake_at"])

    def test_followup_due_and_autoclose_on_reply(self):
        tid = self.add_reply_thread()
        fm = fum.FollowUpManager(self.repo, self.cfg)
        fu = fm.schedule(tid, days=3)
        self.assertEqual(len(fm.list_open()), 1)
        # 我方又发一封 -> 线程变为 waiting_for_other（对方还没回）
        self.add(mk_msg("<m9@x>", "Re: 关于产学研申报", SELF, "本人",
                        to=["office@example.edu"],
                        date_iso="2026-09-19T09:00:00+08:00", body="请尽快确认。"))
        self.assertTrue(self.repo.get_thread(tid)["waiting_for_other"])
        # 到期
        self.repo.db.execute("UPDATE followups SET due_at=? WHERE followup_id=?",
                             (util.add_seconds(util.now_iso(), -60), fu["followup_id"]))
        due = fm.mark_due()
        self.assertEqual(len(due), 1)
        self.assertEqual(len(fm.list_due()), 1)
        # 对方回信后 -> 关闭
        self.add(mk_msg("<m10@x>", "Re: 关于产学研申报", "office@example.edu",
                        "科研办", date_iso="2026-09-20T09:00:00+08:00", body="收到。"))
        n = fm.close_on_reply(tid)
        self.assertEqual(n, 1)
        self.assertEqual(self.repo.list_followups(status="due"), [])

    def test_followup_skips_when_already_replied(self):
        tid = self.add_reply_thread()
        fm = fum.FollowUpManager(self.repo, self.cfg)
        fu = fm.schedule(tid, days=1)
        self.repo.db.execute("UPDATE followups SET due_at=? WHERE followup_id=?",
                             (util.add_seconds(util.now_iso(), -60), fu["followup_id"]))
        # 线程现在 waiting_for_me（对方来信）-> 到期的跟进应被自动关闭而不是打扰用户
        due = fm.mark_due()
        self.assertEqual(due, [])
        self.assertEqual(self.repo.db.query_one(
            "SELECT status FROM followups WHERE followup_id=?", (fu["followup_id"],))["status"],
            "replied")

    def test_sent_draft_schedules_followup(self):
        self.add(mk_msg("<fu@x>", "发出后跟进", "a@b.com", classification=CLASS_REPLY))
        mid = self.repo.db.query_one("SELECT message_id FROM messages LIMIT 1")["message_id"]
        job = dq.ensure_job(self.repo, self.cfg, mid)
        dq.claim_next(self.repo, self.cfg, worker="w")
        dq.set_draft(self.repo, job["job_id"], "正文")
        dq.approve(self.repo, self.cfg, job["job_id"])
        dq.mark_sent(self.repo, self.cfg, job["job_id"], "<sent@x>")
        self.assertEqual(len(self.repo.list_followups(status="scheduled")), 1)


# ==========================================================================
# A13. Recovery / 本地 scheduler / Brief / Metrics
# ==========================================================================
class TestRecoveryAndScheduler(Base):
    def test_recovery_silent_when_clean(self):
        res = rcv.run_recovery(self.repo, self.cfg)
        self.assertEqual(res["actions"], 0)

    def test_recovery_throttle(self):
        rcv.run_recovery(self.repo, self.cfg)
        self.repo.kv_set("last_recovery", {"at": util.now_iso(), "actions": 1})
        self.assertFalse(rcv.should_run(self.repo, self.cfg, min_interval_seconds=1800))

    def test_scheduler_tick_runs_all_phases(self):
        from mail_workbench.workflow.scheduler import Scheduler
        s = Scheduler(self.repo, self.cfg)
        out = s.tick()
        for k in ("snoozes_woken", "followups_due", "recovery", "brief"):
            self.assertIn(k, out)
        self.assertTrue(out["brief"])

    def test_brief_content(self):
        self.add_reply_thread()
        snap = briefmod.build(self.repo, self.cfg)
        h = snap["headline"]
        self.assertGreaterEqual(h["needs_attention"], 1)
        self.assertIn("邮件早报", snap["text"])
        self.assertLessEqual(len(snap["text"].splitlines()), 30)

    def test_brief_numbers_match_buckets(self):
        """简报与工作桶必须给同一个数字（同一件事不能有两个答案）。

        brief 曾经自己写了一套 SQL 且带 LIMIT 50，于是早报说「50 封需要你处理」
        而首页说「83 封」。现在 brief 直接消费 buckets，这里锁死这个契约。
        """
        # 造出超过 brief 旧 SQL 的 50 条上限，确保不是巧合相等
        for i in range(60):
            self.add(mk_msg("<bn%02d@x>" % i, "简报一致性 %02d" % i, "a@b.com", "张三",
                            classification=CLASS_REPLY,
                            date_iso="2026-09-20T09:00:00+08:00"))
        # 再加一个系统类和三个等对方线程
        self.add(mk_msg("<bsys@x>", "系统通知", "noreply@sjtu.edu.cn", "系统",
                        classification=CLASS_SYSTEM,
                        date_iso="2026-09-20T09:00:00+08:00"))
        for i in range(3):
            self.add(mk_msg("<bw%d@x>" % i, "等对方 %d" % i, "b@b.com", "李四",
                            classification=CLASS_REPLY,
                            date_iso="2026-09-21T09:00:00+08:00"))
            self.add(mk_msg("<bwr%d@x>" % i, "Re: 等对方 %d" % i, SELF, "本人",
                            to=["b@b.com"], date_iso="2026-09-21T10:00:00+08:00"))

        data = bucketmod.compute(self.repo, self.cfg, limit=0)
        snap = briefmod.build(self.repo, self.cfg)
        h = snap["headline"]
        c = data["counts"]
        self.assertEqual(h["needs_attention"], c["ACTION_REQUIRED"])
        self.assertEqual(h["needs_attention"], 60, "系统类不计入；也不能被 50 截断")
        # replies_required 必须统计**全量**待处理，而不是展示用的前 20 条切片
        self.assertEqual(h["replies_required"], 60,
                         "只在前 20 条里统计会把「60 封要回」算成别的数字")
        self.assertEqual(h["waiting_for_reply"], c["WAITING_FOR_REPLY"])
        self.assertEqual(h["waiting_for_reply"], 3)
        self.assertEqual(h["drafts_ready"], c["AI_DRAFT_READY"])
        self.assertEqual(h["snoozed"], c["SNOOZED"])
        self.assertEqual(h["done_today"], c["DONE_TODAY"])
        # 简报文本里的第一个数字要与桶一致（用户最先看到的就是它）
        first = snap["text"].splitlines()[2]
        self.assertTrue(first.startswith("%d 封需要你处理" % c["ACTION_REQUIRED"]), first)
        # 工作集信息也要透出，UI 才能解释「为什么只有这些」
        self.assertIn("workingset", snap)

    def test_ttiz_and_acceptance_rate(self):
        tid = self.add_reply_thread()
        mid = self.repo.db.query_one("SELECT message_id FROM messages WHERE message_id='<m3@x>'"
                                     )["message_id"]
        actionsmod.apply(self.repo, self.cfg, "done", message_id=mid)
        r = metricsmod.record_ttiz_for_today(self.repo)
        self.assertGreater(r["count"], 0)
        self.assertGreater(r["average_hours"], 0)

        # 接受率：1 发送 + 1 放弃 = 50%
        self.add(mk_msg("<acc1@x>", "接受率A", "a@b.com", classification=CLASS_REPLY))
        self.add(mk_msg("<acc2@x>", "接受率B", "a@b.com", classification=CLASS_REPLY))
        j1 = dq.ensure_job(self.repo, self.cfg, "<acc1@x>")
        dq.claim_next(self.repo, self.cfg, worker="w")
        dq.set_draft(self.repo, j1["job_id"], "x")
        dq.approve(self.repo, self.cfg, j1["job_id"])
        dq.mark_sent(self.repo, self.cfg, j1["job_id"])
        j2 = dq.ensure_job(self.repo, self.cfg, "<acc2@x>")
        dq.dismiss(self.repo, j2["job_id"])
        snap = metricsmod.snapshot(self.repo)
        self.assertEqual(snap["kpi"]["draft_acceptance_rate"], 0.5)
        self.assertTrue(snap["local_only"])

    def test_edit_ratio_metric(self):
        self.assertAlmostEqual(dq.edit_ratio("abc", "abc"), 0.0)
        self.assertGreater(dq.edit_ratio("abc", "xyzxyz"), 0.9)


# ==========================================================================
# B. V1 功能回归（规范 §37）
# ==========================================================================
class TestV1Regression(Base):
    def test_folder_browsing_and_unread(self):
        self.add(mk_msg("<f1@x>", "收件箱信", "a@b.com", classification=CLASS_REPLY,
                        flags={"unread": True}))
        self.add(mk_msg("<f1b@x>", "收件箱信2", "c@b.com", classification=CLASS_REPLY,
                        flags={"unread": False}))
        self.add(mk_msg("<f2@x>", "已发送信", SELF, "本人", to=["a@b.com"],
                        folder="Sent", flags={"unread": False}))
        self.add(mk_msg("<f3@x>", "垃圾信", "p@x.top", "垃圾", folder="Junk",
                        classification=CLASS_DISMISS))
        counts = {c["folder"]: c for c in self.repo.folder_counts()}
        self.assertEqual(counts["INBOX"]["total"], 2)
        self.assertEqual(counts["INBOX"]["unread"], 1)
        self.assertEqual(counts["Sent"]["total"], 1)
        self.assertEqual(counts["Junk"]["total"], 1)
        self.assertEqual(self.repo.count_messages("INBOX"), 2)
        inbox_unread = self.repo.list_messages(folder="INBOX", unread_only=True)
        self.assertEqual(len(inbox_unread), 1)
        # 被标删的不计入
        self.repo.set_flags("<f1@x>", deleted=True)
        self.assertEqual(self.repo.count_messages("INBOX"), 1)
        self.assertEqual(len(self.repo.list_messages(folder="INBOX", include_deleted=True)), 2)

    def test_mime_parsing_plain_and_folded_subject(self):
        """V1 的坑：主题 MIME 跨行折叠 + base64 正文。"""
        raw = (
            b"From: =?utf-8?B?5byg5LiJ?= <zhangsan@qq.com>\r\n"
            b"To: me@example.edu\r\n"
            b"Subject: =?GB2312?B?xO+6w7K6s/ehsMa7?=\r\n"
            b"         \r\n"
            b"Date: Fri, 18 Sep 2026 10:00:00 +0800\r\n"
            b"Message-ID: <mime1@x>\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\n"
            + MIMEText("你好，这是 base64 正文。", "plain", "utf-8").get_payload().encode()
        )
        info = mparser.parse_message(raw)
        self.assertEqual(info["from_addr"], "zhangsan@qq.com")
        self.assertTrue(info["subject"])
        self.assertIn("base64", info["body_text"])
        self.assertEqual(info["message_id"], "<mime1@x>")

    def test_html_only_body_fallback(self):
        html = "<html><body><p>你好</p><br><ul><li>要点一</li></ul></body></html>"
        raw = ("From: a@b.com\r\nTo: %s\r\nSubject: HTML\r\n"
               "Date: Fri, 18 Sep 2026 10:00:00 +0800\r\n"
               "Message-ID: <html1@x>\r\nContent-Type: text/html; charset=utf-8\r\n\r\n%s"
               % (SELF, html)).encode("utf-8")
        info = mparser.parse_message(raw)
        self.assertIn("你好", info["body_text"])
        self.assertIn("要点一", info["body_text"])
        self.assertNotIn("<li>", info["body_text"])

    def test_attachment_detection_and_metadata(self):
        """用真实 MIME 原文（含 RFC2231 编码的中文文件名），不用拼装对象。"""
        pdf = b"%PDF-1.4 fake pdf payload"
        raw = (
            b"From: a@b.com\r\nTo: " + SELF.encode() + b"\r\n"
            b"Subject: =?utf-8?B?5bim6ZmE5Lu2?=\r\n"
            b"Date: Fri, 18 Sep 2026 10:00:00 +0800\r\n"
            b"Message-ID: <att1@x>\r\n"
            b'MIME-Version: 1.0\r\n'
            b'Content-Type: multipart/mixed; boundary="BOUND"\r\n\r\n'
            b"--BOUND\r\nContent-Type: text/plain; charset=utf-8\r\n\r\n"
            b"\xe6\xad\xa3\xe6\x96\x87\r\n"
            b"--BOUND\r\nContent-Type: application/pdf\r\n"
            b'Content-Disposition: attachment; filename="=?utf-8?B?566A5Y6GLnBkZg==?="\r\n'
            b"Content-Transfer-Encoding: base64\r\n\r\n"
            + __import__("base64").b64encode(pdf) + b"\r\n"
            b"--BOUND--\r\n"
        )
        info = mparser.parse_message(raw)
        self.assertTrue(info["has_attachments"])
        self.assertEqual(info["attachment_names"], ["简历.pdf"])
        att = info["attachments"][0]
        self.assertEqual(att["content_type"], "application/pdf")
        self.assertEqual(att["size_bytes"], len(pdf))
        self.assertTrue(att["sha256"])
        self.assertIn("正文", info["body_text"])

        row = mk_msg("<att1@x>", "带附件", "a@b.com", classification=CLASS_REPLY,
                     flags={"has_attachments": True, "attachment_names": ["简历.pdf"]})
        self.add(row)
        self.repo.record_attachment({
            "attachment_id": "a1", "message_id": "<att1@x>", "filename": "简历.pdf",
            "content_type": "application/pdf", "size_bytes": len(pdf),
            "sha256": att["sha256"]})
        got = self.repo.attachments_for_message("<att1@x>")
        self.assertEqual(len(got), 1)
        self.assertIsNone(self.repo.get_attachment_summary(att["sha256"]),
                          "未分析前不应有摘要缓存（避免无谓的 LLM 调用）")
        self.repo.set_attachment_summary(att["sha256"], "简历.pdf", {"summary": "一份简历"})
        self.assertEqual(self.repo.get_attachment_summary(att["sha256"])["summary"], "一份简历")

    def test_search_operators(self):
        self.add(mk_msg("<q1@x>", "葡萄机器人标定", "zhangsan@qq.com", "张三",
                        date_iso="2026-09-10T10:00:00+08:00", classification=CLASS_REPLY,
                        body="关于 LiDAR 标定"))
        self.add(mk_msg("<q2@x>", "产学研申报", "office@example.edu", "科研办",
                        date_iso="2026-09-20T10:00:00+08:00", classification=CLASS_IMPORTANT,
                        flags={"has_attachments": True, "attachment_names": ["指南.pdf"]}))
        self.add(mk_msg("<q3@x>", "无关", "x@y.com", "李四",
                        date_iso="2026-09-19T10:00:00+08:00", classification=CLASS_READ))

        self.assertEqual(searchmod.run(self.repo, "sender:office")["count"], 1)
        self.assertEqual(searchmod.run(self.repo, "subject:产学研")["count"], 1)
        self.assertEqual(searchmod.run(self.repo, "has:attachment")["count"], 1)
        self.assertEqual(searchmod.run(self.repo, "after:2026-09-15")["count"], 2)
        self.assertEqual(searchmod.run(self.repo, "before:2026-09-15")["count"], 1)
        self.assertEqual(searchmod.run(self.repo, "label:important")["count"], 1)
        self.assertEqual(searchmod.run(self.repo, "葡萄")["count"], 1)
        self.assertEqual(searchmod.run(self.repo, "标定 葡萄")["count"], 1)
        self.assertEqual(searchmod.run(self.repo, "葡萄 不存在")["count"], 0)
        q = searchmod.parse_query("sender:a after:7d 葡萄")
        self.assertTrue(q["after"])
        self.assertEqual(q["terms"], ["葡萄"])
        self.assertIn("发件人", searchmod.describe_query("sender:abc"))

    def test_search_groups_by_thread(self):
        self.add_reply_thread()
        res = searchmod.run(self.repo, "subject:产学研")
        threads = {(m["thread_id"]) for m in res["messages"]}
        self.assertEqual(len(threads), 1, "同线程结果应聚在一起")

    def test_star_and_read_sync_local(self):
        self.add(mk_msg("<st@x>", "星标测试", "a@b.com", classification=CLASS_REPLY))
        r = actionsmod.apply(self.repo, self.cfg, "star", message_id="<st@x>")
        self.assertTrue(r["ok"])
        self.assertTrue(self.repo.get_message("<st@x>")["flagged"])
        r = actionsmod.apply(self.repo, self.cfg, "read", message_id="<st@x>")
        self.assertFalse(self.repo.get_message("<st@x>")["unread"])

    def test_trash_is_recoverable_then_undo(self):
        self.add(mk_msg("<tr@x>", "删除测试", "a@b.com", classification=CLASS_REPLY))
        r = actionsmod.apply(self.repo, self.cfg, "trash", message_id="<tr@x>")
        self.assertTrue(r["ok"])
        self.assertTrue(r["recoverable"])
        self.assertTrue(self.repo.get_message("<tr@x>")["deleted"])
        u = actionsmod.undo(self.repo, self.cfg)
        self.assertTrue(u["ok"])
        self.assertFalse(self.repo.get_message("<tr@x>")["deleted"])

    def test_undo_restores_workflow_state(self):
        tid = self.add_reply_thread()
        m = self.repo.get_message("<m3@x>")
        before = m["workflow_state"]
        actionsmod.apply(self.repo, self.cfg, "archive", message_id="<m3@x>")
        self.assertEqual(self.repo.get_message("<m3@x>")["workflow_state"], WF_ARCHIVED)
        actionsmod.undo(self.repo, self.cfg)
        self.assertEqual(self.repo.get_message("<m3@x>")["workflow_state"], before)

    def test_batch_actions(self):
        ids = []
        for i in range(6):
            mid = "<b%d@x>" % i
            ids.append(mid)
            self.add(mk_msg(mid, "批量 %d" % i, "a%d@b.com" % i, classification=CLASS_READ))
        res = actionsmod.batch(self.repo, self.cfg, "archive", ids)
        self.assertEqual(res["succeeded"], 6)
        self.assertEqual(res["failed"], 0)
        self.assertEqual(self.repo.all_metrics().get("emails_archived"), 6)
        u = actionsmod.undo(self.repo, self.cfg)
        self.assertTrue(u["ok"])
        for mid in ids:
            self.assertNotEqual(self.repo.get_message(mid)["workflow_state"], WF_ARCHIVED)

    def test_batch_rejects_unsupported_action(self):
        with self.assertRaises(actionsmod.ActionError):
            actionsmod.batch(self.repo, self.cfg, "send", ["<a@x>"])

    # ---------------- 批处理：服务器标志必须合并下发 ----------------
    class _FakeImap:
        """记录调用的假 IMAP 客户端（不联网）。"""

        def __init__(self, fail=False):
            self.fail = fail
            self.connects = 0
            self.selects = []
            self.bulk = []       # store_flags_many 的调用
            self.one = []        # store_flags（逐封路径）的调用

        def connect(self):
            self.connects += 1
            return self

        def close(self):
            pass

        def select(self, folder, readonly=True):
            self.selects.append(folder)
            return {}

        def store_flags_many(self, uids, op="+", flags=None, chunk=150):
            ids = list(uids)
            self.bulk.append({"uids": ids, "op": op, "flags": list(flags or [])})
            if self.fail:
                return {"ok": False, "error": "boom", "count": 0, "chunks": 0}
            return {"ok": True, "count": len(ids), "chunks": 1}

        def store_flags(self, uid, op, flags):
            self.one.append({"uid": uid, "op": op, "flags": list(flags)})
            return True

    def _batch_env(self, n=5, folder="INBOX", fail=False, uid_from=100):
        """造 n 封带 UID 的邮件，并把 _client 换成假客户端。

        假客户端与计数装饰器**整个类共用一份**（`self._fake`）——
        否则多次调用会互相覆盖 stub，测出来的调用记录张冠李戴。
        """
        if not hasattr(self, "_fake"):
            self._fake = self._FakeImap()
            self._fake.fail = bool(fail)
            orig_client = actionsmod._client
            actionsmod._client = lambda cfg, log=None: self._fake
            self.addCleanup(lambda: setattr(actionsmod, "_client", orig_client))

            self._calls = {"all": 0, "skipped": 0, "real": 0}
            # 包装的是**真身**（模块级常量），还原也由 tearDown 负责 ——
            # 这里不再注册 cleanup，避免上面说的「cleanup 盖住 tearDown」的坑。
            def _counting(*a, **kw):
                self._calls["all"] += 1
                if kw.get("skip"):
                    self._calls["skipped"] += 1
                else:
                    self._calls["real"] += 1
                return _REAL_IMAP_STORE(*a, **kw)

            actionsmod._imap_store = _counting
        fake = self._fake
        fake.fail = bool(fail)
        ids = []
        for i in range(n):
            mid = "<bulk%d@x>" % (uid_from + i)      # id 与 uid 绑定，便于对账
            ids.append(mid)
            self.repo.upsert_message(mk_msg(mid, "批量 %d" % i, "a%d@b.com" % i,
                                            classification=CLASS_READ,
                                            uid=uid_from + i, folder=folder,
                                            flags={"unread": True}))
        return ids, fake, self._calls

    def test_batch_coalesces_server_flags_into_one_command(self):
        """5 封邮件只应建 1 次连接、发 1 条合并 STORE —— 而不是 5 次。"""
        ids, fake, calls = self._batch_env(n=5)
        res = actionsmod.batch(self.repo, self.cfg, "read", ids)
        self.assertEqual(res["succeeded"], 5, res)
        self.assertEqual(fake.connects, 1, "应只建一次 IMAP 连接")
        self.assertEqual(len(fake.bulk), 1, "应只发一条合并 STORE")
        self.assertEqual(sorted(fake.bulk[0]["uids"]),
                         ["%d" % u for u in range(100, 105)])
        self.assertEqual(fake.bulk[0]["flags"], ["\\Seen"])
        self.assertEqual(fake.one, [], "合并下发后不该再逐封 STORE（会白跑一遍）")
        self.assertEqual(calls["real"], 0, "不该有真正联网的逐封调用")
        self.assertEqual(calls["skipped"], 5, "5 封都应走「已合并下发」的跳过路径")
        for mid in ids:
            self.assertFalse(self.repo.get_message(mid)["unread"], "本地状态要跟着改")

    def test_batch_can_mark_unread(self):
        """批量「标未读」必须是 `-FLAGS \\Seen`，与逐封 apply('unread') 完全一致。

        这条是补出来的：`BATCH_ACTIONS` 一直缺 `unread`，于是 V2 的批量条里
        根本没有「标未读」按钮（V1 有）。点开一封邮件会自动标已读，点错了却
        退不回来 —— 缺少的只是一个批处理入口，单个动作一直支持。
        """
        ids, fake, calls = self._batch_env(n=3)
        for mid in ids:
            self.repo.set_flags(mid, unread=False)      # 先把它们变成已读
        fake.bulk.clear()
        res = actionsmod.batch(self.repo, self.cfg, "unread", ids)
        self.assertEqual(res["succeeded"], 3, res)
        self.assertEqual(fake.connects, 1, "应只建一次 IMAP 连接")
        self.assertEqual(len(fake.bulk), 1, "应只发一条合并 STORE")
        self.assertEqual(fake.bulk[0]["op"], "-", "标未读是 -FLAGS，不是 +")
        self.assertEqual(fake.bulk[0]["flags"], ["\\Seen"])
        self.assertEqual(calls["real"], 0, "不该有真正联网的逐封调用")
        for mid in ids:
            self.assertTrue(self.repo.get_message(mid)["unread"], "本地状态要跟着改")

    def test_batch_action_registry_is_complete(self):
        """每个「纯标志」批量动作都要在 _BATCH_FLAGS 里有对应项。

        漏一个的后果不是报错，而是**静默降级**：那批邮件走逐封路径，
        每封各建一次 IMAP 连接（实测单次 150ms，全选 151 封 ≈ 22.6 秒），
        用户看到的就是「点了批量按钮，半天没反应」。
        """
        for a in ("archive", "ignore", "done", "read", "unread", "star", "trash"):
            self.assertIn(a, actionsmod.BATCH_ACTIONS, "%s 应在批量动作清单里" % a)
            self.assertIn(a, actionsmod._BATCH_FLAGS,
                          "%s 是纯标志动作，必须能合并下发" % a)
        # 单封动作里支持的 read/unread 一对，批量必须同样成对
        self.assertIn("read", actionsmod.BATCH_ACTIONS)
        self.assertIn("unread", actionsmod.BATCH_ACTIONS)

    def test_batch_groups_by_folder(self):
        """不同文件夹要各 select 一次、各发一条 STORE（不能把 UID 混在一起发）。"""
        ids1, fake, _ = self._batch_env(n=3, folder="INBOX", uid_from=200)
        ids2, _, _ = self._batch_env(n=2, folder="专利代理", uid_from=300)
        res = actionsmod.batch(self.repo, self.cfg, "star", ids1 + ids2)
        self.assertEqual(res["succeeded"], 5, res)
        self.assertEqual(sorted(fake.selects), ["INBOX", "专利代理"])
        self.assertEqual(len(fake.bulk), 2, "两个文件夹 = 两条 STORE")
        self.assertEqual(fake.connects, 1, "连接仍复用同一条")
        got = {f["flags"][0] for f in fake.bulk}
        self.assertEqual(got, {"\\Flagged"})

    def test_batch_falls_back_to_per_message_when_bulk_fails(self):
        """合并下发失败 -> 退回逐封（宁可慢，也不能让本地状态领先于服务器）。"""
        ids, fake, calls = self._batch_env(n=3, fail=True)
        res = actionsmod.batch(self.repo, self.cfg, "read", ids)
        self.assertEqual(len(fake.bulk), 1, "先尝试过合并下发")
        self.assertEqual(calls["real"], 3, "失败后 3 封都要真的逐封再走一遍")
        self.assertIn("boom", res["imap"], "失败原因要如实带出来（便于排查）")

    def test_batch_writes_only_one_undo_row(self):
        """整批只写一条 undo —— 否则 151 封会灌进 151 行，还会把「整批撤销」挤下去。"""
        ids, fake, _ = self._batch_env(n=6)
        actionsmod.batch(self.repo, self.cfg, "read", ids)
        rows = self.repo.db.query(
            "SELECT action FROM undo_log WHERE action LIKE 'batch_%'")
        self.assertEqual(len(rows), 1, "应该只有一条 batch_read")
        self.assertEqual(
            int(self.repo.db.scalar("SELECT COUNT(*) FROM undo_log", (), 0)), 1,
            "整批不该产生逐封 undo 行")
        u = actionsmod.undo(self.repo, self.cfg)
        self.assertTrue(u["ok"], "整批撤销要能一次撤掉")
        self.assertEqual(len(u.get("results") or []), 6)

    def test_batch_without_uid_still_reports_failure(self):
        """没有 UID 的邮件（Foxmail 历史）语义不变：如实报失败，不假装成功。

        这条测试要用**真的** `_imap_store`（Base 的打桩把 UID 检查一起替掉了，
        那样测不出语义）。真的那个在无 UID 时会直接返回错误、不联网。
        """
        actionsmod._imap_store = _REAL_IMAP_STORE
        mid = "<no-uid@x>"
        self.repo.upsert_message(mk_msg(mid, "没有 UID", "a@b.com",
                                        classification=CLASS_READ))
        with_uid = "<has-uid@x>"
        self.repo.upsert_message(mk_msg(with_uid, "有 UID", "a@b.com",
                                        classification=CLASS_READ, uid=900))
        ids, fake, _ = self._batch_env(n=0)          # 只为装好假 _client
        res = actionsmod.batch(self.repo, self.cfg, "read", [mid, with_uid])
        self.assertEqual(res["succeeded"], 1, res)
        self.assertEqual(res["failed"], 1, res)
        by = {r["message_id"]: r for r in res["results"]}
        self.assertFalse(by[mid]["ok"])
        self.assertIn("UID", by[mid]["message"])
        # 有 UID 的那封走合并下发；没 UID 的不会被塞进 UID 列表
        self.assertEqual(fake.bulk[0]["uids"], ["900"])

    def test_batch_undo_also_coalesces(self):
        """撤销整批也要合并下发。

        逐封撤销每封最多 2 次连接（这里 5 封 = 10 次）—— 145 封就是 ≈43 秒，
        用户点「撤销」会觉得卡死。合并后应只按**不同 (op,flags) 的组数**建连接。

        注意：快照里 deleted/flagged/unread 三项都要还原，所以这里是 **3 组**
        （-Deleted / -Flagged / -Seen 各一条 STORE），不是一条。
        """
        ids, fake, calls = self._batch_env(n=5)
        actionsmod.batch(self.repo, self.cfg, "read", ids)
        connects_after_batch = fake.connects
        bulk_after_batch = len(fake.bulk)

        u = actionsmod.undo(self.repo, self.cfg)
        self.assertTrue(u["ok"], u)
        self.assertEqual(u["count"], 5)

        new_bulk = fake.bulk[bulk_after_batch:]
        groups = {(b["op"], tuple(b["flags"])) for b in new_bulk}
        self.assertEqual(groups, {("-", ("\\Deleted",)), ("-", ("\\Flagged",)),
                                  ("-", ("\\Seen",))},
                         "应按不同的 (op,flags) 各发一条，而不是逐封发")
        for b in new_bulk:
            self.assertEqual(len(b["uids"]), 5, "同组 5 封应合成一条 STORE")
        self.assertEqual(fake.connects, connects_after_batch + len(new_bulk),
                         "每条分组一条连接（不再逐封各建一条）")
        self.assertLess(fake.connects - connects_after_batch, 10,
                        "必须明显少于逐封的 10 次连接")
        for mid in ids:
            self.assertTrue(self.repo.get_message(mid)["unread"], "本地状态要恢复")
        self.assertIn("合并下发", u.get("imap") or "")

    def test_single_apply_still_hits_imap(self):
        """逐封路径不受影响：skip_imap 默认 False，必须走真的联网分支。

        这条只验「路由」：是否走到逐封分支、有没有被误传 skip。
        **不能**用真的 `_imap_store` —— 那会连真邮箱（单元测试不许联网，
        之前那么写让整个套件从 4 秒涨到 14 秒）。
        """
        mid = "<single@x>"
        self.repo.upsert_message(mk_msg(mid, "单封", "a@b.com", classification=CLASS_READ,
                                        uid=777, flags={"unread": True}))
        calls = {"n": 0, "skipped": 0}

        def _counting(*a, **kw):
            calls["n"] += 1
            if kw.get("skip"):
                calls["skipped"] += 1
            return {"ok": True}

        actionsmod._imap_store = _counting
        self.addCleanup(lambda: setattr(actionsmod, "_imap_store", _REAL_IMAP_STORE))
        actionsmod.apply(self.repo, self.cfg, "read", message_id=mid)
        self.assertEqual(calls["n"], 1, "单封动作仍要改服务器状态")
        self.assertEqual(calls["skipped"], 0, "逐封路径不能走跳过分支")
        self.assertFalse(self.repo.get_message(mid)["unread"])

    def test_context_snapshot_preserved_across_sync(self):
        """重复同步不应覆盖本地工作流状态（V1 会整份重写 JSON）。"""
        self.add(mk_msg("<pres@x>", "保持状态", "a@b.com", classification=CLASS_REPLY))
        actionsmod.apply(self.repo, self.cfg, "archive", message_id="<pres@x>")
        # 模拟再次同步同一封
        again = mk_msg("<pres@x>", "保持状态", "a@b.com", classification=CLASS_REPLY)
        res = self.add(again)
        m = self.repo.get_message("<pres@x>")
        self.assertEqual(m["workflow_state"], WF_ARCHIVED, "本地工作流状态必须保留")
        self.assertEqual(m["classification"], CLASS_REPLY)

    def test_sent_copy_flags(self):
        """已发送副本走 APPEND，只写不投递。"""
        import inspect
        src = inspect.getsource(smtp_client)
        self.assertIn("sendmail", src)
        self.assertNotIn("def delete_permanently", src)


# ==========================================================================
# C. 规则引擎
# ==========================================================================
class TestRuleEngine(Base):
    """规则引擎测试。

    **必须用夹具规则，不能读用户真实的 classify_rules.json。**
    真实规则里是用户自己的 VIP 地址 / 垃圾域名清单（还会随他调整而变），
    断言一旦依赖它，测试就变成「只在这台机器上通过」——换个环境必挂。
    真实规则的**可解析性**由 test_real_rules_file_parses 单独覆盖。
    """

    def setUp(self):
        super().setUp()
        import importlib
        self.rulesmod = importlib.import_module("mail_workbench.intelligence.rule_engine")
        self.rules = {
            "vip": {"senders": ["office@example.edu"], "domains": ["catl.com"]},
            "act": {"domains": ["xcdsystem.com", "easychair.org"]},
            "dump": {"domains": ["researchgatemail.net"],
                     "tlds": ["shop", "top", "xyz", "icu"]},
            "read": {"domains": ["mdpi.com"]},
            "bulk_pattern": "noreply|no-reply|newsletter",
            # example.* 是保留域名，本身会被「随机域名」启发式误判成钓鱼，
            # 所以必须列进白名单 —— 这正好也是白名单机制该被测到的场景。
            "trusted_domains": {"domains": ["sjtu.edu.cn", "qq.com", "mdpi.com",
                                            "example.edu", "example.com"]},
            "heuristics": {"name_equals_localpart": True, "localpart_maxlen": 15,
                           "digits_in_localpart": 6, "random_label_minlen": 6,
                           "random_label_maxlen": 14},
            "human_name": {"cjk_name_means_human": True,
                           "name_prefix_brackets_not_human": True},
            "defaults": {"unmatched": "read"},
        }

    def test_real_rules_file_parses(self):
        """真实规则文件（如果本机有）必须能被加载且结构完整。

        只验证「能加载、有关键段」，不断言具体分类结果 ——
        用户随时会调整自己的 VIP / 垃圾域名清单。
        """
        real = self.rulesmod.load_rules(self.cfg)
        if not real:
            self.skipTest("本机没有 classify_rules.json（包内默认兜底）")
        for k in ("vip", "act", "dump", "read"):
            self.assertIn(k, real, "真实规则缺少 %s 段" % k)
        # 分类器能跑通不报错
        got = self.rulesmod.classify(
            {"from_addr": "someone@sjtu.edu.cn", "from_name": "张三", "subject": "你好"},
            real)
        self.assertIn(got, (CLASS_IMPORTANT, CLASS_REPLY, CLASS_SYSTEM,
                            CLASS_READ, CLASS_DISMISS))

    def test_vip_and_act_and_dump(self):
        R = self.rulesmod
        self.assertEqual(R.classify({"from_addr": "office@example.edu",
                                     "from_name": "科研办", "subject": "申报"},
                                    self.rules), CLASS_IMPORTANT)
        self.assertEqual(R.classify({"from_addr": "x@xcdsystem.com",
                                     "from_name": "投稿系统", "subject": "投稿"},
                                    self.rules), CLASS_SYSTEM)
        self.assertEqual(R.classify({"from_addr": "a@researchgatemail.net",
                                     "from_name": "RG", "subject": "hi"},
                                    self.rules), CLASS_DISMISS)
        self.assertEqual(R.classify({"from_addr": "a@some.icu",
                                     "from_name": "x", "subject": "hi"},
                                    self.rules), CLASS_DISMISS)

    def test_human_signal(self):
        R = self.rulesmod
        self.assertEqual(R.classify({"from_addr": "student@example.com", "from_name": "王同学",
                                     "subject": "导师双选"}, self.rules), CLASS_REPLY)

    def test_machine_generated(self):
        R = self.rulesmod
        # 显示名 == @ 前缀
        self.assertEqual(R.classify({"from_addr": "zvlld@zvlld.bwmj888.com",
                                     "from_name": "zvlld", "subject": "hi"},
                                    self.rules), CLASS_DISMISS)
        # @ 前超长
        self.assertEqual(R.classify({"from_addr": "carpenteruclaeditorjake931@x.com",
                                     "from_name": "Jake", "subject": "hi"},
                                    self.rules), CLASS_DISMISS)
        # 随机域名主体
        self.assertEqual(R.classify({"from_addr": "a@jhquauc.cn", "from_name": "A",
                                     "subject": "hi"}, self.rules), CLASS_DISMISS)

    def test_trusted_domain_exempts_machine_heuristics(self):
        R = self.rulesmod
        got = R.classify({"from_addr": "noreply@newsletter.springer.com",
                          "from_name": "Springer", "subject": "new issue"}, self.rules)
        self.assertIn(got, (CLASS_READ, CLASS_DISMISS))

    def test_bulk_and_header_signals(self):
        R = self.rulesmod
        # 用 x.com：域名段太短，不触发「随机域名」启发式，才能验证 bulk_pattern 生效
        self.assertEqual(R.classify({"from_addr": "noreply@x.com",
                                     "from_name": "系统", "subject": "通知"},
                                    self.rules), CLASS_READ)
        self.assertEqual(R.classify({"from_addr": "someone@x.com",
                                     "from_name": "真人", "subject": "你好",
                                     "auto_submitted": True}, self.rules), CLASS_READ)
        self.assertEqual(R.classify({"from_addr": "someone@x.com",
                                     "from_name": "真人", "subject": "你好",
                                     "precedence": "bulk"}, self.rules), CLASS_READ)
        # 无任何机器信号 + 中文署名 -> 判为真人
        self.assertEqual(R.classify({"from_addr": "someone@x.com",
                                     "from_name": "真人", "subject": "你好"},
                                    self.rules), CLASS_REPLY)

    def test_explain_and_buckets(self):
        R = self.rulesmod
        ex = R.explain({"from_addr": "office@example.edu", "from_name": "科研办",
                        "subject": "x"}, self.rules)
        self.assertEqual(ex["classification"], CLASS_IMPORTANT)
        self.assertTrue(ex["reasons"])
        summary = R.summarize_buckets(
            [{"from_addr": "office@example.edu", "from_name": "科研办", "subject": "a"},
             {"from_addr": "x@x.top", "from_name": "s", "subject": "b"}], self.rules)
        self.assertEqual(summary["total"], 2)


# ==========================================================================
# C2. 群发通知降级（bulk_notice）
# ==========================================================================
class TestBulkNotice(Base):
    """科研办/基金委的群发通知不该占「待我处理」，但真事务必须保住。

    这组测试是这块逻辑的安全网：**降级过头会把真邮件藏起来**，
    比不降级更糟。所以重点是「作用域」与「保命词」两条边界。
    """

    def setUp(self):
        super().setUp()
        import importlib
        self.rulesmod = importlib.import_module("mail_workbench.intelligence.rule_engine")

    def _rules(self, **over):
        r = {
            # 真实规则里 catl.com / sunrayip.com 都在 vip.domains ——
            # 夹具要照着来，否则「真人邮件不受影响」这条根本测不到
            "vip": {"senders": ["office@example.edu"],
                    "domains": ["nsfc.gov.cn", "catl.com", "sunrayip.com"]},
            "act": {"domains": ["xcdsystem.com"]},
            "dump": {"domains": ["spam.top"], "tlds": ["top"]},
            "read": {"domains": []},
            "bulk_pattern": "noreply|newsletter",
            "trusted_domains": {"domains": ["example.edu", "example.com", "sjtu.edu.cn"]},
            "bulk_notice": {
                "senders": ["office@example.edu"],
                "domains": ["nsfc.gov.cn"],
                "subject_keywords": ["申报指南", "征求意见", "通告", "征集", "转发"],
                "except_keywords": ["人员信息核查", "结题", "答辩", "合作意向"],
            },
            "human_name": {"cjk_name_means_human": True},
            "defaults": {"unmatched": "read"},
        }
        r.update(over)
        return r

    def test_broadcast_notice_demoted_from_important_to_read(self):
        R = self.rulesmod
        rules = self._rules()
        rec = {"from_addr": "office@example.edu", "from_name": "科研办",
               "subject": "关于XX重点专项2026年度项目申报指南征求意见的通知"}
        self.assertEqual(R.classify(rec, rules), CLASS_IMPORTANT,
                         "没有 bulk_notice 时它确实是 ★重点（这就是问题所在）")
        ex = R.classify_ex(rec, rules)
        self.assertEqual(ex["classification"], CLASS_READ, "命中群发通知 -> 降为 ○看一眼")
        self.assertTrue(ex["broadcast"])
        self.assertTrue(any("群发通知" in s for s in ex["reasons"]))

    def test_scoped_by_sender_so_real_people_untouched(self):
        """CATL / 专利代理这类真人对人往来**不在作用域**，一个字都不能动。"""
        R = self.rulesmod
        rules = self._rules()
        # 同一主题特征，但发件人不在 bulk_notice 名单里
        rec = {"from_addr": "someone@catl.com", "from_name": "Chang An",
               "subject": "关于XX项目申报指南征求意见的通知"}
        ex = R.classify_ex(rec, rules)
        self.assertFalse(ex["broadcast"], "不在名单内的发件人不得被判为群发通知")

    def test_except_keywords_protect_real_tasks(self):
        """保命词优先：同样来自科研办，但要动手的必须留在待处理。"""
        R = self.rulesmod
        rules = self._rules()
        for subj in ("2026年度国自然申请书人员信息核查通知",
                     "关于组织申报2027年度项目结题验收工作的通知",
                     "关于征集与宁德时代合作意向的通知"):
            ex = R.classify_ex(
                {"from_addr": "office@example.edu", "from_name": "科研办",
                 "subject": subj}, rules)
            self.assertFalse(ex["broadcast"], "保命词命中不得降级：%s" % subj)

    def test_no_match_when_subject_has_no_keyword(self):
        R = self.rulesmod
        rules = self._rules()
        ex = R.classify_ex({"from_addr": "office@example.edu", "from_name": "科研办",
                            "subject": "下周组会改到周三"}, rules)
        self.assertFalse(ex["broadcast"])

    def test_disabled_when_section_empty(self):
        """默认（中性规则）不启用：名单为空 -> 谁都不降级。"""
        R = self.rulesmod
        rules = self._rules(bulk_notice={"senders": [], "domains": [],
                                        "subject_keywords": ["申报指南"],
                                        "except_keywords": []})
        ex = R.classify_ex({"from_addr": "office@example.edu", "from_name": "科研办",
                            "subject": "关于项目申报指南征求意见的通知"}, rules)
        self.assertFalse(ex["broadcast"])
        self.assertEqual(ex["classification"], CLASS_IMPORTANT)

    def test_reclassify_all_is_idempotent_and_keeps_workflow(self):
        """重算分类必须幂等，且**不许动 workflow_state**（那是人的决定）。"""
        R = self.rulesmod
        rules = self._rules()
        self.add(mk_msg("<rc1@x>", "关于XX重点专项项目申报指南征求意见的通知",
                        "office@example.edu", "科研办", classification=CLASS_IMPORTANT,
                        date_iso="2026-09-20T09:00:00+08:00"))
        self.add(mk_msg("<rc2@x>", "2026年度国自然申请书人员信息核查通知",
                        "office@example.edu", "科研办", classification=CLASS_IMPORTANT,
                        date_iso="2026-09-20T10:00:00+08:00"))
        self.add(mk_msg("<rc3@x>", "老师您好", "someone@catl.com", "Chang An",
                        classification=CLASS_IMPORTANT,
                        date_iso="2026-09-20T11:00:00+08:00"))
        # 人工把第二封标成「等对方」——重算不许把它冲掉
        actionsmod.apply(self.repo, self.cfg, "waiting", message_id="<rc2@x>")

        # 用测试规则做重算（真实 rules_path 不一定存在）
        saved = R.load_rules
        R.load_rules = lambda cfg: rules
        try:
            out = R.reclassify_all(self.repo, self.cfg)
        finally:
            R.load_rules = saved
        self.assertTrue(out["ok"])
        self.assertEqual(out["scanned"], 3)
        self.assertEqual(out["broadcast"], 1, "只有第一封是群发通知")

        m1 = self.repo.get_message("<rc1@x>")
        self.assertEqual(m1["classification"], CLASS_READ)
        self.assertEqual(m1["is_broadcast"], 1)
        # 保命词那封仍是 IMPORTANT，且 workflow_state 没被覆盖
        m2 = self.repo.get_message("<rc2@x>")
        self.assertEqual(m2["classification"], CLASS_IMPORTANT)
        self.assertEqual(m2["is_broadcast"], 0)
        self.assertEqual(m2["workflow_state"], WF_WAITING_FOR_OTHER,
                         "重算分类不得覆盖人的工作流决定")
        # 不在作用域的真人邮件原样
        m3 = self.repo.get_message("<rc3@x>")
        self.assertEqual(m3["classification"], CLASS_IMPORTANT)
        self.assertEqual(m3["is_broadcast"], 0)

        # 幂等：再跑一次不应再改任何东西
        R.load_rules = lambda cfg: rules
        try:
            out2 = R.reclassify_all(self.repo, self.cfg)
        finally:
            R.load_rules = saved
        self.assertEqual(out2["changed"], 0)

    def test_reclassify_skips_outbound_and_foxmail(self):
        """重算只针对收件箱：我方发出的、Foxmail 历史都不该被重新分类。"""
        R = self.rulesmod
        rules = self._rules()
        self.add(mk_msg("<ro1@x>", "我发的", SELF, "本人", to=["a@b.com"]))   # 无分类
        self.repo.db.execute("UPDATE messages SET source='foxmail' WHERE message_id='<ro1@x>'")
        saved = R.load_rules
        R.load_rules = lambda cfg: rules
        try:
            out = R.reclassify_all(self.repo, self.cfg)
        finally:
            R.load_rules = saved
        self.assertEqual(out["scanned"], 0, "source != imap 的邮件不参与重算")


# ==========================================================================
# D. Foxmail 索引解析（V1 资产复用）
# ==========================================================================
class TestFoxmailIndex(Base):
    def _build_index(self, path, records):
        header = bytearray(512)
        struct.pack_into("<I", header, 8, len(records))
        with open(path, "wb") as f:
            f.write(header)
            for mail_id, ole, text in records:
                rec = bytearray(512)
                struct.pack_into("<I", rec, 0, mail_id)
                struct.pack_into("<d", rec, 8, ole)
                blob = text.encode("utf-8") + b"\x00\x00"
                rec[51:51 + len(blob)] = blob[: 512 - 51]
                f.write(rec)

    def test_parse_index_and_field_split(self):
        import datetime
        path = os.path.join(self.tmp.name, "Index")
        dt = datetime.datetime(2026, 9, 18, 15, 48)
        ole = (dt - foxmail_index.OLE_BASE).total_seconds() / 86400.0
        self._build_index(path, [
            (101, ole, "李同学lisi@example.com本人me@example.edu择导申请"),
            (102, ole - 1, "科研办office@example.edu本人me@example.edu产学研申报通知"),
            (0, ole, "空槽应被跳过"),
        ])
        recs = foxmail_index.parse_index(path)
        self.assertEqual(len(recs), 2)
        by_id = {r["mail_id"]: r for r in recs}
        self.assertEqual(by_id[101]["from_addr"], "lisi@example.com")
        self.assertEqual(by_id[101]["from_name"], "李同学")
        self.assertEqual(by_id[101]["to_addr"], "me@example.edu")
        self.assertEqual(by_id[101]["subject"], "择导申请")
        self.assertEqual(by_id[102]["subject"], "产学研申报通知")
        # OLE -> 本地时间不再 +8h（V1 修正过的坑）
        self.assertEqual(by_id[101]["received"].hour, 15)
        self.assertEqual(by_id[101]["received"].minute, 48)

    def test_tld_anchor_prevents_swallowing(self):
        """V1 的坑：普通邮箱正则会吞掉 TLD 后的收件人名。"""
        got = foxmail_index.split_fields("王同学student@example.com本人me@example.edu导师双选")
        self.assertEqual(got["from_addr"], "student@example.com")
        self.assertEqual(got["to_addr"], "me@example.edu")
        self.assertEqual(got["subject"], "导师双选")

    def test_import_history_is_idempotent(self):
        import datetime
        path = os.path.join(self.tmp.name, "Index")
        dt = datetime.datetime(2026, 1, 5, 9, 0)
        ole = (dt - foxmail_index.OLE_BASE).total_seconds() / 86400.0
        self._write = self._build_index(path, [
            (201, ole, "张三a@qq.com本人me@example.edu历史邮件一"),
            (202, ole - 1, "张三a@qq.com本人me@example.edu历史邮件二"),
        ])
        cfg = dict(self.cfg, foxmail_enabled=True, foxmail_index_path=path)
        r1 = foxmail_index.import_history(self.repo, cfg)
        self.assertEqual(r1["imported"], 2)
        r2 = foxmail_index.import_history(self.repo, cfg)
        self.assertEqual(r2["imported"], 0, "重复导入不应产生重复记录")
        rows = [r for r in self.repo.db.query("SELECT * FROM messages")
                if r["source"] == "foxmail"]
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertTrue(r["message_id"].startswith("<foxmail-"))
            self.assertEqual(r["body_text"], "", "历史记录只有元数据，正文需联网按需拉取")

    def test_stats_and_missing(self):
        st = foxmail_index.stats("")
        self.assertFalse(st["ok"])
        path = os.path.join(self.tmp.name, "Index")
        self._build_index(path, [(301, 46000.0, "a@b.comc@d.com主题")])
        st2 = foxmail_index.stats(path)
        self.assertTrue(st2["ok"])
        self.assertEqual(st2["declared_records"], 1)


    # ---------------- 历史邮件 × 线程 × 工作桶 的交互 ----------------
    def _history_cfg(self, path):
        return dict(self.cfg, foxmail_enabled=True, foxmail_index_path=path)

    def _build_two_subjects(self):
        import datetime
        path = os.path.join(self.tmp.name, "Index")
        dt = datetime.datetime(2025, 3, 10, 9, 0)
        ole = (dt - foxmail_index.OLE_BASE).total_seconds() / 86400.0
        self._build_index(path, [
            (401, ole, "张三a@qq.com本人%s老项目讨论一" % SELF),
            (402, ole - 1, "张三a@qq.com本人%sRe: 老项目讨论一" % SELF),
            (403, ole - 2, "科研办office@example.edu本人%s产学研申报" % SELF),
        ])
        return self._history_cfg(path)

    def test_history_links_only_to_existing_threads(self):
        """历史邮件只挂到**已存在**的同主题线程上（成为起草上下文，规范 §7）。"""
        cfg = self._build_two_subjects()
        tid = self.add(mk_msg("<cur@x>", "老项目讨论一", "a@qq.com", "张三",
                              date_iso="2026-09-22T09:00:00+08:00",
                              classification=CLASS_REPLY))
        res = foxmail_index.import_history(self.repo, cfg)
        self.assertEqual(res["imported"], 3)
        self.assertEqual(res["linked_existing_threads"], 2)
        rows = {r["subject"]: r["thread_id"] for r in self.repo.db.query(
            "SELECT subject, thread_id FROM messages WHERE source='foxmail'")}
        self.assertEqual(rows["老项目讨论一"], tid)
        self.assertEqual(rows["Re: 老项目讨论一"], tid, "归一化后应挂到同一线程")
        self.assertIsNone(rows["产学研申报"], "没有对应线程就不挂，避免凭空造线程")
        msgs = self.repo.messages_for_thread(tid)
        self.assertGreaterEqual(len(msgs), 3)
        self.assertTrue(any(m["source"] == "foxmail" for m in msgs),
                        "线程里应同时有当前邮件与历史邮件")

    def test_history_import_does_not_create_threads(self):
        """6696 封历史不能凭空生出几千个只有一封旧邮件的线程。"""
        cfg = self._build_two_subjects()
        foxmail_index.import_history(self.repo, cfg)
        self.assertEqual(len(self.repo.thread_ids()), 0,
                         "导入历史不应新建任何线程")
        for r in self.repo.db.query("SELECT * FROM messages WHERE source='foxmail'"):
            self.assertIsNone(r["thread_id"])
            self.assertEqual(r["workflow_state"], WF_ARCHIVED)
        data = bucketmod.compute(self.repo, cfg)
        self.assertEqual(data["counts"]["ACTION_REQUIRED"], 0)
        self.assertEqual(data["counts"]["WAITING_FOR_REPLY"], 0)
        self.assertEqual(data["counts"]["DONE_TODAY"], 0)

    def test_new_mail_backfills_history_as_context(self):
        """核心场景：新邮件到达 -> 同主题历史被拉进线程，成为起草上下文。"""
        cfg = self._build_two_subjects()
        foxmail_index.import_history(self.repo, cfg)
        self.assertEqual(len(self.repo.thread_ids()), 0)
        tid = self.add(mk_msg("<fresh@x>", "老项目讨论一", "a@qq.com", "张三",
                              date_iso="2026-09-22T10:00:00+08:00",
                              body="再聊聊这个老项目。", classification=CLASS_REPLY))
        msgs = self.repo.messages_for_thread(tid)
        self.assertGreaterEqual(len(msgs), 3, "历史邮件应被回填进该线程")
        self.assertTrue(any(m["source"] == "foxmail" for m in msgs))
        # 工作桶里只出现这一封新邮件对应的线程（历史不外溢）
        data = bucketmod.compute(self.repo, cfg)
        self.assertEqual(data["counts"]["ACTION_REQUIRED"], 1)
        self.assertEqual(data["counts"]["WAITING_FOR_REPLY"], 0)
        # 上下文里能拿到历史邮件
        pkg = cbm.MailContextBuilder(self.repo, cfg).build(message_id="<fresh@x>")
        self.assertGreaterEqual(len(pkg["recent_messages"]), 2)
        self.assertTrue(any("老项目" in (m.get("subject") or "")
                            for m in pkg["recent_messages"]))

    def test_rebuild_threads_respects_direction(self):
        """rebuild_threads_for_subjects 必须接收 cfg：漏传会把所有邮件当成来信。"""
        self.add(mk_msg("<d1@x>", "方向测试", "a@b.com", "张三",
                        date_iso="2026-09-20T09:00:00+08:00", classification=CLASS_REPLY))
        self.add(mk_msg("<d2@x>", "Re: 方向测试", SELF, "本人", to=["a@b.com"],
                        date_iso="2026-09-21T09:00:00+08:00", body="已回复"))
        tid = self.repo.db.query_one(
            "SELECT thread_id FROM messages WHERE message_id='<d1@x>'")["thread_id"]
        # 传了 cfg：应该判定为「等对方」
        agg.rebuild_threads_for_subjects(self.repo, ["方向测试"], self.cfg)
        t = self.repo.get_thread(tid)
        self.assertTrue(t["waiting_for_other"], "传 cfg 才能正确识别我方发出的邮件")
        self.assertFalse(t["waiting_for_me"])
        # 不传 cfg：全部被当成来信（这正是历史导入曾经踩的坑）
        agg.rebuild_threads_for_subjects(self.repo, ["方向测试"])
        t2 = self.repo.get_thread(tid)
        self.assertTrue(t2["waiting_for_me"], "不传 cfg 会把所有邮件当来信 —— 这就是为什么要测")

    def test_thread_id_for_subject_is_stable_and_reuses_existing(self):
        self.add(mk_msg("<s1@x>", "稳定线程", "a@b.com", classification=CLASS_REPLY))
        tid1 = agg.thread_id_for_subject(self.repo, "稳定线程")
        tid2 = agg.thread_id_for_subject(self.repo, "Re: 稳定线程")
        self.assertEqual(tid1, tid2, "归一化后同主题必须得到同一 thread_id")
        # 未在库里的主题 -> 确定性生成，两次一致
        a = agg.thread_id_for_subject(self.repo, "库里没有的主题")
        b = agg.thread_id_for_subject(self.repo, "库里没有的主题")
        self.assertEqual(a, b)

    def test_prune_history_only_threads(self):
        """早期版本为每个历史主题建了线程，修复必须能回收（幂等）。"""
        cfg = self._build_two_subjects()
        foxmail_index.import_history(self.repo, cfg)
        # 新代码导入时**不**为历史主题建线程（这就是修复后的正确行为）
        self.assertEqual(self.repo.db.scalar("SELECT COUNT(*) FROM threads"), 0)
        # 现在模拟旧版本留下的坏数据形态：历史邮件带着 thread_id + 对应线程行
        self.repo.db.execute(
            "UPDATE messages SET thread_id = 'thr_' || replace(subject_norm,' ','_') "
            "WHERE source='foxmail' AND subject_norm IS NOT NULL AND subject_norm <> ''")
        self.repo.db.execute(
            "INSERT OR IGNORE INTO threads(thread_id, subject, subject_norm, message_count) "
            "SELECT DISTINCT thread_id, subject, subject_norm, 1 FROM messages "
            "WHERE source='foxmail' AND thread_id IS NOT NULL")
        self.repo.db.execute(
            "UPDATE messages SET workflow_state='NEW' WHERE source='foxmail'")
        self.assertGreater(self.repo.db.scalar("SELECT COUNT(*) FROM threads"), 0)

        res = agg.repair(self.repo, cfg)
        self.assertFalse(res["silent"])
        self.assertGreater(res["threads_removed"], 0)
        self.assertGreater(res["messages_detached"], 0)
        # 历史邮件本身不能被删（还能作为上下文回填）
        self.assertEqual(
            self.repo.db.scalar("SELECT COUNT(*) FROM messages WHERE source='foxmail'"), 3)
        # 历史邮件不再挂线程
        self.assertEqual(
            self.repo.db.scalar(
                "SELECT COUNT(*) FROM messages WHERE source='foxmail' AND thread_id IS NOT NULL"),
            0)
        # 线程表里不再有「只有历史邮件」的线程
        self.assertEqual(
            self.repo.db.scalar(
                "SELECT COUNT(*) FROM threads t WHERE NOT EXISTS ("
                "SELECT 1 FROM messages m WHERE m.thread_id=t.thread_id AND m.source='imap')"),
            0)
        # 幂等
        res2 = agg.repair(self.repo, cfg)
        self.assertTrue(res2["silent"])
        self.assertEqual(res2["threads_removed"], 0)

    def test_prune_keeps_threads_with_current_mail(self):
        """有当前邮件（IMAP）的线程必须保留，历史只作为上下文留在里面。"""
        cfg = self._build_two_subjects()
        foxmail_index.import_history(self.repo, cfg)
        self.add(mk_msg("<keep1@x>", "老项目讨论一", "a@qq.com", "张三",
                        date_iso="2026-09-22T09:00:00+08:00", classification=CLASS_REPLY))
        tid = self.repo.db.query_one(
            "SELECT thread_id FROM messages WHERE message_id='<keep1@x>'")["thread_id"]
        self.repo.db.execute(
            "UPDATE messages SET thread_id='thr_bogus_hist' WHERE source='foxmail' "
            "AND subject_norm NOT IN (SELECT subject_norm FROM messages WHERE source='imap')")
        self.repo.db.execute(
            "INSERT OR IGNORE INTO threads(thread_id, subject, subject_norm, message_count) "
            "VALUES('thr_bogus_hist','x','x',1)")
        agg.repair(self.repo, cfg)
        self.assertIsNotNone(self.repo.get_thread(tid), "有当前邮件的线程不得被回收")
        # 该线程里历史邮件应通过回填重新挂上
        msgs = self.repo.messages_for_thread(tid)
        self.assertTrue(any(m["source"] == "foxmail" for m in msgs),
                        "同主题历史邮件应作为上下文留在线程里")

    def test_legacy_new_history_rows_are_reconciled(self):
        """旧版本导入的历史邮件残留 workflow_state=NEW，升级后必须被归正。"""
        cfg = self._build_two_subjects()
        foxmail_index.import_history(self.repo, cfg)
        # 模拟旧数据：把历史邮件改回 NEW
        self.repo.db.execute(
            "UPDATE messages SET workflow_state='NEW' WHERE source='foxmail'")
        self.assertEqual(
            self.repo.db.scalar(
                "SELECT COUNT(*) FROM messages WHERE source='foxmail' AND workflow_state='NEW'"),
            3)
        res = foxmail_index.import_history(self.repo, cfg)
        self.assertEqual(res["reconciled_archived"], 3)
        self.assertEqual(
            self.repo.db.scalar(
                "SELECT COUNT(*) FROM messages WHERE source='foxmail' AND workflow_state='NEW'"),
            0)
        data = bucketmod.compute(self.repo, cfg)
        self.assertEqual(data["counts"]["ACTION_REQUIRED"], 0)


# ==========================================================================
# E. 附件下载与「用默认程序打开」
# ==========================================================================
def _build_att_mail(subject="带附件", body="正文", atts=(("简历.pdf", b"PDFDATA"),)):
    m = MIMEMultipart()
    m["From"] = "a@b.com"
    m["To"] = SELF
    m["Subject"] = subject
    m["Date"] = "Fri, 18 Sep 2026 10:00:00 +0800"
    m["Message-ID"] = "<attmail@x>"
    m.attach(MIMEText(body, "plain", "utf-8"))
    for name, data in atts:
        part = MIMEText(data.decode("latin-1"), "plain", "utf-8")
        part.replace_header("Content-Type", "application/octet-stream")
        part.add_header("Content-Disposition", 'attachment; filename="%s"' % name)
        m.attach(part)
    return m.as_bytes()


class TestAttachmentSecurity(Base):
    """附件名来自**外部邮件**，是不可信输入。这组测试守住路径穿越。"""

    def test_safe_filename_strips_path_traversal(self):
        cases = {
            "../../../etc/passwd": "passwd",
            r"..\..\Windows\System32\config": "config",
            "/etc/shadow": "shadow",
            r"C:\Users\x\.ssh\id_rsa": "id_rsa",
            "....//....//x.txt": "x.txt",
            "..\\..\\evil.exe": "evil.exe",
        }
        for raw, want in cases.items():
            got = atstore.safe_filename(raw)
            self.assertEqual(got, want, "%r -> %r" % (raw, got))
            self.assertNotIn("..", got)
            self.assertNotIn("/", got)
            self.assertNotIn("\\", got)

    def test_safe_filename_handles_edge_cases(self):
        self.assertEqual(atstore.safe_filename(""), "attachment")
        self.assertEqual(atstore.safe_filename("   "), "attachment")
        self.assertEqual(atstore.safe_filename("...."), "attachment")
        # Windows 保留名不能直接当文件名
        self.assertTrue(atstore.safe_filename("CON.txt").startswith("_"))
        self.assertTrue(atstore.safe_filename("LPT1").startswith("_"))
        # 非法字符被替换、首尾点空格被去掉
        self.assertNotIn(":", atstore.safe_filename("a:b?.txt"))
        self.assertEqual(atstore.safe_filename("  name.txt  "), "name.txt")
        # 超长要截断但保留扩展名，且总长受限
        long_name = "x" * 300 + ".pdf"
        got = atstore.safe_filename(long_name)
        self.assertLessEqual(len(got), atstore.MAX_FILENAME)
        self.assertTrue(got.endswith(".pdf"))

    def test_local_path_is_deterministic_and_contained(self):
        cfg = dict(self.cfg, attachments_dir=os.path.join(self.tmp.name, "att"))
        p1 = atstore.local_path(cfg, "abc123", "../../evil.sh")
        p2 = atstore.local_path(cfg, "abc123", "../../evil.sh")
        self.assertEqual(p1, p2, "同一 sha256 必须得到同一路径（缓存命中靠它）")
        root = os.path.abspath(cfg["attachments_dir"])
        self.assertTrue(os.path.abspath(p1).startswith(root + os.sep),
                        "落盘路径必须留在 attachments_dir 之内：%s" % p1)
        # 即使文件名恶意，也只在文件名那一段被消毒
        self.assertTrue(os.path.basename(p1).startswith("abc123"))

    def test_download_rejects_history_and_missing_uid(self):
        cfg = dict(self.cfg, attachments_dir=os.path.join(self.tmp.name, "att"))
        self.add(mk_msg("<a1@x>", "带附件", "a@b.com", classification=CLASS_REPLY,
                        flags={"has_attachments": True, "attachment_names": ["a.pdf"]}))
        self.repo.record_attachment({
            "attachment_id": "aid1", "message_id": "<a1@x>", "filename": "a.pdf",
            "content_type": "application/pdf", "size_bytes": 7, "sha256": "s1"})
        # 没有 uid -> 明确报错，而不是静默失败或下个空文件
        with self.assertRaises(atstore.AttachmentError) as cm:
            atstore.download(cfg, self.repo, "aid1")
        self.assertIn("UID", str(cm.exception))

        self.repo.db.execute(
            "UPDATE messages SET uid='12', source='foxmail' WHERE message_id='<a1@x>'")
        with self.assertRaises(atstore.AttachmentError) as cm2:
            atstore.download(cfg, self.repo, "aid1")
        self.assertIn("历史记录", str(cm2.exception))

    def test_download_writes_file_and_caches(self):
        cfg = dict(self.cfg, attachments_dir=os.path.join(self.tmp.name, "att"))
        raw = _build_att_mail()
        parts = mparser.list_attachments(raw, with_data=True)
        self.assertEqual(len(parts), 1)
        real_sha = parts[0]["sha256"]

        self.add(mk_msg("<a2@x>", "带附件", "a@b.com", classification=CLASS_REPLY,
                        flags={"has_attachments": True, "attachment_names": ["简历.pdf"]}))
        self.repo.db.execute("UPDATE messages SET uid='42' WHERE message_id='<a2@x>'")
        self.repo.record_attachment({
            "attachment_id": "aid2", "message_id": "<a2@x>", "filename": "简历.pdf",
            "content_type": "application/pdf", "size_bytes": len(parts[0]["data"]),
            "sha256": real_sha})

        calls = []
        orig_full = imap_client.ImapClient.fetch_full
        orig_conn = imap_client.ImapClient.connect
        orig_sel = imap_client.ImapClient.select
        orig_close = imap_client.ImapClient.close
        imap_client.ImapClient.connect = lambda self_, *a, **k: None
        imap_client.ImapClient.select = lambda self_, f, readonly=True: {"exists": 1}
        imap_client.ImapClient.close = lambda self_, *a, **k: None

        def fake_full(self_, uids):
            calls.append(list(uids))
            return {42: {"raw": raw, "flags": []}}

        imap_client.ImapClient.fetch_full = fake_full
        try:
            info = atstore.download(cfg, self.repo, "aid2")
            self.assertFalse(info["cached"])
            self.assertTrue(os.path.exists(info["path"]))
            with open(info["path"], "rb") as f:
                self.assertEqual(f.read(), parts[0]["data"], "落盘内容必须与附件一致")
            self.assertEqual(calls, [[42]])
            # 第二次应当命中缓存，不再走 IMAP
            info2 = atstore.download(cfg, self.repo, "aid2")
            self.assertTrue(info2["cached"])
            self.assertEqual(len(calls), 1, "重复点击不应重新下载")
            self.assertTrue(atstore.is_downloaded(cfg, self.repo.get_attachment("aid2")))
        finally:
            imap_client.ImapClient.fetch_full = orig_full
            imap_client.ImapClient.connect = orig_conn
            imap_client.ImapClient.select = orig_sel
            imap_client.ImapClient.close = orig_close

    def test_list_attachments_matches_stored_metadata(self):
        """list_attachments 与入库元数据必须能按 sha256 对上（不能靠下标）。"""
        raw = _build_att_mail(atts=(("a.pdf", b"AAA"), ("b.txt", b"BBBB")))
        parts = mparser.list_attachments(raw, with_data=True)
        self.assertEqual([p["filename"] for p in parts], ["a.pdf", "b.txt"])
        self.assertEqual([p["size_bytes"] for p in parts], [3, 4])
        self.assertTrue(all(p["sha256"] for p in parts))
        # 入库元数据（collect）与下载用的清单必须一致
        info = mparser.parse_message(raw)
        self.assertEqual([a["filename"] for a in info["attachments"]],
                         [p["filename"] for p in parts])
        self.assertEqual([a["sha256"] for a in info["attachments"]],
                         [p["sha256"] for p in parts])


# ==========================================================================
# E. Worker 契约闭环
# ==========================================================================
class TestWorkerContract(Base):
    def test_full_loop(self):
        self.add(mk_msg("<wc@x>", "契约闭环", "a@b.com", classification=CLASS_REPLY))
        mid = self.repo.db.query_one("SELECT message_id FROM messages LIMIT 1")["message_id"]
        j = dq.ensure_job(self.repo, self.cfg, mid)

        claimed = wc.claim(self.repo, self.cfg, worker="wb")
        self.assertIsNotNone(claimed["job"])
        jid = claimed["job"]["job_id"]

        brief = wc.get_context(self.repo, self.cfg, jid, worker="wb")
        self.assertIn("instructions", brief)
        self.assertIn("context", brief)
        self.assertIn("gate", brief)
        self.assertEqual(brief["stage"], "plan")

        p = wc.submit_plan(self.repo, self.cfg, jid, {
            "intent": "acknowledge", "questions_to_answer": ["确认收到"],
            "facts_to_include": [], "missing_information": [],
            "tone": "professional", "language": "zh"}, worker="wb")
        self.assertFalse(p["needs_input"])

        d = wc.submit_draft(self.repo, self.cfg, jid, "您好，已收到。", worker="wb")
        self.assertEqual(d["status"], JOB_READY)
        self.assertEqual(self.repo.get_job(jid)["claimed_by"], None,
                         "提交草稿后必须释放租约")

        # 人类侧
        appr = dq.approve(self.repo, self.cfg, jid)
        self.assertIn("approve_token", appr)

    def test_claim_empty(self):
        res = wc.claim(self.repo, self.cfg)
        self.assertIsNone(res["job"])

    def test_worker_cannot_touch_other_lease(self):
        self.add(mk_msg("<wc2@x>", "租约保护", "a@b.com", classification=CLASS_REPLY))
        mid = self.repo.db.query_one("SELECT message_id FROM messages LIMIT 1")["message_id"]
        dq.ensure_job(self.repo, self.cfg, mid)
        claimed = wc.claim(self.repo, self.cfg, worker="wb1")
        jid = claimed["job"]["job_id"]
        with self.assertRaises(wc.ContractError):
            wc.get_context(self.repo, self.cfg, jid, worker="wb2")

    def test_plan_validation_error(self):
        self.add(mk_msg("<wc3@x>", "校验", "a@b.com", classification=CLASS_REPLY))
        mid = self.repo.db.query_one("SELECT message_id FROM messages LIMIT 1")["message_id"]
        dq.ensure_job(self.repo, self.cfg, mid)
        jid = wc.claim(self.repo, self.cfg, worker="wb")["job"]["job_id"]
        with self.assertRaises(wc.ContractError):
            wc.submit_plan(self.repo, self.cfg, jid, {"intent": "不存在的意图"}, worker="wb")

    def test_draft_rejects_empty(self):
        self.add(mk_msg("<wc4@x>", "空草稿", "a@b.com", classification=CLASS_REPLY))
        mid = self.repo.db.query_one("SELECT message_id FROM messages LIMIT 1")["message_id"]
        dq.ensure_job(self.repo, self.cfg, mid)
        jid = wc.claim(self.repo, self.cfg, worker="wb")["job"]["job_id"]
        with self.assertRaises(wc.ContractError):
            wc.submit_draft(self.repo, self.cfg, jid, "   ", worker="wb")


# ==========================================================================
# F. 事实抽取
# ==========================================================================
class TestFactExtractor(Base):
    def test_deadlines(self):
        d = fx.extract_deadlines("请于 2026-09-23 12:00 前提交，截止时间不变。")
        self.assertTrue(d)
        self.assertIn("2026-09-23", d[0]["dates"])
        self.assertIn("12:00", d[0]["times"])
        d2 = fx.extract_deadlines("deadline: 9/25")
        self.assertTrue(d2)

    def test_questions_and_commitments(self):
        q = fx.extract_questions("你下周方便吗？申报方向怎么定？好的。")
        self.assertTrue(len(q) >= 2)
        c = fx.extract_commitments("我会在 9/20 前反馈。谢谢。")
        self.assertEqual(len(c), 1)
        self.assertIn("我会", c[0]["text"])

    def test_money_and_sensitive(self):
        money = fx.extract_money("资助经费 100万元/年")
        self.assertTrue(any("100" in m and "万" in m for m in money), money)
        hits = fx.sensitive_hits("身份证 110101199003071234")
        self.assertIn("id_card", hits)
        self.assertEqual(fx.sensitive_hits("普通正文"), [])


# ==========================================================================
# G. 性能（§38）
# ==========================================================================
class TestPerformance(Base):
    N = 5000

    @classmethod
    def setUpClass(cls):
        cls._perf = None

    def _seed(self):
        if TestPerformance._perf:
            return TestPerformance._perf
        msgs, threads = [], []
        base_ts = util.to_epoch("2026-09-01T00:00:00+08:00")
        for i in range(self.N):
            tid = "thr_perf_%d" % (i // 8)
            ts = base_ts + i * 600
            iso = util.now().fromtimestamp(ts, tz=util.TZ_CST).replace(
                microsecond=0).isoformat()
            msgs.append({
                "message_id": "<perf%d@x>" % i,
                "account": SELF,
                "folder": "INBOX" if i % 3 else "Sent",
                "uid": 10000 + i,
                "thread_id": tid,
                "subject": "性能测试邮件 %d 葡萄机器人" % i,
                "from_addr": "user%d@example.com" % (i % 400),
                "from_name": "用户%d" % (i % 400),
                "to_addrs": [SELF],
                "date_iso": iso,
                "internal_ts": ts,
                "unread": i % 5 == 0,
                "has_attachments": i % 7 == 0,
                "attachment_names": ["a.pdf"] if i % 7 == 0 else [],
                "body_text": ("关于 LiDAR 标定与产学研申报的正文内容 %d。" % i) * 30,
                "classification": [CLASS_IMPORTANT, CLASS_REPLY, CLASS_READ,
                                   CLASS_SYSTEM, CLASS_DISMISS][i % 5],
                "source": "imap",
            })
        for i in range(0, self.N, 8):
            tid = "thr_perf_%d" % (i // 8)
            inbound = (i // 8) % 2 == 0
            threads.append({
                "thread_id": tid, "account": SELF,
                "subject": "性能测试邮件 %d 葡萄机器人" % i,
                "participants": [SELF, "user%d@example.com" % (i % 400)],
                "message_count": min(8, self.N - i),
                "latest_message_id": "<perf%d@x>" % (i + min(7, self.N - i - 1)),
                "latest_ts": base_ts + (i + 7) * 600,
                "last_inbound_ts": base_ts + i * 600 if inbound else 0,
                "last_outbound_ts": 0 if inbound else base_ts + i * 600,
                "needs_reply": inbound, "waiting_for_me": inbound,
                "waiting_for_other": not inbound,
                "classification": CLASS_REPLY if inbound else None,
                "workflow_state": WF_WAITING_FOR_ME if inbound else WF_WAITING_FOR_OTHER,
                "priority": 70 if inbound else 40,
            })
        t0 = time.time()
        self.repo.bulk_upsert_messages(msgs)
        self.repo.bulk_upsert_threads(threads)
        TestPerformance._perf = {"seed_seconds": round(time.time() - t0, 2),
                                 "messages": self.repo.count_messages()}

    def _time(self, fn):
        t0 = time.time()
        r = fn()
        return (time.time() - t0) * 1000.0, r

    def test_common_ops_under_200ms(self):
        self._seed()
        self.assertGreaterEqual(self.repo.count_messages(), self.N)

        ms_buckets, data = self._time(lambda: bucketmod.compute(self.repo, self.cfg, limit=100))
        ms_list, rows = self._time(lambda: self.repo.list_messages(folder="INBOX", limit=60))
        ms_msg, _ = self._time(lambda: self.repo.get_message("<perf2500@x>"))
        ms_search, res = self._time(lambda: searchmod.run(self.repo, "subject:葡萄 after:7d"))
        ms_counts, _ = self._time(lambda: self.repo.counts_summary())
        ms_brief, _ = self._time(lambda: briefmod.build(self.repo, self.cfg))

        print("\n[性能] N=%d  seed=%ss" % (self.N, TestPerformance._perf["seed_seconds"]))
        for name, ms in (("buckets", ms_buckets), ("list_messages", ms_list),
                         ("get_message", ms_msg), ("search", ms_search),
                         ("counts_summary", ms_counts), ("brief", ms_brief)):
            print("  %-16s %7.1f ms" % (name, ms))

        self.assertLess(ms_buckets, 200, "工作桶计算必须 < 200ms")
        self.assertLess(ms_list, 200, "邮件列表必须 < 200ms")
        self.assertLess(ms_msg, 200, "单封读取必须 < 200ms")
        self.assertLess(ms_search, 200, "搜索必须 < 200ms")
        self.assertLess(ms_counts, 200)
        self.assertGreaterEqual(data["counts"]["ACTION_REQUIRED"], 1)
        self.assertEqual(len(rows), 60)
        self.assertGreaterEqual(res["count"], 1)

    def test_no_full_scan_for_listing(self):
        self._seed()
        ms, _ = self._time(lambda: self.repo.list_messages(folder="INBOX", limit=30))
        self.assertLess(ms, 50, "列表查询应走索引，不该全表扫描")


class TestFolderNameEncoding(Base):
    """文件夹名的 modified UTF-7 编解码（RFC 3501 §5.1.3）。

    实测抓到两个缺陷，都不是理论的：
      1. 编码时用了**标准 base64**，而 modified UTF-7 的字母表把 `/` 换成 `,`。
         对「未来电池中心」这种名字，base64 里正好出现 `/`，编出来是
         `&ZypnZXU1bGBOLV/D-`，而服务器上的是 `&ZypnZXU1bGBOLV,D-` ——
         status() 于是去查一个不存在的文件夹。
         症状很迷惑人：list/select 都正常（select 走的是另一条路径），
         容易误判成「服务器上没有这个文件夹」，进而把文件夹从侧边栏删掉。
      2. 解码时把 `&-`（转义后的字面 &）当成空段，名字里带 & 就会丢字符。
    """

    def test_encode_matches_server_form(self):
        cases = {
            "专利代理": "&ThNSKU7jdAY-",
            "未来电池中心": "&ZypnZXU1bGBOLV,D-",   # 服务器实际回报的形式
        }
        for name, enc in cases.items():
            got = imap_client.mutf7_encode(name)
            self.assertEqual(got, enc, name)
            self.assertNotIn("/", got, "modified UTF-7 的字母表里没有 /")
            self.assertEqual(imap_client.mutf7_decode(enc), name)

    def test_ascii_and_literal_ampersand(self):
        self.assertEqual(imap_client.mutf7_encode("INBOX"), "INBOX")
        self.assertEqual(imap_client.mutf7_encode("A&B"), "A&-B")
        self.assertEqual(imap_client.mutf7_decode("A&-B"), "A&B",
                         "&- 必须是字面 &，不是空段")

    def test_roundtrip_is_lossless(self):
        for name in ("未来电池中心", "专利代理", "收件箱 备份",
                     "Tom&Jerry 邮件", "mixed 混合 名字", "INBOX"):
            enc = imap_client.mutf7_encode(name)
            self.assertEqual(imap_client.mutf7_decode(enc), name, name)


class TestIdleRawRead(Base):
    """IDLE 必须绕开 imaplib 的缓冲层读 socket。

    踩过的坑：imaplib 的 readline 走 `sock.makefile('rb')` 缓冲层，
    socket 一超时缓冲对象就损坏，之后所有读取都抛
    `OSError: cannot read from timed out object`。
    IDLE 的本质就是「长期无数据 + 周期性超时」，
    于是表现为「每 30 秒断一次、永远收不到新邮件事件」。
    """

    class _Timeout:
        pass

    class _FakeSock:
        def __init__(self, script):
            self.script = list(script)
            self.saw_timeouts = 0

        def settimeout(self, t):
            pass

        def recv(self, n):
            if not self.script:
                return b""
            item = self.script.pop(0)
            if item is TestIdleRawRead._Timeout:
                self.saw_timeouts += 1
                raise socket.timeout("timed out")
            return item

    class _FakeM:
        def __init__(self, sock):
            self.sock = sock

    def test_raw_readline_survives_timeout(self):
        from mail_workbench.mail.imap_client import _raw_readline
        sock = self._FakeSock([self._Timeout, b"+ idling\r\n"])
        buf = bytearray()
        with self.assertRaises((socket.timeout, TimeoutError)):
            _raw_readline(sock, buf)
        # 关键：超时之后仍然能正常读到下一行（缓冲层方案在这里就废了）
        line = _raw_readline(sock, buf)
        self.assertEqual(line, b"+ idling\r\n")
        self.assertEqual(sock.saw_timeouts, 1)

    def test_raw_readline_reassembles_partial_chunks(self):
        from mail_workbench.mail.imap_client import _raw_readline
        sock = self._FakeSock([b"* 5 EX", b"ISTS\r\n", b"* 6 EXPUNGE\r\n"])
        buf = bytearray()
        self.assertEqual(_raw_readline(sock, buf), b"* 5 EXISTS\r\n")
        self.assertEqual(_raw_readline(sock, buf), b"* 6 EXPUNGE\r\n")

    def test_raw_readline_returns_none_on_eof(self):
        from mail_workbench.mail.imap_client import _raw_readline
        sock = self._FakeSock([])
        self.assertIsNone(_raw_readline(sock, bytearray()))

    def test_wait_continuation_tolerates_timeout(self):
        """等 continuation 时超时不应判定失败 —— 要继续等。"""
        from mail_workbench.mail.imap_client import IdleWatcher
        w = IdleWatcher(self.cfg)
        sock = self._FakeSock([self._Timeout, self._Timeout, b"+ idling\r\n"])
        m = self._FakeM(sock)
        self.assertTrue(w._wait_continuation(m, b"TAG1", bytearray(), timeout=5))
        self.assertEqual(sock.saw_timeouts, 2)

    def test_wait_continuation_reports_tagged_rejection(self):
        from mail_workbench.mail.imap_client import IdleWatcher
        w = IdleWatcher(self.cfg)
        sock = self._FakeSock([b"TAG1 BAD IDLE not supported\r\n"])
        m = self._FakeM(sock)
        self.assertFalse(w._wait_continuation(m, b"TAG1", bytearray(), timeout=3))

    def test_is_idle_unsupported_detector(self):
        from mail_workbench.mail.imap_client import IdleWatcher
        w = IdleWatcher(self.cfg)
        self.assertTrue(w._is_idle_unsupported("TAG BAD unknown command IDLE"))
        self.assertTrue(w._is_idle_unsupported("IDLE not supported"))
        self.assertFalse(w._is_idle_unsupported("IDLE 未收到 continuation"))

    def test_idle_watcher_status_shape(self):
        from mail_workbench.mail.imap_client import IdleWatcher
        w = IdleWatcher(self.cfg)
        st = w.status()
        for k in ("mode", "folder", "last_event_at", "last_error", "reconnects",
                  "fallback_reason", "running"):
            self.assertIn(k, st)


# ==========================================================================
# H. 存储层
# ==========================================================================
class TestStorage(Base):
    def test_migration_versioned_and_idempotent(self):
        import mail_workbench.storage.database as dbmod
        self.assertEqual(self.db.version(), dbmod.SCHEMA_VERSION)
        # 再跑一次必须幂等：版本不变、不抛错
        self.assertEqual(self.db.migrate(), dbmod.SCHEMA_VERSION)
        self.assertEqual(self.db.version(), dbmod.SCHEMA_VERSION)
        stats = self.db.stats()
        for t in ("messages", "threads", "candidates", "draft_jobs", "snoozes",
                  "followups", "contacts", "attachments"):
            self.assertIn(t, stats)

    def test_migration_v1_to_v2_upgrades_in_place(self):
        """v1 老库必须能原地升到 v2，且已有数据不丢（规范 §36）。"""
        import mail_workbench.storage.database as dbmod
        self.assertGreaterEqual(dbmod.SCHEMA_VERSION, 2)
        # 手工造一个只跑过 migration 1 的老库
        path = os.path.join(self.tmp.name, "old_v1.sqlite")
        old = dbmod.Database(path)
        try:
            c = old.conn()
            for sql in dbmod.MIGRATIONS[1]:
                c.execute(sql)
            c.execute("PRAGMA user_version=1")
            c.commit()
            old.execute("INSERT INTO messages(message_id, account, subject) VALUES(?,?,?)",
                        ("<legacy@x>", "t@x", "老邮件"))
            self.assertEqual(old.version(), 1)
            # v2 索引此时还不存在
            idx_before = {r["name"] for r in old.query(
                "SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertNotIn("idx_messages_subject_norm", idx_before)

            # 升到最新版：数据必须还在，v2 的索引必须建起来
            self.assertEqual(old.migrate(), dbmod.SCHEMA_VERSION)
            self.assertEqual(old.version(), dbmod.SCHEMA_VERSION)
            row = old.query_one("SELECT * FROM messages WHERE message_id='<legacy@x>'")
            self.assertIsNotNone(row, "迁移不得丢已有数据")
            self.assertEqual(row["subject"], "老邮件")
            idx_after = {r["name"] for r in old.query(
                "SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertIn("idx_messages_subject_norm", idx_after)
            self.assertIn("idx_messages_source", idx_after)
            # 再跑一次仍幂等
            self.assertEqual(old.migrate(), dbmod.SCHEMA_VERSION)
        finally:
            old.close()

    def test_reject_downgrade(self):
        with self.assertRaises(RuntimeError):
            self.db.migrate(target=0)

    def test_kv_roundtrip(self):
        self.repo.kv_set("k", {"a": [1, 2, 3]})
        self.assertEqual(self.repo.kv_get("k"), {"a": [1, 2, 3]})

    def test_contacts(self):
        self.repo.touch_contact("a@b.com", "张三", "2026-09-18T10:00:00+08:00")
        self.repo.touch_contact("a@b.com", "张三")
        c = self.repo.get_contact("a@b.com")
        self.assertEqual(c["message_count"], 2)
        self.repo.set_contact("a@b.com", organization="上海交大", preferred_language="zh",
                              relationship="学生")
        c = self.repo.get_contact("a@b.com")
        self.assertEqual(c["organization"], "上海交大")

    def test_undo_log(self):
        uid = self.repo.push_undo("test", {"message_id": "x"})
        self.assertTrue(uid)
        self.assertEqual(self.repo.last_undo()["action"], "test")
        self.repo.mark_undone(uid)
        self.assertIsNone(self.repo.last_undo())

    def test_draft_memory_toggle(self):
        self.repo.add_draft_memory("removed_phrase", "此致敬礼")
        self.assertEqual(len(self.repo.draft_memory()), 1)
        self.repo.clear_draft_memory()
        self.assertEqual(self.repo.draft_memory(), [])
        cfg = dict(self.cfg, draft_memory_enabled=False)
        self.add(mk_msg("<dm@x>", "记忆开关", "a@b.com", classification=CLASS_REPLY))
        mid = self.repo.db.query_one("SELECT message_id FROM messages LIMIT 1")["message_id"]
        pkg = cbm.MailContextBuilder(self.repo, cfg).build(message_id=mid)
        self.assertFalse(pkg["memory_hints"]["enabled"])


# ==========================================================================
# I. 服务器路由（不发网络请求，仅验证分发与关键策略）
# ==========================================================================
class TestServerRouting(Base):
    def test_router_matches(self):
        from mail_workbench.server import ROUTER
        fn, params = ROUTER.match("GET", "/api/draft-jobs/next")
        self.assertIsNotNone(fn)
        fn, params = ROUTER.match("GET", "/api/draft-jobs/job_abc/context")
        self.assertIsNotNone(fn)
        self.assertEqual(params["job_id"], "job_abc")
        fn, _ = ROUTER.match("POST", "/api/drafts/job_abc/approve")
        self.assertIsNotNone(fn)
        fn, _ = ROUTER.match("POST", "/api/actions/batch")
        self.assertIsNotNone(fn)
        fn, _ = ROUTER.match("GET", "/api/nope")
        self.assertIsNone(fn)

    def test_no_ai_send_route(self):
        from mail_workbench.server import ROUTER
        paths = [rx.pattern for rx, _ in ROUTER.posts]
        self.assertFalse(any("draft-jobs" in p and "send" in p for p in paths),
                         "Worker 契约里不允许存在发送端点")

    def test_utils(self):
        self.assertEqual(util.normalize_subject("Re: Re: 关于会议"), "关于会议")
        self.assertEqual(util.normalize_subject("答复：转发：Re: 主题"), "主题")
        self.assertEqual(util.parse_recipients("a@b.com, B <c@d.com>;e@f.com"),
                         ["a@b.com", "c@d.com", "e@f.com"])
        self.assertEqual(util.guess_language("这是中文邮件"), "zh")
        self.assertEqual(util.guess_language("This is English"), "en")
        self.assertIn("***", util.redact("me@example.edu"))
        self.assertTrue(util.parse_iso("2026-09-18T10:00:00+08:00"))

    def test_synthetic_message_id_stable(self):
        a = mparser.synth_message_id(SELF, "INBOX", 12345)
        b = mparser.synth_message_id(SELF, "INBOX", 12345)
        c = mparser.synth_message_id(SELF, "INBOX", 12346)
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_thread_key_reason(self):
        tid, reason = mparser.build_thread_key({"references_ids": ["<root@x>"], "subject": "a"})
        self.assertTrue(reason.startswith("references:"))
        tid2, reason2 = mparser.build_thread_key({"subject": "Re: 项目"})
        self.assertTrue(reason2.startswith("subject:"))

    def test_health_endpoint_components(self):
        """规范 §29：/api/health 必须覆盖 IMAP/SMTP/Foxmail/队列/WorkBuddy/Scheduler。

        探活一律打桩，测试不联网。
        """
        from mail_workbench.server import App
        from mail_workbench.mail import imap_client, smtp_client, foxmail_index
        o1 = imap_client.check_connection
        o2 = smtp_client.verify_connection
        o3 = foxmail_index.stats
        imap_client.check_connection = lambda cfg: {"ok": True, "latency_ms": 1.0,
                                                    "inbox": {"folder": "INBOX"}}
        smtp_client.verify_connection = lambda cfg: {"ok": True, "latency_ms": 1.0}
        foxmail_index.stats = lambda p: {"ok": True, "declared_records": 1}
        app = App(self.cfg)
        try:
            h = app.health()
        finally:
            imap_client.check_connection = o1
            smtp_client.verify_connection = o2
            foxmail_index.stats = o3
            app.repo.db.close()
        self.assertIn(h["status"], ("healthy", "warning", "error"))
        for k in ("imap", "smtp", "foxmail_index", "draft_queue", "workbuddy",
                  "scheduler", "imap_idle", "storage"):
            self.assertIn(k, h["components"], "health 缺少组件 %s" % k)
            self.assertIn(h["components"][k]["status"], ("healthy", "warning", "error"))
        self.assertEqual(h["components"]["imap"]["status"], "healthy")
        self.assertIn("recent_events", h)
        # Repo 必须透传底层 stats（健康检查依赖它）
        self.assertIn("messages", Repo(self.db).stats())


class TestTestHarnessStaysInsideTempDir(unittest.TestCase):
    """测试自己不许往项目目录里写东西。

    这条来自一次真实的隐私/整洁事故：在**发布目录**里跑
    `python -m unittest discover` 之后，`attachments/` 下多出两份
    7 字节的测试固件（`<sha16>_简历.pdf`、`<sha16>_report.pdf`）。
    成因是 `make_cfg` 没设 `attachments_dir`，`attachment_store` 于是回落到
    `<project_root>/attachments` —— 而跑测试时 cwd 就是发布目录。
    接着把发布目录整个上传，固件就跟着走了（`.gitignore` 拦得住 git，
    拦不住打包/上传那一步）。

    所以这里把「所有会落盘的目录都必须在临时目录里」钉死。
    """

    def test_all_output_dirs_are_derived_from_tmp_db_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            db = os.path.join(tmp, "t.db")
            cfg = make_cfg(db)
            for key in ("attachments_dir", "logs_dir", "data_dir"):
                p = os.path.abspath(cfg[key])
                self.assertTrue(
                    p.startswith(os.path.abspath(tmp)),
                    "%s 必须落在临时目录里，实际是 %s —— "
                    "否则跑测试会往项目目录写文件，打包时被一起带走" % (key, p))

    def test_attachment_store_writes_only_under_attachments_dir(self):
        """再验一次实际落盘路径（不信 cfg，信附件存储真正用的那个目录）。"""
        from mail_workbench.mail import attachment_store as ats
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(os.path.join(tmp, "t.db"))
            root = os.path.abspath(ats.ensure_dir(cfg))
            self.assertTrue(root.startswith(os.path.abspath(tmp)),
                            "attachment_store 的落盘根目录跑出临时目录了：%s" % root)
            p = os.path.abspath(ats.local_path(cfg, "a" * 64, "简历.pdf"))
            self.assertTrue(p.startswith(os.path.abspath(tmp)),
                            "落盘文件路径跑出临时目录了：%s" % p)
            # 文件名消毒也要仍然生效（中文 + 路径穿越）
            self.assertNotIn(os.sep, ats.safe_filename("../../x/简历.pdf"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
