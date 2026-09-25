# -*- coding: utf-8 -*-
"""Mail Workbench —— 本地 HTTP 服务。

设计定位（规范 §1）：
    Mail Workbench = 邮件基础设施 + 状态管理 + 工作流 + 队列 + UI
    WorkBuddy      = 邮件理解 + 推理 + 摘要 + 回复规划 + 草稿生成
    Foxmail        = 桌面客户端 + 历史邮件来源 + 备用完整客户端
    用户           = 最终决策 + 草稿审核 + 修改 + 发送确认

启动：
    python -m mail_workbench.server            # 正式启动（IDLE + scheduler）
    python -m mail_workbench.server --no-idle   # 只开 UI（离线调试）

只监听 127.0.0.1，仅本机可访问；不引入任何第三方依赖（§34 不过度设计）。
"""
from __future__ import annotations

import argparse
import json
import os
import queue
import re
import socket
import sys
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import config as cfgmod
from . import metrics as metricsmod
from . import util
from .constants import (
    BUCKET_HINT, BUCKET_LABEL, BUCKETS, CLASSIFICATIONS, CLASS_TREE_DOT,
    CLASS_TREE_LABEL, DRAFT_MODE_LABEL, DRAFT_MODES, FOLDER_FOXMAIL,
    REPLY_CONTROL_GROUP, REPLY_CONTROL_LABEL, REPLY_CONTROLS,
    SYSTEM_FOLDERS, WF_LABEL,
)
from .draft import queue as dq
from .draft import recovery as rcv
from .draft import templates as draft_templates
from .draft import worker_contract as wc
from .mail import attachment_store as ats
from .mail import foxmail_index, imap_client, smtp_client
from .mail import sync_engine as syncmod
from .storage import search as searchmod
from .storage.database import open_db
from .storage.repo import Repo
from .thread import aggregator as agg
from .thread import context_builder as cbm
from .workflow import actions as actionsmod
from .workflow import brief as briefmod
from .workflow import buckets as bucketmod
from .workflow import scheduler as schedmod
from .workflow import snooze_manager as snzm
from .workflow import followup_manager as fum
from .workflow.candidate_detector import CandidateDetector

UI_PLACEHOLDER = """<!DOCTYPE html><html><head><meta charset="utf-8">
<title>Mail Workbench V2</title></head><body style="font-family:system-ui;padding:40px">
<h2>UI 未找到</h2><p>缺少 <code>mail_workbench/ui/index.html</code>。</p>
<p>仍可使用全部 API：<code>/api/health</code>、<code>/api/buckets</code>、
<code>/api/draft-jobs/next</code>。</p></body></html>"""


# ==========================================================================
# 事件总线（SSE）
# ==========================================================================
class EventBus:
    """极简发布订阅：给每个 SSE 连接一个有界队列，慢了就丢旧事件。"""

    def __init__(self, maxsize: int = 200, log=None):
        self.subs = set()
        self.lock = threading.Lock()
        self.maxsize = maxsize
        self.log = log
        self.recent = []

    def subscribe(self) -> queue.Queue:
        q = queue.Queue(maxsize=self.maxsize)
        with self.lock:
            self.subs.add(q)
        return q

    def unsubscribe(self, q) -> None:
        with self.lock:
            self.subs.discard(q)

    def publish(self, event: dict) -> None:
        if not isinstance(event, dict):
            return
        if "at" not in event:
            event = dict(event, at=util.now_iso())
        self.recent.append(event)
        self.recent = self.recent[-50:]
        with self.lock:
            subs = list(self.subs)
        for q in subs:
            try:
                q.put_nowait(event)
            except queue.Full:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    pass

    def subscriber_count(self) -> int:
        with self.lock:
            return len(self.subs)


