"""On-demand root-cause investigation.

This module answers "WHY is this link broken?" — it is deliberately separate
from :mod:`app.diagnostics`, which only answers "which link is broken?".

Two entry surfaces use it, both strictly manual (never polled):

* the frontend Diagnostics page, via ``POST /api/system/diagnostics/investigate``
* the Electron shell, which merges these results with its own local evidence

Design rules (consistent with project conventions):
- Read-only. Never writes business data, never sends email, never mutates config.
- Never returns secrets: no tokens, client_secret, API keys, recipients, bodies.
- Every collector is bounded (timeouts, capped log tails) and isolated: a
  raising collector degrades to ``status=unknown`` instead of aborting the run.
- Only produces ``remedies``. It never executes a repair.
"""
from __future__ import annotations

import ctypes
import os
import shutil
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from sqlalchemy import inspect, text

from .config import _DATA, get_diagnostic_settings, is_gmail_configured, is_llm_configured
from .consumer_status import read_consumer_status
from .diagnostics import run_diagnostics
from .models import Automation, GmailAccount
from .redact import mask_email, redact_text
from .services import flags as flag_svc
from .services.ai_config import peek_email_config
from .services.real_send import is_real_send_enabled

_LOGS = _DATA.logs
_MAX_TAIL_LINES = 40
_MAX_TAIL_BYTES = 64 * 1024
_MAX_EVIDENCE = 40
_PROBE_TIMEOUT_SECONDS = 5
_CONSUMER_STALE_MULTIPLIER = 3

# Paths that keep their meaning in evidence but lose their location.
_SAFE_ROOTS = [str(_DATA.root)]


def _redact(value: Any) -> str:
    return redact_text(value, safe_roots=_SAFE_ROOTS)


# Gmail demo sentinel cursor (see services/sync.py). Present => not a real cursor.
_BAD_HISTORY_IDS = {"", "0", "1000"}


def _ev(source: str, finding: Any) -> dict:
    return {"source": source, "finding": _redact(finding)}


def _remedy(action: str, label: str, risk: str = "low", confirm: bool = True) -> dict:
    return {"action": action, "label": label, "risk": risk, "requires_confirmation": confirm}


def _report(
    target: str,
    status: str,
    root_cause: Optional[str],
    root_cause_label: Optional[str],
    confidence: Optional[str],
    summary: str,
    evidence: list,
    remedies: list,
    next_checks: Optional[list] = None,
) -> dict:
    return {
        "target": target,
        "status": status,
        "root_cause": root_cause,
        "root_cause_label": root_cause_label,
        "confidence": confidence,
        "summary": summary,
        "evidence": evidence,
        "remedies": remedies,
        "next_checks": next_checks or [],
    }


def _tail(filename: str, max_lines: int = _MAX_TAIL_LINES, max_bytes: int = _MAX_TAIL_BYTES) -> list:
    """Read and redact the tail of a log file under the data logs directory."""
    try:
        path = _LOGS / filename
        if not path.exists():
            return []
        size = path.stat().st_size
        with path.open("rb") as handle:
            handle.seek(max(0, size - max_bytes))
            data = handle.read().decode("utf-8", "replace")
        lines = [line.strip() for line in data.splitlines() if line.strip()]
        return [_redact(line) for line in lines[-max_lines:]]
    except Exception:
        return []


def _dir_state(path) -> dict:
    """Read-only directory state. NEVER creates, writes or deletes anything:
    the old ``.ea_write_test`` write probe is deliberately gone. Writability
    is inferred from OS access flags (a heuristic on Windows) and the summary
    text says so."""
    try:
        p = Path(path)
        exists = p.is_dir()
        return {
            "exists": exists,
            "readable": bool(exists and os.access(p, os.R_OK)),
            "writable_hint": bool(exists and os.access(p, os.W_OK)),
        }
    except Exception:
        return {"exists": False, "readable": False, "writable_hint": False}


