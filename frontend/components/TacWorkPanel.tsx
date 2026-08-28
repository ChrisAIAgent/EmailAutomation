"use client";

import { Bot, ExternalLink, PanelRightClose, PanelRightOpen, Plus, RefreshCw } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useLang } from "@/lib/i18n";

const runtimeConfig = typeof window !== "undefined" ? window.__EMAIL_AUTOMATION_RUNTIME__ : undefined;
const TACWORK_URL = runtimeConfig?.tacworkUrl || process.env.NEXT_PUBLIC_TACWORK_URL || "http://127.0.0.1:18003";
const TACWORK_SERVER_URL = runtimeConfig?.tacworkServerUrl || process.env.NEXT_PUBLIC_TACWORK_SERVER_URL || "http://127.0.0.1:18002";
// Fixed local loopback service identifier exchanged between this Email Automation
// host and the co-located TACWork server. It is NOT a secret and is intentionally
// not configurable as a NEXT_PUBLIC_* value (those ship in browser bundles).
// A backend proxy for TACWork status/session is planned to remove this entirely.
const TACWORK_CLIENT_TOKEN = "email-automation-local-v1";
const COMPACT_BREAKPOINT = 1280;
const LAST_SESSION_STORAGE_KEY = "email-automation.tacwork.last-session-url";

type TacWorkSession = {
  id: string;
  parentID?: string | null;
  time?: { created?: number; updated?: number; archived?: number };
};

const sessionTimestamp = (session: TacWorkSession) => session.time?.updated ?? session.time?.created ?? 0;
const wait = (milliseconds: number) => new Promise((resolve) => window.setTimeout(resolve, milliseconds));

function localClock() {
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone || "local";
  const value = new Intl.DateTimeFormat(undefined, {
    hour: "2-digit", minute: "2-digit", timeZoneName: "short",
  }).format(new Date());
  return { timezone, value };
}

