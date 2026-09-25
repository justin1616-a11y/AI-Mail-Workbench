# -*- coding: utf-8 -*-
"""V2 规范枚举的**唯一来源**。

关键设计：`MailClassification`（这是什么邮件）与 `WorkflowState`（这封邮件
处于什么处理阶段）必须严格分离（规范 §14）。二者可以自由组合，例如：

    classification = IMPORTANT
    workflow_state = WAITING_FOR_ME

绝不允许把「★ 重点」当成「还没处理」的替身。
"""
from __future__ import annotations

# --------------------------------------------------------------------------
# 邮件分类（确定性规则产出，绝不调用 LLM）
# --------------------------------------------------------------------------
CLASS_IMPORTANT = "IMPORTANT"   # ★ 重点：漏掉会出事
CLASS_REPLY = "REPLY"           # ✉ 要回：真人点对点
CLASS_SYSTEM = "SYSTEM"         # ⚙ 系统处理：去系统里点，不用回邮件
CLASS_READ = "READ"             # ○ 看一眼：留档即可
CLASS_DISMISS = "DISMISS"       # × 可忽略：直接扔，只报数量

CLASSIFICATIONS = (CLASS_IMPORTANT, CLASS_REPLY, CLASS_SYSTEM, CLASS_READ, CLASS_DISMISS)

# V1 五级 slug -> V2 分类（保证 V1 规则文件零改动即可复用）
TIER_TO_CLASS = {
    "vip": CLASS_IMPORTANT,
    "reply": CLASS_REPLY,
    "act": CLASS_SYSTEM,
    "read": CLASS_READ,
    "dump": CLASS_DISMISS,
}
CLASS_TO_TIER = {v: k for k, v in TIER_TO_CLASS.items()}

CLASS_LABEL = {
    CLASS_IMPORTANT: "★ 重点",
    CLASS_REPLY: "✉ 要回",
    CLASS_SYSTEM: "⚙ 系统",
    CLASS_READ: "○ 看一眼",
    CLASS_DISMISS: "× 可忽略",
}

CLASS_ORDER = [CLASS_IMPORTANT, CLASS_REPLY, CLASS_SYSTEM, CLASS_READ, CLASS_DISMISS]

# 分类 -> 基础优先级（越大越靠前）
CLASS_PRIORITY = {
    CLASS_IMPORTANT: 90,
    CLASS_REPLY: 70,
    CLASS_SYSTEM: 55,
    CLASS_READ: 30,
    CLASS_DISMISS: 10,
}

# --------------------------------------------------------------------------
# 工作流状态（人类动作产出）
# --------------------------------------------------------------------------
WF_NEW = "NEW"
WF_WAITING_FOR_ME = "WAITING_FOR_ME"
WF_WAITING_FOR_OTHER = "WAITING_FOR_OTHER"
WF_SNOOZED = "SNOOZED"
WF_DONE = "DONE"
WF_IGNORED = "IGNORED"
WF_ARCHIVED = "ARCHIVED"

WORKFLOW_STATES = (WF_NEW, WF_WAITING_FOR_ME, WF_WAITING_FOR_OTHER,
                   WF_SNOOZED, WF_DONE, WF_IGNORED, WF_ARCHIVED)

WF_LABEL = {
    WF_NEW: "未处理",
    WF_WAITING_FOR_ME: "等我处理",
    WF_WAITING_FOR_OTHER: "等对方回复",
    WF_SNOOZED: "已延后",
    WF_DONE: "已完成",
    WF_IGNORED: "已忽略",
    WF_ARCHIVED: "已归档",
}

# --------------------------------------------------------------------------
# DraftJob 状态（规范 §5 正式状态机）
# --------------------------------------------------------------------------
JOB_CANDIDATE = "candidate"
JOB_QUEUED = "queued"
JOB_GENERATING = "generating"
JOB_NEEDS_INPUT = "needs_input"
JOB_READY = "ready"
JOB_REVIEWING = "reviewing"
JOB_APPROVED = "approved"
JOB_SENT = "sent"
JOB_DISMISSED = "dismissed"
JOB_FAILED = "failed"
JOB_EXPIRED = "expired"

