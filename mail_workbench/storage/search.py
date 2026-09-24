# -*- coding: utf-8 -*-
"""搜索查询解析（规范 §21）。

支持操作符：
    sender:      发件人（姓名或地址片段）
    recipient:   收件人片段
    subject:     主题片段
    after:       某日之后（含当天，YYYY-MM-DD）
    before:      某日之前（不含当天，YYYY-MM-DD）
    has:         附件（has:attachment / has:att）
    status:      工作流状态（new/waiting/waiting_for_me/waiting_for_other/
                 snoozed/done/ignored/archived）
    label:       分类（important/reply/read/system/dismiss，兼容 vip/act/dump）
    thread:      只看某线程
    folder:      限定文件夹
    其余裸词      全文匹配（主题 + 发件人 + 收件人 + 正文前若干字符）

设计取舍：V1 的 `search` 只能按单一关键词过滤本地索引（仅元数据）。
V2 把「IMAP 当前邮件 + Foxmail 历史邮件」统一到同一个 `messages` 表，
因此**结果天然按 thread 聚合**，且可以组合操作符。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta

from . import database as _db  # noqa: F401  (仅为类型提示与包内聚)

from ..constants import (
    CLASS_IMPORTANT, CLASS_REPLY, CLASS_READ, CLASS_SYSTEM, CLASS_DISMISS,
    CLASS_TO_TIER, WF_NEW, WF_WAITING_FOR_ME, WF_WAITING_FOR_OTHER,
    WF_SNOOZED, WF_DONE, WF_IGNORED, WF_ARCHIVED,
)

_OPS = {
    "sender": "sender", "from": "sender",
    "recipient": "recipient", "to": "recipient",
    "subject": "subject", "subj": "subject",
    "after": "after", "since": "after",
    "before": "before", "until": "before",
    "has": "has",
    "status": "status", "state": "status",
    "label": "label", "class": "label",
    "thread": "thread", "thread_id": "thread",
    "folder": "folder", "in": "folder",
}

_STATUS_ALIASES = {
    "new": WF_NEW, "unread": WF_NEW, "未处理": WF_NEW,
    "waiting": WF_WAITING_FOR_ME, "waiting_for_me": WF_WAITING_FOR_ME,
    "waitingforme": WF_WAITING_FOR_ME, "me": WF_WAITING_FOR_ME, "等我": WF_WAITING_FOR_ME,
    "waiting_for_other": WF_WAITING_FOR_OTHER, "waitingforother": WF_WAITING_FOR_OTHER,
    "other": WF_WAITING_FOR_OTHER, "等对方": WF_WAITING_FOR_OTHER,
    "snoozed": WF_SNOOZED, "延后": WF_SNOOZED,
    "done": WF_DONE, "完成": WF_DONE,
    "ignored": WF_IGNORED, "忽略": WF_IGNORED,
    "archived": WF_ARCHIVED, "归档": WF_ARCHIVED,
}

_LABEL_ALIASES = {
    "important": CLASS_IMPORTANT, "vip": CLASS_IMPORTANT, "重点": CLASS_IMPORTANT,
    "reply": CLASS_REPLY, "要回": CLASS_REPLY,
    "system": CLASS_SYSTEM, "act": CLASS_SYSTEM, "系统": CLASS_SYSTEM,
    "read": CLASS_READ, "看一眼": CLASS_READ,
    "dismiss": CLASS_DISMISS, "dump": CLASS_DISMISS, "忽略": CLASS_DISMISS,
}

_TOKEN_RE = re.compile(r'(\w+):("[^"]*"|\'[^\']*\'|\S+)')


def _norm_date(value: str) -> str:
    """把 after:/before: 的值归一成 ISO 日期字符串。"""
    v = (value or "").strip().strip('"').strip("'")
    if not v:
        return ""
    # 相对日期：7d / 2w / 3m
    m = re.fullmatch(r"(\d+)([dwm])", v.lower())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        days = n if unit == "d" else (n * 7 if unit == "w" else n * 30)
        return (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    # 补全 2026-9-1 这类写法
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", v)
    if m:
        return "%04d-%02d-%02d" % (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    m = re.fullmatch(r"(\d{4})[-/](\d{1,2})", v)
    if m:
        return "%04d-%02d-01" % (int(m.group(1)), int(m.group(2)))
    return v[:10]


def parse_query(raw: str) -> dict:
    """把查询串解析成结构化过滤器。"""
    text = (raw or "").strip()
    q = {
        "raw": text,
        "sender": "", "recipient": "", "subject": "",
        "after": "", "before": "", "has_attachment": False,
        "status": "", "label": "", "thread": "", "folder": "",
        "terms": [],
        "unknown_ops": [],
    }
    if not text:
        return q

    consumed = []
    for m in _TOKEN_RE.finditer(text):
        op_raw = m.group(1).lower()
        if op_raw not in _OPS:
            continue
        val = m.group(2).strip().strip('"').strip("'")
        op = _OPS[op_raw]
        consumed.append(m.span())
        if op == "sender":
            q["sender"] = val
        elif op == "recipient":
            q["recipient"] = val
        elif op == "subject":
            q["subject"] = val
        elif op == "after":
            q["after"] = _norm_date(val)
        elif op == "before":
            q["before"] = _norm_date(val)
        elif op == "has":
            q["has_attachment"] = val.lower() in ("attachment", "att", "attach", "附件", "yes", "true", "1")
        elif op == "status":
            mapped = _STATUS_ALIASES.get(val.lower().strip())
            if mapped:
                q["status"] = mapped
            else:
                q["unknown_ops"].append("status:%s" % val)
        elif op == "label":
            mapped = _LABEL_ALIASES.get(val.lower().strip())
            if mapped:
                q["label"] = mapped
            else:
                q["unknown_ops"].append("label:%s" % val)
        elif op == "thread":
            q["thread"] = val
        elif op == "folder":
            q["folder"] = val

    # 去掉已消费的 token，剩余作为全文词
    rest = list(text)
    for a, b in reversed(consumed):
        for i in range(a, b):
            rest[i] = " "
    free = "".join(rest).strip()
    q["terms"] = [t for t in re.split(r"\s+", free) if t]
    return q


def _like(term: str) -> str:
    return "%" + term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def build_sql(q: dict, limit: int = 200, offset: int = 0):
    """把解析结果编译成 (sql, params, order_desc)。"""
    where = []
    params = []

    if q.get("sender"):
        where.append("(m.from_addr LIKE ? ESCAPE '\\' OR m.from_name LIKE ? ESCAPE '\\')")
        params += [_like(q["sender"]), _like(q["sender"])]
    if q.get("recipient"):
        where.append("m.to_addrs LIKE ? ESCAPE '\\'")
        params.append(_like(q["recipient"]))
    if q.get("subject"):
        where.append("m.subject LIKE ? ESCAPE '\\'")
        params.append(_like(q["subject"]))
    if q.get("after"):
        where.append("substr(COALESCE(m.date_iso,''),1,10) >= ?")
        params.append(q["after"])
    if q.get("before"):
        where.append("substr(COALESCE(m.date_iso,''),1,10) < ?")
        params.append(q["before"])
    if q.get("has_attachment"):
        where.append("m.has_attachments = 1")
    if q.get("status"):
        where.append("m.workflow_state = ?")
        params.append(q["status"])
    if q.get("label"):
        where.append("m.classification = ?")
        params.append(q["label"])
    if q.get("thread"):
        where.append("m.thread_id = ?")
        params.append(q["thread"])
    if q.get("folder"):
        where.append("m.folder LIKE ? ESCAPE '\\'")
        params.append(_like(q["folder"]))
    for t in q.get("terms") or []:
        where.append("m.search_blob LIKE ? ESCAPE '\\'")
        params.append(_like(t))

    sql = "SELECT m.* FROM messages m"
    if where:
        sql += " WHERE " + " AND ".join(where)
    # 排序：按 thread 聚合优先（同一线程的邮件相邻），线程内按时间升序
    sql += (" ORDER BY m.thread_id, m.internal_ts ASC"
            " LIMIT ? OFFSET ?")
    params += [int(limit), int(offset)]
    return sql, params, False


def _conn(db):
    """接受 Repo 或 Database 两种入参，避免调用方为了一个查询换包装。"""
    return getattr(db, "db", db)


def run(db, raw_query: str, limit: int = 200, offset: int = 0, group_by_thread: bool = True):
    """执行搜索，返回 {query, count, messages}。"""
    q = parse_query(raw_query)
    sql, params, _ = build_sql(q, limit=limit, offset=offset)
    rows = _conn(db).query(sql, params)
    return {"query": q, "count": len(rows), "messages": rows}


def describe_query(raw: str) -> str:
    """给 UI 用的自然语言回显，让用户确认操作符被识别正确。"""
    q = parse_query(raw)
    bits = []
    if q["sender"]:
        bits.append("发件人含「%s」" % q["sender"])
    if q["recipient"]:
        bits.append("收件人含「%s」" % q["recipient"])
    if q["subject"]:
        bits.append("主题含「%s」" % q["subject"])
    if q["after"]:
        bits.append("%s 之后" % q["after"])
    if q["before"]:
        bits.append("%s 之前" % q["before"])
    if q["has_attachment"]:
        bits.append("含附件")
    if q["status"]:
        bits.append("状态=%s" % q["status"])
    if q["label"]:
        bits.append("分类=%s" % q["label"])
    if q["thread"]:
        bits.append("线程=%s" % q["thread"])
    if q["folder"]:
        bits.append("文件夹含「%s」" % q["folder"])
    if q["terms"]:
        bits.append("全文含 " + " ".join("「%s」" % t for t in q["terms"]))
    if q["unknown_ops"]:
        bits.append("（未识别：%s）" % ", ".join(q["unknown_ops"]))
    return " 且 ".join(bits) if bits else "（未指定条件）"


SEARCH_HELP = """搜索操作符：
  sender:张三        发件人（姓名或地址片段）
  recipient:sjtu     收件人片段
  subject:产学研     主题片段
  after:2026-09-01   该日期之后（支持 after:7d / after:2w / after:1m）
  before:2026-09-20  该日期之前
  has:attachment     仅含附件
  status:waiting     工作流状态（new/waiting/other/snoozed/done/ignored/archived）
  label:important    分类（important/reply/system/read/dismiss）
  folder:INBOX       限定文件夹
  葡萄 机器人        裸词全文匹配（可多个，AND 关系）
结果按 thread 聚合，同线程邮件相邻排列。"""