export default function TacWorkPanel() {
  const { lang } = useLang();
  const zh = lang === "zh";
  const [open, setOpen] = useState(false);
  const [compact, setCompact] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [frameKey, setFrameKey] = useState(0);
  const [agentUrl, setAgentUrl] = useState<string | null>(null);
  const [newSessionUrl, setNewSessionUrl] = useState<string | null>(null);
  const [connectionError, setConnectionError] = useState(false);
  const [clock, setClock] = useState<{ timezone: string; value: string } | null>(null);
  const previousCompactRef = useRef<boolean | null>(null);
  const agentUrlRef = useRef<string | null>(null);

  useEffect(() => { agentUrlRef.current = agentUrl; }, [agentUrl]);

  useEffect(() => {
    const refreshClock = () => setClock(localClock());
    refreshClock();
    const timer = window.setInterval(refreshClock, 30_000);
    return () => window.clearInterval(timer);
  }, []);

  const restoreLatestSession = useCallback(async () => {
    setConnectionError(false);
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        const statusResponse = await fetch(`${TACWORK_SERVER_URL}/status`, {
          headers: { Authorization: `Bearer ${TACWORK_CLIENT_TOKEN}` },
        });
        if (!statusResponse.ok) throw new Error(`TACWork status ${statusResponse.status}`);
        const status = await statusResponse.json() as { activeWorkspaceId?: string };
        if (!status.activeWorkspaceId) throw new Error("TACWork has no active workspace");

        const workspaceUrl = `${TACWORK_URL}/workspace/${encodeURIComponent(status.activeWorkspaceId)}/session`;
        const sessionsResponse = await fetch(
          `${TACWORK_SERVER_URL}/workspace/${encodeURIComponent(status.activeWorkspaceId)}/sessions?roots=true&limit=200`,
          { headers: { Authorization: `Bearer ${TACWORK_CLIENT_TOKEN}` } },
        );
        if (!sessionsResponse.ok) throw new Error(`TACWork sessions ${sessionsResponse.status}`);
        const payload = await sessionsResponse.json() as { items?: TacWorkSession[] };
        const latest = (payload.items ?? [])
          .filter((session) => session.id && !session.parentID && !session.time?.archived)
          .sort((left, right) => sessionTimestamp(right) - sessionTimestamp(left))[0];

        setNewSessionUrl(workspaceUrl);
        const resolvedUrl = latest ? `${workspaceUrl}/${encodeURIComponent(latest.id)}` : workspaceUrl;
        if (agentUrlRef.current === resolvedUrl) return;
        setLoaded(false);
        setAgentUrl(resolvedUrl);
        if (latest) window.localStorage.setItem(LAST_SESSION_STORAGE_KEY, resolvedUrl);
        setFrameKey((value) => value + 1);
        return;
      } catch {
        if (attempt < 2) await wait(500 * (attempt + 1));
      }
    }
    setConnectionError(true);
  }, []);

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
    try {
      const cachedUrl = window.localStorage.getItem(LAST_SESSION_STORAGE_KEY);
      if (cachedUrl?.startsWith(`${TACWORK_URL}/workspace/`) && cachedUrl.includes("/session/")) {
        setAgentUrl(cachedUrl);
      }
    } catch {
      // Storage may be unavailable in privacy-restricted browser contexts.
    }
    void restoreLatestSession();
    const timer = window.setInterval(() => void restoreLatestSession(), 15_000);
    return () => window.clearInterval(timer);
  }, [restoreLatestSession]);

  const reload = () => {
    void restoreLatestSession();
  };

  const createNewSession = () => {
    setLoaded(false);
    setConnectionError(false);
    if (!newSessionUrl) return;
    setAgentUrl(newSessionUrl);
    setFrameKey((value) => value + 1);
  };

  return (
    <>
      {compact && open ? (
        <button
          type="button"
          className="fixed inset-0 z-30 bg-black/55 backdrop-blur-[1px]"
          onClick={() => setOpen(false)}
          aria-label={zh ? "关闭 TACWork Agent" : "Close TACWork Agent"}
        />
      ) : null}
      <aside
        className={`${compact ? "fixed inset-y-0 right-0 z-40 shadow-2xl" : "relative z-0 shadow-none"} h-full shrink-0 border-l border-border bg-panel flex flex-col transition-[width] duration-200 ${
          open
            ? compact
              ? "w-[min(94vw,720px)] sm:w-[min(88vw,720px)]"
              : "w-[clamp(420px,42vw,680px)]"
            : "w-12 items-center py-3"
        }`}
      >
      {!open ? (
        <button
          type="button"
          onClick={() => setOpen(true)}
          className="btn h-9 w-9 p-0 flex items-center justify-center"
          title={zh ? "打开 TACWork Agent" : "Open TACWork Agent"}
          aria-label={zh ? "打开 TACWork Agent" : "Open TACWork Agent"}
        >
          <PanelRightOpen size={16} />
        </button>
      ) : null}
      {!open ? (
        <div className="mt-3 flex flex-col items-center gap-2 text-muted" aria-hidden>
          <Bot size={17} className="text-accent" />
          <span className="text-[10px] tracking-[0.18em] [writing-mode:vertical-rl]">AGENT NATIVE</span>
        </div>
      ) : null}
      <div className={`${open ? "flex" : "hidden"} h-14 shrink-0 border-b border-border px-3 items-center gap-2`}>
        <div className="h-8 w-8 rounded-lg bg-accent/10 text-accent flex items-center justify-center">
          <Bot size={17} />
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold">TACWork Agent</div>
          <div className="text-[10px] text-muted truncate">
            {zh ? "锁定 Email Automation Workspace" : "Locked to Email Automation Workspace"}
          </div>
          {clock ? (
            <div className="text-[10px] text-muted truncate" title={clock.timezone}>
              {zh ? `本机时间 ${clock.value} · ${clock.timezone}` : `Local ${clock.value} · ${clock.timezone}`}
            </div>
          ) : null}
        </div>
        <span className={`h-2 w-2 rounded-full ${loaded ? "bg-success" : "bg-warning animate-pulse"}`} />
        <button
          type="button"
          onClick={createNewSession}
          className="btn h-8 w-8 p-0 flex items-center justify-center"
          title={zh ? "新建对话" : "New conversation"}
          aria-label={zh ? "新建对话" : "New conversation"}
        >
          <Plus size={15} />
        </button>
        <button
          type="button"
          onClick={reload}
          className="btn h-8 w-8 p-0 flex items-center justify-center"
          title={zh ? "重新加载 Agent" : "Reload Agent"}
          aria-label={zh ? "重新加载 Agent" : "Reload Agent"}
        >
          <RefreshCw size={14} />
        </button>
        <button
          type="button"
          onClick={() => window.open(agentUrl || TACWORK_URL, "_blank", "noopener,noreferrer")}
          className="btn h-8 w-8 p-0 flex items-center justify-center"
          title={zh ? "独立窗口打开" : "Open in a new window"}
          aria-label={zh ? "独立窗口打开" : "Open in a new window"}
        >
          <ExternalLink size={14} />
        </button>
        <button
          type="button"
          onClick={() => setOpen(false)}
          className="btn h-8 w-8 p-0 flex items-center justify-center"
          title={zh ? "收起 Agent" : "Collapse Agent"}
          aria-label={zh ? "收起 Agent" : "Collapse Agent"}
        >
          <PanelRightClose size={14} />
        </button>
      </div>

      <div className={`${open ? "block" : "hidden"} relative flex-1 min-h-0 bg-background w-full`}>
        {!loaded && !connectionError && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 text-muted z-10 bg-background">
            <span className="h-6 w-6 border-2 border-border border-t-accent rounded-full animate-spin" />
            <span className="text-xs">{zh ? "正在连接 TACWork Agent…" : "Connecting to TACWork Agent…"}</span>
          </div>
        )}
        {!loaded && connectionError && !agentUrl && (
          <div className="absolute inset-0 flex flex-col items-center justify-center gap-3 text-muted z-10 bg-background px-6 text-center">
            <Bot size={24} className="text-warning" />
            <span className="text-sm">{zh ? "TACWork Agent 暂时无法连接" : "TACWork Agent is temporarily unavailable"}</span>
            <button type="button" className="btn flex items-center gap-2" onClick={reload}>
              <RefreshCw size={14} />
              {zh ? "重试连接" : "Retry"}
            </button>
          </div>
        )}
        {agentUrl ? (
          <iframe
            key={frameKey}
            src={agentUrl}
            title="TACWork Agent"
            className="h-full w-full border-0 bg-background"
            onLoad={() => setLoaded(true)}
            allow="clipboard-read; clipboard-write"
            referrerPolicy="no-referrer"
          />
        ) : null}
      </div>
      </aside>
    </>
  );
}
