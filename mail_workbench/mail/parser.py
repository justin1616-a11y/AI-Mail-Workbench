# -*- coding: utf-8 -*-
"""MIME 解析。

复用并现代化 V1 的经验（见原 SKILL.md「索引格式备忘」）：
  * 主题在服务器上是 MIME 编码且**常跨行折叠**，必须交给 `email.message_from_bytes`
    整体解析，自己按行切会截断 `=?GB2312?B?...?=` 导致中文关键词搜不到。
  * 已发送邮件常用 base64，直接读 BODY[TEXT] 拿到的是未解码原文，
    必须走 `part.get_payload(decode=True)`。
  * 正文换行统一成 LF 入库（写草稿时再由 draft 层转 CRLF，否则 Foxmail 打开会多出空行）。
"""
from __future__ import annotations

import email
import email.header
import email.utils
import re
from email import message_from_bytes

from .. import util

MAX_BODY_CHARS = 20000


def decode_mime_str(value) -> str:
    """解邮件头的 =?utf-8?B?...?= / =?GB2312?B?...?= 编码。"""
    if not value:
        return ""
    try:
        parts = email.header.decode_header(value)
    except Exception:
        return str(value)
    out = []
    for text, enc in parts:
        if isinstance(text, bytes):
            for c in (enc, "utf-8", "gbk"):
                try:
                    out.append(text.decode(c or "utf-8"))
                    break
                except (UnicodeDecodeError, LookupError):
                    continue
            else:
                out.append(text.decode("utf-8", "replace"))
        else:
            out.append(text)
    return "".join(out)


def parse_from(value: str) -> tuple:
    if not value:
        return "", ""
    name, addr = email.utils.parseaddr(value)
    return util.collapse(decode_mime_str(name)), (addr or "").strip().lower()


def parse_addr_list(value: str) -> list:
    """解析 To/Cc 里的全部地址。"""
    if not value:
        return []
    try:
        pairs = email.utils.getaddresses([value])
    except Exception:
        pairs = []
    out = []
    for _name, addr in pairs:
        a = (addr or "").strip().lower()
        if a and util.EMAIL_FULL_RE.match(a) and a not in out:
            out.append(a)
    if not out:
        out = util.parse_recipients(value)
    return out


def parse_date(value: str) -> str:
    if not value:
        return ""
    try:
        dt = email.utils.parsedate_to_datetime(value)
    except Exception:
        return ""
    if dt is None:
        return ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=util.TZ_CST)
    return dt.astimezone(util.TZ_CST).replace(microsecond=0).isoformat()


def parse_message_id(value: str) -> str:
    """从 Message-ID 头里取纯净的 `<...>` 形式。"""
    if not value:
        return ""
    m = re.search(r"<[^<>]+>", value)
    if m:
        return m.group(0).strip()
    return util.collapse(value)


def parse_refs(value: str) -> list:
    if not value:
        return []
    return re.findall(r"<[^<>]+>", value)


