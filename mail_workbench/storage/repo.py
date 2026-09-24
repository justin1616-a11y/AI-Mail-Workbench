# -*- coding: utf-8 -*-
"""数据访问层（Repo）。

只做「SQL ↔ dict」的机械映射与少量聚合，**不含业务规则**：
  业务规则在 intelligence/（分类）、workflow/（状态与候选）、draft/（队列）里。
这样做的原因：状态机的合法性校验必须只有一处实现，否则早晚会漂移。
"""
from __future__ import annotations

import json
import sqlite3
from typing import Iterable, Optional

from .. import util
from ..constants import (
    CLASS_DISMISS, CLASS_PRIORITY, JOB_ACTIVE_STATUSES, JOB_LABEL, JOB_STATUSES,
    WF_DONE, WF_NEW, WF_SNOOZED, WF_WAITING_FOR_ME, WF_WAITING_FOR_OTHER,
)


def _j(value, default="[]") -> str:
    """把 list/dict 安全序列化成 JSON 字符串。"""
    if value is None:
        return default
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False)
    except Exception:
        return default


def _loads(value, default=None):
    if value in (None, ""):
        return default if default is not None else []
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default if default is not None else []


# --------------------------------------------------------------------------
# 「线程里最新一封」的**规范排序**。凡是需要挑「最新一封」的地方都必须用它。
#
# 为什么需要专门定义一个：
#   Foxmail 历史记录会作为上下文被回填进线程（规范 §7），而它与 IMAP 里的
#   同一封邮件**时间戳完全相同**。只按 internal_ts 排，同分时取到哪一条是不确定的，
#   于是：
#     * 线程的 waiting_for_me / workflow_state / classification 会随取到哪条而漂移
#       —— 历史记录的状态是 ARCHIVED，取到它就会把这封真实邮件从工作桶里挤掉；
#     * 列表显示的发件人/摘要可能来自没有正文的历史存根；
#     * 「群发通知」之类的标记判断失效。
#   实测本机 2742 个多封线程里，有 2569 个就这样取错了。
#
# 排序意图：时间新的优先 >> 同一时刻 IMAP 真实邮件优先 >> 更大的优先（更可能是原文）
#          >> message_id 兜底，保证完全确定（可复现，不随查询计划变化）。
# --------------------------------------------------------------------------
LATEST_ORDER = ("t.internal_ts DESC, (t.source = 'imap') DESC, "
                "COALESCE(t.size_bytes, 0) DESC, t.message_id DESC")

# 同一排序的升序版（用于「按时间正序浏览整个线程」）
OLDEST_FIRST_ORDER = ("t.internal_ts ASC, (t.source = 'imap') ASC, "
                      "COALESCE(t.size_bytes, 0) ASC, t.message_id ASC")


def build_search_blob(msg: dict) -> str:
    """构造全文检索用的合并串。

    只取正文前 4000 字符：既覆盖绝大多数正文，又把 LIKE 扫描量压住
    （实测 5000 封信件的 common search < 40ms，满足 §38 的 200ms 目标）。
    """
    parts = [
        msg.get("subject") or "",
        msg.get("from_name") or "",
        msg.get("from_addr") or "",
        msg.get("to_addrs") or "",
        msg.get("snippet") or "",
        (msg.get("body_text") or "")[:4000],
    ]
    return util.collapse(" ".join(str(p) for p in parts)).lower()


