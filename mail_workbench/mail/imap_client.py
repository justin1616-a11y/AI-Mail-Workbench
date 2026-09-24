# -*- coding: utf-8 -*-
"""IMAP 客户端（含 IMAP IDLE 事件驱动）。

关键修正（相对 V1）：
  * V1 的 `_fetch_body.py` 用 **sequence number** 当 uid 存进视图，导致
    `inbox.html` 的标识与服务器真实 UID 不一致（V1 日志里记录了这个坑：
    「Zimbra 此服务器 UID FETCH 返回的 UID 是 message-sequence 而非真实 UID」）。
    V2 统一用 `UID SEARCH` / `UID FETCH` 取**真实 UID**，并在 store 里单列一栏。
  * V1 用「指数+二分探测总数」绕过 Zimbra 的 FETCH 越界报错。V2 直接用
    `UID SEARCH ALL` 拿 UID 列表，既准确又少一次往返。
  * 非 ASCII 文件夹名（&ThNSKU7jdAY- = 专利代理）用 UTF-8 字符串字面量发送，
    这是 V1 实测通过的方式；同时提供 modified UTF-7 解码用于展示。
  * **只读优先**：默认 `EXAMINE`（readonly），只有显式动作才会 SELECT 可写。
  * **不做 GUI 自动化**，一切走标准协议（规范 §32）。
"""
from __future__ import annotations

import base64
import imaplib
import re
import socket
import threading
import time

from .. import util
from . import parser as mparser

DEFAULT_TIMEOUT = 30


class ImapError(Exception):
    pass


# --------------------------------------------------------------------------
# 文件夹名编解码
# --------------------------------------------------------------------------
def mutf7_decode(s: str) -> str:
    """解 IMAP modified UTF-7（&ThNSKU7jdAY- -> 专利代理）。

    注意 `&-` 是**转义后的字面 &**（不是空段）。早期版本把它当空段解码，
    于是名字里带 `&` 的文件夹会被显示成少了那个字符
    （`A&B` -> `AB`），用户会以为文件夹被改过名。
    """

    def rep(mo):
        raw = mo.group(1).replace(",", "/")     # modified BASE64：, 就是 /
        if not raw:
            return "&"
        pad = (-len(raw)) % 4
        try:
            return base64.b64decode(raw + "=" * pad).decode("utf-16-be")
        except Exception:
            return mo.group(0)

    return re.sub(r"&([A-Za-z0-9+/,]*)-", rep, s or "")


def mutf7_encode(s: str) -> str:
    """编 IMAP modified UTF-7（RFC 3501 §5.1.3）。

    ⚠️ 这里有个必须记住的坑：modified UTF-7 用的是 **改过的 BASE64**，
    字母表里 `/` 被换成 `,`（`,` 同时充当 &...- 段的结束符）。
    直接 base64 输出会得到含 `/` 的名字，服务器不认。

    实测证据：服务器自己回报的未来电池中心是 `&ZypnZXU1bGBOLV,D-`，
    而用标准 base64 编出来是 `&ZypnZXU1bGBOLV/D-` —— 差一个字符，
    `status()` 就会查不到这个文件夹。
    """
    out = []
    i = 0
    while i < len(s):
        c = s[i]
        if c == "&":
            out.append("&-")
            i += 1
        elif 0x20 <= ord(c) <= 0x7E:
            out.append(c)
            i += 1
        else:
            j = i
            while j < len(s) and not (0x20 <= ord(s[j]) <= 0x7E):
                j += 1
            run = s[i:j].encode("utf-16-be")
            b64 = base64.b64encode(run).decode("ascii").rstrip("=")
            out.append("&" + b64.replace("/", ",") + "-")
            i = j
    return "".join(out)


def _is_ascii(s: str) -> bool:
    return all(ord(c) < 128 for c in (s or ""))


