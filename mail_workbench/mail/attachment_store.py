# -*- coding: utf-8 -*-
r"""附件落盘与「用默认程序打开」。

设计要点：

1. **按需下载，不预取**。附件只有在你真的点它时才会从 IMAP 拉取 ——
   本地只先存 filename/type/size/hash（规范 §22），不把附件默认喂给模型，
   也不为了「提前准备好」而白白下载几百 MB。

2. **内容指纹当缓存键**。文件名会重复、会被改名，`sha256` 不会。
   落盘路径用 `sha256前16位_安全文件名`，于是同一附件只下一份，
   重复点击直接命中缓存。

3. **文件名必须消毒**。附件名来自外部邮件，可以包含 `../` 或 `C:\`，
   直接拼路径就是目录穿越漏洞。这里只保留基名并过滤非法字符，
   且最终拼出的路径必须仍在 attachments_dir 之内（二次兜底）。

4. **「用默认程序打开」是本机行为**。只在本机启动一个查看器打开刚下载的文件，
   不发信、不改邮箱、不碰网络。因此不违反「AI 绝不发信」的边界。
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

from .. import util
from . import imap_client as imc
from . import parser as mparser

# Windows 文件名非法字符 + 控制字符 + 路径分隔符
_BAD_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_RESERVED_WIN = {
    "CON", "PRN", "AUX", "NUL",
    *(("COM%d" % i) for i in range(1, 10)),
    *(("LPT%d" % i) for i in range(1, 10)),
}
MAX_FILENAME = 120


def safe_filename(name: str) -> str:
    """把邮件里的附件名变成一个安全的本地文件名。

    只取基名（挡掉 `../` 与 `C:\\`），过滤非法字符与首尾点空格，
    过长则截断但保留扩展名，并回避 Windows 保留名。
    """
    raw = (name or "").strip()
    if not raw:
        return "attachment"
    # 统一分隔符后只取最后一段：/etc/passwd、..\..\x、C:\a\b 都只剩最后一段
    raw = raw.replace("\\", "/").rsplit("/", 1)[-1]
    raw = raw.replace("..", "_")
    cleaned = _BAD_CHARS.sub("_", raw).strip(" .")
    # 只剩点/下划线（比如原名就是 "...."）等于没有名字，给个兜底
    if not cleaned or not cleaned.strip("._"):
        return "attachment"
    if len(cleaned) > MAX_FILENAME:
        stem, ext = os.path.splitext(cleaned)
        keep = max(1, MAX_FILENAME - len(ext))
        cleaned = stem[:keep] + ext
    stem = os.path.splitext(cleaned)[0].upper()
    if stem in _RESERVED_WIN:
        cleaned = "_" + cleaned
    return cleaned


def local_path(cfg: dict, sha256: str, filename: str) -> str:
    """算出附件的本地落盘路径（确定性：同一 sha256 永远同一路径）。"""
    base = cfg.get("attachments_dir") or os.path.join(cfg.get("project_root", "."), "attachments")
    key = (sha256 or util.sha256_text(filename or "x"))[:16]
    path = os.path.join(base, "%s_%s" % (key, safe_filename(filename)))
    # 兜底：即便上面哪一步漏了，也不许越出 attachments_dir
    root = os.path.abspath(base)
    if not os.path.abspath(path).startswith(root + os.sep):
        path = os.path.join(root, "%s_attachment" % key)
    return path


def ensure_dir(cfg: dict) -> str:
    base = cfg.get("attachments_dir") or os.path.join(cfg.get("project_root", "."), "attachments")
    os.makedirs(base, exist_ok=True)
    return base


class AttachmentError(Exception):
    pass


def _fetch_bytes(cfg: dict, message_row: dict, att_row: dict, log=None) -> bytes:
    """从 IMAP 取回附件字节，按 sha256 精确匹配 MIME part。"""
    mid = message_row.get("message_id") or ""
    folder = message_row.get("folder") or "INBOX"
    uid = message_row.get("uid")
    if not uid:
        raise AttachmentError(
            "这封邮件没有 IMAP UID（多为 Foxmail 历史记录，本地只有元数据），"
            "无法下载附件")
    if message_row.get("source") == "foxmail":
        raise AttachmentError("Foxmail 历史记录不含附件内容，无法下载")

    client = imc.ImapClient(cfg, log=log)
    try:
        client.connect()
        client.select(folder, readonly=True)
        got = client.fetch_full([int(uid)])
    except imc.ImapError as e:
        raise AttachmentError("连邮箱取附件失败：%s" % e)
    finally:
        client.close()

    raw = (got or {}).get(int(uid), {}).get("raw")
    if not raw:
        raise AttachmentError("邮箱里没取到这封邮件的正文（可能已被删除或移走）")

    want = (att_row.get("sha256") or "").strip()
    want_name = (att_row.get("filename") or "").strip()
    parts = mparser.list_attachments(raw, with_data=True)
    if not parts:
        raise AttachmentError("这封邮件里没有可下载的附件")

    if want:
        for p in parts:
            if p["sha256"] == want:
                return p["data"]
    # sha 对不上（例如入库后又改过）时按文件名 + 大小兜底
    size = int(att_row.get("size_bytes") or 0)
    for p in parts:
        if p["filename"] == want_name and (not size or p["size_bytes"] == size):
            return p["data"]
    raise AttachmentError("邮件里找不到这个附件（可能已被发件人替换或邮件被改动）")


def download(cfg: dict, repo, attachment_id: str, log=None, force: bool = False) -> dict:
    """把附件下载到本地（已存在则直接复用），返回 {path, filename, size_bytes, cached}。"""
    att = repo.get_attachment(attachment_id)
    if not att:
        raise AttachmentError("附件记录不存在：%s" % attachment_id)
    mid = att.get("message_id")
    msg = repo.get_message(mid) if mid else None
    if not msg:
        raise AttachmentError("附件所属的邮件不在本地库里")

    path = local_path(cfg, att.get("sha256"), att.get("filename"))
    if os.path.exists(path) and os.path.getsize(path) > 0 and not force:
        return {"path": path, "filename": att.get("filename") or os.path.basename(path),
                "size_bytes": os.path.getsize(path), "cached": True}

    data = _fetch_bytes(cfg, msg, att, log=log)
    ensure_dir(cfg)
    tmp = path + ".part"
    with open(tmp, "wb") as f:
        f.write(data)
    os.replace(tmp, path)
    if log:
        log("附件已下载：%s（%d 字节）" % (os.path.basename(path), len(data)))
    return {"path": path, "filename": att.get("filename") or os.path.basename(path),
            "size_bytes": len(data), "cached": False}


def is_downloaded(cfg: dict, att: dict) -> bool:
    try:
        p = local_path(cfg, att.get("sha256"), att.get("filename"))
        return os.path.exists(p) and os.path.getsize(p) > 0
    except Exception:
        return False


# --------------------------------------------------------------------------
# 用系统默认程序打开
# --------------------------------------------------------------------------
def open_with_default(path: str) -> dict:
    """用操作系统的默认程序打开本地文件。

    这是**纯本机**动作：等价于你在资源管理器里双击它。
    不发信、不改邮箱、不访问网络，因此不触碰「AI 绝不发信」那条边界。
    """
    if not path or not os.path.exists(path):
        raise AttachmentError("文件不存在：%s" % path)
    try:
        if os.name == "nt":
            os.startfile(path)  # noqa: S606  （Windows 专用，正是意图）
        elif sys.platform == "darwin":
            subprocess.Popen(["open", path])
        else:
            subprocess.Popen(["xdg-open", path])
    except OSError as e:
        raise AttachmentError("调起默认程序失败：%s" % e)
    return {"ok": True, "path": path}


def reveal(path: str) -> dict:
    """在文件管理器里定位该文件（下载但不打开时用）。"""
    if not path or not os.path.exists(path):
        raise AttachmentError("文件不存在：%s" % path)
    try:
        if os.name == "nt":
            subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["xdg-open", os.path.dirname(path) or "."])
    except OSError as e:
        raise AttachmentError("打开文件管理器失败：%s" % e)
    return {"ok": True, "path": path}
