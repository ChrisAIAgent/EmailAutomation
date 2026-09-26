"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { CheckCircle, Clock, Loader2, MessageCircle, Pause, Play, Plus, Trash2, Users } from "lucide-react";
import { api } from "@/lib/api";
import { useLang } from "@/lib/i18n";
import CampaignRun from "./CampaignRun";

export default function CampaignAutomationPanel({
  onChanged,
  onNavigate,
}: {
  onChanged: () => void;
  onNavigate?: (target: "campaigns") => void;
}) {
  const { t, lang, formatDate } = useLang();
  const [automations, setAutomations] = useState<any[]>([]);
  const [campaigns, setCampaigns] = useState<any[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [saving, setSaving] = useState(false);
  const [deletingId, setDeletingId] = useState<number | null>(null);
  const [confirmDeleteId, setConfirmDeleteId] = useState<number | null>(null);
  const [selCampaign, setSelCampaign] = useState<number>(0);
  const [followupDays, setFollowupDays] = useState(3);
  const [maxFollowups, setMaxFollowups] = useState(2);
  const [stopOnReply, setStopOnReply] = useState(true);
  const [stopOnReject, setStopOnReject] = useState(true);
  const [executionMode, setExecutionMode] = useState<"full_auto" | "semi_auto">("full_auto");
  const [selectedId, setSelectedId] = useState<number | null>(null);
  const [runIds, setRunIds] = useState<Record<number, number>>({});
  const [runModes, setRunModes] = useState<Record<number, "full_auto" | "semi_auto">>({});
  const [pendingEnableId, setPendingEnableId] = useState<number | null>(null);
  const [busyIds, setBusyIds] = useState<Record<number, boolean>>({});
  const actionLocks = useRef(new Set<number>());
  const saveLock = useRef(false);
  const loadGeneration = useRef(0);

  const displayTime = (item: any, key: "next_run_at" | "last_run_at") =>
    item?.display_time?.[key]?.local || formatDate(item?.[key]);

  const load = useCallback(async () => {
    const generation = ++loadGeneration.current;
    setLoading(true);
    try {
      const [aData, cData] = await Promise.all([api.automations(), api.campaigns()]);
      if (generation !== loadGeneration.current) return;
      const items = aData?.items || aData || [];
      const campaignItems = Array.isArray(items) ? items.filter((item: any) => item.scope !== "global") : [];
      setAutomations(campaignItems);
      setCampaigns(Array.isArray(cData) ? cData.filter((c: any) => c.status !== "archived") : []);
      const details = await Promise.allSettled(campaignItems.map((item: any) => api.automationDetail(item.id)));
      if (generation !== loadGeneration.current) return;
      const recovered: Record<number, number> = {};
      details.forEach((result, index) => {
        if (result.status !== "fulfilled") return;
        const current = result.value.runs?.find((run: any) => ["queued", "running", "confirmed", "awaiting_confirmation", "recovery_pending"].includes(run.status));
        if (current) recovered[campaignItems[index].id] = current.id;
      });
      setRunIds(previous => ({ ...previous, ...recovered }));
      if (details.some(result => result.status === "rejected")) setError(lang === "zh" ? "部分 Run 状态读取失败，请刷新状态。" : "Some Run statuses could not be recovered. Refresh status.");
    } catch (e: any) {
      if (generation === loadGeneration.current) setError(e?.message || t("err_loading_automation"));
    } finally {
      if (generation === loadGeneration.current) setLoading(false);
    }
  }, [t, lang]);

  useEffect(() => { load(); }, [load]);
  useEffect(() => () => { loadGeneration.current += 1; }, []);

  const resetForm = () => {
    const activeCamp = campaigns.find((c: any) => c.status === "active");
    setSelCampaign(activeCamp ? activeCamp.id : (campaigns.length > 0 ? campaigns[0].id : 0));
    setFollowupDays(3);
    setMaxFollowups(2);
    setStopOnReply(true);
    setStopOnReject(true);
    setExecutionMode("full_auto");
  };

  const openCreate = () => {
    if (campaigns.length === 0) return;
    setError("");
    resetForm();
    setShowCreate(true);
  };

  const handleSave = async () => {
    if (saveLock.current) return;
    if (!selCampaign) {
      setError(lang === "zh" ? "请先选择营销任务" : "Please select a campaign");
      return;
    }
    saveLock.current = true;
    setSaving(true);
    setError("");
    let createdId: number | null = null;
    try {
      const plan = {
        enabled: false,
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
        lang === "zh" ? `自动跟进: ${followupDays}天后跟进, 最多${maxFollowups}次` : `Follow-up after ${followupDays}d, max ${maxFollowups}`,
        plan,
        lang === "zh" ? "Campaign 自动跟进" : "Campaign Automation",
        "campaign",
      );
      createdId = res?.id || res?.automation_id;
      if (!createdId) throw new Error("Automation ID missing");
      setPendingEnableId(createdId);
      setShowCreate(false);
      setSelectedId(createdId);
      await api.enableAutomation(createdId);
      setPendingEnableId(null);
      setSuccess(lang === "zh" ? "保存成功，Campaign 自动化已启用" : "Saved - Campaign automation is enabled");
      setTimeout(() => setSuccess(""), 4000);
      onChanged();
    } catch (e: any) {
      setError(createdId ? `${lang === "zh" ? "已保存，启用未确认；请重试启用已有配置" : "Saved; enablement unconfirmed. Retry enabling the existing configuration"} #${createdId}: ${e?.message || ""}` : (e?.message || t("common_failed")));
    } finally {
      if (createdId) await load();
      saveLock.current = false;
      setSaving(false);
    }
  };

  const doAction = async (id: number, action: string) => {
    if (actionLocks.current.has(id)) return;
    actionLocks.current.add(id);
    setBusyIds(previous => ({ ...previous, [id]: true }));
    setError("");
    try {
      if (action === "enable") {
        await api.enableAutomation(id);
        if (pendingEnableId === id) setPendingEnableId(null);
      }
      else if (action === "pause") await api.pauseAutomation(id);
      else if (action === "run-now") {
        const configuredMode = automations.find(item => item.id === id)?.execution_mode || "full_auto";
        const run = await api.agentRun(id, runModes[id] || configuredMode);
        setRunIds(previous => ({ ...previous, [id]: run.run_id }));
        if (run.mode === "semi_auto") {
          setSuccess(lang === "zh" ? `已开始准备发送计划（Run #${run.run_id}）` : `Preparing send plan (Run #${run.run_id})`);
        } else {
          setSuccess(lang === "zh" ? `全自动执行已启动（Run #${run.run_id}）` : `Full-auto run started (Run #${run.run_id})`);
        }
      }
      await load();
      onChanged();
    } catch (e: any) {
      setError(e?.message || t("common_failed"));
    } finally {
      actionLocks.current.delete(id);
      setBusyIds(previous => ({ ...previous, [id]: false }));
    }
  };

  const doDelete = async (id: number) => {
    setDeletingId(id);
    setError("");
    try {
      await api.deleteAutomation(id);
      setConfirmDeleteId(null);
      await load();
      onChanged();
      setSuccess(lang === "zh" ? "已删除" : "Deleted");
      setTimeout(() => setSuccess(""), 3000);
    } catch (e: any) {
      setError(e?.message || t("common_failed"));
    } finally {
      setDeletingId(null);
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

  if (loading) {
    return <div className="card text-center py-10 text-muted"><Loader2 size={20} className="animate-spin mx-auto mb-2" />{t("common_loading")}</div>;
  }

  const campaignName = (campaignId: number) => campaigns.find((c: any) => c.id === campaignId)?.name || `Campaign #${campaignId}`;

  return (
    <section className="card space-y-3 border border-accent/30">
      <div className="flex items-start justify-between gap-3 flex-wrap">
        <div>
          <h3 className="font-semibold">{lang === "zh" ? "Campaign 自动化" : "Campaign Automation"}</h3>
          <p className="text-xs text-muted mt-1">
            {lang === "zh"
              ? "Campaign 的首封获客邮件和后续跟进在这里配置；全局收件箱自动化保持独立。"
              : "Configure Campaign outreach and follow-ups here; Global Inbox Automation remains separate."}
          </p>
        </div>
        {campaigns.length > 0 && (
          <button className="btn-primary flex items-center gap-1.5" onClick={openCreate}>
            <Plus size={14} /> {lang === "zh" ? "创建 Campaign 自动化" : "Create Campaign Automation"}
          </button>
        )}
      </div>

      {error && <div role="alert" className="text-sm text-danger bg-danger/10 rounded p-2">{error}<button className="btn ml-2" onClick={() => { setError(""); void load(); }}>{lang === "zh" ? "刷新状态" : "Refresh status"}</button></div>}
      {pendingEnableId !== null && <button className="btn" disabled={busyIds[pendingEnableId]} onClick={() => doAction(pendingEnableId, "enable")}>{lang === "zh" ? "重试启用已有配置" : "Retry enabling saved automation"} #{pendingEnableId}</button>}
      {success && <div className="flex items-center gap-2 text-sm text-ok bg-ok/10 rounded p-2"><CheckCircle size={14} />{success}</div>}

      {campaigns.length === 0 && !showCreate ? (
        <div className="text-center py-8 text-muted">
          <Users size={30} className="mx-auto mb-2 opacity-40" />
          <p className="text-sm font-medium mb-1">{lang === "zh" ? "尚未创建 Campaign" : "No Campaign yet"}</p>
          <p className="text-xs mb-4">{lang === "zh" ? "创建 Campaign 后可在这里配置获客和跟进计划。" : "Create a Campaign before configuring outreach and follow-ups."}</p>
          <button className="btn-primary" onClick={() => onNavigate?.("campaigns")}>
            {lang === "zh" ? "创建 Campaign" : "Create a Campaign"}
          </button>
        </div>
      ) : (
        <>
          {showCreate && (
            <div className="border-t border-border pt-3 space-y-4">
              <h4 className="font-semibold">{t("auto_create_title")}</h4>
              <p className="text-xs text-muted">{t("auto_review_note")}</p>
              <label className="text-xs text-muted block">{t("auto_select_campaign")}
                <select className="input w-full text-sm mt-1" value={selCampaign} onChange={e => setSelCampaign(Number(e.target.value))}>
                  <option value={0}>{lang === "zh" ? "-- 请选择 --" : "-- Select --"}</option>
                  {campaigns.map((c: any) => <option key={c.id} value={c.id}>{c.name}</option>)}
                </select>
              </label>
              <label className="text-xs text-muted block">{lang === "zh" ? "执行模式" : "Execution mode"}
                <select className="input w-full text-sm mt-1" value={executionMode} onChange={e => setExecutionMode(e.target.value as "full_auto" | "semi_auto")}>
                  <option value="full_auto">{lang === "zh" ? "全自动：校验后直接发送并汇报" : "Full auto: send after checks and report"}</option>
                  <option value="semi_auto">{lang === "zh" ? "半自动：发送前汇报并等待确认" : "Semi auto: report and wait before send"}</option>
                </select>
              </label>
              <div className="grid grid-cols-2 gap-4">
                <label className="text-xs text-muted">{t("auto_followup_after")}
                  <input type="number" className="input w-full text-sm mt-1" min={1} max={30} value={followupDays} onChange={e => setFollowupDays(Number(e.target.value))} />
                </label>
                <label className="text-xs text-muted">{t("auto_max_followups")}
                  <input type="number" className="input w-full text-sm mt-1" min={1} max={10} value={maxFollowups} onChange={e => setMaxFollowups(Number(e.target.value))} />
                </label>
              </div>
              <div className="space-y-2">
                <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={stopOnReply} onChange={e => setStopOnReply(e.target.checked)} /> {t("auto_stop_on_reply")}</label>
                <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={stopOnReject} onChange={e => setStopOnReject(e.target.checked)} /> {t("auto_stop_on_reject")}</label>
              </div>
              <div className="flex gap-2">
                <button className="btn-primary flex-1" onClick={handleSave} disabled={saving}>{saving ? t("common_processing") : t("auto_save_enable")}</button>
                <button className="btn" onClick={() => setShowCreate(false)}>{t("common_cancel")}</button>
              </div>
            </div>
          )}

          {automations.length === 0 && !showCreate && <div className="text-sm text-muted py-4">{lang === "zh" ? "还没有 Campaign 自动化" : "No Campaign automations yet"}</div>}
          {automations.map((a: any) => {
            const isSelected = selectedId === a.id;
            const plan = a.plan || {};
            return (
              <div key={a.id} className={`border-t border-border pt-3 space-y-2 ${isSelected ? "ring-2 ring-accent rounded-lg p-3" : ""}`}>
                <div className="flex items-center justify-between flex-wrap gap-2">
                  <div>
                    <h4 className="font-medium text-sm">{campaignName(a.campaign_id)}</h4>
                    <p className="text-xs text-muted">{a.name || `Automation #${a.id}`} · {t("auto_followup_after_days", { days: plan.follow_up_after_days || followupDays })} · {t("auto_max_followups_count", { count: plan.max_follow_ups || maxFollowups })}</p>
                  </div>
                  <span className={`text-xs px-2 py-0.5 rounded ${a.status === "enabled" ? "bg-ok/10 text-ok" : "bg-warn/10 text-warn"}`}>{a.status === "enabled" ? t("auto_enabled_label") : a.status === "disabled" ? (lang === "zh" ? "已禁用" : "Disabled") : t("auto_paused_label")}</span>
                </div>
                <div className="flex items-center gap-4 text-xs text-muted flex-wrap">
                  <span className="flex items-center gap-1"><Users size={12} /> {t("auto_customer_count")}: {a.contact_count ?? "-"}</span>
                  <span className="flex items-center gap-1"><MessageCircle size={12} /> {t("auto_awaiting_reply")}: {a.pending_reply ?? "-"}</span>
                  <span className="flex items-center gap-1"><Clock size={12} /> {t("auto_pending_followup")}: {a.pending_followup ?? "-"}</span>
                </div>
                <div className="flex items-center gap-1.5 flex-wrap">
                  <select className="input text-xs py-1 w-28" value={a.tick_interval_minutes || 60} onChange={e => updateSchedule(a.id, Number(e.target.value))} aria-label={lang === "zh" ? "执行频率" : "Schedule interval"}>
                    <option value={15}>{lang === "zh" ? "每 15 分钟" : "Every 15 min"}</option>
                    <option value={30}>{lang === "zh" ? "每 30 分钟" : "Every 30 min"}</option>
                    <option value={60}>{lang === "zh" ? "每小时" : "Hourly"}</option>
                    <option value={360}>{lang === "zh" ? "每 6 小时" : "Every 6 hours"}</option>
                    <option value={720}>{lang === "zh" ? "每 12 小时" : "Every 12 hours"}</option>
                    <option value={1440}>{lang === "zh" ? "每天" : "Daily"}</option>
                  </select>
                  <select className="input text-xs py-1 w-32" value={runModes[a.id] || a.execution_mode || "full_auto"} onChange={e => setRunModes(previous => ({ ...previous, [a.id]: e.target.value as "full_auto" | "semi_auto" }))} aria-label={lang === "zh" ? "本次执行模式" : "Mode for this run"}>
                    <option value="full_auto">{lang === "zh" ? "全自动" : "Full auto"}</option>
                    <option value="semi_auto">{lang === "zh" ? "半自动" : "Semi auto"}</option>
                  </select>
                  <button className="btn text-xs flex items-center gap-1" disabled={busyIds[a.id]} onClick={() => doAction(a.id, "run-now")}><Play size={12} /> {t("auto_run_now")}</button>
                  {a.status === "enabled" ? <button className="btn text-xs flex items-center gap-1" onClick={() => doAction(a.id, "pause")}><Pause size={12} /> {t("auto_pause")}</button> : <button className="btn text-xs flex items-center gap-1" onClick={() => doAction(a.id, "enable")}><Play size={12} /> {t("auto_resume")}</button>}
                  <button className="btn text-xs flex items-center gap-1 text-danger" onClick={() => setConfirmDeleteId(a.id)} title={lang === "zh" ? "删除这个自动跟进" : "Delete this automation"}><Trash2 size={12} /> {t("common_delete")}</button>
                  <button className="btn text-xs ml-auto" onClick={() => setSelectedId(selectedId === a.id ? null : a.id)}>{isSelected ? (lang === "zh" ? "取消选中" : "Deselect") : (lang === "zh" ? "查看详情" : "Details")}</button>
                </div>
                {isSelected && <div className="border-t border-border pt-2 space-y-1 text-xs text-muted"><div>{t("auto_next_run")}: {a.schedule_owner === "agent_takeover" ? (lang === "zh" ? "已由 Agent 接管，Huey 不执行" : "Owned by Agent Takeover; Huey does not run it") : displayTime(a, "next_run_at")}</div><div>{t("auto_last_result")}: {a.last_status || "-"}</div></div>}
              </div>
            );
          })}
        </>
      )}

      {Object.entries(runIds).filter(([id]) => automations.some(item => item.id === Number(id))).map(([id, runId]) => <CampaignRun key={`${id}:${runId}`} runId={runId} onChanged={onChanged} />)}

      {confirmDeleteId !== null && (
        <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
          <div className="card max-w-sm w-full mx-4 space-y-3">
            <h3 className="font-semibold text-danger">{t("common_confirm_delete")}</h3>
            <p className="text-sm text-muted">{t("common_delete_warning")}</p>
            <div className="flex gap-2"><button className="btn-danger flex-1" disabled={deletingId === confirmDeleteId} onClick={() => doDelete(confirmDeleteId)}>{deletingId === confirmDeleteId ? t("common_processing") : t("common_confirm")}</button><button className="btn flex-1" onClick={() => setConfirmDeleteId(null)}>{t("common_cancel")}</button></div>
          </div>
        </div>
      )}
    </section>
  );
}
