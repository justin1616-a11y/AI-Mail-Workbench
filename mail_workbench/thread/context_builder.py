# -*- coding: utf-8 -*-
"""MailContextBuilder —— 为 WorkBuddy 构造**最小充分上下文**（规范 §8）。

铁律：**禁止把整个邮箱交给 LLM**。
输入只有 message_id / thread_id，输出一个 MailContextPackage：

    {
      "current_message": {...},
      "thread_summary": "...",          # 本地生成，不是 LLM
      "recent_messages": [...],         # 默认最近 6 封（上限 10）
      "participants": [...],
      "attachments": [...],
      "previous_commitments": [...],    # 我方历史承诺（禁止翻悔/自相矛盾）
      "open_questions": [...],          # 对方未获答复的问题
      "known_deadlines": [...],
      "user_notes": [...]
    }

「最小充分」的两层含义：
  * 减少 token —— 每封正文截断、只取最近 N 封、附件默认只给元数据；
  * 降低模型混淆 —— 只给与当前待回复邮件同一线程的材料，不给无关历史。
"""
from __future__ import annotations

import hashlib
import json

from .. import util
from ..constants import DRAFT_MODE_HINT, MODE_NORMAL
from ..intelligence import fact_extractor as fx

BODY_CHAR_LIMIT = 1500
RECENT_BODY_CHAR_LIMIT = 900