# ==========================================================================
# 应用容器
# ==========================================================================
class App:
    def __init__(self, cfg: dict, log=None):
        self.cfg = cfg
        self.log = log
        # 注意：open_db() 返回 Database（裸连接管理器），业务层统一用 Repo。
        # 必须显式传 cfg["db_path"]，不能用 default_db()（它会去读全局配置，
        # 测试/多实例场景会连到错误的库）。
        self.repo = Repo(open_db(cfg["db_path"], log=log))
        self.bus = EventBus(log=log)
        self.sync = syncmod.SyncEngine(self.repo, cfg, on_event=self.bus.publish, log=log)
        self.scheduler = schedmod.Scheduler(self.repo, cfg,
                                           on_event=self.bus.publish, log=log)
        self.rules = None
        self._metrics_cache = {"at": 0, "data": None}
        self.started_at = util.now_iso()
        self.errors = []

    def _log(self, msg: str) -> None:
        """App 可能在没有 logger 的场景被构造（测试 / 嵌入），日志必须可空。"""
        if not self.log:
            return
        try:
            self.log(msg)
        except Exception:
            pass

    # ---------------- 生命周期 ----------------
    def start(self, idle: bool = True, scheduler: bool = True):
        cfg = self.cfg
        probe = imap_client.check_connection(cfg)
        if not probe.get("ok"):
            self._log("IMAP 探活失败：%s（UI 仍会启动，可稍后重试同步）" % probe.get("error"))
        else:
            self._log("IMAP 就绪 %s，延迟 %sms" % (probe.get("inbox", {}).get("folder"),
                                              probe.get("latency_ms")))
        if idle:
            self.sync.start_idle()
            self._log("IMAP IDLE 已启动（事件驱动）")
        if scheduler:
            self.scheduler.start()
            self._log("本地 scheduler 已启动（snooze / followup / recovery / brief）")

    def stop(self):
        try:
            self.scheduler.stop()
        except Exception:
            pass
        try:
            self.sync.stop()
        except Exception:
            pass
        self.repo.checkpoint()

    # ---------------- 派生视图 ----------------
    def buckets(self, limit: int = 100) -> dict:
        return bucketmod.compute(self.repo, self.cfg, limit=limit)

    def metrics(self, ttl: float = 5.0) -> dict:
        now = time.time()
        if self._metrics_cache["data"] and now - self._metrics_cache["at"] < ttl:
            return self._metrics_cache["data"]
        data = metricsmod.snapshot(self.repo)
        data["windows"] = metricsmod.window_stats(self.repo, days=7)
        self._metrics_cache = {"at": now, "data": data}
        return data

    def health(self) -> dict:
        cfg = self.cfg
        t0 = time.time()
        imap = imap_client.check_connection(cfg)
        smtp = smtp_client.verify_connection(cfg)
        fx = foxmail_index.stats(cfg.get("foxmail_index_path") or "")
        jobs = self.repo.job_counts()
        queued = jobs.get("queued", 0)
        generating = jobs.get("generating", 0)
        idle_state = self.sync.health().get("idle") or {}
        workers = 1 if generating else 0

        problems = []
        if not imap.get("ok"):
            problems.append("IMAP 不可用")
        if not smtp.get("ok"):
            problems.append("SMTP 不可用")
        if not fx.get("ok"):
            problems.append("Foxmail 索引不可用（历史检索受限）")
        if idle_state.get("mode") == "polling":
            problems.append("IMAP IDLE 降级为本地轮询")
        if queued and not generating:
            problems.append("有任务排队但无 worker 认领（可运行 /api/maintenance/recovery）")

        def status_of(ok, warn=False):
            if not ok:
                return "error"
            return "warning" if warn else "healthy"

        components = {
            "imap": {"status": status_of(imap.get("ok")), "detail": {
                "latency_ms": imap.get("latency_ms"), "error": imap.get("error"),
                "inbox": imap.get("inbox")}},
            "smtp": {"status": status_of(smtp.get("ok")), "detail": {
                "latency_ms": smtp.get("latency_ms"), "error": smtp.get("error"),
                "note": "只做登录探活，绝不投递"}},
            "foxmail_index": {"status": status_of(fx.get("ok")), "detail": fx},
            "draft_queue": {"status": status_of(True, warn=bool(queued)), "detail": {
                "counts": jobs, "queued": queued, "generating": generating}},
            "workbuddy": {"status": status_of(True, warn=bool(queued)), "detail": {
                "contract": "/api/draft-jobs/next",
                "pending_claims": queued,
                "note": "WorkBuddy 通过 Worker 契约认领任务；未认领时会显示 warning"}},
            "scheduler": {"status": status_of(self.scheduler.status()["running"] is not None),
                          "detail": self.scheduler.status()},
            "imap_idle": {"status": status_of(idle_state.get("running", False),
                                              warn=idle_state.get("mode") == "polling"),
                          "detail": idle_state},
            "storage": {"status": "healthy", "detail": self.repo.stats()},
            "sse_clients": {"status": "healthy", "detail": {"count": self.bus.subscriber_count()}},
        }

        overall = "healthy"
        if any(c["status"] == "error" for c in components.values()):
            overall = "error"
        elif any(c["status"] == "warning" for c in components.values()):
            overall = "warning"

        return {
            "status": overall,
            "checked_at": util.now_iso(),
            "uptime_seconds": round(util.hours_between(self.started_at, util.now_iso()) * 3600, 1),
            "elapsed_ms": round((time.time() - t0) * 1000, 1),
            "problems": problems,
            "components": components,
            "config": {
                "user": cfg.get("user"),
                "imap": "%s:%s" % (cfg.get("imap_host"), cfg.get("imap_port")),
                "smtp": "%s:%s" % (cfg.get("smtp_host"), cfg.get("smtp_port")),
                "idle_enabled": bool(cfg.get("idle_enabled")),
                "scheduler_enabled": bool(cfg.get("scheduler_enabled")),
                "auto_prepare_drafts": bool(cfg.get("auto_prepare_drafts")),
                "draft_memory_enabled": bool(cfg.get("draft_memory_enabled")),
                "note": "UI 只显示 Healthy/Warning/Error；详细日志在 logs/ 目录。",
            },
            "recent_events": self.bus.recent[-10:],
        }

    # ---------------- 发送（人类确认后的唯一出站路径）----------------
    def send_draft(self, job_id: str, token: str, confirm: str = "",
                   followup_days=None) -> dict:
        ok, reason, job = dq.authorize_send(self.repo, job_id, token)
        if not ok:
            return {"ok": False, "http": 403, "error": reason,
                    "policy": "AI NEVER SENDS EMAIL WITHOUT EXPLICIT HUMAN CONFIRMATION."}
        # 双重确认：必须显式带上 confirm=SEND，避免误触与自动化调用
        if (confirm or "").strip().upper() != "SEND":
            return {"ok": False, "http": 400,
                    "error": '缺少显式确认字段 confirm="SEND"（防止脚本/AI 误触发送）'}

        msg = self.repo.get_message(job["message_id"]) or {}
        in_reply = msg.get("in_reply_to") or msg.get("message_id") or ""
        refs = list(msg.get("references_ids") or [])
        if msg.get("message_id") and msg["message_id"] not in refs:
            refs.append(msg["message_id"])
        signature = self.repo.kv_get("signature") or ""
        raw = smtp_client.build_message(
            self.cfg, job["recipients"], job["subject"], job.get("draft_text") or "",
            in_reply_to=in_reply, references=refs, signature=signature)

        res = smtp_client.send(self.cfg, raw, job["recipients"])
        if not res.get("ok"):
            dq.fail(self.repo, self.cfg, job_id,
                    error="SMTP: %s" % res.get("error"), retryable=False)
            return {"ok": False, "http": 502, "error": "发送失败：%s" % res.get("error")}

        sent_mid = _extract_message_id(raw)
        # 写入已发送副本（best effort，失败不影响"已发送"事实）
        appended = False
        try:
            c = imap_client.ImapClient(self.cfg, log=self.log)
            c.connect()
            c.append(self.cfg.get("folder_sent", "Sent"), raw, flags="(\\Seen)")
            appended = True
            c.close()
        except Exception as e:
            self._log("写入已发送失败（不影响发送结果）：%s" % e)

        out = dq.mark_sent(self.repo, self.cfg, job_id, sent_message_id=sent_mid,
                          followup_days=followup_days, actor="user")
        # 把刚发出的信写回本地库并重算线程方向。
        #
        # 不做这一步的后果（用户实测反馈）：「我处理完某封邮件了，
        # 但『待我处理』里还有它，计数也不动，得等我去点『完成』才变」。
        # 原因是线程的「最新一封」还停在对方那封来信上 —— waiting_for_me 仍为真，
        # 于是它继续挂在「待我处理」，直到下次 IMAP 同步把 Sent 里的副本拉回才纠正。
        # 可我们**此刻就已经知道**信发出去了（SMTP 成功 + append 成功），
        # 没道理让它继续显示成「待我处理」。
        self._remember_sent_copy(job, msg, sent_mid, in_reply, refs)
        self.bus.publish({"type": "job", "job_id": job_id, "status": "sent",
                          "subject": job.get("subject")})
        return {"ok": True, "job": dq.to_public(out), "smtp": res,
                "sent_message_id": sent_mid, "appended_to_sent": appended,
                "message": "已发送，并%s写入「已发送」"
                           % ("已" if appended else "尝试")}

    def _remember_sent_copy(self, job: dict, origin: dict, sent_mid: str,
                            in_reply: str, refs: list) -> None:
        """见模块级 remember_sent_copy —— 这里只做转发（逻辑放外面便于单测）。"""
        remember_sent_copy(self.repo, self.cfg, job, origin, sent_mid,
                           in_reply, refs, log=self._log)

    # ---------------- 写邮件（人工起草，非 AI 草稿）----------------
    # 与「AI 草稿」是两条不同的通道，但共用同一批底层 helper：
    #   build_message → smtp_client.send → imap_client.append(已发送)
    # 刻意不另写一套 SMTP 逻辑 —— 两条发信路径迟早会分叉，
    # 而分叉的那一条往往就是漏掉「留副本」或「确认门禁」的那一条。
    def compose_draft(self, to, subject: str, body: str, cc=None) -> dict:
        """把人工写的信存进服务器草稿箱。**绝不发送**（规范 §33）。"""
        try:
            rcpts = _valid_recipients(to)
        except ValueError as e:
            return {"ok": False, "http": 400, "error": str(e)}
        if not rcpts:
            return {"ok": False, "http": 400, "error": "缺少收件人"}
        if not (body or "").strip():
            return {"ok": False, "http": 400, "error": "正文为空"}
        signature = self.repo.kv_get("signature") or ""
        raw = smtp_client.build_message(self.cfg, rcpts, subject, body,
                                        cc=cc, signature=signature)
        folder = self.cfg.get("folder_drafts", "Drafts")
        try:
            c = imap_client.ImapClient(self.cfg, log=self.log)
            c.connect()
            c.append(folder, raw, flags="(\\Seen \\Draft)")
            c.close()
        except Exception as e:
            return {"ok": False, "http": 502,
                    "error": "写入草稿箱失败：%s" % e}
        self.repo.inc_metric("compose_drafts_saved")
        return {"ok": True, "folder": folder, "recipients": rcpts,
                "message": "已存入「%s」，不会发送（Foxmail 接收后可见）" % folder}

    def compose_send(self, to, subject: str, body: str, cc=None,
                     confirm: str = "") -> dict:
        """发送人工写的新邮件。

        三道闸，与草稿发送一致、刻意保持同一种形状：
          1. 必须显式传 confirm="SEND"
          2. 收件人格式必须过校验
          3. 留副本到「已发送」（失败不算发送失败，但如实告知）
        AI 不得调用本方法（SKILL.md 里已明确）。
        """
        if (confirm or "").strip().upper() != "SEND":
            return {"ok": False, "http": 400,
                    "error": '缺少显式确认字段 confirm="SEND"（防止脚本/AI 误触发送）',
                    "policy": "AI NEVER SENDS EMAIL WITHOUT EXPLICIT HUMAN CONFIRMATION."}
        try:
            rcpts = _valid_recipients(to)
        except ValueError as e:
            return {"ok": False, "http": 400, "error": str(e)}
        if not rcpts:
            return {"ok": False, "http": 400, "error": "缺少收件人"}
        if not (body or "").strip():
            return {"ok": False, "http": 400, "error": "正文为空"}
        if not self.cfg.get("pass"):
            return {"ok": False, "http": 400, "error": "没读到邮箱凭据，无法发送"}

        signature = self.repo.kv_get("signature") or ""
        raw = smtp_client.build_message(self.cfg, rcpts, subject, body,
                                        cc=cc, signature=signature)
        res = smtp_client.send(self.cfg, raw, rcpts)
        if not res.get("ok"):
            self.repo.inc_metric("compose_send_failed")
            return {"ok": False, "http": 502, "error": "发送失败：%s" % res.get("error")}

        sent_mid = _extract_message_id(raw)
        appended = False
        try:
            c = imap_client.ImapClient(self.cfg, log=self.log)
            c.connect()
            c.append(self.cfg.get("folder_sent", "Sent"), raw, flags="(\\Seen)")
            appended = True
            c.close()
        except Exception as e:
            self._log("写入已发送失败（不影响发送结果）：%s" % e)

        self.repo.inc_metric("compose_sent")
        self.bus.publish({"type": "sent", "subject": subject, "message_id": sent_mid})
        return {"ok": True, "sent": True, "to": rcpts, "sent_message_id": sent_mid,
                "appended_to_sent": appended, "smtp": res,
                "message": "已发送，并%s写入「已发送」"
                           % ("已" if appended else "尝试")}


