# -*- coding: utf-8 -*-
"""命令行入口。

用途：
  * 给 automation 用的**薄触发**（尤其 `recovery` —— 队列无异常时静默退出）
  * 人工排查（doctor / jobs / show / buckets / brief）
  * 一次性运维（migrate / import-foxmail / secrets-init）

刻意不依赖 HTTP：automation 与人工诊断都应该能在服务没启动时工作。
"""
from __future__ import annotations

import argparse
import json
import os
import sys

from . import config as cfgmod
from . import metrics as metricsmod
from . import util
from .draft import queue as dq
from .draft import recovery as rcv
from .mail import foxmail_index, imap_client, smtp_client
from .mail import sync_engine as syncmod
from .storage.database import default_db
from .storage.repo import Repo
from .thread import aggregator as agg
from .thread import context_builder as cbm
from .workflow import actions as actionsmod
from .workflow import brief as briefmod
from .workflow import buckets as bucketmod
from .workflow.candidate_detector import CandidateDetector


def _ctx(args):
    cfg = cfgmod.load_config()
    if getattr(args, "log", False):
        cfgmod.ensure_dirs(cfg)
    repo = Repo(default_db(log=None))
    return cfg, repo


def _out(obj, as_json=False, text=None):
    if as_json:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))
    elif text is not None:
        print(text)
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


# ==========================================================================
def cmd_doctor(args):
    cfg, repo = _ctx(args)
    from .server import build_app
    app = build_app(cfg)
    h = app.health()
    if args.json:
        _out(h, True)
    else:
        print("总体状态 :", h["status"].upper())
        print("用时     :", h["elapsed_ms"], "ms")
        for k, v in h["components"].items():
            print("  %-14s %s" % (k, v["status"]))
        if h["problems"]:
            print("\n需要注意：")
            for p in h["problems"]:
                print("  -", p)
    return 0 if h["status"] != "error" else 1


def cmd_health_json(args):
    cfg, repo = _ctx(args)
    from .server import build_app
    print(json.dumps(build_app(cfg).health(), ensure_ascii=False))
    return 0


def cmd_sync(args):
    cfg, repo = _ctx(args)
    eng = syncmod.SyncEngine(repo, cfg)
    res = eng.sync_all(folders=args.folders, limit=args.limit, reason="cli")
    _out(res, args.json or not args.quiet)
    return 0 if res.get("ok") else 1


def cmd_import_foxmail(args):
    cfg, repo = _ctx(args)
    res = foxmail_index.import_history(repo, cfg, limit=args.limit)
    _out(res, True)
    return 0 if res.get("ok") else 1


def cmd_brief(args):
    cfg, repo = _ctx(args)
    snap = briefmod.build(repo, cfg)
    briefmod.store_snapshot(repo, snap)
    if args.json:
        _out(snap, True)
    else:
        print(snap["text"])
    return 0


def cmd_buckets(args):
    cfg, repo = _ctx(args)
    data = bucketmod.compute(repo, cfg, limit=args.limit)
    if args.json:
        _out(data, True)
        return 0
    labels = data["labels"]
    for k, n in data["counts"].items():
        print("%-20s %s  %d" % (labels.get(k, k), "·" * min(n, 40), n))
    print("\n流水线：", data["pipeline"])
    for k, items in data["buckets"].items():
        if not items:
            continue
        print("\n[%s]" % labels.get(k, k))
        for it in items[: args.limit]:
            print("  %s | %s | %s" % ((it.get("from_name") or it.get("from_addr") or "")[:16],
                                      (it.get("subject") or "")[:44],
                                      it.get("classification") or ""))
    return 0


