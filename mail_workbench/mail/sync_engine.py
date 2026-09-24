# -*- coding: utf-8 -*-
"""Mail Sync Engine —— IMAP IDLE 事件驱动 + 增量同步（P0-1）。

对比 V1 的做法：
    V1: 一个 WorkBuddy automation 每 5 分钟跑一次 `_fetch_body.py`，
        每次重新拉 INBOX 最新 50 封、重写整个 JSON、重新注入 HTML。
        -> 48 次/小时 × 24 = 每天上千次无意义的重拉，还会被删除的邮件反复刷出来。

    V2: 常驻进程 + IMAP IDLE。服务器有新邮件 -> 推送 -> **只拉新的那几封**。
        UI 走 SSE 实时更新，不再靠「重新生成静态 HTML」。

同步语义：
  * 按 UID 增量：只取未入库的 UID（真实 UID，不是 sequence number）。
  * 最新 N 封抓完整正文；更早的只抓头（省流量也省时间）。
  * UIDVALIDITY 变化 -> 该文件夹视为全新，重新索引。
  * 同步完立刻做**本地**处理：规则分类 -> 线程聚合 -> 候选检测。
    全过程不调用 LLM（P0-2）。
"""
from __future__ import annotations

import threading
import time

from .. import util
from ..intelligence import rule_engine
from . import imap_client as imc
from . import parser as mparser


