# -*- coding: utf-8 -*-
"""One-click Processing / Batch / Undo（规范 §15 / §19）。

目标：**一封邮件最多 1~2 次操作就能退出 Inbox。**

可用动作（与 UI 快捷键一一对应）：
    reply_ai   → 建 DraftJob（r）
    reply_man  → 人工回复（返回 mailto / 客户端提示）
    archive    → 归档（a）
    snooze     → 延后（s）
    waiting    → 标记等对方（w）
    done       → 完成（d）
    ignore     → 忽略
    star/unstar→ 星标切换
    read/unread→ 已读切换
    trash      → 软删除（可 undo）
    undo       → 撤销上一次可撤销动作（u）

写操作策略：
  * 先动服务器（IMAP STORE / COPY），成功后再改本地库 —— 避免本地与服务器分叉。
  * 服务器失败时不静默吞掉：返回 ok=False + error，本地状态保持不变。
  * 删除一律是 **\\Deleted 标记 + 不 EXPUNGE**，与 V1 的约定一致，永远可恢复。
  * 每个可撤销动作都写 undo_log，UI 按 `u` 一键回滚。
"""
from __future__ import annotations

from .. import util
from ..constants import (
    WF_ARCHIVED, WF_DONE, WF_IGNORED, WF_NEW, WF_SNOOZED, WF_WAITING_FOR_ME,
    WF_WAITING_FOR_OTHER,
)
from ..mail import imap_client as imc
from ..thread import aggregator as agg
from . import snooze_manager as snzm


class ActionError(Exception):
    pass


# --------------------------------------------------------------------------
def _client(cfg, log=None):
    return imc.ImapClient(cfg, log=log)


def _folder_of(msg: dict) -> str:
    return msg.get("folder") or "INBOX"


def _uid_of(msg: dict):
    return msg.get("uid")


def _imap_store(cfg, msg: dict, op: str, flags: list, log=None, skip: bool = False) -> dict:
    """改服务器标志。

    `skip=True`：调用方（批处理）已经把标志**合并下发**过了，这里只当成功返回。
    只有批量路径会用它 —— 逐封路径永远是 False，避免「以为发了其实没发」。
    """
    if skip:
        return {"ok": True, "skipped": True}
    if not _uid_of(msg):
        return {"ok": False, "error": "该邮件没有服务器 UID（可能是 Foxmail 历史记录），无法改服务器状态"}
    c = _client(cfg, log=log)
    try:
        c.connect()
        c.select(_folder_of(msg), readonly=False)
        ok = c.store_flags(_uid_of(msg), op, flags)
        return {"ok": bool(ok)}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        c.close()


def _imap_move_to_archive(cfg, msg: dict, archive_folder: str, log=None) -> dict:
    if not _uid_of(msg):
        return {"ok": False, "error": "没有 UID"}
    c = _client(cfg, log=log)
    try:
        c.connect()
        folders = {f["name"] for f in c.list_folders()}
        if archive_folder not in folders:
            return {"ok": False, "error": "服务器上没有 %s 文件夹" % archive_folder}
        c.select(_folder_of(msg), readonly=False)
        name = archive_folder
        typ, _ = c.m.uid("COPY", str(_uid_of(msg)), '"%s"' % name)
        if typ != "OK":
            return {"ok": False, "error": "COPY 失败"}
        c.store_flags(_uid_of(msg), "+", ["\\Deleted"])
        return {"ok": True, "moved_to": archive_folder}
    except Exception as e:
        return {"ok": False, "error": str(e)}
    finally:
        c.close()


# --------------------------------------------------------------------------
def _snapshot(msg: dict) -> dict:
    return {
        "message_id": msg["message_id"],
        "workflow_state": msg.get("workflow_state"),
        "unread": bool(msg.get("unread")),
        "flagged": bool(msg.get("flagged")),
        "deleted": bool(msg.get("deleted")),
        "snooze_until": msg.get("snooze_until"),
    }