JOB_STATUSES = (JOB_CANDIDATE, JOB_QUEUED, JOB_GENERATING, JOB_NEEDS_INPUT,
                JOB_READY, JOB_REVIEWING, JOB_APPROVED, JOB_SENT,
                JOB_DISMISSED, JOB_FAILED, JOB_EXPIRED)

JOB_LABEL = {
    JOB_CANDIDATE: "候选中",
    # 「排队中」是个含糊的说法：它没告诉用户**在等谁**。
    # 实测用户点完「AI 起草」以为过一会儿就有草稿，实际上这个队列是拉模式，
    # 必须有一个 Worker 主动来认领，否则永远停在 queued。
    # 所以直接说清楚在等什么 —— 等待认领。
    JOB_QUEUED: "等待认领",
    JOB_GENERATING: "起草中",
    JOB_NEEDS_INPUT: "需要你的信息",
    JOB_READY: "草稿就绪",
    JOB_REVIEWING: "审核中",
    JOB_APPROVED: "已确认待发送",
    JOB_SENT: "已发送",
    JOB_DISMISSED: "已放弃",
    JOB_FAILED: "失败",
    JOB_EXPIRED: "已过期",
}

# 仍然「活着」、占用幂等键的状态
JOB_ACTIVE_STATUSES = (JOB_QUEUED, JOB_GENERATING, JOB_NEEDS_INPUT,
                       JOB_READY, JOB_REVIEWING, JOB_APPROVED)
# 终态
JOB_TERMINAL_STATUSES = (JOB_SENT, JOB_DISMISSED, JOB_EXPIRED)

# --------------------------------------------------------------------------
# 草稿模式（规范 §11）
# --------------------------------------------------------------------------
MODE_QUICK = "quick"
MODE_NORMAL = "normal"
MODE_DETAILED = "detailed"
MODE_FORMAL = "formal"
MODE_ACADEMIC = "academic"

DRAFT_MODES = (MODE_QUICK, MODE_NORMAL, MODE_DETAILED, MODE_FORMAL, MODE_ACADEMIC)
DRAFT_MODE_LABEL = {
    MODE_QUICK: "快速回复（1~3 句）",
    MODE_NORMAL: "标准回复",
    MODE_DETAILED: "详细回复（复杂事务）",
    MODE_FORMAL: "正式回复（行政/机构）",
    MODE_ACADEMIC: "学术回复（论文/合作）",
}

# 每个模式的写作约束，直接塞进 Worker 契约的 context，避免模型自由发挥
DRAFT_MODE_HINT = {
    MODE_QUICK: "1~3 句，直接给结论，不要客套铺垫。",
    MODE_NORMAL: "标准商务/校内邮件长度，2~3 段，先回应再说明。",
    MODE_DETAILED: "复杂事务，分点说明，覆盖对方全部问题。",
    MODE_FORMAL: "正式行政/机构公文口吻，措辞克制，保留必要敬语。",
    MODE_ACADEMIC: "学术交流口吻，术语准确，协作/论文语境，避免过度客套。",
}

# --------------------------------------------------------------------------
# Reply Controls（规范 §12）—— 一键改写指令，用户不必重写 prompt
# --------------------------------------------------------------------------
REPLY_CONTROLS = {
    "shorter": "在保留全部关键事实与承诺的前提下，把长度压缩到原文的一半以内。",
    "longer": "在不新增任何未经确认的事实的前提下，把要点展开说明得更充分。",
    "more_formal": "改为更正式、克制的措辞（适用于行政/机构往来）。",
    "more_friendly": "改为更亲切自然的措辞，减少程式化表达。",
    "more_direct": "开门见山给出结论，去掉铺垫与客套。",
    "chinese": "改写为中文。",
    "english": "Rewrite in English.",
    "bilingual": "中英双语，先中文后英文，两段之间空一行。",
    "regenerate": "换一个思路重新起草，可以换开头与结构，但事实必须一致。",
    "fix_typo": "只修正错别字、标点与病句，不改动实质内容与长度。",
    "strip_boilerplate": "删除套话与重复铺垫，只留必要信息。",
}

LANGUAGES = ("zh", "en", "bilingual")

