"""回复起草队列

为什么用文件队列，而不是应用直接调 WorkBuddy：
  实测 WorkBuddy CLI 冷启动 2 分半没有任何返回，做成「点一下等结果」不成立。
  所以改成异步交接：
    1. 应用把「请帮我起草回复」写进 reply-queue/pending/
    2. WorkBuddy 那一侧（我）看到后，读完整往来线程 → 起草 → 写入草稿箱
       → 再把草稿正文写进 reply-queue/done/
    3. 应用轮询到 done，就把草稿填进编辑器给你改
  你始终在「改稿」这一步；系统只写草稿箱，绝不自动发送。

目录结构：
  reply-queue/pending/<id>.json   待起草（含生成中 / 失败的，靠 status 字段区分）
  reply-queue/done/<id>.json      已起草（含草稿正文），应用取走后删除

每条记录的状态（status）：
  queued      待生成（排队中）
  generating  生成中（WorkBuddy 已经拿走，正在读往来线程 / 起草）
  done        已完成（已移到 done/ 目录，含草稿正文）
  failed      失败（可重试；attempts 记着重试了几次）

来源（source）：
  manual  用户在邮件上亲手点的「让 WorkBuddy 起草」—— 优先级最高，插到队首
  auto    定时任务按规则自动挑出来预生成的 —— 排在手动的后面

优先级（priority）：0 = 手动加急（最高），1 = 自动入队。pending() 按它排序，
所以「手动点的」永远排在「自动攒的」前面。
"""

import json
import os
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
QROOT = os.path.join(HERE, "reply-queue")
PENDING = os.path.join(QROOT, "pending")
DONE = os.path.join(QROOT, "done")
# 归档区。待办和已就绪的草稿如果一直没人管，会无限堆下去 ——
# 定时任务每 6 小时最多自动挑 5 封，用户从不点开的话 done/ 一天就多 20 份。
# 超期的（见 DONE_TTL / FAILED_TTL）统一挪到这里：**是搬家不是删除**，
# 出问题还能翻回来，队列本身保持干净、一眼看得出「现在到底有几件没办」。
EXPIRED = os.path.join(QROOT, "expired")

# 事件日志 —— 排队 / 开始生成 / 交回 / 超时回滚，每一步都留痕。
# 以前出了问题只能干瞪眼：界面说「已加急」，但没人知道任务到底有没有被拿走。
LOG = os.path.join(QROOT, "_replyq.log")

# 状态取值。done 目录里的一律视为 done（它已经不在 pending 里了）。
QUEUED, GENERATING, FAILED = "queued", "generating", "failed"
PRIO_MANUAL, PRIO_AUTO = 0, 1

# 卡死判定（秒）：
#   generating 超过 GEN_TIMEOUT 还没交回 → 认定 AI 那边中断了，自动回滚重新排队。
#     不回滚的话这条会永远停在「生成中」，而定时任务被明确告知「生成中的不要重复处理」
#     —— 于是这封邮件被永久遗忘。这是最危险的一种卡死。
#   queued 超过 URGENT_STALL / AUTO_STALL 还没被拿走 → 只是告警（不擅自改状态），
#     让界面显示「已等 N 分钟」，别再拿「几分钟内就好」糊弄人。
GEN_TIMEOUT = 15 * 60
URGENT_STALL = 10 * 60
AUTO_STALL = 90 * 60

# 归档阈值（秒）：过了就挪进 expired/，不再占着队列
DONE_TTL = 7 * 86400       # 草稿就绪 7 天还没被取走
FAILED_TTL = 7 * 86400     # 失败 7 天没人重试
# 积压闸门：done/ 里没人取的草稿超过这个数，就别再自动入队了
# （否则「自动挑 → 没人看 → 继续挑」会越滚越多）
AUTOPICK_MAX_BACKLOG = 5


def log(event, rid="", extra=""):
    """每一步都写一行日志。排查「点了没反应」时这是唯一的证据。"""
    line = "%s | %-14s | %-24s | %s" % (
        time.strftime("%Y-%m-%d %H:%M:%S"), event, str(rid or "-"), extra or "")
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        _rotate()
    except OSError:
        pass
    return line


def _rotate(max_bytes=512 * 1024, keep=1500):
    """日志别无限涨：超了就只留最后 keep 行"""
    try:
        if os.path.getsize(LOG) <= max_bytes:
            return
        with open(LOG, encoding="utf-8") as f:
            lines = f.read().splitlines()
        with open(LOG, "w", encoding="utf-8") as f:
            f.write("\n".join(lines[-keep:]) + "\n")
    except OSError:
        pass