def _apply_local(repo, cfg: dict, msg: dict, state: str, patch: dict = None) -> None:
    """把人类动作落到本地：先写 message，再让聚合器把派生 flag 对齐。

    必须走 aggregator 且**必须传 cfg** —— cfg.self_addresses 决定收发方向，
    漏传会让所有邮件都被当成「对方来信」。这样 waiting_for_me /
    waiting_for_other 会跟着「最新一封上的人类决定」走，不会被聚合反向覆盖。
    """
    data = {"workflow_changed_at": util.now_iso()}
    data.update(patch or {})
    repo.set_workflow_state(msg["message_id"], state, **data)
    tid = msg.get("thread_id")
    if not tid:
        return
    agg.aggregate(repo, tid, cfg)
    latest = repo.latest_in_thread(tid)
    if not (latest and latest["message_id"] == msg["message_id"]):
        # 动作落在非最新一封上（少见）：线程级状态显式设置，别让它被派生值盖掉
        repo.set_thread_workflow(tid, state)


def apply(repo, cfg: dict, action: str, message_id: str = None,
          thread_id: str = None, **kw) -> dict:
    """执行单个动作。返回 {ok, action, message_id, message, undo_id?}

    批处理专用的两个开关（逐封调用不会传）：
      skip_imap  服务器标志已由批处理**合并下发**，这里不要再连一次
      no_undo    不写逐封 undo 行 —— 批处理自己有整批快照，
                 151 封各写一行会让 undo_log 爆炸，也会把「整批撤销」挤下去
    """
    log = kw.get("log")
    skip_imap = bool(kw.get("skip_imap"))
    no_undo = bool(kw.get("no_undo"))
    if action == "open_external":
        return _open_external(repo, cfg, message_id)

    msg = repo.get_message(message_id) if message_id else None
    if action in ("snooze_thread", "waiting_thread") and not msg and thread_id:
        pass
    if msg is None and thread_id:
        msg = repo.latest_in_thread(thread_id)
    if msg is None:
        raise ActionError("找不到邮件：message_id=%s thread_id=%s" % (message_id, thread_id))
    message_id = msg["message_id"]
    undo_id = None if no_undo else repo.push_undo(action, _snapshot(msg))

    def _st(op, flags):
        """本封邮件的服务器标志。批处理下 skip_imap=True，只走本地。"""
        return _imap_store(cfg, msg, op, flags, log=log, skip=skip_imap)

    if action == "reply_ai":
        from ..draft import queue as dq
        job = dq.ensure_job(repo, cfg, message_id, trigger_source="manual",
                            draft_mode=kw.get("draft_mode"))
        cand = repo.get_candidate_by_message(message_id)
        if cand:
            repo.set_candidate_status(cand["candidate_id"], "converted")
        return {"ok": True, "action": action, "message_id": message_id,
                "job": dq.to_public(job), "undo_id": None,
                "message": "已排入草稿队列（%s）" % job["status"]}

    if action == "reply_manual":
        from urllib.parse import quote
        addr = msg.get("from_addr") or ""
        subj = msg.get("subject") or ""
        return {"ok": True, "action": action, "message_id": message_id,
                "mailto": "mailto:%s?subject=%s" % (addr, quote("Re: " + subj)),
                "undo_id": None,
                "message": "已生成人工回复入口（不代发邮件）"}

    if action == "archive":
        res = {"ok": True}
        af = cfg.get("folder_archive", "Archive")
        if cfg.get("archive_move_enabled"):
            res = _imap_move_to_archive(cfg, msg, af, log=log)
            if not res.get("ok"):
                res = _st("+", ["\\Seen"])
                res["fallback"] = "仅标记已读，未移动"
        else:
            res = _st("+", ["\\Seen"])
        if not res.get("ok"):
            return {"ok": False, "action": action, "error": res.get("error"),
                    "message": "归档失败：%s" % res.get("error"), "undo_id": undo_id}
        _apply_local(repo, cfg, msg, WF_ARCHIVED)
        repo.set_flags(message_id, unread=False)
        repo.inc_metric("emails_archived")
        return {"ok": True, "action": action, "message_id": message_id,
                "workflow_state": WF_ARCHIVED, "undo_id": undo_id,
                "detail": res, "message": "已归档"}

    if action == "snooze":
        when = kw.get("when") or "tomorrow"
        s = snzm.SnoozeManager(repo, cfg, log=log).snooze(
            message_id=message_id, thread_id=msg.get("thread_id"),
            when=when, note=kw.get("note") or "")
        return {"ok": True, "action": action, "message_id": message_id,
                "snooze": s, "undo_id": undo_id,
                "message": "已延后到 %s" % s["wake_at"]}

    if action == "unsnooze":
        snzm.SnoozeManager(repo, cfg, log=log).cancel(
            message_id=message_id, thread_id=msg.get("thread_id"))
        return {"ok": True, "action": action, "message_id": message_id,
                "undo_id": undo_id, "message": "已取消延后"}

    if action == "waiting":
        # 同 done / ignore：这是本地工作流决定，
        # 「\Answered」只是顺手同步给服务器，同步不上也不该拦住它。
        res = _st("+", ["\\Answered"]) if kw.get("mark_answered") \
            else {"ok": True}
        imap_note = "" if res.get("ok") else (res.get("error") or "服务器标志未同步")
        _apply_local(repo, cfg, msg, WF_WAITING_FOR_OTHER)
        if msg.get("thread_id"):
            repo.close_followups_for_thread(msg["thread_id"], status="replied")
        return {"ok": True, "action": action, "message_id": message_id,
                "workflow_state": WF_WAITING_FOR_OTHER, "undo_id": undo_id,
                "imap": imap_note,
                "message": "已标记为等对方回复" if not imap_note
                           else "已标记为等对方回复（%s）" % imap_note}

    if action == "done":
        # 「完成」是**本地**动作：目的是把这个线程从工作桶里收起来。
        # 顺手标已读只是同步给服务器，做不到**不该拦住主操作** ——
        # Foxmail 历史邮件（从本地索引导入的老邮件）根本没有服务器副本，
        # 若在这里 return，这类邮件就永远无法「完成」，用户会以为按钮坏了。
        # 实测就是卡在这儿：「点了完成，但提示没有服务器 UID」。
        res = _st("+", ["\\Seen"])
        imap_note = "" if res.get("ok") else (res.get("error") or "服务器标志未同步")
        _apply_local(repo, cfg, msg, WF_DONE)
        repo.set_flags(message_id, unread=False)
        if msg.get("thread_id"):
            repo.close_followups_for_thread(msg["thread_id"], status="done")
        return {"ok": True, "action": action, "message_id": message_id,
                "workflow_state": WF_DONE, "undo_id": undo_id,
                "imap": imap_note,
                "message": "已完成" if not imap_note
                           else "已完成（本地状态已更新；%s）" % imap_note}

    if action == "ignore":
        # 同 done：忽略也是本地决定，服务器标志同步不了就算了。
        res = _st("+", ["\\Seen"])
        imap_note = "" if res.get("ok") else (res.get("error") or "服务器标志未同步")
        _apply_local(repo, cfg, msg, WF_IGNORED)
        repo.set_flags(message_id, unread=False)
        cand = repo.get_candidate_by_message(message_id)
        if cand:
            repo.set_candidate_status(cand["candidate_id"], "dismissed")
        return {"ok": True, "action": action, "message_id": message_id,
                "workflow_state": WF_IGNORED, "undo_id": undo_id,
                "imap": imap_note,
                "message": "已忽略" if not imap_note
                           else "已忽略（本地状态已更新；%s）" % imap_note}

    if action in ("star", "unstar"):
        add = action == "star"
        res = _st("+" if add else "-", ["\\Flagged"])
        if not res.get("ok"):
            return {"ok": False, "action": action, "error": res.get("error"), "undo_id": undo_id}
        repo.set_flags(message_id, flagged=add)
        if add:
            repo.inc_metric("emails_starred")
        return {"ok": True, "action": action, "message_id": message_id,
                "flagged": add, "undo_id": undo_id,
                "message": "已加星标" if add else "已取消星标"}

    if action in ("read", "unread"):
        unread = action == "unread"
        # 置回未读要 -FLAGS \Seen；部分服务器不允许，失败时如实返回错误
        res = (_st("-", ["\\Seen"]) if unread
               else _st("+", ["\\Seen"]))
        if not res.get("ok"):
            return {"ok": False, "action": action, "error": res.get("error"),
                    "undo_id": undo_id}
        repo.set_flags(message_id, unread=unread)
        return {"ok": True, "action": action, "message_id": message_id,
                "unread": unread, "undo_id": undo_id,
                "message": "已标为未读" if unread else "已标为已读"}

    if action == "trash":
        res = _st("+", ["\\Deleted"])
        if not res.get("ok"):
            return {"ok": False, "action": action, "error": res.get("error"), "undo_id": undo_id}
        repo.set_flags(message_id, deleted=True)
        repo.inc_metric("emails_trashed")
        return {"ok": True, "action": action, "message_id": message_id,
                "undo_id": undo_id, "recoverable": True,
                "message": "已标记删除（可恢复，未彻底删除）"}

    if action == "restore":
        res = _st("-", ["\\Deleted"])
        if not res.get("ok"):
            return {"ok": False, "action": action, "error": res.get("error"), "undo_id": undo_id}
        repo.set_flags(message_id, deleted=False)
        return {"ok": True, "action": action, "message_id": message_id,
                "undo_id": undo_id, "message": "已恢复"}

    if action == "reopen":
        _apply_local(repo, cfg, msg, WF_WAITING_FOR_ME)
        return {"ok": True, "action": action, "message_id": message_id,
                "workflow_state": WF_WAITING_FOR_ME, "undo_id": undo_id,
                "message": "已重新打开为待我处理"}

    raise ActionError("未知动作：%s" % action)


