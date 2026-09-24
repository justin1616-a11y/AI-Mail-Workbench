# -*- coding: utf-8 -*-
"""本地事实抽取（确定性，不调用 LLM）。

为什么放在本地（P0-2 Local First）：截止日期、疑问句、承诺句、金额
都是**正则可解**的确定性任务，交给模型既慢又不稳定，还容易编造。
这里抽取的 `known_deadlines` / `open_questions` / `previous_commitments`
直接进 MailContextPackage，成为「禁止编造」的硬约束来源。
"""
from __future__ import annotations

import re

from .. import util

# 绝对日期 ----------------------------------------------------------------
_DATE_ISO = re.compile(r"(20\d{2})\s*[-/年\.]\s*(\d{1,2})\s*[-/月\.]\s*(\d{1,2})\s*日?")
_DATE_MD = re.compile(r"(?<!\d)(\d{1,2})\s*[-/月]\s*(\d{1,2})\s*日?(?!\d)")
_DATE_YM = re.compile(r"(20\d{2})\s*[-/年]\s*(\d{1,2})\s*月")
_TIME = re.compile(r"(?<!\d)([01]?\d|2[0-3])\s*[:：]\s*([0-5]\d)")
_REL_DATE = re.compile(r"(今天|明天|后天|本周[一二三四五六日天]|下周[一二三四五六日天]|"
                       r"周[一二三四五六日天]|本月|下月|月底|月末|本月底)")

DEADLINE_CUES = re.compile(
    r"(截止|截至|不迟于|之前|以前|前完成|前提交|前回复|deadline|due\b|by\s+\d|"
    r"no\s+later\s+than|expire|到期|限期|限\s*\d+\s*(?:个)?(?:工作)?日)",
    re.IGNORECASE)

# 金额 --------------------------------------------------------------------
_MONEY = re.compile(r"(?:￥|¥|\$|USD|CNY|RMB|人民币|美元)?\s*"
                    r"(\d[\d,]*(?:\.\d+)?)\s*(万元|亿元|元|万|亿|美元|USD|RMB|CNY|欧|欧元)")

# 句子切分 ----------------------------------------------------------------
_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;])\s*|\n+")

_QUESTION_CUES = ("？", "?", "吗", "是否", "能否", "可否", "请问", "需要吗", "意见", "觉得", "打算")
_COMMIT_CUES = ("我会", "我将", "我们会", "我方将", "承诺", "答应", "保证", "可以完成", "会尽快",
                "会及时", "会安排", "愿意", "同意", "确认接收", "会处理", "I will", "We will",
                "I'll", "we'll")

_ACTION_CUES = ("请", "需要", "麻烦", "务必", "尽快", "记得", "别忘", "回复", "确认", "填", "提交",
                "报名", "反馈", "参加", "预约", "审核", "审批", "签字", "盖章", "please")


def _sentences(text: str) -> list:
    if not text:
        return []
    out = []
    for s in _SENT_SPLIT.split(text):
        s = util.collapse(s)
        if len(s) >= 4:
            out.append(s)
    return out


def _norm_date(y, m, d) -> str:
    try:
        return "%04d-%02d-%02d" % (int(y), int(m), int(d))
    except Exception:
        return ""


def extract_dates(text: str) -> list:
    """抽取文本里所有可识别的日期（返回归一化后的 YYYY-MM-DD）。"""
    if not text:
        return []
    found = []
    for y, m, d in _DATE_ISO.findall(text):
        v = _norm_date(y, m, d)
        if v:
            found.append(v)
    for m, d in _DATE_MD.findall(text):
        if 1 <= int(m) <= 12 and 1 <= int(d) <= 31:
            found.append("%04d-%02d-%02d" % (util.now().year, int(m), int(d)))
    for y, m in _DATE_YM.findall(text):
        found.append("%s-%02d" % (y, int(m)))
    out = []
    for f in found:
        if f not in out:
            out.append(f)
    return out


def extract_deadlines(text: str, source_message_id: str = "", source_subject: str = "") -> list:
    """抽取带「截止」语义的日期。返回值直接作为 known_deadlines。"""
    out = []
    if not text:
        return out
    for sent in _sentences(text):
        if not DEADLINE_CUES.search(sent):
            continue
        dates = extract_dates(sent)
        times = _TIME.findall(sent)
        rel = _REL_DATE.findall(sent)
        out.append({
            "text": sent[:300],
            "dates": dates,
            "times": ["%s:%s" % (h, m) for h, m in times],
            "relative": rel,
            "source_message_id": source_message_id,
            "source_subject": util.collapse(source_subject)[:120],
        })
    return out[:8]


def extract_money(text: str) -> list:
    if not text:
        return []
    out = []
    for num, unit in _MONEY.findall(text):
        v = ("%s%s" % (num, unit)).strip()
        if v not in out:
            out.append(v)
    return out[:10]


def extract_questions(text: str, source_message_id: str = "") -> list:
    out = []
    for sent in _sentences(text):
        if "？" in sent or sent.endswith("?") or any(c in sent for c in _QUESTION_CUES):
            if len(sent) <= 6:
                continue
            out.append({"text": sent[:300], "source_message_id": source_message_id})
    # 去重保序
    seen, uniq = set(), []
    for q in out:
        k = q["text"][:60]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(q)
    return uniq[:10]


def extract_commitments(text: str, source_message_id: str = "") -> list:
    out = []
    for sent in _sentences(text):
        if any(c in sent for c in _COMMIT_CUES):
            out.append({"text": sent[:300], "source_message_id": source_message_id})
    seen, uniq = set(), []
    for c in out:
        k = c["text"][:60]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(c)
    return uniq[:10]


def extract_actions(text: str, source_message_id: str = "") -> list:
    """对方要求「我」做的事。"""
    out = []
    for sent in _sentences(text):
        if any(c in sent for c in _ACTION_CUES) and DEADLINE_CUES.search(sent) is None:
            out.append({"text": sent[:300], "source_message_id": source_message_id})
    seen, uniq = set(), []
    for a in out:
        k = a["text"][:60]
        if k in seen:
            continue
        seen.add(k)
        uniq.append(a)
    return uniq[:10]


def sensitive_hits(text: str) -> list:
    """日志/隐私自检用：正文里是否含疑似敏感号段。"""
    if not text:
        return []
    pats = {
        "id_card": r"\b\d{17}[\dXx]\b",
        "bank_card": r"\b\d{16,19}\b",
        "phone": r"\b1[3-9]\d{9}\b",
        "password": r"(?i)(password|密码|passwd)\s*[:：=]\s*\S+",
    }
    hits = []
    for name, p in pats.items():
        if re.search(p, text):
            hits.append(name)
    return hits
