# -*- coding: utf-8 -*-
"""V1 ↔ V2 桥接层。

这是「增量升级、不推倒重来」的关键粘合点。原则：

  1. **V1 的路由一行不改。** 桥接层只在 `server.py` 的 do_GET/do_POST 最前面
     拦下 `/api/v2/*` 与 `/workbench`，其余请求原样交给 V1。
     => V1 的 16 个 Playwright UI 测试、全部既有 API 天然不受影响。

  2. **旧 JSON 队列不删。** V1 的 `reply-queue/*.json` 通过 `migrate_reply_queue()`
     以 dual-read 方式导入 SQLite（幂等），文件原地保留，验证无误后再考虑废弃。

  3. **凭据共用。** V2 读 V1 的 `config.json` 与 `~/.workbuddy/secrets/mail.cred`，
     不引入第二套配置。

URL 映射（对外 /api/v2/x  → 内部 /api/x）：

    /api/v2/buckets              GET
    /api/v2/brief                GET
    /api/v2/health               GET
    /api/v2/metrics              GET
    /api/v2/draft-jobs/next      GET      ← WorkBuddy Worker 契约
    /api/v2/draft-jobs/{id}/plan POST
    /api/v2/drafts/{id}/approve  POST
    /api/v2/drafts/{id}/send     POST     ← 需要人类令牌 + confirm=SEND
    /api/v2/search               GET
    /api/v2/events               GET (SSE)
    /api/v2/reply-queue/migrate  POST     ← 把 V1 文件队列迁进 SQLite
    ...
"""
from __future__ import annotations

import json
import os
import threading
import time
from urllib.parse import unquote

from . import config as cfgmod
from . import util

V2_PREFIX = "/api/v2"
UI_ROUTE = "/workbench"

_app = None
_app_lock = threading.Lock()
_log = None


# --------------------------------------------------------------------------
# 日志
# --------------------------------------------------------------------------
def make_log(logs_dir: str):
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

    return log


# --------------------------------------------------------------------------
# 应用单例
# --------------------------------------------------------------------------
def get_app(autostart: bool = True):
    """懒加载 V2 App（含 SQLite、SyncEngine、Scheduler）。"""
    global _app, _log
    with _app_lock:
        if _app is not None:
            return _app
        cfg = cfgmod.load_config()
        cfgmod.ensure_dirs(cfg)
        _log = make_log(cfg["logs_dir"])
        from .server import App
        _app = App(cfg, log=_log)
        _log("V2 已在 V1 进程内就绪（db=%s）" % cfg["db_path"])
        if autostart:
            try:
                ok = bool(cfg.get("pass"))
                if ok:
                    _app.sync.start_idle()
                    _log("V2 IMAP IDLE 已启动（事件驱动，替代 automation 轮询）")
                else:
                    _log("未读到凭据，V2 暂不启动 IMAP IDLE（UI 仍可用）")
                _app.scheduler.start()
                _log("V2 本地 scheduler 已启动（snooze / followup / recovery / brief）")
            except Exception as e:
                _log("V2 后台线程启动失败：%s" % e)
        return _app


def set_app(app, log=None):
    """注入 App（测试 / 嵌入场景）。传 None 表示清空。"""
    global _app, _log
    with _app_lock:
        if app is None:
            _app = None
            return
        _app = app
        if log is not None:
            _log = log


def shutdown():
    global _app
    with _app_lock:
        if _app is not None:
            try:
                _app.stop()
            except Exception:
                pass
            _app = None


def app_status() -> dict:
    if _app is None:
        return {"running": False}
    try:
        return {
            "running": True,
            "db": _app.cfg.get("db_path"),
            "idle": (_app.sync.health().get("idle") or {}).get("mode"),
            "scheduler": _app.scheduler.status().get("running"),
            "sse_clients": _app.bus.subscriber_count(),
        }
    except Exception as e:
        return {"running": True, "error": str(e)}


# --------------------------------------------------------------------------
# 路径判定与内部路径映射
# --------------------------------------------------------------------------
def is_v2_api(path: str) -> bool:
    return path == V2_PREFIX or path.startswith(V2_PREFIX + "/")


def is_v2_ui(path: str) -> bool:
    return path in (UI_ROUTE, UI_ROUTE + "/", UI_ROUTE + ".html")


def to_internal(path: str) -> str:
    """ /api/v2/draft-jobs/next -> /api/draft-jobs/next """
    rest = unquote(path[len(V2_PREFIX):])
    if not rest or rest == "/":
        return "/api"
    return "/api" + rest


# --------------------------------------------------------------------------
# 请求适配器：把我的 _Request 接口套在 V1 的 Handler 上
# --------------------------------------------------------------------------
class _Req:
    def __init__(self, method, path, query, path_params, payload):
        self.method = method
        self.path = path
        self.query = query or {}
        self.path_params = path_params or {}
        self._json = payload if payload is not None else {}

    @property
    def json(self):
        return self._json