def html_to_text(html: str) -> str:
    """把 HTML 正文降级成纯文本（V1 的做法，补上更细的块级处理）。"""
    if not html:
        return ""
    s = re.sub(r"<(script|style|head)[^>]*>.*?</\1>", " ", html,
               flags=re.DOTALL | re.IGNORECASE)
    s = re.sub(r"<!--.*?-->", " ", s, flags=re.DOTALL)
    s = re.sub(r"<br\s*/?>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"</(p|div|li|tr|h[1-6]|blockquote|section|article)>", "\n", s, flags=re.IGNORECASE)
    s = re.sub(r"<li[^>]*>", " • ", s, flags=re.IGNORECASE)
    s = re.sub(r"<[^>]+>", "", s)
    for a, b in (("&nbsp;", " "), ("&lt;", "<"), ("&gt;", ">"), ("&quot;", '"'),
                 ("&#39;", "'"), ("&apos;", "'"), ("&mdash;", "—"), ("&ndash;", "–"),
                 ("&hellip;", "…"), ("&amp;", "&")):
        s = s.replace(a, b)
    s = re.sub(r"&#(\d+);", lambda m: chr(int(m.group(1))), s)
    s = re.sub(r"[ \t]+\n", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _decode_part(part) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        raw = part.get_payload()
        return raw if isinstance(raw, str) else ""
    charset = part.get_content_charset() or "utf-8"
    for c in (charset, "utf-8", "gbk"):
        try:
            return payload.decode(c, "replace")
        except (LookupError, UnicodeDecodeError):
            continue
    return payload.decode("utf-8", "replace")


def walk_parts(msg, depth: int = 0):
    """深度优先遍历所有叶子 part（含内嵌 message/rfc822）。"""
    if depth > 8:
        return
    if msg.is_multipart():
        for sub in msg.get_payload():
            if isinstance(sub, email.message.Message):
                yield from walk_parts(sub, depth + 1)
        return
    yield msg


_DISP_RE = re.compile(r"^\s*(attachment|inline)\b", re.I)
_FILENAME_RE = re.compile(r'filename\*?\s*=\s*"([^"]*)"|filename\*?\s*=\s*([^;]+)', re.I)


def attachment_meta(part) -> tuple:
    """取 (disposition, filename)，对**不合规的写法也能认出来**。

    现实中附件名有三种写法，第三种最容易出问题：
      1. 规范 RFC2231：`filename*=utf-8''%E7%AE%80%E5%8E%86.pdf`
      2. RFC2047：`filename="=?utf-8?B?...?="`
      3. **裸 UTF-8 中文**：`filename="简历.pdf"` 直接把 UTF-8 字节写进头里
         （不合规，但在中文邮件里很常见）

    第 3 种情况下 Python 解析出的整条 Content-Disposition 会变成一个
    RFC2047 blob，于是 `get_filename()` 返回 None、`get_content_disposition()`
    也认不出来 —— **附件会被整个丢掉，用户根本看不到它存在**。
    所以这里对拿不到的情况回退到「解码整条头 + 正则抽 filename」。
    """
    disp = part.get_content_disposition()
    fname = part.get_filename()
    raw = part.get("Content-Disposition") or ""

    # get_filename() 有时会把 RFC2047 的原文**原样**返回（`=?utf-8?B?...?=`），
    # 所以不能只在拿不到时解码 —— 只要看着像编码就得解。
    if fname and "=?" in fname:
        fname = decode_mime_str(fname) or fname

    if not raw:
        return disp, fname

    decoded = decode_mime_str(raw) if "=?" in raw else raw
    if disp not in ("attachment", "inline"):
        m = _DISP_RE.match(decoded)
        disp = m.group(1).lower() if m else disp
    if not fname:
        m = _FILENAME_RE.search(decoded)
        if m:
            cand = (m.group(1) or m.group(2) or "").strip().strip('"')
            if cand:
                fname = decode_mime_str(cand) if "=?" in cand else cand
    return disp, fname


def is_attachment(part) -> bool:
    disp, fn = attachment_meta(part)
    if disp == "attachment":
        return True
    if fn and part.get_content_type() not in ("text/plain", "text/html"):
        return True
    return False


def collect(msg) -> dict:
    """从已解析的 MIME 对象里抽取正文与附件信息。"""
    plains, htmls, atts = [], [], []

    for part in walk_parts(msg):
        ctype = part.get_content_type()
        disp, fn = attachment_meta(part)
        if is_attachment(part):
            data = part.get_payload(decode=True) or b""
            atts.append({
                "filename": util.collapse(fn) if fn else "(未命名)",
                "content_type": ctype,
                "size_bytes": len(data),
                "sha256": util.sha256_bytes(data) if data else "",
            })
            continue
        if disp == "inline" and fn:
            # 内联图片：不算附件，但也不当正文
            continue
        if ctype == "text/plain":
            plains.append(_decode_part(part))
        elif ctype == "text/html":
            htmls.append(_decode_part(part))

    if plains:
        body = max(plains, key=len)
    elif htmls:
        body = html_to_text(max(htmls, key=len))
    else:
        body = ""
    body = util.meta_clean(body, MAX_BODY_CHARS)
    # 去掉常见的 "> " 引用块压缩后的极端长度，但保留内容供 thread context 使用
    return {"body_text": body, "attachments": atts}


def list_attachments(raw: bytes, with_data: bool = True) -> list:
    """列出附件的**完整信息**，含解码后的字节（`data`）。

    与 `collect()` 用同一套 `walk_parts` + `is_attachment` 判定，
    因此顺序、文件名、大小、sha256 与入库的元数据一一对应。

    为什么不用「第 N 个附件」这种下标来配对：入库后是按 filename 排序取的
    （`repo.attachments_for_message`），下标早就对不上了。
    调用方应拿 **sha256** 去匹配 —— 那是内容指纹，改名也不会错配。
    """
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    msg = message_from_bytes(raw)
    out = []
    for part in walk_parts(msg):
        if not is_attachment(part):
            continue
        data = part.get_payload(decode=True) or b""
        _disp, fn = attachment_meta(part)
        item = {
            "filename": util.collapse(fn) if fn else "(未命名)",
            "content_type": part.get_content_type(),
            "size_bytes": len(data),
            "sha256": util.sha256_bytes(data) if data else "",
        }
        if with_data:
            item["data"] = data
        out.append(item)
    return out


def parse_message(raw: bytes, body_limit: int = MAX_BODY_CHARS) -> dict:
    """把 RFC822 原始字节解析成入库字典（不含 account/folder/uid）。"""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    msg = message_from_bytes(raw)
    from_name, from_addr = parse_from(msg.get("From", ""))
    info = collect(msg)
    body = util.meta_clean(info["body_text"], body_limit)

    atts = info["attachments"]
    return {
        "message_id": parse_message_id(msg.get("Message-ID", "")),
        "subject": util.collapse(decode_mime_str(msg.get("Subject", ""))),
        "from_name": from_name,
        "from_addr": from_addr,
        "to_addrs": parse_addr_list(msg.get("To", "")),
        "cc_addrs": parse_addr_list(msg.get("Cc", "")),
        "date_iso": parse_date(msg.get("Date", "")),
        "in_reply_to": parse_message_id(msg.get("In-Reply-To", "")),
        "references_ids": parse_refs(msg.get("References", "")),
        "body_text": body,
        "snippet": util.collapse(body[:300]),
        "has_attachments": bool(atts),
        "attachment_names": [a["filename"] for a in atts],
        "attachments": atts,
        "size_bytes": len(raw),
        "list_unsubscribe": bool(msg.get("List-Unsubscribe")),
        "auto_submitted": util.collapse(msg.get("Auto-Submitted", "")).lower() not in ("", "no"),
        "precedence": util.collapse(msg.get("Precedence", "")).lower(),
    }


def parse_headers_only(raw: bytes) -> dict:
    """只解析头（同步历史邮件时的轻量路径，不碰正文）。"""
    if isinstance(raw, str):
        raw = raw.encode("utf-8", "replace")
    msg = message_from_bytes(raw)
    from_name, from_addr = parse_from(msg.get("From", ""))
    return {
        "message_id": parse_message_id(msg.get("Message-ID", "")),
        "subject": util.collapse(decode_mime_str(msg.get("Subject", ""))),
        "from_name": from_name,
        "from_addr": from_addr,
        "to_addrs": parse_addr_list(msg.get("To", "")),
        "cc_addrs": parse_addr_list(msg.get("Cc", "")),
        "date_iso": parse_date(msg.get("Date", "")),
        "in_reply_to": parse_message_id(msg.get("In-Reply-To", "")),
        "references_ids": parse_refs(msg.get("References", "")),
        "body_text": "",
        "snippet": "",
    }


def synth_message_id(account: str, folder: str, uid) -> str:
    """服务器没给 Message-ID 时合成稳定键。

    注意用 UID 而不是 sequence number —— Zimbra 的 UID 空间与 seq 空间不同
    （V1 曾因混淆二者导致 `openEmail` 匹配不到、点击无反应）。
    """
    return "<synth-%s-%s-%s@mail-workbench.local>" % (
        util.sha256_text((account or "").lower())[:8], util.sha256_text(folder or "")[:8], uid)


def ensure_message_id(info: dict, account: str, folder: str, uid) -> dict:
    if not info.get("message_id"):
        info["message_id"] = synth_message_id(account, folder, uid)
    return info


def build_thread_key(info: dict) -> tuple:
    """返回 (thread_id, reason)。

    优先用 References/In-Reply-To 的**最根节点**（RFC 5322 定义线程的可靠依据）；
    没有的话退回「归一化主题」。两条路都保留 reason 便于调试线程误合并。
    """
    refs = list(info.get("references_ids") or [])
    root = refs[0] if refs else (info.get("in_reply_to") or "")
    if root:
        return "thr_" + util.sha256_text(root)[:20], "references:%s" % root
    norm = util.normalize_subject(info.get("subject"))
    if norm:
        return "thr_" + util.sha256_text("subj:" + norm)[:20], "subject:%s" % norm
    mid = info.get("message_id") or ""
    return "thr_" + util.sha256_text("mid:" + mid)[:20], "message_id"
