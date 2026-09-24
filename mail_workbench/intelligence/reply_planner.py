# -*- coding: utf-8 -*-
"""Reply Planner —— Plan → Draft 两段式 + Missing Information Gate（规范 §9 / §10）。

为什么不要「Mail → 直接生成正文」：
    一步生成时，模型会把「理解对方要什么」和「组织语言」混在一起，
    于是遇到没有依据的信息（日期、金额、名额、名单）就倾向于**编一个看起来合理的**。
    拆成 Plan → Draft 后，第一步只产出结构化的 ReplyPlan，
    缺什么必须在 `missing_information` 里显式声明，**没有中间地带可以含糊过去**。

Missing Information Gate（§10 硬门禁）：
    若邮件要求「具体日期 / 金额 / 承诺 / 人员名单 / 附件 / 实验数据 / 会议时间 /
    项目决定 / 正式意见」，而 Context 中不存在可靠答案 —— **禁止编造**。
    本模块用确定性规则先做一遍预检，把可疑类别升级成强制缺项，
    服务端会校验：只要命中风险类别而 plan 又没有声明缺项，就强制 NEEDS_INPUT。
"""
from __future__ import annotations

import re

from .. import util
from ..constants import DRAFT_MODES, MODE_NORMAL

# --------------------------------------------------------------------------
# ReplyPlan 结构（规范 §9）
# --------------------------------------------------------------------------
REPLY_PLAN_SCHEMA = {
    "intent": "confirm|decline|acknowledge|clarify|ask|inform|negotiate|defer",
    "questions_to_answer": [],      # 对方提问 -> 我的答复要点（逐条对应）
    "facts_to_include": [],         # 必须出现在草稿里的既成事实
    "missing_information": [],      # 我无法从上下文得知、必须由用户提供的信息
    "tone": "professional|friendly|neutral|formal|academic",
    "attachment_required": False,
    "attachment_note": "",
    "language": "zh|en|bilingual",
    "status": "READY|NEEDS_INPUT",
}

VALID_INTENTS = {"confirm", "decline", "acknowledge", "clarify", "ask",
                 "inform", "negotiate", "defer"}
VALID_TONES = {"professional", "friendly", "neutral", "formal", "academic"}
VALID_STATUS = {"READY", "NEEDS_INPUT"}

# --------------------------------------------------------------------------
# 风险类别（需要「可靠事实」才能回答的类别）
# --------------------------------------------------------------------------
RISK_PATTERNS = {
    "date": (
        re.compile(r"(哪天|什么时候|何时|时间安排|日期|几号|周[一二三四五六日天]|"
                   r"哪天方便|available|availability|schedule)", re.I),
        "可以参加的日期 / 时间", "date"),
    "amount": (
        re.compile(r"(金额|经费|预算|报价|费用|多少钱|单价|总额|补助标准)"),
        "金额 / 预算 / 补助标准", "amount"),
    "decision": (
        re.compile(r"(是否同意|同不同意|能否接收|是否接收|是否合作|是否参与|能否参与|"
                   r"是否批准|是否支持|愿不愿意|可否接收)"),
        "是否同意 / 接收 / 参与（需要你的决定）", "decision"),
    "roster": (
        re.compile(r"(名单|人员名单|参与人|团队成员|组队|成员列表)"),
        "人员名单 / 团队成员", "roster"),
    "attachment": (
        re.compile(r"(附件|附上|随信|发送.{0,4}文件|提供.{0,4}(材料|文档|简历|证明|证书))"),
        "要发送的附件 / 材料", "attachment"),
    "data": (
        re.compile(r"(实验数据|数据结果|测试结果|实测数据|benchmark|指标数据)"),
        "实验数据 / 测试结果", "data"),
    "meeting": (
        re.compile(r"(会议时间|开个会|约个时间|面聊|面谈|线上会议|腾讯会议|zoom|meeting)"),
        "会议时间 / 约谈时段", "meeting"),
    "commitment": (
        re.compile(r"(承诺|保证|答应|务必.{0,6}(完成|提交)|限期|deadline.{0,10}(承诺|保证))"),
        "需要你给出的承诺（完成时间 / 交付物）", "commitment"),
    "formal_opinion": (
        re.compile(r"(正式意见|书面意见|审核意见|评审意见|表态|书面答复)"),
        "正式 / 书面意见", "formal_opinion"),
}

# 这些类别的答案**永远**不可能从邮件上下文里推断出来，必须由用户给
ALWAYS_USER_OWNED = {"decision", "commitment", "amount", "roster", "data", "formal_opinion"}


def detect_risk_categories(text: str) -> list:
    """返回命中的风险类别列表 [{key,label,type}]。"""
    if not text:
        return []
    out = []
    for key, (pat, label, kind) in RISK_PATTERNS.items():
        if pat.search(text):
            out.append({"key": key, "label": label, "type": kind})
    return out


def context_has_fact(package: dict, kind: str) -> bool:
    """判断 context 里是否已有该类别的可靠答案。"""
    facts = package.get("facts_you_may_use") or {}
    if kind in ("date", "meeting"):
        return bool(facts.get("deadlines"))
    if kind == "amount":
        return bool(facts.get("amounts"))
    # 其余类别：只有用户显式提供过才算有
    for n in package.get("user_notes") or []:
        if n.get("source") == "user_input":
            if kind in str(n.get("key") or ""):
                return True
    return False


