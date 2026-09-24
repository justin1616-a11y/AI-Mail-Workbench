# -*- coding: utf-8 -*-
"""Foxmail 7.2 本地邮件索引解析（**只读**）。

定位（规范 §32）：Foxmail 是「桌面客户端 + 历史邮件来源 + 备用完整客户端」，
**绝不是自动化控制目标**。本模块只读它的二进制索引文件，不做任何 GUI
自动化、不模拟鼠标键盘、不依赖 Foxmail 窗口状态。

二进制格式（V1 逆向结论，已实测校准，勿再重复踩坑）：
    Header            512 字节，offset 8 = 总记录数（uint32 LE）
    Record            512 字节
        offset 0      mail_id（0 表示空槽，跳过）
        offset 8      接收时间（OLE Date，double）
        offset 16     发送时间（OLE Date，double）
        offset 51 起  紧凑文本区，以双 0x00 0x00 终止
    文本区字段间**无分隔符**：发件人名 + 发件邮箱 + 收件人名 + 收件邮箱 + 主题
        -> 必须靠 TLD 白名单正则锚定邮箱右边界，
           否则会把 `someone@example.com` 读成 `someone@example.com发件人名`

时间：索引里存的**就是本地时间（北京时间）**。
    OLE -> 本地：datetime(1899,12,30) + timedelta(days=ole)，**不要再 +8h**。
    实测与 IMAP INTERNALDATE(+0800) 完全吻合；额外 +8h 会把 10:24 错算成 18:24。
"""
from __future__ import annotations

import datetime
import os
import struct

from .. import util
from ..constants import FOLDER_FOXMAIL, WF_ARCHIVED

HEADER_SIZE = 512
RECORD_SIZE = 512
OLE_BASE = datetime.datetime(1899, 12, 30)

# 历史邮件没有 Message-ID，用 mail_id 合成稳定键
HISTORY_ID_PREFIX = "<foxmail-"
HISTORY_ID_SUFFIX = "@mail-workbench.local>"


def ole_to_dt(value):
    try:
        return OLE_BASE + datetime.timedelta(days=float(value))
    except Exception:
        return None


def decode_bytes(b: bytes) -> str:
    for enc in ("utf-8", "gbk"):
        try:
            return b.decode(enc)
        except UnicodeDecodeError:
            continue
    return b.decode("utf-8", "replace")


def split_fields(raw: str):
    """把紧凑文本区切成 发件人名/发件邮箱/收件人名/收件邮箱/主题。"""
    matches = list(util.EMAIL_RE.finditer(raw))
    if not matches:
        return None
    first = matches[0]
    from_name = util.clean(raw[: first.start()])
    from_addr = first.group(0).lower()
    if len(matches) >= 2:
        second = matches[1]
        to_name = util.clean(raw[first.end(): second.start()])
        to_addr = second.group(0).lower()
        subject = util.clean(raw[second.end():])
    else:
        to_name, to_addr = "", ""
        subject = util.clean(raw[first.end():])
    # 少数记录主题前会残留一个邮箱地址（收件人名缺失导致），剥掉
    lead = util.EMAIL_RE.match(subject)
    if lead and lead.start() == 0:
        subject = util.clean(subject[lead.end():])
    return {
        "from_name": from_name,
        "from_addr": from_addr,
        "to_name": to_name,
        "to_addr": to_addr,
        "subject": util.collapse(subject),
    }


def index_exists(path: str) -> bool:
    return bool(path) and os.path.isfile(path)


def parse_index(path: str, limit: int = 0, since: datetime.datetime = None) -> list:
    """解析索引文件，返回记录列表（已去重）。

    limit / since 只是取样窗口，不影响去重语义。
    """
    if not index_exists(path):
        raise FileNotFoundError("Foxmail 索引不存在：%s" % path)

    size = os.path.getsize(path)
    out = []
    with open(path, "rb") as f:
        header = f.read(HEADER_SIZE)
        if len(header) < HEADER_SIZE:
            raise ValueError("索引文件头不足 512 字节，格式不符")
        total = struct.unpack_from("<I", header, 8)[0]
        max_rec = (size - HEADER_SIZE) // RECORD_SIZE
        count = min(total, max_rec)
        for i in range(count):
            f.seek(HEADER_SIZE + i * RECORD_SIZE)
            rec = f.read(RECORD_SIZE)
            if len(rec) < RECORD_SIZE:
                break
            mail_id = struct.unpack_from("<I", rec, 0)[0]
            if mail_id == 0:
                continue
            received = ole_to_dt(struct.unpack_from("<d", rec, 8)[0])
            if since and received and received < since:
                continue
            text_blob = rec[51:].split(b"\x00\x00")[0]
            fields = split_fields(decode_bytes(text_blob))
            if not fields:
                continue
            if since and not received:
                continue
            fields.update({
                "mail_id": mail_id,
                "received": received,
                "date_iso": received.replace(microsecond=0).isoformat() if received else "",
                "internal_ts": received.timestamp() if received else 0.0,
            })
            out.append(fields)

    # 同一封邮件常因多文件夹/重复同步出现两条以上记录，按 人+主题+时间 去重
    seen, unique = set(), []
    for r in out:
        key = (r["from_addr"], r["to_addr"], r["subject"], r["received"])
        if key in seen:
            continue
        seen.add(key)
        unique.append(r)

    unique.sort(key=lambda x: x["received"] or datetime.datetime.min, reverse=True)
    if limit:
        unique = unique[:limit]
    return unique


def history_message_id(mail_id) -> str:
    return "%s%s%s" % (HISTORY_ID_PREFIX, mail_id, HISTORY_ID_SUFFIX)


