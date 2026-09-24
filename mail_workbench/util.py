# -*- coding: utf-8 -*-
"""通用工具：时间、ID、哈希、主题归一化、收件人解析、文本清洗。

全部使用标准库，无第三方依赖（与 V1 保持一致的可维护性）。
"""
from __future__ import annotations

import datetime
import hashlib
import os
import re
import secrets
import uuid

TZ_CST = datetime.timezone(datetime.timedelta(hours=8))

# 主题归一化：剥掉回复/转发前缀（中英日），用于线程合并
_SUBJ_PREFIX = re.compile(
    r"^\s*(?:(?:re|fwd?|fw|答复|回复|转发|答复|回覆|轉發|轉寄|返信|転送)"
    r"\s*(?:\[\d+\])?\s*[:：]\s*)+",
    re.IGNORECASE,
)
# 列表标签 [xxx] / 【xxx】 在主题里一般不代表新话题，保留但折叠空白
_WS = re.compile(r"\s+")
_INVISIBLE = re.compile(r"[\u200b-\u200f\u2060\ufeff\u00ad]")

# 邮箱右边界用 TLD 白名单锚定（Foxmail 索引文本区无分隔符，普通正则会把
# TLD 后的下一个字段吞进来 —— 见 V1 SKILL.md「索引格式备忘」）。
TLD = (
    r"(?:com|cn|net|org|edu|gov|info|biz|io|ai|co|me|dev|app|tech|online|site|shop|store|"
    r"top|xyz|club|wiki|space|live|email|cloud|data|digital|network|center|world|today|news|"
    r"life|work|company|solutions|systems|group|team|zone|fun|host|press|run|social|software|"
    r"studio|tips|video|website|ac|ae|af|ag|am|as|at|au|be|bg|br|ca|cc|ch|cl|cm|cz|de|dk|ee|"
    r"es|eu|fi|fm|fr|ge|gg|gl|hk|hr|hu|id|ie|il|im|in|iq|ir|is|it|jp|ke|kr|kz|la|lk|lt|lu|lv|"
    r"ly|ma|md|mk|ml|mn|ms|mt|mu|mx|my|nl|no|np|nu|nz|om|pa|pe|pk|pl|pm|ps|pt|pw|qa|ro|rs|ru|"
    r"se|sg|sh|si|sk|sm|sn|so|st|su|sx|tc|th|tj|tk|tl|tm|tn|to|tr|tv|tw|tz|ua|ug|uk|us|uy|uz|"
    r"vc|ve|vn|ws|za|zm|zw)"
)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@(?:[A-Za-z0-9\-]+\.)+" + TLD, re.IGNORECASE)
EMAIL_FULL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@(?:[A-Za-z0-9\-]+\.)+" + TLD + r"$", re.IGNORECASE)


# --------------------------------------------------------------------------
# 时间
# --------------------------------------------------------------------------
def now() -> datetime.datetime:
    return datetime.datetime.now(TZ_CST)


def now_iso() -> str:
    return now().replace(microsecond=0).isoformat()


def parse_iso(value):
    """宽松解析 ISO 时间串；失败返回 None。"""
    if not value:
        return None
    if isinstance(value, datetime.datetime):
        return value if value.tzinfo else value.replace(tzinfo=TZ_CST)
    try:
        s = str(value).strip().replace("Z", "+00:00")
        dt = datetime.datetime.fromisoformat(s)
        return dt if dt.tzinfo else dt.replace(tzinfo=TZ_CST)
    except Exception:
        return None


def to_epoch(value) -> float:
    dt = parse_iso(value)
    return dt.timestamp() if dt else 0.0


def hours_between(a, b) -> float:
    da, db = parse_iso(a), parse_iso(b)
    if not da or not db:
        return 0.0
    return (db - da).total_seconds() / 3600.0


def add_seconds(iso_value: str, seconds: float) -> str:
    dt = parse_iso(iso_value) or now()
    return (dt + datetime.timedelta(seconds=seconds)).replace(microsecond=0).isoformat()


def add_days(iso_value: str, days: float) -> str:
    return add_seconds(iso_value, days * 86400)


def today_key() -> str:
    return now().strftime("%Y-%m-%d")


# --------------------------------------------------------------------------
# ID / 哈希
# --------------------------------------------------------------------------
def new_id(prefix: str) -> str:
    return "%s_%s" % (prefix, uuid.uuid4().hex[:16])


def new_token(nbytes: int = 24) -> str:
    return secrets.token_urlsafe(nbytes)


def sha256_text(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8", "replace")).hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data or b"").hexdigest()


def sha256_file(path: str, chunk: int = 1 << 20) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(chunk)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# --------------------------------------------------------------------------
# 文本
# --------------------------------------------------------------------------
def clean(value) -> str:
    """去掉零宽/软连字符等不可见字符并折叠首尾空白。"""
    return _INVISIBLE.sub("", value or "").strip()


def collapse(value) -> str:
    return _WS.sub(" ", clean(value))


def strip_subject_prefix(subject: str) -> str:
    prev = None
    s = clean(subject)
    while prev != s:
        prev = s
        s = _SUBJ_PREFIX.sub("", s)
    return collapse(s)


def normalize_subject(subject: str) -> str:
    """线程合并键用的主题归一化：去前缀 + 折叠空白 + 小写 + 去常见尾部标记。"""
    s = strip_subject_prefix(subject).lower()
    s = re.sub(r"^\[(?:external|外部|spam|bulk)\]\s*", "", s)
    return collapse(s)


_STOP_SUBJECTS = {"", "(无主题)", "(no subject)", "无主题", "no subject"}


def subject_is_empty(subject: str) -> bool:
    return collapse(subject).lower() in _STOP_SUBJECTS


def parse_recipients(raw: str) -> list:
    """把 To/Cc 头切成邮箱地址列表。"""
    if not raw:
        return []
    out = []
    for part in re.split(r"[,;]", raw):
        found = EMAIL_RE.findall(part or "")
        for f in found:
            a = f.lower().strip()
            if a and a not in out:
                out.append(a)
    return out


def split_addrs(raw: str) -> list:
    return parse_recipients(raw)


def domain_of(addr: str) -> str:
    return (addr or "").lower().split("@")[-1]


def redact(addr: str) -> str:
    """日志用：只保留域名，避免把联系人邮箱写进普通日志。"""
    if not addr or "@" not in addr:
        return ""
    local, dom = addr.split("@", 1)
    return "%s***@%s" % (local[:2], dom)


def meta_clean(s: str, limit: int = 20000) -> str:
    """入库前的正文清洗：统一换行、去掉超长尾部引用。"""
    t = (s or "").replace("\r\n", "\n").replace("\r", "\n")
    t = _INVISIBLE.sub("", t)
    if len(t) > limit:
        t = t[:limit] + "\n...[truncated]"
    return t


def guess_language(text: str) -> str:
    """极轻量语言判定：中日韩字符占比。用于 ContactContext / Draft Memory。"""
    if not text:
        return "unknown"
    cjk = len(re.findall(r"[\u4e00-\u9fff]", text))
    latin = len(re.findall(r"[A-Za-z]", text))
    if cjk == 0 and latin == 0:
        return "unknown"
    return "zh" if cjk * 3 >= latin else "en"


def relpath_under(base: str, path: str) -> bool:
    try:
        return os.path.commonpath([os.path.abspath(base), os.path.abspath(path)]) == os.path.abspath(base)
    except Exception:
        return False