def _open_external(repo, cfg: dict, message_id: str) -> dict:
    """规范 §32：禁止 GUI 自动化点击 Foxmail。

    因此这里**不**尝试唤起 Foxmail 窗口，只提供：
      * 一个 mailto 链接（系统默认客户端可处理）
      * 明确的提示：邮件全文在 Mail Workbench 内查看
    """
    msg = repo.get_message(message_id) if message_id else None
    addr = (msg or {}).get("from_addr") or ""
    subj = (msg or {}).get("subject") or ""
    return {
        "ok": True, "action": "open_external", "message_id": message_id,
        "mailto": "mailto:%s" % addr if addr else "",
        "hint": "Mail Workbench 是主处理界面；Foxmail 仅作为备用完整客户端。"
                "按规范不做法务外的 GUI 自动化，请在本页内查看全文。",
        "subject": subj,
    }


# --------------------------------------------------------------------------
# 批处理（规范 §19）
# --------------------------------------------------------------------------
BATCH_ACTIONS = ("archive", "ignore", "done", "read", "star", "trash", "waiting")

# 批处理里「纯标志」动作 —— 可以把整批 UID 合并成一条 UID STORE。
# (op, flags) 必须与 apply() 里各动作真正下发的**完全一致**，
# 否则会出现「本地状态已改、服务器没改」的分叉。
_BATCH_FLAGS = {
    "archive": ("+", ["\\Seen"]),
    "ignore": ("+", ["\\Seen"]),
    "done": ("+", ["\\Seen"]),
    "read": ("+", ["\\Seen"]),
    "star": ("+", ["\\Flagged"]),
    "trash": ("+", ["\\Deleted"]),
}


