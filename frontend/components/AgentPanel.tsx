"use client";

import { Bot, ExternalLink, PanelRightClose, PanelRightOpen, Plus, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api, friendlyError } from "@/lib/api";
import { useLang } from "@/lib/i18n";

const COMPACT_BREAKPOINT = 1280;

type AgentSurface = {
  type: "embedded_url" | "event_stream" | "external_url" | "unavailable";
  url?: string;
  new_session_url?: string;
  external_url?: string;
  reason?: string;
};

type AgentProvider = {
  id: string;
  display_name: string;
  configured: boolean;
  capabilities: string[];
  surface?: AgentSurface;
};

type ProviderCatalog = {
  selected_provider: string;
  providers: AgentProvider[];
};

function localClock() {
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "local";
  const value = new Intl.DateTimeFormat(undefined, {
    hour: "2-digit", minute: "2-digit", timeZoneName: "short",
  }).format(new Date());
  return { timezone, value };
}

export default function AgentPanel() {
  const { lang } = useLang();
  const zh = lang === "zh";
  const [open, setOpen] = useState(false);
  const [compact, setCompact] = useState(true);
  const [catalog, setCatalog] = useState<ProviderCatalog | null>(null);
  const [surface, setSurface] = useState<AgentSurface>({ type: "unavailable", reason: "loading" });
  const [loaded, setLoaded] = useState(false);
  const [frameKey, setFrameKey] = useState(0);
  const [error, setError] = useState("");
  const [events, setEvents] = useState<string[]>([]);
  const [clock, setClock] = useState(localClock());
  const previousCompactRef = useRef<boolean | null>(null);

  const selected = useMemo(
    () => catalog?.providers.find((provider) => provider.id === catalog.selected_provider) ?? null,
    [catalog],
  );

  const refresh = useCallback(async () => {
    try {
      const next = await api.agentProviders() as ProviderCatalog;
      const provider = next.providers.find((item) => item.id === next.selected_provider);
      setCatalog(next);
      setSurface(provider?.surface ?? { type: "unavailable", reason: "agent_provider_surface_unavailable" });
      setError("");
    } catch (cause) {
      setError(friendlyError(cause, zh));
      setSurface({ type: "unavailable", reason: "agent_provider_unreachable" });
    }
  }, [zh]);

  useEffect(() => {
    const syncLayout = () => {
      const nextCompact = window.innerWidth < COMPACT_BREAKPOINT;
      setCompact(nextCompact);
      if (previousCompactRef.current === null || previousCompactRef.current !== nextCompact) {
        setOpen(!nextCompact);
        previousCompactRef.current = nextCompact;
      }
    };
    syncLayout();
    window.addEventListener("resize", syncLayout);
    return () => window.removeEventListener("resize", syncLayout);
  }, []);

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 15_000);
    return () => window.clearInterval(timer);
  }, [refresh]);

  useEffect(() => {
    const timer = window.setInterval(() => setClock(localClock()), 30_000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    setLoaded(false);
    setEvents([]);
    if (surface.type !== "event_stream" || !surface.url) return;
    const stream = new EventSource(surface.url);
    stream.onopen = () => setLoaded(true);
    stream.onmessage = (event) => setEvents((current) => [...current.slice(-99), event.data]);
    stream.onerror = () => setError(zh ? "Agent 事件流已断开" : "Agent event stream disconnected");
    return () => stream.close();
  }, [surface.type, surface.url, zh]);

  const switchProvider = async (providerId: string) => {
    try {
      const next = await api.selectAgentProvider(providerId) as ProviderCatalog;
      const provider = next.providers.find((item) => item.id === next.selected_provider);
      setCatalog(next);
      setSurface(provider?.surface ?? { type: "unavailable", reason: "agent_provider_surface_unavailable" });
      setLoaded(false);
      setFrameKey((value) => value + 1);
      setError("");
    } catch (cause) {
      setError(friendlyError(cause, zh));
    }
  };

  const openExternal = () => {
    const url = surface.external_url || surface.url;
    if (url) window.open(url, "_blank", "noopener,noreferrer");
  };

  const newSession = () => {
    if (!surface.new_session_url) return;
    setSurface({ ...surface, url: surface.new_session_url, external_url: surface.new_session_url });
    setLoaded(false);
    setFrameKey((value) => value + 1);
  };

  return (
    <>
      {compact && open ? (
        <button type="button" className="fixed inset-0 z-30 bg-black/55 backdrop-blur-[1px]" onClick={() => setOpen(false)} aria-label={zh ? "关闭 Agent" : "Close Agent"} />
      ) : null}
      <aside className={`${compact ? "fixed inset-y-0 right-0 z-40 shadow-2xl" : "relative z-0 shadow-none"} h-full shrink-0 border-l border-border bg-panel flex flex-col transition-[width] duration-200 ${open ? (compact ? "w-[min(94vw,720px)] sm:w-[min(88vw,720px)]" : "w-[clamp(420px,42vw,680px)]") : "w-12 items-center py-3"}`}>
        {!open ? <button type="button" onClick={() => setOpen(true)} className="btn h-9 w-9 p-0 flex items-center justify-center" title={zh ? "打开 Agent" : "Open Agent"}><PanelRightOpen size={16} /></button> : null}
        {!open ? <div className="mt-3 flex flex-col items-center gap-2 text-muted" aria-hidden><img src="/brand/agent-avatar.png" alt="" className="h-5 w-5 rounded-full object-cover" /><span className="text-[10px] tracking-[0.18em] [writing-mode:vertical-rl]">AGENT NATIVE</span></div> : null}

        <div className={`${open ? "flex" : "hidden"} min-h-14 shrink-0 border-b border-border px-3 py-2 items-center gap-2`}>
          <img src="/brand/agent-avatar.png" alt="" className="h-8 w-8 shrink-0 rounded-full object-cover" />
          <div className="min-w-0 flex-1"><div className="text-sm font-semibold">{selected?.display_name || "Agent"}</div><div className="text-[10px] text-muted truncate">{zh ? "锁定 Email Automation Workspace" : "Locked to Email Automation Workspace"}</div><div className="text-[10px] text-muted truncate" title={clock.timezone}>{zh ? `本机时间 ${clock.value} · ${clock.timezone}` : `Local ${clock.value} · ${clock.timezone}`}</div></div>
          <select className="input h-8 max-w-40 py-0 text-xs" value={catalog?.selected_provider || "tacwork"} onChange={(event) => void switchProvider(event.target.value)} aria-label={zh ? "选择 Agent Provider" : "Select Agent Provider"}>
            {(catalog?.providers ?? []).map((provider) => <option key={provider.id} value={provider.id} disabled={!provider.configured}>{provider.display_name}{provider.configured ? "" : zh ? "（未配置）" : " (not configured)"}</option>)}
          </select>
          <span className={`h-2 w-2 rounded-full ${loaded ? "bg-success" : "bg-warning animate-pulse"}`} />
          {surface.new_session_url ? <button type="button" onClick={newSession} className="btn h-8 w-8 p-0 flex items-center justify-center" title={zh ? "新建对话" : "New conversation"}><Plus size={15} /></button> : null}
          <button type="button" onClick={() => { setLoaded(false); setFrameKey((value) => value + 1); void refresh(); }} className="btn h-8 w-8 p-0 flex items-center justify-center" title={zh ? "刷新 Agent" : "Refresh Agent"}><RefreshCw size={14} /></button>
          {(surface.external_url || surface.url) && surface.type !== "event_stream" ? <button type="button" onClick={openExternal} className="btn h-8 w-8 p-0 flex items-center justify-center" title={zh ? "独立窗口打开" : "Open in a new window"}><ExternalLink size={14} /></button> : null}
          <button type="button" onClick={() => setOpen(false)} className="btn h-8 w-8 p-0 flex items-center justify-center" title={zh ? "收起 Agent" : "Collapse Agent"}><PanelRightClose size={14} /></button>
        </div>

        <div className={`${open ? "block" : "hidden"} relative flex-1 min-h-0 bg-background w-full`}>
          {error ? <div className="border-b border-danger/40 bg-danger/10 px-3 py-2 text-xs text-danger">{error}</div> : null}
          {surface.type === "embedded_url" && surface.url ? <iframe key={frameKey} src={surface.url} title={`${selected?.display_name || "Agent"} Agent`} className="h-full w-full border-0 bg-background" onLoad={() => setLoaded(true)} allow="clipboard-read; clipboard-write" referrerPolicy="no-referrer" /> : null}
          {surface.type === "event_stream" ? <div className="h-full overflow-auto p-4 font-mono text-xs whitespace-pre-wrap">{events.length ? events.join("\n\n") : (zh ? "等待 Agent 事件…" : "Waiting for Agent events…")}</div> : null}
          {surface.type === "external_url" ? <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 px-6 text-center text-muted"><Bot size={28} /><p>{zh ? "该 Agent 需要在独立窗口使用。" : "This Agent opens in a separate window."}</p><button type="button" className="btn" onClick={openExternal}>{zh ? "打开 Agent" : "Open Agent"}</button></div> : null}
          {surface.type === "unavailable" ? <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 px-6 text-center text-muted"><Bot size={28} className="text-warning" /><p>{zh ? "当前 Agent 界面不可用" : "The selected Agent surface is unavailable"}</p><code className="text-[11px]">{surface.reason || "agent_provider_surface_unavailable"}</code><button type="button" className="btn" onClick={() => void refresh()}>{zh ? "重试" : "Retry"}</button></div> : null}
        </div>
      </aside>
    </>
  );
}
