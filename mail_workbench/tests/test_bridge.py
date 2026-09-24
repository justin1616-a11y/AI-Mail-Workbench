# -*- coding: utf-8 -*-
"""V1 ↔ V2 桥接层测试。

这一层是「增量升级、不推倒重来」的核心承诺，必须被测住：

  1. URL 映射：/api/v2/x -> /api/x，UI 走 /workbench
  2. V1 文件队列 → SQLite 的 dual-read 迁移：幂等、不删旧文件、状态映射正确
  3. UI 注入：同一份 ui/index.html 支持独立部署与嵌入部署
  4. V1 既有路由不受影响（服务器脚本层用冒烟测试覆盖，这里测映射契约）
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
PKG_ROOT = os.path.dirname(HERE)
PROJ_ROOT = os.path.dirname(PKG_ROOT)
if PROJ_ROOT not in sys.path:
    sys.path.insert(0, PROJ_ROOT)

from mail_workbench import v1_bridge                       # noqa: E402
from mail_workbench import util                            # noqa: E402
from mail_workbench.server import App                      # noqa: E402
from mail_workbench.storage.database import open_db         # noqa: E402
from mail_workbench.storage.repo import Repo               # noqa: E402

from .test_v2 import make_cfg                              # noqa: E402


class TestUrlMapping(unittest.TestCase):
    def test_api_detection(self):
        self.assertTrue(v1_bridge.is_v2_api("/api/v2"))
        self.assertTrue(v1_bridge.is_v2_api("/api/v2/buckets"))
        self.assertTrue(v1_bridge.is_v2_api("/api/v2/draft-jobs/next"))
        # V1 路由绝不能被误判
        for p in ("/api/bootstrap", "/api/messages", "/api/health", "/api/reply-queue",
                  "/api/send", "/", "/workbench"):
            self.assertFalse(v1_bridge.is_v2_api(p), p)

    def test_ui_detection(self):
        for p in ("/workbench", "/workbench/", "/workbench.html"):
            self.assertTrue(v1_bridge.is_v2_ui(p), p)
        self.assertFalse(v1_bridge.is_v2_ui("/"))

    def test_internal_mapping(self):
        cases = {
            "/api/v2/health": "/api/health",
            "/api/v2/buckets": "/api/buckets",
            "/api/v2/draft-jobs/next": "/api/draft-jobs/next",
            "/api/v2/drafts/job_1/approve": "/api/drafts/job_1/approve",
            "/api/v2/search": "/api/search",
            "/api/v2/events": "/api/events",
        }
        for src, dst in cases.items():
            self.assertEqual(v1_bridge.to_internal(src), dst, src)

    def test_mapped_paths_hit_real_routes(self):
        from mail_workbench.server import ROUTER
        self.assertIsNotNone(ROUTER.match("GET", v1_bridge.to_internal("/api/v2/health"))[0])
        self.assertIsNotNone(ROUTER.match("GET", v1_bridge.to_internal("/api/v2/buckets"))[0])
        self.assertIsNotNone(ROUTER.match("GET", v1_bridge.to_internal("/api/v2/draft-jobs/next"))[0])
        fn, params = ROUTER.match("GET", v1_bridge.to_internal("/api/v2/draft-jobs/j1/context"))
        self.assertIsNotNone(fn)
        self.assertEqual(params["job_id"], "j1")
        fn, _ = ROUTER.match("POST", v1_bridge.to_internal("/api/v2/drafts/j1/send"))
        self.assertIsNotNone(fn)

    def test_unknown_v2_path_is_404(self):
        obj, code = v1_bridge.dispatch("GET", "/api/v2/definitely-not-a-route", {}, None)
        self.assertIsNone(obj)
        self.assertEqual(code, 404)


class TestUiRender(unittest.TestCase):
    def test_render_injects_api_base_only(self):
        """注入 API base；**不再**注入「← 经典视图」入口（用户要求去掉）。

        V1 经典视图本身没删（`/` 与 `/classic` 仍可用），只是不再从 V2 界面上露出来。
        """
        ui = os.path.join(PKG_ROOT, "ui", "index.html")
        body = v1_bridge.render_ui(ui).decode("utf-8")
        self.assertIn("MW_API_BASE='/api/v2'", body)
        self.assertNotIn("经典视图", body, "顶栏不该再有「经典视图」入口")
        self.assertIn("Mail Workbench", body)
        # UI 源码里也不该残留这个入口
        with open(ui, "r", encoding="utf-8") as f:
            self.assertNotIn("经典视图", f.read())

    def test_ui_reads_api_base(self):
        ui = os.path.join(PKG_ROOT, "ui", "index.html")
        with open(ui, "r", encoding="utf-8") as f:
            body = f.read()
        self.assertIn("window.MW_API_BASE", body)
        self.assertIn("API_BASE + path", body)
        self.assertIn("API_BASE + \"/events\"", body)

    def test_ui_never_hardcodes_api_prefix(self):
        """UI 内部路径必须**不带** /api，由 API_BASE 决定前缀。

        踩过的坑：UI 写 `api("/api/brief")` + API_BASE='/api/v2'
        => 请求 /api/v2/api/brief => 404，整个界面空白。
        """
        ui = os.path.join(PKG_ROOT, "ui", "index.html")
        with open(ui, "r", encoding="utf-8") as f:
            body = f.read()
        self.assertNotIn('api("/api/', body, "UI 里不应出现 api(\"/api/ 形式（会拼出双前缀）")
        self.assertIn('? window.MW_API_BASE : "/api";', body,
                      "独立部署时默认前缀应为 /api")

    def test_message_id_is_url_decoded(self):
        """Message-ID 含 < > @，浏览器会百分号编码，服务端必须解回来。"""
        self.assertEqual(
            v1_bridge.to_internal("/api/v2/messages/%3Cabc%40123%3E"),
            "/api/messages/<abc@123>")
        from mail_workbench.server import ROUTER
        fn, params = ROUTER.match(
            "GET", v1_bridge.to_internal("/api/v2/messages/%3Cabc%40123%3E"))
        self.assertIsNotNone(fn)
        self.assertEqual(params["message_id"], "<abc@123>")

    def test_sse_emits_named_events(self):
        """V2 UI 用 addEventListener('<type>')，SSE 必须带 event: 行。

        直接读源文件比 inspect.getsource 稳（后者对函数块的解析偶发截断）。
        """
        src_path = os.path.join(PKG_ROOT, "v1_bridge.py")
        with open(src_path, "r", encoding="utf-8") as f:
            body = f.read()
        i = body.index("def _sse_write(")
        segment = body[i:i + 900]
        self.assertIn('event: ', segment, "SSE 必须写 event: 名，否则命名监听收不到")
        self.assertIn('"type"', segment)
        # 命名事件必须取自适应：type 缺省时回落为 'event'
        self.assertIn('or "event"', segment)


class TestReplyQueueMigration(unittest.TestCase):
    """V1 文件队列（reply-queue/{pending,done,expired}/*.json）→ SQLite。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = self.tmp.name
        self.db = open_db(os.path.join(self.root, "t.db"))
        self.repo = Repo(self.db)
        self.cfg = make_cfg(os.path.join(self.root, "t.db"),
                            project_root=self.root, logs_dir=os.path.join(self.root, "logs"))
        self.app = App(self.cfg)
        v1_bridge.set_app(self.app)
        self._write_legacy()

    def tearDown(self):
        v1_bridge.set_app(None)
        try:
            self.app.repo.db.close()
        except Exception:
            pass
        self.db.close()
        self.tmp.cleanup()

    def _write_legacy(self):
        for bucket in ("pending", "done", "expired"):
            os.makedirs(os.path.join(self.root, "reply-queue", bucket), exist_ok=True)
        items = {
            "pending": [
                {"id": "rq_manual_1", "status": "pending", "source": "manual",
                 "message_id": "<rq1@x>", "subject": "导师双选", "sender": "王同学",
                 "folder": "INBOX", "created_at": "2026-09-21T10:00:00+08:00",
                 "priority": 80},
                {"id": "rq_auto_2", "status": "generating", "source": "auto",
                 "message_id": "<rq2@x>", "subject": "产学研申报", "sender": "科研办",
                 "created_at": "2026-09-21T11:00:00+08:00", "attempt": 2},
            ],
            "done": [
                {"id": "rq_done_3", "status": "ready", "message_id": "<rq3@x>",
                 "subject": "已完成草稿", "draft": "你好，已收到。",
                 "created_at": "2026-09-20T09:00:00+08:00"},
            ],
            "expired": [
                {"id": "rq_exp_4", "status": "expired", "message_id": "<rq4@x>",
                 "subject": "过期任务", "created_at": "2026-09-10T09:00:00+08:00"},
            ],
        }
        for bucket, objs in items.items():
            for o in objs:
                p = os.path.join(self.root, "reply-queue", bucket, o["id"] + ".json")
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(o, f, ensure_ascii=False)

    def test_dry_run_does_not_write(self):
        res = v1_bridge.migrate_reply_queue(dry_run=True)
        self.assertEqual(res["scanned"], 4)
        self.assertEqual(res["imported"], 4)
        self.assertEqual(self.repo.job_counts(), {})

    def test_migration_imports_all_with_rid_as_job_id(self):
        res = v1_bridge.migrate_reply_queue()
        self.assertEqual(res["scanned"], 4)
        self.assertEqual(res["imported"], 4)
        self.assertEqual(res["skipped"], 0)
        for rid in ("rq_manual_1", "rq_auto_2", "rq_done_3", "rq_exp_4"):
            self.assertIsNotNone(self.repo.get_job(rid), rid)

    def test_status_mapping(self):
        v1_bridge.migrate_reply_queue()
        self.assertEqual(self.repo.get_job("rq_manual_1")["status"], "queued")
        self.assertEqual(self.repo.get_job("rq_auto_2")["status"], "generating")
        self.assertEqual(self.repo.get_job("rq_done_3")["status"], "ready")
        self.assertEqual(self.repo.get_job("rq_done_3")["draft_text"], "你好，已收到。")
        self.assertEqual(self.repo.get_job("rq_exp_4")["status"], "expired")

    def test_preserves_metadata_and_audit(self):
        v1_bridge.migrate_reply_queue()
        j = self.repo.get_job("rq_manual_1")
        self.assertEqual(j["subject"], "导师双选")
        self.assertEqual(j["sender"], "王同学")
        self.assertEqual(j["priority"], 80)
        self.assertEqual(j["trigger_source"], "manual")
        self.assertEqual(j["retry_count"], 0)
        ctx = j["context_snapshot"]
        self.assertEqual(ctx["migrated_from"], "v1_reply_queue")
        events = self.repo.job_events("rq_manual_1")
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["actor"], "migration")
        self.assertEqual(j["retry_count"], 0)
        self.assertEqual(self.repo.get_job("rq_auto_2")["retry_count"], 2)

    def test_migration_is_idempotent(self):
        first = v1_bridge.migrate_reply_queue()
        second = v1_bridge.migrate_reply_queue()
        self.assertEqual(first["imported"], 4)
        self.assertEqual(second["imported"], 0)
        self.assertEqual(second["skipped"], 4)
        self.assertEqual(len(self.repo.list_jobs(limit=100)), 4)

    def test_old_json_files_are_kept(self):
        v1_bridge.migrate_reply_queue()
        for bucket, names in (("pending", 2), ("done", 1), ("expired", 1)):
            d = os.path.join(self.root, "reply-queue", bucket)
            got = [n for n in os.listdir(d) if n.endswith(".json")]
            self.assertEqual(len(got), names, bucket)

    def test_legacy_view_reads_files(self):
        view = v1_bridge.legacy_queue_view()
        self.assertEqual(view["buckets"]["pending"]["count"], 2)
        self.assertEqual(view["buckets"]["done"]["count"], 1)
        self.assertEqual(view["buckets"]["expired"]["count"], 1)

    def test_missing_dir_is_not_an_error(self):
        empty = tempfile.TemporaryDirectory()
        try:
            cfg = make_cfg(os.path.join(empty.name, "x.db"), project_root=empty.name)
            app = App(cfg)
            v1_bridge.set_app(app)
            res = v1_bridge.migrate_reply_queue()
            self.assertTrue(res["ok"])
            self.assertEqual(res["reason"], "no_legacy_queue")
            app.repo.db.close()
        finally:
            v1_bridge.set_app(self.app)
            empty.cleanup()

    def test_unmappable_status_goes_failed_with_reason(self):
        p = os.path.join(self.root, "reply-queue", "pending", "rq_weird_5.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"id": "rq_weird_5", "status": "量子态", "message_id": "<rq5@x>"},
                      f, ensure_ascii=False)
        v1_bridge.migrate_reply_queue()
        j = self.repo.get_job("rq_weird_5")
        self.assertEqual(j["status"], "failed")
        self.assertIn("量子态", j["last_error"])

    def test_migrated_jobs_appear_in_v2_queue_view(self):
        v1_bridge.migrate_reply_queue()
        queued = [j["job_id"] for j in self.repo.list_jobs(statuses=("queued",))]
        self.assertIn("rq_manual_1", queued)
        ready = [j["job_id"] for j in self.repo.list_jobs(statuses=("ready",))]
        self.assertIn("rq_done_3", ready)

    def test_migrated_job_can_be_claimed_and_approved(self):
        """迁移进来的 queued 任务必须能走完整的 V2 生命周期。"""
        from mail_workbench.draft import queue as dq
        v1_bridge.migrate_reply_queue()
        job = self.repo.get_job("rq_manual_1")
        self.repo.update_job("rq_manual_1", {
            "recipients": ["a@b.com"], "idempotency_key": None})
        claimed = dq.claim_next(self.repo, self.app.cfg, worker="wb")
        self.assertIsNotNone(claimed)
        dq.set_draft(self.repo, "rq_manual_1", "迁移后的草稿正文")
        self.assertEqual(self.repo.get_job("rq_manual_1")["status"], "ready")
        res = dq.approve(self.repo, self.app.cfg, "rq_manual_1")
        self.assertIn("approve_token", res)
        self.assertEqual(self.repo.get_job("rq_manual_1")["status"], "approved")


class TestAppStatus(unittest.TestCase):
    def test_status_shape(self):
        tmp = tempfile.TemporaryDirectory()
        try:
            cfg = make_cfg(os.path.join(tmp.name, "s.db"),
                           logs_dir=os.path.join(tmp.name, "logs"))
            app = App(cfg)
            v1_bridge.set_app(app)
            st = v1_bridge.app_status()
            self.assertTrue(st["running"])
            self.assertIn("db", st)
            self.assertIn("sse_clients", st)
        finally:
            v1_bridge.set_app(None)
            try:
                app.repo.db.close()
            except Exception:
                pass
            tmp.cleanup()

    def test_status_when_not_started(self):
        v1_bridge.set_app(None)
        self.assertEqual(v1_bridge.app_status(), {"running": False})


class TestApiContract(unittest.TestCase):
    """V2 API 响应契约：每个端点都必须带 `ok` 字段。

    踩过的坑：`/api/health` 与 `/api/metrics` 早期返回裸 dict（没有 ok），
    前端 `if (!r.ok) return;` 直接静默退出 —— 表现是「KPI 区永远是空的」，
    既不报错也不提示，极难排查。所以这里把它固化成契约测试。
    """

    READ_PATHS = [
        "/api/v2/health", "/api/v2/metrics", "/api/v2/buckets", "/api/v2/brief",
        "/api/v2/constants", "/api/v2/contract", "/api/v2/state", "/api/v2/state-machine",
        "/api/v2/folders", "/api/v2/messages", "/api/v2/threads", "/api/v2/candidates",
        "/api/v2/draft-jobs", "/api/v2/draft-jobs/next", "/api/v2/search",
        "/api/v2/snoozes", "/api/v2/followups", "/api/v2/contacts",
        "/api/v2/draft-memory", "/api/v2/app", "/api/v2/legacy/reply-queue",
        "/api/v2/undo/last", "/api/v2/events/recent",
    ]

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = make_cfg(os.path.join(self.tmp.name, "c.db"),
                       logs_dir=os.path.join(self.tmp.name, "logs"),
                       foxmail_enabled=False)
        self.app = App(cfg)
        from mail_workbench.mail import imap_client, smtp_client, foxmail_index
        self._stubs = (imap_client.check_connection, smtp_client.verify_connection,
                       foxmail_index.stats)
        imap_client.check_connection = lambda c: {"ok": True, "latency_ms": 1.0, "inbox": {}}
        smtp_client.verify_connection = lambda c: {"ok": True, "latency_ms": 1.0}
        foxmail_index.stats = lambda p: {"ok": False, "reason": "test"}
        v1_bridge.set_app(self.app)
        import mail_workbench.server as mwserver
        self.mwserver = mwserver
        self._saved_G = mwserver.G
        mwserver.G = self.app

    def tearDown(self):
        imap_client, smtp_client, foxmail_index = (
            __import__("mail_workbench.mail.imap_client", fromlist=["x"]),
            __import__("mail_workbench.mail.smtp_client", fromlist=["x"]),
            __import__("mail_workbench.mail.foxmail_index", fromlist=["x"]))
        imap_client.check_connection, smtp_client.verify_connection, foxmail_index.stats = self._stubs
        self.mwserver.G = self._saved_G
        v1_bridge.set_app(None)
        try:
            self.app.repo.db.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def _call(self, method, path, query=None, payload=None):
        """走真实入口 dispatch()（含桥接层自有端点），而不是直接调 ROUTER。"""
        obj, code = v1_bridge.dispatch(method, path, query or {}, payload or {})
        self.assertIsNotNone(obj, "路由未实现：%s %s" % (method, path))
        return obj

    def test_read_endpoints_all_return_ok(self):
        for p in self.READ_PATHS:
            res = self._call("GET", p)
            self.assertIsInstance(res, dict, p)
            self.assertIn("ok", res, "端点 %s 缺少 ok 字段（前端会静默失败）" % p)

    def test_health_and_metrics_payload_shape(self):
        h = self._call("GET", "/api/v2/health")
        self.assertTrue(h["ok"])
        self.assertIn("components", h["data"])
        self.assertIn("status", h["data"])
        m = self._call("GET", "/api/v2/metrics")
        self.assertTrue(m["ok"])
        self.assertIn("kpi", m["data"])
        self.assertIn("time_to_inbox_zero_hours", m["data"]["kpi"])
        self.assertIn("draft_acceptance_rate", m["data"]["kpi"])

    def test_buckets_payload_shape(self):
        b = self._call("GET", "/api/v2/buckets")
        self.assertTrue(b["ok"])
        for k in ("ACTION_REQUIRED", "WAITING_FOR_REPLY", "AI_DRAFT_READY",
                  "SNOOZED", "DONE_TODAY"):
            self.assertIn(k, b["data"]["counts"])
            self.assertIn(k, b["data"]["buckets"])

    def test_worker_contract_flat_shape(self):
        """Worker 契约的 /draft-jobs/next 保持扁平（job 在顶层），并带 ok。"""
        res = self._call("GET", "/api/v2/draft-jobs/next")
        self.assertTrue(res["ok"])
        self.assertIn("job", res)

    def test_message_detail_with_special_message_id(self):
        from mail_workbench.thread import aggregator as agg
        from .test_v2 import mk_msg
        msg = mk_msg("<special/id@example.com>", "带斜杠的 Message-ID", "a@b.com",
                     classification="REPLY")
        agg.attach_message(self.app.repo, msg, self.app.cfg)
        from urllib.parse import quote
        p = "/api/v2/messages/" + quote("<special/id@example.com>", safe="")
        res = self._call("GET", p)
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["data"]["subject"], "带斜杠的 Message-ID")

    def test_maintenance_reclassify_route(self):
        """重算分类端点在桥接层可用（改完规则要靠它生效）。"""
        res = self._call("POST", "/api/v2/maintenance/reclassify", payload={})
        self.assertTrue(res.get("ok"), res)
        self.assertIn("scanned", res["data"])

    def test_attachment_download_and_open_endpoints(self):
        """附件三件套：列表带可点信息、下载给二进制、打开走默认程序。"""
        from mail_workbench.thread import aggregator as agg
        from .test_v2 import mk_msg, _build_att_mail
        from mail_workbench.mail import imap_client
        from mail_workbench.mail import parser as mparser
        from mail_workbench.server import Raw

        raw = _build_att_mail()
        parts = mparser.list_attachments(raw, with_data=True)
        self.assertTrue(parts)
        msg = mk_msg("<att-ish@example.com>", "带附件", "a@b.com",
                     classification="REPLY",
                     flags={"has_attachments": True, "attachment_names": ["简历.pdf"]})
        agg.attach_message(self.app.repo, msg, self.app.cfg)
        self.app.repo.db.execute(
            "UPDATE messages SET uid='77' WHERE message_id='<att-ish@example.com>'")
        self.app.repo.record_attachment({
            "attachment_id": "bridge-att-1", "message_id": "<att-ish@example.com>",
            "filename": "简历.pdf", "content_type": "application/pdf",
            "size_bytes": len(parts[0]["data"]), "sha256": parts[0]["sha256"]})

        # 列表：必须给出可用的 id/URL 与 downloadable 判定
        lst = self._call("GET", "/api/v2/attachments/<att-ish@example.com>")
        self.assertTrue(lst.get("ok"))
        item = lst["data"]["attachments"][0]
        self.assertEqual(item["attachment_id"], "bridge-att-1")
        self.assertTrue(item["downloadable"])
        # aid 放在 query 里且经过 quote —— 附件 id 含中文文件名时路径段会失效
        self.assertIn("/api/v2/attachments/download?aid=", item["download_url"])

        # 下载：返回 Raw（二进制），不是 JSON
        orig = imap_client.ImapClient.fetch_full
        orig_c, orig_s, orig_cl = (imap_client.ImapClient.connect,
                                   imap_client.ImapClient.select,
                                   imap_client.ImapClient.close)
        imap_client.ImapClient.connect = lambda self_, *a, **k: None
        imap_client.ImapClient.select = lambda self_, f, readonly=True: {"exists": 1}
        imap_client.ImapClient.close = lambda self_, *a, **k: None
        imap_client.ImapClient.fetch_full = lambda self_, uids: {77: {"raw": raw, "flags": []}}
        try:
            got = self._call("GET", "/api/v2/attachments/download",
                             query={"aid": ["bridge-att-1"]})
            self.assertIsInstance(got, Raw, "下载必须是二进制而非 JSON")
            self.assertEqual(got.data, parts[0]["data"])
            self.assertIn("attachment", got.headers()["Content-Disposition"])
            self.assertIn("filename*=UTF-8''", got.headers()["Content-Disposition"])
        finally:
            imap_client.ImapClient.fetch_full = orig
            imap_client.ImapClient.connect = orig_c
            imap_client.ImapClient.select = orig_s
            imap_client.ImapClient.close = orig_cl

        # 打开：真去调系统默认程序会弹窗，测试里替换掉
        from mail_workbench.mail import attachment_store as atstore
        opened = []
        orig_open = atstore.open_with_default
        atstore.open_with_default = lambda p: (opened.append(p), {"ok": True, "path": p})[1]
        try:
            res = self._call("POST", "/api/v2/attachments/open",
                             payload={"attachment_id": "bridge-att-1"})
            self.assertTrue(res.get("ok"), res)
            self.assertEqual(len(opened), 1, "应当调用了一次默认程序打开")
        finally:
            atstore.open_with_default = orig_open

    def test_attachment_id_with_slash_is_reachable(self):
        """附件名含 `/` 时必须仍然能下载（这是把 aid 放 query 而不是路径段的理由）。"""
        from mail_workbench.thread import aggregator as agg
        from .test_v2 import mk_msg
        from mail_workbench.mail import imap_client
        from mail_workbench.server import Raw
        from mail_workbench.mail import parser as mparser
        from .test_v2 import _build_att_mail

        raw = _build_att_mail()
        parts = mparser.list_attachments(raw, with_data=True)
        msg = mk_msg("<slash-att@example.com>", "斜杠附件", "a@b.com",
                     classification="REPLY")
        agg.attach_message(self.app.repo, msg, self.app.cfg)
        self.app.repo.db.execute(
            "UPDATE messages SET uid='88' WHERE message_id='<slash-att@example.com>'")
        # 模拟邮件里写成 "docs/report.pdf" 的附件名
        weird_id = "%s:docs/report.pdf" % parts[0]["sha256"]
        self.app.repo.record_attachment({
            "attachment_id": weird_id, "message_id": "<slash-att@example.com>",
            "filename": "docs/report.pdf", "content_type": "application/pdf",
            "size_bytes": len(parts[0]["data"]), "sha256": parts[0]["sha256"]})

        orig_c, orig_s, orig_cl = (imap_client.ImapClient.connect,
                                   imap_client.ImapClient.select,
                                   imap_client.ImapClient.close)
        orig = imap_client.ImapClient.fetch_full
        imap_client.ImapClient.connect = lambda self_, *a, **k: None
        imap_client.ImapClient.select = lambda self_, f, readonly=True: {"exists": 1}
        imap_client.ImapClient.close = lambda self_, *a, **k: None
        imap_client.ImapClient.fetch_full = lambda self_, uids: {88: {"raw": raw, "flags": []}}
        try:
            got = self._call("GET", "/api/v2/attachments/download",
                             query={"aid": [weird_id]})
            self.assertIsInstance(got, Raw, "含斜杠的附件 id 也必须能下载")
        finally:
            imap_client.ImapClient.fetch_full = orig
            imap_client.ImapClient.connect = orig_c
            imap_client.ImapClient.select = orig_s
            imap_client.ImapClient.close = orig_cl

    def test_attachment_error_is_400_not_500(self):
        """附件取不到时应是明确的 4xx + 中文原因，不是 500。"""
        res = self._call("POST", "/api/v2/attachments/open",
                         payload={"attachment_id": "does-not-exist"})
        self.assertFalse(res.get("ok"))
        self.assertEqual(res.get("code") or 400, 400)
        self.assertIn("不存在", res.get("error") or "")
        # 缺 aid 也要给出明确提示，而不是匹配到别的路由
        res2 = self._call("GET", "/api/v2/attachments/download")
        self.assertFalse(res2.get("ok"))
        self.assertIn("aid", res2.get("error") or "")


class TestConfigSources(unittest.TestCase):
    """配置来源优先级 —— 用户改的那份必须**真的**被读到。

    踩过的坑：V2 起初只读「包内」与「技能目录」两份 config.json，
    不读**项目根**那一份 —— 而 README / install 引导用户改的正是它。
    于是「把自建文件夹加进 sync_folders」这种操作毫无效果，
    现象是「我明明改了配置，界面没变」，排查起来像见鬼。
    """

    def test_project_root_config_is_last_and_highest_priority(self):
        import mail_workbench.config as cfgmod
        paths = [os.path.normcase(os.path.abspath(p)) for p in cfgmod.CONFIG_PATHS]
        proj = os.path.normcase(os.path.abspath(
            os.path.join(cfgmod.PROJECT_ROOT, "config.json")))
        self.assertIn(proj, paths, "项目根的 config.json 必须在读取列表里")
        self.assertEqual(paths[-1], proj, "项目配置应最后读 => 最高优先级")
        self.assertEqual(len(paths), len(set(paths)), "读取列表不该有重复")

    def test_later_config_overrides_earlier(self):
        """用临时文件验证合并机制本身（不依赖本机 config.json 的内容）。"""
        import mail_workbench.config as cfgmod
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        low = os.path.join(tmp.name, "low.json")
        high = os.path.join(tmp.name, "high.json")
        with open(low, "w", encoding="utf-8") as f:
            json.dump({"from_name": "低", "sync_folders": ["INBOX"]}, f)
        with open(high, "w", encoding="utf-8") as f:
            json.dump({"from_name": "高",
                       "sync_folders": ["INBOX", "专利代理"]}, f, ensure_ascii=False)
        saved = cfgmod.CONFIG_PATHS
        cfgmod.CONFIG_PATHS = [low, high]
        try:
            cfg = cfgmod.load_config()
        finally:
            cfgmod.CONFIG_PATHS = saved
        self.assertEqual(cfg["from_name"], "高", "后一份应覆盖前一份")
        self.assertEqual(cfg["sync_folders"], ["INBOX", "专利代理"],
                         "中文文件夹名必须原样读进来（不能被编码搞坏）")

    def test_underscore_keys_are_ignored(self):
        """`_comment` 这类下划线键是给人看的，不该进配置。"""
        import mail_workbench.config as cfgmod
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = os.path.join(tmp.name, "c.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({"_comment": "说明文字", "_x": 1, "page_size": 60}, f)
        saved = cfgmod.CONFIG_PATHS
        cfgmod.CONFIG_PATHS = [p]
        try:
            cfg = cfgmod.load_config()
        finally:
            cfgmod.CONFIG_PATHS = saved
        self.assertNotIn("_comment", cfg)
        self.assertNotIn("_x", cfg)


class TestSidebarAndCompose(unittest.TestCase):
    """侧边栏（文件夹树 / 标签树）与「写邮件」通道。

    这两块的共同点是**用户直接看得见**，所以除了功能还要测「形状」：
    文件夹列表必须自带计数（否则侧边栏上一排 0 就是骗人），
    自建文件夹必须能被标出来（否则会被当成系统文件夹混在一起），
    写邮件必须在没有 confirm 时被**服务端**拒绝（前端确认框不是闸门）。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        cfg = make_cfg(os.path.join(self.tmp.name, "c.db"),
                       logs_dir=os.path.join(self.tmp.name, "logs"),
                       foxmail_enabled=False,
                       sync_folders=["INBOX", "Sent", "Drafts", "Junk", "Trash",
                                     "专利代理", "未来电池中心"])
        self.app = App(cfg)
        from mail_workbench.mail import imap_client, smtp_client, foxmail_index
        self._stubs = (imap_client.check_connection, smtp_client.verify_connection,
                       foxmail_index.stats)
        imap_client.check_connection = lambda c: {"ok": True, "latency_ms": 1.0, "inbox": {}}
        smtp_client.verify_connection = lambda c: {"ok": True, "latency_ms": 1.0}
        foxmail_index.stats = lambda p: {"ok": False, "reason": "test"}
        v1_bridge.set_app(self.app)
        import mail_workbench.server as mwserver
        self.mwserver = mwserver
        self._saved_G = mwserver.G
        mwserver.G = self.app
        self.cfg = cfg

    def tearDown(self):
        from mail_workbench.mail import imap_client, smtp_client, foxmail_index
        (imap_client.check_connection, smtp_client.verify_connection,
         foxmail_index.stats) = self._stubs
        self.mwserver.G = self._saved_G
        v1_bridge.set_app(None)
        try:
            self.app.repo.db.close()
        except Exception:
            pass
        self.tmp.cleanup()

    def _call(self, method, path, query=None, payload=None):
        obj, code = v1_bridge.dispatch(method, path, query or {}, payload or {})
        self.assertIsNotNone(obj, "路由未实现：%s %s" % (method, path))
        return obj

    def _stub_imap_append(self, sink):
        """把 IMAP 的 append 换掉，避免测试真的联网写邮箱。"""
        from mail_workbench.mail import imap_client
        saved = (imap_client.ImapClient.connect, imap_client.ImapClient.append,
                 imap_client.ImapClient.close)

        def _append(self_, folder, raw, flags="(\\Seen)"):
            sink.append({"folder": folder, "raw": raw, "flags": flags})
            return True

        imap_client.ImapClient.connect = lambda self_, *a, **k: None
        imap_client.ImapClient.append = _append
        imap_client.ImapClient.close = lambda self_, *a, **k: None
        return lambda: (setattr(imap_client.ImapClient, "connect", saved[0]),
                        setattr(imap_client.ImapClient, "append", saved[1]),
                        setattr(imap_client.ImapClient, "close", saved[2]))

    def _add(self, mid, subject, cls, folder="INBOX", unread=True):
        from mail_workbench.thread import aggregator as agg
        from .test_v2 import mk_msg
        msg = mk_msg(mid, subject, "a@b.com", classification=cls, folder=folder,
                     flags={"unread": unread})
        agg.attach_message(self.app.repo, msg, self.cfg)

    # ---------------- 文件夹树 ----------------
    def test_folders_tree_shape_with_counts(self):
        """文件夹要带计数与 custom 标记，且顺序是「系统在前、自建在后」。"""
        self._add("<f1@x>", "收件箱来信", "REPLY", folder="INBOX")
        self._add("<f2@x>", "专利代理来信", "REPLY", folder="专利代理")
        res = self._call("GET", "/api/v2/folders")
        self.assertTrue(res["ok"])
        data = res["data"]
        paths = [f["path"] for f in data["folders"]]
        for p in ("INBOX", "Drafts", "Sent", "Junk", "Trash"):
            self.assertIn(p, paths, "系统文件夹 %s 必须始终出现（哪怕是 0）" % p)
        for p in ("专利代理", "未来电池中心"):
            self.assertIn(p, paths, "自建文件夹 %s 必须在侧边栏里" % p)
        # 系统在前
        self.assertLess(paths.index("Trash"), paths.index("专利代理"))

        by = {f["path"]: f for f in data["folders"]}
        self.assertTrue(by["专利代理"]["custom"], "自建文件夹必须打上 custom 标记")
        self.assertFalse(by["INBOX"]["custom"])
        self.assertEqual(by["专利代理"]["total"], 1, "计数要来自真实库")
        self.assertEqual(by["未来电池中心"]["total"], 0, "没同步过也要列出来，计数为 0")
        self.assertEqual(by["INBOX"]["unread"], 1)

    def test_index_count_comes_from_foxmail_rows(self):
        """「N 封可检索」= Foxmail 历史条数，且它不该作为文件夹出现在树里。"""
        from .test_v2 import mk_msg
        self.app.repo.upsert_message(
            mk_msg("<h1@x>", "历史邮件", "old@qq.com", folder="(Foxmail 历史)"))
        res = self._call("GET", "/api/v2/folders")
        data = res["data"]
        self.assertEqual(data["index_count"], 1)
        self.assertNotIn("(Foxmail 历史)", [f["path"] for f in data["folders"]],
                         "伪文件夹不该出现在文件夹树里（它是底部的「本地索引」）")
        self.assertGreaterEqual(data["total_messages"], 1)

    # ---------------- 按文件夹 / 标签浏览 ----------------
    def test_messages_filter_by_folder_and_classification(self):
        self._add("<m1@x>", "要回的", "REPLY", folder="INBOX")
        self._add("<m2@x>", "看看就行", "READ", folder="INBOX", unread=False)
        self._add("<m3@x>", "专利代理的", "REPLY", folder="专利代理")
        self._add("<m4@x>", "已发信", "REPLY", folder="Sent")

        r1 = self._call("GET", "/api/v2/messages", query={"folder": ["INBOX"]})
        self.assertEqual({m["subject"] for m in r1["data"]["messages"]},
                         {"要回的", "看看就行"})
        self.assertEqual(r1["data"]["total"], 2, "total 不能被 limit 截断")

        r2 = self._call("GET", "/api/v2/messages", query={"folder": ["专利代理"]})
        self.assertEqual([m["subject"] for m in r2["data"]["messages"]], ["专利代理的"])

        r3 = self._call("GET", "/api/v2/messages", query={"classification": ["READ"]})
        self.assertEqual([m["subject"] for m in r3["data"]["messages"]], ["看看就行"])

        r4 = self._call("GET", "/api/v2/messages",
                        query={"folder": ["INBOX"], "unread": ["1"]})
        self.assertEqual([m["subject"] for m in r4["data"]["messages"]], ["要回的"])

        # 列表与计数必须用同一套条件，否则「N 封」会对不上
        self.assertEqual(r1["data"]["total"], self.app.repo.count_messages(folder="INBOX"))
        self.assertEqual(r3["data"]["total"],
                         self.app.repo.count_messages(classification="READ"))

    def test_list_rows_carry_broadcast_flag(self):
        """列表行要带 broadcast —— 行上的「📢 群发通知」chip 靠它。"""
        from .test_v2 import mk_msg
        msg = mk_msg("<b1@x>", "群发通知", "official@example.edu", classification="READ")
        msg["is_broadcast"] = 1
        self.app.repo.upsert_message(msg)
        res = self._call("GET", "/api/v2/messages", query={"classification": ["READ"]})
        self.assertTrue(res["data"]["messages"][0]["broadcast"])

    # ---------------- 写邮件 ----------------
    def test_compose_draft_writes_to_drafts_and_never_sends(self):
        from mail_workbench.mail import smtp_client
        sink = []
        restore = self._stub_imap_append(sink)
        sent = []
        orig_send = smtp_client.send
        smtp_client.send = lambda *a, **k: sent.append(a) or {"ok": True}
        try:
            res = self._call("POST", "/api/v2/compose/draft",
                             payload={"to": "a@b.com", "subject": "草稿", "body": "正文"})
        finally:
            orig_send, smtp_client.send = smtp_client.send, orig_send
            restore()
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(sink), 1, "应当写入一次草稿箱")
        self.assertEqual(sink[0]["folder"], "Drafts")
        self.assertIn("Draft", sink[0]["flags"])
        self.assertEqual(sent, [], "存草稿绝不能触发发送")

    def test_compose_send_requires_explicit_confirm(self):
        """没有 confirm=SEND 时，服务端必须拒绝，且**不能**调用 SMTP。"""
        from mail_workbench.mail import smtp_client
        sent = []
        orig_send = smtp_client.send
        smtp_client.send = lambda *a, **k: sent.append(a) or {"ok": True}
        try:
            res = self._call("POST", "/api/v2/compose/send",
                             payload={"to": "a@b.com", "subject": "x", "body": "y"})
        finally:
            smtp_client.send = orig_send
        self.assertFalse(res.get("ok"), "缺 confirm 必须被拒")
        self.assertEqual(sent, [], "被拒时绝不能已经发出去")
        self.assertIn("confirm", res.get("error") or "")
        self.assertIn("AI NEVER SENDS", res.get("policy") or "")

    def test_compose_send_happy_path_appends_to_sent(self):
        from mail_workbench.mail import smtp_client
        sink = []
        restore = self._stub_imap_append(sink)
        calls = []
        orig_send = smtp_client.send

        def _send(cfg, raw, rcpts, dry_run=False):
            calls.append({"rcpts": list(rcpts), "raw": raw})
            return {"ok": True, "recipients": list(rcpts)}

        smtp_client.send = _send
        try:
            res = self._call("POST", "/api/v2/compose/send",
                             payload={"to": "a@b.com, c@d.com", "subject": "主题",
                                      "body": "正文", "confirm": "SEND"})
        finally:
            smtp_client.send = orig_send
            restore()
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["rcpts"], ["a@b.com", "c@d.com"])
        self.assertEqual(len(sink), 1, "要留一份副本")
        self.assertEqual(sink[0]["folder"], "Sent")
        self.assertTrue(res["data"]["appended_to_sent"])

    def test_compose_send_rejects_bad_recipient(self):
        res = self._call("POST", "/api/v2/compose/send",
                         payload={"to": "不是邮箱", "subject": "x", "body": "y",
                                  "confirm": "SEND"})
        self.assertFalse(res.get("ok"))
        self.assertIn("收件人", res.get("error") or "")

    def test_compose_draft_requires_body(self):
        res = self._call("POST", "/api/v2/compose/draft",
                         payload={"to": "a@b.com", "subject": "只有主题"})
        self.assertFalse(res.get("ok"))
        self.assertIn("正文", res.get("error") or "")

    # ---------------- 界面形状 ----------------
    def test_ui_has_select_all_and_to_top(self):
        """列表上方的「全选」与下方的「回到顶部」。

        这两样看起来只是按钮，但缺一样用户就得靠手点 200 行 ——
        所以把它们的**可测形式**钉住：全选控件（含三态）、
        回到顶部按钮（含滚动阈值），以及全选作用在「当前列表」而不是固定集合。
        """
        ui = os.path.join(PKG_ROOT, "ui", "index.html")
        with open(ui, "r", encoding="utf-8") as f:
            body = f.read()
        for needle in ("selAllChk", "toggleSelectAll", "全选", "取消全选",
                       "indeterminate", "toTop", "syncToTop", "回到顶部",
                       "TO_TOP_AFTER", "is-show", "scrollTo"):
            self.assertIn(needle, body, "侧边栏/列表缺少：%s" % needle)
        self.assertIn("S.items.map(x=>x.message_id)", body,
                      "全选必须作用在当前列表（S.items），不能硬编码某个集合")
        # 「回到顶部」的显隐要在列表重绘后重算，否则重绘一次按钮就不见了
        self.assertIn("syncToTop();   // 「回到顶部」的显隐跟着列表走",
                      body, "renderList 里没有重算「回到顶部」显隐")

    def test_ui_has_sidebar_sections(self):
        """侧边栏必须真的有「写邮件 / 文件夹 / 标签 / 本地索引」这几块。

        这是「符合过去的操作习惯」这件事的**可测形式** —— 免得以后
        有人重构时悄悄把它们删掉，而功能测试还全绿。
        """
        ui = os.path.join(PKG_ROOT, "ui", "index.html")
        with open(ui, "r", encoding="utf-8") as f:
            body = f.read()
        for needle in ("compose-btn", "写邮件", "data-folder", "data-tag",
                       "tree-group-title", "自建文件夹", "本地索引", "封可检索",
                       "class_tree", "tag-dot"):
            self.assertIn(needle, body, "侧边栏缺少：%s" % needle)
        # 写邮件必须走 V2 自己的带门禁端点，而不是随便一个发信接口
        self.assertIn('"/compose/send"', body)
        self.assertIn('confirm: kind==="send" ? "SEND" : ""', body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