def read_log(limit=200):
    if not os.path.exists(LOG):
        return []
    try:
        with open(LOG, encoding="utf-8") as f:
            return f.read().splitlines()[-limit:]
    except OSError:
        return []


def _ts(obj):
    """记录的时间戳：优先看 updated，没有就退回文件修改时间

    文件时间的兜底要在 pending/done/expired **三个目录都找一遍** ——
    只查 pending 的话，已完成的条目拿不到时间，age 算出来是 0，
    「草稿放了 7 天没人取」这种归档规则就永远不触发。
    """
    s = obj.get("updated") or obj.get("created") or ""
    try:
        return time.mktime(time.strptime(s, "%Y-%m-%d %H:%M:%S"))
    except (ValueError, TypeError, OverflowError):
        pass
    name = str(obj.get("_file") or (str(obj.get("id") or "") + ".json"))
    for folder in (PENDING, DONE, EXPIRED):
        try:
            return os.path.getmtime(os.path.join(folder, name))
        except OSError:
            continue
    return 0.0


def age_sec(obj):
    return max(0, int(time.time() - _ts(obj)))


def stall_after(obj):
    """这条等多久算「不对劲」"""
    if obj.get("status") == GENERATING:
        return GEN_TIMEOUT
    # 又是 `0 会被 or 吃掉` 的坑：手动加急的 priority 就是 0，
    # 写 `int(x.get("priority") or 1)` 会把它变成 1，于是加急件用上了
    # 自动件 90 分钟的宽松阈值 —— 等于告警对最该告警的那种失效。
    v = obj.get("priority")
    v = PRIO_AUTO if v is None else int(v)
    return URGENT_STALL if v == PRIO_MANUAL else AUTO_STALL


def is_stalled(obj):
    """卡住了吗：排队太久没人拿，或者生成了太久没交回"""
    if obj.get("status") not in (QUEUED, GENERATING):
        return False
    return age_sec(obj) > stall_after(obj)


