# -*- coding: utf-8 -*-
"""SQLite 存储：WAL 模式 + versioned migration。

设计说明（对应 V2 规范 §36）：
  * 所有零散 JSON 状态逐步收敛到 SQLite；V1 的 `_raw_inbox.json` /
    `sjtu_data.json` / `mail_state.json` **不删除**，由 migrations 做 dual-read 导入。
  * schema 变更一律走 `MIGRATIONS` 列表，靠 `PRAGMA user_version` 版本化，
    绝不做破坏性原地改表。
  * 并发模型：每线程一个连接（thread-local）+ 写锁串行化 + `busy_timeout`。
    本应用是单用户本地工作站应用，不需要连接池或消息中间件（§34 不过度设计）。
"""
from __future__ import annotations

import contextlib
import os
import sqlite3
import threading
import time

SCHEMA_VERSION = 4

_MIGRATION_1 = [
    """
    CREATE TABLE IF NOT EXISTS kv (
        k           TEXT PRIMARY KEY,
        v           TEXT,
        updated_at  TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS messages (
        message_id       TEXT PRIMARY KEY,
        account          TEXT NOT NULL DEFAULT '',
        folder           TEXT NOT NULL DEFAULT '',
        uid              TEXT,
        uidvalidity      TEXT,
        seq              INTEGER,
        thread_id        TEXT,
        subject          TEXT DEFAULT '',
        subject_norm     TEXT DEFAULT '',
        from_addr        TEXT DEFAULT '',
        from_name        TEXT DEFAULT '',
        to_addrs         TEXT DEFAULT '[]',
        cc_addrs         TEXT DEFAULT '[]',
        date_iso         TEXT,
        internal_ts      REAL DEFAULT 0,
        unread           INTEGER DEFAULT 0,
        answered         INTEGER DEFAULT 0,
        flagged          INTEGER DEFAULT 0,
        deleted          INTEGER DEFAULT 0,
        has_attachments  INTEGER DEFAULT 0,
        attachment_names TEXT DEFAULT '[]',
        body_text        TEXT DEFAULT '',
        snippet          TEXT DEFAULT '',
        search_blob      TEXT DEFAULT '',
        size_bytes       INTEGER DEFAULT 0,
        classification   TEXT,
        workflow_state   TEXT DEFAULT 'NEW',
        workflow_changed_at TEXT,
        snooze_until     TEXT,
        priority         INTEGER DEFAULT 50,
        source           TEXT DEFAULT 'imap',
        in_reply_to      TEXT DEFAULT '',
        references_ids   TEXT DEFAULT '[]',
        ingested_at      TEXT,
        indexed_at       TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_messages_thread  ON messages(thread_id)",
    "CREATE INDEX IF NOT EXISTS idx_messages_ts      ON messages(internal_ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_messages_folder  ON messages(account, folder, internal_ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_messages_uid     ON messages(account, folder, uid)",
    "CREATE INDEX IF NOT EXISTS idx_messages_wf      ON messages(workflow_state)",
    "CREATE INDEX IF NOT EXISTS idx_messages_cls     ON messages(classification)",
    "CREATE INDEX IF NOT EXISTS idx_messages_from    ON messages(from_addr)",
    """
    CREATE TABLE IF NOT EXISTS threads (
        thread_id         TEXT PRIMARY KEY,
        account           TEXT DEFAULT '',
        subject           TEXT DEFAULT '',
        subject_norm      TEXT DEFAULT '',
        participants      TEXT DEFAULT '[]',
        message_count     INTEGER DEFAULT 0,
        latest_message_id TEXT,
        latest_ts         REAL DEFAULT 0,
        last_inbound_ts   REAL DEFAULT 0,
        last_outbound_ts  REAL DEFAULT 0,
        needs_reply       INTEGER DEFAULT 0,
        waiting_for_me    INTEGER DEFAULT 0,
        waiting_for_other INTEGER DEFAULT 0,
        classification    TEXT,
        workflow_state    TEXT DEFAULT 'NEW',
        workflow_changed_at TEXT,
        priority          INTEGER DEFAULT 50,
        has_attachments   INTEGER DEFAULT 0,
        updated_at        TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_threads_latest ON threads(latest_ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_threads_wf     ON threads(workflow_state)",
    """
    CREATE TABLE IF NOT EXISTS candidates (
        candidate_id  TEXT PRIMARY KEY,
        message_id    TEXT,
        thread_id     TEXT,
        account       TEXT DEFAULT '',
        folder        TEXT DEFAULT '',
        sender        TEXT DEFAULT '',
        recipients    TEXT DEFAULT '[]',
        subject       TEXT DEFAULT '',
        priority      INTEGER DEFAULT 50,
        trigger_source TEXT DEFAULT 'rule',
        reason        TEXT DEFAULT '',
        status        TEXT DEFAULT 'new',
        created_at    TEXT,
        updated_at    TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_cand_status ON candidates(status, priority DESC, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_cand_thread ON candidates(thread_id)",
    """
    CREATE TABLE IF NOT EXISTS draft_jobs (
        job_id                TEXT PRIMARY KEY,
        message_id            TEXT,
        thread_id             TEXT,
        account               TEXT DEFAULT '',
        folder                TEXT DEFAULT '',
        sender                TEXT DEFAULT '',
        recipients            TEXT DEFAULT '[]',
        subject               TEXT DEFAULT '',
        created_at            TEXT,
        updated_at            TEXT,
        priority              INTEGER DEFAULT 50,
        trigger_source        TEXT DEFAULT 'manual',
        draft_mode            TEXT DEFAULT 'normal',
        status                TEXT DEFAULT 'queued',
        retry_count           INTEGER DEFAULT 0,
        last_error            TEXT,
        draft_text            TEXT,
        original_draft_text   TEXT,
        edit_ratio            REAL,
        context_snapshot      TEXT,
        context_hash          TEXT,
        plan_json             TEXT,
        missing_info_json     TEXT,
        user_input_json       TEXT,
        revision_of           TEXT,
        revision_instruction  TEXT,
        idempotency_key       TEXT,
        claimed_by            TEXT,
        claim_time            TEXT,
        lease_until           TEXT,
        approve_token         TEXT,
        approve_token_expires TEXT,
        approved_at           TEXT,
        sent_at               TEXT,
        expire_at             TEXT,
        next_attempt_at       TEXT,
        variant               INTEGER DEFAULT 0,
        sent_message_id       TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_jobs_status ON draft_jobs(status, priority DESC, created_at)",
    "CREATE INDEX IF NOT EXISTS idx_jobs_thread ON draft_jobs(thread_id)",
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ux_jobs_idem ON draft_jobs(idempotency_key)
    WHERE status IN ('queued','generating','needs_input','ready','reviewing','approved')
    """,
    """
    CREATE TABLE IF NOT EXISTS job_events (
        id          INTEGER PRIMARY KEY AUTOINCREMENT,
        job_id      TEXT,
        ts          TEXT,
        from_status TEXT,
        to_status   TEXT,
        actor       TEXT,
        note        TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_job_events ON job_events(job_id, id)",
    """
    CREATE TABLE IF NOT EXISTS snoozes (
        snooze_id  TEXT PRIMARY KEY,
        message_id TEXT,
        thread_id  TEXT,
        wake_at    TEXT,
        created_at TEXT,
        status     TEXT DEFAULT 'active',
        note       TEXT,
        woke_at    TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_snooze_wake ON snoozes(status, wake_at)",
    """
    CREATE TABLE IF NOT EXISTS followups (
        followup_id       TEXT PRIMARY KEY,
        thread_id         TEXT,
        message_id        TEXT,
        sent_message_id   TEXT,
        created_at        TEXT,
        due_at            TEXT,
        status            TEXT DEFAULT 'scheduled',
        note              TEXT,
        closed_at         TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_followup_due ON followups(status, due_at)",
    """
    CREATE TABLE IF NOT EXISTS contacts (
        email              TEXT PRIMARY KEY,
        name               TEXT DEFAULT '',
        organization       TEXT DEFAULT '',
        relationship       TEXT DEFAULT '',
        preferred_language TEXT DEFAULT '',
        last_contact_at    TEXT,
        first_seen_at      TEXT,
        message_count      INTEGER DEFAULT 0,
        notes              TEXT DEFAULT '',
        updated_at         TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS attachments (
        attachment_id TEXT PRIMARY KEY,
        message_id    TEXT,
        filename      TEXT DEFAULT '',
        content_type  TEXT DEFAULT '',
        size_bytes    INTEGER DEFAULT 0,
        sha256        TEXT DEFAULT '',
        summary_json  TEXT,
        analyzed_at   TEXT,
        created_at    TEXT
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_att_msg  ON attachments(message_id)",
    "CREATE INDEX IF NOT EXISTS idx_att_hash ON attachments(sha256)",
    """
    CREATE TABLE IF NOT EXISTS metrics (
        name       TEXT PRIMARY KEY,
        value      REAL DEFAULT 0,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS metric_events (
        id    INTEGER PRIMARY KEY AUTOINCREMENT,
        name  TEXT,
        ts    TEXT,
        value REAL DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_metric_name ON metric_events(name, id)",
    """
    CREATE TABLE IF NOT EXISTS draft_memory (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        kind       TEXT,
        pattern    TEXT,
        weight     REAL DEFAULT 1.0,
        updated_at TEXT
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS undo_log (
        id     INTEGER PRIMARY KEY AUTOINCREMENT,
        ts     TEXT,
        actor  TEXT,
        action TEXT,
        payload TEXT,
        undone INTEGER DEFAULT 0
    )
    """,
    "CREATE INDEX IF NOT EXISTS idx_undo_live ON undo_log(undone, id)",
]

MIGRATIONS = {1: _MIGRATION_1}

# --------------------------------------------------------------------------
# v2：为 Foxmail 历史邮件的「按主题回填线程」加索引。
#
# 背景：历史邮件（6696 封）接入线程的正确时机不是导入时 ——
# 导入时若为每个历史主题都建线程，会凭空多出几千个只有一封旧邮件的线程。
# 正确做法是「新邮件到达时，把同主题的历史邮件拉进这个线程」，
# 这样历史邮件只作为上下文出现（规范 §7），工作桶也不会被旧邮件淹没。
# 这条回填按 subject_norm 查找，量级是万级，必须有索引。
# --------------------------------------------------------------------------
_MIGRATION_2 = [
    "CREATE INDEX IF NOT EXISTS idx_messages_subject_norm ON messages(subject_norm)",
    "CREATE INDEX IF NOT EXISTS idx_messages_source ON messages(source)",
]

MIGRATIONS[2] = _MIGRATION_2

# --------------------------------------------------------------------------
# v3：threads 的列表查询对每行都要跑 5 个相关子查询（取最新一封的主题/发件人/
# 摘要/未读），这些子查询全都按 thread_id 过滤。没有索引时等于对 1 万行的
# messages 反复全表扫。补上索引后 bucket 计算仍稳定在几十毫秒。
# --------------------------------------------------------------------------
_MIGRATION_3 = [
    "CREATE INDEX IF NOT EXISTS idx_messages_thread ON messages(thread_id, internal_ts DESC)",
    "CREATE INDEX IF NOT EXISTS idx_messages_source_thread ON messages(source, thread_id)",
]

MIGRATIONS[3] = _MIGRATION_3

# --------------------------------------------------------------------------
# v4：把「群发通知」这个判定结果落库，并补存分类用到的邮件头。
#
# 为什么需要 is_broadcast 独立列，而不是在算工作桶时现推：
#   1. 现推要拿规则文件对每封邮件重跑主题匹配 —— 首页会被拖慢，且和
#      sync 时的分类结果可能不一致（两处算同一件事，迟早分叉）。
#   2. 工作桶需要它来做「不算待我处理」的过滤，这是 SQL 层的事。
# 判定完全由本地规则产出（rule_engine），确定性、可解释、可重算。
#
# 同时补存 list_unsubscribe / auto_submitted / precedence：
# 这三项是规则引擎的输入（V2 新增的确定性机器信号）。不存的话
# 「改完规则重算分类」就无法复现同步时的判定，会静默漂移 ——
# 重算两次得到不同结果，是最难查的那种 bug。
# --------------------------------------------------------------------------
_MIGRATION_4 = [
    "ALTER TABLE messages ADD COLUMN is_broadcast INTEGER DEFAULT 0",
    "ALTER TABLE messages ADD COLUMN list_unsubscribe INTEGER DEFAULT 0",
    "ALTER TABLE messages ADD COLUMN auto_submitted INTEGER DEFAULT 0",
    "ALTER TABLE messages ADD COLUMN precedence TEXT DEFAULT ''",
    "CREATE INDEX IF NOT EXISTS idx_messages_broadcast ON messages(is_broadcast, classification)",
]

MIGRATIONS[4] = _MIGRATION_4


class Database:
    """线程安全的 SQLite 封装（WAL + 写锁 + 版本化迁移）。"""

    def __init__(self, path: str, timeout: float = 20.0):
        self.path = path
        self.timeout = timeout
        self._local = threading.local()
        self._write_lock = threading.RLock()
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)

    # ---------------- connection ----------------
    def conn(self) -> sqlite3.Connection:
        c = getattr(self._local, "conn", None)
        if c is None:
            c = sqlite3.connect(self.path, timeout=self.timeout, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA synchronous=NORMAL")
            c.execute("PRAGMA busy_timeout=%d" % int(self.timeout * 1000))
            c.execute("PRAGMA foreign_keys=ON")
            c.execute("PRAGMA temp_store=MEMORY")
            self._local.conn = c
        return c

    def close(self) -> None:
        c = getattr(self._local, "conn", None)
        if c is not None:
            try:
                c.close()
            except Exception:
                pass
            self._local.conn = None

    # ---------------- migrations ----------------
    def version(self) -> int:
        return int(self.conn().execute("PRAGMA user_version").fetchone()[0])

    def migrate(self, target: int = SCHEMA_VERSION, log=None) -> int:
        """幂等地把 schema 升到 target 版本，返回最终版本号。"""
        with self._write_lock:
            c = self.conn()
            cur = int(c.execute("PRAGMA user_version").fetchone()[0])
            if cur > target:
                raise RuntimeError("数据库版本 %d 高于本程序支持的 %d，拒绝降级" % (cur, target))
            for v in range(cur + 1, target + 1):
                stmts = MIGRATIONS.get(v)
                if stmts is None:
                    raise RuntimeError("缺少 migration v%d" % v)
                for sql in stmts:
                    c.execute(sql)
                c.execute("PRAGMA user_version=%d" % v)
                c.commit()
                if log:
                    log("migrated schema -> v%d (%d statements)" % (v, len(stmts)))
            return int(c.execute("PRAGMA user_version").fetchone()[0])

    # ---------------- query helpers ----------------
    def execute(self, sql: str, params=()):
        with self._write_lock:
            c = self.conn()
            cur = c.execute(sql, params)
            c.commit()
            return cur

    def executemany(self, sql: str, seq):
        with self._write_lock:
            c = self.conn()
            cur = c.executemany(sql, seq)
            c.commit()
            return cur

    def query(self, sql: str, params=()) -> list:
        cur = self.conn().execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def query_one(self, sql: str, params=()):
        cur = self.conn().execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None

    def scalar(self, sql: str, params=(), default=None):
        row = self.conn().execute(sql, params).fetchone()
        if not row:
            return default
        return row[0]

    @contextlib.contextmanager
    def tx(self):
        """显式事务。用于 lease/claim 等必须原子化的多步操作。"""
        with self._write_lock:
            c = self.conn()
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.commit()
            except Exception:
                try:
                    c.rollback()
                except Exception:
                    pass
                raise

    def insert(self, table: str, data: dict, replace: bool = False) -> None:
        cols = list(data.keys())
        verb = "INSERT OR REPLACE" if replace else "INSERT"
        sql = "%s INTO %s (%s) VALUES (%s)" % (
            verb, table, ", ".join(cols), ", ".join("?" for _ in cols)
        )
        self.execute(sql, [data[k] for k in cols])

    def update(self, table: str, key_col: str, key_val, data: dict) -> None:
        if not data:
            return
        cols = list(data.keys())
        sql = "UPDATE %s SET %s WHERE %s = ?" % (
            table, ", ".join("%s = ?" % c for c in cols), key_col
        )
        self.execute(sql, [data[k] for k in cols] + [key_val])

    # ---------------- maintenance ----------------
    def checkpoint(self) -> None:
        try:
            self.conn().execute("PRAGMA wal_checkpoint(TRUNCATE)")
        except Exception:
            pass

    def heal(self, log=None) -> list:
        """启动自愈：WAL 一致性检查 + 僵尸租约清理所需的索引重建。"""
        notes = []
        try:
            res = self.conn().execute("PRAGMA quick_check").fetchone()
            if res and str(res[0]).lower() != "ok":
                notes.append("quick_check: %s" % res[0])
        except Exception as e:
            notes.append("quick_check failed: %s" % e)
        for t in ("messages", "threads", "candidates", "draft_jobs", "snoozes",
                  "followups", "attachments", "contacts"):
            try:
                self.conn().execute("ANALYZE %s" % t)
            except Exception:
                pass
        if log:
            for n in notes:
                log(n)
        return notes

    def stats(self) -> dict:
        out = {}
        for t in ("messages", "threads", "candidates", "draft_jobs", "snoozes",
                  "followups", "contacts", "attachments"):
            try:
                out[t] = self.scalar("SELECT COUNT(*) FROM %s" % t, default=0)
            except Exception:
                out[t] = None
        try:
            out["db_bytes"] = os.path.getsize(self.path)
        except OSError:
            out["db_bytes"] = 0
        out["schema_version"] = self.version()
        return out


def open_db(path: str, log=None) -> Database:
    db = Database(path)
    db.migrate(log=log)
    return db


def default_db(log=None) -> Database:
    from .. import config as _cfg
    cfg = _cfg.load_config()
    _cfg.ensure_dirs(cfg)
    return open_db(cfg["db_path"], log=log)


_shared = None
_shared_lock = threading.Lock()


def shared_db(log=None) -> Database:
    """进程内共享连接管理器（供 server / scheduler / sync 复用同一份 WAL）。"""
    global _shared
    with _shared_lock:
        if _shared is None:
            _shared = default_db(log=log)
        return _shared


def _selftest() -> int:
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        p = os.path.join(d, "t.db")
        db = open_db(p, log=print)
        print("schema_version:", db.version())
        db.insert("kv", {"k": "a", "v": "1", "updated_at": "now"})
        print("kv:", db.query_one("SELECT * FROM kv WHERE k=?", ("a",)))
        t0 = time.time()
        db.migrate()
        print("idempotent migrate:", round((time.time() - t0) * 1000, 2), "ms")
        print("stats:", db.stats())
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(_selftest())
