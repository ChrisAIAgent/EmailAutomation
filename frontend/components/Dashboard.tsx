"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Activity, BarChart3, Beaker, BookOpen, Bot, CalendarClock, ChevronLeft, ChevronRight, Inbox as InboxIcon, Megaphone, Moon, Pause, Play, Send, Stethoscope, Sun, Type, Users } from "lucide-react";
import { api, API_BASE, Metrics } from "@/lib/api";
import { useLang } from "@/lib/i18n";
import ConfigBar from "./ConfigBar";
import Overview from "./Overview";
import InboxView from "./Inbox";
import CampaignsView from "./Campaigns";
import ContactsView from "./Contacts";
import ApprovalsView from "./Approvals";
import ActivityView from "./Activity";
import ComparisonView from "./Comparison";
import KnowledgeBaseView from "./KnowledgeBase";
import AgentProfileView from "./AgentProfile";
import AgentPanel from "./AgentPanel";
import AgentTakeoverControl from "./AgentTakeoverControl";
import AutomationView from "./Automation";
import DiagnosticsView from "./Diagnostics";
import { useUiPreferences } from "@/lib/ui-preferences";

type Tab = "overview" | "inbox" | "contacts" | "campaigns" | "approvals" | "agent_profile" | "knowledge" | "activity" | "agent_lab" | "automation" | "diagnostics";

const TAB_KEYS: Record<Tab, { labelKey: string; icon: any }> = {
  overview:  { labelKey: "nav_dashboard",   icon: BarChart3 },
  inbox:     { labelKey: "nav_inbox",       icon: InboxIcon },
  contacts:  { labelKey: "nav_contacts",    icon: Users },
  campaigns: { labelKey: "nav_campaigns",   icon: Megaphone },
  approvals: { labelKey: "nav_approvals",   icon: Send },
  agent_profile:{ labelKey: "nav_agent_profile", icon: Bot },
  knowledge: { labelKey: "nav_knowledge",   icon: BookOpen },
  activity:  { labelKey: "nav_activity",    icon: Activity },
  agent_lab: { labelKey: "nav_agent_lab",   icon: Beaker },
  automation: { labelKey: "nav_automation", icon: CalendarClock },
  diagnostics: { labelKey: "nav_diagnostics", icon: Stethoscope },
};