def _pid_alive(pid: Any) -> Optional[bool]:
    """Existence probe for a process id. True/False, or None when indeterminate.

    ``os.kill(pid, 0)`` is intentionally NOT used on Windows: CPython maps any
    signal there to ``TerminateProcess``, which would kill the very process we
    are diagnosing. Windows uses a query-only ``OpenProcess`` instead; POSIX
    keeps the cheap ``kill(pid, 0)`` existence check.
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    if os.name == "nt":
        try:
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            handle = ctypes.windll.kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION, False, pid
            )
            if not handle:
                return False
            ctypes.windll.kernel32.CloseHandle(handle)
            return True
        except Exception:
            return None
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except Exception:
        return None


def _probe_url(url: str, timeout: int = _PROBE_TIMEOUT_SECONDS) -> tuple:
    try:
        request = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return True, f"HTTP {response.status}"
    except urllib.error.HTTPError as exc:
        # An HTTP status still proves the endpoint is reachable.
        return True, f"HTTP {exc.code}"
    except Exception as exc:
        return False, f"unreachable: {type(exc).__name__}"


def _db_path(db) -> str:
    try:
        url = db.get_bind().url
        if url.get_backend_name() == "sqlite":
            return str(url.database or "")
        return str(url)
    except Exception:
        return ""


def _trace_lines(trace_id: str, max_lines: int = 20) -> list:
    """Collect backend log lines belonging to one request trace."""
    needle = f"tid={trace_id}"
    try:
        lines = _tail("backend.log", max_lines=400, max_bytes=512 * 1024)
        return [line for line in lines if needle in line][-max_lines:]
    except Exception:
        return []


# --------------------------------------------------------------------------
# Collectors
# --------------------------------------------------------------------------
def _investigate_runtime(db, s) -> dict:
    evidence = [_ev("data_dir", f"mode={_DATA.mode}; root={_DATA.root}")]
    states = {k: _dir_state(getattr(_DATA, k)) for k in ("config", "database", "queue", "logs")}
    env_file = _DATA.config / ".env"
    evidence.append(_ev("env_file", "present" if env_file.exists() else "missing"))
    for key, st in states.items():
        if not st["exists"]:
            evidence.append(_ev(f"dir:{key}", "MISSING"))
        elif not st["readable"]:
            evidence.append(_ev(f"dir:{key}", "NOT readable"))
        else:
            evidence.append(_ev(f"dir:{key}", f"ok (writability inferred: {st['writable_hint']}, no write probe)"))
    bad = [k for k, st in states.items() if not st["exists"] or not st["readable"]]
    if bad:
        return _report(
            "runtime.writable", "confirmed", "data_dir_missing_or_unreadable", "数据目录缺失或不可读", "high",
            f"数据目录子项缺失或不可读：{', '.join(bad)}。备份/队列/日志都会失败（诊断为纯读，未做写入探测）。",
            evidence,
            [_remedy("fix_permissions", "以安装时的同一 Windows 用户运行，或修复数据目录权限", "medium")],
            ["确认磁盘未满、未被安全软件拦截"],
        )
    return _report(
        "runtime.writable", "healthy", None, None, "medium",
        "数据目录与全部子目录均存在且可读（可写性为推断值，诊断不执行写入探测）。",
        evidence, [], [],
    )


def _investigate_database(db, s) -> dict:
    evidence = []
    db_file = _db_path(db)
    evidence.append(_ev("db_path", db_file or "unknown"))
    if db_file and db_file.startswith("sqlite:"):
        db_file = db_file.split("sqlite:///", 1)[-1]
    exists = bool(db_file) and os.path.exists(db_file)
    evidence.append(_ev("db_file", "exists" if exists else "missing"))
    if exists:
        try:
            evidence.append(_ev("db_size_bytes", str(os.path.getsize(db_file))))
        except Exception:
            pass
        st = _dir_state(os.path.dirname(db_file) or ".")
        evidence.append(_ev(
            "db_dir",
            "missing" if not st["exists"] else f"readable={st['readable']}; writable_inferred={st['writable_hint']} (no write probe)",
        ))

    if not exists:
        return _report(
            "database.reachable", "confirmed", "db_file_missing", "数据库文件缺失", "high",
            "数据库文件不存在；所有业务数据无法读取。",
            evidence,
            [_remedy("restore_or_migrate", "从备份恢复数据库，或重新初始化后重新连接 Gmail", "high")],
            ["确认数据目录未被迁移或清理"],
        )

    missing = []
    try:
        tables = set(inspect(db.get_bind()).get_table_names())
        core = {"gmail_accounts", "contacts", "campaigns", "approvals"}
        missing = sorted(core - tables)
        evidence.append(_ev("tables", f"{len(tables)} tables; missing core: {missing or 'none'}"))
    except Exception as exc:
        evidence.append(_ev("tables", f"inspect failed: {type(exc).__name__}"))

    try:
        db.execute(text("PRAGMA busy_timeout=3000"))
        row = db.execute(text("PRAGMA quick_check")).fetchone()
        result = str(row[0]) if row else "unknown"
        evidence.append(_ev("pragma_quick_check", result))
        if result.lower() != "ok":
            return _report(
                "database.reachable", "confirmed", "db_integrity_error", "数据库完整性异常", "high",
                "quick_check 未返回 ok，数据库可能损坏。",
                evidence + _tail("backend-error.log", max_lines=15),
                [_remedy("restore_backup", "从最近备份恢复数据库", "high")],
                ["导出可挽救数据后再重建库"],
            )
    except Exception as exc:
        # A check that could not run must never be reported as healthy.
        evidence.append(_ev("pragma_quick_check", f"skipped: {type(exc).__name__}"))
        return _report(
            "database.reachable", "unknown", "db_check_indeterminate", "无法确认数据库完整性", "low",
            "完整性检查未能执行（异常或超时）；无法确认根因，需要重试或查看日志。",
            evidence,
            [],
            ["重试人工诊断；查看 backend-error.log 中的数据库错误"],
        )

    if missing:
        return _report(
            "database.reachable", "confirmed", "missing_tables", "缺少核心数据表", "high",
            f"缺少核心表：{', '.join(missing)}；通常是迁移未执行。",
            evidence,
            [_remedy("run_migrations", "执行后端迁移（增量 ALTER / 建表）", "medium")],
            ["重启后端后复查"],
        )

    return _report("database.reachable", "healthy", None, None, "high", "数据库可连接且结构完整（quick_check=ok）。", evidence, [], [])


def _investigate_consumer(db, s) -> dict:
    status = read_consumer_status()
    state = status.get("state")
    pid = status.get("pid")
    age = status.get("age_seconds")
    evidence = [
        _ev("consumer-status.json", f"state={state}; pid={pid}; heartbeat_age={age}s; healthy={status.get('healthy')}"),
    ]

    alive = _pid_alive(pid)
    if alive is not None:
        evidence.append(_ev("process", f"pid {pid} {'alive' if alive else 'NOT running'}"))
    else:
        evidence.append(_ev("process", "pid existence indeterminate (降级为心跳+日志推断)"))

    queue_db = _DATA.queue / "huey.db"
    evidence.append(_ev("huey.db", "exists" if queue_db.exists() else "missing"))
    st = _dir_state(_DATA.queue)
    evidence.append(_ev("queue_dir", "missing" if not st["exists"] else f"readable={st['readable']}; writable_inferred={st['writable_hint']} (no write probe)"))

    err_tail = _tail("consumer-error.log", max_lines=15)
    for line in err_tail[-5:]:
        evidence.append(_ev("consumer-error.log", line))

    if state == "missing":
        return _report(
            "queue.consumer", "confirmed", "consumer_never_started", "消费进程从未启动", "high",
            "未找到消费心跳文件，说明 Huey 消费进程从未成功写入状态。",
            evidence,
            [_remedy("restart_stack", "重启统一服务（start-stack / Email Automation.exe）", "low")],
            ["确认启动脚本未被安全软件拦截"],
        )
    if alive is False:
        keyword = next((k for k in ("locked", "Traceback", "Error", "error") if any(k in l for l in err_tail)), None)
        return _report(
            "queue.consumer", "confirmed", "consumer_crashed" if keyword else "consumer_process_missing",
            "消费进程已崩溃退出" if keyword else "消费进程已不存在", "high",
            f"心跳记录的 pid {pid} 已不存在" + (f"；错误日志出现关键字 “{keyword}”。" if keyword else "。"),
            evidence,
            [_remedy("restart_stack", "重启统一服务以拉起消费进程", "low")],
            ["consumer-error.log 中查看退出前最后一段堆栈"],
        )
    if not status.get("healthy") and age is not None:
        return _report(
            "queue.consumer", "confirmed", "consumer_heartbeat_stale", "消费心跳过期", "medium",
            f"心跳已 {age}s 未更新；后台任务可能积压。",
            evidence,
            [_remedy("restart_stack", "重启统一服务", "low")],
            ["确认消费进程 CPU 是否被长任务占满"],
        )
    if alive is None and not status.get("healthy"):
        return _report(
            "queue.consumer", "suspected", "consumer_heartbeat_stale", "消费心跳过期（进程存在性未知）", "medium",
            f"无法可靠判定进程存在性；心跳 {age}s 未更新。",
            evidence,
            [_remedy("restart_stack", "重启统一服务", "low")],
            [],
        )
    return _report("queue.consumer", "healthy", None, None, "high", "消费进程存活且心跳新鲜。", evidence, [], [])


def _investigate_gmail(db, s) -> dict:
    evidence = [_ev("oauth_configured", str(bool(is_gmail_configured(s))))]
    account = None
    try:
        account = (
            db.query(GmailAccount)
            .filter(GmailAccount.is_connected == True, GmailAccount.oauth != None)  # noqa: E711
            .first()
        )
    except Exception as exc:
        evidence.append(_ev("query", f"failed: {type(exc).__name__}"))

    if not is_gmail_configured(s):
        return _report(
            "gmail.oauth", "confirmed", "oauth_not_configured", "未配置 Google OAuth", "high",
            "没有 OAuth 客户端配置，无法连接 Gmail。",
            evidence,
            [_remedy("import_credentials", "在「Agent 设置 → Gmail 客户自有 OAuth」导入 credentials.json", "low")],
            [],
        )
    if not account:
        return _report(
            "gmail.oauth", "confirmed", "account_not_connected", "未连接 Gmail 账号", "high",
            "OAuth 已配置，但没有任何已连接的 Gmail 账号。",
            evidence,
            [_remedy("connect_gmail", "点击「连接 Gmail」完成授权", "low")],
            [],
        )

    cred = account.oauth
    now = datetime.now(timezone.utc)
    evidence.append(_ev("account", mask_email(account.email)))
    has_refresh = bool(getattr(cred, "refresh_token_enc", None))
    evidence.append(_ev("refresh_token", "present" if has_refresh else "missing"))
    expiry = getattr(cred, "token_expiry", None)
    if expiry and expiry.tzinfo is None:
        # SQLite drops tzinfo; token_expiry is always stored as UTC.
        expiry = expiry.replace(tzinfo=timezone.utc)
    if expiry:
        secs = (expiry - now).total_seconds()
        evidence.append(_ev("token_expiry", f"in {int(secs)}s" if secs > 0 else f"expired {abs(int(secs))}s ago"))
        if secs <= 0 and not has_refresh:
            return _report(
                "gmail.oauth", "confirmed", "token_expired_no_refresh", "令牌已过期且无刷新令牌", "high",
                "访问令牌已过期且没有刷新令牌，必须重新授权。",
                evidence,
                [_remedy("reauthorize", "断开并重新连接 Gmail 以重新授权", "low")],
                [],
            )
        if secs <= 0:
            return _report(
                "gmail.oauth", "suspected", "token_expired_no_refresh", "令牌已过期（可尝试刷新）", "medium",
                "令牌已过期，但存在刷新令牌；若刷新失败需重新授权。",
                evidence,
                [_remedy("reauthorize", "如刷新持续失败，重新连接 Gmail", "low")],
                ["观察下一次真实调用是否自动刷新成功"],
            )

    history_id = getattr(account, "history_id", None)
    cursor_bad = str(history_id or "").strip() in _BAD_HISTORY_IDS
    evidence.append(_ev("history_cursor", f"{history_id} ({'invalid' if cursor_bad else 'ok'})"))
    if cursor_bad:
        return _report(
            "gmail.oauth", "confirmed", "history_cursor_invalid", "History 游标非法", "high",
            f"游标值为 {history_id!r}，不是合法 Gmail historyId；增量同步必然失败。",
            evidence,
            [_remedy("full_import", "执行一次全量历史导入以重建游标", "medium")],
            [],
        )

    for line in _tail("backend-error.log", max_lines=20)[-3:]:
        if "quota" in line.lower() or "rateLimit" in line:
            evidence.append(_ev("backend-error.log", line))
            return _report(
                "gmail.oauth", "confirmed", "gmail_quota_blocked", "Gmail 配额受限", "medium",
                "日志出现配额/限流错误；同步会被限流。",
                evidence,
                [_remedy("retry_later", "等待配额窗口后重试（约 60s）", "low")],
                [],
            )

    return _report("gmail.oauth", "healthy", None, None, "high", "Gmail 已连接且游标有效。", evidence, [], [])


def _investigate_ai(db, s) -> dict:
    evidence = []
    configured = False
    try:
        configured = bool(peek_email_config(db)) or is_llm_configured(s)
    except Exception as exc:
        evidence.append(_ev("config_lookup", f"failed: {type(exc).__name__}"))
    model = getattr(s, "effective_llm_model", None)
    base = (
        getattr(s, "LLM_BASE_URL", None)
        or getattr(s, "OPENAI_BASE_URL", None)
        or getattr(s, "EMAIL_LLM_BASE_URL", None)
    )
    evidence.append(_ev("model", model or "unset"))
    evidence.append(_ev("base_url", base or "unset"))
    evidence.append(_ev("api_key", "configured" if configured else "missing"))

    if not configured:
        return _report(
            "ai.config", "confirmed", "ai_not_configured", "未配置 AI 模型", "high",
            "没有可用的模型配置；分拣、生成、回复都会失败。",
            evidence,
            [_remedy("configure_ai", "在「Agent 设置」填写 Base URL、模型与 API Key", "low")],
            [],
        )
    if not model:
        return _report(
            "ai.config", "confirmed", "ai_model_missing", "缺少模型名称", "high",
            "已配置凭据但未指定模型。",
            evidence,
            [_remedy("set_model", "在「Agent 设置」填写模型名称", "low")],
            [],
        )
    if base:
        reachable, detail = _probe_url(base.rstrip("/"))
        evidence.append(_ev("base_url_probe", detail))
        if not reachable:
            return _report(
                "ai.config", "suspected", "ai_base_url_unreachable", "模型服务不可达", "medium",
                f"Base URL 探测失败（{detail}）。可能是网络、代理或服务未启动。",
                evidence,
                [_remedy("check_network", "检查网络/代理设置，确认模型服务可访问", "low")],
                ["注意：某些网关不接受根路径 GET，需结合后端日志判断"],
            )
    return _report("ai.config", "healthy", None, None, "medium", "AI 配置存在且服务可达。", evidence, [], [])


def _investigate_scheduler(db, s) -> dict:
    enabled = bool(getattr(s, "ENABLE_SCHEDULER", False))
    interval = int(getattr(s, "POLL_INTERVAL_SECONDS", 60) or 60)
    evidence = [_ev("config", f"ENABLE_SCHEDULER={enabled}; interval={interval}s")]
    heartbeat = _DATA.queue / "scheduler-heartbeat.json"
    age = None
    if heartbeat.exists():
        try:
            import json
            payload = json.loads(heartbeat.read_text(encoding="utf-8"))
            tick = datetime.fromisoformat(payload["tick_at"])
            age = max(0.0, (datetime.now(timezone.utc) - tick).total_seconds())
            evidence.append(_ev("scheduler-heartbeat.json", f"last tick {int(age)}s ago"))
        except Exception as exc:
            evidence.append(_ev("scheduler-heartbeat.json", f"unreadable: {type(exc).__name__}"))
    else:
        evidence.append(_ev("scheduler-heartbeat.json", "missing"))

    if not enabled:
        return _report("scheduler.loop", "healthy", "scheduler_disabled", "调度器已禁用", "high",
                       "调度器被显式关闭，自动任务不会执行（属预期配置）。", evidence, [], [])
    if age is None:
        return _report(
            "scheduler.loop", "confirmed", "scheduler_never_ticked", "调度器从未写入心跳", "high",
            "调度器已启用但没有任何心跳，线程可能未启动。",
            evidence + _tail("backend-error.log", max_lines=10),
            [_remedy("restart_backend", "重启后端以重建调度线程", "low")],
            [],
        )
    if age > interval * _CONSUMER_STALE_MULTIPLIER:
        return _report(
            "scheduler.loop", "confirmed", "scheduler_stale", "调度心跳过期", "high",
            f"心跳 {int(age)}s 未更新（间隔 {interval}s），调度线程可能已卡死或退出。",
            evidence + _tail("backend-error.log", max_lines=10),
            [_remedy("restart_backend", "重启后端以重建调度线程", "low")],
            [],
        )
    return _report("scheduler.loop", "healthy", None, None, "high", f"调度器正常心跳（{int(age)}s 前）。", evidence, [], [])


def _investigate_automations(db, s) -> dict:
    evidence = []
    try:
        autos = db.query(Automation).filter(Automation.status == "enabled").all()
        now = datetime.now(timezone.utc)
        overdue = [a.id for a in autos if a.next_run_at and (a.next_run_at - now) < -timedelta(seconds=300)]
        evidence.append(_ev("automations", f"{len(autos)} enabled; {len(overdue)} overdue ({overdue[:5]})"))
        if overdue:
            heartbeat = _DATA.queue / "scheduler-heartbeat.json"
            if not heartbeat.exists():
                return _report(
                    "automations.stuck", "confirmed", "automation_overdue_scheduler_dead", "自动化逾期且无调度心跳", "high",
                    "存在逾期未执行的自动化，且调度器没有心跳。",
                    evidence,
                    [_remedy("restart_backend", "重启后端以恢复调度", "low")],
                    [],
                )
            return _report(
                "automations.stuck", "suspected", "automation_overdue_scheduler_dead", "自动化逾期", "medium",
                "存在逾期未执行的自动化，请结合调度心跳判断。",
                evidence,
                [_remedy("check_scheduler", "先诊断 scheduler.loop", "low")],
                [],
            )
    except Exception as exc:
        evidence.append(_ev("query", f"failed: {type(exc).__name__}"))
        return _report("automations.stuck", "unknown", None, None, "low", "无法查询自动化状态。", evidence, [], [])
    return _report("automations.stuck", "healthy", None, None, "high", "没有逾期未执行的自动化。", evidence, [], [])


def _investigate_tacwork(db, s) -> dict:
    evidence = []
    try:
        enabled = flag_svc.is_agent_takeover_enabled(db)
    except Exception as exc:
        enabled = False
        evidence.append(_ev("takeover_flag", f"lookup failed: {type(exc).__name__}"))
    evidence.append(_ev("takeover_enabled", str(bool(enabled))))
    if not enabled:
        return _report("tacwork.connection", "healthy", "tacwork_disabled", "Agent Takeover 已关闭", "high",
                       "未启用接管，TACWork 不参与运行（属预期配置）。", evidence, [], [])
    url = f"{str(getattr(s, 'TACWORK_SERVER_URL', '')).rstrip('/')}/health"
    reachable, detail = _probe_url(url)
    evidence.append(_ev("tacwork_server_probe", f"{url} -> {detail}"))
    if not reachable:
        for line in _tail("tacwork-server-error.log", max_lines=10)[-3:]:
            evidence.append(_ev("tacwork-server-error.log", line))
        return _report(
            "tacwork.connection", "confirmed", "tacwork_server_unreachable", "TACWork 服务不可达", "high",
            f"健康探测失败（{detail}）。",
            evidence,
            [_remedy("restart_stack", "重启统一服务以拉起 TACWork", "low")],
            ["确认 18002/18003 端口未被占用"],
        )
    return _report("tacwork.connection", "healthy", None, None, "high", "TACWork 服务可达。", evidence, [], [])


def _investigate_knowledge(db, s) -> dict:
    evidence = []
    try:
        from .knowledge import retrieve_knowledge

        result = retrieve_knowledge("__diagnostic_probe__", limit=1)
        ok = isinstance(result, (list, tuple))
        evidence.append(_ev("retriever", "available" if ok else f"unexpected type {type(result).__name__}"))
        if ok:
            return _report("knowledge.base", "healthy", None, None, "high", "知识检索器可用。", evidence, [], [])
        return _report("knowledge.base", "suspected", "kb_empty", "知识检索返回异常类型", "medium",
                       "检索器返回了非预期类型。", evidence, [_remedy("check_kb", "检查知识库模块与数据", "low")], [])
    except Exception as exc:
        evidence.append(_ev("retriever", f"{type(exc).__name__}: {str(exc)[:120]}"))
        return _report(
            "knowledge.base", "confirmed", "kb_retriever_error", "知识检索器异常", "high",
            f"检索器抛出异常：{type(exc).__name__}。",
            evidence,
            [_remedy("check_kb", "检查知识库依赖与数据目录", "low")],
            ["回复策略会退化为默认模板"],
        )


def _investigate_sending(db, s) -> dict:
    evidence = []
    account = None
    try:
        account = db.query(GmailAccount).filter(GmailAccount.is_connected == True).first()  # noqa: E711
    except Exception:
        pass
    real_send = is_real_send_enabled(s, account, account.oauth if account else None) if account else False
    allowlist = getattr(s, "recipient_allowlist", None) or []
    evidence.append(_ev("real_send", str(bool(real_send))))
    # An empty RESTRICTED_RECIPIENT_ALLOWLIST is a supported configuration:
    # the policy engine only restricts recipients when the allowlist is set.
    evidence.append(_ev("allowlist", f"RESTRICTED_RECIPIENT_ALLOWLIST: {len(allowlist)} address(es)"))
    evidence.append(_ev("connected_account", mask_email(account.email) if account else "none"))
    if not account:
        return _report("sending.safety", "confirmed", "no_connected_account", "无已连接发件账号", "high",
                       "没有已连接的 Gmail 账号，无法发送。", evidence,
                       [_remedy("connect_gmail", "连接 Gmail 账号", "low")], [])
    return _report("sending.safety", "healthy", None, None, "high",
                   "发送配置正常（真实发送关闭，或白名单已按 RESTRICTED_RECIPIENT_ALLOWLIST 配置/留空均属允许状态）。",
                   evidence, [], [])


def _investigate_disk(db, s) -> dict:
    evidence = []
    try:
        usage = shutil.disk_usage(str(_DATA.root))
        free_gib = usage.free / 1024**3
        evidence.append(_ev("free", f"{free_gib:.2f} GiB on {_DATA.root}"))
        try:
            biggest = sorted(
                ((p.stat().st_size, p.name) for p in _LOGS.glob("*.log") if p.is_file()),
                reverse=True,
            )[:3]
            for size, name in biggest:
                evidence.append(_ev("largest_log", f"{name}: {size // 1024} KiB"))
        except Exception:
            pass
        if usage.free < 500 * 1024**2:
            return _report("disk.space", "confirmed", "disk_critical", "磁盘空间严重不足", "high",
                           f"仅剩 {free_gib:.2f} GiB，数据库与日志将停止写入。", evidence,
                           [_remedy("free_space", "清理磁盘空间", "high")], [])
        if usage.free < 2 * 1024**3:
            return _report("disk.space", "confirmed", "disk_low", "磁盘空间偏低", "high",
                           f"仅剩 {free_gib:.2f} GiB。", evidence,
                           [_remedy("free_space", "清理磁盘或归档旧日志", "medium")], [])
        return _report("disk.space", "healthy", None, None, "high", f"磁盘空间充足（{free_gib:.2f} GiB）。", evidence, [], [])
    except Exception as exc:
        return _report("disk.space", "unknown", None, None, "low", f"无法读取磁盘信息：{type(exc).__name__}", evidence, [], [])


_COLLECTORS = {
    "runtime.writable": _investigate_runtime,
    "database.reachable": _investigate_database,
    "queue.consumer": _investigate_consumer,
    "gmail.oauth": _investigate_gmail,
    "ai.config": _investigate_ai,
    "scheduler.loop": _investigate_scheduler,
    "automations.stuck": _investigate_automations,
    "tacwork.connection": _investigate_tacwork,
    "knowledge.base": _investigate_knowledge,
    "sending.safety": _investigate_sending,
    "disk.space": _investigate_disk,
}


def investigate(db, target: str = "system", trace_id: Optional[str] = None, incident_id: Optional[str] = None) -> dict:
    """Run an on-demand root-cause investigation.

    ``target`` is either ``"system"`` (investigate every link the health
    overview flags as error/warn) or one of the 11 diagnostic item ids.
    """
    now = datetime.now(timezone.utc)
    incident = incident_id or f"inc-{now.strftime('%Y%m%d-%H%M%S')}"

    if target in (None, "", "system", "all"):
        overview_error = None
        try:
            overview = run_diagnostics(db)
        except Exception as exc:
            overview = None
            overview_error = str(exc)[:200]
        # An overview that failed (or self-reported unknown) must NEVER be
        # presented as "all healthy" — degrade to an explicit unknown.
        if overview is None or overview.get("error") or overview.get("overall") == "unknown":
            return {
                "incident_id": incident,
                "target": "system",
                "generated_at": now.isoformat(),
                "trace_id": trace_id,
                "backend_up": True,
                "reports": [
                    _report(
                        "system", "unknown", "overview_failed", "总览诊断执行失败", "low",
                        "总览诊断未能完成，无法确认根因，需要重试或查看日志。",
                        [_ev("diagnostics.overview", overview_error or "run_diagnostics returned overall=unknown")],
                        [],
                        ["重试人工诊断", "查看 backend-error.log 获取总览失败原因"],
                    )
                ],
            }
        flagged = [i.get("id") for i in overview.get("items", []) if i.get("status") in ("error", "warn")]
        if not flagged:
            return {
                "incident_id": incident,
                "target": "system",
                "generated_at": now.isoformat(),
                "trace_id": trace_id,
                "backend_up": True,
                "reports": [
                    _report(
                        "system", "healthy", None, None, "high",
                        "总览未发现 error/warn 环节，无需根因调查。",
                        [_ev("diagnostics.overview", f"overall={overview.get('overall')}")],
                        [], [],
                    )
                ],
            }
        targets = flagged
    else:
        targets = [target]

    settings = get_diagnostic_settings()
    reports = []
    for name in targets:
        collector = _COLLECTORS.get(name)
        if collector is None:
            reports.append(
                _report(
                    name, "unknown", None, None, "low",
                    f"未知的诊断目标：{name}",
                    [_ev("targets", ", ".join(sorted(_COLLECTORS)) )],
                    [], [],
                )
            )
            continue
        try:
            reports.append(collector(db, settings))
        except Exception as exc:
            reports.append(
                _report(
                    name, "unknown", None, None, "low",
                    f"调查器异常：{type(exc).__name__}",
                    [_ev("investigator", str(exc)[:200])],
                    [],
                    ["查看 backend-error.log 获取完整堆栈"],
                )
            )

    if trace_id:
        for line in _trace_lines(trace_id):
            reports[0]["evidence"].append(_ev("backend.log", line))

    # Hard cap: no report may carry an unbounded evidence list.
    for report in reports:
        report["evidence"] = report["evidence"][:_MAX_EVIDENCE]

    return {
        "incident_id": incident,
        "target": target,
        "generated_at": now.isoformat(),
        "trace_id": trace_id,
        "backend_up": True,
        "reports": reports,
    }