class ImapClient:
    """一次性 IMAP 会话。调用方负责 close()。"""

    def __init__(self, cfg: dict, log=None):
        self.cfg = cfg
        self.log = log
        self.m = None
        self.folder = None
        self.uidvalidity = None

    # ---------------- 连接 ----------------
    def connect(self):
        if self.m is not None:
            return self.m
        host = self.cfg.get("imap_host", "mail.sjtu.edu.cn")
        port = int(self.cfg.get("imap_port", 993))
        user = self.cfg.get("user")
        pwd = self.cfg.get("pass")
        if not user or not pwd:
            raise ImapError("缺少邮箱凭据（见 config.secrets_file_hint()）")
        try:
            self.m = imaplib.IMAP4_SSL(host, port, timeout=DEFAULT_TIMEOUT)
            self.m.login(user, pwd)
        except Exception as e:
            self.m = None
            raise ImapError("IMAP 登录失败：%s" % e)
        return self.m

    def close(self):
        if self.m is None:
            return
        try:
            self.m.close()
        except Exception:
            pass
        try:
            self.m.logout()
        except Exception:
            pass
        self.m = None
        self.folder = None

    def __enter__(self):
        self.connect()
        return self

    def __exit__(self, *exc):
        self.close()

    # ---------------- 文件夹 ----------------
    def list_folders(self) -> list:
        m = self.connect()
        typ, data = m.list()
        if typ != "OK":
            raise ImapError("LIST 失败：%s" % data)
        out = []
        for raw in data or []:
            if not raw:
                continue
            line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else str(raw)
            mm = re.match(r'\((?P<flags>[^)]*)\)\s+"(?P<delim>[^"]*)"\s+"?(?P<name>.*?)"?\s*$', line)
            if not mm:
                continue
            name_raw = mm.group("name")
            out.append({
                "raw": name_raw,
                "name": mutf7_decode(name_raw),
                "delimiter": mm.group("delim"),
                "flags": [f for f in mm.group("flags").split() if f],
            })
        return out

    def select(self, folder: str, readonly: bool = True) -> dict:
        """选中文件夹。非 ASCII 名走 UTF-8 字面量（Zimbra 实测可行）。"""
        m = self.connect()
        cmd = "EXAMINE" if readonly else "SELECT"
        if _is_ascii(folder):
            arg = ('"%s"' % folder).encode("ascii")
        else:
            raw = folder.encode("utf-8")
            arg = b"{" + str(len(raw)).encode("ascii") + b"}\r\n" + raw
        try:
            typ, data = m._command_complete(cmd, m._command(cmd, arg))
        except Exception as e:
            raise ImapError("选中 %s 失败：%s" % (folder, e))
        if typ != "OK":
            raise ImapError("选中 %s 失败：%s" % (folder, data))
        # 让 imaplib 状态机保持一致
        m.state = "SELECTED"
        m.untagged_responses = {}
        m.message_info = None
        self.folder = folder
        self.uidvalidity = None
        exists = None
        try:
            resp = m.response("EXISTS")[1]
            if resp and resp[0]:
                exists = int(resp[0])
        except Exception:
            exists = None
        try:
            resp = m.response("UIDVALIDITY")[1]
            if resp and resp[0]:
                self.uidvalidity = str(int(resp[0]))
        except Exception:
            pass
        return {"folder": folder, "exists": exists, "uidvalidity": self.uidvalidity}

    def status(self, folder: str) -> dict:
        """不选中文件夹即取统计（用于健康检查）。"""
        m = self.connect()
        name = '"%s"' % folder if _is_ascii(folder) else '"%s"' % mutf7_encode(folder)
        try:
            typ, data = m.status(name, "(MESSAGES UNSEEN UIDNEXT UIDVALIDITY)")
        except Exception as e:
            raise ImapError("STATUS %s 失败：%s" % (folder, e))
        if typ != "OK":
            raise ImapError("STATUS %s 失败：%s" % (folder, data))
        txt = data[0].decode("latin-1", "replace") if data and data[0] else ""
        out = {"folder": folder}
        for k in ("MESSAGES", "UNSEEN", "UIDNEXT", "UIDVALIDITY"):
            mm = re.search(k + r"\s+(\d+)", txt)
            if mm:
                out[k.lower()] = int(mm.group(1))
        return out

    # ---------------- 检索 / 抓取 ----------------
    def uid_search(self, criteria: str = "ALL", charset: str = None) -> list:
        m = self.connect()
        try:
            typ, data = m.uid("SEARCH", charset, criteria)
        except Exception as e:
            raise ImapError("UID SEARCH 失败：%s" % e)
        if typ != "OK" or not data or not data[0]:
            return []
        return [int(x) for x in data[0].split() if x.isdigit()]

    def fetch_flags(self, uids) -> dict:
        """返回 {uid: {'flags': [...], 'unread':bool, 'answered':bool, 'flagged':bool, 'deleted':bool}}"""
        uids = [int(u) for u in (uids or [])]
        if not uids:
            return {}
        m = self.connect()
        out = {}
        CH = 200
        for i in range(0, len(uids), CH):
            chunk = uids[i:i + CH]
            typ, data = m.uid("FETCH", ",".join(str(u) for u in chunk), "(FLAGS)")
            if typ != "OK":
                continue
            for item in (data or []):
                if not isinstance(item, bytes):
                    continue
                mm = re.match(rb"(\d+)\s+\(FLAGS\s*\(([^)]*)\)", item)
                if not mm:
                    continue
                uid = int(mm.group(1))
                flags = [f.decode("ascii", "replace") for f in mm.group(2).split() if f]
                out[uid] = _flags_to_dict(flags)
        return out

    def fetch_headers(self, uids, body_limit: int = 0) -> dict:
        """抓取指定 UID 的头（+ 可选正文）。返回 {uid: parsed_info}。"""
        uids = [int(u) for u in (uids or [])]
        if not uids:
            return {}
        m = self.connect()
        out = {}
        CH = 25
        for i in range(0, len(uids), CH):
            chunk = uids[i:i + CH]
            spec = "(UID FLAGS BODY.PEEK[HEADER.FIELDS (DATE FROM TO CC SUBJECT MESSAGE-ID " \
                   "IN-REPLY-TO REFERENCES LIST-UNSUBSCRIBE PRECEDENCE AUTO-SUBMITTED)])"
            typ, data = m.uid("FETCH", ",".join(str(u) for u in chunk), spec)
            if typ != "OK":
                continue
            out.update(_parse_header_fetch(data))
        return out

    def fetch_full(self, uids) -> dict:
        """抓取完整 RFC822。返回 {uid: {'raw':bytes, 'flags':[...]}}"""
        uids = [int(u) for u in (uids or [])]
        if not uids:
            return {}
        m = self.connect()
        out = {}
        CH = 10
        for i in range(0, len(uids), CH):
            chunk = uids[i:i + CH]
            typ, data = m.uid("FETCH", ",".join(str(u) for u in chunk),
                              "(UID FLAGS BODY.PEEK[])")
            if typ != "OK":
                continue
            cur = None
            for item in (data or []):
                if isinstance(item, tuple):
                    head = item[0].decode("latin-1", "replace")
                    mm = re.search(r"UID\s+(\d+)", head)
                    if mm:
                        cur = int(mm.group(1))
                        out.setdefault(cur, {"raw": b"", "flags": []})
                    fm = re.search(r"FLAGS\s*\(([^)]*)\)", head)
                    if fm and cur is not None:
                        out[cur]["flags"] = [f.decode("ascii", "replace")
                                             for f in fm.group(1).encode("latin-1").split() if f]
                    if "BODY[]" in head and cur is not None:
                        out[cur]["raw"] = item[1] if isinstance(item[1], bytes) else b""
                elif isinstance(item, bytes) and cur is not None:
                    fm = re.search(rb"FLAGS\s*\(([^)]*)\)", item)
                    if fm:
                        out[cur]["flags"] = [f.decode("ascii", "replace")
                                             for f in fm.group(1).split() if f]
        return out

    # ---------------- 标记 / 追加 ----------------
    def store_flags_many(self, uids, op: str = "+", flags: list = None,
                         chunk: int = 150) -> dict:
        """一条 `UID STORE` 给**多个** UID 打标志（分块，避免命令过长）。

        为什么值得单独写 —— 批处理的性能全在这儿：
        UID STORE 本来就接受逗号分隔的 UID 集合，而逐封调用会变成 N 个往返。
        本机实测：建连接 150ms/次，复用连接后单条 STORE 41ms；
        全选 151 封若逐封来 = 22.6 秒纯连接开销（用户看到的就是「点了没反应」）。

        ⚠️ 调用方必须**已经** select(readonly=False)，本方法不自己选文件夹 ——
        选一次就是一次往返，批量路径里不能重复付这个钱。

        返回 {ok, count, chunks} 或 {ok: False, error, count, chunks}。
        """
        ids = [str(u) for u in (uids or []) if u not in (None, "")]
        if not ids:
            return {"ok": True, "count": 0, "chunks": 0}
        m = self.connect()
        arg = "(%s)" % " ".join(flags or [])
        size = max(1, int(chunk))
        done, chunks = 0, 0
        try:
            for i in range(0, len(ids), size):
                part = ids[i:i + size]
                typ, data = m.uid("STORE", ",".join(part), op + "FLAGS", arg)
                chunks += 1
                if typ != "OK":
                    return {"ok": False, "count": done, "chunks": chunks,
                            "error": "UID STORE 失败（%d 个 UID）：%s" % (len(part), data)}
                done += len(part)
            return {"ok": True, "count": done, "chunks": chunks}
        except Exception as e:
            return {"ok": False, "count": done, "chunks": chunks, "error": str(e)}

    def store_flags(self, uid, op: str, flags: list) -> bool:
        """op ∈ {'+', '-', 'set'}。"""
        m = self.connect()
        m.select(self.folder or "INBOX", readonly=False)
        arg = "(%s)" % " ".join(flags)
        typ, data = m.uid("STORE", str(uid), op + "FLAGS", arg)
        return typ == "OK"

    def move_to_trash(self, uid, trash_folder: str) -> bool:
        """软删除：打 \\Deleted（**不 EXPUNGE**，可 undo）。"""
        return self.store_flags(uid, "+", ["\\Deleted"])

    def append(self, folder: str, raw: bytes, flags: str = "(\\Seen \\Draft)") -> bool:
        m = self.connect()
        name = folder if _is_ascii(folder) else mutf7_encode(folder)
        typ, data = m.append('"%s"' % name, flags,
                             imaplib.Time2Internaldate(time.time()), raw)
        if typ != "OK":
            raise ImapError("APPEND 到 %s 失败：%s" % (folder, data))
        return True

    def noop(self) -> bool:
        m = self.connect()
        try:
            typ, _ = m.noop()
            return typ == "OK"
        except Exception:
            return False