def _prestore_flags(cfg, msgs: list, op: str, flags: list, log=None) -> dict:
    """把一批邮件的服务器标志**合并**下发（按文件夹分组，一条 UID STORE 一批）。

    这是批处理性能的关键路径。原来逐封 apply() 会让每封邮件各建一次 IMAP 连接：
    本机实测单次连接 150ms，全选 151 封 = **22.6 秒纯连接开销** ——
    用户看到的就是「点了批量按钮，页面半天没反应」。

    返回 {ok, ids, count, note}。ok=False 时 ids 为空集 ——
    **宁可退回逐封路径，也不要让本地状态领先于服务器**。
    """
    by_folder = {}
    for m in msgs:
        uid = _uid_of(m)
        if uid:
            by_folder.setdefault(_folder_of(m), []).append(str(uid))
    if not by_folder:
        return {"ok": True, "ids": set(), "count": 0, "note": "没有需要改服务器状态的邮件"}

    c = _client(cfg, log=log)
    count = 0
    try:
        c.connect()
        for folder, uids in by_folder.items():
            c.select(folder, readonly=False)
            r = c.store_flags_many(uids, op, flags)
            if not r.get("ok"):
                return {"ok": False, "ids": set(), "count": count,
                        "note": r.get("error") or "合并下发失败"}
            count += r.get("count") or 0
        return {"ok": True, "ids": {m["message_id"] for m in msgs if _uid_of(m)},
                "count": count, "note": "服务器标志已合并下发 %d 封" % count}
    except Exception as e:
        return {"ok": False, "ids": set(), "count": count,
                "note": "合并下发异常：%s" % e}
    finally:
        try:
            c.close()
        except Exception:
            pass


