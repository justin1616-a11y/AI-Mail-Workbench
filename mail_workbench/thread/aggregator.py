# -*- coding: utf-8 -*-
"""ThreadAggregator —— Thread-first，而不是 Mail-first（规范 §7）。

V2 的默认处理对象是 **conversation thread**，不是单封邮件。
这样 WorkBuddy 起草时能看到「最近 N 封 + 当前待回复 + 已有草稿」，
避免出现 V1 时代的问题：重复问已经回答过的问题、不知道历史承诺、
弄错上下文。

线程判定优先级：
  1. References 的最根节点（RFC 5322 可靠依据）
  2. In-Reply-To
  3. 归一化主题（去 Re:/Fwd:/回复/转发 前缀后一致）
"""
from __future__ import annotations

from .. import util
from ..constants import CLASS_PRIORITY, WF_DONE, WF_IGNORED, WF_ARCHIVED, WF_NEW, \
    WF_SNOOZED, WF_WAITING_FOR_ME, WF_WAITING_FOR_OTHER

# 人类显式动作产生的状态。判定「显式」的**唯一依据**是最新一封邮件上的
# workflow_state —— 因为工作流动作总是落在具体某封邮件上，这就是可靠的溯源信息。
# 线程级状态只是这些显式决定的投影，不能反过来当成依据（否则一次
# WAITING_FOR_OTHER 投影会在下一封来信到达时把线程错误地钉死）。
EXPLICIT_HUMAN_STATES = frozenset({
    WF_WAITING_FOR_ME, WF_WAITING_FOR_OTHER, WF_SNOOZED,
    WF_DONE, WF_IGNORED, WF_ARCHIVED,
})


def _flags_for_state(state: str):
    """把人类显式状态翻译成 (waiting_for_me, waiting_for_other, needs_reply)。"""
    if state == WF_WAITING_FOR_OTHER:
        return 0, 1, 0
    if state in (WF_WAITING_FOR_ME, WF_SNOOZED):
        return 1, 0, 1
    if state in (WF_DONE, WF_IGNORED, WF_ARCHIVED):
        return 0, 0, 0
    return None


def compute_thread_id(info: dict) -> tuple:
    from ..mail import parser as mparser
    return mparser.build_thread_key(info)


def _self_set(cfg: dict) -> set:
    return {a.lower() for a in (cfg.get("self_addresses") or [])}


def _sort_key(m: dict):
    """线程内排序键 —— 与 repo.LATEST_ORDER 保持同一语义（升序）。

    同一时刻优先把 IMAP 真实邮件排在后面，于是它成为 `msgs_sorted[-1]`，
    也就是决定线程状态的「最新一封」。历史存根绝不参与状态判定。
    """
    return (
        float(m.get("internal_ts") or 0),
        1 if (m.get("source") or "imap") == "imap" else 0,
        int(m.get("size_bytes") or 0),
        str(m.get("message_id") or ""),
    )