class MailContextBuilder:
    def __init__(self, repo, cfg: dict, log=None):
        self.repo = repo
        self.cfg = cfg
        self.log = log

    # ------------------------------------------------------------------
    def build(self, message_id: str = None, thread_id: str = None,
              draft_mode: str = None, user_input: dict = None,
              extra_instruction: str = "") -> dict:
        msg = self.repo.get_message(message_id) if message_id else None
        if msg is None and thread_id:
            msg = self.repo.latest_in_thread(thread_id)
        if msg is None:
            raise ValueError("context: 找不到邮件（message_id=%s thread_id=%s）"
                             % (message_id, thread_id))
        tid = thread_id or msg.get("thread_id")
        if not tid:
            raise ValueError("context: 邮件没有 thread_id，请先跑聚合器")

        thread = self.repo.get_thread(tid) or {}
        msgs = self.repo.messages_for_thread(tid, limit=100)
        self_set = {a.lower() for a in (self.cfg.get("self_addresses") or [])}

        recent_n = int(self.cfg.get("draft_context_recent_messages", 6))
        max_n = int(self.cfg.get("draft_context_max_messages", 10))
        recent_n = max(3, min(recent_n, max_n))

        # 最近 N 封：始终包含当前待回复的那封
        recent_src = [m for m in msgs if m["message_id"] != msg["message_id"]]
        recent_src = recent_src[-recent_n:]
        recent = []
        for m in recent_src:
            recent.append(self._msg_view(m, self_set, RECENT_BODY_CHAR_LIMIT))
        current = self._msg_view(msg, self_set, BODY_CHAR_LIMIT)

        participants = []
        for a in (thread.get("participants") or []):
            participants.append(self._participant(a, self_set))

        contact = self.repo.get_contact(msg.get("from_addr") or "")

        # 事实抽取：只在「对方来信」里找问题/要求，在「我方去信」里找承诺
        inbound = [m for m in msgs if (m.get("from_addr") or "").lower() not in self_set]
        outbound = [m for m in msgs if (m.get("from_addr") or "").lower() in self_set]

        open_questions, known_deadlines = [], []
        for m in inbound:
            body = m.get("body_text") or ""
            open_questions += fx.extract_questions(body, m["message_id"])
            known_deadlines += fx.extract_deadlines(body, m["message_id"], m.get("subject"))
        # 当前这封的问题排最前
        cur_q = fx.extract_questions(msg.get("body_text") or "", msg["message_id"])
        cur_d = fx.extract_deadlines(msg.get("body_text") or "", msg["message_id"], msg.get("subject"))

        previous_commitments = []
        for m in outbound:
            previous_commitments += fx.extract_commitments(m.get("body_text") or "",
                                                           m["message_id"])
        money = fx.extract_money(msg.get("body_text") or "")

        pkg = {
            "schema": "MailContextPackage/1",
            "generated_at": util.now_iso(),
            "thread_id": tid,
            "current_message": current,
            "thread_summary": self._summarize(thread, msgs, self_set, cur_d, cur_q),
            "recent_messages": recent,
            "participants": participants,
            "attachments": self._attachments(msgs),
            "previous_commitments": self._norm_list(previous_commitments),
            "open_questions": self._norm_list(cur_q + open_questions),
            "known_deadlines": self._norm_list(cur_d + known_deadlines),
            "user_notes": self._user_notes(msg, contact, user_input),
            "draft_mode": draft_mode or self.cfg.get("default_draft_mode", MODE_NORMAL),
            "draft_mode_hint": DRAFT_MODE_HINT.get(
                draft_mode or self.cfg.get("default_draft_mode", MODE_NORMAL), ""),
            "existing_draft": self._existing_draft(tid, msg["message_id"]),
            "memory_hints": self._memory_hints(),
            "hard_constraints": self._hard_constraints(),
            "extra_instruction": extra_instruction or "",
            "facts_you_may_use": {
                "deadlines": cur_d + known_deadlines,
                "amounts": money,
            },
        }
        pkg["context_hash"] = self.hash_package(pkg)
        return pkg

    # ------------------------------------------------------------------
    def _msg_view(self, m: dict, self_set: set, body_limit: int) -> dict:
        direction = "out" if (m.get("from_addr") or "").lower() in self_set else "in"
        return {
            "message_id": m["message_id"],
            "uid": m.get("uid"),
            "folder": m.get("folder"),
            "direction": direction,
            "subject": m.get("subject"),
            "from_name": m.get("from_name"),
            "from_addr": m.get("from_addr"),
            "to_addrs": util.parse_recipients(m.get("to_addrs") or ""),
            "cc_addrs": util.parse_recipients(m.get("cc_addrs") or ""),
            "date_iso": m.get("date_iso"),
            "snippet": m.get("snippet") or "",
            "body_text": (m.get("body_text") or "")[:body_limit],
            "body_truncated": len(m.get("body_text") or "") > body_limit,
            "has_attachments": bool(m.get("has_attachments")),
            "attachment_names": _load_list(m.get("attachment_names")),
            "classification": m.get("classification"),
        }

    def _participant(self, addr: str, self_set: set) -> dict:
        c = self.repo.get_contact(addr) or {}
        return {
            "email": addr,
            "name": c.get("name") or "",
            "organization": c.get("organization") or "",
            "relationship": c.get("relationship") or "",
            "preferred_language": c.get("preferred_language") or "",
            "is_self": addr.lower() in self_set,
            "last_contact_at": c.get("last_contact_at"),
            "message_count": c.get("message_count") or 0,
        }

    def _summarize(self, thread: dict, msgs: list, self_set: set,
                   cur_deadlines: list, cur_questions: list) -> str:
        """本地线程摘要（确定性，绝不调用 LLM）。"""
        if not thread:
            return ""
        inbound = [m for m in msgs if (m.get("from_addr") or "").lower() not in self_set]
        outbound = [m for m in msgs if (m.get("from_addr") or "").lower() in self_set]
        first = min(msgs, key=lambda m: m.get("internal_ts") or 0) if msgs else None
        last = max(msgs, key=lambda m: m.get("internal_ts") or 0) if msgs else None
        who = thread.get("participants") or []
        if thread.get("waiting_for_me"):
            state = "最新一封来自对方，**等你处理**"
        elif thread.get("waiting_for_other"):
            state = "最新一封是你发出的，**等对方回复**"
        else:
            state = "线程已结束或无明确待办"

        span = ""
        if first and last and first.get("internal_ts") and last.get("internal_ts"):
            d1 = util.parse_iso(first.get("date_iso"))
            d2 = util.parse_iso(last.get("date_iso"))
            if d1 and d2:
                span = "%s → %s（%.1f 天）" % (d1.strftime("%Y-%m-%d"),
                                              d2.strftime("%Y-%m-%d"),
                                              (d2 - d1).total_seconds() / 86400)

        parts = [
            "主题「%s」共 %d 封（对方 %d / 我方 %d）；参与者 %d 人：%s。" % (
                thread.get("subject") or "", len(msgs), len(inbound), len(outbound),
                len(who), "、".join(who[:4]) or "-"),
            "时间跨度：%s。" % (span or "未知"),
            "当前状态：%s。" % state,
        ]
        if cur_deadlines:
            ds = []
            for d in cur_deadlines[:3]:
                if d.get("dates"):
                    ds.append("%s（%s）" % ("/".join(d["dates"]), (d.get("text") or "")[:40]))
            if ds:
                parts.append("当前邮件提到的截止：" + "；".join(ds) + "。")
        if cur_questions:
            parts.append("当前邮件含 %d 个待答问题。" % len(cur_questions))
        return "".join(parts)

    def _attachments(self, msgs: list) -> list:
        """默认只给元数据；有缓存摘要才带上摘要（规范 §22 不做无谓的 LLM 调用）。"""
        out = []
        for m in msgs:
            for a in self.repo.attachments_for_message(m["message_id"]):
                cached = None
                if a.get("sha256"):
                    cached = self.repo.get_attachment_summary(a["sha256"])
                out.append({
                    "message_id": m["message_id"],
                    "filename": a.get("filename"),
                    "content_type": a.get("content_type"),
                    "size_bytes": a.get("size_bytes"),
                    "sha256": a.get("sha256"),
                    "summary_available": bool(cached),
                    "summary": cached,
                })
        return out[:30]

    def _existing_draft(self, thread_id: str, message_id: str) -> dict:
        jobs = self.repo.list_jobs(thread_id=thread_id, limit=5)
        for j in jobs:
            if j.get("draft_text"):
                return {"job_id": j["job_id"], "status": j["status"],
                        "draft_text": j["draft_text"], "draft_mode": j.get("draft_mode")}
        return {}

    def _user_notes(self, msg: dict, contact: dict, user_input: dict) -> list:
        notes = []
        if contact and contact.get("notes"):
            notes.append({"source": "contact", "text": contact["notes"]})
        if msg.get("workflow_state") == "SNOOZED":
            notes.append({"source": "snooze", "text": "用户曾把此邮件延后处理。"})
        for k, v in (user_input or {}).items():
            notes.append({"source": "user_input", "key": k, "text": str(v)})
        return notes

    def _memory_hints(self) -> dict:
        """Draft Memory（规范 §24）：只记录「减少重复编辑」的偏好，可整体关闭。"""
        if not self.cfg.get("draft_memory_enabled", True):
            return {"enabled": False}
        rows = self.repo.draft_memory(limit=20)
        return {
            "enabled": True,
            "removed_phrases": [r["pattern"] for r in rows if r["kind"] == "removed_phrase"][:8],
            "length_preference": next((r["pattern"] for r in rows if r["kind"] == "length_pref"), ""),
            "language_preference": next((r["pattern"] for r in rows if r["kind"] == "language_pref"), ""),
            "signature": next((r["pattern"] for r in rows if r["kind"] == "signature"), ""),
        }

    def _hard_constraints(self) -> list:
        return [
            "只能使用本上下文中出现的事实（人名、日期、金额、承诺、附件名）。",
            "任何【上下文里不存在的具体信息】一律不要编造；如果必须要有，"
            "把它放进 missing_information 列表，不要写进草稿正文。",
            "不要替用户做未获授权的承诺（如同意合作、承诺具体日期、答应金额）。",
            "不要重复询问 previous_commitments 里已经承诺过或 open_questions 里已回答过的事。",
            "保持与既有草稿（existing_draft）的事实一致。",
        ]

    @staticmethod
    def _norm_list(items: list) -> list:
        seen, out = set(), []
        for it in items or []:
            k = (it.get("text") or "")[:60]
            if not k or k in seen:
                continue
            seen.add(k)
            out.append(it)
        return out[:12]

    @staticmethod
    def hash_package(pkg: dict) -> str:
        core = {
            "thread_id": pkg.get("thread_id"),
            "current": pkg.get("current_message", {}).get("message_id"),
            "recent": [m.get("message_id") for m in pkg.get("recent_messages") or []],
            "mode": pkg.get("draft_mode"),
            "extra": pkg.get("extra_instruction"),
            "notes": pkg.get("user_notes"),
        }
        return hashlib.sha256(json.dumps(core, ensure_ascii=False, sort_keys=True)
                              .encode("utf-8")).hexdigest()[:24]


def _load_list(value) -> list:
    if isinstance(value, list):
        return value
    if not value:
        return []
    try:
        got = json.loads(value)
        return got if isinstance(got, list) else []
    except Exception:
        return []


def build(repo, cfg: dict, message_id: str = None, thread_id: str = None, **kw) -> dict:
    return MailContextBuilder(repo, cfg).build(message_id=message_id, thread_id=thread_id, **kw)