class SyncEngine:
    def __init__(self, repo, cfg: dict, on_event=None, log=None):
        self.repo = repo
        self.cfg = cfg
        self.on_event = on_event
        self.log = log
        self.lock = threading.Lock()
        self.rules = rule_engine.load_rules(cfg)
        self.idle = None
        self._last_sync = {}
        self.stats_cache = {}

    # ------------------------------------------------------------------
    def _emit(self, event: dict):
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                pass

    # ------------------------------------------------------------------
    def sync_all(self, folders: list = None, limit: int = None, reason: str = "manual") -> dict:
        """同步全部配置文件夹（互斥，避免 IDLE 与手动刷新撞车）。"""
        if not self.lock.acquire(blocking=False):
            return {"ok": False, "reason": "already_syncing"}
        try:
            folders = folders or self.cfg.get("sync_folders") or ["INBOX"]
            out = {"reason": reason, "folders": {}, "new_messages": 0, "errors": []}
            self.repo.inc_metric("sync_runs")
            for f in folders:
                try:
                    res = self.sync_folder(f, limit=limit)
                    out["folders"][f] = res
                    out["new_messages"] += res.get("inserted", 0)
                except Exception as e:
                    out["errors"].append("%s: %s" % (f, e))
                    self.repo.inc_metric("sync_errors")
                    if self.log:
                        self.log("同步 %s 失败：%s" % (f, e))
            out["ok"] = not out["errors"]
            out["at"] = util.now_iso()
            self._post_sync(out)
            return out
        finally:
            self.lock.release()

    # ------------------------------------------------------------------
    def sync_folder(self, folder: str, limit: int = None) -> dict:
        cfg = self.cfg
        c = imc.ImapClient(cfg, log=self.log)
        res = {"folder": folder, "inserted": 0, "updated": 0, "unchanged": 0,
               "classified": 0, "flags_refreshed": 0, "uidvalidity": None}
        try:
            c.connect()
            sel = c.select(folder, readonly=True)
            res["uidvalidity"] = sel.get("uidvalidity")

            stored_uv = self.repo.kv_get("uidvalidity:%s" % folder)
            uv = sel.get("uidvalidity")
            full = False
            if uv and stored_uv and str(uv) != str(stored_uv):
                if self.log:
                    self.log("UIDVALIDITY 变化（%s -> %s），%s 将重新索引" % (stored_uv, uv, folder))
                full = True
            if uv:
                self.repo.kv_set("uidvalidity:%s" % folder, uv)

            known = self._known_uids(folder)
            all_uids = c.uid_search("ALL")
            if not all_uids:
                return res
            new_uids = [u for u in all_uids if u not in known]
            if full:
                new_uids = all_uids
            if limit is None:
                limit = int(cfg.get("sync_initial_limit", 300)) if not known else 0
            if limit and len(new_uids) > limit:
                new_uids = new_uids[-limit:]

            if new_uids:
                res.update(self._ingest(c, folder, new_uids))

            # flags 回扫：保证星标/已读/删除状态与服务器一致（V1 的 star sync）
            refresh_n = min(len(all_uids), 200)
            if refresh_n:
                res["flags_refreshed"] = self._refresh_flags(c, folder, all_uids[-refresh_n:])
            self._last_sync[folder] = util.now_iso()
            self.stats_cache[folder] = res
            return res
        finally:
            c.close()

    # ------------------------------------------------------------------
    def _known_uids(self, folder: str) -> set:
        rows = self.repo.db.query(
            "SELECT uid FROM messages WHERE account = ? AND folder = ? AND uid IS NOT NULL",
            (self.cfg.get("user") or "", folder))
        out = set()
        for r in rows:
            try:
                out.add(int(r["uid"]))
            except (TypeError, ValueError):
                continue
        return out

    def _ingest(self, client, folder: str, uids: list) -> dict:
        """抓取并入库一批 UID，然后本地分类 + 线程聚合 + 候选检测。"""
        cfg = self.cfg
        out = {"inserted": 0, "updated": 0, "unchanged": 0, "classified": 0,
               "attachments": 0, "threads": set()}
        body_limit = int(cfg.get("sync_body_limit", 60))
        body_uids = set(uids[-body_limit:]) if body_limit else set()

        heads = client.fetch_headers(uids)
        fulls = client.fetch_full(sorted(body_uids)) if body_uids else {}

        from ..thread import aggregator as agg
        from ..workflow.candidate_detector import CandidateDetector

        self_set = {a.lower() for a in (cfg.get("self_addresses") or [])}
        account = cfg.get("user") or ""
        detector = None

        for uid in sorted(uids):
            head = heads.get(uid)
            if not head:
                continue
            full = fulls.get(uid)
            if full and full.get("raw"):
                info = mparser.parse_message(full["raw"])
                flags = _flags_dict(full.get("flags") or [])
            else:
                info = head
                flags = _flags_dict(head.get("flags") or [])
                info.setdefault("body_text", "")
                info.setdefault("has_attachments", False)
                info.setdefault("attachment_names", [])
                info.setdefault("attachments", [])

            info = mparser.ensure_message_id(info, account, folder, uid)

            # 本地分类（仅对我方收到的邮件分类，已发送邮件不打"要回"标签）
            inbound = (info.get("from_addr") or "").lower() not in self_set
            classification = None
            is_bcast = False
            if inbound:   # 5) 分类
                res = rule_engine.classify_ex(info, self.rules)
                classification = res["classification"]
                is_bcast = res["broadcast"]
                out["classified"] += 1
                if is_bcast:
                    out["broadcast"] = out.get("broadcast", 0) + 1

            row = dict(info)
            row.update({
                "account": account,
                "folder": folder,
                "uid": uid,
                "uidvalidity": self.repo.kv_get("uidvalidity:%s" % folder),
                "unread": flags.get("unread", True),
                "answered": flags.get("answered", False),
                "flagged": flags.get("flagged", False),
                "deleted": flags.get("deleted", False),
                "classification": classification,
                "is_broadcast": is_bcast,
                "source": "imap",
                "priority": None,
            })
            attached = agg.attach_message(self.repo, row, cfg)
            out["threads"].add(attached["thread_id"])
            act = attached["action"]
            out[act] = out.get(act, 0) + 1

            # 附件元数据（规范 §22：本地先记 filename/type/size/hash）
            for a in info.get("attachments") or []:
                try:
                    self.repo.record_attachment({
                        "attachment_id": "%s:%s" % (a.get("sha256") or util.sha256_text(
                            info["message_id"] + a.get("filename", "")), a.get("filename")),
                        "message_id": info["message_id"],
                        "filename": a.get("filename"),
                        "content_type": a.get("content_type"),
                        "size_bytes": a.get("size_bytes"),
                        "sha256": a.get("sha256"),
                    })
                    out["attachments"] += 1
                except Exception:
                    pass

            if inbound and classification:
                if detector is None:
                    detector = CandidateDetector(self.repo, cfg, log=self.log)
                detector.consider(info["message_id"], trigger_source="sync")

        out["threads"] = len(out["threads"])
        return out

    def _refresh_flags(self, client, folder: str, uids: list) -> int:
        """把服务器 flags 拉回来更新本地（星标/已读/删除同步）。"""
        flags_map = client.fetch_flags(uids)
        n = 0
        for uid, f in flags_map.items():
            row = self.repo.find_by_uid(self.cfg.get("user") or "", folder, uid)
            if not row:
                continue
            if (bool(row.get("unread")) != bool(f.get("unread"))
                    or bool(row.get("answered")) != bool(f.get("answered"))
                    or bool(row.get("flagged")) != bool(f.get("flagged"))
                    or bool(row.get("deleted")) != bool(f.get("deleted"))):
                self.repo.set_flags(row["message_id"], unread=f.get("unread"),
                                    answered=f.get("answered"), flagged=f.get("flagged"),
                                    deleted=f.get("deleted"))
                n += 1
        return n

    # ------------------------------------------------------------------
    def _post_sync(self, result: dict):
        """同步后的收尾：关闭已回复线程的跟进、重算指标、广播事件。"""
        from ..workflow.followup_manager import FollowUpManager
        fm = FollowUpManager(self.repo, self.cfg, log=self.log)
        closed = 0
        # 对方已回信的线程 -> 关闭 follow-up（避免多余提醒）
        for r in self.repo.db.query("SELECT thread_id FROM followups WHERE status = 'scheduled'"):
            t = self.repo.get_thread(r["thread_id"])
            if t and t.get("waiting_for_me"):
                closed += fm.close_on_reply(r["thread_id"])
        if closed:
            self._emit({"type": "followups_closed", "count": closed})

        if result.get("new_messages"):
            try:
                self.repo.inc_metric("emails_received", result["new_messages"])
            except Exception:
                pass
        self._emit({"type": "sync", "result": {
            "reason": result.get("reason"), "new_messages": result.get("new_messages"),
            "ok": result.get("ok"), "at": result.get("at"),
            "errors": result.get("errors") or [],
        }})

    # ------------------------------------------------------------------
    # IMAP IDLE
    # ------------------------------------------------------------------
    def start_idle(self, folder: str = None):
        if not self.cfg.get("idle_enabled", True):
            if self.log:
                self.log("IMAP IDLE 已禁用（idle_enabled=false），依赖手动/定时同步")
            return None
        if self.idle:
            return self.idle
        folder = folder or self.cfg.get("folder_inbox", "INBOX")
        self.idle = imc.IdleWatcher(
            self.cfg, folder=folder,
            on_event=self._on_idle_event,
            on_state=lambda st: self._emit({"type": "idle_state", "state": st}),
            log=self.log)
        self.idle.start()
        return self.idle

    def stop_idle(self):
        if self.idle:
            self.idle.stop()
            self.idle = None

    def _on_idle_event(self, event: dict):
        if event.get("type") != "new_mail":
            return
        # 只同步 INBOX，且只取增量（这里 limit=None 时会自然走增量路径，
        # 因为 _known_uids 非空 -> limit 归零）
        try:
            res = self.sync_folder(event.get("folder") or self.cfg.get("folder_inbox", "INBOX"))
            self._emit({"type": "mail", "folder": res.get("folder"),
                        "new": res.get("inserted", 0), "at": util.now_iso()})
        except Exception as e:
            if self.log:
                self.log("IDLE 触发同步失败：%s" % e)

    def stop(self):
        self.stop_idle()

    # ------------------------------------------------------------------
    def health(self) -> dict:
        ts = self.repo.kv_get("foxmail_stats") or {}
        out = {
            "idle": self.idle.status() if self.idle else {"mode": "disabled", "running": False},
            "last_sync": dict(self._last_sync),
            "folders": {},
        }
        for f in (self.cfg.get("sync_folders") or ["INBOX"]):
            out["folders"][f] = {
                "messages": self.repo.count_messages(f),
                "last_sync": self._last_sync.get(f),
            }
        return out


def _flags_dict(flags) -> dict:
    flags = list(flags or [])
    return {
        "flags": flags,
        "unread": "\\Seen" not in flags,
        "answered": "\\Answered" in flags,
        "flagged": "\\Flagged" in flags,
        "deleted": "\\Deleted" in flags,
    }


def check_smtp_health(cfg: dict) -> dict:
    from . import smtp_client
    return smtp_client.verify_connection(cfg)
