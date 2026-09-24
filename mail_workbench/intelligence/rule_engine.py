# -*- coding: utf-8 -*-
"""Rule Engine —— 确定性分类（P0-2：不浪费 LLM 调用）。

**完整移植 V1 `classify_rules.json` 的判定链**，规则文件零改动即可复用：
    黑名单域名/后缀  -> 系统类(act) -> 白名单前置(vip) -> 留档(read)
    -> 机器特征检测（白名单域名豁免） -> bulk 模式 -> 推广话术 -> 真人信号(中文署名)

V2 新增的机器信号（都来自邮件头，属确定性判据）：
    List-Unsubscribe / Precedence: bulk|list / Auto-Submitted != no

输出是 `MailClassification`（★重点/✉要回/⚙系统/○看一眼/×可忽略），
与 `WorkflowState`（等谁处理）**严格分离**（规范 §14）。
"""
from __future__ import annotations

import json
import os
import re

from .. import util
from ..constants import (
    CLASS_DISMISS, CLASS_IMPORTANT, CLASS_READ, CLASS_REPLY, CLASS_SYSTEM,
    TIER_TO_CLASS,
)


def load_rules(cfg: dict) -> dict:
    from .. import config as _cfg
    path = _cfg.rules_path(cfg)
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _norm(lst) -> list:
    return [str(x).lower() for x in (lst or [])]


def _is_machine_generated(addr: str, name: str, h: dict) -> bool:
    """机器群发 / 钓鱼邮件的特征检测（V1 原逻辑）。"""
    if "@" not in (addr or ""):
        return False
    local, dom = addr.lower().split("@", 1)
    nm = re.sub(r"[^a-z0-9]", "", (name or "").lower())

    if h.get("name_equals_localpart", True):
        if nm and nm == re.sub(r"[^a-z0-9]", "", local):
            return True
    if len(local) > int(h.get("localpart_maxlen", 15)):
        return True
    if re.search(r"\d{%d,}" % int(h.get("digits_in_localpart", 6)), local):
        return True

    labels = [x for x in dom.split(".") if x]
    generic = {"com", "cn", "net", "org", "edu", "gov", "info", "io", "co", "ac", "biz"}
    for lb in labels:
        if lb in generic:
            continue
        if h.get("digits_in_domain_label", True) and len(lb) >= 4 and re.search(r"\d", lb):
            return True
        lo = int(h.get("random_label_minlen", 6))
        hi = int(h.get("random_label_maxlen", 14))
        if re.fullmatch(r"[a-z]{%d,%d}" % (lo, hi), lb):
            return True

    if h.get("repeated_domain_token", True):
        main = labels[-2] if len(labels) >= 2 else labels[0]
        toks = re.split(r"[-]", main)
        if len(toks) > 1 and len(toks) != len(set(toks)):
            return True
    return False


def classify(rec: dict, rules: dict) -> str:
    """返回 MailClassification 常量。rec 需含 from_addr / from_name / subject。"""
    if not rules:
        return CLASS_READ
    addr = (rec.get("from_addr") or "").lower()
    dom = addr.split("@")[-1] if "@" in addr else ""
    tld = dom.split(".")[-1] if dom else ""
    subject = rec.get("subject") or ""

    # 1) 黑名单优先：宁可误扔，也别让垃圾挤进待办
    dmp = rules.get("dump", {})
    if addr in _norm(dmp.get("senders")) or dom in _norm(dmp.get("domains")):
        return CLASS_DISMISS
    if tld and tld in _norm(dmp.get("tlds")):
        return CLASS_DISMISS

    # 2) 系统类（要去系统里点，不用回邮件）
    act = rules.get("act", {})
    if addr in _norm(act.get("senders")) or dom in _norm(act.get("domains")):
        return CLASS_SYSTEM

    # 3) VIP
    vip = rules.get("vip", {})
    if addr in _norm(vip.get("senders")) or dom in _norm(vip.get("domains")):
        return CLASS_IMPORTANT

    # 4) 留档
    rd = rules.get("read", {})
    if addr in _norm(rd.get("senders")) or dom in _norm(rd.get("domains")):
        return CLASS_READ

    # 5) 白名单域名豁免机器特征检测（springer.com 之类会被随机域名规则错杀）
    trusted = _norm(rules.get("trusted_domains", {}).get("domains"))
    is_trusted = dom in trusted or any(dom.endswith("." + t) for t in trusted)
    if not is_trusted and _is_machine_generated(addr, rec.get("from_name") or "",
                                                rules.get("heuristics", {})):
        return CLASS_DISMISS

    # 6) V2 新增：邮件头层面的确定性机器信号
    if not is_trusted:
        prec = (rec.get("precedence") or "").lower()
        if rec.get("auto_submitted") or prec in ("bulk", "list", "junk"):
            return CLASS_READ

    # 7) bulk 地址模式
    bp = rules.get("bulk_pattern")
    if bp and re.search(bp, addr, re.I):
        return CLASS_READ

    # 8) 主题里的推广话术：不扔但降级，避免混进待办
    for kw in (rules.get("spam_keywords", {}).get("keywords") or []):
        if re.search(kw, subject, re.I):
            return CLASS_READ

    # 9) 真人信号兜底：显示名含中文，且不是【】开头的系统标记
    hn = rules.get("human_name", {})
    name = rec.get("from_name") or ""
    if hn.get("name_prefix_brackets_not_human", True) and name.lstrip().startswith("【"):
        return rules.get("defaults", {}).get("unmatched", "read")
    if hn.get("cjk_name_means_human", True) and re.search(r"[\u4e00-\u9fff]", name):
        return CLASS_REPLY

    return TIER_TO_CLASS.get(rules.get("defaults", {}).get("unmatched", "read"), CLASS_READ)