def dispatch(method: str, path: str, query: dict, payload: dict = None):
    """返回 (obj, http_code)；未命中 V2 路由返回 (None, 404)。"""
    # --- 桥接层自有的三个端点（V1 迁移 / 遗留队列只读 / V2 运行状态）---
    internal_early = to_internal(path)
    if method == "GET" and internal_early == "/api/legacy/reply-queue":
        return _wrap(lambda: legacy_queue_view()), 200
    if method == "POST" and internal_early == "/api/reply-queue/migrate":
        body = payload or {}
        return _wrap(lambda: migrate_reply_queue(dry_run=bool(body.get("dry_run")))), 200
    if method == "GET" and internal_early == "/api/app":
        return _wrap(lambda: app_status()), 200

    from .server import ROUTER
    internal = internal_early
    fn, params = ROUTER.match(method, internal)
    if not fn:
        return None, 404
    app = get_app()
    from . import server as mwserver
    mwserver.G = app           # 路由函数通过模块级 G 访问 App
    req = _Req(method, internal, query, params, payload)
    try:
        res = fn(req)
    except Exception as e:
        if _log:
            import traceback
            _log("V2 API 异常 %s %s: %s\n%s" % (method, internal, e, traceback.format_exc()))
        return {"ok": False, "error": str(e), "type": e.__class__.__name__}, 500
    if isinstance(res, tuple):
        return res[0], res[1]
    # 二进制响应（附件下载）直接透传：V1 的响应写出方认识 Raw 类型。
    from .server import Raw
    if isinstance(res, Raw):
        return res, 200
    code = 200
    if isinstance(res, dict) and res.get("ok") is False:
        try:
            code = int(res.get("code") or 400)
        except (TypeError, ValueError):
            code = 400
    return res, code


def _wrap(fn):
    """桥接层自有端点的统一信封：成功 {"ok":true,"data":...}，失败 {"ok":false,...}。

    必须统一 —— 前端 api() 用 `r.ok === false` 判断失败，
    裸 dict 会让成功响应被当成失败（或反之）而静默出错。
    """
    try:
        return {"ok": True, "data": fn()}
    except Exception as e:
        return {"ok": False, "error": str(e), "type": e.__class__.__name__}


# --------------------------------------------------------------------------
# SSE（复用 App 的事件总线）
# --------------------------------------------------------------------------
def sse(handler, query: dict):
    app = get_app()
    sub = app.bus.subscribe()
    try:
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream; charset=utf-8")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Accel-Buffering", "no")
        handler.end_headers()
        _sse_write(handler, {"type": "hello", "at": util.now_iso(),
                             "note": "V2 事件流已连接（IMAP IDLE / 队列 / snooze / followup）"})
        beat = time.time()
        while True:
            try:
                ev = sub.get(timeout=1.0)
                _sse_write(handler, ev)
            except Exception:
                if time.time() - beat > 20:
                    beat = time.time()
                    try:
                        handler.wfile.write(b": ping\n\n")
                        handler.wfile.flush()
                    except Exception:
                        break
    except Exception:
        pass
    finally:
        app.bus.unsubscribe(sub)


def _sse_write(handler, obj: dict):
    """带上 `event:` 名（V2 UI 用 addEventListener('<type>') 监听）。

    V1 的 SSE 只发 data:（浏览器按 'message' 事件派发）。
    V2 必须用命名事件，否则 mail/job/sync 这些监听收不到，
    表现是「界面开了但永远不自动刷新」。
    """
    name = (obj or {}).get("type") or "event"
    raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
    handler.wfile.write(b"event: " + name.encode("ascii", "replace") + b"\n"
                        b"data: " + raw + b"\n\n")
    handler.wfile.flush()


# --------------------------------------------------------------------------
# V1 文件队列 → SQLite 迁移（dual-read，幂等，不删旧文件）
# --------------------------------------------------------------------------
V1_STATUS_MAP = {
    "pending": "queued",
    "waiting": "queued",
    "queued": "queued",
    "generating": "generating",
    "ready": "ready",
    "done": "ready",
    "failed": "failed",
    "error": "failed",
    "expired": "expired",
    "cancelled": "dismissed",
    "consumed": "sent",
}