# --------------------------------------------------------------------------
# FETCH 响应解析
# --------------------------------------------------------------------------
def _flags_to_dict(flags: list) -> dict:
    return {
        "flags": flags,
        "unread": "\\Seen" not in flags,
        "answered": "\\Answered" in flags,
        "flagged": "\\Flagged" in flags,
        "deleted": "\\Deleted" in flags,
    }


def _parse_header_fetch(data) -> dict:
    """解析 (UID FLAGS BODY.PEEK[HEADER.FIELDS ...]) 的返回。"""
    out = {}
    cur = None
    for item in (data or []):
        if isinstance(item, tuple):
            head = item[0].decode("latin-1", "replace")
            mm = re.search(r"UID\s+(\d+)", head)
            if mm:
                cur = int(mm.group(1))
                out.setdefault(cur, {})
            fm = re.search(r"FLAGS\s*\(([^)]*)\)", head)
            if fm and cur is not None:
                flags = [f.decode("ascii", "replace")
                         for f in fm.group(1).encode("latin-1").split() if f]
                out[cur].update(_flags_to_dict(flags))
            if cur is not None and isinstance(item[1], bytes):
                info = mparser.parse_headers_only(item[1])
                info.update(out[cur])
                out[cur] = info
        elif isinstance(item, bytes) and cur is not None:
            fm = re.search(rb"FLAGS\s*\(([^)]*)\)", item)
            if fm:
                flags = [f.decode("ascii", "replace") for f in fm.group(1).split() if f]
                out.setdefault(cur, {}).update(_flags_to_dict(flags))
    return out