def sweep():
    """看门狗：把卡在「生成中」的记录救回来

    AI 那边标记了 generating 之后如果中断（进程被杀 / 网络断了 / 超时），
    记录会永远停在 generating —— 而定时任务被明确要求「生成中的不要碰」，
    于是这封邮件再也不会有人处理。这里按时间兜底：超时就回滚成待生成。
    """
    saved = []
    for r in _read_all(PENDING):
        if r.get("status") != GENERATING or age_sec(r) <= GEN_TIMEOUT:
            continue
        rid = r.get("id")
        set_status(rid, QUEUED,
                   "生成超时（%d 分钟没交回），已自动重新排队" % (GEN_TIMEOUT // 60),
                   bump_attempt=True)
        log("sweep-requeue", rid, "卡在 generating %d 秒，已回滚 queued" % age_sec(r))
        _last_stall_log.pop(rid, None)
        saved.append(rid)
    return saved


# 同一个条目反复喊「卡住了」没意义（看门狗每 30 秒跑一次），
# 这里记下上次喊的时间，同一个条目 10 分钟最多喊一次。
_last_stall_log = {}


def log_stalled(r):
    rid = str(r.get("id") or "")
    now = time.time()
    if now - _last_stall_log.get(rid, 0) < 600:
        return False
    _last_stall_log[rid] = now
    log("stall", rid, "已等 %d 秒（阈值 %d）status=%s source=%s" % (
        age_sec(r), stall_after(r), r.get("status"), r.get("source")))
    return True


def clear_stall_log(rid):
    _last_stall_log.pop(str(rid or ""), None)


def _ensure():
    os.makedirs(PENDING, exist_ok=True)
    os.makedirs(DONE, exist_ok=True)
    os.makedirs(EXPIRED, exist_ok=True)


def _norm(obj):
    """给旧记录补上新字段 —— 升级前入队的那些没有 status/source，
    读出来按「自动入队、待生成」处理，不至于让老数据变成幽灵条目。"""
    obj.setdefault("status", QUEUED)
    obj.setdefault("source", "auto")
    obj.setdefault("priority", PRIO_AUTO)
    obj.setdefault("attempts", 0)
    obj.setdefault("last_error", "")
    obj.setdefault("updated", obj.get("created", ""))
    return obj


def _read_all(folder):
    _ensure()
    out = []
    for name in sorted(os.listdir(folder)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(folder, name), encoding="utf-8") as f:
                obj = json.load(f)
            obj["_file"] = name
            out.append(_norm(obj))
        except (OSError, ValueError):
            continue
    return out


def enqueue(req, source="manual", priority=None):
    """应用侧：登记一封「请起草回复」

    source: manual（用户亲手点的）/ auto（定时任务自动挑的）。
    手动的一律 priority=0，会插到队列最前面。
    """
    _ensure()
    rid = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    prio = PRIO_MANUAL if source == "manual" else PRIO_AUTO
    if priority is not None:
        prio = int(priority)
    obj = {
        "id": rid,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "status": QUEUED,
        "source": source,
        "priority": prio,
        "attempts": 0,
        "last_error": "",
        "folder": req.get("folder") or "INBOX",
        "uid": str(req.get("uid") or ""),
        "subject": req.get("subject") or "",
        "from": req.get("from") or "",
        "date": req.get("date") or "",
        "body": req.get("body") or "",
        "note": req.get("note") or "",
    }
    _write(PENDING, rid, obj)
    log("enqueue", rid, "source=%s priority=%s uid=%s from=%s" % (
        source, prio, obj["uid"], (obj["from"] or "")[:40]))
    return rid


def _write(folder, rid, obj):
    obj["updated"] = time.strftime("%Y-%m-%d %H:%M:%S")
    with open(os.path.join(folder, str(rid) + ".json"), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)


def _load(rid):
    p = os.path.join(PENDING, str(rid) + ".json")
    if not os.path.exists(p):
        return None
    try:
        with open(p, encoding="utf-8") as f:
            return _norm(json.load(f))
    except (OSError, ValueError):
        return None


def find_pending(folder, uid, subject="", sender=""):
    """查重：这封邮件是不是已经在队列里了？

    用途：用户手滑连点两下「让 WorkBuddy 起草」，如果每次都新建一份，
    pending 里就会出现多份同一封邮件 —— AI 要重复起草（白等），
    界面上的「N 封等起草」也会虚高（点 3 下显示 3 封，其实只有 1 封）。

    匹配优先用 uid（最准）；uid 为空（比如来自本地索引的邮件）时退回
    subject + from 比对。
    """
    folder = str(folder or "")
    uid = str(uid or "")
    for r in pending():
        if uid:
            if str(r.get("uid") or "") == uid and str(r.get("folder") or "") == folder:
                return r
        elif subject and str(r.get("subject") or "") == str(subject) \
                and str(r.get("from") or "") == str(sender or ""):
            return r
    return None


def cancel_pending(rid):
    """撤回：只删 pending 里那份，**不动 done**。

    跟 consume() 的区别：consume 会把 done 里的草稿也一起清掉（那是"取走"），
    撤回只是"我点错了，别起草了"，已经起草好的草稿不该被牵连。
    """
    p = os.path.join(PENDING, str(rid) + ".json")
    if not os.path.exists(p):
        return False
    try:
        os.remove(p)
        return True
    except OSError:
        return False


def pending():
    """待处理的队列，**已经按优先级排好序**。

    排序：priority（手动加急 0 在前）→ 入队时间。自动生成的一批永远排在
    用户亲手点的后面。生成中 / 失败的记录也在里面，调用方按 status 分开看。
    """
    out = _read_all(PENDING)
    # 注意别写成 `r.get("priority") or 9`：手动加急的 priority 就是 0，
    # 而 0 是假值 —— 那样手动的会被当成 9 排到**最后**，正好反了。
    def prio(r):
        v = r.get("priority")
        return 9 if v is None else int(v)

    out.sort(key=lambda r: (prio(r), r.get("created") or ""))
    return out


def waiting():
    """真正还在等着的（queued）—— 定时任务要处理的就是这批"""
    return [r for r in pending() if r.get("status") == QUEUED]


def failed():
    return [r for r in pending() if r.get("status") == FAILED]


def stalled_list():
    """卡住的那些（等太久没人拿 / 生成太久没交回）—— 界面要标黄提醒"""
    return [r for r in pending() if is_stalled(r)]


def expired():
    """归档区。默认只读不展示，出问题可以翻回来找。"""
    return _read_all(EXPIRED)


def stats():
    """一句话讲清「现在有几件事」—— 界面和排查都用它"""
    p = pending()
    return {
        "pending": len([r for r in p if r.get("status") == QUEUED]),
        "generating": len([r for r in p if r.get("status") == GENERATING]),
        "failed": len([r for r in p if r.get("status") == FAILED]),
        "stalled": len([r for r in p if is_stalled(r)]),
        "done": len(done()),
        "expired": len(expired()),
    }


def expire(rid, folder, why=""):
    """把一条从 pending/ 或 done/ 挪进 expired/ —— **搬家，不删除**

    为什么不直接删：草稿是 AI 写的、可能用户过几天才想起来要看；
    失败记录是排查线索。归档只花几 KB，删掉就真没了。
    """
    _ensure()
    src = os.path.join(folder, str(rid) + ".json")
    if not os.path.exists(src):
        return False
    try:
        with open(src, encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, ValueError):
        obj = {"id": rid}
    obj["expiredAt"] = time.strftime("%Y-%m-%d %H:%M:%S")
    obj["expiredWhy"] = why
    obj.pop("_file", None)
    try:
        with open(os.path.join(EXPIRED, str(rid) + ".json"), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=2)
        os.remove(src)
    except OSError as e:
        log("expire-fail", rid, repr(e))
        return False
    log("expire", rid, why or "超过保留期")
    return True


def sweep_stale(done_ttl=DONE_TTL, failed_ttl=FAILED_TTL):
    """定期把过期的条目挪进归档区。返回名单，供日志/接口展示。

    两类会过期：
      · done/ 里超过 done_ttl 还没被取走的草稿（用户始终没点开）
      · pending/ 里 failed 且超过 failed_ttl 的（没人重试，也修不好了）
    **不动 queued / generating** —— 那些是还在处理中的，宁可留久一点。
    """
    out = []
    for r in done():
        if age_sec(r) > done_ttl:
            if expire(r.get("id"), DONE, "起草好之后 %d 天没人取走" % (done_ttl // 86400)):
                out.append(r.get("id"))
    for r in pending():
        if r.get("status") != FAILED:
            continue
        if age_sec(r) <= failed_ttl:
            continue
        if expire(r.get("id"), PENDING, "失败后 %d 天没人重试（试过 %s 次）"
                  % (failed_ttl // 86400, r.get("attempts") or 0)):
            out.append(r.get("id"))
    return out


def set_status(rid, status, error="", bump_attempt=False):
    """改状态。失败原因写进 last_error，前端要显示给用户看。"""
    obj = _load(rid)
    if not obj:
        return False
    obj.pop("_file", None)
    obj["status"] = status
    obj["last_error"] = error or ""
    if bump_attempt:
        obj["attempts"] = int(obj.get("attempts") or 0) + 1
    _write(PENDING, rid, obj)
    log("status->%s" % status, rid, (error or "")[:80])
    return True


def mark_generating(rid):
    """WorkBuddy 开始起草 —— 前端据此把按钮切成「生成中…」"""
    return set_status(rid, GENERATING)


def mark_failed(rid, error):
    """起草失败。记录留在队列里，用户可以点「重试」把它放回 queued。"""
    return set_status(rid, FAILED, error, bump_attempt=True)


def retry(rid):
    """重试：失败 → 重新排队（保留 attempts，方便判断重试了几次）"""
    obj = _load(rid)
    if not obj:
        return False
    return set_status(rid, QUEUED)


def promote(rid, source="manual"):
    """提优先级：用户后来又亲手点了这封 —— 插到队首，并按手动来源记。

    不去重、不新建：同一封只有一条记录，只是把它从「自动攒的」变成「我要的」。
    失败状态一并复位成 queued —— 用户点重试/加急的意图就是「再来一次」。
    """
    obj = _load(rid)
    if not obj:
        return False
    obj.pop("_file", None)
    obj["source"] = source
    obj["priority"] = PRIO_MANUAL if source == "manual" else int(obj.get("priority") or PRIO_AUTO)
    obj["last_error"] = ""
    # 已经在生成中的**不要**改回 queued —— 那会让定时任务以为它还没人处理，
    # 于是同一封被起草两遍。生成中就让它继续生成，只把优先级提上去。
    if obj.get("status") != GENERATING:
        obj["status"] = QUEUED
    _write(PENDING, rid, obj)
    log("promote", rid, "提到队首 source=%s（原状态 %s）" % (source, obj.get("status")))
    return True


def done():
    return _read_all(DONE)


def complete(rid, draft):
    """WorkBuddy 侧：起草完成，把草稿交回应用

    draft: {"to":..., "subject":..., "body":..., "note":...}

    返回 (rid, 提示)。以前这里静默吞掉一切：id 写错 / 记录已被撤回，
    草稿照样落进 done/，但**没有 uid** —— 前端靠 uid 配对做自动回传，
    配不上就表现为「草稿好了，但编辑器里什么都没有」。现在这种情况会记进日志。
    """
    _ensure()
    src = os.path.join(PENDING, rid + ".json")
    obj = {"id": rid, "finished": time.strftime("%Y-%m-%d %H:%M:%S")}
    orphan = not os.path.exists(src)
    if not orphan:
        try:
            with open(src, encoding="utf-8") as f:
                obj.update(json.load(f))
        except (OSError, ValueError):
            pass
        os.remove(src)
    obj["draft"] = draft
    obj["status"] = "done"
    obj["last_error"] = ""
    with open(os.path.join(DONE, rid + ".json"), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
    log("complete", rid, "uid=%s body=%d字%s" % (
        obj.get("uid", ""), len((draft or {}).get("body") or ""),
        " ⚠ pending 里没有这条（id 写错或已被撤回）" if orphan else ""))
    return rid


def consume(rid):
    """应用侧：草稿已取走，清掉

    顺带清掉同 id 的 pending（如果还在）—— 正常情况 complete() 已经删了，
    但万一哪天有残留，不清掉会让这封邮件永远挂在「等起草」里出不去。
    """
    hit = False
    for folder in (DONE, PENDING, EXPIRED):
        p = os.path.join(folder, rid + ".json")
        if os.path.exists(p):
            try:
                os.remove(p)
                hit = True
            except OSError:
                pass
    return hit


if __name__ == "__main__":
    import sys

    cmd = sys.argv[1] if len(sys.argv) > 1 else "list"
    if cmd == "list":
        p, d = pending(), done()
        s = stats()
        print("队列 %d 条（等待 %d · 生成中 %d · 失败 %d）｜已就绪 %d 封｜已归档 %d 条" % (
            len(p), s["pending"], s["generating"], s["failed"], s["done"], s["expired"]))
        for r in p:
            print("  [%s] %-10s %-6s x%s | %s | %s" % (
                r["id"], r.get("status", ""), r.get("source", ""), r.get("attempts", 0),
                r.get("from", "")[:28], r.get("subject", "")[:40]))
            if r.get("last_error"):
                print("        失败原因：%s" % r["last_error"])
        for r in d:
            print("  [done] %s | %s" % (r["id"], r.get("subject", "")[:50]))
    elif cmd == "sweep":
        got = sweep()
        stale = sweep_stale()
        print("看门狗回滚 %d 条" % len(got), got)
        print("归档 %d 条" % len(stale), stale)
    elif cmd == "stats":
        print(json.dumps(stats(), ensure_ascii=False))
    elif cmd == "expire" and len(sys.argv) > 2:
        rid = sys.argv[2]
        folder = DONE if os.path.exists(os.path.join(DONE, rid + ".json")) else PENDING
        print("已归档：", expire(rid, folder, sys.argv[3] if len(sys.argv) > 3 else "手动归档"))
    elif cmd == "log":
        for line in read_log(int(sys.argv[2]) if len(sys.argv) > 2 else 60):
            print(line)
    elif cmd == "waiting":
        for r in waiting():
            print(r["id"], r.get("source"), r.get("uid"), r.get("subject", "")[:40])
    elif cmd in ("generating", "fail", "retry", "promote") and len(sys.argv) > 2:
        rid = sys.argv[2]
        if cmd == "generating":
            print("已置为生成中：", mark_generating(rid))
        elif cmd == "fail":
            print("已标记失败：", mark_failed(rid, sys.argv[3] if len(sys.argv) > 3 else "未知原因"))
        elif cmd == "retry":
            print("已重新排队：", retry(rid))
        else:
            print("已提到队首：", promote(rid))
    elif cmd == "show" and len(sys.argv) > 2:
        for r in pending():
            if r["id"] == sys.argv[2]:
                print(json.dumps(r, ensure_ascii=False, indent=2))
    elif cmd == "done" and len(sys.argv) > 2:
        # done <id> <to> <subject> <body文件路径>
        rid, to, subj, bodyfile = sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5]
        with open(bodyfile, encoding="utf-8") as f:
            body = f.read()
        print("已交回应用：", complete(rid, {"to": to, "subject": subj, "body": body}))
    else:
        print(__doc__)
