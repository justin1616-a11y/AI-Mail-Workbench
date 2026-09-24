# -*- coding: utf-8 -*-
"""本地 Scheduler —— **一个**线程管理全部定时事务（规范 §16 / §17 / §18）。

这是 V2 用来替换「多个 WorkBuddy automation 轮询」的核心机制：

    V1: 整点加急扫描 + 半点加急扫描 + 6 小时自动起草 + 每日日报
        -> 4 个 automation，全部要唤起 LLM/会话，成本高且不可靠

    V2: 一个本地线程（默认 30 秒 tick）负责
        * snooze 到期唤醒
        * follow-up 到期提醒
        * 周期性 Recovery（队列自愈）
        * 候选过期清理
        * 预计算 Daily Brief 快照
    只在需要语言理解时（起草/摘要）才去找 WorkBuddy。

scheduler 是**纯本地、确定性**的，不调用任何模型。
"""
from __future__ import annotations

import threading
import time

from .. import util
from ..constants import (
    BUCKET_ACTION_REQUIRED, BUCKET_BROADCAST, BUCKET_DRAFT_READY, BUCKET_DONE_TODAY,
    BUCKET_SNOOZED, BUCKET_WAITING_FOR_REPLY, CLASS_IMPORTANT, CLASS_REPLY,
)
from ..intelligence import rule_engine

DEFAULT_TICK = 30


class Scheduler:
    def __init__(self, repo, cfg: dict, on_event=None, log=None):
        self.repo = repo
        self.cfg = cfg
        self.on_event = on_event
        self.log = log
        self.stop_event = threading.Event()
        self.thread = None
        self.last_tick_at = None
        self.tick_count = 0
        self.last_error = ""
        self.recovery_interval = 1800          # 本地 Recovery 间隔（秒）
        self.candidate_expire_interval = 6 * 3600
        self.brief_interval = 1800

    # ------------------------------------------------------------------
    def start(self):
        if not self.cfg.get("scheduler_enabled", True):
            if self.log:
                self.log("scheduler 已禁用（scheduler_enabled=false）")
            return
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="mail-scheduler", daemon=True)
        self.thread.start()

    def stop(self, timeout: float = 5.0):
        self.stop_event.set()
        t = self.thread
        if t and t.is_alive():
            t.join(timeout=timeout)

    def status(self) -> dict:
        return {
            "running": bool(self.thread and self.thread.is_alive()),
            "tick_seconds": int(self.cfg.get("scheduler_tick_seconds", DEFAULT_TICK)),
            "tick_count": self.tick_count,
            "last_tick_at": self.last_tick_at,
            "last_error": self.last_error,
            "jobs_waiting": {"snoozes": len(self.repo.active_snoozes(limit=1000))},
        }

    # ------------------------------------------------------------------
    def _emit(self, event: dict):
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                pass

    def _run(self):
        tick = max(5, int(self.cfg.get("scheduler_tick_seconds", DEFAULT_TICK)))
        while not self.stop_event.is_set():
            t0 = time.time()
            try:
                self.tick()
                self.last_error = ""
            except Exception as e:
                self.last_error = str(e)
                if self.log:
                    self.log("scheduler tick 异常：%s" % e)
            elapsed = time.time() - t0
            self.stop_event.wait(max(1.0, tick - elapsed))

    # ------------------------------------------------------------------
    def tick(self) -> dict:
        """执行一次调度。所有任务都是幂等且廉价的。"""
        out = {"at": util.now_iso(), "snoozes_woken": 0, "followups_due": 0,
               "recovery": None, "candidates_expired": 0, "brief": False}
        self.tick_count += 1
        self.last_tick_at = out["at"]

        # 1) snooze 到期
        from .snooze_manager import SnoozeManager
        sm = SnoozeManager(self.repo, self.cfg, log=self.log)
        woken = sm.wake_due(on_event=self._emit)
        out["snoozes_woken"] = len(woken)
        if woken:
            self._emit({"type": "snooze_wake_batch", "count": len(woken),
                        "items": [{"message_id": w["message_id"],
                                   "thread_id": w["thread_id"]} for w in woken]})

        # 2) follow-up 到期
        from .followup_manager import FollowUpManager
        fm = FollowUpManager(self.repo, self.cfg, log=self.log)
        due = fm.mark_due(on_event=self._emit)
        out["followups_due"] = len(due)

        # 3) 周期性 Recovery
        if self._due("last_local_recovery", self.recovery_interval):
            from ..draft import recovery as rcv
            res = rcv.run_recovery(self.repo, self.cfg, log=self.log)
            out["recovery"] = res
            self.repo.kv_set("last_local_recovery", {"at": util.now_iso(),
                                                     "actions": res.get("actions", 0)})
            if res.get("actions"):
                self._emit({"type": "recovery", "result": res})

        # 4) 候选过期（低频）
        if self._due("last_candidate_expire", self.candidate_expire_interval):
            try:
                from .candidate_detector import CandidateDetector
                n = CandidateDetector(self.repo, self.cfg, log=self.log).expire_stale()
                out["candidates_expired"] = n
                self.repo.kv_set("last_candidate_expire", {"at": util.now_iso(), "n": n})
            except Exception as e:
                if self.log:
                    self.log("候选过期失败：%s" % e)

        # 5) 预计算 Daily Brief 快照（纯本地，供 8:00 automation 直接读取）
        if self._due("last_brief_build", self.brief_interval):
            try:
                from . import brief as brief_mod
                snap = brief_mod.build(self.repo, self.cfg)
                brief_mod.store_snapshot(self.repo, snap)
                out["brief"] = True
            except Exception as e:
                if self.log:
                    self.log("brief 预计算失败：%s" % e)

        return out

    def _due(self, kv_key: str, interval_seconds: float) -> bool:
        last = self.repo.kv_get(kv_key)
        if not last or not last.get("at"):
            return True
        return util.hours_between(last["at"], util.now_iso()) * 3600 >= interval_seconds


