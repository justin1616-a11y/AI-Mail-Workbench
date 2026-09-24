# -*- coding: utf-8 -*-
"""快速草稿模板（本地规则生成，毫秒级，不调模型）。

**为什么需要它**：
    实测 claim → ready 是 22 秒，但那 22 秒之外还有一段更长的等待 ——
    要有人（WorkBuddy 会话）来认领草稿队列，没人认领就无限期挂着。
    而日常邮件里真正需要斟酌措辞的只是少数；大量回复其实属于
    「收到确认 / 婉拒 / 索取材料 / 稍后答复」这几类。
    把这几类用本地模板即时出稿，等待就从流程里消失了。

**边界（与 AI 草稿完全一致）**：
    * 模板**只写套话，不写事实**。任何需要事实的地方（名额、日期、金额、
      材料清单）一律留 `[请补充：...]` 占位符，绝不替用户编。
    * 生成的草稿只到 `ready` 为止 —— 发送仍然必须经过
      「确认 → 带令牌发送」两步，由人亲手完成。
"""
from __future__ import annotations

import re

from .. import util

# 占位符统一格式：渲染后出现的 [请补充：xxx] 会在 UI / API 里被标出来，
# 提醒用户「这里必须你来填」。用中文方括号以便一眼分辨。
PLACEHOLDER_OPEN = "[请补充："

# --------------------------------------------------------------------------
# 模板本体 —— 语气：简洁公务。直给结论，不堆客套。
# --------------------------------------------------------------------------
TEMPLATES = {
    "ack": {
        "label": "收到确认",
        "hint": "事务性回执：只需让对方知道已收到",
        "body": "{salutation}\n\n来信收到，谢谢告知。\n\n{signature}",
    },
    "thanks": {
        "label": "致谢",
        "hint": "对方提供了信息或帮助，回一句谢",
        "body": "{salutation}\n\n来信收到，感谢告知与协助。\n\n{signature}",
    },
    "pending": {
        "label": "稍后答复",
        "hint": "需要先核实再回，但先让对方知道信已收到（不会说错话）",
        "body": "{salutation}\n\n来信已收到。我需要先确认相关情况，"
                "确认后再回复你。\n\n{signature}",
    },
    "decline": {
        "label": "婉拒",
        "hint": "明确拒绝，但把原因留给你填 —— 模板不替你编理由",
        "body": "{salutation}\n\n来信收到。很抱歉，"
                + PLACEHOLDER_OPEN + "具体原因，如名额已满 / 暂不参与 / 时间冲突"
                + "]，这次无法应允，特此说明。\n\n"
                "感谢你的来信。\n\n{signature}",
    },
    "ask_info": {
        "label": "索取材料",
        "hint": "要对方补齐信息或文件后再处理",
        "body": "{salutation}\n\n来信收到。请补充以下内容，以便进一步处理：\n\n"
                "- " + PLACEHOLDER_OPEN + "材料或信息 1]\n"
                "- " + PLACEHOLDER_OPEN + "材料或信息 2]\n\n{signature}",
    },
    "confirm_time": {
        "label": "确认时间",
        "hint": "会议 / 日程类：确认参加",
        "body": "{salutation}\n\n时间可以，届时参加。\n\n"
                + PLACEHOLDER_OPEN + "如需说明地点、线上方式或议程，补充在这里"
                + "]\n\n{signature}",
    },
}

# 默认模板：**永远安全**（只确认收到、不做任何承诺），推断不出来时用它。
DEFAULT_KIND = "pending"

# --------------------------------------------------------------------------
# 推断：从主题 + 正文摘要猜一个模板。
# 命中不了就用默认 —— 猜错代价只是「换个模板」一下，
# 但默认那个必须无论如何都不会说错话。
# --------------------------------------------------------------------------
_RULES = [
    # 明确要求答复某个事项 -> 先回「稍后答复」最稳
    ("ask_info", ("请提供", "请补充", "请提交", "需提供", "材料", "附件", "证明")),
    ("confirm_time", ("会议", "日程", "邀请", "参会", "出席", "审议", "答辩",
                      "评审会", "座谈", "报告会", "线上", "腾讯会议")),
    ("decline", ("申请名额", "增补名额", "递补名额", "推免", "调剂", "求职",
                 "应聘", "内推", "合作申请")),
    ("thanks", ("感谢", "谢谢", "支持", "协助", "帮助")),
    ("ack", ("通知", "提醒", "公告", "安排", "告知", "回执", "确认单",
             "账单", "流水", "预警", "安全", "体检", "放假", "停用")),
]