def batch(repo, cfg: dict, action: str, message_ids: list, **kw) -> dict:
    if action not in BATCH_ACTIONS:
        raise ActionError("批处理不支持动作：%s（可选 %s）" % (action, BATCH_ACTIONS))
    ids = [m for m in (message_ids or []) if m]
    if len(ids) > 200:
        raise ActionError("单次批处理上限 200 封，当前 %d" % len(ids))

    # 先读一遍并快照 —— 批处理才能整批撤销（否则 undo 只能恢复到「已归档」）
    msgs = {}
    for mid in ids:
        m = repo.get_message(mid)
        if m:
            msgs[mid] = m
    snapshots = {mid: _snapshot(m) for mid, m in msgs.items()}

    # ---- 服务器标志：合并下发（性能关键路径，见 _prestore_flags）----
    skip_ids, imap_note = set(), ""
    plan = _BATCH_FLAGS.get(action)
    if plan and not (action == "archive" and cfg.get("archive_move_enabled")):
        pre = _prestore_flags(cfg, list(msgs.values()), plan[0], plan[1], log=kw.get("log"))
        skip_ids, imap_note = pre["ids"], pre["note"]
    elif action == "archive":
        imap_note = "archive_move_enabled=true，退回逐封移动"
    else:
        imap_note = "非纯标志动作，逐封处理"

    results, ok_n, fail_n = [], 0, 0
    for mid in ids:
        try:
            # skip_imap：上面已合并下发；no_undo：整批只有一条 undo 记录（见下）
            r = apply(repo, cfg, action, message_id=mid,
                      skip_imap=mid in skip_ids, no_undo=True, **kw)
            results.append({"message_id": mid, "ok": bool(r.get("ok")),
                            "message": r.get("message") or r.get("error")})
            ok_n += 1 if r.get("ok") else 0
            fail_n += 0 if r.get("ok") else 1
        except Exception as e:
            results.append({"message_id": mid, "ok": False, "message": str(e)})
            fail_n += 1
    repo.push_undo("batch_" + action, {"action": action, "message_ids": ids,
                                       "snapshots": snapshots})
    return {"ok": fail_n == 0, "action": action, "total": len(ids),
            "succeeded": ok_n, "failed": fail_n, "results": results,
            "imap": imap_note}


def batch_classify(repo, cfg: dict, message_ids: list, classification: str) -> dict:
    """批处理：人工重设分类（覆盖规则引擎的判断）。"""
    n = 0
    for mid in message_ids or []:
        if repo.get_message(mid):
            repo.set_classification(mid, classification)
            n += 1
    return {"ok": True, "action": "classify", "classification": classification, "updated": n}