def classify_tier(rec: dict, rules: dict) -> str:
    """返回 V1 的 tier slug（vip/reply/act/read/dump），便于对照旧日报。"""
    from ..constants import CLASS_TO_TIER
    return CLASS_TO_TIER.get(classify(rec, rules), "read")


def is_broadcast(rec: dict, rules: dict) -> bool:
    """判定这封是不是「群发通知」——只知会、不需要我动手的那种。

    为什么需要这个独立判定：
      ★重点 里混着两类完全不同的邮件 ——
        (a) 科研办/基金委群发的《…项目申报指南征求意见的通知》，一天好几封，
            是**大范围通知**，知道即可；
        (b) 真人对人、或点名要你办的事（师生互选、职称聘任、面试报名）。
      单靠五级分类分不开它们（都是 IMPORTANT，或者都会落进 READ），
      而工作桶又必须只装 (b)。

    判定范围是**白名单式**的：只有 `bulk_notice.senders` / `domains` 里的发件人
    才做主题匹配。这样 CATL、专利代理等真人对人的往来完全不在作用域内，
    不存在误伤。命中 `except_keywords`（保命词）的一律不降级 ——
    比如同样来自科研办的「人员信息核查」「结题验收」「答辩」，那是真要动手的。

    全部是本地正则，确定性、可解释、可重算（P0-2）。
    """
    bn = rules.get("bulk_notice") or {}
    if not bn:
        return False
    addr = (rec.get("from_addr") or "").lower()
    dom = addr.split("@")[-1] if "@" in addr else ""
    if addr not in _norm(bn.get("senders")) and dom not in _norm(bn.get("domains")):
        return False

    subject = rec.get("subject") or ""
    if not subject:
        return False
    if any(re.search(re.escape(k), subject, re.I)
           for k in (bn.get("except_keywords") or [])):
        return False
    return any(re.search(re.escape(k), subject, re.I)
               for k in (bn.get("subject_keywords") or []))


def classify_ex(rec: dict, rules: dict) -> dict:
    """分类 + 「群发通知」标记，一次算完。

    返回 {"classification", "broadcast", "reasons"}。
    分成两个出口是为了让工作桶能拿到 broadcast ——
    但**判定只算一次**，避免两处各算一遍再分叉。
    """
    cls = classify(rec, rules)
    bcast = is_broadcast(rec, rules)
    reasons = []
    if bcast:
        bn = rules.get("bulk_notice") or {}
        subject = rec.get("subject") or ""
        hit = [k for k in (bn.get("subject_keywords") or [])
               if re.search(re.escape(k), subject, re.I)]
        reasons.append("群发通知（命中 %s）：只知会，不占用「待我处理」"
                       % ("、".join(hit[:3]) or "主题特征"))
        # 群发通知一律落到「○ 看一眼」——不扔（日后要检索），但不再是 ★重点
        cls = CLASS_READ
    return {"classification": cls, "broadcast": bcast, "reasons": reasons}


def explain(rec: dict, rules: dict) -> dict:
    """给 UI 看「为什么被判成这个级别」——避免用户面对黑箱。"""
    ex = classify_ex(rec, rules)
    cls = ex["classification"]
    addr = (rec.get("from_addr") or "").lower()
    dom = addr.split("@")[-1] if "@" in addr else ""
    reasons = list(ex["reasons"])
    dmp = rules.get("dump", {})
    if dom in _norm(dmp.get("domains")):
        reasons.append("域名在黑名单 dump.domains")
    if dom.split(".")[-1] in _norm(dmp.get("tlds")):
        reasons.append("TLD 在黑名单 dump.tlds")
    vip = rules.get("vip", {})
    if addr in _norm(vip.get("senders")):
        reasons.append("发件人在 vip.senders" + ("（但命中群发通知，已降级）" if ex["broadcast"] else ""))
    elif dom in _norm(vip.get("domains")):
        reasons.append("域名在 vip.domains" + ("（但命中群发通知，已降级）" if ex["broadcast"] else ""))
    act = rules.get("act", {})
    if dom in _norm(act.get("domains")):
        reasons.append("域名在 act.domains（去系统处理）")
    trusted = _norm(rules.get("trusted_domains", {}).get("domains"))
    if dom in trusted:
        reasons.append("域名在 trusted_domains 白名单")
    if rec.get("auto_submitted"):
        reasons.append("Auto-Submitted 头表明是自动回复")
    if (rec.get("precedence") or "").lower() in ("bulk", "list"):
        reasons.append("Precedence 为 bulk/list")
    if re.search(r"[\u4e00-\u9fff]", rec.get("from_name") or ""):
        reasons.append("发件人显示名含中文（真人信号）")
    if not reasons:
        reasons.append("未命中任何规则，按 defaults.unmatched 处理")
    return {"classification": cls, "broadcast": ex["broadcast"],
            "reasons": reasons, "domain": dom}


