#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""邮件工作台后端：HTTP API + 静态文件服务。

这就是「整合」的含义 —— 数据走双通道，各取所长：

  ┌─ Web 邮箱（IMAP）─────────────────────────────┐
  │  文件夹树、未读标记、附件标识、正文、写草稿           │
  │  准，但要联网，慢                                    │
  └────────────────────────────────────────────────────┘
  ┌─ Foxmail 本地索引 ─────────────────────────────────┐
  │  6646 封历史的毫秒级检索（主题/发件人）              │
  │  快、不联网、零密码，但只有元数据，没有未读/文件夹    │
  └────────────────────────────────────────────────────┘

只用标准库，无需 pip install。启动：python server.py
"""

import os
import re
import sys
import json
import time
import base64
import struct
import imaplib
import datetime
import email.utils
import argparse
import queue
import select
import threading
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse, parse_qs
from email import message_from_bytes
from email.header import decode_header
from email.mime.text import MIMEText
from email.header import Header
from imaplib import Time2Internaldate
import smtplib

try:
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
except Exception:
    pass

HERE = os.path.dirname(os.path.abspath(__file__))
WEB_DIR = os.path.join(HERE, "web")
CONFIG_PATH = os.path.join(HERE, "config.json")

# ---------------------------------------------------------------- 配置

def load_config():
    cfg = {}
    if os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            cfg = json.load(f)
    # 凭据优先从环境变量 / 用户家目录的凭据文件取，不落在项目里
    cred_path = os.path.join(os.path.expanduser("~"), ".workbuddy", "secrets", "mail.cred")
    if os.path.exists(cred_path):
        try:
            with open(cred_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if "=" in line and line.startswith("SJTU_MAIL_"):
                        k, v = line.split("=", 1)
                        cfg[k.strip()] = v.strip()
        except OSError:
            pass
    cfg["user"] = os.environ.get("SJTU_MAIL_USER") or cfg.get("SJTU_MAIL_USER") or cfg.get("user", "")
    cfg["pass"] = os.environ.get("SJTU_MAIL_PASS") or cfg.get("SJTU_MAIL_PASS") or cfg.get("pass", "")
    return cfg


CFG = load_config()

# ---------------------------------------------------------------- V2 桥接
#
# V2（mail_workbench/）以「增量叠加」的方式接入，**不改动下面任何既有路由**：
#   /workbench      -> V2 工作桶界面（Human-in-the-loop Workbench）
#   /api/v2/*       -> V2 API（路由内部映射到 /api/*）
# 桥接层出错时 V2_READY=False，V1 完全不受影响。
try:
    from mail_workbench import v1_bridge as V2_BRIDGE
    V2_READY = True
except Exception as _e:                                   # pragma: no cover
    V2_BRIDGE = None
    V2_READY = False
    print("V2 桥接层未加载（V1 功能不受影响）：%s" % _e)


# ---------------------------------------------------------------- 工具

def crlf(text):
    """把正文换行统一成 CRLF。

    RFC 5322 要求邮件的行尾是 CRLF。我们之前直接把 textarea 里的 LF 写进 MIME，
    结果 Foxmail 打开这封草稿时，会把每个 LF 当成「行尾 + 段落分隔」，
    保存后段落之间就变成 3~5 个空行（用户反馈的「格式里太多空行」）。
    浏览器 textarea 提交的是 LF，所以出口这里必须转一次。
    """
    return text.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\r\n")


def uid_of(meta):
    """从 FETCH 的返回行里取出**真正的 UID**。

    坑：UID FETCH 的返回形如
        b'12 (UID 3456 FLAGS (\\Seen) BODY[HEADER.FIELDS ...] {...})'
    第一个 token 是**序号**（sequence number），UID 在括号里的 `UID nnn`。
    直接拿第一个 token 会得到序号 —— 而序号会随着邮件被删除（EXPUNGE）
    整体往前挪，页面缓存的 id 就会指向别的邮件（点开错信、标错已读）。
    """
    mm = re.search(rb"\bUID\s+(\d+)", meta or b"")
    if mm:
        return mm.group(1).decode()
    return (meta or b"").split(b" ")[0].decode()      # 兜底：极少数服务器不带 UID


def decode_mime_str(s):
    try:
        parts = decode_header(s or "")
    except Exception:
        return s or ""
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


def mutf7_decode(s):
    def rep(mo):
        b = mo.group(1).replace(",", "/")
        pad = (-len(b)) % 4
        try:
            return base64.b64decode(b + "=" * pad).decode("utf-16-be")
        except Exception:
            return mo.group(0)
    return re.sub(r"&([A-Za-z0-9+/,]*)-", rep, s)


def extract_plain_text(raw_bytes):
    msg = message_from_bytes(raw_bytes)
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() == "text/plain" and not part.get_filename():
                try:
                    p = part.get_payload(decode=True)
                    if p:
                        return p.decode(part.get_content_charset() or "utf-8", "replace")
                except Exception:
                    continue
        for part in msg.walk():
            if part.get_content_type() == "text/html" and not part.get_filename():
                try:
                    p = part.get_payload(decode=True)
                    if p:
                        return re.sub(r"<[^>]+>", " ", p.decode(part.get_content_charset() or "utf-8", "replace"))
                except Exception:
                    continue
        return ""
    try:
        p = msg.get_payload(decode=True)
        if p:
            return p.decode(msg.get_content_charset() or "utf-8", "replace")
    except Exception:
        pass
    return raw_bytes.decode("utf-8", "replace")


def has_attachment(msg):
    for part in msg.walk():
        if part.get_filename():
            return True
        if part.get("Content-Disposition", "").lower().startswith("attachment"):
            return True
    return False


def connect():
    m = imaplib.IMAP4_SSL(CFG.get("imap_host", "mail.sjtu.edu.cn"), int(CFG.get("imap_port", 993)), timeout=30)
    m.login(CFG["user"], CFG["pass"])
    return m


# ---------------------------------------------------------------- Foxmail 本地索引

OLE_BASE = datetime.datetime(1899, 12, 30)
_TLD = (r"(?:com|cn|net|org|edu|gov|info|biz|io|ai|co|me|dev|app|tech|online|site|shop|store|"
        r"top|xyz|club|wiki|space|live|email|cloud|data|digital|network|center|world|today|news|"
        r"life|work|company|solutions|systems|group|team|zone|fun|host|press|run|social|software|"
        r"studio|tips|video|website|ac|ae|af|ag|am|as|at|au|be|bg|br|ca|cc|ch|cl|cm|cz|de|dk|ee|"
        r"es|eu|fi|fm|fr|ge|gg|gl|hk|hr|hu|id|ie|il|im|in|iq|ir|is|it|jp|ke|kr|kz|la|lk|lt|lu|lv|"
        r"ly|ma|md|mk|ml|mn|ms|mt|mu|mx|my|nl|no|np|nu|nz|om|pa|pe|pk|pl|pm|ps|pt|pw|qa|ro|rs|ru|"
        r"se|sg|sh|si|sk|sm|sn|so|st|su|sx|tc|th|tj|tk|tl|tm|tn|to|tr|tv|tw|tz|ua|ug|uk|us|uy|uz|"
        r"vc|ve|vn|ws|za|zm|zw)")
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@(?:[A-Za-z0-9\-]+\.)+" + _TLD, re.IGNORECASE)


def parse_foxmail_index(path):
    """解析 Foxmail 7.2 的 Mails/Index。

    格式：Header 512B（offset 8 = 总记录数 uint32 LE）；
    Record 512B，offset 0 = mail_id(0 跳过)，offset 8 = 接收时间 OLE double，
    offset 51 = 紧凑文本区（发件人名+邮箱+收件人名+邮箱+主题，无分隔符）。
    OLE 存的就是北京时间，**不要再 +8h**。
    """
    if not os.path.exists(path):
        return []
    size = os.path.getsize(path)
    out = []
    with open(path, "rb") as f:
        header = f.read(512)
        if len(header) < 512:
            return []
        total = struct.unpack_from("<I", header, 8)[0]
        for i in range(min(total, (size - 512) // 512)):
            f.seek(512 + i * 512)
            rec = f.read(512)
            if len(rec) < 512:
                break
            if struct.unpack_from("<I", rec, 0)[0] == 0:
                continue
            try:
                recv = OLE_BASE + datetime.timedelta(days=float(struct.unpack_from("<d", rec, 8)[0]))
            except Exception:
                continue
            blob = rec[51:].split(b"\x00\x00")[0]
            try:
                raw = blob.decode("utf-8")
            except UnicodeDecodeError:
                raw = blob.decode("gbk", "replace")
            ms = list(EMAIL_RE.finditer(raw))
            if not ms:
                continue
            from_name = raw[: ms[0].start()].strip()
            from_addr = ms[0].group(0)
            if len(ms) >= 2:
                to_addr = ms[1].group(0)
                subject = raw[ms[1].end():].strip()
            else:
                to_addr = ""
                subject = raw[ms[0].end():].strip()
            out.append({
                "id": "idx-%d" % i,
                "from_name": from_name or from_addr,
                "from_addr": from_addr.lower(),
                "subject": subject,
                "date": recv.strftime("%Y-%m-%d %H:%M"),
                "ts": recv.timestamp(),
            })
    seen, uniq = set(), []
    for r in out:
        k = (r["from_addr"], r["subject"], r["date"])
        if k in seen:
            continue
        seen.add(k)
        uniq.append(r)
    return uniq


_INDEX_CACHE = {"mtime": 0, "data": []}


def get_index():
    path = CFG.get("index_path", "")
    try:
        mtime = os.path.getmtime(path)
    except OSError:
        return []
    if mtime != _INDEX_CACHE["mtime"]:
        _INDEX_CACHE["mtime"] = mtime
        _INDEX_CACHE["data"] = parse_foxmail_index(path)
    return _INDEX_CACHE["data"]


# ---------------------------------------------------------------- API

def api_bootstrap(q):
    """账户 + 文件夹树（含未读数）。走 IMAP。"""
    m = connect()
    try:
        st, data = m.list()
        folders = []
        for d in data:
            raw = d.decode("utf-8", "replace")
            mm = re.match(r'\((?P<flags>[^)]*)\)\s+"(?P<delim>.*)"\s+"?(?P<name>[^"]*)"?', raw)
            name = mutf7_decode(mm.group("name")) if mm else raw
            flags = mm.group("flags") if mm else ""
            total = unseen = 0
            try:
                st2, d2 = m.status('"%s"' % (mm.group("name") if mm else raw), "(MESSAGES UNSEEN)")
                if st2 == "OK" and d2 and d2[0]:
                    s = d2[0].decode()
                    tm = re.search(r"MESSAGES\s+(\d+)", s)
                    tu = re.search(r"UNSEEN\s+(\d+)", s)
                    total = int(tm.group(1)) if tm else 0
                    unseen = int(tu.group(1)) if tu else 0
            except Exception:
                pass
            folders.append({"name": name, "raw": (mm.group("name") if mm else raw),
                            "total": total, "unseen": unseen, "flags": flags})
        # 中文文件夹排后面，系统文件夹按固定顺序
        order = {"INBOX": 0, "Drafts": 1, "Sent": 2, "Junk": 3, "Trash": 4}
        folders.sort(key=lambda f: (order.get(f["raw"], 99), f["name"]))
        return {"ok": True, "user": CFG.get("user", ""), "folders": folders,
                "index_count": len(get_index()), "source": "imap",
                # 邮件分级规则（config.json 的 classify 段）。
                # 不下发就表示沿用前端内置的默认值。
                "classify": CFG.get("classify") or {}}
    finally:
        try:
            m.logout()
        except Exception:
            pass


def fetch_meta(m, folder, uids):
    """批量取一批邮件的 header + FLAGS（文件夹已 SELECT，uids 是 int 列表）。

    api_messages（文件夹列表）和 api_tag（全库标签里的手动标重要）共用这一份 ——
    FLAGS 解析踩过坑，只修一处就会在另一处复发。
    """
    out = []
    CHUNK = 30
    for i in range(0, len(uids), CHUNK):
        chunk = uids[i:i + CHUNK]
        uidset = b",".join(str(u).encode() for u in chunk)

        # FLAGS 必须**单独取一次**。
        # 把它和 BODY 合并成一个请求时，服务器会把它排在字面量（BODY 内容）
        # 之后 —— 形如 `1 (BODY[...] {235}` 然后 ` FLAGS (\Seen))` ——
        # imaplib 于是切成两个数据项，而循环只取 item[0]（tuple），
        # 那个单独的 FLAGS 片段被 isinstance 判断跳过，导致 \Seen 永远读不到。
        # 症状：点开邮件标了已读，刷新列表又变回未读（但未读计数是对的，
        # 因为那个走 STATUS/SELECT，不经过这里）。
        # 存原始 FLAGS 串，\Seen 和 \Flagged 都从它里面判 —— 只请求一次。
        flags_of = {}
        stf, df = m.uid("fetch", uidset, "(FLAGS)")
        if stf == "OK":
            for it in df:
                # 两种形态都要认：
                #   带字面量（BODY[]）的响应 → (meta, payload) 二元组
                #   不带字面量（纯 FLAGS）→ 直接就是 bytes
                meta_f = it[0] if isinstance(it, tuple) else it
                if isinstance(meta_f, bytes):
                    flags_of[uid_of(meta_f)] = meta_f

        st, d = m.uid("fetch", uidset,
                      "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE CONTENT-TYPE)])")
        if st != "OK":
            continue
        for item in d:
            if not isinstance(item, tuple):
                continue
            meta, payload = item[0], item[1]
            uid = uid_of(meta)
            raw_flags = flags_of.get(uid, b"")
            seen = b"\\Seen" in raw_flags
            flagged = b"\\Flagged" in raw_flags
            msg = message_from_bytes(payload)
            subj = decode_mime_str(msg.get("Subject", "")) or "(无主题)"
            frm = decode_mime_str(msg.get("From", ""))
            ctype = (msg.get("Content-Type") or "").lower()
            # 附件启发式：multipart/mixed 通常带附件；精确判断留给详情页
            attach = ctype.startswith("multipart/mixed")
            try:
                dt = email.utils.parsedate_to_datetime(msg.get("Date"))
                ts = dt.timestamp()
                date_s = dt.strftime("%Y-%m-%d %H:%M")
            except Exception:
                ts, date_s = 0, ""
            out.append({"uid": uid, "folder": folder, "subject": subj, "from": frm,
                        "date": date_s, "ts": ts, "seen": seen,
                        "flagged": flagged, "attach": attach, "source": "imap"})
    out.sort(key=lambda x: x["ts"], reverse=True)
    return out


def api_messages(q):
    """某文件夹的邮件列表。走 IMAP（有未读/附件）。"""
    folder = (q.get("folder") or ["INBOX"])[0]
    limit = int((q.get("limit") or ["60"])[0])
    # 翻页游标：只要 uid < before 的那些（列表是「新的在前」）。
    # 用 uid 而不是「跳过前 N 条」：后者在翻页期间来了新邮件就会错位
    # （新邮件插到前面，把后面的挤下去，于是重复或漏掉）。
    before = int((q.get("before") or ["0"])[0])
    m = connect()
    try:
        st, _ = m.select('"%s"' % folder, readonly=True)
        if st != "OK":
            return {"ok": False, "error": "打不开文件夹 %s" % folder}
        # 一律用 UID 版：序号会随删信整体向前挪，UID 才是邮件的不变标识
        st, data = m.uid("search", None, "ALL")
        uids = [int(u) for u in data[0].split()]
        if before:
            uids = [u for u in uids if u < before]
        # 最新的在前
        uids.sort(reverse=True)
        has_more = len(uids) > limit
        uids = uids[:limit]
        out = fetch_meta(m, folder, uids)
        # hasMore：后面还有没有更早的邮件。前端靠它决定要不要做「滚到底自动加载下一批」。
        return {"ok": True, "folder": folder, "messages": out,
                "source": "imap", "hasMore": has_more}
    finally:
        try:
            m.logout()
        except Exception:
            pass


# ---------------------------------------------------------------- 全库标签筛选

# 分级规则的 Python 版。**必须与前端 web/js/classify.js 保持一致** ——
# 标签视图现在筛的是本地索引（6615 封全库），规则放在前端就只能筛「当前加载的那 60 封」。
# 规则本身仍来自 config.json 的 classify 段，两边读的是同一份配置。
CLASSIFY_DEFAULTS = {
    "vip_senders": ["gift-research@sjtu.edu.cn"],
    "vip_domains": ["nsfc.gov.cn", "catl.com", "sunrayip.com"],
    "act_senders": ["application.infoplus@sjtu.edu.cn"],
    "act_domains": ["xcdsystem.com", "easychair.org", "editorialmanager.com",
                    "elsevier.com", "pro.nsfc.gov.cn"],
    "dump_domains": [
        "researchgatemail.net", "asianonlinejournals.com", "onlinesciencepublishing.com",
        "onlineacademicpress.com", "conscientiabeam.org", "aessweb.org", "esn-press.org",
        "ecsenet.com", "ejvle.e-nig.com", "cdkz.shop", "dtgwfdj.cn", "zbmiye.com",
    ],
    "dump_tlds": ["shop", "top", "xyz", "click", "link", "info", "biz", "work", "live", "icu"],
    "trusted": ["sjtu.edu.cn", "qq.com", "gmail.com", "163.com", "126.com", "outlook.com",
                "catl.com", "nsfc.gov.cn", "sunrayip.com", "elsevier.com", "springer.com",
                "mdpi.com", "ieee.org", "sae.org", "jove.com", "mathworks.com",
                "edu.cn", "gov.cn", "vip.163.com", "sina.com"],
}

_MACH_OK_LABELS = {"com", "cn", "net", "org", "edu", "gov", "info", "io", "co", "ac"}


def classify_rules():
    r = dict(CLASSIFY_DEFAULTS)
    for k, v in (CFG.get("classify") or {}).items():
        if v:
            r[k] = v
    return r


def machine_generated(addr, from_name):
    """机器批量发出的地址特征（与 classify.js 的同名函数一致）"""
    if "@" not in addr:
        return True
    local, dom = addr.split("@", 1)
    name = re.sub(r"[^a-z0-9]", "", (from_name or "").lower())
    if name and name == re.sub(r"[^a-z0-9]", "", local.lower()):
        return True
    if len(local) > 15:
        return True
    if re.search(r"\d{6,}", local):
        return True
    for lb in dom.split("."):
        if lb in _MACH_OK_LABELS:
            continue
        if len(lb) >= 4 and re.search(r"\d", lb):
            return True
        if re.fullmatch(r"[a-z]{6,14}", lb):
            return True
    return False


def classify_of(addr, from_name, r):
    """按发件人给邮件分级（不含 flagged —— 那是 IMAP 标记，索引里没有）"""
    addr = (addr or "").strip().lower()
    dom = addr.split("@")[-1] if "@" in addr else ""
    tld = dom.split(".")[-1] if dom else ""
    if dom in r["dump_domains"] or tld in r["dump_tlds"]:
        return "dump"
    if addr in r["act_senders"] or dom in r["act_domains"]:
        return "act"
    if addr in r["vip_senders"] or dom in r["vip_domains"]:
        return "vip"
    trusted = any(dom == t or dom.endswith("." + t) for t in r["trusted"])
    if not trusted and machine_generated(addr, from_name):
        return "dump"
    if re.search(r"no-?reply|notification|newsletter|mailer|digest", addr, re.I):
        return "read"
    if re.search(r"[\u4e00-\u9fff]", from_name or ""):
        return "reply"
    return "read"


def api_tag(q):
    """全库标签筛选 —— 扫本地索引（6600+ 封），不再只筛当前加载的 60 封。

    索引里**没有 uid、也没有 flagged**，所以：
      · 结果只能「读」，不能标已读/标重要/删除（前端会提示先回文件夹）；
      · 「★ 重点」另外并入 IMAP `SEARCH FLAGGED` 的邮件（那些**有 uid**，可操作），
        否则用户手动标重要的邮件在全库重点里反而找不到。
    """
    tag = (q.get("tag") or ["vip"])[0]
    limit = int((q.get("limit") or ["60"])[0])
    offset = int((q.get("offset") or ["0"])[0])
    folder = (q.get("folder") or ["INBOX"])[0]

    r = classify_rules()
    idx = sorted(get_index(), key=lambda x: x.get("ts") or 0, reverse=True)
    hits = [x for x in idx
            if classify_of(x.get("from_addr", ""), x.get("from_name", ""), r) == tag]
    total = len(hits)

    page = hits[offset:offset + limit]
    out = []
    for x in page:
        name = (x.get("from_name") or "").strip()
        addr = x.get("from_addr", "")
        # 前端 splitFrom 认 "名 <地址>" 这种格式
        frm = ("%s <%s>" % (name, addr)) if name and name != addr else addr
        out.append({"id": x["id"], "from": frm, "subject": x.get("subject") or "(无主题)",
                    "date": x.get("date", ""), "ts": x.get("ts") or 0,
                    "source": "index"})

    # 「★ 重点」额外并入手动标重要的邮件（有 uid，可标已读/标重要/删除）。
    # 只在第一页前置，避免把 offset 分页搅乱。
    if tag == "vip" and offset == 0:
        try:
            m = connect()
            try:
                st, _ = m.select('"%s"' % folder, readonly=True)
                if st == "OK":
                    st, data = m.uid("search", None, "FLAGGED")
                    uids = [int(u) for u in data[0].split()] if (st == "OK" and data and data[0]) else []
                    uids.sort(reverse=True)
                    out = fetch_meta(m, folder, uids[:30]) + out
            finally:
                try:
                    m.logout()
                except Exception:
                    pass
        except Exception:
            pass        # 拿不到就算了，索引里按规则筛出的重点照常显示

    return {"ok": True, "tag": tag, "messages": out,
            "total": total, "indexCount": len(idx),
            # 前端翻页要用这个，不能拿 len(messages) 当游标 ——
            # 「★ 重点」第一页前面还塞了 IMAP 的手动标重要邮件，会多出来。
            "nextOffset": offset + len(page),
            "hasMore": offset + limit < total, "source": "index"}


def api_message(q):
    """单封详情（正文 + 精确附件列表）。走 IMAP。"""
    folder = (q.get("folder") or ["INBOX"])[0]
    uid = (q.get("uid") or [""])[0]
    m = connect()
    try:
        m.select('"%s"' % folder, readonly=True)
        st, d = m.uid("fetch", uid.encode(), "(BODY.PEEK[] FLAGS)")
        if st != "OK" or not d or not isinstance(d[0], tuple):
            return {"ok": False, "error": "取不到该邮件"}
        payload = d[0][1]
        msg = message_from_bytes(payload)
        atts = []
        for i, part in enumerate(p for p in msg.walk() if p.get_filename()):
            data = part.get_payload(decode=True)
            atts.append({
                "i": i,
                "name": decode_mime_str(part.get_filename() or ""),
                "size": len(data) if data else 0,
            })
        body = extract_plain_text(payload)
        return {"ok": True,
                "uid": uid, "folder": folder,
                "subject": decode_mime_str(msg.get("Subject", "")) or "(无主题)",
                "from": decode_mime_str(msg.get("From", "")),
                "to": decode_mime_str(msg.get("To", "")),
                "date": decode_mime_str(msg.get("Date", "")),
                "body": body[:20000],
                "attachments": atts,
                "source": "imap"}
    finally:
        try:
            m.logout()
        except Exception:
            pass


def api_search(q):
    """历史检索。走 Foxmail 本地索引 —— 6646 封毫秒级，不联网。"""
    kw = (q.get("q") or [""])[0].strip().lower()
    limit = int((q.get("limit") or ["50"])[0])
    if not kw:
        return {"ok": True, "messages": [], "source": "index"}
    hits = [r for r in get_index()
            if kw in (r["subject"] + r["from_name"] + r["from_addr"]).lower()]
    hits.sort(key=lambda x: x["ts"], reverse=True)
    return {"ok": True, "messages": hits[:limit], "total": len(hits), "source": "index"}


def api_find(q):
    """按主题关键词回服务器取正文。

    本地索引检索很快但只有元数据；用户在索引里定位到某封后，
    用这条把正文补上 —— 这就是双通道的衔接点。
    """
    kw = (q.get("q") or [""])[0].strip().lower()
    if not kw:
        return {"ok": False, "error": "缺少关键词"}
    m = connect()
    try:
        m.select('"INBOX"', readonly=True)
        st, data = m.uid("search", None, "ALL")
        ids = data[0].split()
        for uid in reversed(ids[-400:]):
            st, d = m.uid("fetch", uid, "(BODY.PEEK[HEADER.FIELDS (SUBJECT FROM DATE)])")
            if st != "OK" or not d or not isinstance(d[0], tuple):
                continue
            msg = message_from_bytes(d[0][1])
            subj = decode_mime_str(msg.get("Subject", ""))
            if kw not in subj.lower():
                continue
            st2, d2 = m.uid("fetch", uid, "(BODY.PEEK[])")
            body = extract_plain_text(d2[0][1]) if st2 == "OK" and isinstance(d2[0], tuple) else ""
            return {"ok": True, "subject": subj,
                    "from": decode_mime_str(msg.get("From", "")),
                    "date": decode_mime_str(msg.get("Date", "")),
                    "body": body[:20000], "source": "imap"}
        return {"ok": False, "error": "服务器上没找到该主题"}
    finally:
        try:
            m.logout()
        except Exception:
            pass


# 每个操作对应「哪个标记、加上还是去掉」。
#
# 「重要」用的是 IMAP 标准的 \Flagged，而不是本地记一笔 —— 因为它是
# **服务器上的标记**：Foxmail 那边看到的是同一个星标，跟「已读」同理。
# 本地记的话，换个客户端、或者换台机器就丢了。
FLAG_OPS = {
    "read":        ("\\Seen",    "+"),
    "unread":      ("\\Seen",    "-"),
    "important":   ("\\Flagged", "+"),
    "unimportant": ("\\Flagged", "-"),
}


def api_flags(payload):
    """批量改标记：已读 / 未读（\\Seen），重要 / 取消重要（\\Flagged）。"""
    folder = payload.get("folder", "INBOX")
    uids = payload.get("uids") or []
    op = payload.get("op", "read")
    if not uids:
        return {"ok": False, "error": "没有选中邮件"}
    if op not in FLAG_OPS:
        return {"ok": False, "error": "不支持的标记操作：%s" % op}
    flag, sign = FLAG_OPS[op]
    m = connect()
    try:
        m.select('"%s"' % folder)
        ok = 0
        for u in uids:
            st, _ = m.uid("store", u.encode(), sign + "FLAGS", flag)
            if st == "OK":
                ok += 1
        return {"ok": True, "updated": ok}
    finally:
        try:
            m.logout()
        except Exception:
            pass


def has_uidplus(m):
    """服务器支不支持 UIDPLUS —— 决定能不能「只清理指定的那几封」。

    坑（踩过两次）：`m.capabilities` 这个属性只在连接建立时设过一次，
    之后会被 SELECT 响应里带回的精简 CAPABILITY **覆盖**，直接读它会把
    「支持」误判成「不支持」；而 `m.capability()` 只是**返回**能力列表、
    并不更新那个属性 —— 所以必须用它的返回值判断。
    """
    try:
        _typ, dat = m.capability()
        caps = (dat[-1] if dat else b"") or b""
        return b"UIDPLUS" in caps.split()
    except Exception:
        return False


def copyuid_dest(m):
    """COPY 之后立刻调用：取出这封邮件**在目标文件夹里的新 UID**。

    坑（实测踩到）：COPY 的**返回值里没有这个信息**——
        typ, data = m.uid("copy", uid, folder)
        # data == [None]      COPY 不返回字面量，这里是空的

    COPYUID 是作为**未标记响应**回来的，得单独问：
        m.response("COPYUID")   ->  ('COPYUID', [b'1 11566 11567'])
                                        │   │      └─ 目标文件夹里的新 UID
                                        │   └──────── 源 UID
                                        └──────────── 目标文件夹的 UIDVALIDITY

    必须**紧接着** COPY 调用它：imaplib 每条命令开始时会清空未标记响应，
    中间插别的命令就取不到了。

    有了新 UID 才谈得上「撤销」—— 否则只知道邮件被挪走了，却不知道它
    在新文件夹里是第几号，等于捞不回来。取不到就返回空串：
    宁可如实说「这封没法自动撤销」，也不要靠猜编号去搬邮件（猜错就是把别的信搬回来）。
    """
    try:
        _typ, dat = m.response("COPYUID")
    except Exception:
        return ""
    for part in (dat or []):
        if not isinstance(part, (bytes, bytearray)):
            continue
        nums = re.findall(rb"\d+", part)
        # 我们一次只 COPY 一封，所以固定是 `uidvalidity 源UID 目标UID` 三段
        if len(nums) >= 3:
            return nums[-1].decode()
    return ""


def api_trash(payload):
    """把邮件移到「已删除邮件」。**只移动，不彻底删** —— 误点了还能找回来。

    界面上叫「已删除邮件」，服务器上那个文件夹的 IMAP 原名是 Trash
    （想改的话用 config.json 里的 trash_folder）。

    这是应用里唯一的破坏性操作，所以刻意加了三层保护：

      1. 必须显式传 confirm=True —— 前端弹过二次确认才会带这个字段
      2. **先 COPY 过去、确认成功了，才在源文件夹标记删除** ——
         万一 COPY 失败（比如那个文件夹名不对），邮件原封不动
      3. 有 UIDPLUS 就精确 expunge，只清掉我们标记的那几封；
         没有才退回整文件夹 expunge
    """
    folder = payload.get("folder") or "INBOX"
    trash = CFG.get("trash_folder", "Trash")
    uids = [str(u) for u in (payload.get("uids") or []) if str(u)]

    if not uids:
        return {"ok": False, "error": "没有选中邮件"}
    if folder == trash:
        return {"ok": False, "error": "这封已经在「已删除邮件」里了"}
    if not payload.get("confirm"):
        return {"ok": False, "error": "未确认，拒绝删除（需要 confirm=true）"}

    m = connect()
    try:
        st, _ = m.select('"%s"' % folder)
        if st != "OK":
            return {"ok": False, "error": "打不开文件夹 %s" % folder}

        # 先确认那个文件夹存在 —— 目标不在就别动手
        stt, _ = m.select('"%s"' % trash)
        if stt != "OK":
            return {"ok": False, "error": "找不到「%s」文件夹，没有动任何邮件" % trash}
        m.select('"%s"' % folder)

        moved, items, failed = [], [], []
        for u in uids:
            ue = u.encode()
            stc, _data = m.uid("copy", ue, trash)
            if stc != "OK":
                failed.append(u)
                continue
            # 紧接着取（中间不能插别的命令，否则响应被清）
            new_uid = copyuid_dest(m)
            # 复制成功，才在源文件夹标记删除
            m.uid("store", ue, "+FLAGS", "(\\Deleted)")
            moved.append(u)
            # 记下它在新文件夹里的编号，供「撤销」用
            items.append({"uid": u, "trash_uid": new_uid})

        if moved:
            # ⚠️ 这一步**必须看返回码**。如果 EXPUNGE 失败，邮件会留在原文件夹里
            # 只带着 \Deleted 标记 —— 我们自己的列表（只查 UNSEEN/ALL 的返回值里
            # 已经把它过滤掉了看不出来）以为删干净了，而 Foxmail / Zimbra
            # 不会自动清 \Deleted 的邮件，那边**照样显示**。
            # 用户看到的现象就是「在 8080 里删了，别的客户端还留着」。
            # 所以失败要如实报出来，别让界面假装成功。
            expunged, expunge_err = True, ""
            try:
                if has_uidplus(m):
                    ste, sdata = m.uid("expunge", ",".join(moved).encode())
                else:
                    ste, sdata = m.expunge()
                if ste != "OK":
                    expunged, expunge_err = False, str(sdata)
            except Exception as e:
                expunged, expunge_err = False, "%s: %s" % (type(e).__name__, e)
            if not expunged:
                print("  [删除] EXPUNGE 失败：%s（邮件可能仍留在 %s 里）" % (expunge_err, folder))

        return {"ok": True, "moved": moved, "items": items,
                "failed": failed, "trash": trash,
                "expunged": expunged, "expungeError": expunge_err}
    finally:
        try:
            m.logout()
        except Exception:
            pass


def api_untash(payload):
    """撤销「移到已删除邮件」—— 把刚挪走的那几封原样搬回原文件夹。

    应用里那条「已移到已删除邮件 · 撤销」调的就是它。

    顺序跟删除一样不能反：**先复制回原文件夹、确认成功了，才从
    「已删除邮件」里清掉**。反过来的话，复制那一步万一失败，邮件就真没了。

    只能撤销「最近一次」—— 前端每次删除都会用新的一批记录替换掉旧的。
    再往前的邮件可能已经被别处（Foxmail / 网页邮箱）动过，
    凭旧编号去搬有搬错的风险，所以宁可不提供。
    """
    folder = (payload.get("folder") or "").strip()
    trash = payload.get("trash") or CFG.get("trash_folder", "Trash")
    items = [it for it in (payload.get("items") or []) if isinstance(it, dict)]

    if not folder:
        return {"ok": False, "error": "不知道要放回哪个文件夹"}
    if not items:
        return {"ok": False, "error": "没有可撤销的邮件"}
    if folder == trash:
        return {"ok": False, "error": "原文件夹就是「已删除邮件」，不用撤销"}

    m = connect()
    try:
        # 两个文件夹都要能打开 —— 打不开就别动，免得把邮件搬丢
        st, _ = m.select('"%s"' % folder)
        if st != "OK":
            return {"ok": False, "error": "打不开原文件夹 %s，没有动任何邮件" % folder}
        stt, _ = m.select('"%s"' % trash)
        if stt != "OK":
            return {"ok": False, "error": "打不开「%s」文件夹，没有动任何邮件" % trash}
        m.select('"%s"' % trash)

        back, failed, skipped = [], [], []
        for it in items:
            tu = str(it.get("trash_uid") or "")
            if not tu:
                # 拿不到新编号就**不猜** —— 猜错是把别的邮件搬回来
                skipped.append(str(it.get("uid") or ""))
                continue
            stc, _ = m.uid("copy", tu.encode(), folder)
            if stc != "OK":
                failed.append(tu)
                continue
            m.uid("store", tu.encode(), "+FLAGS", "(\\Deleted)")
            back.append(tu)

        note = ""
        if back:
            if has_uidplus(m):
                m.uid("expunge", ",".join(back).encode())
            else:
                # 不支持 UID EXPUNGE 就整文件夹 expunge —— 那会连带清掉
                # 「已删除邮件」里别的待清理邮件。宁可留个残影，
                # 也不越权删用户的东西；如实告诉用户即可。
                note = "邮件已搬回，但「已删除邮件」里还留着一份副本（服务器不支持精确清理）"

        if not back:
            if skipped:
                return {"ok": False, "error": "这批邮件没记下在「已删除邮件」里的编号，"
                                              "没法自动撤销 —— 请去那个文件夹手动移回"}
            return {"ok": False, "error": "撤销失败：邮件可能已经在别处被移动或清理过了"}

        return {"ok": True, "restored": len(back), "failed": failed,
                "skipped": skipped, "note": note}
    finally:
        try:
            m.logout()
        except Exception:
            pass


def save_local_copy(msg_bytes, rcpts, subject=""):
    """本地留底：把发出去的原始邮件写成 sent-log/*.eml

    为什么要在服务器副本之外再存一份：副本靠 IMAP APPEND，是**第二个网络操作**，
    连不上、文件夹名写错、服务器抽风都会失败 —— 而这时候信已经发出去了，
    正文就彻底没了（2026-09-21 就是这么丢的）。原始字节留在本地，
    以后要补档、要查发过什么都还有据可依。
    返回文件名；失败返回空串（留底失败不该影响发送结果）。
    """
    d = os.path.join(HERE, "sent-log")
    try:
        os.makedirs(d, exist_ok=True)
        who = re.sub(r"[^A-Za-z0-9._@-]", "_", (rcpts[0] if rcpts else "unknown"))[:60]
        name = "%s-%s.eml" % (time.strftime("%Y%m%d-%H%M%S"), who)
        with open(os.path.join(d, name), "wb") as f:
            f.write(msg_bytes)
        return name
    except OSError:
        return ""


def imap_folder(name):
    """IMAP 文件夹名要按「修改版 UTF-7」编码（RFC 3501）。

    中文文件夹名（比如「已发送」）直接传给 imaplib 会炸：
    `UnicodeEncodeError: 'ascii' codec can't encode characters`。
    纯 ASCII 名字原样返回，省得白绕一圈。
    """
    name = str(name)
    if all(ord(c) < 128 for c in name):
        return name
    out, buf = [], ""

    def flush():
        nonlocal buf
        if buf:
            b64 = base64.b64encode(buf.encode("utf-16-be")).decode("ascii").rstrip("=")
            out.append("&" + b64.replace("/", ",") + "-")
            buf = ""

    for ch in name:
        if 0x20 <= ord(ch) <= 0x7E:
            flush()
            out.append("&-" if ch == "&" else ch)
        else:
            buf += ch
    flush()
    return "".join(out)


def append_to_folder(folder, msg_bytes, flags=r"(\Seen)", when=None):
    """把一份邮件追加到服务器上的某个文件夹（草稿箱 / 已发送都用它）

    返回 (是否成功, 错误说明)。**不抛异常** —— 调用方要靠这个决定
    是「整体失败」还是「邮件已发出、只是副本没留下」。
    """
    m = connect()
    try:
        st, data = m.append(imap_folder(folder), flags,
                            Time2Internaldate(when or datetime.datetime.now().timestamp()),
                            msg_bytes)
        if st != "OK":
            return False, str(data)
        return True, ""
    except Exception as e:
        return False, "%s: %s" % (type(e).__name__, e)
    finally:
        try:
            m.logout()
        except Exception:
            pass


def api_draft(payload):
    """写草稿到服务器草稿箱。绝不发送。"""
    to = payload.get("to", "").strip()
    subject = payload.get("subject", "").strip()
    body = payload.get("body", "")
    if not to:
        return {"ok": False, "error": "缺少收件人"}
    if not body.strip():
        return {"ok": False, "error": "正文为空"}
    msg = MIMEText(crlf(body), "plain", "utf-8")
    msg["From"] = "%s <%s>" % (Header(CFG.get("from_name", CFG["user"]), "utf-8").encode(), CFG["user"])
    msg["To"] = to
    msg["Subject"] = Header(subject or "(无主题)", "utf-8").encode()
    msg["Date"] = email.utils.formatdate(localtime=True)
    folder = CFG.get("drafts_folder", "Drafts")
    ok, err = append_to_folder(folder, msg.as_bytes(), r"(\Seen \Draft)")
    if not ok:
        return {"ok": False, "error": "写入失败：%s" % err}
    return {"ok": True, "folder": folder, "hint": "已写入草稿箱，Foxmail 点「接收」后可见"}


def api_send(payload):
    """真正发送邮件（SMTP），并把副本追加到服务器「已发送」。

    这是整个应用里唯一「不可撤销」的操作，所以刻意做了三道闸：
      1. 必须显式传 confirm=True —— 前端弹二次确认后才带这个字段
      2. 支持 dry_run=True：只连服务器 + 登录 + 校验，不投递（用来验证配置）
      3. 永远不自动发：只有用户点「发送」并确认，才会走到这里
         （起草队列那条路只写草稿箱，不会触发发送）

    ⚠️ 2026-09-21 修：**以前只投递、不追加副本**，于是发出去的邮件在
    「已发送邮件」里根本找不到。SMTP 和 IMAP 是两回事 —— 服务器不会替你
    留副本，客户端得自己 APPEND 一封进去（Foxmail 就是这么干的）。
    副本追加失败**不算发送失败**（信已经出去了），但要如实告诉用户。
    """
    to = (payload.get("to") or "").strip()
    subject = (payload.get("subject") or "").strip()
    body = payload.get("body") or ""

    if not to:
        return {"ok": False, "error": "缺少收件人"}
    if not body.strip():
        return {"ok": False, "error": "正文为空"}

    rcpts = [a.strip() for a in re.split(r"[;,]", to) if a.strip()]
    bad = [a for a in rcpts if not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", a)]
    if bad:
        return {"ok": False, "error": "收件人格式不对：%s" % "、".join(bad)}

    host = CFG.get("smtp_host", "mail.sjtu.edu.cn")
    port = int(CFG.get("smtp_port", 465))
    user = CFG.get("user", "")
    pwd = CFG.get("pass", "")
    if not user or not pwd:
        return {"ok": False, "error": "没读到邮箱凭据，无法发送"}

    msg = MIMEText(crlf(body), "plain", "utf-8")
    msg["From"] = "%s <%s>" % (Header(CFG.get("from_name", user), "utf-8").encode(), user)
    msg["To"] = ", ".join(rcpts)
    msg["Subject"] = Header(subject or "(无主题)", "utf-8").encode()
    msg["Date"] = email.utils.formatdate(localtime=True)

    def _connect():
        if port == 465:
            return smtplib.SMTP_SSL(host, port, timeout=30)
        s = smtplib.SMTP(host, port, timeout=30)
        s.starttls()
        return s

    # ---- dry_run：只验证能不能连上并登录 ----
    if payload.get("dry_run"):
        try:
            s = _connect()
            try:
                s.login(user, pwd)
            finally:
                try:
                    s.quit()
                except Exception:
                    pass
            return {"ok": True, "dry_run": True, "host": "%s:%d" % (host, port),
                    "hint": "SMTP 连接与登录正常，未投递任何邮件"}
        except Exception as e:
            return {"ok": False, "error": "SMTP 验证失败：%s: %s" % (type(e).__name__, e)}

    # ---- 真正发送：必须显式确认 ----
    if not payload.get("confirm"):
        return {"ok": False, "error": "未确认，拒绝发送（需要 confirm=true）"}

    try:
        s = _connect()
        try:
            s.login(user, pwd)
            s.sendmail(user, rcpts, msg.as_bytes())
        finally:
            try:
                s.quit()
            except Exception:
                pass
    except smtplib.SMTPAuthenticationError as e:
        return {"ok": False, "error": "SMTP 登录被拒：%s" % e}
    except Exception as e:
        return {"ok": False, "error": "发送失败：%s: %s" % (type(e).__name__, e)}

    # ---- 留副本：先本地留底，再追加到「已发送」 ----
    local = save_local_copy(msg.as_bytes(), rcpts, subject)
    sent_folder = str(CFG.get("sent_folder", "Sent") or "Sent")
    saved, save_err = False, "已按配置跳过（save_sent_to_server=false）"
    if CFG.get("save_sent_to_server", True):
        saved, save_err = append_to_folder(sent_folder, msg.as_bytes(), r"(\Seen)")
    if saved:
        hint = "已发送，并已存入「%s」" % sent_folder
    else:
        hint = ("邮件已发出（对方能收到），但**没能存进「%s」**：%s。"
                "本地留底在 sent-log/%s，可用 /api/file-sent 补档。"
                % (sent_folder, save_err, local or "(留底也失败)"))
    return {"ok": True, "sent": True, "to": rcpts, "savedToSent": saved,
            "sentFolder": sent_folder, "sentError": "" if saved else save_err,
            "localCopy": local, "hint": hint}


def api_file_sent(payload):
    """补档：把一封「已经发出去、但没留下副本」的邮件补写进已发送

    用途 —— 2026-09-21 之前 `api_send()` 不追加副本，那段时间发的信在
    「已发送邮件」里是空的。用户把正文贴回来就能补上，不用重发给对方。
    """
    to = (payload.get("to") or "").strip()
    subject = (payload.get("subject") or "").strip()
    body = payload.get("body") or ""
    if not to:
        return {"ok": False, "error": "缺少收件人"}
    if not body.strip():
        return {"ok": False, "error": "正文为空"}

    when = payload.get("ts")
    try:
        when = float(when) if when else None
    except (TypeError, ValueError):
        when = None

    msg = MIMEText(crlf(body), "plain", "utf-8")
    msg["From"] = "%s <%s>" % (Header(CFG.get("from_name", CFG["user"]), "utf-8").encode(), CFG["user"])
    msg["To"] = to
    msg["Subject"] = Header(subject or "(无主题)", "utf-8").encode()
    msg["Date"] = email.utils.formatdate(when or time.time(), localtime=True)
    if payload.get("note"):
        msg["X-Workbench-Note"] = Header(str(payload["note"]), "utf-8").encode()

    folder = str(CFG.get("sent_folder", "Sent") or "Sent")
    ok, err = append_to_folder(folder, msg.as_bytes(), r"(\Seen)", when)
    if not ok:
        return {"ok": False, "error": "补档失败：%s" % err}
    return {"ok": True, "folder": folder, "hint": "已补写进「%s」，Foxmail 点「接收」后可见" % folder}


# ---------------------------------------------------------------- 附件
#
# 点击正文下方附件 → 从服务器取下来存到本地，能安全打开的顺带用默认程序打开。

# 可以放心交给系统默认程序打开的扩展名
_OPEN_OK = {
    "pdf", "doc", "docx", "xls", "xlsx", "ppt", "pptx", "txt", "csv", "md", "rtf",
    "jpg", "jpeg", "png", "gif", "bmp", "webp", "tif", "tiff", "svg",
    "wps", "et", "dps", "ofd", "eml", "msg",
}
# 一律只保存、绝不自动打开。
# 邮件附件里的可执行文件是钓鱼最常用的载体，点一下就跑起来风险太高。
_NEVER_OPEN = {
    "exe", "bat", "cmd", "com", "scr", "pif", "js", "jse", "vbs", "vbe", "wsf",
    "wsh", "ps1", "psm1", "hta", "jar", "msi", "msp", "reg", "lnk", "cpl",
    "dll", "sys", "inf", "scf", "url", "chm", "gadget", "application",
}
ATT_DIR = os.path.join(HERE, "attachments")


def _safe_name(name):
    """把附件名洗成安全的文件名（防目录穿越 / 非法字符 / 超长）"""
    name = os.path.basename((name or "").replace("\\", "/")).strip()
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name)
    name = name.lstrip(".") or "未命名附件"
    if len(name) > 120:
        stem, ext = os.path.splitext(name)
        name = stem[:100] + ext
    return name


def api_attachment(payload):
    """取某封邮件的第 index 个附件：存到本地，安全类型顺带打开。

    安全策略：
      · 可执行类（_NEVER_OPEN）—— **只保存，绝不自动打开**，并说明原因
      · 常见文档/图片（_OPEN_OK）—— 存下后用系统默认程序打开
      · 其他未知类型 —— 只保存，让用户自己决定
    """
    folder = payload.get("folder") or "INBOX"
    uid = str(payload.get("uid") or "")
    if not uid:
        return {"ok": False, "error": "缺少 uid"}
    try:
        index = int(payload.get("index"))
    except (TypeError, ValueError):
        return {"ok": False, "error": "缺少附件序号"}

    m = connect()
    try:
        m.select('"%s"' % folder, readonly=True)
        st, d = m.uid("fetch", uid.encode(), "(BODY.PEEK[])")
        if st != "OK" or not d or not isinstance(d[0], tuple):
            return {"ok": False, "error": "取不到该邮件"}
        msg = message_from_bytes(d[0][1])
    finally:
        try:
            m.logout()
        except Exception:
            pass

    parts = [p for p in msg.walk() if p.get_filename()]
    if index < 0 or index >= len(parts):
        return {"ok": False, "error": "附件序号超出范围（共 %d 个）" % len(parts)}

    part = parts[index]
    raw = part.get_payload(decode=True)
    if raw is None:
        return {"ok": False, "error": "这个附件解不出来（可能是内嵌引用）"}

    name = _safe_name(decode_mime_str(part.get_filename() or ""))
    os.makedirs(ATT_DIR, exist_ok=True)
    stem, ext = os.path.splitext(name)
    dest = os.path.join(ATT_DIR, name)
    n = 1
    while os.path.exists(dest):
        dest = os.path.join(ATT_DIR, "%s(%d)%s" % (stem, n, ext))
        n += 1
    with open(dest, "wb") as f:
        f.write(raw)

    ext_l = ext.lstrip(".").lower()
    want_open = payload.get("open", True) is not False
    opened, note = False, ""
    if ext_l in _NEVER_OPEN:
        note = "这是可执行类附件（.%s），防钓鱼只保存、不自动打开；确认来源可靠再自己双击" % ext_l
    elif ext_l in _OPEN_OK and want_open:
        try:
            os.startfile(dest)
            opened = True
            note = "已用系统默认程序打开"
        except OSError as e:
            note = "已保存，但打开失败（%s）" % e
    else:
        note = "已保存到本地"

    return {"ok": True, "name": os.path.basename(dest), "path": dest,
            "size": len(raw), "opened": opened, "note": note}


# ---------------------------------------------------------------- 回复起草队列
#
# 应用这边不动 AI：它只负责把「请帮我起草回复」写进队列，
# 以及把起草好的草稿取回来给用户改。
# 真正的起草由 WorkBuddy 那一侧完成（它能读到完整往来线程，不会重复回复）。
# 详见 reply_queue.py 顶部说明。

import reply_queue  # noqa: E402


def web_version():
    """web/ 下最新修改时间，当作前端版本号

    用途：面板里可能一直开着一个旧页面，改完代码它也不会自己更新，
    表现就是「新功能看不到」。页面定时比对这个值，不一致就自动刷新。
    """
    newest = 0.0
    for root, _dirs, files in os.walk(WEB_DIR):
        for fn in files:
            try:
                newest = max(newest, os.path.getmtime(os.path.join(root, fn)))
            except OSError:
                pass
    return str(int(newest))


def api_reply_request(payload):
    """登记一封「请 WorkBuddy 起草回复」

    两条来源：
      manual —— 用户在邮件上亲手点的。priority=0，插到队首，定时任务优先处理。
      auto   —— 定时任务按规则自动挑的。priority=1，排在手动的后面。

    带查重：同一封邮件重复提交（手滑连点）不会新建第二份。
    已经在了的话就**提升优先级**（promote）：万一它是自动入队的、或者上次失败了，
    用户这一点的意思就是「我现在就要」—— 于是置为 manual + 队首 + 复位成待生成。
    """
    if not payload.get("uid") and not payload.get("subject"):
        return {"ok": False, "error": "缺少邮件信息"}

    source = payload.get("source") or "manual"
    exist = reply_queue.find_pending(
        payload.get("folder") or "INBOX",
        payload.get("uid") or "",
        payload.get("subject") or "",
        payload.get("from") or "",
    )
    if exist:
        promoted = reply_queue.promote(exist["id"], "manual")
        return {
            "ok": True,
            "id": exist["id"],
            "duplicate": True,
            "promoted": promoted,
            "source": "manual",
            "hint": "这封已经在队列里了 —— 已把它提到最前面（按手动加急处理），WorkBuddy 只会起草一次。",
        }

    rid = reply_queue.enqueue(payload, source=source)
    return {
        "ok": True,
        "id": rid,
        "duplicate": False,
        "promoted": False,
        "source": source,
        "hint": ("已加急：插到队首，通常 30 分钟内出草稿（按钮上会显示已等多久）。"
                 if source == "manual" else "已自动入队，等下一轮处理。"),
    }


def api_reply_cancel(payload):
    """撤回：把还没起草的请求从队列里拿掉（误点了可以救回来）"""
    rid = str(payload.get("id") or "")
    if not rid:
        # 没给 id 就按「folder + uid」找
        found = reply_queue.find_pending(
            payload.get("folder") or "INBOX",
            payload.get("uid") or "",
            payload.get("subject") or "",
            payload.get("from") or "",
        )
        if not found:
            return {"ok": False, "error": "队列里没有这封"}
        rid = found["id"]
    return {"ok": True, "id": rid, "removed": reply_queue.cancel_pending(rid)}


def api_reply_queue(_q):
    """应用轮询用：只给「数量」和元信息，**不给草稿内容**

    这是刻意的：面板里可能还开着一个旧版本的页面，它的轮询逻辑是
    「谁先拿到谁消费」。只要这里不下发 done 内容，旧页面就永远吃不掉草稿。
    真正取走草稿只能走 /api/reply-draft（用户点「点开修改」、或自动回传时触发）。

    pendingMeta：每条的**状态**（待生成 / 生成中 / 失败）和来源（手动 / 自动），
      前端据此把「让 WorkBuddy 起草」按钮切成四种样子，失败的那封还能点重试。
    """
    pend = reply_queue.pending()
    dn = reply_queue.done()
    meta = [{
        "id": r["id"],
        "uid": str(r.get("uid") or ""),
        "subject": r.get("subject", ""),
        "from": r.get("from", ""),
        "status": r.get("status") or "queued",
        "source": r.get("source") or "auto",
        # 同理别写 `or 1`：手动加急的 priority 是 0，会被 or 吃掉变成 1
        "priority": r.get("priority") if r.get("priority") is not None else 1,
        # 已经等了多久 + 等多久算不对劲 + 是不是已经卡住了。
        # 没有这三个值，界面只能干巴巴显示「已加急」，用户等 10 分钟
        # 也不知道是正常排队还是已经卡死（2026-09-21 王琦超那封就是这么没的）。
        "ageSec": reply_queue.age_sec(r),
        "stallAfter": reply_queue.stall_after(r),
        "stalled": reply_queue.is_stalled(r),
        "attempts": int(r.get("attempts") or 0),
        "last_error": r.get("last_error") or "",
    } for r in pend]
    waiting = [m for m in meta if m["status"] == "queued"]
    failed = [m for m in meta if m["status"] == "failed"]
    stalled = [m for m in meta if m["stalled"]]
    return {
        "ok": True,
        "pending": len(waiting),
        "generating": len([m for m in meta if m["status"] == "generating"]),
        "failed": len(failed),
        "stalled": len(stalled),
        "doneCount": len(dn),
        # 归档区的条数 + 保留时长，前端用它解释「为什么队列一直是干净的」
        "expiredCount": len(reply_queue.expired()),
        "doneTtlDays": reply_queue.DONE_TTL // 86400,
        "backlogLimit": reply_queue.AUTOPICK_MAX_BACKLOG,
        "pendingIds": [m["uid"] for m in meta if m["status"] in ("queued", "generating")],
        "pendingMeta": meta,
        "donePreview": [
            {"id": r["id"], "uid": str(r.get("uid") or ""),
             "from": r.get("from", ""), "subject": r.get("subject", "")}
            for r in dn[:10]
        ],
    }


def api_reply_purge(_payload=None):
    """手动清理：把过期的条目挪进归档区（也顺手回滚卡死的 generating）

    看门狗每 30 秒会自己跑一遍，这个接口是给「我现在就想看着队列变干净」用的。
    """
    rolled = reply_queue.sweep()
    moved = reply_queue.sweep_stale()
    return {"ok": True, "requeued": rolled, "archived": moved,
            "stats": reply_queue.stats()}


def api_reply_draft(_q=None):
    """取走已就绪的草稿

    不给 id：取第一封（用户点「点开修改」时的行为）。
    给 id：只取那一封（自动回传用 —— 用户点起草的那封好了，直接填给他，
    不会错拿成队列里别的邮件）。
    """
    ds = reply_queue.done()
    if not ds:
        return {"ok": False, "error": "没有待处理的草稿", "doneCount": 0}

    # 注意：GET 走 parse_qs，值是 list；POST 直接是标量。两种都要能接。
    raw_id = (_q or {}).get("id") if isinstance(_q, dict) else None
    if isinstance(raw_id, list):
        raw_id = raw_id[0] if raw_id else ""
    want = str(raw_id or "")
    first = None
    if want:
        first = next((d for d in ds if str(d.get("id")) == want), None)
        if not first:
            return {"ok": False, "error": "这封草稿还没好", "doneCount": len(ds)}
    else:
        first = ds[0]

    reply_queue.consume(first["id"])
    return {"ok": True, "id": first["id"], "uid": str(first.get("uid") or ""),
            "draft": first.get("draft") or {},
            "doneCount": len(ds) - 1}


def api_reply_consume(payload):
    """应用取走草稿后清掉"""
    rid = str(payload.get("id") or "")
    if not rid:
        return {"ok": False, "error": "缺少 id"}
    return {"ok": True, "removed": reply_queue.consume(rid)}


def api_reply_complete(payload):
    """AI 侧交回草稿 —— **走 HTTP，不要再 python -c 或直接写文件**

    以前让 AI 用 `python -c "import reply_queue as q; q.complete(...)"` 交回，
    失败了就手写 JSON。两条路都没有日志、没有校验：写错了 id、写漏了字段，
    服务端一概不知，最后表现为「界面说已加急，但草稿永远不出现」。
    现在统一走这个接口，服务端能校验 + 记账 + 留日志。
    """
    rid = str(payload.get("id") or "")
    draft = payload.get("draft") or {}
    if not rid:
        return {"ok": False, "error": "缺少 id"}
    body = str(draft.get("body") or "").strip()
    if not body:
        reply_queue.log("complete-reject", rid, "草稿正文是空的")
        return {"ok": False, "error": "草稿正文为空，不接收"}
    if not draft.get("to"):
        reply_queue.log("complete-reject", rid, "缺少收件人")
        return {"ok": False, "error": "缺少收件人 to"}
    reply_queue.complete(rid, {
        "to": draft.get("to") or "",
        "subject": draft.get("subject") or "",
        "body": body,
        "note": draft.get("note") or "",
    })
    return {"ok": True, "id": rid, "doneCount": len(reply_queue.done())}


def api_reply_trace(q):
    """可观测性：一眼看清「排队 → 执行 → 回传」三段各自卡在哪

    返回队列快照（每条等了多久、有没有卡住）+ 最近事件日志。
    排查「点了加急没反应」时先看这个，不用再去翻目录。
    """
    pend = reply_queue.pending()
    snap = [{
        "id": r.get("id"), "uid": str(r.get("uid") or ""),
        "status": r.get("status"), "source": r.get("source"),
        "priority": r.get("priority") if r.get("priority") is not None else 1,
        "ageSec": reply_queue.age_sec(r),
        "stallAfter": reply_queue.stall_after(r),
        "stalled": reply_queue.is_stalled(r),
        "attempts": int(r.get("attempts") or 0),
        "last_error": r.get("last_error") or "",
        "from": (r.get("from") or "")[:40],
        "subject": (r.get("subject") or "")[:50],
    } for r in pend]
    n = int((q.get("limit") or ["120"])[0])
    return {
        "ok": True,
        "now": time.strftime("%Y-%m-%d %H:%M:%S"),
        "counts": {
            "queued": len([s for s in snap if s["status"] == "queued"]),
            "generating": len([s for s in snap if s["status"] == "generating"]),
            "failed": len([s for s in snap if s["status"] == "failed"]),
            "stalled": len([s for s in snap if s["stalled"]]),
            "done": len(reply_queue.done()),
        },
        "items": snap,
        "log": reply_queue.read_log(n),
        "genTimeoutSec": reply_queue.GEN_TIMEOUT,
    }


def api_reply_status(payload):
    """改队列条目的状态 —— WorkBuddy 那一侧用

    status：generating（开始起草）/ failed（失败，要带 error）。
    前端看不到这个接口，它只是让「生成中」和「失败」两种状态能落进队列文件，
    界面上才不会一直显示「排队中」。
    """
    rid = str(payload.get("id") or "")
    status = str(payload.get("status") or "")
    if not rid or status not in ("generating", "failed", "queued"):
        return {"ok": False, "error": "id 或 status 不对"}
    if status == "failed":
        ok = reply_queue.mark_failed(rid, str(payload.get("error") or "起草失败"))
    elif status == "generating":
        ok = reply_queue.mark_generating(rid)
    else:
        ok = reply_queue.set_status(rid, "queued")
    return {"ok": True, "id": rid, "status": status, "updated": ok}


def api_reply_retry(payload):
    """重试一次失败的记录：failed → queued（attempts 保留，看得出重试了几次）"""
    rid = str(payload.get("id") or "")
    if not rid:
        return {"ok": False, "error": "缺少 id"}
    return {"ok": True, "id": rid, "retried": reply_queue.retry(rid)}


def _norm_subject(s):
    """主题归一化：去掉 Re:/答复: 前缀和所有空白，用来判断「是不是同一个往来线程」"""
    s = (s or "").lower()
    s = re.sub(r"^(re|fw|fwd|答复|回复|转发)\s*[:：]?\s*", "", s)
    return re.sub(r"[\s　]+", "", s).strip()


def api_reply_autopick(q):
    """自动兜底：挑出「该回还没回」的邮件，给定时任务去预生成草稿

    筛选规则（宁可少挑，也不给不该回的起草）：
      1. 未读
      2. days 天内（默认 3 天）
      3. 分级不是「× 可忽略」也不是「⚙ 系统处理」—— 那两类不用人回
      4. 不在队列里（排队中 / 生成中 / 失败过的都算「已经处理过」）
      5. Sent 里没有同主题的邮件 —— 已经回过的人不再回

    apply=1 时直接以 source=auto 入队（priority=1，排在用户手动点的后面）。

    ⚠️ **积压闸门**：done/ 里还有一大堆没人去取的草稿时，本轮直接不入队。
    没有这道闸的话，「自动挑一封 → 用户不看 → 下轮再挑一封」会滚成雪球：
    每 6 小时最多 +5 封，一天就是 20 封，一周 140 封堆在队列里没人管。
    积压上限见 `AUTOPICK_MAX_BACKLOG`，过期的由 `sweep_stale()` 归档。
    """
    folder = (q.get("folder") or ["INBOX"])[0]
    limit = int((q.get("limit") or ["5"])[0])
    days = float((q.get("days") or ["3"])[0])
    apply_now = str((q.get("apply") or ["0"])[0]).lower() in ("1", "true", "yes")

    backlog = len(reply_queue.done())
    if apply_now and backlog >= reply_queue.AUTOPICK_MAX_BACKLOG:
        reply_queue.log("autopick-skip", "-",
                        "done/ 里积压 %d 封没人取（上限 %d），本轮不再自动入队"
                        % (backlog, reply_queue.AUTOPICK_MAX_BACKLOG))
        return {"ok": True, "picked": 0, "candidates": [], "enqueued": [],
                "skipped": True, "backlog": backlog,
                "hint": "已起草好的还有 %d 封没取走，这轮先不自动挑新的了" % backlog}

    m = connect()
    try:
        st, _ = m.select('"%s"' % folder, readonly=True)
        if st != "OK":
            return {"ok": False, "error": "打不开文件夹 %s" % folder}
        st, data = m.uid("search", None, "ALL")
        uids = sorted((int(u) for u in data[0].split()), reverse=True)[:250]
        rows = fetch_meta(m, folder, uids)

        # 已回过的线程（Sent 里最近的 150 封）
        sent_subj = set()
        try:
            st2, _ = m.select('"Sent"', readonly=True)
            if st2 == "OK":
                st3, d3 = m.uid("search", None, "ALL")
                suids = sorted((int(u) for u in d3[0].split()), reverse=True)[:150]
                for r in fetch_meta(m, "Sent", suids):
                    sent_subj.add(_norm_subject(r.get("subject")))
        except Exception:
            pass
    finally:
        try:
            m.logout()
        except Exception:
            pass

    rules = classify_rules()
    in_queue = {str(r.get("uid") or "") for r in reply_queue.pending()}
    in_queue |= {str(r.get("uid") or "") for r in reply_queue.done()}
    deadline = time.time() - days * 86400

    picks = []
    for r in rows:
        uid = str(r.get("uid") or "")
        if not uid or uid in in_queue:
            continue
        if r.get("seen"):                        # 只要未读
            continue
        if (r.get("ts") or 0) < deadline:
            continue
        addr, name = email.utils.parseaddr(r.get("from") or "")
        addr = addr.lower()
        if classify_of(addr, name, rules) in ("dump", "act"):
            continue
        # 群发地址不该回（科研办通知、系统通告这类），起草了也是废稿
        if re.search(r"no-?reply|notification|newsletter|mailer|digest|donotreply", addr, re.I):
            continue
        if _norm_subject(r.get("subject")) in sent_subj:
            continue
        picks.append({
            "folder": folder, "uid": uid,
            "subject": r.get("subject") or "", "from": r.get("from") or "",
            "date": r.get("date") or "",
        })
        if len(picks) >= limit:
            break

    enqueued = []
    if apply_now:
        for p in picks:
            rid = reply_queue.enqueue(p, source="auto")
            enqueued.append(rid)
    return {"ok": True, "picked": len(picks), "candidates": picks,
            "enqueued": enqueued, "scanned": len(rows),
            "sent_threads": len(sent_subj)}


# ---------------------------------------------------------------- 实时同步
#
# 问题：以前只有「拉」没有「推」—— 页面打开后不会再问服务器，于是
#   · Foxmail / Zimbra 收到的新邮件，页面看不到（要手动刷新）
#   · 在别的客户端删掉 / 标已读的邮件，页面还留在那儿
#
# 对策：服务端开一条**专用的只读 IMAP 连接**挂 IDLE 在「当前正在看的文件夹」上，
# 服务器一有变化就主动推过来（实测这台服务器会推 EXISTS / EXPUNGE / FETCH FLAGS），
# 再由服务端转成 SSE 长连接推给浏览器。全程只读，不改用户邮箱。
#
# 为什么自己在 IMAP 连接上分线程：Python 3.13 的 imaplib **没有** idle()，
# 只能手写（send "IDLE" → 等 '+ idling' → 收未标记响应 → send "DONE"）。
# 注意别用 socket.settimeout 等响应：超时会把 imaplib 内部那个缓冲 reader 弄坏，
# 之后这条连接上的读全是脏的。改成 select 等 socket 可读、只有可读才 readline。

WATCH = {
    "folder": "INBOX",      # 当前监听的文件夹（前端切文件夹时会改）
    "gen": 0,               # 每换一次文件夹 +1，watcher 看到变化就重连
    "subs": [],             # 每个 SSE 客户端一个 queue.Queue
    "last": None,           # 最近一条事件，新订阅者先补一条
    "status": "starting",   # starting / idling / reconnecting / error
    "error": "",
    "events": 0,            # 一共推过多少条（排查用）
    "lastAt": "",
    "lock": threading.Lock(),
}

IDLE_CYCLE = 60             # 一轮 IDLE 最多挂 60 秒就 DONE 重来
#   为什么不用 RFC 建议的 29 分钟：imaplib 会把多行一起缓冲，只靠 select 会漏读
#   压在缓冲里的行；每轮结束的 DONE 往返正好把它们逼出来。60 秒是「漏读兜底延迟」
#   和「往返开销」的折中 —— 常见情况（第一行就是 EXISTS）仍然是立刻推送。
WATCH_REFRESH = 10 * 60     # 即便没有事件，也定期重连一次，防止半死连接


def _watch_hub(ev):
    """把事件发给所有 SSE 订阅者。队列满了就丢 —— 宁可少推一条，别阻塞。"""
    WATCH["events"] += 1
    WATCH["last"] = ev
    WATCH["lastAt"] = time.strftime("%H:%M:%S")
    with WATCH["lock"]:
        subs = list(WATCH["subs"])
    for sub in subs:
        try:
            sub.put_nowait(ev)
        except Exception:
            pass


def watch_folder(folder):
    """告诉监听线程：现在要看这个文件夹了。变了就让它重连。"""
    folder = str(folder or "INBOX")
    if folder == WATCH["folder"]:
        return False
    WATCH["folder"] = folder
    WATCH["gen"] += 1
    WATCH["status"] = "reconnecting"
    return True


def _idle_read(m, timeout):
    """最多等 timeout 秒读一行；这段没数据就返回 None

    ⚠️ 只能用 select 等，**不能给 socket 设超时**：
    CPython 3.5 起，socket 一旦超时过就会被标记，之后在它派生出的读句柄上
    再读会直接抛 `OSError: cannot read from timed out object` —— 连接当场报废。
    （第一版用了 settimeout，结果空闲一秒就断一次，重连风暴。）

    select 的代价：imaplib 会把读到的字节缓存进自己的 BufferedReader，
    可能一次读进来好几行；只看 socket 可读性，压在缓冲里的行得等下次有数据
    才会被读到。所以 IDLE 周期刻意收短（见 IDLE_CYCLE），
    每轮结束时发 DONE 逼出服务器的 tag 响应，那些压着的行就会在这时被读出来。
    """
    try:
        r, _, _ = select.select([m.sock], [], [], timeout)
    except (OSError, ValueError) as e:
        raise IOError("连接断了：%s" % e)
    if not r:
        return None
    try:
        return m.readline()
    except imaplib.IMAP4.abort as e:
        raise IOError("连接断了：%s" % e)


def _idle_once(m, timeout=IDLE_CYCLE):
    """在已 SELECT 的连接上 IDLE 最多 timeout 秒，返回收到的未标记响应行

    规矩：只要不是自己的 tag、也不是 `+ idling`，就一律当成**事件**收下，
    绝不因此报错重连 —— 未标记响应在任何时刻都可能冒出来
    （上一轮边界上攒下的 EXPUNGE 就会正好插在这里）。
    """
    tag = m._new_tag()
    m.send(b"%s IDLE\r\n" % tag)
    lines = []
    # 等 `+ idling`；这期间冒出来的未标记响应先收着
    deadline = time.time() + 20
    while time.time() < deadline:
        line = _idle_read(m, 1.0)
        if line is None:
            continue
        if line.startswith(b"+"):
            break
        if line.startswith(tag):
            raise IOError("IDLE 被拒绝: %r" % line)
        lines.append(line)
    else:
        raise IOError("等 IDLE 确认超时")

    # 收事件。**收到第一条变化就收尾** —— 不能等整轮跑完，
    # 否则一封新邮件要等到 60 秒后才推给页面，就不叫实时了。
    # 同一批里剩下的行（EXISTS 后面常跟着 RECENT / FETCH）会在下面的
    # DONE 收尾里被读出来一起算，最坏也是下一轮立刻补上。
    deadline = time.time() + timeout
    while time.time() < deadline:
        line = _idle_read(m, 1.0)
        if line is None:
            continue
        if line.startswith(tag):
            break                      # 服务器主动收尾
        lines.append(line)
        if any(k in line for k in (b"EXISTS", b"EXPUNGE", b"FETCH", b"BYE")):
            break

    # 收尾：DONE 之后服务器必回 tag，这一读会把缓冲里压着的行一并带出来
    try:
        m.send(b"DONE\r\n")
    except OSError as e:
        raise IOError("连接断了：%s" % e)
    deadline = time.time() + 5
    while time.time() < deadline:
        line = _idle_read(m, 1.0)
        if line is None:
            continue
        if line.startswith(tag):
            break
        lines.append(line)
    return lines


def _parse_idle(lines):
    """把 IDLE 收到的行翻译成前端看得懂的事件"""
    exists = expunged = flags = 0
    for l in lines:
        if b"EXISTS" in l:
            mm = re.match(rb"\*\s+(\d+)\s+EXISTS", l)
            if mm:
                exists = int(mm.group(1))
        elif b"EXPUNGE" in l:
            expunged += 1
        elif b"FETCH" in l and b"FLAGS" in l:
            flags += 1
    return exists, expunged, flags


def watcher():
    """常驻线程：IDLE 监听 WATCH['folder']，有变化就广播

    任何异常都退避重连（2→30 秒），线程本身绝不退出 ——
    它死了，实时同步就无声无息地没了（那正是这次要解决的问题）。
    """
    backoff = 2
    while True:
        gen, folder = WATCH["gen"], WATCH["folder"]
        m = None
        try:
            m = connect()
            st, _ = m.select('"%s"' % imap_folder(folder), readonly=True)
            if st != "OK":
                raise IOError("select %s -> %s" % (folder, st))
            WATCH["status"] = "idling"
            WATCH["error"] = ""
            backoff = 2
            reply_queue.log("watch", "-", "开始监听 %s" % folder)
            started = time.time()
            while WATCH["gen"] == gen and time.time() - started < WATCH_REFRESH:
                lines = _idle_once(m)
                exists, expunged, flags = _parse_idle(lines)
                if exists or expunged or flags:
                    _watch_hub({"type": "change", "folder": folder,
                                "exists": exists, "expunged": expunged, "flags": flags,
                                "at": time.strftime("%H:%M:%S")})
                    reply_queue.log("watch-event", "-", "%s: exists=%d expunge=%d flags=%d"
                                    % (folder, exists, expunged, flags))
            if WATCH["gen"] != gen:
                continue           # 用户换了文件夹，立刻重连
        except Exception as e:
            WATCH["status"] = "error"
            WATCH["error"] = "%s: %s" % (type(e).__name__, e)
            reply_queue.log("watch-error", "-", WATCH["error"][:120])
            # 断了要让前端知道 —— 它收到 resync 会退回轮询，并做一次全量对账
            _watch_hub({"type": "resync", "folder": WATCH["folder"],
                        "error": WATCH["error"], "at": time.strftime("%H:%M:%S")})
            time.sleep(backoff)
            backoff = min(backoff * 2, 30)
        finally:
            if m is not None:
                try:
                    m.logout()
                except Exception:
                    pass


def api_check(q):
    """轻量兜底：没有 SSE 时前端每 30 秒问一次

    只回「多少封 / 下一个 uid / 未读几封」，不拉任何邮件体，所以很便宜。
    前端拿它和手里的数据比：对不上就做一次 diff 刷新。
    """
    folder = (q.get("folder") or ["INBOX"])[0]
    m = connect()
    try:
        st, dat = m.status('"%s"' % imap_folder(folder), "(MESSAGES UIDNEXT UNSEEN)")
        raw = b" ".join(x for x in dat if isinstance(x, bytes)).decode("utf-8", "replace")
        got = dict((k.upper(), int(v)) for k, v in re.findall(r"([A-Z]+)\s+(\d+)", raw))
        return {"ok": True, "folder": folder, "messages": got.get("MESSAGES", 0),
                "uidNext": got.get("UIDNEXT", 0), "unseen": got.get("UNSEEN", 0),
                "watch": {"status": WATCH["status"], "folder": WATCH["folder"],
                          "events": WATCH["events"], "lastAt": WATCH["lastAt"],
                          "error": WATCH["error"], "subs": len(WATCH["subs"])}}
    except Exception as e:
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    finally:
        try:
            m.logout()
        except Exception:
            pass



# ---------------------------------------------------------------- HTTP

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **kw):
        super().__init__(*a, directory=WEB_DIR, **kw)

    def log_message(self, fmt, *args):
        sys.stderr.write("  %s\n" % (fmt % args))

    def _sse(self, q):
        """SSE 长连接：把 watcher 推来的变化转给浏览器

        这是长连接，不能走 _json（那会带 Content-Length 立刻结束）。
        这里只写流、不设长度，靠心跳（每 20 秒一行注释）维持；
        写失败 = 浏览器关了页面 → 退订收工。
        """
        watch_folder((q.get("folder") or ["INBOX"])[0])
        sub = queue.Queue(maxsize=64)
        with WATCH["lock"]:
            WATCH["subs"].append(sub)
            last = WATCH["last"]
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            self._sse_write({"type": "hello", "folder": WATCH["folder"],
                             "status": WATCH["status"], "events": WATCH["events"],
                             "at": time.strftime("%H:%M:%S")})
            if last:
                self._sse_write(last)
            while True:
                try:
                    ev = sub.get(timeout=20)
                    self._sse_write(ev)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
            pass
        finally:
            with WATCH["lock"]:
                if sub in WATCH["subs"]:
                    WATCH["subs"].remove(sub)

    def _sse_write(self, obj):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.wfile.write(b"data: " + raw + b"\n\n")
        self.wfile.flush()

    def end_headers(self):
        # 开发模式：静态资源不缓存，改完代码刷新即可生效
        p = self.path.split("?")[0]
        if p == "/" or p.endswith((".html", ".js", ".css")):
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        super().end_headers()

    # ---------------- V2 桥接（UI / API / SSE）----------------
    def _v2_ui(self):
        """V2 工作桶界面：复用 mail_workbench/ui/index.html，注入 /api/v2 前缀。"""
        try:
            ui = os.path.join(HERE, "mail_workbench", "ui", "index.html")
            body = V2_BRIDGE.render_ui(ui)
        except Exception as e:
            raw = ("V2 UI 渲染失败：%s: %s" % (type(e).__name__, e)).encode("utf-8")
            self.send_response(500)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            return self.wfile.write(raw)
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
        self.end_headers()
        self.wfile.write(body)

    def _v2_json(self, method, path, q, payload):
        try:
            if method == "GET" and V2_BRIDGE.to_internal(path) == "/api/events":
                return V2_BRIDGE.sse(self, q)
            obj, code = V2_BRIDGE.dispatch(method, path, q, payload)
            if obj is None:
                return self._json({"ok": False, "error": "V2 路由未实现：%s %s"
                                   % (method, path)}, 404)
            # 附件下载这类二进制响应：不能走 JSON（否则得 base64，还会丢掉
            # Content-Disposition，浏览器就没法用原生下载行为）。
            from mail_workbench.server import Raw as _V2Raw
            if isinstance(obj, _V2Raw):
                return self._v2_raw(obj, code)
            return self._json(obj, code)
        except Exception as e:
            return self._json({"ok": False, "error": "V2 桥接异常：%s: %s"
                               % (type(e).__name__, e)}, 500)

    def _v2_raw(self, raw, code=200):
        try:
            self.send_response(code)
            for k, v in raw.headers().items():
                self.send_header(k, v)
            self.send_header("Content-Length", str(len(raw.data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw.data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        raw = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        # ---- V2 拦截点（落在 try 之外：V2 异常绝不影响 V1 的既有路由）----
        if V2_READY and V2_BRIDGE.is_v2_ui(u.path):
            return self._v2_ui()
        if V2_READY and V2_BRIDGE.is_v2_api(u.path):
            return self._v2_json("GET", u.path, q, None)
        try:
            if u.path == "/api/bootstrap":
                return self._json(api_bootstrap(q))
            if u.path == "/api/messages":
                return self._json(api_messages(q))
            if u.path == "/api/tag":
                return self._json(api_tag(q))
            if u.path == "/api/message":
                return self._json(api_message(q))
            if u.path == "/api/search":
                return self._json(api_search(q))
            if u.path == "/api/find":
                return self._json(api_find(q))
            if u.path == "/api/health":
                return self._json({"ok": True, "user": CFG.get("user", ""), "has_cred": bool(CFG.get("pass"))})
            if u.path == "/api/reply-queue":
                return self._json(api_reply_queue(q))
            if u.path == "/api/reply-draft":
                return self._json(api_reply_draft(q))
            if u.path == "/api/reply-autopick":
                return self._json(api_reply_autopick(q))
            if u.path == "/api/reply-trace":
                return self._json(api_reply_trace(q))
            if u.path == "/api/check":
                return self._json(api_check(q))
            if u.path == "/api/events":
                return self._sse(q)
            if u.path == "/api/version":
                return self._json({"ok": True, "v": web_version()})
        except Exception as e:
            return self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)
        return super().do_GET()

    def do_POST(self):
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8")) if n else {}
        except Exception:
            payload = {}
        try:
            if V2_READY and V2_BRIDGE.is_v2_api(u.path):
                return self._v2_json("POST", u.path, parse_qs(u.query), payload)
            if u.path == "/api/flags":
                return self._json(api_flags(payload))
            if u.path == "/api/trash":
                return self._json(api_trash(payload))
            if u.path == "/api/untash":
                return self._json(api_untash(payload))
            if u.path == "/api/draft":
                return self._json(api_draft(payload))
            if u.path == "/api/send":
                return self._json(api_send(payload))
            if u.path == "/api/file-sent":
                return self._json(api_file_sent(payload))
            if u.path == "/api/attachment":
                return self._json(api_attachment(payload))
            if u.path == "/api/reply-request":
                return self._json(api_reply_request(payload))
            if u.path == "/api/reply-cancel":
                return self._json(api_reply_cancel(payload))
            if u.path == "/api/reply-consume":
                return self._json(api_reply_consume(payload))
            if u.path == "/api/reply-status":
                return self._json(api_reply_status(payload))
            if u.path == "/api/reply-retry":
                return self._json(api_reply_retry(payload))
            if u.path == "/api/reply-complete":
                return self._json(api_reply_complete(payload))
            if u.path == "/api/reply-purge":
                return self._json(api_reply_purge(payload))
        except Exception as e:
            return self._json({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)
        return self._json({"ok": False, "error": "未知接口"}, 404)


def watchdog(interval=30):
    """队列看门狗 —— 服务进程自己每 30 秒扫一次

    为什么必须有它：起草是 WorkBuddy 的定时任务在做，服务这边完全不知道
    对方有没有跑、跑到哪一步。AI 标记了 generating 之后一旦中断（进程被杀、
    网络断了、超了时），那条记录就永远停在 generating —— 而定时任务被明确
    告知「生成中的不要重复处理」，于是这封邮件再也没人管。
    最典型的症状就是：界面显示「已加急」，等十分钟二十分钟毫无动静。

    这里只做三件兜底的事：
      1. generating 超时 → 回滚成 queued（重新排队，attempts+1）
      2. 排队 / 生成超过告警阈值 → 记一行日志，界面据此标黄（不擅自改状态）
      3. 过期的条目（草稿 7 天没人取、失败 7 天没人重试）→ 挪进 expired/ 归档，
         队列不会无限堆下去
    """
    while True:
        time.sleep(interval)
        try:
            saved = reply_queue.sweep()
            for r in reply_queue.stalled_list():
                reply_queue.log_stalled(r)
            archived = reply_queue.sweep_stale()
            if archived:
                print("  [看门狗] 归档 %d 条过期条目：%s" % (len(archived), archived))
            if saved:
                print("  [看门狗] 回滚 %d 条卡死的生成任务：%s" % (len(saved), saved))
        except Exception as e:      # 看门狗自己绝不能崩，崩了服务就没兜底了
            print("  [看门狗] 出错：%r" % (e,))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8080)
    ap.add_argument("--host", default="127.0.0.1")
    args = ap.parse_args()
    if not CFG.get("user") or not CFG.get("pass"):
        print("警告：没读到邮箱凭据，IMAP 接口会失败（本地索引检索仍可用）。")
        print("      请在 config.json 或 ~/.workbuddy/secrets/mail.cred 里配置。")
    print("邮件工作台已启动：http://%s:%d" % (args.host, args.port))
    print("账户：%s" % CFG.get("user", "(未配置)"))
    try:
        print("本地索引：%d 封" % len(get_index()))
    except Exception:
        pass
    threading.Thread(target=watchdog, daemon=True).start()
    print("队列看门狗已启动（每 30 秒扫一次卡死任务）。")
    threading.Thread(target=watcher, daemon=True).start()
    print("邮件实时监听已启动（IMAP IDLE，当前监听 %s）。" % WATCH["folder"])

    # ---- V2：工作桶界面 + DraftJobQueue + 本地 scheduler ----
    if V2_READY:
        try:
            st = V2_BRIDGE.get_app(autostart=True)
            print("V2 已就绪：http://%s:%d/workbench" % (args.host, args.port))
            print("     工作桶 / DraftJobQueue / Worker 契约：/api/v2/draft-jobs/next")
            print("     事件流（SSE）：/api/v2/events")
            print("     V1 经典三栏视图保持不变：http://%s:%d/" % (args.host, args.port))
        except Exception as e:
            print("V2 启动失败（V1 仍可用）：%s" % e)
        try:
            print("V1 文件队列双读迁移：%s" % V2_BRIDGE.migrate_reply_queue())
        except Exception as e:
            print("V1 队列迁移跳过：%s" % e)

    print("按 Ctrl+C 停止。")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