def aggregate(repo, thread_id: str, cfg: dict = None) -> dict:
    """从 messages 表重算某个 thread 的聚合视图并写回。"""
    cfg = cfg or {}
    self_set = _self_set(cfg)
    msgs = repo.messages_for_thread(thread_id)
    if not msgs:
        return {}

    msgs_sorted = sorted(msgs, key=_sort_key)
    participants = []
    for m in msgs_sorted:
        for a in [m.get("from_addr")] + util.parse_recipients(m.get("to_addrs") or ""):
            if a and a not in participants:
                participants.append(a)
    for m in msgs_sorted:
        for a in util.parse_recipients(m.get("cc_addrs") or ""):
            if a and a not in participants:
                participants.append(a)

    inbound = [m for m in msgs_sorted if (m.get("from_addr") or "").lower() not in self_set]
    outbound = [m for m in msgs_sorted if (m.get("from_addr") or "").lower() in self_set]

    last_in = max((m.get("internal_ts") or 0) for m in inbound) if inbound else 0
    last_out = max((m.get("internal_ts") or 0) for m in outbound) if outbound else 0
    latest = msgs_sorted[-1]

    # needs_reply / waiting_for_me：最新一封是对方来的，且我方没有在其后回过
    latest_is_inbound = (latest.get("from_addr") or "").lower() not in self_set
    waiting_for_me = bool(latest_is_inbound)
    waiting_for_other = bool(not latest_is_inbound and outbound)
    needs_reply = waiting_for_me

    # 分类取「最新一封对方来信」的分类（我方自己的信不该决定这个线程的分类）
    classification = None
    priority = 50
    for m in reversed(inbound or msgs_sorted):
        if m.get("classification"):
            classification = m["classification"]
            priority = max(priority, int(m.get("priority") or 0))
            break
    if classification is None and latest.get("classification"):
        classification = latest["classification"]
        priority = max(priority, int(latest.get("priority") or 0))

    # 线程工作流状态：人类对「最新一封」的显式动作优先；
    # 没有显式动作时完全由方向（谁最后发的）推导。
    latest_wf = latest.get("workflow_state") or WF_NEW
    if latest_wf in EXPLICIT_HUMAN_STATES:
        wf = latest_wf
        flags = _flags_for_state(latest_wf)
        if flags:
            waiting_for_me, waiting_for_other, needs_reply = flags
    else:
        if waiting_for_me:
            wf = WF_WAITING_FOR_ME
        elif waiting_for_other:
            wf = WF_WAITING_FOR_OTHER
        else:
            wf = WF_NEW

    thread = {
        "thread_id": thread_id,
        "account": latest.get("account") or "",
        "subject": latest.get("subject") or "",
        "subject_norm": latest.get("subject_norm") or util.normalize_subject(latest.get("subject")),
        "participants": participants,
        "message_count": len(msgs_sorted),
        "latest_message_id": latest.get("message_id"),
        "latest_ts": latest.get("internal_ts") or 0,
        "last_inbound_ts": last_in,
        "last_outbound_ts": last_out,
        "needs_reply": needs_reply,
        "waiting_for_me": waiting_for_me,
        "waiting_for_other": waiting_for_other,
        "classification": classification,
        "workflow_state": wf,
        "priority": priority,
        "has_attachments": any(m.get("has_attachments") for m in msgs_sorted),
    }
    repo.upsert_thread(thread)
    return thread


def rebuild_threads_for_subjects(repo, subject_norms, cfg: dict = None) -> int:
    """按归一化主题找出相关 thread 并重算。

    **必须传 cfg**：aggregate 用它区分「我方发出」与「对方来信」。
    漏传会让所有邮件都被当成对方来信，于是每个线程都被判成「等我处理」。
    用于 Foxmail 历史导入后的补算。
    """
    n = 0
    seen = set()
    for norm in subject_norms or []:
        if not norm or norm in seen:
            continue
        seen.add(norm)
        rows = repo.db.query(
            "SELECT DISTINCT thread_id FROM messages WHERE subject_norm = ? AND thread_id IS NOT NULL",
            (norm,))
        for r in rows:
            if r.get("thread_id"):
                aggregate(repo, r["thread_id"], cfg)
                n += 1
    return n


def thread_id_for_subject(repo, subject: str, cache: dict = None, create: bool = True):
    """给「按主题成线程」的邮件算一个稳定的 thread_id。

    create=True  ：找不到既有线程时按主题哈希**新建**一个（用于当前邮件）。
    create=False ：只复用既有线程，找不到返回 None（用于 Foxmail 历史邮件 —— 
                   不给历史主题凭空造线程，否则会多出几千个只有旧邮件的空线程）。
    """
    norm = util.normalize_subject(subject)
    if cache is not None and norm in cache:
        return cache[norm]
    tid = None
    if norm:
        row = repo.db.query_one(
            "SELECT thread_id FROM threads WHERE subject_norm = ? LIMIT 1", (norm,))
        if row and row.get("thread_id"):
            tid = row["thread_id"]
    if not tid:
        if not create:
            if cache is not None:
                cache[norm] = None
            return None
        tid = ("thr_" + util.sha256_text("subj:" + norm)[:20]) if norm \
            else ("thr_" + util.sha256_text("subj:__empty__")[:20])
    if cache is not None:
        cache[norm] = tid
    return tid