def remember_sent_copy(repo, cfg: dict, job: dict, origin: dict, sent_mid: str,
                       in_reply: str, refs: list, log=None) -> bool:
    """把刚发出的信写成本地一条记录，并重算它所在线程。返回是否写成功。

    **为什么必须做**（用户实测反馈）：
        「我处理完某封邮件了，但『待我处理』里还有它，计数也不动，
          得等我去点『完成』才变。」
    线程的收发方向（waiting_for_me / waiting_for_other）由
    「最新一封的 from_addr 是否属于自己」推导（见 thread/aggregator._self_set）。
    信发出去了却没进库，线程就仍以为「最新一封是对方来信」——
    邮件继续挂在「待我处理」，直到下次 IMAP 同步把 Sent 副本拉回来才纠正。
    可 SMTP 与 append 此刻都已成功，我们已经知道它发出去了。

    **失败不影响「已发送」这个事实**：写不进去只是意味着「要等下次同步」，
    所以这里吞掉异常、记日志，并返回 False，绝不向上抛。
    """
    def _log(m):
        if log:
            try:
                log(m)
            except Exception:
                pass

    if not sent_mid:
        _log("未能取出 Message-ID，跳过写回（下次同步会补上）")
        return False
    try:
        repo.upsert_message({
            "message_id": sent_mid,
            "account": cfg.get("user") or "",
            "folder": cfg.get("folder_sent") or "Sent",
            "thread_id": (origin or {}).get("thread_id"),
            "subject": job.get("subject") or "",
            # from_addr 必须是自己 —— aggregator 靠它认「这封是我发的」
            "from_addr": cfg.get("user") or "",
            "from_name": cfg.get("from_name") or "",
            "to_addrs": list(job.get("recipients") or []),
            "date_iso": util.now_iso(),
            "internal_ts": time.time(),
            "unread": False,
            "body_text": job.get("draft_text") or "",
            "source": "imap",
            "in_reply_to": in_reply,
            "references_ids": list(refs or []),
        })
        tid = (origin or {}).get("thread_id")
        if tid:
            agg.aggregate(repo, tid, cfg)
            _log("已把发出的信写回本地，并重算线程 %s" % tid)
        return True
    except Exception as e:
        _log("写回已发送副本失败（不影响发送，等下次同步）：%s" % e)
        return False


def _extract_message_id(raw: bytes) -> str:
    try:
        from email import message_from_bytes
        msg = message_from_bytes(raw)
        return (msg.get("Message-ID") or "").strip()
    except Exception:
        return ""


