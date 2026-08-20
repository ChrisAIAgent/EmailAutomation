"use client";

import { useEffect, useState, useCallback } from "react";
import { api } from "@/lib/api";
import { useLang } from "@/lib/i18n";
import {
  Bot, Play, Pause, Plus, Loader2, AlertTriangle, CheckCircle,
  ChevronRight, Send, Clock, Users, MessageCircle, RefreshCw, Trash2,
} from "lucide-react";

export default function AutomationView({
  onChanged,
  onNavigate,
}: {
  onChanged: () => void;
  onNavigate?: (target: "campaigns") => void;
}) {
  const { t, lang, formatDate } = useLang();
  const [automations, setAutomations] = useState<any[]>([]);
  const [globalAutomation, setGlobalAutomation] = useState<any | null>(null);
  const [scheduler, setScheduler] = useState<any | null>(null);
  const [campaigns, setCampaigns] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null);

  // form
  const [selCampaign, setSelCampaign] = useState<number>(0);
  const [followupDays, setFollowupDays] = useState(3);
  const [maxFollowups, setMaxFollowups] = useState(2);
  const [stopOnReply, setStopOnReply] = useState(true);
  const [stopOnReject, setStopOnReject] = useState(true);
  const [executionMode, setExecutionMode] = useState<"full_auto" | "semi_auto">("full_auto");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [preparedRun, setPreparedRun] = useState<any | null>(null);
  const [takeoverScope, setTakeoverScope] = useState<"" | "recent_days" | "all_business" | "future_only">("");
  const [takeoverDays, setTakeoverDays] = useState(30);
  const [globalInterval, setGlobalInterval] = useState(60);

  const displayTime = (item: any, key: "next_run_at" | "last_run_at") =>
    item?.display_time?.[key]?.local || formatDate(item?.[key]);

  const load = useCallback(async () => {
    setLoading(true);
    try {
      const [aData, cData, globalData, schedulerData] = await Promise.all([
        api.automations(), api.campaigns(), api.globalAutomation(), api.automationSchedulerStatus(),
      ]);
      const items = aData?.items || aData || [];
      setAutomations(Array.isArray(items) ? items.filter((item: any) => item.scope !== 'global') : []);
      setGlobalAutomation(globalData);
      setScheduler(schedulerData);
      setTakeoverScope(globalData?.plan?.takeover_scope || "");
      setTakeoverDays(globalData?.plan?.takeover_days || 30);
      setGlobalInterval(globalData?.tick_interval_minutes || 60);
      setCampaigns(Array.isArray(cData) ? cData.filter((c: any) => c.status !== 'archived') : []);
    } catch (e: any) {
      setError(e?.message || t('err_loading_automation'));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => { load(); }, [load]);

  // auto-open create form when campaigns exist but no automations
  useEffect(() => {
  }, [loading, campaigns.length, automations.length]);

  const resetForm = () => {
    // default to the active campaign if any, otherwise first
    const activeCamp = campaigns.find((c: any) => c.status === 'active');
    setSelCampaign(activeCamp ? activeCamp.id : (campaigns.length > 0 ? campaigns[0].id : 0));
    setFollowupDays(3);
    setMaxFollowups(2);
    setStopOnReply(true);
    setStopOnReject(true);
    setExecutionMode("full_auto");
  };

  const openCreate = () => {
    if (campaigns.length === 0) {
      return;
    }
    setError("");
    resetForm();
    setShowCreate(true);
  };

  const handleSave = async () => {
    if (!selCampaign) {
      setError(lang === 'zh' ? '请先选择营销任务' : 'Please select a campaign');
      return;
    }
    setSaving(true);
    setError("");
    try {
      const plan = {
        enabled: true,
        tick_interval_minutes: 60,
        first_email_approval_required: true,
        follow_up_after_days: followupDays,
        max_follow_ups: maxFollowups,
        auto_send_low_risk_follow_up: false,
        execution_mode: executionMode,
        stop_on_intents: [
          ...(stopOnReply ? ["interested", "asking_question"] : []),
          ...(stopOnReject ? ["bounce", "not_interested", "unsubscribe", "opt_out"] : []),
        ],
      };
      const res = await api.createAutomation(
        selCampaign,
        lang === 'zh' ? `自动跟进: ${followupDays}天后跟进, 最多${maxFollowups}次` : `Follow-up after ${followupDays}d, max ${maxFollowups}`,
        plan,
        lang === 'zh' ? 'Campaign 自动跟进' : 'Campaign Automation',
        "campaign"
      );
      setShowCreate(false);
      await load();
      // auto-select the new automation
      const newId = res?.id || res?.automation_id;
      if (newId) setSelectedId(newId);
      setSuccess(lang === 'zh' ? '保存成功，自动跟进已创建（请点击启用）' : 'Saved - automation created (click Resume to enable)');
      setTimeout(() => setSuccess(""), 4000);
    } catch (e: any) {
      setError(e?.message || t('common_failed'));
    } finally {
      setSaving(false);
    }
  };

  const toggleGlobal = async (enabled: boolean) => {
    setError("");
    if (enabled && !takeoverScope) {
      setError(lang === 'zh' ? '开启前必须选择历史邮件接管范围。' : 'Choose a mailbox takeover scope before enabling.');
      return;
    }
    try {
      const updated = await api.updateGlobalAutomation({
        enabled, mode: "full_auto", tick_interval_minutes: globalInterval,
        takeover_scope: takeoverScope || undefined,
        takeover_days: takeoverScope === "recent_days" ? takeoverDays : undefined,
      });
      setGlobalAutomation(updated);
      setSuccess(enabled ? (lang === 'zh' ? '全局邮件自动化已开启' : 'Global inbox automation enabled') : (lang === 'zh' ? '全局邮件自动化已关闭' : 'Global inbox automation disabled'));
      await load();
    } catch (e: any) {
      setError(e?.message || t('common_failed'));
    }
  };

  const doAction = async (id: number, action: string) => {
    setError("");
    try {
      if (action === "enable") await api.enableAutomation(id);
      else if (action === "pause") await api.pauseAutomation(id);
      else if (action === "run-now") {
        const run = await api.agentRun(id, executionMode);
        if (executionMode === "semi_auto") {
          setSuccess(lang === 'zh' ? `已开始准备发送计划（Run #${run.run_id}）` : `Preparing send plan (Run #${run.run_id})`);
          const timer = setTimeout(async () => {
            try { setPreparedRun(await api.agentRunDetail(run.run_id)); } catch { /* status will refresh */ }
          }, 1000);
          return () => clearTimeout(timer);
        }
        setSuccess(lang === 'zh' ? `全自动执行已启动（Run #${run.run_id}）` : `Full-auto run started (Run #${run.run_id})`);
      }
      await load();
    } catch (e: any) {
      setError(e?.message || t('common_failed'));
    }
  };

  const doDelete = async (id: number) => {
    setDeletingId(id);
    setError("");
    try {
      await api.deleteAutomation(id);
      setConfirmDeleteId(null);
      await load();
      setSuccess(lang === 'zh' ? '已删除' : 'Deleted');
      setTimeout(() => setSuccess(""), 3000);
    } catch (e: any) {
      setError(e?.message || t('common_failed'));
    } finally {
      setDeletingId(null);
    }
  };

  const updateSchedule = async (id: number, minutes: number) => {
    setError("");
    try {
      await api.updateAutomationSchedule(id, minutes);
      setSuccess(lang === 'zh' ? '执行频率已更新' : 'Schedule updated');
      await load();
    } catch (e: any) {
      setError(e?.message || t('common_failed'));
    }
  };

  const scanDueNow = async () => {
    setError("");
    try {
      const result = await api.runDueAutomations();
      setSuccess(
        lang === 'zh'
          ? (result.enqueued ? `已排入 ${result.enqueued} 个到期任务` : '当前没有到期任务')
          : (result.enqueued ? `${result.enqueued} due task(s) enqueued` : 'No automation is due')
      );
      await load();
    } catch (e: any) {
      setError(e?.message || t('common_failed'));
    }
  };

  if (loading) {
    return <div className="text-center py-16 text-muted"><Loader2 size={20} className="animate-spin mx-auto mb-2" />{t('common_loading')}</div>;
  }

  const selectedAuto = selectedId ? automations.find(a => a.id === selectedId) : null;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-3 flex-wrap">
        <h2 className="text-lg font-semibold">{t('auto_title')}</h2>
      </div>

      {error && (
        <div className="flex items-center gap-2 text-sm text-danger bg-danger/10 rounded p-2">
          <AlertTriangle size={14} /> {error}
          <button className="underline ml-auto text-xs" onClick={() => setError("")}>x</button>
        </div>
      )}
      {success && (
        <div className="flex items-center gap-2 text-sm text-ok bg-ok/10 rounded p-2">
          <CheckCircle size={14} /> {success}
        </div>
      )}

      <div className="card border border-border space-y-3">
        <div className="flex items-start justify-between gap-3 flex-wrap">
          <div>
            <h3 className="font-semibold flex items-center gap-2">
              <Clock size={16} /> {lang === 'zh' ? 'Web 定时调度器' : 'Web Scheduler'}
            </h3>
            <p className="text-xs text-muted mt-1">
              {lang === 'zh'
                ? '由 Email Automation Backend 与 Consumer 执行，无需 Electron；电脑和统一服务需保持运行。'
                : 'Runs through the Email Automation Backend and Consumer without Electron. The computer and unified services must stay running.'}
            </p>
          </div>
          <button className="btn text-xs flex items-center gap-1" onClick={scanDueNow} disabled={!scheduler?.consumer_healthy}>
            <RefreshCw size={12} /> {lang === 'zh' ? '立即检查到期任务' : 'Check due tasks now'}
          </button>
        </div>
        <div className="flex items-center gap-4 text-xs text-muted flex-wrap">
          <span className={scheduler?.consumer_healthy ? 'text-ok' : 'text-danger'}>
            {lang === 'zh' ? '调度状态' : 'Scheduler'}: {scheduler?.consumer_healthy ? (lang === 'zh' ? '运行中' : 'Running') : (lang === 'zh' ? '不可用' : 'Unavailable')}
          </span>
          <span>{lang === 'zh' ? '执行引擎' : 'Engine'}: Backend + Huey</span>
          <span>{lang === 'zh' ? '已启用任务' : 'Enabled'}: {scheduler?.enabled_automations ?? 0}</span>
          <span>{lang === 'zh' ? '最近到期' : 'Next due'}: {scheduler?.agent_takeover_owns_global ? (lang === 'zh' ? 'Global Inbox 已由 Agent 接管' : 'Global Inbox owned by Agent Takeover') : (scheduler?.display_time?.next_due_at?.local || (scheduler?.next_due_at ? formatDate(scheduler.next_due_at) : '-'))}</span>
        </div>
      </div>

      {/* Global Inbox scheduling is independent from Campaigns. */}
      <div className="card space-y-3 border border-accent/30">
        <div className="flex items-start justify-between gap-3">
          <div><h3 className="font-semibold">{lang === 'zh' ? '全局收件箱定时任务' : 'Global Inbox Scheduled Task'}</h3><p className="text-xs text-muted mt-1">{lang === 'zh' ? '无需 Campaign。按计划处理已分拣、已确认真人且需要回复或跟进的业务邮件；不会创建陌生开发信。' : 'No Campaign required. Handles triaged, verified-human business threads eligible for reply or follow-up; never creates cold outreach.'}</p></div>
          <button className={globalAutomation?.status === 'enabled' ? 'btn-primary text-xs' : 'btn text-xs'} onClick={() => toggleGlobal(globalAutomation?.status !== 'enabled')}>{globalAutomation?.status === 'enabled' ? (lang === 'zh' ? '已开启，点击关闭' : 'Enabled — turn off') : (lang === 'zh' ? '创建并开启全局任务' : 'Create & enable global task')}</button>
        </div>
        <label className="text-xs text-muted">{lang === 'zh' ? '收件箱接管范围（必选）' : 'Inbox takeover scope (required)'}
          <select className="input w-full text-sm mt-1" value={takeoverScope} disabled={globalAutomation?.status === 'enabled'} onChange={e => setTakeoverScope(e.target.value as any)}>
            <option value="">{lang === 'zh' ? '-- 请选择 --' : '-- Select --'}</option>
            <option value="recent_days">{lang === 'zh' ? '接管最近 X 天历史邮件' : 'Take over the last X days'}</option>
            <option value="all_business">{lang === 'zh' ? '接管全部历史业务邮件' : 'Take over all historical business mail'}</option>
            <option value="future_only">{lang === 'zh' ? '仅接管后续新邮件' : 'Only take over new mail from now on'}</option>
          </select>
        </label>
        {takeoverScope === "recent_days" && <label className="text-xs text-muted">{lang === 'zh' ? '历史天数' : 'History days'}<input type="number" min={1} max={3650} className="input w-full text-sm mt-1" value={takeoverDays} disabled={globalAutomation?.status === 'enabled'} onChange={e => setTakeoverDays(Number(e.target.value))}/></label>}
        <label className="text-xs text-muted">{lang === 'zh' ? '执行频率' : 'Schedule interval'}
          <select
            className="input w-full text-sm mt-1"
            value={globalInterval}
            onChange={async e => {
              const minutes = Number(e.target.value);
              setGlobalInterval(minutes);
              if (globalAutomation?.id) await updateSchedule(globalAutomation.id, minutes);
            }}
          >
            <option value={15}>{lang === 'zh' ? '每 15 分钟' : 'Every 15 min'}</option>
            <option value={30}>{lang === 'zh' ? '每 30 分钟' : 'Every 30 min'}</option>
            <option value={60}>{lang === 'zh' ? '每小时' : 'Hourly'}</option>
            <option value={360}>{lang === 'zh' ? '每 6 小时' : 'Every 6 hours'}</option>
            <option value={720}>{lang === 'zh' ? '每 12 小时' : 'Every 12 hours'}</option>
            <option value={1440}>{lang === 'zh' ? '每天' : 'Daily'}</option>
          </select>
        </label>
      </div>

      <div className="flex items-center justify-between gap-3 flex-wrap pt-2">
        <div>
          <h3 className="font-semibold">{lang === 'zh' ? 'Campaign 定时自动化' : 'Campaign Scheduled Automation'}</h3>
          <p className="text-xs text-muted mt-1">{lang === 'zh' ? '用于获客邮件和 Campaign 跟进；需要先创建 Campaign。' : 'For outreach and Campaign follow-ups; requires a Campaign.'}</p>
        </div>
        {campaigns.length > 0 && (
          <button className="btn-primary flex items-center gap-1.5" onClick={openCreate}>
            <Plus size={14} /> {lang === 'zh' ? '创建 Campaign 自动化' : 'Create Campaign Automation'}
          </button>
        )}
      </div>

      {campaigns.length === 0 && !showCreate ? (
        <div className="card text-center py-10 text-muted">
          <Users size={32} className="mx-auto mb-2 opacity-40" />
          <p className="text-sm font-medium mb-1">{lang === 'zh' ? '尚未创建 Campaign' : 'No Campaign yet'}</p>
          <p className="text-xs mb-4">{lang === 'zh' ? '这不会影响上方全局收件箱定时任务。创建 Campaign 后可配置获客和跟进计划。' : 'This does not affect the Global Inbox task above. Create a Campaign to schedule outreach and follow-ups.'}</p>
          <button className="btn-primary inline-flex items-center gap-1.5" onClick={() => onNavigate?.("campaigns")}>
            <Plus size={14} /> {lang === 'zh' ? '前往创建 Campaign' : 'Create a Campaign'}
          </button>
        </div>
      ) : (
        <>
          {/* create form: auto-shown when no automations yet (via useEffect), or toggled manually */}
          {showCreate && (
            <div className="card space-y-4">
              <h3 className="font-semibold">{t('auto_create_title')}</h3>
              <p className="text-xs text-muted">{t('auto_review_note')}</p>
              <div className="space-y-2">
                <label className="text-xs text-muted">{t('auto_select_campaign')}</label><select className="input w-full text-sm" value={selCampaign} onChange={e => setSelCampaign(Number(e.target.value))}><option value={0}>{lang === 'zh' ? '-- 请选择 --' : '-- Select --'}</option>{campaigns.map(c => <option key={c.id} value={c.id}>{c.name}</option>)}</select>
              </div>

              <div className="space-y-1">
                <label className="text-xs text-muted">{lang === 'zh' ? '执行模式' : 'Execution mode'}</label>
                <select className="input w-full text-sm" value={executionMode} onChange={e => setExecutionMode(e.target.value as "full_auto" | "semi_auto") }>
                  <option value="full_auto">{lang === 'zh' ? '全自动：校验后直接发送并汇报' : 'Full auto: send after checks and report'}</option>
                  <option value="semi_auto">{lang === 'zh' ? '半自动：发送前汇报并等待确认' : 'Semi auto: report and wait before send'}</option>
                </select>
              </div>

              <div className="grid grid-cols-2 gap-4">
                <div className="space-y-1">
                  <label className="text-xs text-muted">{t('auto_followup_after')}</label>
                  <input type="number" className="input w-full text-sm" min={1} max={30} value={followupDays} onChange={e => setFollowupDays(Number(e.target.value))} />
                </div>
                <div className="space-y-1">
                  <label className="text-xs text-muted">{t('auto_max_followups')}</label>
                  <input type="number" className="input w-full text-sm" min={1} max={10} value={maxFollowups} onChange={e => setMaxFollowups(Number(e.target.value))} />
                </div>
              </div>

              <div className="space-y-2">
                <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={stopOnReply} onChange={e => setStopOnReply(e.target.checked)} /> {t('auto_stop_on_reply')}</label>
                <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={stopOnReject} onChange={e => setStopOnReject(e.target.checked)} /> {t('auto_stop_on_reject')}</label>
              </div>

              <div className="flex gap-2">
                <button className="btn-primary flex-1" onClick={handleSave} disabled={saving}>
                  {saving ? t('common_processing') : t('auto_save_enable')}
                </button>
                <button className="btn" onClick={() => setShowCreate(false)}>{t('common_cancel')}</button>
              </div>
            </div>
          )}

          {/* automation list */}
          {automations.map(a => {
            const isSelected = selectedId === a.id;
            const plan = a.plan || {};
            return (
              <div key={a.id} className={`card space-y-2 ${isSelected ? 'ring-2 ring-accent' : ''}`}>
                <div className="flex items-center justify-between flex-wrap gap-2">
                  <div>
                    <h3 className="font-medium text-sm">{a.name || `Automation #${a.id}`}</h3>
                    <p className="text-xs text-muted">
                      <span className="mr-1">{lang === 'zh' ? 'Campaign' : 'Campaign'}</span>
                      {t('auto_followup_after_days', { days: plan.follow_up_after_days || followupDays })}
                      {' · '}{t('auto_max_followups_count', { count: plan.max_follow_ups || maxFollowups })}
                    </p>
                  </div>
                  <div className="flex items-center gap-2">
                    <span className={`text-xs px-2 py-0.5 rounded ${a.status === 'enabled' ? 'bg-ok/10 text-ok' : 'bg-warn/10 text-warn'}`}>
                      {a.status === 'enabled' ? t('auto_enabled_label') : t('auto_paused_label')}
                    </span>
                  </div>
                </div>

                {/* metrics */}
                <div className="flex items-center gap-4 text-xs text-muted flex-wrap">
                  <span className="flex items-center gap-1"><Users size={12} /> {t('auto_customer_count')}: {a.contact_count ?? '-'}</span>
                  <span className="flex items-center gap-1"><MessageCircle size={12} /> {t('auto_awaiting_reply')}: {a.pending_reply ?? '-'}</span>
                  <span className="flex items-center gap-1"><Clock size={12} /> {t('auto_pending_followup')}: {a.pending_followup ?? '-'}</span>
                </div>

                {/* actions */}
                <div className="flex items-center gap-1.5">
                  <select
                    className="input text-xs py-1 w-28"
                    value={a.tick_interval_minutes || 60}
                    onChange={e => updateSchedule(a.id, Number(e.target.value))}
                    aria-label={lang === 'zh' ? '执行频率' : 'Schedule interval'}
                  >
                    <option value={15}>{lang === 'zh' ? '每 15 分钟' : 'Every 15 min'}</option>
                    <option value={30}>{lang === 'zh' ? '每 30 分钟' : 'Every 30 min'}</option>
                    <option value={60}>{lang === 'zh' ? '每小时' : 'Hourly'}</option>
                    <option value={360}>{lang === 'zh' ? '每 6 小时' : 'Every 6 hours'}</option>
                    <option value={720}>{lang === 'zh' ? '每 12 小时' : 'Every 12 hours'}</option>
                    <option value={1440}>{lang === 'zh' ? '每天' : 'Daily'}</option>
                  </select>
                  <select className="input text-xs py-1 w-32" value={executionMode} onChange={e => setExecutionMode(e.target.value as "full_auto" | "semi_auto") } aria-label="Execution mode">
                    <option value="full_auto">{lang === 'zh' ? '全自动' : 'Full auto'}</option>
                    <option value="semi_auto">{lang === 'zh' ? '半自动' : 'Semi auto'}</option>
                  </select>
                  <button className="btn text-xs flex items-center gap-1" onClick={() => doAction(a.id, "run-now")}>
                    <Play size={12} /> {t('auto_run_now')}
                  </button>
                  {a.status === 'enabled' ? (
                    <button className="btn text-xs flex items-center gap-1" onClick={() => doAction(a.id, "pause")}>
                      <Pause size={12} /> {t('auto_pause')}
                    </button>
                  ) : (
                    <button className="btn text-xs flex items-center gap-1" onClick={() => doAction(a.id, "enable")}>
                      <Play size={12} /> {t('auto_resume')}
                    </button>
                  )}
                  <button className="btn text-xs flex items-center gap-1 text-danger" onClick={() => setConfirmDeleteId(a.id)} title={lang === 'zh' ? '删除这个自动跟进' : 'Delete this automation'}>
                    <Trash2 size={12} /> {t('common_delete')}
                  </button>
                  <button className="btn text-xs ml-auto" onClick={() => setSelectedId(selectedId === a.id ? null : a.id)}>
                    {isSelected ? (lang === 'zh' ? '取消选中' : 'Deselect') : (lang === 'zh' ? '查看详情' : 'Details')}
                  </button>
                </div>

                {/* selected detail: next run, last result */}
                {isSelected && (
                  <div className="border-t border-border pt-2 space-y-1 text-xs text-muted">
                    <div>{t('auto_next_run')}: {a.schedule_owner === 'agent_takeover' ? (lang === 'zh' ? '已由 Agent 接管，Huey 不执行' : 'Owned by Agent Takeover; Huey does not run it') : displayTime(a, 'next_run_at')}</div>
                    <div>{t('auto_last_result')}: {a.last_status || '-'}</div>
                  </div>
                )}
              </div>
            );
          })}
          {preparedRun?.status === "awaiting_confirmation" && (
            <div className="card border border-warn/40 space-y-3">
              <h3 className="font-semibold">{lang === 'zh' ? '发送计划待确认' : 'Send plan awaiting confirmation'}</h3>
              <p className="text-sm text-muted">{lang === 'zh' ? `本轮将发送 ${preparedRun.send_plan?.length || 0} 封邮件；确认后不会重新生成内容。` : `${preparedRun.send_plan?.length || 0} emails are frozen for this run; confirmation will not regenerate them.`}</p>
              <div className="max-h-36 overflow-auto text-xs space-y-1">{(preparedRun.send_plan || []).map((item: any) => <div key={item.approval_id}>{item.to_email} · {item.subject}</div>)}</div>
              <div className="flex gap-2"><button className="btn-primary" onClick={async () => { await api.confirmAgentRun(preparedRun.id); setPreparedRun(null); await load(); }}>{lang === 'zh' ? '确认并执行' : 'Confirm & execute'}</button><button className="btn" onClick={async () => { await api.cancelAgentRun(preparedRun.id); setPreparedRun(null); await load(); }}>{lang === 'zh' ? '取消本轮' : 'Cancel run'}</button></div>
            </div>
          )}
      {/* confirm delete modal */}
      {confirmDeleteId !== null && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="card max-w-sm w-full mx-4 space-y-3">
            <h3 className="font-semibold text-danger">{t('common_confirm_delete')}</h3>
            <p className="text-sm text-muted">{t('common_delete_warning')}</p>
            <div className="flex gap-2">
              <button className="btn-danger flex-1" disabled={deletingId === confirmDeleteId} onClick={() => doDelete(confirmDeleteId)}>
                {deletingId === confirmDeleteId ? t('common_processing') : t('common_confirm')}
              </button>
              <button className="btn flex-1" onClick={() => setConfirmDeleteId(null)}>{t('common_cancel')}</button>
            </div>
          </div>
        </div>
      )}
    </>
  )}
    </div>
  );
}

// Users icon from lucide-react; no local wrapper needed