# --------------------------------------------------------------------------
# Undo
# --------------------------------------------------------------------------
def undo(repo, cfg: dict, undo_id: int = None, log=None) -> dict:
    row = repo.last_undo() if undo_id is None else repo.db.query_one(
        "SELECT * FROM undo_log WHERE id = ?", (int(undo_id),))
    if row and isinstance(row.get("payload"), str):
        try:
            import json
            row["payload"] = json.loads(row["payload"])
        except Exception:
            row["payload"] = {}
    if not row:
        return {"ok": False, "message": "没有可撤销的动作"}
    if row.get("undone"):
        return {"ok": False, "message": "该动作已经撤销过了"}

    payload = row.get("payload") or {}
    action = row.get("action")
    results = []
    imap_note = ""

    if action and str(action).startswith("batch_"):
        snaps = payload.get("snapshots") or {}
        ids = payload.get("message_ids") or []

        # 先把服务器标志按 (op, flags) 归组合并下发（性能关键路径）：
        # 逐封撤销每封最多 2 次连接，145 封 ≈ 43 秒，点一下像卡死。
        groups = {}
        for mid in ids:
            m = repo.get_message(mid)
            if not m or not _uid_of(m):
                continue
            for op, flags in _undo_flag_ops(snaps.get(mid) or {}):
                groups.setdefault((op, tuple(flags)), []).append(m)

        skip_ids, notes = set(), []
        if groups:
            all_ok = True
            for (op, flags), group_msgs in groups.items():
                pre = _prestore_flags(cfg, group_msgs, op, list(flags), log=log)
                if pre.get("ok"):
                    skip_ids |= pre["ids"]
                    notes.append(pre["note"])
                else:
                    all_ok = False
                    notes.append(pre.get("note") or "合并下发失败")
            if not all_ok:
                # 任一分组失败就**整批退回逐封**：不能一半合并一半不合并，
                # 那样会出现「有的邮件恢复了、有的没有」还看不出来。
                skip_ids = set()
                notes.append("有分组失败，整批退回逐封")
        for mid in ids:
            results.append(undo_one(repo, cfg, mid, payload=snaps.get(mid), log=log,
                                    skip_imap=mid in skip_ids))
        imap_note = "；".join(n for n in notes if n)
    else:
        mid = payload.get("message_id")
        if mid:
            results.append(undo_one(repo, cfg, mid, payload=payload, log=log))

    repo.mark_undone(row["id"])
    ok = all(r.get("ok", True) for r in results) if results else True
    return {"ok": ok, "action": "undo", "undid": action, "count": len(results),
            "results": results, "imap": imap_note,
            "message": "已撤销「%s」" % action if ok else "部分撤销失败"}


def _undo_flag_ops(payload: dict) -> list:
    """撤销某封邮件时需要对服务器做的 (op, flags) 列表。

    与 `undo_one` 里的判断**一一对应**，抽出来是为了能按 (op, flags) 归组、
    一次下发一批 UID（撤销 145 封若逐封来 ≈ 2×145 次连接 ≈ 43 秒，
    用户点「撤销」会觉得卡死了）。
    """
    ops = []
    if payload.get("deleted") is False:
        ops.append(("-", ["\\Deleted"]))
    if payload.get("flagged") is False:
        ops.append(("-", ["\\Flagged"]))
    if payload.get("flagged") is True:
        ops.append(("+", ["\\Flagged"]))
    if payload.get("unread") is True:
        ops.append(("-", ["\\Seen"]))
    elif payload.get("unread") is False:
        ops.append(("+", ["\\Seen"]))
    return ops


def undo_one(repo, cfg: dict, message_id: str, payload: dict = None, log=None,
             skip_imap: bool = False) -> dict:
    """把一封邮件恢复到 payload 里记录的状态。

    `skip_imap=True`：调用方（批量撤销）已经把服务器标志合并下发过了，这里只恢复本地。
    """
    msg = repo.get_message(message_id)
    if not msg:
        return {"ok": False, "message_id": message_id, "error": "邮件不存在"}
    if payload is None:
        payload = _snapshot(msg)

    # 服务器：清掉本动作可能加上的标记
    if not skip_imap:
        for op, flags in _undo_flag_ops(payload):
            _imap_store(cfg, msg, op, flags, log=log)

    repo.set_flags(message_id,
                   unread=bool(payload.get("unread")),
                   flagged=bool(payload.get("flagged")),
                   deleted=bool(payload.get("deleted")))
    state = payload.get("workflow_state") or WF_NEW
    _apply_local(repo, cfg, msg, state, {"snooze_until": payload.get("snooze_until")})
    if payload.get("snooze_until") is None:
        repo.cancel_snoozes_for(message_id=message_id)
    repo.inc_metric("undos")
    return {"ok": True, "message_id": message_id, "workflow_state": state}