def _valid_recipients(to) -> list:
    """把收件人字符串切成地址列表，并做最基本的格式校验。

    手写字面的信最容易出的错就是地址写错 —— 这一步拦住它，
    而不是等 SMTP 报一个看不懂的错。
    """
    if isinstance(to, (list, tuple)):
        parts = [str(a) for a in to]
    else:
        parts = re.split(r"[;,]", to or "")
    addrs = [a.strip() for a in parts if a and a.strip()]
    bad = [a for a in addrs if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", a)]
    if bad:
        raise ValueError("收件人格式不对：%s" % "、".join(bad))
    return addrs


# ==========================================================================
# 路由
# ==========================================================================
class Router:
    def __init__(self):
        self.gets = []
        self.posts = []

    def get(self, pattern):
        def deco(fn):
            self.gets.append((re.compile("^" + pattern + "$"), fn))
            return fn
        return deco

    def post(self, pattern):
        def deco(fn):
            self.posts.append((re.compile("^" + pattern + "$"), fn))
            return fn
        return deco

    def match(self, method: str, path: str):
        table = self.gets if method == "GET" else self.posts
        for rx, fn in table:
            m = rx.match(path)
            if m:
                return fn, m.groupdict()
        return None, None


ROUTER = Router()
G: App = None      # 由 run() 注入


def ok(data=None, **kw):
    out = {"ok": True}
    if data is not None:
        out["data"] = data
    out.update(kw)
    return out


def err(message, code=400, **kw):
    out = {"ok": False, "error": message, "code": code}
    out.update(kw)
    return out


# -------------------------- meta / health --------------------------
@ROUTER.get(r"/api/health")
def h_health(req):
    # 统一契约：所有 V2 端点都必须带 ok 字段，否则前端 api() 的成功判断会静默失败
    return ok(G.health())


@ROUTER.get(r"/api/metrics")
def h_metrics(req):
    return ok(G.metrics())


@ROUTER.get(r"/api/state")
def h_state(req):
    return ok(G.repo.counts_summary(), buckets=bucketmod.counts_only(G.repo, G.cfg),
              jobs=G.repo.job_counts(), at=util.now_iso())


@ROUTER.get(r"/api/contract")
def h_contract(req):
    return ok(wc.contract_doc())


@ROUTER.get(r"/api/state-machine")
def h_sm(req):
    from .workflow import state_machine as sm
    return ok({"transitions": sm.describe(), "mermaid": sm.mermaid(),
               "send_allowed_states": sorted(sm.SEND_ALLOWED_STATES)})


@ROUTER.get(r"/api/constants")
def h_constants(req):
    return ok({
        "classifications": CLASSIFICATIONS,
        "workflow_states": WF_LABEL,
        "draft_modes": DRAFT_MODES,
        # 模式与改写按钮的**中文文案**。UI 之前直接渲染英文标识符
        # （quick / normal / shorter / strip_boilerplate），那是给程序读的。
        "draft_mode_labels": DRAFT_MODE_LABEL,
        "reply_controls": REPLY_CONTROLS,
        "reply_control_labels": REPLY_CONTROL_LABEL,
        "reply_control_group": REPLY_CONTROL_GROUP,
        "buckets": BUCKET_LABEL,
        "bucket_hints": BUCKET_HINT,
        "work_buckets": list(BUCKETS),
        # 批处理可用的动作（UI 据此渲染按钮，不再在前端硬编码一份）
        "batch_actions": list(actionsmod.BATCH_ACTIONS),
        # 侧边栏标签树：名称与圆点色走常量，UI 不硬编码（改一处就够）
        "class_tree": [{"key": c, "label": CLASS_TREE_LABEL[c], "dot": CLASS_TREE_DOT[c]}
                       for c in CLASSIFICATIONS],
        "search_help": searchmod.SEARCH_HELP,
        # 快速草稿模板清单：草稿面板里的「换模板」按钮要用它渲染
        "draft_templates": draft_templates.kinds(),
    })


# -------------------------- buckets / brief --------------------------
@ROUTER.get(r"/api/buckets")
def h_buckets(req):
    limit = int(req.query.get("limit", [100])[0])
    return ok(G.buckets(limit=limit))


@ROUTER.get(r"/api/brief")
def h_brief(req):
    rebuild = req.query.get("rebuild", ["0"])[0] in ("1", "true", "yes")
    snap = briefmod.build(G.repo, G.cfg) if rebuild else briefmod.latest(G.repo, G.cfg)
    if rebuild:
        briefmod.store_snapshot(G.repo, snap)
    return ok(snap)


@ROUTER.post(r"/api/brief/refresh")
def h_brief_refresh(req):
    snap = briefmod.build(G.repo, G.cfg)
    briefmod.store_snapshot(G.repo, snap)
    return ok(snap)


# -------------------------- mail / folders --------------------------
@ROUTER.get(r"/api/folders")
def h_folders(req):
    """侧边栏的文件夹树数据。

    这里**不再只回 config.sync_folders**，而是「库里有的 ∪ 配置里要同步的」：
    只看配置的话，一旦某个文件夹还没同步过，它就会从侧边栏消失，
    用户会以为邮件没了 —— 空文件夹也要如实列出来（计数 0）。

    顺序沿用 V1：先系统文件夹（INBOX/Drafts/Sent/Junk/Trash），再自建文件夹。
    Foxmail 历史那个伪文件夹不出现在树里，折算成 index_count（「N 封可检索」）。
    """
    counts = {c["folder"]: c for c in G.repo.folder_counts()}
    configured = list(G.cfg.get("sync_folders") or [])
    in_db = [f for f in counts.keys() if f != FOLDER_FOXMAIL]

    system = [f for f in SYSTEM_FOLDERS]
    custom = []
    for f in configured + sorted(in_db):
        if f in SYSTEM_FOLDERS or f == FOLDER_FOXMAIL or f in custom:
            continue
        custom.append(f)

    def one(path, is_custom):
        c = counts.get(path) or {}
        return {"path": path, "label": path,
                "total": int(c.get("total") or 0),
                "unread": int(c.get("unread") or 0),
                "custom": bool(is_custom)}

    folders = [one(f, False) for f in system] + [one(f, True) for f in custom]

    index_count = int((counts.get(FOLDER_FOXMAIL) or {}).get("total") or 0)

    live = None
    if req.query.get("live", ["0"])[0] in ("1", "true"):
        try:
            c = imap_client.ImapClient(G.cfg, log=G.log)
            c.connect()
            live = c.list_folders()
            c.close()
        except Exception as e:
            live = {"error": str(e)}

    return ok({"folders": folders,
               "system": list(SYSTEM_FOLDERS),
               "index_count": index_count,
               "total_messages": G.repo.count_messages(),
               "live": live})


@ROUTER.get(r"/api/messages")
def h_messages(req):
    """按文件夹 / 分类浏览邮件（侧边栏点文件夹、点标签都走这里）。

    与 /api/buckets 的分工：buckets 是「工作视图」（线程级、带工作集语义），
    这里是「档案视图」（邮件级、所见即全量）。两者刻意不共用查询 ——
    档案视图要的是准确与完整，不该被工作集时间窗过滤掉。
    """
    q = req.query
    folder = q.get("folder", [None])[0]
    source = q.get("source", [None])[0]
    if folder == FOLDER_FOXMAIL:
        # 伪文件夹：库里没有这个 folder 值，历史邮件靠 source='foxmail' 标识。
        # 这里原来**只把 folder 置空**、没有设 source —— 结果这个伪文件夹的查询
        # 等价于「不带任何过滤」，返回的是全库最新的 40 封（全是 IMAP），
        # 跟「Foxmail 历史」没有任何关系。任何按它过滤的调用方（自检脚本、
        # 以后可能加的「点底部索引数进历史列表」）都会拿到错的数据。
        # 现在真的按 source 过滤。
        folder = None
        source = "foxmail"
    classification = q.get("classification", [None])[0]
    limit = min(int(q.get("limit", [60])[0]), 500)
    offset = int(q.get("offset", [0])[0])
    unread = q.get("unread", ["0"])[0] in ("1", "true")
    if q.get("index", ["0"])[0] in ("1", "true"):
        source = "foxmail"
    rows = G.repo.list_messages(folder=folder, limit=limit, offset=offset,
                                unread_only=unread, classification=classification,
                                source=source)
    total = G.repo.count_messages(folder=folder, classification=classification, source=source)
    return ok({"messages": [_msg_public(r) for r in rows], "count": len(rows),
               "total": total, "folder": folder, "classification": classification,
               "limit": limit, "offset": offset})


# Message-ID 里确实可能出现 `/`（部分邮件网关会生成），所以用 `.+` 而不是 `[^/]+`。
# 代价是必须把更具体的 `/context` 子路由**注册在前面**，否则会被上面那条吞掉。
@ROUTER.get(r"/api/messages/(?P<message_id>.+)/context")
def h_message_context(req):
    mid = req.path_params["message_id"]
    m = G.repo.get_message(mid)
    if not m:
        return err("邮件不存在", 404)
    pkg = cbm.MailContextBuilder(G.repo, G.cfg).build(message_id=mid)
    return ok(pkg)


@ROUTER.get(r"/api/messages/(?P<message_id>.+)")
def h_message(req):
    mid = req.path_params["message_id"]
    m = G.repo.get_message(mid)
    if not m:
        return err("邮件不存在", 404)
    out = _msg_public(m, full=True)
    out["thread"] = G.repo.get_thread(m.get("thread_id")) if m.get("thread_id") else None
    out["candidates"] = G.repo.get_candidate_by_message(mid) or None
    out["jobs"] = [dq.to_public(j) for j in G.repo.list_jobs(message_id=mid, limit=5)]
    out["attachments"] = [_att_public(G.cfg, a, mid)
                          for a in G.repo.attachments_for_message(mid)]
    out["broadcast"] = bool(m.get("is_broadcast"))
    out["contact"] = G.repo.get_contact(m.get("from_addr") or "")
    return ok(out)


@ROUTER.get(r"/api/threads")
def h_threads(req):
    limit = min(int(req.query.get("limit", [80])[0]), 500)
    wf = req.query.get("status", [None])[0]
    rows = G.repo.list_threads(limit=limit, workflow_state=wf)
    return ok({"threads": rows, "count": len(rows)})


@ROUTER.get(r"/api/threads/(?P<thread_id>[^/]+)")
def h_thread(req):
    tid = req.path_params["thread_id"]
    view = agg.thread_view(G.repo, tid, G.cfg)
    if not view:
        return err("线程不存在", 404)
    view["jobs"] = [dq.to_public(j) for j in G.repo.list_jobs(thread_id=tid, limit=10)]
    return ok(view)


@ROUTER.get(r"/api/search")
def h_search(req):
    q = req.query.get("q", [""])[0]
    limit = min(int(req.query.get("limit", [200])[0]), 500)
    res = searchmod.run(G.repo, q, limit=limit)
    res["explain"] = searchmod.describe_query(q)
    res["help"] = searchmod.SEARCH_HELP
    # 按 thread 聚合
    groups = {}
    for m in res["messages"]:
        groups.setdefault(m.get("thread_id") or m["message_id"], []).append(_msg_public(m))
    res["threads"] = [{"thread_id": k, "messages": v, "count": len(v)}
                      for k, v in groups.items()]
    return ok(res)


# -------------------------- candidates --------------------------
@ROUTER.get(r"/api/candidates")
def h_candidates(req):
    status = req.query.get("status", ["new"])[0]
    rows = G.repo.list_candidates(status=status,
                                  limit=min(int(req.query.get("limit", [100])[0]), 500))
    out = []
    for c in rows:
        item = dict(c)
        m = G.repo.get_message(c["message_id"])
        if m:
            item["snippet"] = m.get("snippet")
            item["date_iso"] = m.get("date_iso")
            item["classification"] = m.get("classification")
            item["unread"] = bool(m.get("unread"))
        out.append(item)
    return ok({"candidates": out, "count": len(out)})


@ROUTER.post(r"/api/candidates/(?P<candidate_id>[^/]+)/dismiss")
def h_cand_dismiss(req):
    cid = req.path_params["candidate_id"]
    cand = G.repo.get_candidate(cid)
    if not cand:
        return err("候选不存在", 404)
    G.repo.set_candidate_status(cid, "dismissed")
    return ok({"candidate_id": cid, "status": "dismissed"})


@ROUTER.post(r"/api/candidates/scan")
def h_cand_scan(req):
    body = req.json or {}
    res = CandidateDetector(G.repo, G.cfg, log=G.log).scan(
        limit=int(body.get("limit", 200)), trigger_source=body.get("trigger", "manual"))
    return ok(res)


# -------------------------- draft jobs / worker contract --------------------------
@ROUTER.get(r"/api/draft-jobs")
def h_jobs(req):
    status = req.query.get("status", [None])[0]
    statuses = None
    if status:
        statuses = [s.strip() for s in status.split(",") if s.strip()]
    rows = G.repo.list_jobs(statuses=statuses,
                            limit=min(int(req.query.get("limit", [60])[0]), 300))
    return ok({"jobs": [dq.to_public(j) for j in rows], "count": len(rows),
               "counts": G.repo.job_counts()})


@ROUTER.get(r"/api/draft-jobs/next")
def h_jobs_next(req):
    worker = req.query.get("worker", ["workbuddy"])[0]
    res = wc.claim(G.repo, G.cfg, worker=worker)
    # 契约形状保持扁平（job 在顶层），但同样带 ok 方便调用方统一判断
    res["ok"] = True
    if res.get("job"):
        G.bus.publish({"type": "job", "job_id": res["job"]["job_id"], "status": "generating"})
    return res


@ROUTER.get(r"/api/draft-jobs/(?P<job_id>[^/]+)")
def h_job(req):
    j = G.repo.get_job(req.path_params["job_id"])
    if not j:
        return err("任务不存在", 404)
    out = dq.to_public(j, include_plan=True)
    out["events"] = G.repo.job_events(j["job_id"])
    return ok(out)


@ROUTER.get(r"/api/draft-jobs/(?P<job_id>[^/]+)/context")
def h_job_context(req):
    rebuild = req.query.get("rebuild", ["0"])[0] in ("1", "true")
    worker = req.query.get("worker", [None])[0]
    try:
        return ok(wc.get_context(G.repo, G.cfg, req.path_params["job_id"],
                                 worker=worker, rebuild=rebuild))
    except wc.ContractError as e:
        return err(str(e), 409)
    except Exception as e:
        return err(str(e), 500)


@ROUTER.get(r"/api/draft-jobs/(?P<job_id>[^/]+)/events")
def h_job_events(req):
    """任务的事件审计轨迹。

    同其它 `/draft-jobs/{id}/*` 端点一样，**任务不存在要报 404** ——
    这里原来无条件返回 `{"events": []}`，于是「拼错 job_id」和
    「这个任务确实没有事件」在界面上长得一模一样，排查时会被带偏。
    """
    jid = req.path_params["job_id"]
    if not G.repo.get_job(jid):
        return err("任务不存在：%s" % jid, 404)
    return ok({"events": G.repo.job_events(jid)})


@ROUTER.post(r"/api/draft-jobs/(?P<job_id>[^/]+)/plan")
def h_job_plan(req):
    worker = (req.json or {}).get("worker")
    plan = (req.json or {}).get("plan") or req.json or {}
    try:
        res = wc.submit_plan(G.repo, G.cfg, req.path_params["job_id"], plan, worker=worker)
        G.bus.publish({"type": "job", "job_id": req.path_params["job_id"],
                       "status": res["status"]})
        return ok(res)
    except wc.ContractError as e:
        return err(str(e), 409)


@ROUTER.post(r"/api/draft-jobs/(?P<job_id>[^/]+)/draft")
def h_job_draft(req):
    body = req.json or {}
    text = body.get("draft_text") or body.get("text") or ""
    try:
        res = wc.submit_draft(G.repo, G.cfg, req.path_params["job_id"], text,
                              draft_mode=body.get("draft_mode"),
                              variant=body.get("variant"),
                              worker=body.get("worker"))
        G.bus.publish({"type": "job", "job_id": req.path_params["job_id"], "status": "ready"})
        return ok(res)
    except wc.ContractError as e:
        return err(str(e), 409)


@ROUTER.post(r"/api/draft-jobs/(?P<job_id>[^/]+)/fail")
def h_job_fail(req):
    body = req.json or {}
    try:
        res = wc.submit_fail(G.repo, G.cfg, req.path_params["job_id"],
                             body.get("error") or "unspecified",
                             retryable=bool(body.get("retryable", True)),
                             worker=body.get("worker"))
        G.bus.publish({"type": "job", "job_id": req.path_params["job_id"],
                       "status": res["status"]})
        return ok(res)
    except wc.ContractError as e:
        return err(str(e), 409)


@ROUTER.post(r"/api/draft-jobs/(?P<job_id>[^/]+)/heartbeat")
def h_job_heartbeat(req):
    body = req.json or {}
    try:
        return ok(wc.heartbeat(G.repo, G.cfg, req.path_params["job_id"], worker=body.get("worker")))
    except wc.ContractError as e:
        return err(str(e), 409)


@ROUTER.post(r"/api/draft-jobs")
def h_job_create(req):
    body = req.json or {}
    mid = body.get("message_id")
    if not mid:
        return err("需要 message_id")
    job = dq.ensure_job(G.repo, G.cfg, mid, trigger_source=body.get("trigger", "manual"),
                        draft_mode=body.get("draft_mode"), instruction=body.get("instruction", ""))
    cand = G.repo.get_candidate_by_message(mid)
    if cand:
        G.repo.set_candidate_status(cand["candidate_id"], "converted")
    G.bus.publish({"type": "job", "job_id": job["job_id"], "status": job["status"]})
    return ok(dq.to_public(job))


@ROUTER.post(r"/api/drafts/fast")
def h_draft_fast(req):
    """本地模板「快速草稿」—— 毫秒级返回，不调用模型。

    存在理由：日常邮件里大量回复属于「收到确认 / 婉拒 / 索取材料 / 稍后答复」，
    让它们去排 AI 队列，就要等「有人认领 + 生成」（实测生成 22 秒，
    认领之前的等待更久且不可控）。模板直接出稿，把这段等待整个去掉。

    带 `kind` 可指定模板；不带则从主题/正文推断（推断不出用最安全的
    「稍后答复」—— 只确认收到、不做任何承诺）。
    """
    body = req.json or {}
    mid = body.get("message_id")
    if not mid:
        return err("需要 message_id")
    pkg = draft_templates.build_fast_draft(G.repo, G.cfg, mid, kind=body.get("kind"))
    if not pkg.get("ok"):
        return err(pkg.get("error") or "模板草稿生成失败", 404)
    try:
        job = dq.set_fast_draft(G.repo, G.cfg, mid, pkg["draft_text"],
                                draft_mode=body.get("draft_mode"))
    except dq.QueueError as e:
        return err(str(e), 409)
    G.bus.publish({"type": "job", "job_id": job["job_id"], "status": job["status"]})
    return ok({
        "job": dq.to_public(job),
        "template": {k: pkg.get(k) for k in
                     ("kind", "label", "hint", "auto", "missing_slots", "note")},
        "available_kinds": pkg.get("available_kinds"),
    })


@ROUTER.get(r"/api/draft-templates")
def h_draft_templates(req):
    """可用的快速草稿模板清单（给 UI 的「换一个模板」用）。"""
    return ok({"kinds": draft_templates.kinds(),
               "default": draft_templates.DEFAULT_KIND})


# -------------------------- 草稿的人工侧操作 --------------------------
@ROUTER.post(r"/api/drafts/(?P<job_id>[^/]+)/edit")
def h_draft_edit(req):
    body = req.json or {}
    try:
        j = dq.save_edit(G.repo, req.path_params["job_id"], body.get("draft_text") or "")
        return ok(dq.to_public(j))
    except dq.QueueError as e:
        return err(str(e), 409)


@ROUTER.post(r"/api/drafts/(?P<job_id>[^/]+)/mode")
def h_draft_mode(req):
    body = req.json or {}
    mode = body.get("draft_mode")
    if mode not in DRAFT_MODES:
        return err("draft_mode 非法，可选 %s" % list(DRAFT_MODES))
    j = G.repo.get_job(req.path_params["job_id"])
    if not j:
        return err("任务不存在", 404)
    G.repo.update_job(j["job_id"], {"draft_mode": mode})
    return ok({"job_id": j["job_id"], "draft_mode": mode,
               "note": "模式已切换；点「重新生成」会按新模式重写"})


@ROUTER.post(r"/api/drafts/(?P<job_id>[^/]+)/input")
def h_draft_input(req):
    body = req.json or {}
    try:
        j = dq.provide_input(G.repo, req.path_params["job_id"],
                             body.get("user_input") or body.get("answers") or {})
        G.bus.publish({"type": "job", "job_id": j["job_id"], "status": j["status"]})
        return ok({"job": dq.to_public(j),
                   "next": "已重新入队，WorkBuddy 认领后会带上你补的信息"})
    except dq.QueueError as e:
        return err(str(e), 409)


@ROUTER.post(r"/api/drafts/(?P<job_id>[^/]+)/revise")
def h_draft_revise(req):
    body = req.json or {}
    instruction = body.get("instruction") or ""
    control = body.get("control")
    if control:
        instruction = REPLY_CONTROLS.get(control, "")
        if not instruction:
            return err("未知的快捷改写：%s（可选 %s）" % (control, list(REPLY_CONTROLS)))
    if not instruction:
        return err("需要 instruction，或使用 control 指定快捷改写")
    try:
        j = dq.revise(G.repo, G.cfg, req.path_params["job_id"], instruction,
                      draft_mode=body.get("draft_mode"))
        G.bus.publish({"type": "job", "job_id": j["job_id"], "status": j["status"]})
        return ok({"job": dq.to_public(j), "instruction": instruction,
                   "note": "新版任务已入队；旧版本标记为被取代（可查审计）"})
    except dq.QueueError as e:
        return err(str(e), 409)


@ROUTER.post(r"/api/drafts/(?P<job_id>[^/]+)/approve")
def h_draft_approve(req):
    body = req.json or {}
    try:
        res = dq.approve(G.repo, G.cfg, req.path_params["job_id"],
                         actor=body.get("actor") or "user")
        G.bus.publish({"type": "job", "job_id": req.path_params["job_id"], "status": "approved"})
        return ok({"job": dq.to_public(res["job"]), "approve_token": res["approve_token"],
                   "expires_at": res["expires_at"], "confirm_hint": res["confirm_hint"]})
    except dq.QueueError as e:
        return err(str(e), 409)


@ROUTER.post(r"/api/drafts/(?P<job_id>[^/]+)/revoke")
def h_draft_revoke(req):
    try:
        j = dq.revoke_approval(G.repo, req.path_params["job_id"])
        return ok(dq.to_public(j))
    except dq.QueueError as e:
        return err(str(e), 409)


@ROUTER.post(r"/api/drafts/(?P<job_id>[^/]+)/send")
def h_draft_send(req):
    body = req.json or {}
    res = G.send_draft(req.path_params["job_id"], body.get("approve_token") or "",
                       confirm=body.get("confirm") or "",
                       followup_days=body.get("followup_days"))
    if not res.get("ok"):
        return err(res.get("error"), res.get("http", 400), policy=res.get("policy"))
    return ok(res)


@ROUTER.post(r"/api/drafts/(?P<job_id>[^/]+)/dismiss")
def h_draft_dismiss(req):
    try:
        j = dq.dismiss(G.repo, req.path_params["job_id"],
                       note=(req.json or {}).get("note") or "用户在界面放弃")
        G.bus.publish({"type": "job", "job_id": j["job_id"], "status": j["status"]})
        return ok(dq.to_public(j))
    except dq.QueueError as e:
        return err(str(e), 409)


# -------------------------- 写邮件（人工起草）--------------------------
@ROUTER.post(r"/api/compose/draft")
def h_compose_draft(req):
    """把人工写的信存进草稿箱（绝不发送）。"""
    body = req.json or {}
    res = G.compose_draft(body.get("to"), body.get("subject") or "",
                          body.get("body") or "", cc=body.get("cc"))
    if not res.get("ok"):
        return err(res.get("error") or "保存草稿失败", int(res.get("http") or 400))
    return ok(res)


@ROUTER.post(r"/api/compose/send")
def h_compose_send(req):
    """发送人工写的新邮件。

    ⚠️ 这是 V2 里唯一能发出「新邮件」（非草稿任务）的入口，且必须带
    confirm="SEND"。WorkBuddy 不得调用 —— 发送只能由人点、由人确认。
    """
    body = req.json or {}
    res = G.compose_send(body.get("to"), body.get("subject") or "",
                         body.get("body") or "", cc=body.get("cc"),
                         confirm=body.get("confirm") or "")
    if not res.get("ok"):
        out = {"ok": False, "error": res.get("error") or "发送失败"}
        if res.get("policy"):
            # 把策略语句原样带出去：前端/日志都能看到「为什么被拒」，
            # 而不是只看到一句泛泛的「缺少确认」。
            out["policy"] = res["policy"]
        return out, int(res.get("http") or 400)
    return ok(res)


# -------------------------- 动作 / 批处理 / undo --------------------------
@ROUTER.post(r"/api/actions")
def h_actions(req):
    body = req.json or {}
    action = body.get("action")
    if not action:
        return err("需要 action")
    try:
        res = actionsmod.apply(G.repo, G.cfg, action,
                               message_id=body.get("message_id"),
                               thread_id=body.get("thread_id"),
                               when=body.get("when"), note=body.get("note"),
                               draft_mode=body.get("draft_mode"),
                               mark_answered=body.get("mark_answered"),
                               log=G.log)
        G.bus.publish({"type": "action", "action": action,
                       "message_id": body.get("message_id"), "ok": res.get("ok")})
        return res if res.get("ok") else err(res.get("error") or "动作失败", 502, result=res)
    except actionsmod.ActionError as e:
        return err(str(e), 400)


@ROUTER.post(r"/api/actions/batch")
def h_actions_batch(req):
    body = req.json or {}
    action = body.get("action")
    ids = body.get("message_ids") or []
    if not action:
        return err("需要 action")
    try:
        res = actionsmod.batch(G.repo, G.cfg, action, ids,
                               when=body.get("when"), log=G.log)
        G.bus.publish({"type": "batch", "action": action, "succeeded": res["succeeded"]})
        return ok(res)
    except actionsmod.ActionError as e:
        return err(str(e), 400)


@ROUTER.post(r"/api/undo")
def h_undo(req):
    body = req.json or {}
    return ok(actionsmod.undo(G.repo, G.cfg, undo_id=body.get("undo_id"), log=G.log))


@ROUTER.get(r"/api/undo/last")
def h_undo_last(req):
    row = G.repo.last_undo()
    return ok(row or {})


@ROUTER.post(r"/api/classify")
def h_classify(req):
    body = req.json or {}
    mids = body.get("message_ids") or ([body["message_id"]] if body.get("message_id") else [])
    cls = body.get("classification")
    if not mids:
        return err("需要 message_ids")
    if cls:
        return ok(actionsmod.batch_classify(G.repo, G.cfg, mids, cls))
    # 不带分类 -> 用规则引擎重算
    from .intelligence import rule_engine
    rules = rule_engine.load_rules(G.cfg)
    n = 0
    for mid in mids:
        m = G.repo.get_message(mid)
        if not m:
            continue
        G.repo.set_classification(mid, rule_engine.classify(m, rules))
        if m.get("thread_id"):
            agg.aggregate(G.repo, m["thread_id"], G.cfg)
        n += 1
    return ok({"updated": n, "mode": "rule_engine"})


# -------------------------- snooze / followup --------------------------
@ROUTER.get(r"/api/snoozes")
def h_snoozes(req):
    return ok({"snoozes": G.repo.active_snoozes(limit=200),
               "presets": snzm.PRESETS})


@ROUTER.post(r"/api/snooze")
def h_snooze(req):
    body = req.json or {}
    try:
        s = snzm.SnoozeManager(G.repo, G.cfg, log=G.log).snooze(
            message_id=body.get("message_id"), thread_id=body.get("thread_id"),
            when=body.get("when") or "tomorrow", note=body.get("note") or "")
        return ok({"snooze": s,
                   "note": "不使用 WorkBuddy automation；由本地 scheduler 到期恢复"})
    except Exception as e:
        return err(str(e), 400)


@ROUTER.get(r"/api/followups")
def h_followups(req):
    fm = fum.FollowUpManager(G.repo, G.cfg, log=G.log)
    return ok({"scheduled": fm.list_open(), "due": fm.list_due(),
               "snapshot": fm.snapshot()})


@ROUTER.post(r"/api/followups")
def h_followup_create(req):
    body = req.json or {}
    try:
        fu = fum.FollowUpManager(G.repo, G.cfg, log=G.log).schedule(
            thread_id=body.get("thread_id"), message_id=body.get("message_id"),
            days=body.get("days"), sent_message_id=body.get("sent_message_id") or "",
            note=body.get("note") or "")
        return ok({"followup": fu, "note": "本地 scheduler 统一管理，不为每封邮件建 automation"})
    except Exception as e:
        return err(str(e), 400)


@ROUTER.post(r"/api/followups/(?P<followup_id>[^/]+)/cancel")
def h_followup_cancel(req):
    fid = req.path_params["followup_id"]
    fm = fum.FollowUpManager(G.repo, G.cfg, log=G.log)
    return ok({"followup_id": fid, "cancelled": fm.cancel(fid)})


# -------------------------- sync / maintenance --------------------------
@ROUTER.post(r"/api/sync")
def h_sync(req):
    body = req.json or {}
    folders = body.get("folders")
    res = G.sync.sync_all(folders=folders, limit=body.get("limit"), reason="manual")
    G.bus.publish({"type": "sync_done", "result": res})
    return ok(res)


@ROUTER.post(r"/api/maintenance/recovery")
def h_recovery(req):
    body = req.json or {}
    dry = bool(body.get("dry_run"))
    res = rcv.run_recovery(G.repo, G.cfg, log=G.log, dry_run=dry)
    if res["actions"]:
        G.bus.publish({"type": "recovery", "result": res})
    else:
        return ok({"silent": True, "actions": 0,
                   "note": "队列无异常，静默退出（不打扰用户）"})
    return ok(res)


@ROUTER.post(r"/api/maintenance/repair-threads")
def h_repair_threads(req):
    """线程层自愈：回收「只由 Foxmail 历史构成」的线程（幂等）。"""
    from .thread import aggregator as agg
    res = agg.repair(G.repo, G.cfg, log=G.log)
    if not res.get("silent"):
        G.bus.publish({"type": "threads_repaired", "result": res})
    return ok(res)


@ROUTER.post(r"/api/maintenance/reclassify")
def h_reclassify(req):
    """用当前 classify_rules.json 重算全库分类与群发标记（幂等）。

    改完规则后必须跑一次 —— 分类是存在库里的，不重算界面不会变。
    """
    from .intelligence import rule_engine
    body = req.json or {}
    res = rule_engine.reclassify_all(G.repo, G.cfg, log=G.log,
                                    limit=int(body.get("limit") or 0))
    if res.get("changed"):
        G.bus.publish({"type": "reclassify", "result": res})
    return ok(res)


@ROUTER.post(r"/api/maintenance/tick")
def h_tick(req):
    return ok(G.scheduler.tick())


@ROUTER.get(r"/api/maintenance/tick")
def h_tick_get(req):
    return ok(G.scheduler.status())


@ROUTER.post(r"/api/import/foxmail")
def h_import_foxmail(req):
    body = req.json or {}
    res = foxmail_index.import_history(G.repo, G.cfg, limit=int(body.get("limit", 0)), log=G.log)
    return ok(res)


def _aid_from(req) -> str:
    """取附件 id。优先查 query，其次 JSON body。

    **为什么不把它放在路径段里**：attachment_id 是 `<sha256>:<原始文件名>`，
    文件名可能含中文、空格，甚至 `/`。放进路径段后，percent-encoding 会先被
    整段解码，`%2F` 变成 `/` 就把一个路径段切成了两段，路由直接匹配不上 ——
    表现为「有些附件点了 404」。query 值与 JSON body 都以整体为单位解码，
    没有这个问题。
    """
    v = (req.query.get("aid") or req.query.get("attachment_id") or [None])[0]
    if v:
        return v
    body = req.json or {}
    return body.get("attachment_id") or body.get("aid") or ""


@ROUTER.get(r"/api/attachments/download")
def h_attachment_download(req):
    """下载附件（二进制直出，浏览器原生下载）。`?inline=1` 为内联预览。"""
    aid = _aid_from(req)
    if not aid:
        return err("需要 aid（附件 id）")
    inline = (req.query.get("inline") or ["0"])[0] in ("1", "true", "yes")
    try:
        info = ats.download(G.cfg, G.repo, aid, log=G.log)
    except ats.AttachmentError as e:
        return err(str(e), 404)
    try:
        with open(info["path"], "rb") as f:
            data = f.read()
    except OSError as e:
        return err("读取本地文件失败：%s" % e, 500)
    att = G.repo.get_attachment(aid) or {}
    G.repo.inc_metric("attachments_downloaded")
    return Raw(data, att.get("content_type") or "application/octet-stream",
               info["filename"], inline=inline)


@ROUTER.post(r"/api/attachments/open")
def h_attachment_open(req):
    """下载后用**系统默认程序**打开。

    纯本机动作（等价于在资源管理器里双击），不发信、不改邮箱、不访问网络。
    """
    aid = _aid_from(req)
    if not aid:
        return err("需要 attachment_id")
    try:
        info = ats.download(G.cfg, G.repo, aid, log=G.log)
        ats.open_with_default(info["path"])
    except ats.AttachmentError as e:
        return err(str(e), 400)
    return ok({"path": info["path"], "filename": info["filename"],
               "size_bytes": info["size_bytes"], "cached": info["cached"],
               "message": "已用默认程序打开" + ("（本地缓存）" if info["cached"] else "")})


@ROUTER.post(r"/api/attachments/reveal")
def h_attachment_reveal(req):
    """只下载 + 在文件管理器里定位（不想直接打开时用）。"""
    aid = _aid_from(req)
    if not aid:
        return err("需要 attachment_id")
    try:
        info = ats.download(G.cfg, G.repo, aid, log=G.log)
        ats.reveal(info["path"])
    except ats.AttachmentError as e:
        return err(str(e), 400)
    return ok({"path": info["path"], "filename": info["filename"],
               "size_bytes": info["size_bytes"], "cached": info["cached"],
               "message": "已下载并在文件管理器中定位"})


# 注意：这条必须注册在上面几条**之后**。它的 message_id 用 `.+`
# 是为了容纳含 `/` 的 Message-ID，代价就是会吞掉任何 `/api/attachments/xxx`，
# 所以更具体的路由一定得先匹配。
@ROUTER.get(r"/api/attachments/(?P<message_id>.+)")
def h_attachments(req):
    """列出某封邮件的附件（带「是否已下载到本地」与可直接用的 URL）。"""
    mid = req.path_params["message_id"]
    rows = G.repo.attachments_for_message(mid)
    return ok({"attachments": [_att_public(G.cfg, a, mid) for a in rows]})


@ROUTER.get(r"/api/contacts")
def h_contacts(req):
    return ok({"contacts": G.repo.list_contacts(limit=500)})


@ROUTER.post(r"/api/contacts/(?P<email>[^/]+)")
def h_contact_set(req):
    email = req.path_params["email"]
    G.repo.set_contact(email, **(req.json or {}))
    return ok(G.repo.get_contact(email))


@ROUTER.get(r"/api/draft-memory")
def h_draft_memory(req):
    return ok({"enabled": bool(G.cfg.get("draft_memory_enabled", True)),
               "entries": G.repo.draft_memory()})


@ROUTER.post(r"/api/draft-memory/clear")
def h_draft_memory_clear(req):
    G.repo.clear_draft_memory()
    return ok({"cleared": True})


@ROUTER.get(r"/api/events/recent")
def h_events_recent(req):
    return ok({"events": G.bus.recent[-50:]})


# ==========================================================================
# HTTP Handler
# ==========================================================================
class Raw:
    """路由返回它 = 「不要 JSON 包装，直接把这些字节发出去」。

    需要它是因为下载附件是二进制：走 JSON 就得 base64，既浪费内存又让
    浏览器无法用原生的下载行为（文件名、断点、Content-Disposition 全丢了）。
    """

    __slots__ = ("data", "ctype", "filename", "inline")

    def __init__(self, data: bytes, ctype: str = "application/octet-stream",
                 filename: str = "", inline: bool = False):
        self.data = data or b""
        self.ctype = ctype or "application/octet-stream"
        self.filename = filename or ""
        self.inline = inline

    def headers(self) -> dict:
        from urllib.parse import quote
        disp = "inline" if self.inline else "attachment"
        if self.filename:
            # RFC 5987：filename* 用 UTF-8，中文附件名才不会乱码
            disp += "; filename*=UTF-8''%s" % quote(self.filename, safe="")
        return {"Content-Type": self.ctype, "Content-Disposition": disp,
                "X-Content-Type-Options": "nosniff"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "MailWorkbench/2.0"

    def log_message(self, *args):
        pass

    # ---------------- 基础设施 ----------------
    def _json_body(self):
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            n = 0
        if n <= 0:
            return {}
        if n > 8 * 1024 * 1024:
            return {}
        raw = self.rfile.read(n)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _send(self, obj, code=200, ctype="application/json; charset=utf-8"):
        data = obj if isinstance(obj, (bytes, bytearray)) else json.dumps(
            obj, ensure_ascii=False).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _send_file(self, path, ctype="text/html; charset=utf-8"):
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError as e:
            return self._send({"ok": False, "error": "读取失败：%s" % e}, 500)
        try:
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _dispatch(self, method: str):
        from urllib.parse import urlparse, parse_qs
        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/") or "/"
        qs = parse_qs(parsed.query)

        if method == "GET" and path in ("/", "/index.html"):
            ui = os.path.join(G.cfg["ui_dir"], "index.html")
            if os.path.exists(ui):
                return self._send_file(ui)
            return self._send(UI_PLACEHOLDER, 200)
        if method == "GET" and path == "/classic":
            # V1 的经典视图。
            # 这里原来找的是 `inbox.html` —— 那个文件从来不存在（V1 的页面叫
            # `web/index.html`），于是文档里承诺的 `/classic` 一直是 404：
            # 「直接敲 URL 就能到」到不了。现在按真实文件名找，并保留
            # `inbox.html` 作为兼容回退。
            root = G.cfg["project_root"]
            for rel in ("web/index.html", "inbox.html"):
                legacy = os.path.join(root, rel)
                if os.path.exists(legacy):
                    return self._send_file(legacy)
            return self._send({"ok": False,
                               "error": "找不到 V1 经典视图（应为 web/index.html）"}, 404)
        if method == "GET" and path == "/favicon.ico":
            return self._send(b"", 204)
        if method == "GET" and path == "/api/events":
            return self._sse()

        fn, params = ROUTER.match(method, path)
        if not fn:
            return self._send({"ok": False, "error": "not found: %s %s" % (method, path)}, 404)

        req = _Request(self, method, path, qs, params)
        try:
            res = fn(req)
        except Exception as e:
            if G and G.log:
                G.log("API 异常 %s %s: %s\n%s" % (method, path, e, traceback.format_exc()))
            return self._send({"ok": False, "error": str(e),
                               "type": e.__class__.__name__}, 500)
        if isinstance(res, tuple):
            body, code = res[0], res[1]
            if isinstance(body, Raw):
                return self._send_raw(body, code)
            return self._send(body, code)
        if isinstance(res, Raw):
            return self._send_raw(res, 200)
        code = 200
        if isinstance(res, dict) and res.get("ok") is False:
            code = int(res.get("code") or 400)
        self._send(res, code)

    def _send_raw(self, raw, code=200):
        try:
            self.send_response(code)
            for k, v in raw.headers().items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(raw.data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw.data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def do_GET(self):
        self._dispatch("GET")

    def do_POST(self):
        self._dispatch("POST")

    def do_OPTIONS(self):
        self._send(b"", 204)

    # ---------------- SSE ----------------
    def _sse(self):
        q = G.bus.subscribe()
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self._sse_write("hello", {"at": util.now_iso(),
                                      "note": "Mail Workbench V2 事件流已连接"})
            last_beat = time.time()
            while True:
                try:
                    ev = q.get(timeout=1.0)
                    self._sse_write(ev.get("type") or "event", ev)
                except queue.Empty:
                    pass
                if time.time() - last_beat > 20:
                    last_beat = time.time()
                    try:
                        self.wfile.write(b": keep-alive\n\n")
                        self.wfile.flush()
                    except Exception:
                        break
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            G.bus.unsubscribe(q)

    def _sse_write(self, name: str, payload: dict):
        body = "event: %s\ndata: %s\n\n" % (name, json.dumps(payload, ensure_ascii=False))
        self.wfile.write(body.encode("utf-8"))
        self.wfile.flush()


class _Request:
    def __init__(self, handler, method, path, query, path_params):
        self.handler = handler
        self.method = method
        self.path = path
        self.query = query
        self.path_params = path_params
        self._json = None

    @property
    def json(self):
        if self._json is None:
            self._json = self.handler._json_body()
        return self._json


def _msg_public(m: dict, full: bool = False) -> dict:
    out = {
        "message_id": m.get("message_id"),
        "uid": m.get("uid"),
        "folder": m.get("folder"),
        "thread_id": m.get("thread_id"),
        "subject": m.get("subject"),
        "from_name": m.get("from_name"),
        "from_addr": m.get("from_addr"),
        "to_addrs": util.parse_recipients(m.get("to_addrs") or ""),
        "date_iso": m.get("date_iso"),
        "unread": bool(m.get("unread")),
        "answered": bool(m.get("answered")),
        "flagged": bool(m.get("flagged")),
        "deleted": bool(m.get("deleted")),
        "has_attachments": bool(m.get("has_attachments")),
        "attachment_names": m.get("attachment_names") if isinstance(m.get("attachment_names"), list)
        else [],
        "snippet": m.get("snippet"),
        "classification": m.get("classification"),
        "workflow_state": m.get("workflow_state"),
        "workflow_changed_at": m.get("workflow_changed_at"),
        "snooze_until": m.get("snooze_until"),
        "source": m.get("source"),
        "priority": m.get("priority"),
        # 群发通知判定：列表行要在意它（用来解释「为什么这封不在待我处理里」）。
        # 工作桶的 _row() 也会给这个字段，两处形状保持一致，UI 才能复用同一套渲染。
        "broadcast": bool(m.get("is_broadcast")),
    }
    if full:
        out["body_text"] = m.get("body_text") or ""
        out["in_reply_to"] = m.get("in_reply_to")
    return out


def _att_public(cfg: dict, att: dict, message_id: str = "") -> dict:
    """给 UI 的附件条目：加上可点的 id、下载/打开 URL、是否已在本地。

    带上 `downloadable` 是因为 Foxmail 历史记录只有元数据、没有附件内容 ——
    UI 得能区分「可以点」和「点了也没用」，而不是让用户点了才报错。

    URL 里的 aid 必须 quote：附件 id 是 `<sha256>:<原始文件名>`，含中文或 `/`
    时不编码就会拼出非法 URL（详见 `_aid_from` 的说明）。
    """
    from urllib.parse import quote
    aid = att.get("attachment_id") or ""
    mid = message_id or att.get("message_id") or ""
    q = quote(aid, safe="")
    downloaded = ats.is_downloaded(cfg, att)
    return {
        "attachment_id": aid,
        "message_id": mid,
        "filename": att.get("filename") or "",
        "content_type": att.get("content_type") or "",
        "size_bytes": int(att.get("size_bytes") or 0),
        "sha256": att.get("sha256") or "",
        "downloaded": downloaded,
        "downloadable": bool(mid),
        "download_url": "/api/v2/attachments/download?aid=%s" % q,
        "preview_url": "/api/v2/attachments/download?aid=%s&inline=1" % q,
        # 本地路径只在本机可见（服务只监听 127.0.0.1），方便用户直接去翻
        "local_path": ats.local_path(cfg, att.get("sha256"), att.get("filename"))
        if downloaded else "",
    }


# ==========================================================================
# 启动
# ==========================================================================
def make_logger(logs_dir: str):
    os.makedirs(logs_dir, exist_ok=True)
    path = os.path.join(logs_dir, "workbench-%s.log" % util.now().strftime("%Y-%m-%d"))
    lock = threading.Lock()

    def log(msg: str):
        line = "[%s] %s" % (util.now().strftime("%H:%M:%S"), msg)
        with lock:
            try:
                with open(path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
            except OSError:
                pass
        try:
            print(line, flush=True)
        except Exception:
            pass

    return log


def build_app(cfg: dict = None, log=None) -> App:
    cfg = cfg or cfgmod.load_config()
    cfgmod.ensure_dirs(cfg)
    return App(cfg, log=log)


def run(host: str = None, port: int = None, idle: bool = True, scheduler: bool = True,
        cfg: dict = None) -> int:
    global G
    cfg = cfg or cfgmod.load_config()
    cfgmod.ensure_dirs(cfg)
    log = make_logger(cfg["logs_dir"])
    log("Mail Workbench V2 启动中（project_root=%s）" % cfg["project_root"])

    if not cfgmod.has_credentials(cfg):
        log("⚠ 未找到邮箱凭据：请在 %s 写入 SJTU_MAIL_USER / SJTU_MAIL_PASS"
            % cfgmod.secrets_file_hint())

    G = build_app(cfg, log=log)
    G.start(idle=idle, scheduler=scheduler)

    host = host or cfg.get("http_host", "127.0.0.1")
    port = int(port or cfg.get("http_port", 18952))
    try:
        srv = ThreadingHTTPServer((host, port), Handler)
    except OSError as e:
        log("端口 %s 绑定失败：%s（可能已有实例在跑）" % (port, e))
        G.stop()
        return 2
    srv.daemon_threads = True
    log("UI:      http://%s:%d/" % (host, port))
    log("经典视图: http://%s:%d/classic  （V1 UI 原样保留）" % (host, port))
    log("健康检查: http://%s:%d/api/health" % (host, port))
    log("Worker 契约: http://%s:%d/api/draft-jobs/next" % (host, port))
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        log("收到中断，正在退出…")
    finally:
        try:
            srv.shutdown()
        except Exception:
            pass
        G.stop()
        log("已停止。")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Mail Workbench 本地服务")
    ap.add_argument("--host", default=None)
    ap.add_argument("--port", type=int, default=None)
    ap.add_argument("--no-idle", action="store_true", help="不启动 IMAP IDLE（离线调试）")
    ap.add_argument("--no-scheduler", action="store_true", help="不启动本地 scheduler")
    ap.add_argument("--check", action="store_true", help="只做自检不启动服务")
    args = ap.parse_args(argv)

    if args.check:
        cfg = cfgmod.load_config()
        log = make_logger(cfg["logs_dir"])
        app = build_app(cfg, log=log)
        print(json.dumps(app.health(), ensure_ascii=False, indent=2))
        return 0

    return run(host=args.host, port=args.port,
               idle=not args.no_idle, scheduler=not args.no_scheduler)


if __name__ == "__main__":
    sys.exit(main())