def parse_full_fetch(data) -> dict:
    return _parse_header_fetch(data)


# --------------------------------------------------------------------------
# IMAP IDLE 监听（P0-1 事件驱动）
# --------------------------------------------------------------------------
def _raw_readline(sock, buf: bytearray, max_bytes: int = 1 << 20):
    """从 socket **直接**读一行，返回 bytes；EOF 返回 None。

    为什么不直接用 imaplib 的 `m.readline()`：
        imaplib 的 readline 走 `sock.makefile('rb')` 的**缓冲层**。
        一旦 socket 读超时，这个缓冲对象就进入不可恢复状态，之后任何读取
        都抛 `OSError: cannot read from timed out object`（实测复现）。
        IDLE 的本质就是「长时间无数据 + 周期性超时」，
        所以必须绕开缓冲层 —— 否则表现为「IDLE 每 30 秒断一次、永远收不到事件」。

    超时会以 socket.timeout / TimeoutError 抛出，由调用方决定继续等待还是收工。
    """
    while True:
        i = buf.find(b"\n")
        if i >= 0:
            line = bytes(buf[:i + 1])
            del buf[:i + 1]
            return line
        if len(buf) > max_bytes:
            buf.clear()
            raise ImapError("IDLE 单行超过 %d 字节，疑似协议异常" % max_bytes)
        chunk = sock.recv(4096)
        if not chunk:
            return None
        buf.extend(chunk)