def backfill_history(repo, subject_norm: str, thread_id: str) -> int:
    """把同主题的 Foxmail 历史邮件拉进指定线程，作为起草上下文。

    只在「新邮件到达」时触发（见 attach_message），因此不会为历史主题凭空造线程。
    返回被并入的历史邮件数。

    **跳过与 IMAP 已有邮件重复的历史记录**：Foxmail 索引里含大量邮箱里还在的邮件
    （实测 6696 封历史中有 3590 封在 IMAP 里也存在）。它们作为「同一封邮件」的
    副本被并入线程后，会因为时间戳与真实邮件完全相同而干扰「最新一封」的判定
    —— 那会让线程状态漂移、把真实邮件从工作桶里挤掉。真正的历史价值在于
    「邮箱里已经没有了」的旧邮件，那才是要补的上下文。
    """
    if not subject_norm or not thread_id:
        return 0
    cur = repo.db.execute(
        "UPDATE messages SET thread_id = ? "
        "WHERE source = 'foxmail' AND thread_id IS NULL AND subject_norm = ? "
        "  AND NOT EXISTS ("
        "    SELECT 1 FROM messages m WHERE m.source = 'imap' AND m.deleted = 0 "
        "      AND m.subject_norm = messages.subject_norm "
        "      AND ABS(COALESCE(m.internal_ts,0) - COALESCE(messages.internal_ts,0)) < 2)",
        (thread_id, subject_norm))
    return cur.rowcount or 0


def rebuild_all(repo, cfg: dict = None, limit: int = 0) -> int:
    ids = repo.thread_ids()
    if limit:
        ids = ids[:limit]
    n = 0
    for tid in ids:
        try:
            aggregate(repo, tid, cfg)
            n += 1
        except Exception:
            continue
    return n


def attach_message(repo, row: dict, cfg: dict = None) -> dict:
    """给一封新入库的邮件分配 thread_id，然后重算该线程。

    返回 {"thread_id","action","reason"}；action 是 store 侧的实际结果
    （inserted / updated / unchanged），避免调用方为了拿到它而重复写一次库。

    若同一主题已有线程，优先复用既有 thread_id（主题归并），
    这样 Foxmail 历史邮件与 IMAP 当前邮件能合并到同一个 thread。
    """
    info = {"references_ids": row.get("references_ids") or [],
            "in_reply_to": row.get("in_reply_to") or "",
            "subject": row.get("subject") or "",
            "message_id": row.get("message_id") or ""}
    tid, reason = compute_thread_id(info)

    if not row.get("thread_id"):
        norm = util.normalize_subject(row.get("subject"))
        if norm:
            existing = repo.db.query_one(
                "SELECT thread_id FROM threads WHERE subject_norm = ? LIMIT 1", (norm,))
            if existing and existing.get("thread_id"):
                tid = existing["thread_id"]
                reason = "existing_subject:%s" % norm

    row["thread_id"] = tid
    action = repo.upsert_message(row)
    # 新邮件到达时，把同主题的历史邮件拉进这个线程当上下文（规范 §7）。
    # 放在这里而不是导入时：避免为历史主题凭空造几千个「只有一封旧邮件」的空线程。
    if (row.get("source") or "imap") != "foxmail":
        backfill_history(repo, util.normalize_subject(row.get("subject")), tid)
    aggregate(repo, tid, cfg)
    return {"thread_id": tid, "action": action, "reason": reason}


def prune_redundant_history(repo, log=None) -> dict:
    """把「邮箱里本来就还有」的 Foxmail 历史存根从线程里摘出去（幂等）。

    这些存根是同一封邮件的副本（同主题 + 时间戳相差 < 2 秒）。它们留在线程里
    没有任何上下文价值 —— 真实邮件本身就在，而且信息更全 ——
    却会因为时间戳同为最新而抢走「最新一封」的位置，干扰线程状态判定。

    **只摘 thread_id，不删数据**：记录仍在库里，搜索照常搜得到，
    需要时也能重新挂回去。历史邮件的价值在「邮箱里已经没有的旧邮件」。
    """
    cur = repo.db.execute(
        "UPDATE messages SET thread_id = NULL "
        "WHERE source = 'foxmail' AND thread_id IS NOT NULL AND EXISTS ("
        "  SELECT 1 FROM messages m WHERE m.source = 'imap' AND m.deleted = 0 "
        "    AND m.subject_norm = messages.subject_norm "
        "    AND ABS(COALESCE(m.internal_ts,0) - COALESCE(messages.internal_ts,0)) < 2)")
    n = cur.rowcount or 0
    if log and n:
        log("prune_redundant_history: 摘除 %d 封与 IMAP 重复的历史存根" % n)
    return {"ok": True, "detached": n}