def migrate_reply_queue(dry_run: bool = False) -> dict:
    """把 V1 `reply-queue/{pending,done,expired}/*.json` 导入 SQLite draft_jobs。

    * 用原 rid 作为 job_id，重复执行幂等
    * 旧 JSON **原地保留**（规范 §36：不要一次性删除旧 JSON）
    * 无法识别的状态进 failed 并保留 last_error，便于人工核对
    """
    app = get_app(autostart=False)
    repo = app.repo
    root = os.path.join(app.cfg["project_root"], "reply-queue")
    if not os.path.isdir(root):
        return {"ok": True, "reason": "no_legacy_queue", "path": root,
                "scanned": 0, "imported": 0, "skipped": 0}

    scanned = imported = skipped = 0
    details = []
    for bucket in ("pending", "done", "expired"):
        d = os.path.join(root, bucket)
        if not os.path.isdir(d):
            continue
        for fn in sorted(os.listdir(d)):
            if not fn.endswith(".json"):
                continue
            scanned += 1
            p = os.path.join(d, fn)
            try:
                with open(p, "r", encoding="utf-8") as f:
                    obj = json.load(f)
            except Exception as e:
                skipped += 1
                details.append({"file": fn, "reason": "unreadable: %s" % e})
                continue

            rid = obj.get("id") or obj.get("rid") or os.path.splitext(fn)[0]
            if repo.get_job(rid):
                skipped += 1
                details.append({"file": fn, "reason": "already_imported"})
                continue

            raw_status = (obj.get("status") or bucket or "").lower()
            status = V1_STATUS_MAP.get(raw_status, "failed")
            draft = obj.get("draft") or obj.get("draft_text") or ""
            created = obj.get("created_at") or obj.get("created") or util.now_iso()
            if not repo.get_message(obj.get("message_id") or ""):
                # 旧队列里的 message_id 可能不在 V2 store 里（还没同步到），
                # 仍然导入：任务本身是有价值的，等同步到那封邮件后自然接上。
                pass
            if dry_run:
                imported += 1
                continue
            try:
                repo.insert_job({
                    "job_id": rid,
                    "message_id": obj.get("message_id") or obj.get("uid") or "",
                    "thread_id": obj.get("thread_id"),
                    "account": obj.get("account") or app.cfg.get("user") or "",
                    "folder": obj.get("folder") or "",
                    "sender": obj.get("sender") or obj.get("from_addr") or "",
                    "recipients": obj.get("recipients") or obj.get("to") or [],
                    "subject": obj.get("subject") or "",
                    "created_at": created,
                    "priority": int(obj.get("priority") or 50),
                    "trigger_source": obj.get("source") or "v1_migrated",
                    "draft_mode": obj.get("draft_mode") or "normal",
                    "status": status,
                    "retry_count": int(obj.get("attempt") or obj.get("retry_count") or 0),
                    "last_error": obj.get("error") or ("V1 状态 %r 无法映射" % raw_status
                                                       if status == "failed" else None),
                    "draft_text": draft,
                    "original_draft_text": draft,
                    "idempotency_key": "draft:%s:%s" % (app.cfg.get("user"),
                                                        obj.get("message_id") or rid),
                    "context_snapshot": {"migrated_from": "v1_reply_queue",
                                         "v1_status": raw_status,
                                         "v1_file": os.path.join(bucket, fn)},
                })
                repo.log_job_event(rid, None, status, actor="migration",
                                   note="从 V1 reply-queue/%s/%s 导入" % (bucket, fn))
                imported += 1
            except Exception as e:
                skipped += 1
                details.append({"file": fn, "reason": str(e)})

    app.repo.inc_metric("v1_queue_migrated", imported)
    out = {"ok": True, "path": root, "scanned": scanned, "imported": imported,
           "skipped": skipped, "dry_run": dry_run,
           "note": "旧 JSON 文件已原地保留，未删除（dual-read）。",
           "details": details[:50]}
    if _log:
        _log("V1 队列迁移：扫描 %d，导入 %d，跳过 %d" % (scanned, imported, skipped))
    return out


def legacy_queue_view() -> dict:
    """只读查看 V1 文件队列（用于 V2 UI 的「历史遗留」区块与核对）。"""
    app = get_app(autostart=False)
    root = os.path.join(app.cfg["project_root"], "reply-queue")
    out = {"path": root, "buckets": {}}
    for bucket in ("pending", "done", "expired"):
        d = os.path.join(root, bucket)
        names = []
        if os.path.isdir(d):
            names = [n for n in sorted(os.listdir(d)) if n.endswith(".json")]
        out["buckets"][bucket] = {"count": len(names), "files": names[:50]}
    return out


# --------------------------------------------------------------------------
# V2 UI 页面注入：让同一份 ui/index.html 同时支持两种部署
# --------------------------------------------------------------------------
def render_ui(ui_path: str) -> bytes:
    """读取 V2 的 ui/index.html，并注入 API base（本部署挂在 /api/v2 下）。

    这里**只**注入 API base，不再往顶栏塞「← 经典视图」入口 ——
    用户明确要求去掉它（顶栏只留工作台自己的操作：简报 / 健康 / 同步）。

    V1 经典视图本身还在（`/` 与 `/classic` 都仍可访问），只是不再从
    V2 界面上露出来 —— 需要时直接敲 URL 就能到，不影响 V1 的可用性。
    """
    with open(ui_path, "r", encoding="utf-8") as f:
        html = f.read()
    inject = "<script>window.MW_API_BASE='/api/v2';</script>\n"
    if "<head>" in html:
        html = html.replace("<head>", "<head>\n" + inject, 1)
    else:
        html = inject + html
    return html.encode("utf-8")
