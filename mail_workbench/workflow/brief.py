# -*- coding: utf-8 -*-
"""Daily Mail Brief（规范 §18）。

定位：**不要逐封邮件骚扰用户**。汇总成一份：
    今日待处理 / 高优先级 / AI 草稿 / 等待别人回复 / 即将 deadline / snooze 到期

实现策略：
  * 数字与清单全部由本地 SQL 计算（廉价、确定、无需 LLM）
  * 预渲染一份手机可读的纯文本，8:00 的投递任务直接取用
  * 需要更自然的叙述时，才把这份结构化数据交给 WorkBuddy 润色
"""
from __future__ import annotations

from .. import util
from .scheduler import build_brief_payload, render_text  # noqa: F401

KV_KEY = "daily_brief"


def build(repo, cfg: dict) -> dict:
    return build_brief_payload(repo, cfg)


def store_snapshot(repo, snapshot: dict) -> None:
    repo.kv_set(KV_KEY, snapshot)


def latest(repo, cfg: dict = None, rebuild_if_missing: bool = True) -> dict:
    snap = repo.kv_get(KV_KEY)
    if snap:
        return snap
    if rebuild_if_missing:
        snap = build(repo, cfg or {})
        store_snapshot(repo, snap)
    return snap or {}


def text(repo, cfg: dict = None) -> str:
    snap = latest(repo, cfg)
    return snap.get("text") or ""


def headline(repo, cfg: dict = None) -> dict:
    return (latest(repo, cfg) or {}).get("headline") or {}