def summarize_buckets(records: list, rules: dict) -> dict:
    """给 Daily Brief 用的本地聚合（不调用 LLM）。"""
    from ..constants import CLASS_LABEL, CLASS_ORDER
    buckets = {c: [] for c in CLASS_ORDER}
    for r in records:
        buckets[classify(r, rules)].append(r)
    return {
        "total": len(records),
        "by_class": {c: {"label": CLASS_LABEL[c], "count": len(v)} for c, v in buckets.items()},
        "buckets": buckets,
    }


def reclassify_all(repo, cfg: dict, log=None, limit: int = 0,
                   trigger_source: str = "reclassify") -> dict:
    """用当前规则文件重算全库分类与群发标记（幂等）。

    **改完 classify_rules.json 必须跑一次。** 分类结果是存在库里的，
    不重算的话界面上的标签与工作桶都还是旧判定 ——
    这正是「规则改了但看起来没生效」的唯一原因。

    只重算源自信箱的邮件（source='imap'）：
      * Foxmail 历史邮件没有真实分类语义（它们是 ARCHIVED 上下文），
        硬算会凭空多出上千条无意义标签；
      * 我方发出的邮件（Sent）本来就不分类。

    分类结果会同时刷新 priority 与 is_broadcast，保证两者永远一致；
    但**不动 workflow_state** —— 那是人的决定，规则重算不许覆盖。
    """
    rules = load_rules(cfg)
    if not rules:
        return {"ok": False, "reason": "没有规则文件", "scanned": 0, "changed": 0}

    sql = ("SELECT message_id, from_addr, from_name, subject, classification, "
           "       priority, COALESCE(is_broadcast,0) AS is_broadcast, "
           "       COALESCE(list_unsubscribe,0) AS list_unsubscribe, "
           "       COALESCE(auto_submitted,0) AS auto_submitted, "
           "       COALESCE(precedence,'') AS precedence "
           "FROM messages WHERE source='imap' AND deleted=0")
    if limit and limit > 0:
        sql += " ORDER BY internal_ts DESC LIMIT %d" % int(limit)
    rows = repo.db.query(sql)

    from ..constants import CLASS_PRIORITY
    changed, bcast_n = 0, 0
    touched_threads = set()
    for r in rows:
        rec = {
            "from_addr": r["from_addr"], "from_name": r["from_name"],
            "subject": r["subject"],
            "list_unsubscribe": bool(r["list_unsubscribe"]),
            "auto_submitted": bool(r["auto_submitted"]),
            "precedence": r["precedence"] or "",
        }
        res = classify_ex(rec, rules)
        cls, bcast = res["classification"], res["broadcast"]
        if bcast:
            bcast_n += 1
        if cls == r["classification"] and int(bcast) == int(r["is_broadcast"] or 0) \
                and CLASS_PRIORITY.get(cls, 50) == (r["priority"] or 50):
            continue
        repo.set_classification(r["message_id"], cls, is_broadcast=bcast)
        changed += 1
        tid = repo.db.scalar(
            "SELECT thread_id FROM messages WHERE message_id = ?", (r["message_id"],))
        if tid:
            touched_threads.add(tid)

    # 受影响的线程要重算聚合（分类变了会影响线程级 classification/priority）
    from ..thread import aggregator as _agg
    for tid in touched_threads:
        try:
            _agg.aggregate(repo, tid, cfg)
        except Exception:
            continue

    out = {"ok": True, "scanned": len(rows), "changed": changed,
           "broadcast": bcast_n, "threads_rebuilt": len(touched_threads),
           "rules_path": _rules_path(cfg)}
    if log:
        log("reclassify: 扫描 %d，改动 %d，群发通知 %d，重算线程 %d"
            % (len(rows), changed, bcast_n, len(touched_threads)))
    return out


def _rules_path(cfg: dict) -> str:
    try:
        from .. import config as _cfg
        return _cfg.rules_path(cfg)
    except Exception:
        return ""