# Reply Control 的**按钮文案**。
#
# 为什么必须单独一份：`REPLY_CONTROLS` 的键是给 Worker 看的标识符
# （shorter / more_formal / …），值是一整句给模型的指令。UI 之前直接
# 拿键当按钮文字渲染，于是界面上出现一排 `shorter` `more_formal`
# `strip_boilerplate` —— 它们是给程序读的，不是给人读的。
REPLY_CONTROL_LABEL = {
    "shorter": "更短",
    "longer": "更详细",
    "more_formal": "更正式",
    "more_friendly": "更亲切",
    "more_direct": "更直接",
    "chinese": "改成中文",
    "english": "改成英文",
    "bilingual": "中英双语",
    "regenerate": "换一版",
    "fix_typo": "只改错别字",
    "strip_boilerplate": "去掉套话",
}

# 快捷改写的分组：11 个按钮平铺太长，按「长度 / 语气 / 语言 / 其他」分组，
# 用户找的是「我想让它怎么样」，而不是按字母序找标识符。
REPLY_CONTROL_GROUP = {
    "长度": ("shorter", "longer"),
    "语气": ("more_formal", "more_friendly", "more_direct"),
    "语言": ("chinese", "english", "bilingual"),
    "其它": ("regenerate", "fix_typo", "strip_boilerplate"),
}

# --------------------------------------------------------------------------
# 工作桶（规范 §13）—— 首页只展示桶，不展示「全部邮件数量」
# --------------------------------------------------------------------------
BUCKET_ACTION_REQUIRED = "ACTION_REQUIRED"
BUCKET_WAITING_FOR_REPLY = "WAITING_FOR_REPLY"
BUCKET_DRAFT_READY = "AI_DRAFT_READY"
BUCKET_SNOOZED = "SNOOZED"
BUCKET_DONE_TODAY = "DONE_TODAY"

# 不是「工作桶」，是**信息区**：群发通知 + 用户配置为「仅知会」的分类。
# 它们不占用「待我处理」（用户要的是聚焦的工作集），但必须**可见、可点进去**，
# 否则就等于把邮件藏了 —— 所以单独成一个列表，计数也不隐瞒。
BUCKET_BROADCAST = "BROADCAST"

# 「等待对方回复」的**活跃窗口**（天，可配 waiting_window_days）。
#
# 为什么这个桶必须有窗口，而「待我处理」有窗口、它却没有是不对称的：
#   待我处理 = 球在我这边 -> 计数上升会推动我行动，压力是建设性的
#   等对方   = 球在对方那边 -> 我**没有任何动作**能推进它，计数上升只产生无力感
# 也就是说，一个我没有控制权的计数器，涨得越多越像在指控我，而我无从反驳。
#
# 实测（2026-09）：用户 145 个 waiting_for_other 线程里，
#   90 天以上 128 个、一年以上 102 个，最早的是 2023 年的「打印机权限设置」。
# 它们不是「在等」，而是「我最后说了一句话」的历史沉降 ——
# 一封 2023 年的通知我回了句「收到」，凭什么在 2026 年还挂在我的待办上？
#
# 所以规则改成：只有我方最后一封在 N 天内发出的，才算「活跃等待」。
# 更早的进入沉淀区（BUCKET_STALE_WAIT）：不占侧边栏计数、不删除、不隐藏，
# 仍可点开、可搜索、可一键捞回。让「等待」是**正在进行时的动作**，
# 而不是一个只增不减的负债。
DEFAULT_WAITING_WINDOW_DAYS = 14

# 沉淀区：同样不是工作桶，不出现在 BUCKETS / BUCKETS_ALL 里，
# 也就不在侧边栏占一个计数位 —— 不给焦虑换一个地方重新长出来。
# 它只以「一行灰色小字」的形式挂在「等待对方回复」下面。
BUCKET_STALE_WAIT = "STALE_WAIT"

BUCKETS = (BUCKET_ACTION_REQUIRED, BUCKET_WAITING_FOR_REPLY,
           BUCKET_DRAFT_READY, BUCKET_SNOOZED, BUCKET_DONE_TODAY)

