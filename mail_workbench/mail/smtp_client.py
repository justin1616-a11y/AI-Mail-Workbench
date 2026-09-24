# -*- coding: utf-8 -*-
"""SMTP 发送（**唯一**的出站通道，且必须经过人类确认门禁）。

安全边界（规范 §33）：
    AI 可以做：draft / rewrite / summarize
    AI 不可以做：send / delete permanently / forward externally / 改账号凭据

因此本模块**不暴露给 Worker 契约**。调用链固定为：

    用户点「确认发送」
      -> draft.queue.approve()   产生一次性 approve_token（10 分钟有效）
      -> 用户再带 token 调 /api/drafts/{id}/send
      -> draft.queue.authorize_send() 校验 token + 状态必须是 approved
      -> 才允许调用本模块 smtp_client.send()

任何绕过 approve_token 的调用都会抛 PermissionError。
"""
from __future__ import annotations

import smtplib
import ssl
import time
from email.header import Header
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid

from .. import util


class SendDenied(PermissionError):
    """没有人类显式确认 —— 一律拒绝发送。"""


def crlf(text: str) -> str:
    """把正文换行统一成 CRLF。

    RFC 5322 要求行尾 CRLF。V1 的教训：用 LF 写进草稿箱后，Foxmail 打开会
    把每个 LF 当「行尾 + 段落分隔」，保存后段落间变成 3~5 个空行。
    """
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


def build_message(cfg: dict, to, subject: str, body: str,
                  in_reply_to: str = "", references=None,
                  cc=None, signature: str = "") -> bytes:
    """构造待发邮件原始字节（不发送）。"""
    from_name = cfg.get("from_name") or ""
    sender = cfg.get("user") or ""
    text = body or ""
    if signature and signature.strip() and signature.strip() not in text:
        text = text.rstrip() + "\n\n" + signature.strip()

    msg = MIMEText(crlf(text), "plain", "utf-8")
    msg["From"] = "%s <%s>" % (Header(from_name, "utf-8").encode(), sender) if from_name else sender
    if isinstance(to, (list, tuple)):
        msg["To"] = ", ".join(to)
    else:
        msg["To"] = to or ""
    if cc:
        msg["Cc"] = ", ".join(cc) if isinstance(cc, (list, tuple)) else cc
    msg["Subject"] = Header(subject or "(无主题)", "utf-8").encode()
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=(sender.split("@")[-1] if "@" in sender else None))
    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to
        refs = references or [in_reply_to]
        msg["References"] = " ".join(refs)
    return msg.as_bytes()


def send(cfg: dict, raw: bytes, recipients, dry_run: bool = False) -> dict:
    """真正投递。recipients 必须显式传入（不从头部猜）。"""
    if dry_run:
        return {"ok": True, "dry_run": True, "recipients": recipients}

    host = cfg.get("smtp_host", "mail.sjtu.edu.cn")
    port = int(cfg.get("smtp_port", 465))
    user, pwd = cfg.get("user"), cfg.get("pass")
    if not user or not pwd:
        raise SendDenied("缺少 SMTP 凭据")

    ctx = ssl.create_default_context()
    t0 = time.time()
    try:
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, timeout=30, context=ctx)
        else:
            s = smtplib.SMTP(host, port, timeout=30)
            s.ehlo()
            try:
                s.starttls(context=ctx)
                s.ehlo()
            except smtplib.SMTPException:
                pass
        with s:
            s.login(user, pwd)
            s.sendmail(user, list(recipients), raw)
        return {"ok": True, "latency_ms": round((time.time() - t0) * 1000, 1),
                "recipients": list(recipients)}
    except Exception as e:
        return {"ok": False, "error": str(e), "recipients": list(recipients)}


def verify_connection(cfg: dict) -> dict:
    """给 /api/health 用的 SMTP 探活：只登录不投递。"""
    host = cfg.get("smtp_host", "mail.sjtu.edu.cn")
    port = int(cfg.get("smtp_port", 465))
    user, pwd = cfg.get("user"), cfg.get("pass")
    if not user or not pwd:
        return {"ok": False, "error": "缺少凭据"}
    t0 = time.time()
    try:
        ctx = ssl.create_default_context()
        if port == 465:
            s = smtplib.SMTP_SSL(host, port, timeout=15, context=ctx)
        else:
            s = smtplib.SMTP(host, port, timeout=15)
            s.ehlo()
            try:
                s.starttls(context=ctx)
                s.ehlo()
            except smtplib.SMTPException:
                pass
        with s:
            s.login(user, pwd)
        return {"ok": True, "latency_ms": round((time.time() - t0) * 1000, 1),
                "host": host, "port": port}
    except Exception as e:
        return {"ok": False, "error": str(e), "host": host, "port": port}


def guard_send_context(job: dict, token_ok: bool) -> None:
    """发送前置校验。集中在一处，避免各处重复判断导致漏网。"""
    if not token_ok:
        raise SendDenied("缺少或已失效的人类确认令牌（approve_token）")
    if not job:
        raise SendDenied("草稿任务不存在")
    if job.get("status") != "approved":
        raise SendDenied("草稿任务状态为 %s，只有 approved 才能发送" % job.get("status"))
    if not (job.get("draft_text") or "").strip():
        raise SendDenied("草稿正文为空，拒绝发送")
    if not job.get("recipients"):
        raise SendDenied("收件人为空，拒绝发送")
    if job.get("claimed_by"):
        # 允许发送，但要清掉租约（由调用方处理）
        pass