# --------------------------------------------------------------------------
# Daily Brief（规范 §18）—— 本地生成，必要时才让 WorkBuddy 写自然语言摘要
# --------------------------------------------------------------------------
def build_brief_payload(repo, cfg: dict, classify_new: bool = True) -> dict:
    """生成结构化 Daily Brief。

    **数字的唯一来源必须是 buckets.compute()** —— 工作桶首页和早报说的是同一件事，
    两处各写一套 SQL 就必然对不上。这里曾经自己写了套查询（还顺手带了 LIMIT 50），
    结果早报说「50 封需要你处理」而首页说「83 封」，用户无法判断该信哪个。
    因此本函数只做三件事：取桶 → 加「临近截止」→ 渲染文本。
    """
    from . import buckets as bucketmod

    now = util.now_iso()
    # limit=0：拿**全量**桶，headline 的统计才准；展示用的列表在下面各自切片。
    # （只在切过片的列表上做统计，会把「83 封待处理里有几封要回」算成 0 —— 
    #  因为前 20 条恰好都是重点类。）
    data = bucketmod.compute(repo, cfg, limit=0)
    counts = data["counts"]
    action_all = data["buckets"].get(BUCKET_ACTION_REQUIRED, [])
    waiting_all = data["buckets"].get(BUCKET_WAITING_FOR_REPLY, [])
    draft_all = data["buckets"].get(BUCKET_DRAFT_READY, [])
    snooze_all = data["buckets"].get(BUCKET_SNOOZED, [])
    done_all = data["buckets"].get(BUCKET_DONE_TODAY, [])

    due_fn = repo.due_followups(now, limit=50)

    # 即将到期：从待处理线程的最新正文里抽日期（本地正则，不调 LLM）
    upcoming = []
    from ..intelligence import fact_extractor as fx
    for it in action_all:
        mid = it.get("message_id")
        if not mid:
            continue
        row = repo.get_message(mid) or {}
        for d in fx.extract_deadlines(row.get("body_text") or "", mid, it.get("subject")):
            for ds in d.get("dates") or []:
                dt = util.parse_iso(ds + "T12:00:00+08:00")
                if not dt:
                    continue
                hours = (dt - util.parse_iso(now)).total_seconds() / 3600
                if -24 <= hours <= 48:
                    upcoming.append({
                        "subject": it.get("subject"), "date": ds,
                        "hours_left": round(hours, 1),
                        "text": (d.get("text") or "")[:120],
                        "message_id": mid,
                    })
    upcoming.sort(key=lambda x: x["hours_left"])

    high = [i for i in action_all if (i.get("classification") or "") == CLASS_IMPORTANT]
    drafts_ready = [i for i in draft_all
                    if i.get("job_status") in ("ready", "reviewing", "approved")]
    drafts_waiting = [i for i in draft_all if i.get("job_status") == "needs_input"]

    payload = {
        "generated_at": now,
        "headline": {
            "needs_attention": counts[BUCKET_ACTION_REQUIRED],
            "replies_required": len([i for i in action_all
                                     if i.get("classification") == CLASS_REPLY]),
            "drafts_ready": len(drafts_ready),
            "drafts_waiting_input": len(drafts_waiting),
            "waiting_for_reply": counts[BUCKET_WAITING_FOR_REPLY],
            "snoozed": counts[BUCKET_SNOOZED],
            "done_today": counts[BUCKET_DONE_TODAY],
            "followups_due": len(due_fn),
            "broadcast": counts.get(BUCKET_BROADCAST, 0),
        },
        "high_priority": [{"message_id": i["message_id"], "thread_id": i["thread_id"],
                           "subject": i["subject"], "from": i["from_name"] or i["from_addr"],
                           "date": i["date_iso"]} for i in high[:10]],
        "action_required": [{"message_id": i["message_id"], "thread_id": i["thread_id"],
                             "subject": i["subject"],
                             "from": i["from_name"] or i["from_addr"],
                             "classification": i["classification"],
                             "date": i["date_iso"]} for i in action_all[:20]],
        "drafts": [{"job_id": i["job_id"], "subject": i["subject"],
                    "status": i["job_status"], "mode": i.get("draft_mode"),
                    "has_draft": i.get("has_draft")} for i in draft_all[:10]],
        "waiting_for_reply": [{"thread_id": i["thread_id"], "subject": i["subject"],
                               "latest_ts": i["latest_ts"]} for i in waiting_all[:10]],
        "snoozed": [{"message_id": i["message_id"], "thread_id": i["thread_id"],
                     "wake_at": i.get("wake_at"), "subject": i["subject"]}
                    for i in snooze_all[:10]],
        "upcoming_deadlines": upcoming[:10],
        "followups_due": [{"thread_id": f.get("thread_id"), "due_at": f.get("due_at"),
                           "note": f.get("note")} for f in due_fn[:10]],
        "done_today": [{"message_id": i["message_id"], "subject": i["subject"]}
                       for i in done_all[:10]],
        # 把工作集信息一并带出，UI 才能解释「为什么只有这么多」
        "workingset": data.get("workingset"),
    }
    payload["text"] = render_text(payload)
    return payload