export default function Dashboard() {
  const { t, lang, setLang } = useLang();
  const { theme, density, sidebarCollapsed, setTheme, setDensity, setSidebarCollapsed } = useUiPreferences();
  const [tab, setTab] = useState<Tab>("overview");
  const [metrics, setMetrics] = useState<Metrics | null>(null);
  const [gmail, setGmail] = useState<any>(null);
  const [health, setHealth] = useState<any[]>([]);
  const [readiness, setReadiness] = useState<any>(null);
  const [paused, setPaused] = useState(false);
  const [loading, setLoading] = useState(true);
  const [refreshing, setRefreshing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [gmailError, setGmailError] = useState<string | null>(null);
  const firstLoadRef = useRef(true);

  const load = useCallback(async () => {
    const isFirst = firstLoadRef.current;
    if (isFirst) setLoading(true); else setRefreshing(true);
    setError(null);
    try {
      const [m, g, h, p, r] = await Promise.all([
        api.metrics(),
        api.gmailStatus(),
        api.agentHealth(),
        api.pauseStatus(),
        api.readiness(),
      ]);
      setMetrics(m);
      setGmail(g);
      setHealth(h);
      setPaused((p as any).global_pause);
      setReadiness(r);
      firstLoadRef.current = false;
    } catch (e: any) {
      // On background refresh failure, keep the last good view (no flicker, no false success).
      // Only surface a blocking error before the first successful load.
      if (firstLoadRef.current) setError(e?.message || t('err_loading'));
      else console.warn("Dashboard background refresh failed:", e?.message);
    } finally {
      setLoading(false);
      setRefreshing(false);
    }
  }, [t]);

  useEffect(() => {
    load();
    const es = new EventSource(`${API_BASE}/api/events`);
    es.onmessage = () => load();
    return () => es.close();
  }, [load]);

  const togglePause = async () => {
    await api.setPause(!paused);
    setPaused(!paused);
  };

  const disconnectGmail = async () => {
    if (!window.confirm(lang === 'zh' ? '确定要断开 Gmail 授权吗？不会删除业务数据。' : 'Disconnect Gmail authorization? Business data will not be deleted.')) return;
    await api.gmailDisconnect();
    await load();
  };

  const connectGmail = async () => {
    setGmailError(null);
    try {
      const result = await api.gmailStart();
      if (result?.url) {
        if (window.emailAutomation) await window.emailAutomation.openExternal(result.url);
        else window.location.href = result.url;
      }
    } catch (e: any) {
      setGmailError(e?.message || (lang === "zh" ? "连接 Gmail 失败：请先在「全局 Agent Profile / 设置」中填写 Google OAuth 凭证" : "Gmail connect failed: configure Google OAuth credentials in Settings first"));
    }
  };

  if (loading) {
    return (
      <div className="min-h-screen flex items-center justify-center">
        <div className="flex flex-col items-center gap-3 text-muted">
          <span className="h-6 w-6 border-2 border-border border-t-accent rounded-full animate-spin" />
          <span>{t('common_loading')}</span>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="min-h-screen flex items-center justify-center p-6">
        <div className="card max-w-md w-full">
          <h2 className="text-lg font-semibold text-danger mb-2">{t('dash_title')}</h2>
          <p className="text-muted text-sm mb-3 break-words">{error}</p>
          <p className="text-muted text-xs mb-1">
            {lang === 'zh' ? '后端地址：' : 'Backend: '}<span className="break-all">{API_BASE}</span>
          </p>
          <p className="text-muted text-xs mb-4">
            {lang === 'zh' ? '请确认后端服务已启动且可访问，然后点击重试。' : 'Please confirm the backend is running and accessible, then click retry.'}
          </p>
          <button className="btn-primary" onClick={load} disabled={loading}>
            {loading ? t('common_loading') : t('common_retry')}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="h-screen flex overflow-hidden">
      <aside className={`h-full ${sidebarCollapsed ? "w-16" : "w-60"} shrink-0 border-r border-border bg-panel p-2 ${sidebarCollapsed ? "" : "lg:p-4"} flex flex-col gap-1 relative overflow-hidden transition-[width] duration-200`}>
        <div className="absolute -top-12 -right-12 h-28 w-28 rounded-full bg-accent/10 blur-2xl" />
        <div className={`flex items-center ${sidebarCollapsed ? "justify-center" : "justify-between"} mb-5 ${sidebarCollapsed ? "" : "px-2"} relative`}>
          <div className={`min-w-0 ${sidebarCollapsed ? "text-center" : "text-left"}`}>
            <img src="/brand/tac-wordmark.png" alt="TAC" className="h-[18px] w-auto" />
            {!sidebarCollapsed && <div className="text-[10px] uppercase tracking-[0.16em] text-muted mt-1 truncate">{t('dash_email_automation')}</div>}
          </div>
          {!sidebarCollapsed && <button type="button" className="btn !px-1.5 !py-1" onClick={() => setSidebarCollapsed(true)} title={lang === "zh" ? "收缩功能栏" : "Collapse navigation"}><ChevronLeft size={16} /></button>}
        </div>
        {sidebarCollapsed && <button type="button" className="btn !px-1.5 !py-1 mb-2 self-center" onClick={() => setSidebarCollapsed(false)} title={lang === "zh" ? "展开功能栏" : "Expand navigation"}><ChevronRight size={16} /></button>}
        {(Object.entries(TAB_KEYS) as [Tab, typeof TAB_KEYS[Tab]][]).map(([id, cfg]) => {
          const Icon = cfg.icon;
          return (
            <button
              key={id}
              onClick={() => setTab(id)}
              title={t(cfg.labelKey)}
              aria-label={t(cfg.labelKey)}
              className={`flex items-center ${sidebarCollapsed ? "justify-center" : "justify-start"} gap-2 px-2 ${sidebarCollapsed ? "" : "lg:px-3"} py-2 rounded-lg text-left text-sm ${
                tab === id ? "bg-brand text-white shadow-[0_6px_18px_rgba(239,27,45,.2)]" : "text-muted hover:bg-panel2"
              }`}
            >
              <Icon size={16} className="shrink-0" /> {!sidebarCollapsed && <span className="truncate">{t(cfg.labelKey)}</span>}
            </button>
          );
        })}
        <div className="mt-auto" />
        {!sidebarCollapsed && <AgentTakeoverControl paused={paused} onChanged={load} />}
        <button onClick={togglePause} className="btn flex items-center justify-center gap-2 text-xs px-2" title={paused ? (lang === 'zh' ? '恢复全部' : 'Resume All') : (lang === 'zh' ? '暂停全部' : 'Pause All')}>
          {paused ? <Play size={14} /> : <Pause size={14} />}
          {!sidebarCollapsed && <span>{paused ? (lang === 'zh' ? '恢复全部' : 'Resume All') : (lang === 'zh' ? '暂停全部' : 'Pause All')}</span>}
        </button>
        <div className={`mt-2 flex ${sidebarCollapsed ? "flex-col" : ""} gap-1`}>
          <button type="button" onClick={() => setTheme(theme === "dark" ? "light" : "dark")} className="text-xs text-muted hover:text-foreground text-center border border-border rounded py-1 px-1" title={theme === "dark" ? (lang === "zh" ? "切换浅色主题" : "Use light theme") : (lang === "zh" ? "切换深色主题" : "Use dark theme")}>
            {theme === "dark" ? <Sun size={15} className="inline" /> : <Moon size={15} className="inline" />} {!sidebarCollapsed && <span>{theme === "dark" ? (lang === "zh" ? "浅色" : "Light") : (lang === "zh" ? "深色" : "Dark")}</span>}
          </button>
          <button type="button" onClick={() => setDensity(density === "standard" ? "large" : "standard")} className="text-xs text-muted hover:text-foreground text-center border border-border rounded py-1 px-1" title={density === "standard" ? (lang === "zh" ? "切换大字" : "Use large text") : (lang === "zh" ? "切换标准字" : "Use standard text")}>
            <Type size={15} className="inline" /> {!sidebarCollapsed && <span>{density === "standard" ? (lang === "zh" ? "大字" : "Large text") : (lang === "zh" ? "标准字" : "Standard text")}</span>}
          </button>
          <button type="button" onClick={() => setLang(lang === 'zh' ? 'en' : 'zh')} className="text-xs text-muted hover:text-foreground text-center border border-border rounded py-1 px-1" title={lang === 'zh' ? 'Switch to English' : '切换到中文'}>
            <span>🌐</span>{!sidebarCollapsed && <span>{lang === 'zh' ? ' English' : ' 中文'}</span>}
          </button>
        </div>
      </aside>

      <main className="min-w-0 flex-1 p-3 md:p-4 xl:p-6 overflow-auto relative">
        {refreshing && (
          <div className="absolute top-0 left-0 right-0 h-0.5 bg-accent/70 animate-pulse" aria-hidden />
        )}
        {gmailError && (
          <div className="mb-3 rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">
            {gmailError}
          </div>
        )}
        <ConfigBar metrics={metrics} gmail={gmail} health={health} paused={paused} onDisconnect={disconnectGmail} onConnect={connectGmail} />
        <div className="mt-4">
          {tab === "overview" && <Overview metrics={metrics} gmail={gmail} health={health} readiness={readiness} onRefresh={load} />}
          {tab === "inbox" && <InboxView onChanged={load} />}
          {tab === "contacts" && <ContactsView onChanged={load} />}
          {tab === "campaigns" && <CampaignsView onChanged={load} onNavigate={(tgt: Tab) => setTab(tgt)} />}
          {tab === "approvals" && <ApprovalsView onChanged={load} />}
          {tab === "agent_profile" && <AgentProfileView onChanged={load} />}
          {tab === "knowledge" && <KnowledgeBaseView />}
          {tab === "activity" && <ActivityView />}
          {tab === "agent_lab" && <ComparisonView onChanged={load} agents={health} />}
          {tab === "automation" && <AutomationView onChanged={load} onNavigate={(tgt) => setTab(tgt)} />}
          {tab === "diagnostics" && <DiagnosticsView />}
        </div>
      </main>
      <AgentPanel />
    </div>
  );
}
