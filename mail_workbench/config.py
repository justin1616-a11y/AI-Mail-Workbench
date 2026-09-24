# -*- coding: utf-8 -*-
"""配置与凭据加载。

安全约束（V2 硬性要求）：
  1. **密码永不写入项目目录**。V1 有 4 个脚本把邮箱密码明文硬编码，
     这是 V2 必须修掉的缺陷。凭据只从下面几处按优先级加载：
         环境变量  >  ~/.workbuddy/secrets/mail.env  >  ~/.workbuddy/secrets/mail.cred
  2. config.json 里不应有密码；若存在也仅在最后兜底。
  3. 普通日志只允许输出脱敏后的地址（见 util.redact）。

注意：本文件是**会进分发包的源码**，因此下面 DEFAULT_CONFIG 里的账号字段
一律是占位符。真实值来自安装向导写入的 config.json / 凭据文件。
不要把任何真实邮箱、姓名、密码、本机路径写回这里 —— `pack.py` 的隐私自检会拦下来。
"""
from __future__ import annotations

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(HERE)                    # .../Mails
WORKSPACE_ROOT = os.path.dirname(PROJECT_ROOT)          # .../2026-09-18-17-38-25

HOME = os.path.expanduser("~")
SKILL_DIR = os.path.join(HOME, ".workbuddy", "skills", "sjtu-mail-remote")

DATA_DIR = os.path.join(HERE, "data")
LOGS_DIR = os.path.join(PROJECT_ROOT, "logs")
UI_DIR = os.path.join(HERE, "ui")
DB_PATH = os.path.join(DATA_DIR, "mail_workbench.db")
# 下载下来的附件落盘位置（点「用默认程序打开」时先下到这里）。
# 放项目根而不是包内，方便用户自己翻；也在 pack.py 的忽略清单里。
ATTACHMENTS_DIR = os.path.join(PROJECT_ROOT, "attachments")

# 凭据文件（KEY=VALUE）。V1 的旧路径一并兼容，避免升级后用户配置失效。
SECRETS_PATHS = [
    os.path.join(HOME, ".workbuddy", "secrets", "mail.env"),
    os.path.join(HOME, ".workbuddy", "secrets", "mail.cred"),
    os.path.join(SKILL_DIR, ".secrets.env"),
]

# 配置来源，**后面的覆盖前面的**（优先级从低到高）。
#
# ⚠️ 这里踩过一个坑，别调换顺序、也别删项目根那一条：
# V2 起初只读「包内」和「技能目录」两份 config.json，**不读项目根的那份** ——
# 而项目根那份正是 V1 用的、README/INSTALL 引导用户去改的那一份。
# 结果就是：用户改了 config.json（比如把自建文件夹加进 sync_folders），
# 界面上毫无变化，排查起来像见鬼。两份配置并存本身没问题，
# 有问题的是「你以为在读的那份其实没被读」。
CONFIG_PATHS = [
    os.path.join(HERE, "config.json"),            # 包内：独立部署时的兜底
    os.path.join(SKILL_DIR, "config.json"),       # 技能目录：AI 侧共用设置
    os.path.join(PROJECT_ROOT, "config.json"),    # 应用配置 ← 用户改的就是这份，最高优先级
]

# 占位符。真实值由 config.json 覆盖（见 load_config）。
_PLACEHOLDER_USER = "someone@example.com"