def render_text(b) -> str:
    """把结构化 brief 渲染成手机一屏能看完的纯文本（规范 §18 的示例格式）。"""
    h = b["headline"]
    lines = ["%s · 邮件早报" % util.parse_iso(b["generated_at"]).strftime("%m-%d %H:%M"), ""]
    lines.append("%d 封需要你处理 · %d 封要回 · %d 份草稿待审 · %d 封等你/等对方"
                 % (h["needs_attention"], h["replies_required"], h["drafts_ready"],
                    h["waiting_for_reply"]))
    if h["drafts_waiting_input"]:
        lines.append("⚠ %d 份草稿缺你的信息，补齐后即可生成" % h["drafts_waiting_input"])
    if h["followups_due"]:
        lines.append("⏰ %d 个跟进到期" % h["followups_due"])
    if h.get("broadcast"):
        lines.append("📢 另有 %d 封群发通知（只知会，不用处理）" % h["broadcast"])
    lines.append("")
    if b["high_priority"]:
        lines.append("★ 重点")
        for m in b["high_priority"][:5]:
            lines.append("  %s — %s" % ((m["from"] or "")[:14], (m["subject"] or "")[:34]))
        lines.append("")
    if b["upcoming_deadlines"]:
        lines.append("⏳ 临近截止")
        for d in b["upcoming_deadlines"][:4]:
            lines.append("  %s（%s，剩 %.1fh）" % ((d["subject"] or "")[:22], d["date"], d["hours_left"]))
        lines.append("")
    if b["drafts"]:
        lines.append("✎ 草稿")
        for j in b["drafts"][:5]:
            lines.append("  [%s] %s" % (j["status"], (j["subject"] or "")[:32]))
        lines.append("")
    if b["followups_due"]:
        lines.append("↻ 该跟进")
        for f in b["followups_due"][:4]:
            lines.append("  %s" % (f.get("note") or f.get("thread_id")))
        lines.append("")
    if h["done_today"]:
        lines.append("✓ 今天已完成 %d 封" % h["done_today"])
    return "\n".join(lines).strip()