def suggest_missing_information(package: dict) -> list:
    """确定性预检：哪些类别必须问用户。"""
    cur = package.get("current_message") or {}
    text = "%s\n%s" % (cur.get("subject") or "", cur.get("body_text") or "")
    risks = detect_risk_categories(text)
    out = []
    for r in risks:
        if r["type"] in ALWAYS_USER_OWNED or not context_has_fact(package, r["type"]):
            out.append({
                "key": "risk_%s" % r["key"],
                "label": r["label"],
                "type": r["type"],
                "why": "邮件涉及「%s」，上下文中没有可靠依据" % r["label"],
                "required": True,
            })
    return out


# --------------------------------------------------------------------------
# Plan 校验
# --------------------------------------------------------------------------
def validate_plan(plan: dict) -> tuple:
    """返回 (ok, errors, normalized_plan)。宽容但会归一化缺省字段。"""
    errors = []
    if not isinstance(plan, dict):
        return False, ["plan 必须是 JSON 对象"], {}
    p = dict(plan)
    p.setdefault("intent", "inform")
    if p["intent"] not in VALID_INTENTS:
        errors.append("intent 非法：%s（可选 %s）" % (p["intent"], sorted(VALID_INTENTS)))
    p.setdefault("tone", "professional")
    if p["tone"] not in VALID_TONES:
        errors.append("tone 非法：%s" % p["tone"])
    p.setdefault("language", "zh")
    for k in ("questions_to_answer", "facts_to_include", "missing_information"):
        v = p.get(k)
        if v is None:
            p[k] = []
        elif isinstance(v, str):
            p[k] = [{"text": v}] if k == "missing_information" else [v]
        elif not isinstance(v, list):
            errors.append("%s 必须是数组" % k)
            p[k] = []
    p.setdefault("attachment_required", False)
    p.setdefault("attachment_note", "")
    missing = p.get("missing_information") or []
    p["status"] = "NEEDS_INPUT" if (missing or p.get("status") == "NEEDS_INPUT") else "READY"
    return (len(errors) == 0), errors, p


def enforce_gate(package: dict, plan: dict) -> tuple:
    """硬门禁：风险类别命中但没有声明缺项 -> 强制补齐并转 NEEDS_INPUT。"""
    suggested = suggest_missing_information(package)
    if not suggested:
        return plan, []
    declared = plan.get("missing_information") or []
    declared_keys = set()
    for d in declared:
        if isinstance(d, dict):
            declared_keys.add((d.get("key") or "") + "|" + (d.get("type") or ""))
        else:
            declared_keys.add(str(d))
    added = []
    for s in suggested:
        if any(s["key"] in k or s["type"] in k for k in declared_keys):
            continue
        declared.append(s)
        added.append(s)
    if added:
        plan["missing_information"] = declared
        plan["status"] = "NEEDS_INPUT"
    return plan, added


# --------------------------------------------------------------------------
# 给 WorkBuddy 的 Brief（Worker 契约里附带的执行说明）
# --------------------------------------------------------------------------
PLAN_BRIEF = """你是邮件回复规划器。**只做规划，不要写正文。**

看 MailContextPackage 后输出 JSON（严格对齐这个结构）：
{
  "intent": "confirm|decline|acknowledge|clarify|ask|inform|negotiate|defer",
  "questions_to_answer": ["对方问的每个问题 -> 我方的答复要点"],
  "facts_to_include":      ["草稿里必须出现的既成事实（只能来自 context）"],
  "missing_information":   [{"key":"...","label":"...","type":"...","required":true}],
  "tone":    "professional|friendly|neutral|formal|academic",
  "language":"zh|en|bilingual",
  "attachment_required": false,
  "attachment_note": "",
  "status": "READY|NEEDS_INPUT"
}

硬规则：
1. 对方是谁？对方要什么？我需要回答什么？有没有 deadline？要不要附件？我是否缺信息？
2. **任何 context 里不存在的事实都不许编**。缺就写进 missing_information。
3. 涉及「具体日期 / 金额 / 是否同意合作 / 人员名单 / 要发哪个附件 / 实验数据 /
   会议时间 / 项目决定 / 正式意见」而这些在 context 里找不到可靠答案时，
   必须列入 missing_information，status 置 NEEDS_INPUT。
4. 不要重复询问 previous_commitments 里已经承诺过、或 open_questions 里已答复过的事。
"""

DRAFT_BRIEF = """你是邮件正文写作者。**已经有人类审核过的 ReplyPlan，按它写。**

输出 JSON：
{"draft_text": "完整邮件正文（含称呼、正文、落款，不要再套 JSON 转义之外的包装）",
 "language": "zh|en|bilingual",
 "attachment_note": ""}

硬规则：
1. 严格按 ReplyPlan 的 intent / questions_to_answer / facts_to_include 组织内容。
2. **不得引入 ReplyPlan 与 context 之外的新事实**（人名、日期、金额、承诺、档名）。
3. 需要用户补的信息，**不要写进正文**，也不要用「XXX」占位；那是 missing_information 的事。
4. 遵守 draft_mode_hint 的长度与语气要求。
5. 遵守 memory_hints（用户历史偏好，例如常删掉的套话、偏好的语言与署名格式）。
6. 署名默认用 context.participants 里 is_self=true 的那位的姓名（若缺失则留空行由用户补）。
"""


def build_brief(package: dict, stage: str = "plan") -> dict:
    """把 context 与执行说明打包成给 WorkBuddy 的 brief。"""
    return {
        "stage": stage,
        "instructions": PLAN_BRIEF if stage == "plan" else DRAFT_BRIEF,
        "context": package,
        "reply_plan_schema": REPLY_PLAN_SCHEMA,
        "draft_modes": list(DRAFT_MODES),
    }


def normalize_mode(mode: str) -> str:
    return mode if mode in DRAFT_MODES else MODE_NORMAL