def to_message_row(rec: dict, account: str, folder: str = None) -> dict:
    """把索引记录转成 store 的 messages 行。

    历史记录**只有元数据、没有正文**（正文在 Foxmail 里加密存放），
    因此 body_text 留空，靠按需联网的取正文命令补。

    workflow_state 置为 ARCHIVED —— 这是关键设计：
      * 历史邮件已经处理过了，不应该出现在「待我处理」里（否则 6696 封历史
        会瞬间淹没工作桶）；
      * 同时仍参与线程聚合，成为起草时的 thread context（规范 §7）；
      * 一旦该线程来了新邮件，新邮件是 latest 且状态 NEW，线程会自然
        重新变成「等我处理」。
    """
    subject = rec.get("subject") or ""
    return {
        "message_id": history_message_id(rec.get("mail_id")),
        "account": account,
        "folder": folder or FOLDER_FOXMAIL,
        "uid": None,
        "thread_id": None,
        "subject": subject,
        "from_addr": rec.get("from_addr") or "",
        "from_name": rec.get("from_name") or "",
        "to_addrs": [rec["to_addr"]] if rec.get("to_addr") else [],
        "date_iso": rec.get("date_iso") or "",
        "internal_ts": rec.get("internal_ts") or 0.0,
        "unread": False,
        "has_attachments": False,
        "attachment_names": [],
        "body_text": "",
        "workflow_state": WF_ARCHIVED,
        "source": "foxmail",
        "in_reply_to": "",
        "references_ids": [],
    }


def import_history(repo, cfg: dict, limit: int = 0, log=None, since=None) -> dict:
    """把 Foxmail 历史索引导入 store（幂等）。

    只导入元数据；已存在的记录不会重复写。
    导入后按主题归并到 ThreadAggregator，成为 WorkBuddy 起草时的历史上下文。
    """
    path = cfg.get("foxmail_index_path") or ""
    if not cfg.get("foxmail_enabled", True):
        return {"ok": False, "reason": "foxmail_enabled=false", "imported": 0, "scanned": 0}
    if not index_exists(path):
        return {"ok": False, "reason": "index_missing", "path": path, "imported": 0, "scanned": 0}

    try:
        recs = parse_index(path, limit=limit, since=since)
    except Exception as e:
        return {"ok": False, "reason": str(e), "imported": 0, "scanned": 0}

    from .. import util as _util
    from ..thread import aggregator as _agg

    account = cfg.get("user") or ""
    inserted = 0
    linked = 0
    subjects = []
    tid_cache = {}
    for rec in recs:
        row = to_message_row(rec, account)
        # create=False：只挂到**已经存在**的同主题线程上，不为历史主题新建线程。
        # 否则 6696 封历史会凭空生成几千个「只有一封旧邮件」的线程。
        # 历史邮件真正被用起来是在「新邮件到达」时由 aggregator.backfill_history
        # 拉进当前线程，成为起草上下文。
        tid = _agg.thread_id_for_subject(repo, row["subject"], tid_cache, create=False)
        row["thread_id"] = tid
        if tid:
            linked += 1
        try:
            action = repo.upsert_message(row)
        except Exception:
            continue
        if action == "inserted":
            inserted += 1
        subjects.append(_util.normalize_subject(row["subject"]))

    # 每个受影响线程只重算一次（传 cfg，才能正确判定收发方向）
    touched = _agg.rebuild_threads_for_subjects(repo, subjects, cfg) if linked else 0

    # 不变式收敛：source='foxmail' 的邮件状态不应该是 NEW。
    # 旧版本导入的历史邮件会残留 NEW（当时还没有 ARCHIVED 语义），
    # 这里做一次幂等归正，否则它们会把「待我处理」桶刷爆。
    fixed = repo.db.execute(
        "UPDATE messages SET workflow_state=?, "
        "workflow_changed_at=COALESCE(workflow_changed_at, ?) "
        "WHERE source='foxmail' AND (workflow_state IS NULL OR workflow_state='NEW')",
        (WF_ARCHIVED, util.now_iso())).rowcount or 0
    if fixed and linked:
        touched += _agg.rebuild_threads_for_subjects(repo, subjects, cfg)

    result = {"ok": True, "path": path, "scanned": len(recs),
              "imported": inserted, "linked_existing_threads": linked,
              "threads_touched": touched, "reconciled_archived": fixed,
              "note": "历史邮件状态为 ARCHIVED（不进「待我处理」桶）；"
                      "不新建线程，等新邮件到达时按主题回填为起草上下文。"}
    if log:
        log("Foxmail history: scanned=%d imported=%d linked=%d threads=%d"
            % (len(recs), inserted, linked, touched))
    return result
    if log:
        log("Foxmail history: scanned=%d imported=%d threads=%d" % (len(recs), inserted, touched))
    return result


def stats(path: str) -> dict:
    """给 /api/health 用：索引健康状态。"""
    if not index_exists(path):
        return {"ok": False, "reason": "missing", "path": path}
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            header = f.read(HEADER_SIZE)
        total = struct.unpack_from("<I", header, 8)[0] if len(header) >= HEADER_SIZE else 0
        mtime = datetime.datetime.fromtimestamp(os.path.getmtime(path))
        return {"ok": True, "path": path, "size_bytes": size,
                "declared_records": total,
                "mtime": mtime.replace(microsecond=0).isoformat(),
                "stale_hours": round((datetime.datetime.now() - mtime).total_seconds() / 3600, 1)}
    except Exception as e:
        return {"ok": False, "reason": str(e), "path": path}