def prune_history_only_threads(repo, cfg: dict = None, log=None) -> dict:
    """回收「只由 Foxmail 历史邮件构成」的线程。

    背景：早期版本的导入会为每个历史主题新建线程，于是 6696 封历史邮件
    凭空造出几千个「只有一封旧邮件」的线程（实测 5236 个），
    既污染线程列表，又让历史邮件看起来像「待处理」。

    正确模型是 **线程由当前邮件（IMAP）拥有，历史只作为上下文挂靠**：
      - 线程里只要还有至少一封 source='imap' 的邮件 -> 保留
      - 线程里全是 source='foxmail' -> 是导入副产物，回收

    回收 = 把该线程的历史邮件 thread_id 置空 + 删掉线程行。
    历史邮件本身**不删**（它们仍可按主题在下次新邮件到达时被回填）。
    幂等：重复执行不会再有可回收对象。
    """
    cfg = cfg or {}
    stale = [r["thread_id"] for r in repo.db.query(
        "SELECT t.thread_id FROM threads t "
        "WHERE NOT EXISTS (SELECT 1 FROM messages m "
        "                  WHERE m.thread_id = t.thread_id AND m.source = 'imap')")]
    if not stale:
        return {"ok": True, "threads_removed": 0, "messages_detached": 0,
                "candidates_removed": 0, "silent": True}

    detached = 0
    # 分批，避免 SQL 变量上限（历史线程可能上千）
    for i in range(0, len(stale), 400):
        chunk = stale[i:i + 400]
        ph = ",".join("?" * len(chunk))
        detached += repo.db.execute(
            "UPDATE messages SET thread_id = NULL WHERE thread_id IN (%s)" % ph,
            tuple(chunk)).rowcount or 0
        repo.db.execute("DELETE FROM threads WHERE thread_id IN (%s)" % ph, tuple(chunk))

    # 历史邮件的候选也是同一次错误导入的产物：候选只应对当前邮件产生
    cand = repo.db.execute(
        "DELETE FROM candidates WHERE message_id IN "
        "(SELECT message_id FROM messages WHERE source = 'foxmail')").rowcount or 0

    if log:
        log("prune_history_only_threads: 回收线程 %d，摘除历史邮件 %d，清理候选 %d"
            % (len(stale), detached, cand))
    return {"ok": True, "threads_removed": len(stale), "messages_detached": detached,
            "candidates_removed": cand, "silent": False}


def repair(repo, cfg: dict = None, log=None) -> dict:
    """线程层的整体自愈入口（幂等）。

    顺序有意义：先摘掉「与 IMAP 重复的历史存根」（否则它们会干扰下一步的
    线程归属判断），再回收「只剩下历史邮件的空线程」。
    """
    out = prune_redundant_history(repo, log=log)
    out.update(prune_history_only_threads(repo, cfg, log=log))
    return out


def thread_view(repo, thread_id: str, cfg: dict = None) -> dict:
    """给 UI / Worker 契约用的线程详情。"""
    t = repo.get_thread(thread_id)
    if not t:
        return {}
    msgs = repo.messages_for_thread(thread_id)
    self_set = _self_set(cfg or {})
    out_msgs = []
    for m in msgs:
        out_msgs.append({
            "message_id": m["message_id"],
            "uid": m.get("uid"),
            "folder": m.get("folder"),
            "subject": m.get("subject"),
            "from_name": m.get("from_name"),
            "from_addr": m.get("from_addr"),
            "to_addrs": util.parse_recipients(m.get("to_addrs") or ""),
            "date_iso": m.get("date_iso"),
            "direction": "out" if (m.get("from_addr") or "").lower() in self_set else "in",
            "unread": bool(m.get("unread")),
            "has_attachments": bool(m.get("has_attachments")),
            "attachment_names": util.parse_recipients(m.get("attachment_names") or "") or [],
            "snippet": m.get("snippet"),
            "classification": m.get("classification"),
            "workflow_state": m.get("workflow_state"),
            "body_text": m.get("body_text") or "",
        })
    return {"thread": t, "messages": out_msgs}