DEFAULT_CONFIG = {
    # --- 账号（占位符，勿写真实值）---
    "imap_host": "mail.sjtu.edu.cn",
    "imap_port": 993,
    "smtp_host": "mail.sjtu.edu.cn",
    "smtp_port": 465,
    "user": _PLACEHOLDER_USER,
    "from_name": "",
    "self_addresses": [],

    # --- 文件夹（IMAP 名，中文名由服务器 modified UTF-7 承载）---
    "folder_inbox": "INBOX",
    "folder_drafts": "Drafts",
    "folder_sent": "Sent",
    "folder_trash": "Trash",
    "folder_junk": "Junk",
    "folder_archive": "Archive",
    "archive_move_enabled": False,      # 默认「归档」只标已读，不移动（更保守，避免误移）
    # 要同步哪些文件夹。
    #
    # ⚠️ 这里**刻意只放 5 个系统文件夹**：默认配置会随分发包给别人，
    # 不该出现某个人自己的自建文件夹名。自建文件夹请写进**你自己的
    # config.json**（例如 "sync_folders": [..., "专利代理", "未来电池中心"]）。
    #
    # 另外别被「EXAMINE failed」骗了：中文文件夹名在 Zimbra 上是以
    # modified UTF-7 存放的（&ThNSKU7jdAY- = 专利代理）。如果看到「找不到
    # 这个文件夹」，先怀疑名字编解码，而不是文件夹不存在 ——
    # 详见 imap_client.mutf7_encode 的注释。
    "sync_folders": ["INBOX", "Sent", "Drafts", "Junk", "Trash"],

    # --- 本地服务 ---
    "http_host": "127.0.0.1",
    "http_port": 18952,

    # --- 同步 ---
    "idle_enabled": True,
    "idle_reenable_seconds": 1500,      # IDLE 最长 29 分钟，之后重发（RFC 2177）
    "idle_fallback_poll_seconds": 90,   # IDLE 不可用时的降级 NOOP 间隔
    "sync_initial_limit": 300,          # 首次同步每文件夹最多拉多少封
    "sync_body_limit": 60,              # 同步时抓正文的最新 N 封（其余只存元数据）

    # --- Foxmail 历史索引（只读，绝不 GUI 自动化）---
    # 真实路径由安装向导探测后写进 config.json；这里只是形态示例。
    "foxmail_index_path": (
        r"%LOCALAPPDATA%\Foxmail 7.2\Storage\<你的邮箱>\Mails\Index"
    ),
    "foxmail_enabled": True,

    # --- 分类规则 ---
    "classify_rules": "classify_rules.json",

    # --- DraftJobQueue 时间策略 ---
    "max_retry": 3,
    "lease_seconds": 300,
    "queued_timeout_seconds": 7200,
    "generating_timeout_seconds": 900,
    "ready_expire_hours": 72,
    "retry_base_seconds": 60,
    "retry_max_seconds": 1800,
    "draft_context_recent_messages": 6,
    "draft_context_max_messages": 10,

    # --- 默认行为 ---
    "default_draft_mode": "normal",
    "auto_prepare_drafts": False,        # 规则自动预备草稿（默认关闭，Candidate 不自动调 AI）
    "snooze_default_hour": 9,
    "followup_default_days": 3,
    "draft_memory_enabled": True,        # 用户可关闭
    "attachment_summary_enabled": True,
    "auto_classify_new": True,           # 本地规则给新邮件打分类（不调 LLM）

    # --- 工作集（首页工作桶）---
    # 「待我处理」不应该是「所有最新一封是来信的线程」—— 几千封历史邮件会让
    # 这个桶永远收敛不了，TIME TO INBOX ZERO 就失去意义（规范 §30）。
    # 未读的邮件永远在工作集里；已读的只在最近 N 天内还算待处理。
    # 设 0 表示不设窗口（纳入全量）。
    "action_window_days": 30,

    # 「等待对方回复」的活跃窗口（天）。
    #
    # 为什么它需要一个和 action_window_days 分开的旋钮：
    # 这个桶的语义和「待我处理」正相反 —— 球在对方那边，我做什么都不会让它变小。
    # 不给窗口，它就是个只增不减、且我无法用行动消掉的计数器（实测 145 个里
    # 128 个超过 90 天）。用户说得对，那是焦虑，不是工作视图。
    # 超期的会沉底（不删除、不隐藏、可搜索），有 followup 的不受窗口限制。
    # 设 0 表示不设窗口（退回旧行为：全部算活跃等待）。
    "waiting_window_days": 14,

    # 哪些分类算「要我动手」，因而进「待我处理」。
    #   ["IMPORTANT","REPLY","READ"]（默认）= 重点 / 要回 / 看一眼 都算我的事
    #   ["IMPORTANT","REPLY"]                 = 只看真正要办要回的
    # 改成后者能让「待我处理」更聚焦：○看一眼的邮件会移到「仅知会」区，
    # **仍然可见、可搜索、可点开**，不会消失。
    "inbox_informational_classes": [],

    # --- 调度器 ---
    "scheduler_enabled": True,
    "scheduler_tick_seconds": 30,
}

_SECRET_KEYS = ("SJTU_MAIL_USER", "SJTU_MAIL_PASS")
_ENV_ALIASES = {
    "SJTU_MAIL_USER": ["SJTU_MAIL_USER", "MAIL_USER"],
    "SJTU_MAIL_PASS": ["SJTU_MAIL_PASS", "MAIL_PASS", "SJTU_MAIL_PASSWORD"],
}


def _read_env_file(path: str) -> dict:
    out = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def load_secrets() -> dict:
    """返回 {'user':..., 'pass':...}，优先级：环境变量 > 凭据文件。"""
    file_cred = {}
    for p in SECRETS_PATHS:
        got = _read_env_file(p)
        if got:
            file_cred = got
            break

    def pick(canonical):
        for name in _ENV_ALIASES.get(canonical, [canonical]):
            v = os.environ.get(name)
            if v:
                return v
        for name in _ENV_ALIASES.get(canonical, [canonical]):
            if file_cred.get(name):
                return file_cred[name]
        return ""

    return {"user": pick("SJTU_MAIL_USER"), "pass": pick("SJTU_MAIL_PASS")}