def guess_kind(subject: str, body: str = "") -> str:
    """按主题优先、正文辅的顺序猜模板类型；猜不出返回 DEFAULT_KIND。"""
    s = (subject or "")
    b = (body or "")[:600]
    for kind, kws in _RULES:
        for kw in kws:
            if kw in s:
                return kind
    for kind, kws in _RULES:
        for kw in kws:
            if kw in b:
                return kind
    return DEFAULT_KIND


# --------------------------------------------------------------------------
# 渲染
# --------------------------------------------------------------------------
def _salutation(from_name: str, from_addr: str) -> str:
    """称呼。**不猜身份**（不写「老师」「同学」）——
    身份猜错比称呼平淡更失礼。"""
    name = (from_name or "").strip()
    if not name:
        local = (from_addr or "").split("@")[0]
        name = local if local else ""
    if name:
        # 去掉常见的括号备注与多余后缀，避免「张三（教务处）：」这种
        name = re.sub(r"[（(].*?[)）]", "", name).strip()
    return ("%s：" % name) if name else "您好："


def _signature(cfg: dict, signature: str = "") -> str:
    if signature and signature.strip():
        return signature.strip()
    return (cfg or {}).get("from_name") or ""


def render(kind: str, cfg: dict, from_name: str = "", from_addr: str = "",
           signature: str = "") -> str:
    """把指定模板渲染成草稿正文。未知 kind 退回默认模板。"""
    tpl = TEMPLATES.get(kind) or TEMPLATES[DEFAULT_KIND]
    return tpl["body"].format(
        salutation=_salutation(from_name, from_addr),
        signature=_signature(cfg, signature),
    )


def missing_slots(draft_text: str) -> list:
    """列出草稿里还没填的占位符 —— UI 要据此提示「这几处必须你来写」。"""
    return re.findall(re.escape(PLACEHOLDER_OPEN) + r"([^\]]*)\]",
                      draft_text or "")


def build_fast_draft(repo, cfg: dict, message_id: str, kind: str = None) -> dict:
    """给一封邮件生成快速草稿（本地模板）。

    返回 {ok, kind, label, hint, draft_text, missing_slots, auto}。
    `auto=True` 表示 kind 是推断出来的（用户可以考虑换一个）。
    """
    msg = repo.get_message(message_id)
    if not msg:
        return {"ok": False, "error": "邮件不存在：%s" % message_id}

    auto = False
    if kind not in TEMPLATES:
        kind = guess_kind(msg.get("subject") or "", msg.get("body_text") or "")
        auto = True

    try:
        sig = repo.kv_get("signature") or ""
    except Exception:
        sig = ""

    text = render(kind, cfg, from_name=msg.get("from_name") or "",
                  from_addr=msg.get("from_addr") or "", signature=sig)
    tpl = TEMPLATES[kind]
    return {
        "ok": True,
        "kind": kind,
        "label": tpl["label"],
        "hint": tpl["hint"],
        "draft_text": text,
        "missing_slots": missing_slots(text),
        "auto": auto,
        "available_kinds": [{"kind": k, "label": v["label"], "hint": v["hint"]}
                            for k, v in TEMPLATES.items()],
        "note": ("这是本地模板生成的快速草稿（未调用模型），"
                 "带 [请补充：] 的地方需要你填写；"
                 "想要更贴合的措辞可以再用 AI 精细起草。"),
    }


def kinds() -> list:
    return [{"kind": k, "label": v["label"], "hint": v["hint"]}
            for k, v in TEMPLATES.items()]