class IdleWatcher:
    """在独立线程里跑 IMAP IDLE，把「有新邮件」变成事件。

    IDLE 不可用（服务器不支持 / 网络抖动）时**自动降级**为本地 NOOP 轮询，
    并把降级状态暴露给 /api/health。降级是本地廉价轮询，不是 WorkBuddy
    automation，因此不违反 P0-1 的「WorkBuddy 不承担 daemon 职责」。
    """

    def __init__(self, cfg: dict, folder: str = "INBOX", on_event=None, on_state=None, log=None):
        self.cfg = cfg
        self.folder = folder
        self.on_event = on_event
        self.on_state = on_state
        self.log = log
        self.stop_event = threading.Event()
        self.thread = None
        self.mode = "starting"          # idling | polling | stopped | error
        self.last_event_at = None
        self.last_error = ""
        self.reconnects = 0
        self.fallback_reason = ""

    # ---------------- 状态 ----------------
    def _set_state(self, mode: str, err: str = ""):
        self.mode = mode
        if err:
            self.last_error = err
        if self.on_state:
            try:
                self.on_state(self.status())
            except Exception:
                pass

    def status(self) -> dict:
        return {
            "mode": self.mode,
            "folder": self.folder,
            "last_event_at": self.last_event_at,
            "last_error": self.last_error,
            "reconnects": self.reconnects,
            "fallback_reason": self.fallback_reason,
            "running": bool(self.thread and self.thread.is_alive()),
        }

    # ---------------- 生命周期 ----------------
    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = threading.Thread(target=self._run, name="imap-idle", daemon=True)
        self.thread.start()

    def stop(self, timeout: float = 6.0):
        self.stop_event.set()
        t = self.thread
        if t and t.is_alive():
            t.join(timeout=timeout)
        self._set_state("stopped")

    # ---------------- 主循环 ----------------
    def _run(self):
        """每个外层循环 = 一次完整的 IDLE 周期，然后重连。

        为什么每周期都重连（而不是在同一条连接上连续 IDLE）：
          IDLE 期间我们绕开 imaplib 的缓冲层用裸 socket 读，
          一旦某次读超时，imaplib 的 `m.file` 就与 socket 不同步了。
          直接弃用连接最省事，代价是约每 25 分钟重建一次连接 —— 可忽略。
        """
        delay = 3.0
        while not self.stop_event.is_set():
            client = ImapClient(self.cfg, log=self.log)
            try:
                client.connect()
                client.select(self.folder, readonly=True)
                base = self._exists(client)
                self._set_state("idling")
                delay = 3.0
                got, new_exists = self._idle_cycle(client, base)
                if got:
                    self._emit("new_mail", new_exists if new_exists is not None else base)
            except Exception as e:
                self.reconnects += 1
                msg = str(e)
                if self._is_idle_unsupported(msg):
                    self.fallback_reason = "server rejected IDLE"
                    self._set_state("polling", msg)
                    self._poll_loop(delay)
                    continue
                self._set_state("error", msg)
                if self.log:
                    self.log("IDLE 断线，%.0fs 后重连：%s" % (delay, msg))
                self.stop_event.wait(delay)
                delay = min(delay * 2, 60.0)
            finally:
                client.close()
        self._set_state("stopped")

    def _exists(self, client) -> int:
        try:
            return len(client.uid_search("ALL"))
        except Exception:
            return 0

    def _is_idle_unsupported(self, msg: str) -> bool:
        low = (msg or "").lower()
        return ("idle" in low and ("unknown" in low or "bad" in low or "not support" in low))

    def _emit(self, kind: str, exists: int):
        self.last_event_at = util.now_iso()
        if self.on_event:
            try:
                self.on_event({"type": kind, "folder": self.folder,
                               "exists": exists, "at": self.last_event_at})
            except Exception as e:
                if self.log:
                    self.log("IDLE 回调异常：%s" % e)

    def _idle_cycle(self, client, base_exists):
        """发送 IDLE、等待事件，返回 (是否有变化, 最新 EXISTS)。

        读全部走裸 socket（见 _raw_readline 的说明），绝不碰 imaplib 的缓冲层。
        """
        m = client.m
        max_wait = int(self.cfg.get("idle_reenable_seconds", 1500))
        idle_read_timeout = 60
        tag = m._new_tag()
        m.send(b"%s IDLE\r\n" % (tag,))
        buf = bytearray()
        if not self._wait_continuation(m, tag, buf, timeout=20):
            raise ImapError("IDLE 未收到 continuation")

        changed = False
        latest = None
        start = time.time()
        try:
            while not self.stop_event.is_set() and (time.time() - start) < max_wait:
                try:
                    m.sock.settimeout(idle_read_timeout)
                    line = _raw_readline(m.sock, buf)
                except (socket.timeout, TimeoutError):
                    continue                      # 正常的「没消息」，继续等
                except OSError as e:
                    raise ImapError("IDLE 读取失败：%s" % e)
                if line is None:
                    raise ImapError("IDLE 连接被服务端关闭")
                if not line.startswith(b"*"):
                    continue
                upper = line.upper()
                if b"EXISTS" in upper:
                    mm = re.search(rb"\*\s+(\d+)\s+EXISTS", upper)
                    if mm:
                        latest = int(mm.group(1))
                        changed = True
                        break
                if b"EXPUNGE" in upper or b"RECENT" in upper:
                    changed = True
                if b"BYE" in upper:
                    raise ImapError("服务端发送 BYE")
        finally:
            try:
                m.send(b"DONE\r\n")
            except Exception:
                pass
            # 读完 DONE 的 tagged 响应，之后就弃用这条连接（_run 会重连）
            deadline = time.time() + 5
            while time.time() < deadline:
                try:
                    m.sock.settimeout(max(0.5, deadline - time.time()))
                    line = _raw_readline(m.sock, buf)
                except (socket.timeout, TimeoutError):
                    break
                except Exception:
                    break
                if line is None or line.startswith(tag):
                    break

        # UID 计数比 EXISTS 更可靠（客户端可能只同步部分）
        try:
            real = len(client.uid_search("ALL"))
            if latest is None:
                latest = real
            if base_exists is not None and real != base_exists:
                changed = True
            latest = real
        except Exception:
            pass
        return changed, latest

    def _wait_continuation(self, m, tag, buf: bytearray, timeout: float) -> bool:
        """等 `+ idling`。同样走裸 socket。

        读超时**只是重试**，不当作失败 —— 服务端可能稍慢一点才回 `+`；
        只有在超时预算用完、拿到 tagged 拒绝、或连接 EOF 时才判失败。
        """
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                m.sock.settimeout(max(0.5, deadline - time.time()))
                line = _raw_readline(m.sock, buf)
            except (socket.timeout, TimeoutError):
                continue
            except Exception:
                return False
            if line is None:
                return False
            if line.startswith(b"+"):
                return True
            if line.startswith(tag):
                return False
        return False

    def _poll_loop(self, delay: float):
        """IDLE 不可用时的降级：本地 NOOP 轮询。"""
        interval = int(self.cfg.get("idle_fallback_poll_seconds", 90))
        if self.log:
            self.log("IDLE 降级为本地轮询，间隔 %ds" % interval)
        base = None
        while not self.stop_event.is_set():
            client = ImapClient(self.cfg, log=self.log)
            try:
                client.connect()
                client.select(self.folder, readonly=True)
                cur = self._exists(client)
                if base is None:
                    base = cur
                elif cur != base:
                    base = cur
                    self._emit("new_mail", cur)
            except Exception as e:
                self.reconnects += 1
                self.last_error = str(e)
            finally:
                client.close()
            self.stop_event.wait(interval)


# --------------------------------------------------------------------------
# 便捷函数
# --------------------------------------------------------------------------
def check_connection(cfg: dict) -> dict:
    """给 /api/health 用的 IMAP 探活。"""
    c = ImapClient(cfg)
    t0 = time.time()
    try:
        c.connect()
        folders = c.list_folders()
        inbox = c.status(cfg.get("folder_inbox", "INBOX"))
        return {"ok": True, "latency_ms": round((time.time() - t0) * 1000, 1),
                "folder_count": len(folders), "inbox": inbox}
    except Exception as e:
        return {"ok": False, "latency_ms": round((time.time() - t0) * 1000, 1), "error": str(e)}
    finally:
        c.close()