def load_config(overrides: dict = None) -> dict:
    """合并默认值 + config.json + 环境变量 + 调用方覆盖。"""
    cfg = dict(DEFAULT_CONFIG)

    for p in CONFIG_PATHS:
        if not os.path.exists(p):
            continue
        try:
            with open(p, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                # V1 config.json 的键名映射到 V2 的命名（保持 V1 配置零改动可用）
                if data.get("index_path") and "foxmail_index_path" not in data:
                    cfg["foxmail_index_path"] = data["index_path"]
                if data.get("drafts_folder") and "folder_drafts" not in data:
                    cfg["folder_drafts"] = data["drafts_folder"]
                if data.get("sent_folder") and "folder_sent" not in data:
                    cfg["folder_sent"] = data["sent_folder"]
                for k, v in data.items():
                    if v is not None and not k.startswith("_"):
                        cfg[k] = v
        except Exception:
            continue

    sec = load_secrets()
    cfg["user"] = os.environ.get("SJTU_MAIL_USER") or sec["user"] or cfg["user"]
    cfg["pass"] = sec["pass"] or ""
    if not cfg.get("self_addresses"):
        cfg["self_addresses"] = [cfg["user"]]

    for k in ("imap_port", "smtp_port", "http_port", "max_retry", "lease_seconds",
              "queued_timeout_seconds", "generating_timeout_seconds", "ready_expire_hours",
              "retry_base_seconds", "retry_max_seconds", "sync_initial_limit",
              "sync_body_limit", "scheduler_tick_seconds"):
        try:
            cfg[k] = int(cfg[k])
        except (TypeError, ValueError):
            cfg[k] = DEFAULT_CONFIG[k]

    for k in ("db_path", "logs_dir", "data_dir", "ui_dir", "attachments_dir",
              "project_root", "workspace_root"):
        cfg.setdefault(k, {"db_path": DB_PATH, "logs_dir": LOGS_DIR, "data_dir": DATA_DIR,
                           "ui_dir": UI_DIR, "attachments_dir": ATTACHMENTS_DIR,
                           "project_root": PROJECT_ROOT,
                           "workspace_root": WORKSPACE_ROOT}[k])

    if overrides:
        cfg.update({k: v for k, v in overrides.items() if v is not None})

    return cfg


def rules_path(cfg: dict) -> str:
    """找分类规则文件。

    优先级（重要，别调换）——用户自己的规则永远优先于包内的通用默认，
    否则升级后分类会突然全变：

      1. config.json 里给的绝对路径              —— 用户显式指定
      2. `~/.workbuddy/skills/sjtu-mail-remote/classify_rules.json` —— 用户自己的
         （V1 时代就在这儿，按他自己几千封邮件调出来的 VIP / 垃圾域名清单）
      3. 项目根目录的 classify_rules.json        —— 项目本地的用户规则
      4. `mail_workbench/classify_rules.default.json` —— **包内中性默认**，只给新装机用

    包内默认故意用 `.default.json` 后缀：这样它既不会遮蔽用户放在项目根的
    同名文件，也不会被误当成「用户已经调过的规则」。
    """
    name = cfg.get("classify_rules") or "classify_rules.json"
    if os.path.isabs(name):
        return name
    base, ext = os.path.splitext(name)
    default_name = base + ".default" + ext
    candidates = [
        os.path.join(SKILL_DIR, name),        # 用户自己的（优先）
        os.path.join(PROJECT_ROOT, name),     # 项目本地的用户规则
        os.path.join(HERE, default_name),     # 包内默认（兜底）
    ]
    for cand in candidates:
        if os.path.exists(cand):
            return cand
    return candidates[2]


def ensure_dirs(cfg: dict = None) -> None:
    cfg = cfg or load_config()
    for d in (cfg["data_dir"], cfg["logs_dir"]):
        os.makedirs(d, exist_ok=True)


def secrets_file_hint() -> str:
    return SECRETS_PATHS[0]


def has_credentials(cfg: dict = None) -> bool:
    cfg = cfg or load_config()
    return bool(cfg.get("user") and cfg.get("pass"))


def main() -> int:
    """`python -m mail_workbench.config` —— 自检凭据与关键路径。"""
    cfg = load_config()
    ok = True
    print("user            :", cfg["user"])
    print("password        :", "OK (已加载)" if cfg.get("pass") else "MISSING -> %s" % secrets_file_hint())
    if not cfg.get("pass"):
        ok = False
    print("imap            : %s:%s" % (cfg["imap_host"], cfg["imap_port"]))
    print("smtp            : %s:%s" % (cfg["smtp_host"], cfg["smtp_port"]))
    print("http            : %s:%s" % (cfg["http_host"], cfg["http_port"]))
    print("db_path         :", cfg["db_path"])
    print("logs_dir        :", cfg["logs_dir"])
    ip = cfg["foxmail_index_path"]
    print("foxmail index   :", ip, "(存在)" if os.path.exists(ip) else "(缺失，历史检索将不可用)")
    rp = rules_path(cfg)
    print("classify rules  :", rp, "(存在)" if os.path.exists(rp) else "(缺失)")
    print("ui_dir          :", cfg["ui_dir"], "(存在)" if os.path.isdir(cfg["ui_dir"]) else "(缺失)")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
