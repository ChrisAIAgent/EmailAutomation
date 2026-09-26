"use client";

import { useCallback, useEffect, useState } from "react";
import { AlertTriangle, CheckCircle, Clock, RefreshCw } from "lucide-react";
import { api } from "@/lib/api";
import { useLang } from "@/lib/i18n";

export default function AutomationView({
  onChanged,
}: {
  onChanged: () => void;
  onNavigate?: (target: "campaigns") => void;
}) {
  const { t, lang, formatDate } = useLang();
  const [globalAutomation, setGlobalAutomation] = useState<any | null>(null);
  const [scheduler, setScheduler] = useState<any | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [takeoverScope, setTakeoverScope] = useState<"" | "recent_days" | "all_business" | "future_only">("");
  const [takeoverDays, setTakeoverDays] = useState(30);
  const [globalInterval, setGlobalInterval] = useState(60);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [globalData, schedulerData] = await Promise.all([
        api.globalAutomation(),
        api.automationSchedulerStatus(),
      ]);
      setGlobalAutomation(globalData);
      setScheduler(schedulerData);
      setTakeoverScope(globalData?.plan?.takeover_scope || "");
      setTakeoverDays(globalData?.plan?.takeover_days || 30);
      setGlobalInterval(globalData?.tick_interval_minutes || 60);
    } catch (e: any) {
      setError(e?.message || t("err_loading_automation"));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => { load(); }, [load]);

  const toggleGlobal = async (enabled: boolean) => {
    setError("");
    if (enabled && !takeoverScope) {
      setError(lang === "zh" ? "开启前必须选择历史邮件接管范围。" : "Choose a mailbox takeover scope before enabling.");
      return;
    }
    try {
      const updated = await api.updateGlobalAutomation({
        enabled,
        mode: "full_auto",
        tick_interval_minutes: globalInterval,
        takeover_scope: takeoverScope || undefined,
        takeover_days: takeoverScope === "recent_days" ? takeoverDays : undefined,
      });
      setGlobalAutomation(updated);
      setSuccess(enabled
        ? (lang === "zh" ? "全局邮件自动化已开启" : "Global inbox automation enabled")
        : (lang === "zh" ? "全局邮件自动化已关闭" : "Global inbox automation disabled"));
      await load();
      onChanged();
    } catch (e: any) {
      setError(e?.message || t("common_failed"));
    }
  };

  const updateSchedule = async (id: number, minutes: number) => {
    setError("");
    try {
      await api.updateAutomationSchedule(id, minutes);
      setSuccess(lang === "zh" ? "执行频率已更新" : "Schedule updated");
      await load();
      onChanged();
    } catch (e: any) {
      setError(e?.message || t("common_failed"));
    }
  };

  const scanDueNow = async () => {
    setError("");
    try {
      const result = await api.runDueAutomations();
      setSuccess(lang === "zh"
        ? (result.enqueued ? `已排入 ${result.enqueued} 个到期任务` : "当前没有到期任务")
        : (result.enqueued ? `${result.enqueued} due task(s) enqueued` : "No automation is due"));
      await load();
      onChanged();
    } catch (e: any) {
      setError(e?.message || t("common_failed"));
    }
  };

  if (loading) {
    return <div className="text-center py-16 text-muted"><span className="inline-flex"><RefreshCw size={20} className="animate-spin" /></span><div className="mt-2">{t("common_loading")}</div></div>;
  }

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3 flex-wrap">
        <h2 className="text-lg font-semibold">{t("auto_title")}</h2>
        <span className="text-xs text-muted">{lang === "zh" ? "全局收件箱自动化" : "Global Inbox Automation"}</span>
      </div>

      {error && <div className="flex items-center gap-2 text-sm text-danger bg-danger/10 rounded p-2"><AlertTriangle size={14} />{error}<button className="underline ml-auto text-xs" onClick={() => setError("")}>x</button></div>}
      {success && <div className="flex items-center gap-2 text-sm text-ok bg-ok/10 rounded p-2"><CheckCircle size={14} />{success}</div>}

      <div className="card border border-border space-y-3">
        <div className="flex items-start justify-between gap-3 flex-wrap">
          <div>
            <h3 className="font-semibold flex items-center gap-2"><Clock size={16} />{lang === "zh" ? "Web 定时调度器" : "Web Scheduler"}</h3>
            <p className="text-xs text-muted mt-1">{lang === "zh" ? "由 Email Automation Backend 与 Consumer 执行，无需 Electron；电脑和统一服务需保持运行。Campaign 自动化请在 Campaigns 页面配置。" : "Runs through the Email Automation Backend and Consumer without Electron. Configure Campaign automation on the Campaigns page."}</p>
          </div>
          <button className="btn text-xs flex items-center gap-1" onClick={scanDueNow} disabled={!scheduler?.consumer_healthy}><RefreshCw size={12} />{lang === "zh" ? "立即检查到期任务" : "Check due tasks now"}</button>
        </div>
        <div className="flex items-center gap-4 text-xs text-muted flex-wrap">
          <span className={scheduler?.consumer_healthy ? "text-ok" : "text-danger"}>{lang === "zh" ? "调度状态" : "Scheduler"}: {scheduler?.consumer_healthy ? (lang === "zh" ? "运行中" : "Running") : (lang === "zh" ? "不可用" : "Unavailable")}</span>
          <span>{lang === "zh" ? "执行引擎" : "Engine"}: Backend + Huey</span>
          <span>{lang === "zh" ? "已启用全局任务" : "Enabled global task"}: {globalAutomation?.status === "enabled" ? 1 : 0}</span>
          <span>{lang === "zh" ? "最近到期" : "Next due"}: {scheduler?.agent_takeover_owns_global ? (lang === "zh" ? "Global Inbox 已由 Agent 接管" : "Global Inbox owned by Agent Takeover") : (scheduler?.display_time?.next_due_at?.local || (scheduler?.next_due_at ? formatDate(scheduler.next_due_at) : "-"))}</span>
        </div>
      </div>

      <div className="card space-y-3 border border-accent/30">
        <div className="flex items-start justify-between gap-3">
          <div>
            <h3 className="font-semibold">{lang === "zh" ? "全局收件箱定时任务" : "Global Inbox Scheduled Task"}</h3>
            <p className="text-xs text-muted mt-1">{lang === "zh" ? "无需 Campaign。按计划处理已分拣、已确认真人且需要回复或跟进的业务邮件；不会创建陌生开发信。Campaign 自动化不在此处配置。" : "No Campaign required. Handles triaged, verified-human business threads eligible for reply or follow-up; never creates cold outreach. Campaign automation is configured in Campaigns."}</p>
          </div>
          <button className={globalAutomation?.status === "enabled" ? "btn-primary text-xs" : "btn text-xs"} onClick={() => toggleGlobal(globalAutomation?.status !== "enabled")}>
            {globalAutomation?.status === "enabled" ? (lang === "zh" ? "已开启，点击关闭" : "Enabled — turn off") : (lang === "zh" ? "创建并开启全局任务" : "Create & enable global task")}
          </button>
        </div>
        <label className="text-xs text-muted">{lang === "zh" ? "收件箱接管范围（必选）" : "Inbox takeover scope (required)"}
          <select className="input w-full text-sm mt-1" value={takeoverScope} disabled={globalAutomation?.status === "enabled"} onChange={e => setTakeoverScope(e.target.value as any)}>
            <option value="">{lang === "zh" ? "-- 请选择 --" : "-- Select --"}</option>
            <option value="recent_days">{lang === "zh" ? "接管最近 X 天历史邮件" : "Take over the last X days"}</option>
            <option value="all_business">{lang === "zh" ? "接管全部历史业务邮件" : "Take over all historical business mail"}</option>
            <option value="future_only">{lang === "zh" ? "仅接管后续新邮件" : "Only take over new mail from now on"}</option>
          </select>
        </label>
        {takeoverScope === "recent_days" && <label className="text-xs text-muted">{lang === "zh" ? "历史天数" : "History days"}<input type="number" min={1} max={3650} className="input w-full text-sm mt-1" value={takeoverDays} disabled={globalAutomation?.status === "enabled"} onChange={e => setTakeoverDays(Number(e.target.value))} /></label>}
        <label className="text-xs text-muted">{lang === "zh" ? "执行频率" : "Schedule interval"}
          <select className="input w-full text-sm mt-1" value={globalInterval} onChange={async e => { const minutes = Number(e.target.value); setGlobalInterval(minutes); if (globalAutomation?.id) await updateSchedule(globalAutomation.id, minutes); }}>
            <option value={15}>{lang === "zh" ? "每 15 分钟" : "Every 15 min"}</option>
            <option value={30}>{lang === "zh" ? "每 30 分钟" : "Every 30 min"}</option>
            <option value={60}>{lang === "zh" ? "每小时" : "Hourly"}</option>
            <option value={360}>{lang === "zh" ? "每 6 小时" : "Every 6 hours"}</option>
            <option value={720}>{lang === "zh" ? "每 12 小时" : "Every 12 hours"}</option>
            <option value={1440}>{lang === "zh" ? "每天" : "Daily"}</option>
          </select>
        </label>
      </div>
    </div>
  );
}