def cmd_jobs(args):
    cfg, repo = _ctx(args)
    statuses = [s.strip() for s in args.status.split(",")] if args.status else None
    jobs = repo.list_jobs(statuses=statuses, limit=args.limit)
    if args.json:
        _out([dq.to_public(j) for j in jobs], True)
        return 0
    if not jobs:
        print("没有匹配的任务。当前计数：", repo.job_counts())
        return 0
    for j in jobs:
        print("%-22s %-13s pri=%-3s retry=%s  %s" % (
            j["job_id"], j["status"], j.get("priority"), j.get("retry_count") or 0,
            (j.get("subject") or "")[:38]))
        if j.get("last_error"):
            print("    last_error:", j["last_error"][:110])
    print("\n计数：", repo.job_counts())
    return 0


def cmd_show(args):
    cfg, repo = _ctx(args)
    j = repo.get_job(args.job_id)
    if not j:
        print("任务不存在：%s" % args.job_id)
        return 1
    if args.json:
        _out(dq.to_public(j, include_context=args.context, include_plan=True), True)
        return 0
    pub = dq.to_public(j, include_context=args.context, include_plan=True)
    print("job_id     :", pub["job_id"])
    print("状态       :", pub["status"], "(%s)" % pub["status_label"])
    print("收件人     :", ", ".join(pub["recipients"] or []))
    print("主题       :", pub["subject"])
    print("模式       :", pub["draft_mode"])
    print("重试       :", pub["retry_count"])
    print("租约       :", pub["claimed_by"], pub["lease_until"])
    if pub.get("plan"):
        print("\nReplyPlan:")
        print(json.dumps(pub["plan"], ensure_ascii=False, indent=2))
    if pub.get("missing_information"):
        print("\n缺信息:")
        for mi in pub["missing_information"]:
            print("  -", mi.get("label") or mi.get("key"))
    if pub.get("draft_text"):
        print("\n--- 草稿 ---")
        print(pub["draft_text"])
    print("\n事件：")
    for e in repo.job_events(j["job_id"]):
        print("  %s %-14s -> %-14s %s" % (e["ts"], e["from_status"], e["to_status"], e["note"] or ""))
    return 0


def cmd_recovery(args):
    """automation 用的薄触发：无异常静默退出（退出码 0，不输出噪音）。"""
    cfg, repo = _ctx(args)
    if not args.force and not rcv.should_run(repo, cfg, min_interval_seconds=args.min_interval):
        if args.json:
            print(json.dumps({"silent": True, "reason": "throttled"}, ensure_ascii=False))
        return 0
    res = rcv.run_recovery(repo, cfg, dry_run=args.dry_run)
    if res["actions"] == 0:
        if args.json:
            print(json.dumps({"silent": True, "actions": 0}, ensure_ascii=False))
        return 0
    print("Recovery 处理了 %d 项：" % res["actions"])
    for k in ("expired_leases", "orphan_claims", "stale_queued", "retryable_failed",
              "expired_ready", "budget_exhausted", "expired_candidates", "reconciled"):
        if res.get(k):
            print("  %-20s %d" % (k, res[k]))
    return 0


def cmd_metrics(args):
    cfg, repo = _ctx(args)
    _out(metricsmod.snapshot(repo), args.json or True)
    return 0


def cmd_candidates(args):
    cfg, repo = _ctx(args)
    if args.scan:
        res = CandidateDetector(repo, cfg).scan(limit=args.limit)
        _out(res, True)
        return 0
    rows = repo.list_candidates(status=args.status, limit=args.limit)
    if args.json:
        _out(rows, True)
        return 0
    if not rows:
        print("没有 %s 状态的候选。" % args.status)
        return 0
    for c in rows:
        print("[%s] %s | %s | %s" % (c["created_at"][:16], (c["sender"] or "")[:24],
                                    (c["subject"] or "")[:40], c.get("reason") or ""))
    return 0


def cmd_context(args):
    cfg, repo = _ctx(args)
    pkg = cbm.MailContextBuilder(repo, cfg).build(message_id=args.message_id,
                                                  thread_id=args.thread_id)
    _out(pkg, True)
    return 0


