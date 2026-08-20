"use client";

import { Bot, Clock, Loader2, Power, ShieldCheck } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { api } from "@/lib/api";
import { useLang } from "@/lib/i18n";

const QUICK_INTERVALS = [1, 15, 30, 60, 120, 240, 1440];
const MIN_INTERVAL_MINUTES = 1;
const MAX_INTERVAL_MINUTES = 1440;

function localDateTime(value: string | null | undefined) {
  if (!value) return "-";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "-";
  return new Intl.DateTimeFormat(undefined, {
    month: "short", day: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit", timeZoneName: "short",
  }).format(date);
}

function computerTimezone() {
  return Intl.DateTimeFormat().resolvedOptions().timeZone || "UTC";
}

export default function AgentTakeoverControl({ paused, onChanged }: { paused: boolean; onChanged: () => void }) {
  const { lang } = useLang();
  const zh = lang === "zh";
  const [state, setState] = useState<any>(null);
  const [intervalInput, setIntervalInput] = useState("60");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [, setClockTick] = useState(0);
  const syncedTimezone = useRef("");

  const load = async () => {
    try {
      const timezone = computerTimezone();
      const value = syncedTimezone.current !== timezone
        ? await api.updateWorkspaceDisplayTimezone(timezone)
        : await api.agentTakeover();
      syncedTimezone.current = timezone;
      setState(value);
      setIntervalInput(String(value.interval_minutes || 60));
      setError("");
    } catch (e: any) {
      setError(e?.message || (zh ? "无法读取接管状态" : "Unable to load takeover status"));
    }
  };

  useEffect(() => {
    void load();
    const statusTimer = window.setInterval(() => void load(), 15_000);
    const clockTimer = window.setInterval(() => setClockTick((value) => value + 1), 30_000);
    return () => { window.clearInterval(statusTimer); window.clearInterval(clockTimer); };
  }, []);

  const toggle = async () => {
    const enabled = !state?.enabled;
    if (enabled && !window.confirm(zh
      ? "开启 Agent 接管后，TACWork 将按计划自动处理并真实发送符合安全规则的客户回复；需要人工审核的项目仍会跳过。确认开启？"
      : "Agent Takeover will let TACWork process and really send eligible customer replies on schedule. Human-review items remain blocked. Enable it?")) return;
    const minutes = parseInterval(intervalInput);
    if (minutes === null) return;
    setBusy(true); setError("");
    try {
      const value = await api.updateAgentTakeover(enabled, minutes, computerTimezone());
      setState(value);
      setIntervalInput(String(value.interval_minutes));
      syncedTimezone.current = computerTimezone();
      onChanged();
    } catch (e: any) {
      setError(e?.message || (zh ? "更新失败" : "Update failed"));
    } finally { setBusy(false); }
  };

  const parseInterval = (value: string): number | null => {
    if (!/^\d+$/.test(value)) {
      setError(zh ? "请输入 1–1440 的整数分钟数" : "Enter a whole number of minutes from 1 to 1440.");
      return null;
    }
    const minutes = Number(value);
    if (!Number.isInteger(minutes) || minutes < MIN_INTERVAL_MINUTES || minutes > MAX_INTERVAL_MINUTES) {
      setError(zh ? "分钟数必须在 1–1440 之间" : "Minutes must be between 1 and 1440.");
      return null;
    }
    return minutes;
  };

  const saveInterval = async (value = intervalInput) => {
    const minutes = parseInterval(value);
    if (minutes === null || busy || !state) return;
    setBusy(true); setError("");
    try {
      const updated = await api.updateAgentTakeover(Boolean(state.enabled), minutes, computerTimezone());
      setState(updated);
      setIntervalInput(String(updated.interval_minutes));
      syncedTimezone.current = computerTimezone();
      onChanged();
    } catch (e: any) {
      setError(e?.message || (zh ? "更新频率失败" : "Failed to update interval"));
      await load();
    } finally { setBusy(false); }
  };

  const permissions = useMemo(() => state?.enabled
    ? (zh ? "Agent Review · 自动发送 · 定时唤醒 · Inbox 运营" : "Agent Review · Auto-send · Scheduled wake-up · Inbox operations")
    : (zh ? "常规写操作需要人工授权" : "Routine writes require user authorization"), [state?.enabled, zh]);
  const running = state?.last_status === "running" || String(state?.current_stage || "").includes("started") || state?.current_stage === "agent_running";

  return (
    <section className={`mb-2 rounded-lg border p-2 lg:p-3 ${state?.enabled ? "border-accent/50 bg-accent/5" : "border-border bg-panel2/40"}`}>
      <div className="flex items-center justify-center lg:justify-between gap-2">
        <div className="hidden lg:flex items-center gap-2 min-w-0">
          <Bot size={15} className={state?.enabled ? "text-accent" : "text-muted"} />
          <div className="min-w-0"><div className="text-xs font-semibold">{zh ? "Agent 接管" : "Agent Takeover"}</div><div className={`text-[10px] ${state?.enabled ? "text-ok" : "text-muted"}`}>{state?.enabled ? (zh ? "已开启" : "Enabled") : (zh ? "已关闭" : "Disabled")}</div></div>
        </div>
        <button type="button" onClick={toggle} disabled={busy || !state} className={`h-8 w-8 lg:w-auto lg:px-2 rounded-md flex items-center justify-center gap-1 text-xs ${state?.enabled ? "bg-accent text-white" : "btn"}`} title={zh ? "Agent 接管" : "Agent Takeover"}>
          {busy ? <Loader2 size={14} className="animate-spin" /> : <Power size={14} />}<span className="hidden lg:inline">{state?.enabled ? (zh ? "关闭" : "Turn off") : (zh ? "开启" : "Enable")}</span>
        </button>
      </div>
      <div className="hidden lg:block mt-2 space-y-2">
        <label className="block text-[10px] text-muted" htmlFor="agent-takeover-interval">{zh ? "调度间隔（分钟）" : "Schedule interval (minutes)"}</label>
        <div className="flex gap-1">
          <input id="agent-takeover-interval" className="input min-w-0 flex-1 text-xs py-1" type="text" inputMode="numeric" pattern="[0-9]*" value={intervalInput} disabled={busy} onChange={(e) => { setIntervalInput(e.target.value); setError(""); }} onBlur={() => void saveInterval()} onKeyDown={(e) => { if (e.key === "Enter") { e.currentTarget.blur(); } }} aria-label={zh ? "接管频率分钟数" : "Takeover interval in minutes"} />
          <span className="self-center text-[10px] text-muted">{zh ? "分钟" : "min"}</span>
        </div>
        <div className="flex flex-wrap gap-1" aria-label={zh ? "常用分钟数" : "Quick intervals"}>
          {QUICK_INTERVALS.map((minutes) => <button type="button" key={minutes} disabled={busy} className="rounded border border-border px-1.5 py-0.5 text-[9px] text-muted hover:text-fg" onClick={() => { setIntervalInput(String(minutes)); void saveInterval(String(minutes)); }}>{minutes}</button>)}
        </div>
        <div className="text-[10px] text-muted space-y-1">
          <div className="flex items-center gap-1"><Clock size={10} /><span>{zh ? "下次" : "Next"}: {state?.display_time?.next_run_at?.local || localDateTime(state?.next_run_at)}</span></div>
          <div>{zh ? "状态" : "Status"}: {paused ? (zh ? "已全局暂停" : "Globally paused") : running ? (zh ? "运行中" : "Running") : (state?.last_status || (zh ? "空闲" : "Idle"))}</div>
          {state?.current_stage && <div className="truncate" title={state.current_stage}>{zh ? "阶段" : "Stage"}: {state.current_stage}</div>}
          {state?.last_error && <div className="text-danger line-clamp-2" title={state.last_error}>{state.last_error}</div>}
        </div>
        <div className="flex items-start gap-1 text-[9px] leading-3 text-muted" title={permissions}><ShieldCheck size={10} className="mt-0.5 shrink-0" /><span>{permissions}</span></div>
      </div>
      {error && <div className="hidden lg:block text-[10px] text-danger mt-2 break-words">{error}</div>}
    </section>
  );
}