# 首页会渲染的全部条目（工作桶 + 信息区），顺序即展示顺序
BUCKETS_ALL = BUCKETS + (BUCKET_BROADCAST,)

BUCKET_LABEL = {
    BUCKET_ACTION_REQUIRED: "待我处理",
    BUCKET_WAITING_FOR_REPLY: "等待对方回复",
    BUCKET_DRAFT_READY: "AI 草稿",
    BUCKET_SNOOZED: "已延后",
    BUCKET_DONE_TODAY: "今天已完成",
    BUCKET_BROADCAST: "仅知会",
    BUCKET_STALE_WAIT: "已沉底的等待",
}

# 工作桶的判定说明（UI 用来解释「为什么这些不算待我处理」）
BUCKET_HINT = {
    BUCKET_ACTION_REQUIRED: "球在我这边，需要我看或回",
    # 这句刻意不写「我已回复，等对方」——那是在陈述一个我无法改变的事实，
    # 听多了会变成负债。改成「对方不回不是你的待办」，把责任划清。
    BUCKET_WAITING_FOR_REPLY: "我发出后还没回音的。对方不回不是你的待办，超期会自动沉底",
    BUCKET_DRAFT_READY: "草稿任务的全过程：等待认领 / 起草中 / 需要你的信息 / 待审核",
    BUCKET_SNOOZED: "已延后，到点自动回来",
    BUCKET_DONE_TODAY: "今天处理完成",
    BUCKET_BROADCAST: "群发通知 / 只看一眼的邮件：不占「待我处理」，但都在这儿可查",
    BUCKET_STALE_WAIT: "发出很久没有回音的线程：已退出工作桶，但都在这儿可查、可搜索",
}

# --------------------------------------------------------------------------
# 侧边栏的文件夹树 / 标签树
# --------------------------------------------------------------------------
# 系统文件夹的顺序（侧边栏先列这几个，其余归入「自建文件夹」）
SYSTEM_FOLDERS = ("INBOX", "Drafts", "Sent", "Junk", "Trash")

# Foxmail 历史在 store 里用一个伪文件夹名承载；它不出现在文件夹树里，
# 而是折算成侧边栏底部的「N 封可检索」。
FOLDER_FOXMAIL = "(Foxmail 历史)"

# 标签树用的名称 —— 与 V1 的 web/js/classify.js TIERS **逐字一致**。
# 为什么要单独一份、不直接用 CLASS_LABEL：列表里的 chip 需要短（★ 重点），
# 而侧边栏是用户按肌肉记忆点的，得保留他认得的叫法（✉ 要回的 / ⚙ 系统处理）。
CLASS_TREE_LABEL = {
    CLASS_IMPORTANT: "★ 重点",
    CLASS_REPLY: "✉ 要回的",
    CLASS_SYSTEM: "⚙ 系统处理",
    CLASS_READ: "○ 看一眼",
    CLASS_DISMISS: "× 可忽略",
}

# 标签圆点颜色（与 V1 web/js/components/folders.js 的 dotColor 一致）
CLASS_TREE_DOT = {
    CLASS_IMPORTANT: "#d93a33",
    CLASS_REPLY: "#3a66ac",
    CLASS_SYSTEM: "#2f86d4",
    CLASS_READ: "#8ba5cb",
    CLASS_DISMISS: "#c4daf5",
}

# --------------------------------------------------------------------------
# 触发来源（可观测性：看清每封邮件为什么被处理）
# --------------------------------------------------------------------------
TRIGGER_IDLE = "idle"           # IMAP IDLE 事件驱动
TRIGGER_RULE = "rule"           # 本地规则判定
TRIGGER_SYNC = "sync"           # 增量同步发现
TRIGGER_MANUAL = "manual"       # 用户点击
TRIGGER_RECOVERY = "recovery"   # Recovery 重投
TRIGGER_FOLLOWUP = "followup"   # 跟进提醒
TRIGGER_SNOOZE_WAKE = "snooze_wake"

# SSE 事件名
EVENT_MAIL = "mail"
EVENT_JOB = "job"
EVENT_CANDIDATE = "candidate"
EVENT_SYNC = "sync"
EVENT_METRIC = "metric"
EVENT_ALERT = "alert"