def cmd_retarget(args):
    cfg, repo = _ctx(args)
    n = agg.rebuild_all(repo, cfg, limit=args.limit)
    print("重算线程数：", n)
    return 0


def cmd_worker(args):
    """最小可运行的 worker 循环（无 AI）。

    存在的意义：证明 Worker 契约是闭环可跑的 —— claim → context → plan → draft
    → 等人类审核。真实起草由 WorkBuddy（或任何模型）通过同样的 HTTP 契约完成。
    """
    cfg, repo = _ctx(args)
    from .draft import worker_contract as wc
    rounds = 0
    while rounds < args.rounds:
        rounds += 1
        claimed = wc.claim(repo, cfg, worker=args.name)
        job = claimed.get("job")
        if not job:
            print("没有可认领的任务。")
            return 0
        jid = job["job_id"]
        print("已认领 %s（%s）" % (jid, job["subject"]))
        brief = wc.get_context(repo, cfg, jid, worker=args.name)
        pkg = brief["context"]
        print("  上下文：thread=%s 近期 %d 封，待答问题 %d 个，截止 %d 条"
              % (pkg.get("thread_id"), len(pkg.get("recent_messages") or []),
                 len(pkg.get("open_questions") or []), len(pkg.get("known_deadlines") or [])))
        gate = brief.get("gate", {}).get("suggested_missing_information") or []
        if gate:
            print("  Missing Information Gate 提示 %d 项：" % len(gate))
            for g in gate:
                print("    -", g.get("label"))
        if args.no_ai:
            wc.submit_fail(repo, cfg, jid, "worker 无模型后端（--no-ai）", retryable=False)
            print("  已报告失败（不重试）。")
            continue
        print("  这里是 CLI 演示 worker：真实场景下 WorkBuddy 会调用 "
              "/plan 与 /draft 回填草稿。")
        wc.submit_fail(repo, cfg, jid, "cli demo worker 不做实际生成", retryable=False)
    return 0


def cmd_mode(args):
    cfg, repo = _ctx(args)
    print("状态机合法转换（default 状态用于说明）：")
    from .workflow import state_machine as sm
    for k, v in sm.describe().items():
        print("  %-14s -> %s" % (k, ", ".join(v) or "(终态)"))
    print("\n允许发送的状态：", sorted(sm.SEND_ALLOWED_STATES))
    return 0


def cmd_secrets_init(args):
    """把凭据写入 ~/.workbuddy/secrets/mail.env，并检查 V1 脚本是否还残留明文密码。"""
    cfg = cfgmod.load_config()
    path = cfgmod.secrets_file_hint()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    exists = os.path.exists(path)
    if exists and not args.force:
        print("凭据文件已存在：%s（如需覆盖加 --force）" % path)
    else:
        user = args.user or cfg.get("user") or ""
        pwd = args.password or ""
        if not pwd:
            print("缺少 --password；或先设置环境变量 SJTU_MAIL_PASS。")
            return 2
        with open(path, "w", encoding="utf-8") as f:
            f.write("# Mail Workbench 凭据（勿提交/勿同步）\n")
            f.write("SJTU_MAIL_USER=%s\n" % user)
            f.write("SJTU_MAIL_PASS=%s\n" % pwd)
        try:
            os.chmod(path, 0o600)
        except Exception:
            pass
        print("已写入 %s（权限已收紧到仅当前用户）" % path)
    # 扫描项目里是否仍有明文密码
    hits = scan_plaintext_passwords(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    if hits:
        print("\n⚠ 以下文件仍含疑似明文密码（建议改为读取凭据文件）：")
        for p, line in hits:
            print("  %s:%s" % (p, line))
        return 1
    print("\n未发现残留明文密码。")
    return 0


def scan_plaintext_passwords(root: str) -> list:
    import re
    hits = []
    pat = re.compile(r"(password|passwd|pass)\s*[\"']?\s*[:=]\s*[\"']([^\"'{}]{6,})[\"']", re.I)
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "data", "logs", ".git")]
        for fn in filenames:
            if not fn.endswith((".py", ".json", ".bat", ".cmd", ".ps1", ".sh")):
                continue
            p = os.path.join(dirpath, fn)
            try:
                with open(p, "r", encoding="utf-8", errors="ignore") as f:
                    for i, line in enumerate(f, 1):
                        m = pat.search(line)
                        if m and "os.environ" not in line and "secrets_file_hint" not in line:
                            hits.append((os.path.relpath(p, root), i))
                            break
            except OSError:
                continue
    return hits