class Repo:
    def __init__(self, db):
        if db is None:
            raise ValueError("Repo 需要 Database 实例")
        self.db = db

    # ==================================================================
    # kv
    # ==================================================================
    def kv_get(self, key: str, default=None):
        row = self.db.query_one("SELECT v FROM kv WHERE k = ?", (key,))
        if not row:
            return default
        try:
            return json.loads(row["v"])
        except Exception:
            return row["v"]

    def kv_set(self, key: str, value) -> None:
        self.db.insert("kv", {"k": key, "v": json.dumps(value, ensure_ascii=False),
                              "updated_at": util.now_iso()}, replace=True)

    # ==================================================================
    # messages
    # ==================================================================
    def upsert_message(self, msg: dict) -> str:
        """写入/更新一封邮件。返回 'inserted' | 'updated' | 'unchanged'。

        message_id 是主键；若上游拿不到 RFC Message-ID，必须先用
        util 合成稳定键（见 mail/sync_engine.py:synth_message_id）。
        """
        mid = msg.get("message_id")
        if not mid:
            raise ValueError("upsert_message 需要 message_id")

        row = {
            "message_id": mid,
            "account": msg.get("account") or "",
            "folder": msg.get("folder") or "",
            "uid": str(msg.get("uid")) if msg.get("uid") is not None else None,
            "uidvalidity": str(msg.get("uidvalidity")) if msg.get("uidvalidity") else None,
            "seq": msg.get("seq"),
            "thread_id": msg.get("thread_id"),
            "subject": util.collapse(msg.get("subject")),
            "subject_norm": util.normalize_subject(msg.get("subject")),
            "from_addr": (msg.get("from_addr") or "").lower(),
            "from_name": util.collapse(msg.get("from_name")),
            "to_addrs": _j(msg.get("to_addrs") or []),
            "cc_addrs": _j(msg.get("cc_addrs") or []),
            "date_iso": msg.get("date_iso") or "",
            "internal_ts": float(msg.get("internal_ts") or util.to_epoch(msg.get("date_iso"))),
            "unread": 1 if msg.get("unread") else 0,
            "answered": 1 if msg.get("answered") else 0,
            "flagged": 1 if msg.get("flagged") else 0,
            "deleted": 1 if msg.get("deleted") else 0,
            "has_attachments": 1 if msg.get("has_attachments") else 0,
            "attachment_names": _j(msg.get("attachment_names") or []),
            "body_text": util.meta_clean(msg.get("body_text") or ""),
            "snippet": util.collapse((msg.get("body_text") or "")[:300]),
            "size_bytes": int(msg.get("size_bytes") or 0),
            "classification": msg.get("classification"),
            # 「群发通知」判定：由本地规则产出，决定它算不算「待我处理」。
            # 与 classification 一起写入，保证两处看到的是同一个判定结果。
            "is_broadcast": 1 if msg.get("is_broadcast") else 0,
            # 分类用到的邮件头（补存以便「改规则后重算」能复现判定）
            "list_unsubscribe": 1 if msg.get("list_unsubscribe") else 0,
            "auto_submitted": 1 if msg.get("auto_submitted") else 0,
            "precedence": (msg.get("precedence") or "").lower(),
            "workflow_state": msg.get("workflow_state") or WF_NEW,
            "priority": int(msg.get("priority") or CLASS_PRIORITY.get(msg.get("classification"), 50)),
            "source": msg.get("source") or "imap",
            "in_reply_to": msg.get("in_reply_to") or "",
            "references_ids": _j(msg.get("references_ids") or []),
            "ingested_at": util.now_iso(),
            "indexed_at": util.now_iso(),
        }
        row["search_blob"] = build_search_blob(row)

        existing = self.db.query_one(
            "SELECT message_id, body_text, unread, answered, flagged, deleted, "
            "       classification, workflow_state, thread_id, uid, folder "
            "FROM messages WHERE message_id = ?", (mid,))
        if not existing:
            self.db.insert("messages", row)
            return "inserted"

        # 已存在：只覆盖「服务器侧会变」的字段，保留本地工作流状态与人工分类。
        patch = {}
        for k in ("folder", "uid", "uidvalidity", "seq", "subject", "subject_norm",
                  "from_addr", "from_name", "to_addrs", "cc_addrs", "date_iso",
                  "internal_ts", "has_attachments", "attachment_names", "size_bytes",
                  "in_reply_to", "references_ids", "search_blob", "unread",
                  "answered", "flagged", "deleted"):
            patch[k] = row[k]
        if not existing.get("body_text") and row["body_text"]:
            patch["body_text"] = row["body_text"]
            patch["snippet"] = row["snippet"]
        elif row["body_text"] and len(row["body_text"]) > len(existing.get("body_text") or ""):
            patch["body_text"] = row["body_text"]
            patch["snippet"] = row["snippet"]
        if not existing.get("thread_id") and row["thread_id"]:
            patch["thread_id"] = row["thread_id"]
        if not existing.get("classification") and row["classification"]:
            patch["classification"] = row["classification"]
            patch["priority"] = row["priority"]
            patch["is_broadcast"] = row["is_broadcast"]
        patch["indexed_at"] = row["indexed_at"]

        changed = any(
            str(existing.get(k)) != str(patch.get(k))
            for k in patch if k in existing
        )
        self.db.update("messages", "message_id", mid, patch)
        return "updated" if changed else "unchanged"

    def get_message(self, message_id: str) -> Optional[dict]:
        return self.db.query_one("SELECT * FROM messages WHERE message_id = ?", (message_id,))

    def bulk_upsert_messages(self, msgs: list) -> int:
        """批量写入（首次全量导入 / Foxmail 历史导入的性能路径）。

        语义：已存在的 message_id 直接跳过（用 INSERT OR IGNORE），
        因此不会覆盖本地已积累的工作流状态与人工分类。
        """
        if not msgs:
            return 0
        cols = ["message_id", "account", "folder", "uid", "uidvalidity", "seq", "thread_id",
                "subject", "subject_norm", "from_addr", "from_name", "to_addrs", "cc_addrs",
                "date_iso", "internal_ts", "unread", "answered", "flagged", "deleted",
                "has_attachments", "attachment_names", "body_text", "snippet", "search_blob",
                "size_bytes", "classification", "workflow_state", "priority", "source",
                "in_reply_to", "references_ids", "ingested_at", "indexed_at"]
        now = util.now_iso()
        rows = []
        for m in msgs:
            rows.append([
                m.get("message_id"), m.get("account") or "", m.get("folder") or "",
                str(m["uid"]) if m.get("uid") is not None else None,
                str(m["uidvalidity"]) if m.get("uidvalidity") else None, m.get("seq"),
                m.get("thread_id"), util.collapse(m.get("subject")),
                util.normalize_subject(m.get("subject")),
                (m.get("from_addr") or "").lower(), util.collapse(m.get("from_name")),
                _j(m.get("to_addrs") or []), _j(m.get("cc_addrs") or []),
                m.get("date_iso") or "",
                float(m.get("internal_ts") or util.to_epoch(m.get("date_iso"))),
                1 if m.get("unread") else 0, 1 if m.get("answered") else 0,
                1 if m.get("flagged") else 0, 1 if m.get("deleted") else 0,
                1 if m.get("has_attachments") else 0, _j(m.get("attachment_names") or []),
                util.meta_clean(m.get("body_text") or ""),
                util.collapse((m.get("body_text") or "")[:300]),
                build_search_blob(m), int(m.get("size_bytes") or 0),
                m.get("classification"),
                m.get("workflow_state") or WF_NEW,
                int(m.get("priority") or CLASS_PRIORITY.get(m.get("classification"), 50)),
                m.get("source") or "imap", m.get("in_reply_to") or "",
                _j(m.get("references_ids") or []), now, now,
            ])
        before = int(self.db.scalar("SELECT COUNT(*) FROM messages", default=0) or 0)
        self.db.executemany(
            "INSERT OR IGNORE INTO messages (%s) VALUES (%s)"
            % (", ".join(cols), ", ".join("?" for _ in cols)), rows)
        after = int(self.db.scalar("SELECT COUNT(*) FROM messages", default=0) or 0)
        return after - before

    def bulk_upsert_threads(self, threads: list) -> int:
        if not threads:
            return 0
        cols = ["thread_id", "account", "subject", "subject_norm", "participants",
                "message_count", "latest_message_id", "latest_ts", "last_inbound_ts",
                "last_outbound_ts", "needs_reply", "waiting_for_me", "waiting_for_other",
                "classification", "workflow_state", "workflow_changed_at", "priority",
                "has_attachments", "updated_at"]
        now = util.now_iso()
        rows = [[
            t.get("thread_id"), t.get("account") or "", util.collapse(t.get("subject")),
            t.get("subject_norm") or util.normalize_subject(t.get("subject")),
            _j(t.get("participants") or []), int(t.get("message_count") or 0),
            t.get("latest_message_id"), float(t.get("latest_ts") or 0),
            float(t.get("last_inbound_ts") or 0), float(t.get("last_outbound_ts") or 0),
            1 if t.get("needs_reply") else 0, 1 if t.get("waiting_for_me") else 0,
            1 if t.get("waiting_for_other") else 0, t.get("classification"),
            t.get("workflow_state") or WF_NEW, t.get("workflow_changed_at") or now,
            int(t.get("priority") or 50), 1 if t.get("has_attachments") else 0, now,
        ] for t in threads]
        self.db.executemany(
            "INSERT OR REPLACE INTO threads (%s) VALUES (%s)"
            % (", ".join(cols), ", ".join("?" for _ in cols)), rows)
        return len(rows)

    def find_by_uid(self, account: str, folder: str, uid) -> Optional[dict]:
        return self.db.query_one(
            "SELECT * FROM messages WHERE account=? AND folder=? AND uid=?",
            (account, folder, str(uid)))

    def list_messages(self, folder: str = None, limit: int = 100, offset: int = 0,
                      unread_only: bool = False, include_deleted: bool = False,
                      classification=None, source: str = None,
                      flagged_only: bool = False) -> list:
        """按文件夹 / 分类 / 来源列邮件（侧边栏的「档案视图」）。

        classification 支持单值或列表 —— 侧边栏点一个标签是单值，
        以后若要做「★重点 + ✉要回的」组合视图也不用改接口。
        """
        sql, params = self._message_filter_sql(
            folder=folder, unread_only=unread_only, include_deleted=include_deleted,
            classification=classification, source=source, flagged_only=flagged_only)
        sql += " ORDER BY internal_ts DESC LIMIT ? OFFSET ?"
        params += [int(limit), int(offset)]
        return self.db.query(sql, params)

    @staticmethod
    def _message_filter_sql(folder=None, unread_only=False, include_deleted=False,
                            classification=None, source=None, flagged_only=False):
        """把筛选条件集中到一处 —— 列表与计数必须用同一套条件，否则「N 封」会对不上。"""
        where, params = [], []
        if folder:
            where.append("folder = ?")
            params.append(folder)
        if classification:
            if isinstance(classification, (list, tuple, set)):
                vals = [c for c in classification if c]
                if vals:
                    where.append("classification IN (%s)" % ",".join("?" * len(vals)))
                    params += vals
            else:
                where.append("classification = ?")
                params.append(classification)
        if source:
            where.append("source = ?")
            params.append(source)
        if unread_only:
            where.append("unread = 1")
        if flagged_only:
            where.append("flagged = 1")
        if not include_deleted:
            where.append("deleted = 0")
        sql = "SELECT * FROM messages"
        if where:
            sql += " WHERE " + " AND ".join(where)
        return sql, params

    def messages_for_thread(self, thread_id: str, limit: int = 50) -> list:
        return self.db.query(
            "SELECT * FROM messages t WHERE thread_id = ? "
            "ORDER BY " + OLDEST_FIRST_ORDER + " LIMIT ?",
            (thread_id, int(limit)))

    def latest_in_thread(self, thread_id: str) -> Optional[dict]:
        return self.db.query_one(
            "SELECT * FROM messages t WHERE thread_id = ? "
            "ORDER BY " + LATEST_ORDER + " LIMIT 1",
            (thread_id,))

    def set_flags(self, message_id: str, **flags) -> None:
        allowed = ("unread", "answered", "flagged", "deleted")
        data = {k: (1 if v else 0) for k, v in flags.items() if k in allowed}
        if data:
            self.db.update("messages", "message_id", message_id, data)

    def set_workflow_state(self, message_id: str, state: str, **extra) -> None:
        data = {"workflow_state": state, "workflow_changed_at": util.now_iso()}
        data.update(extra)
        self.db.update("messages", "message_id", message_id, data)

    def set_classification(self, message_id: str, classification: str,
                           is_broadcast: bool = None) -> None:
        """写入分类。is_broadcast 为 None 时不动该列（人工改分类不该顺手改群发标记）。"""
        patch = {
            "classification": classification,
            "priority": CLASS_PRIORITY.get(classification, 50),
        }
        if is_broadcast is not None:
            patch["is_broadcast"] = 1 if is_broadcast else 0
        self.db.update("messages", "message_id", message_id, patch)

    def count_messages(self, folder: str = None, classification=None,
                       source: str = None) -> int:
        """计数。与 list_messages 共用同一套筛选条件（见 _message_filter_sql）。"""
        if not folder and not classification and not source:
            return int(self.db.scalar("SELECT COUNT(*) FROM messages WHERE deleted=0", default=0))
        sql, params = self._message_filter_sql(folder=folder, classification=classification,
                                               source=source)
        return int(self.db.scalar(sql.replace("SELECT *", "SELECT COUNT(*)"), params, 0))

    def folder_counts(self) -> list:
        return self.db.query(
            "SELECT folder, COUNT(*) AS total, "
            "       SUM(CASE WHEN unread=1 THEN 1 ELSE 0 END) AS unread "
            "FROM messages WHERE deleted=0 GROUP BY folder ORDER BY folder")

    def new_message_ids_since(self, since_iso: str = None) -> list:
        if since_iso:
            return [r["message_id"] for r in self.db.query(
                "SELECT message_id FROM messages WHERE ingested_at > ? ORDER BY ingested_at",
                (since_iso,))]
        return [r["message_id"] for r in self.db.query(
            "SELECT message_id FROM messages ORDER BY ingested_at DESC LIMIT 100")]

    # ==================================================================
    # threads
    # ==================================================================
    def upsert_thread(self, thread: dict) -> None:
        tid = thread.get("thread_id")
        if not tid:
            raise ValueError("upsert_thread 需要 thread_id")
        row = {
            "thread_id": tid,
            "account": thread.get("account") or "",
            "subject": util.collapse(thread.get("subject")),
            "subject_norm": thread.get("subject_norm") or util.normalize_subject(thread.get("subject")),
            "participants": _j(thread.get("participants") or []),
            "message_count": int(thread.get("message_count") or 0),
            "latest_message_id": thread.get("latest_message_id"),
            "latest_ts": float(thread.get("latest_ts") or 0),
            "last_inbound_ts": float(thread.get("last_inbound_ts") or 0),
            "last_outbound_ts": float(thread.get("last_outbound_ts") or 0),
            "needs_reply": 1 if thread.get("needs_reply") else 0,
            "waiting_for_me": 1 if thread.get("waiting_for_me") else 0,
            "waiting_for_other": 1 if thread.get("waiting_for_other") else 0,
            "classification": thread.get("classification"),
            "workflow_state": thread.get("workflow_state") or WF_NEW,
            "workflow_changed_at": thread.get("workflow_changed_at") or util.now_iso(),
            "priority": int(thread.get("priority") or 50),
            "has_attachments": 1 if thread.get("has_attachments") else 0,
            "updated_at": util.now_iso(),
        }
        self.db.insert("threads", row, replace=True)

    def get_thread(self, thread_id: str) -> Optional[dict]:
        row = self.db.query_one("SELECT * FROM threads WHERE thread_id = ?", (thread_id,))
        if row:
            row["participants"] = _loads(row.get("participants"), [])
        return row

    def list_threads(self, limit: int = 200, offset: int = 0, workflow_state: str = None) -> list:
        """列出线程。limit<=0 表示不限制。

        注意两点：
        1. 「计数」和「列表」必须分开 —— 用带 LIMIT 的结果去 len() 统计，
           得到的只是切片大小，不是真实数量（首页计数会因此失真）。
        2. 六个「最新一封」子查询必须用**同一套规范排序**（见 LATEST_ORDER）。
           否则列表里显示的标题、发件人、未读、群发标记可能分别来自不同的信，
           看起来像数据错乱 —— 那正是同分不定序的典型症状。
        """
        cols = ("subject", "from_name", "from_addr", "snippet", "unread", "source",
                "size_bytes", "message_id")
        sub = "".join(
            " (SELECT %s FROM messages t WHERE t.thread_id=t0.thread_id "
            "  ORDER BY %s LIMIT 1) AS last_%s, " % (c, LATEST_ORDER, c)
            for c in cols)
        sub += (" (SELECT COALESCE(is_broadcast,0) FROM messages t "
                "  WHERE t.thread_id=t0.thread_id ORDER BY %s LIMIT 1) AS last_broadcast "
                % LATEST_ORDER)
        sql = "SELECT t0.*, " + sub + "FROM threads t0"
        params = []
        if workflow_state:
            sql += " WHERE t0.workflow_state = ?"
            params.append(workflow_state)
        sql += " ORDER BY t0.priority DESC, t0.latest_ts DESC"
        if limit and int(limit) > 0:
            sql += " LIMIT ? OFFSET ?"
            params += [int(limit), int(offset)]
        rows = self.db.query(sql, params)
        for r in rows:
            r["participants"] = _loads(r.get("participants"), [])
        return rows

    def set_thread_workflow(self, thread_id: str, state: str) -> None:
        self.db.update("threads", "thread_id", thread_id, {
            "workflow_state": state,
            "workflow_changed_at": util.now_iso(),
            "updated_at": util.now_iso(),
        })

    def thread_ids(self) -> list:
        return [r["thread_id"] for r in self.db.query("SELECT thread_id FROM threads")]

    # ==================================================================
    # candidates
    # ==================================================================
    def create_candidate(self, cand: dict) -> tuple:
        """创建候选。返回 (candidate, created_bool)。

        幂等：同一 message_id 若已有 status='new' 的候选，直接返回既有那条。
        这让「同步反复跑」不会制造重复候选。
        """
        mid = cand.get("message_id")
        existing = self.db.query_one(
            "SELECT * FROM candidates WHERE message_id = ? AND status = 'new'", (mid,))
        if existing:
            return existing, False
        row = {
            "candidate_id": cand.get("candidate_id") or util.new_id("cand"),
            "message_id": mid,
            "thread_id": cand.get("thread_id"),
            "account": cand.get("account") or "",
            "folder": cand.get("folder") or "",
            "sender": cand.get("sender") or "",
            "recipients": _j(cand.get("recipients") or []),
            "subject": util.collapse(cand.get("subject")),
            "priority": int(cand.get("priority") or 50),
            "trigger_source": cand.get("trigger_source") or "rule",
            "reason": cand.get("reason") or "",
            "status": "new",
            "created_at": util.now_iso(),
            "updated_at": util.now_iso(),
        }
        try:
            self.db.insert("candidates", row)
        except sqlite3.IntegrityError:
            again = self.db.query_one(
                "SELECT * FROM candidates WHERE message_id = ? AND status = 'new'", (mid,))
            return (again or row), False
        return row, True

    def get_candidate(self, candidate_id: str) -> Optional[dict]:
        return self.db.query_one("SELECT * FROM candidates WHERE candidate_id = ?", (candidate_id,))

    def get_candidate_by_message(self, message_id: str) -> Optional[dict]:
        return self.db.query_one(
            "SELECT * FROM candidates WHERE message_id = ? AND status = 'new'", (message_id,))

    def list_candidates(self, status: str = "new", limit: int = 100) -> list:
        if status:
            return self.db.query(
                "SELECT * FROM candidates WHERE status = ? "
                "ORDER BY priority DESC, created_at DESC LIMIT ?", (status, int(limit)))
        return self.db.query(
            "SELECT * FROM candidates ORDER BY created_at DESC LIMIT ?", (int(limit),))

    def set_candidate_status(self, candidate_id: str, status: str) -> None:
        self.db.update("candidates", "candidate_id", candidate_id,
                       {"status": status, "updated_at": util.now_iso()})

    # ==================================================================
    # draft_jobs
    # ==================================================================
    def insert_job(self, job: dict) -> None:
        data = dict(job)
        data.setdefault("status", "queued")
        data.setdefault("created_at", util.now_iso())
        data["updated_at"] = util.now_iso()
        for k in ("recipients", "context_snapshot", "missing_info_json", "user_input_json"):
            if k in data and not isinstance(data[k], str):
                data[k] = _j(data[k])
        self.db.insert("draft_jobs", data)

    def get_job(self, job_id: str) -> Optional[dict]:
        row = self.db.query_one("SELECT * FROM draft_jobs WHERE job_id = ?", (job_id,))
        return self._hydrate_job(row)

    def get_active_job_by_idem(self, key: str) -> Optional[dict]:
        if not key:
            return None
        marks = ",".join("?" for _ in JOB_ACTIVE_STATUSES)
        row = self.db.query_one(
            "SELECT * FROM draft_jobs WHERE idempotency_key = ? AND status IN (%s) "
            "ORDER BY created_at DESC LIMIT 1" % marks, (key,) + tuple(JOB_ACTIVE_STATUSES))
        return self._hydrate_job(row)

    def find_job_by_message(self, message_id: str, active_only: bool = True) -> Optional[dict]:
        if active_only:
            marks = ",".join("?" for _ in JOB_ACTIVE_STATUSES)
            row = self.db.query_one(
                "SELECT * FROM draft_jobs WHERE message_id = ? AND status IN (%s) "
                "ORDER BY created_at DESC LIMIT 1" % marks, (message_id,) + tuple(JOB_ACTIVE_STATUSES))
        else:
            row = self.db.query_one(
                "SELECT * FROM draft_jobs WHERE message_id = ? ORDER BY created_at DESC LIMIT 1",
                (message_id,))
        return self._hydrate_job(row)

    def list_jobs(self, statuses: Iterable[str] = None, limit: int = 100,
                  message_id: str = None, thread_id: str = None) -> list:
        where, params = [], []
        if statuses:
            marks = ",".join("?" for _ in statuses)
            where.append("status IN (%s)" % marks)
            params.extend(list(statuses))
        if message_id:
            where.append("message_id = ?")
            params.append(message_id)
        if thread_id:
            where.append("thread_id = ?")
            params.append(thread_id)
        sql = "SELECT * FROM draft_jobs"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY priority DESC, created_at DESC LIMIT ?"
        params.append(int(limit))
        return [self._hydrate_job(r) for r in self.db.query(sql, params)]

    def _hydrate_job(self, row):
        if not row:
            return None
        for k in ("recipients", "context_snapshot", "missing_info_json", "user_input_json", "plan_json"):
            row[k] = _loads(row.get(k), None if k == "plan_json" else ([] if k != "context_snapshot" else None))
        return row

    def update_job(self, job_id: str, patch: dict) -> None:
        data = dict(patch)
        for k in ("recipients", "context_snapshot", "missing_info_json", "user_input_json", "plan_json"):
            if k in data and data[k] is not None and not isinstance(data[k], str):
                data[k] = _j(data[k])
        data["updated_at"] = util.now_iso()
        self.db.update("draft_jobs", "job_id", job_id, data)

    def log_job_event(self, job_id: str, from_status, to_status, actor: str = "system",
                      note: str = "") -> None:
        self.db.insert("job_events", {
            "job_id": job_id, "ts": util.now_iso(), "from_status": from_status,
            "to_status": to_status, "actor": actor, "note": note,
        })

    def job_events(self, job_id: str) -> list:
        return self.db.query(
            "SELECT * FROM job_events WHERE job_id = ? ORDER BY id ASC", (job_id,))

    def reconcile_job_statuses(self, mapper) -> int:
        """对历史脏状态做一次归一（Recovery / 迁移用）。"""
        n = 0
        for row in self.db.query("SELECT job_id, status FROM draft_jobs"):
            fixed = mapper(row["status"])
            if fixed and fixed != row["status"]:
                self.update_job(row["job_id"], {"status": fixed, "last_error": "status reconciled"})
                n += 1
        return n

    def job_counts(self) -> dict:
        rows = self.db.query("SELECT status, COUNT(*) AS n FROM draft_jobs GROUP BY status")
        return {r["status"]: r["n"] for r in rows}

    # ==================================================================
    # snoozes / followups
    # ==================================================================
    def create_snooze(self, snooze: dict) -> dict:
        row = {
            "snooze_id": snooze.get("snooze_id") or util.new_id("snz"),
            "message_id": snooze.get("message_id"),
            "thread_id": snooze.get("thread_id"),
            "wake_at": snooze["wake_at"],
            "created_at": util.now_iso(),
            "status": "active",
            "note": snooze.get("note") or "",
        }
        self.db.insert("snoozes", row)
        if row["message_id"]:
            self.db.update("messages", "message_id", row["message_id"],
                           {"snooze_until": row["wake_at"]})
        return row

    def cancel_snoozes_for(self, message_id: str = None, thread_id: str = None) -> int:
        where, params = ["status = 'active'"], []
        if message_id:
            where.append("message_id = ?")
            params.append(message_id)
        if thread_id:
            where.append("thread_id = ?")
            params.append(thread_id)
        cur = self.db.execute(
            "UPDATE snoozes SET status='cancelled' WHERE " + " AND ".join(where), params)
        if message_id:
            self.db.update("messages", "message_id", message_id, {"snooze_until": None})
        return cur.rowcount or 0

    def due_snoozes(self, now_iso: str = None, limit: int = 200) -> list:
        return self.db.query(
            "SELECT * FROM snoozes WHERE status='active' AND wake_at <= ? "
            "ORDER BY wake_at ASC LIMIT ?", (now_iso or util.now_iso(), int(limit)))

    def active_snoozes(self, limit: int = 200) -> list:
        return self.db.query(
            "SELECT * FROM snoozes WHERE status='active' ORDER BY wake_at ASC LIMIT ?", (int(limit),))

    def mark_snooze_woken(self, snooze_id: str) -> None:
        self.db.update("snoozes", "snooze_id", snooze_id,
                       {"status": "woken", "woke_at": util.now_iso()})

    def create_followup(self, fu: dict) -> dict:
        row = {
            "followup_id": fu.get("followup_id") or util.new_id("fu"),
            "thread_id": fu.get("thread_id"),
            "message_id": fu.get("message_id"),
            "sent_message_id": fu.get("sent_message_id"),
            "created_at": util.now_iso(),
            "due_at": fu["due_at"],
            "status": "scheduled",
            "note": fu.get("note") or "",
        }
        self.db.insert("followups", row)
        return row

    def due_followups(self, now_iso: str = None, limit: int = 200) -> list:
        return self.db.query(
            "SELECT * FROM followups WHERE status='scheduled' AND due_at <= ? "
            "ORDER BY due_at ASC LIMIT ?", (now_iso or util.now_iso(), int(limit)))

    def list_followups(self, status: str = None, limit: int = 200) -> list:
        if status:
            return self.db.query(
                "SELECT * FROM followups WHERE status=? ORDER BY due_at ASC LIMIT ?",
                (status, int(limit)))
        return self.db.query("SELECT * FROM followups ORDER BY due_at ASC LIMIT ?", (int(limit),))

    def close_followups_for_thread(self, thread_id: str, status: str = "replied") -> int:
        cur = self.db.execute(
            "UPDATE followups SET status=?, closed_at=? WHERE thread_id=? AND status IN "
            "('scheduled','due')", (status, util.now_iso(), thread_id))
        return cur.rowcount or 0

    def mark_followup(self, followup_id: str, status: str) -> None:
        self.db.update("followups", "followup_id", followup_id,
                       {"status": status, "closed_at": util.now_iso()})

    # ==================================================================
    # contacts / attachments
    # ==================================================================
    def touch_contact(self, email: str, name: str = None, when_iso: str = None,
                      org: str = None) -> None:
        if not email:
            return
        email = email.lower()
        row = self.db.query_one("SELECT * FROM contacts WHERE email = ?", (email,))
        now = when_iso or util.now_iso()
        if not row:
            self.db.insert("contacts", {
                "email": email, "name": name or "", "organization": org or "",
                "relationship": "", "preferred_language": "",
                "last_contact_at": now, "first_seen_at": now, "message_count": 1,
                "notes": "", "updated_at": util.now_iso(),
            })
            return
        patch = {"message_count": int(row.get("message_count") or 0) + 1,
                 "updated_at": util.now_iso()}
        if now and (not row.get("last_contact_at") or str(now) > str(row["last_contact_at"])):
            patch["last_contact_at"] = now
        if name and not row.get("name"):
            patch["name"] = name
        self.db.update("contacts", "email", email, patch)

    def get_contact(self, email: str) -> Optional[dict]:
        return self.db.query_one("SELECT * FROM contacts WHERE email = ?", ((email or "").lower(),))

    def set_contact(self, email: str, **fields) -> None:
        allowed = ("name", "organization", "relationship", "preferred_language", "notes")
        data = {k: v for k, v in fields.items() if k in allowed}
        if not data:
            return
        data["updated_at"] = util.now_iso()
        if self.get_contact(email):
            self.db.update("contacts", "email", (email or "").lower(), data)
        else:
            data["email"] = (email or "").lower()
            data.setdefault("first_seen_at", util.now_iso())
            data.setdefault("message_count", 0)
            self.db.insert("contacts", data)

    def list_contacts(self, limit: int = 500) -> list:
        return self.db.query(
            "SELECT * FROM contacts ORDER BY message_count DESC, last_contact_at DESC LIMIT ?",
            (int(limit),))

    def record_attachment(self, att: dict) -> None:
        aid = att.get("attachment_id")
        if not aid:
            raise ValueError("record_attachment 需要 attachment_id")
        if self.db.query_one("SELECT attachment_id FROM attachments WHERE attachment_id = ?", (aid,)):
            self.db.update("attachments", "attachment_id", aid, {
                "message_id": att.get("message_id"),
                "size_bytes": int(att.get("size_bytes") or 0),
            })
            return
        self.db.insert("attachments", {
            "attachment_id": aid,
            "message_id": att.get("message_id"),
            "filename": att.get("filename") or "",
            "content_type": att.get("content_type") or "",
            "size_bytes": int(att.get("size_bytes") or 0),
            "sha256": att.get("sha256") or "",
            "summary_json": None,
            "analyzed_at": None,
            "created_at": util.now_iso(),
        })

    def get_attachment_summary(self, sha256: str) -> Optional[dict]:
        if not sha256:
            return None
        row = self.db.query_one(
            "SELECT * FROM attachments WHERE sha256 = ? AND analyzed_at IS NOT NULL LIMIT 1",
            (sha256,))
        if row and row.get("summary_json"):
            return _loads(row["summary_json"], None)
        return None

    def set_attachment_summary(self, sha256: str, filename: str, summary: dict) -> None:
        row = self.db.query_one("SELECT attachment_id FROM attachments WHERE sha256 = ? LIMIT 1",
                                (sha256,))
        if row:
            self.db.update("attachments", "attachment_id", row["attachment_id"], {
                "summary_json": _j(summary), "analyzed_at": util.now_iso(),
                "filename": filename or "",
            })

    def attachments_for_message(self, message_id: str) -> list:
        return self.db.query(
            "SELECT * FROM attachments WHERE message_id = ? ORDER BY filename", (message_id,))

    def get_attachment(self, attachment_id: str) -> Optional[dict]:
        if not attachment_id:
            return None
        return self.db.query_one(
            "SELECT * FROM attachments WHERE attachment_id = ?", (attachment_id,))

    # ==================================================================
    # metrics
    # ==================================================================
    def inc_metric(self, name: str, delta: float = 1.0) -> float:
        cur = self.db.execute(
            "INSERT INTO metrics (name, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = value + excluded.value, "
            "updated_at = excluded.updated_at",
            (name, float(delta), util.now_iso()))
        return float(self.db.scalar("SELECT value FROM metrics WHERE name = ?", (name,), 0.0))

    def set_metric(self, name: str, value: float) -> None:
        self.db.execute(
            "INSERT INTO metrics (name, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(name) DO UPDATE SET value = excluded.value, updated_at = excluded.updated_at",
            (name, float(value), util.now_iso()))

    def all_metrics(self) -> dict:
        return {r["name"]: r["value"] for r in self.db.query("SELECT name, value FROM metrics")}

    def record_metric_event(self, name: str, value: float) -> None:
        self.db.insert("metric_events", {"name": name, "ts": util.now_iso(), "value": float(value)})

    def metric_average(self, name: str, limit: int = 200) -> float:
        row = self.db.query_one(
            "SELECT AVG(value) AS a, COUNT(*) AS n FROM ("
            " SELECT value FROM metric_events WHERE name=? ORDER BY id DESC LIMIT ?)",
            (name, int(limit)))
        if not row or not row.get("n"):
            return 0.0
        return float(row["a"] or 0.0)

    def metric_series(self, name: str, limit: int = 200) -> list:
        return self.db.query(
            "SELECT ts, value FROM metric_events WHERE name=? ORDER BY id DESC LIMIT ?",
            (name, int(limit)))

    # ==================================================================
    # draft memory（可关闭，规范 §24）
    # ==================================================================
    def add_draft_memory(self, kind: str, pattern: str, weight: float = 1.0) -> None:
        exist = self.db.query_one(
            "SELECT id, weight FROM draft_memory WHERE kind=? AND pattern=?", (kind, pattern))
        if exist:
            self.db.update("draft_memory", "id", exist["id"],
                           {"weight": float(exist["weight"]) + weight, "updated_at": util.now_iso()})
        else:
            self.db.insert("draft_memory", {"kind": kind, "pattern": pattern,
                                            "weight": weight, "updated_at": util.now_iso()})

    def draft_memory(self, limit: int = 50) -> list:
        return self.db.query(
            "SELECT * FROM draft_memory ORDER BY weight DESC LIMIT ?", (int(limit),))

    def clear_draft_memory(self) -> None:
        self.db.execute("DELETE FROM draft_memory")

    # ==================================================================
    # undo（删除/归档可撤销）
    # ==================================================================
    def push_undo(self, action: str, payload: dict, actor: str = "user") -> int:
        cur = self.db.execute(
            "INSERT INTO undo_log (ts, actor, action, payload, undone) VALUES (?,?,?,?,0)",
            (util.now_iso(), actor, action, _j(payload)))
        return int(cur.lastrowid or 0)

    def last_undo(self) -> Optional[dict]:
        row = self.db.query_one(
            "SELECT * FROM undo_log WHERE undone = 0 ORDER BY id DESC LIMIT 1")
        if row:
            row["payload"] = _loads(row.get("payload"), {})
        return row

    def mark_undone(self, undo_id: int) -> None:
        self.db.execute("UPDATE undo_log SET undone = 1 WHERE id = ?", (int(undo_id),))

    # ==================================================================
    # 汇总
    # ==================================================================
    def counts_summary(self) -> dict:
        return {
            "messages": self.count_messages(),
            "folders": self.folder_counts(),
            "jobs": self.job_counts(),
            "candidates_new": int(self.db.scalar(
                "SELECT COUNT(*) FROM candidates WHERE status='new'", default=0)),
            "snoozes_active": int(self.db.scalar(
                "SELECT COUNT(*) FROM snoozes WHERE status='active'", default=0)),
            "followups_open": int(self.db.scalar(
                "SELECT COUNT(*) FROM followups WHERE status IN ('scheduled','due')", default=0)),
            "threads": int(self.db.scalar("SELECT COUNT(*) FROM threads", default=0)),
        }

    @staticmethod
    def job_label(status: str) -> str:
        return JOB_LABEL.get(status, status)

    @staticmethod
    def valid_job_status(status: str) -> bool:
        return status in JOB_STATUSES

    # ==================================================================
    # 存储层维护透传（业务层只拿得到 Repo，不该为了 checkpoint 去摸 db）
    # ==================================================================
    def stats(self) -> dict:
        return self.db.stats()

    def checkpoint(self) -> None:
        self.db.checkpoint()

    def migrate(self, target=None) -> int:
        return self.db.migrate() if target is None else self.db.migrate(target=target)