def cmd_tick(args):
    cfg, repo = _ctx(args)
    from .workflow.scheduler import Scheduler
    s = Scheduler(repo, cfg)
    _out(s.tick(), True)
    return 0


# ==========================================================================
def build_parser():
    ap = argparse.ArgumentParser(prog="mail_workbench",
                                description="Mail Workbench 命令行")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("doctor", help="健康检查")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_doctor)

    p = sub.add_parser("sync", help="增量同步 IMAP")
    p.add_argument("--folders", nargs="*")
    p.add_argument("--limit", type=int)
    p.add_argument("--json", action="store_true")
    p.add_argument("--quiet", action="store_true")
    p.set_defaults(func=cmd_sync)

    p = sub.add_parser("import-foxmail", help="导入 Foxmail 历史索引（只读）")
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(func=cmd_import_foxmail)

    p = sub.add_parser("brief", help="生成每日简报（本地）")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_brief)

    p = sub.add_parser("buckets", help="查看工作桶")
    p.add_argument("--limit", type=int, default=10)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_buckets)

    p = sub.add_parser("jobs", help="列出草稿任务")
    p.add_argument("--status", default="")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_jobs)

    p = sub.add_parser("show", help="查看某个任务详情与审计事件")
    p.add_argument("job_id")
    p.add_argument("--json", action="store_true")
    p.add_argument("--context", action="store_true")
    p.set_defaults(func=cmd_show)

    p = sub.add_parser("recovery", help="队列自愈（automation 薄触发；无异常静默退出）")
    p.add_argument("--json", action="store_true")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force", action="store_true", help="忽略节流")
    p.add_argument("--min-interval", type=int, default=1800)
    p.set_defaults(func=cmd_recovery)

    p = sub.add_parser("metrics", help="指标快照")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_metrics)

    p = sub.add_parser("candidates", help="回复候选")
    p.add_argument("--status", default="new")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--scan", action="store_true", help="重新扫描一轮")
    p.add_argument("--json", action="store_true")
    p.set_defaults(func=cmd_candidates)

    p = sub.add_parser("context", help="打印某封邮件的 MailContextPackage")
    p.add_argument("--message-id")
    p.add_argument("--thread-id")
    p.set_defaults(func=cmd_context)

    p = sub.add_parser("retarget", help="重算全部线程聚合")
    p.add_argument("--limit", type=int, default=0)
    p.set_defaults(func=cmd_retarget)

    p = sub.add_parser("worker", help="最小 worker 循环（演示 Worker 契约闭环）")
    p.add_argument("--name", default="cli-worker")
    p.add_argument("--rounds", type=int, default=1)
    p.add_argument("--no-ai", action="store_true", default=True)
    p.set_defaults(func=cmd_worker)

    p = sub.add_parser("state-machine", help="打印状态机")
    p.set_defaults(func=cmd_mode)

    p = sub.add_parser("secrets-init", help="写凭据文件并扫描明文密码残留")
    p.add_argument("--user")
    p.add_argument("--password")
    p.add_argument("--force", action="store_true")
    p.set_defaults(func=cmd_secrets_init)

    p = sub.add_parser("tick", help="跑一次本地 scheduler tick")
    p.set_defaults(func=cmd_tick)

    return ap


def main(argv=None) -> int:
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = build_parser()
    args = ap.parse_args(argv)
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        return 130
    except Exception as e:
        print("ERROR: %s" % e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
