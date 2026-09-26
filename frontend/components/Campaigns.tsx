"use client";

import { useCallback, useEffect, useState } from "react";
import { Bot, Check, Edit, Pause, Play, Plus, Search, Send, Trash2, UserPlus } from "lucide-react";
import { api } from "@/lib/api";
import { useLang } from "@/lib/i18n";
import CampaignAutomationPanel from "./CampaignAutomation";

type Tab = "overview" | "inbox" | "contacts" | "campaigns" | "approvals" | "activity";
const EMPTY = { name: "", objective: "", product_description: "", target_audience: "", sender_name: "", sender_company: "" };

export default function CampaignsView({ onChanged, onNavigate }: { onChanged: () => void; onNavigate: (tab: Tab) => void }) {
  const { lang } = useLang();
  const zh = lang === "zh";
  const [campaigns, setCampaigns] = useState<any[]>([]);
  const [counts, setCounts] = useState<Record<number, number>>({});
  const [generation, setGeneration] = useState<Record<number, any>>({});
  const [form, setForm] = useState<any | null>(null);
  const [picker, setPicker] = useState<any | null>(null);
  const [contacts, setContacts] = useState<any[]>([]);
  const [members, setMembers] = useState<any[]>([]);
  const [selected, setSelected] = useState<Set<number>>(new Set());
  const [search, setSearch] = useState("");
  const [category, setCategory] = useState("");
  const [segmentFilters, setSegmentFilters] = useState<Set<string>>(new Set());
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    try {
      const rows = await api.campaigns(); setCampaigns(rows);
      const values = await Promise.all(rows.map(async (c:any) => [c.id, (await api.campaignContacts(c.id)).length, await api.campaignGenerationStatus(c.id)]));
      setCounts(Object.fromEntries(values.map(([id, count]) => [id, count])));
      setGeneration(Object.fromEntries(values.map(([id, _count, status]) => [id, status])));
    } catch (e:any) { setError(e.message); }
  }, []);
  useEffect(() => { load(); }, [load]);

  const save = async () => {
    if (!Number.isInteger(Number(form.daily_send_limit ?? 20)) || Number(form.daily_send_limit ?? 20) < 1) {
      setError(zh ? "每日发送上限必须是大于 0 的整数" : "Daily send limit must be an integer greater than 0"); return;
    }
    if (!Number.isInteger(Number(form.max_follow_ups ?? 2)) || Number(form.max_follow_ups ?? 2) < 0) {
      setError(zh ? "最大跟进次数必须是非负整数" : "Max follow-ups must be a non-negative integer"); return;
    }
    if (!form.name?.trim()) { setError(zh?"请填写活动名称":"Campaign name is required"); return; }
    try { if (form.id) await api.updateCampaign(form.id, form); else await api.createCampaign(form); setForm(null); await load(); onChanged(); }
    catch(e:any){setError(e.message);}
  };
  const openPicker = async (campaign:any) => {
    try {
      const [all, campaignMembers] = await Promise.all([api.contacts(), api.campaignContacts(campaign.id, true)]);
      const ids = new Set(campaignMembers.filter((m:any)=>m.membership_active).map((m:any)=>m.id));
      setContacts(all.filter((c:any)=>!ids.has(c.id) && c.category !== "invalid" && !["unsubscribed","not_interested","bounced","archived"].includes(c.status)));
      setMembers(campaignMembers);
      setPicker(campaign); setSelected(new Set()); setSearch(""); setCategory(""); setSegmentFilters(new Set());
    } catch(e:any){setError(e.message);}
  };
  const addSelected = async () => {
    if (!picker || !selected.size) return;
    setBusy(true);
    try { await api.addCampaignContacts(picker.id, [...selected]); setSelected(new Set()); await openPicker(picker); await load(); onChanged(); }
    catch(e:any){setError(e.message);} finally {setBusy(false);}
  };
  const removeMember = async (contact:any) => {
    if (!picker) return;
    const message = zh
      ? "移出后，该联系人不会继续收到此 Campaign 的后续邮件。历史邮件、Draft、Approval 和审计记录会保留；不会删除 Contact，也不会加入 Suppression。"
      : "This contact will receive no further emails from this Campaign. Sent mail, Draft, Approval and audit history will be preserved. The Contact will not be deleted or suppressed.";
    if (!confirm(message)) return;
    setBusy(true); setError("");
    try {
      const result = await api.removeCampaignContact(picker.id, contact.id);
      if (result.cancelled_pending_items) {
        setError(zh
          ? `已移出联系人，并取消 ${result.cancelled_pending_items} 个未完成项目。`
          : `Contact removed; ${result.cancelled_pending_items} pending item(s) cancelled.`);
      }
      await openPicker(picker); await load(); onChanged();
    } catch(e:any) { setError(e.message); } finally { setBusy(false); }
  };
  const addMember = async (contactId:number) => {
    if (!picker) return;
    setBusy(true); setError("");
    try {
      await api.addCampaignContacts(picker.id, [contactId]);
      await openPicker(picker); await load(); onChanged();
    } catch(e:any) { setError(e.message); } finally { setBusy(false); }
  };
  const action = async (c:any, name:string) => {
    setBusy(true); setError("");
    try {
      if(name==="generate") setGeneration(prev => ({...prev, [c.id]: {...prev[c.id], status: "running"}}));
      const result = name==="generate" ? await api.generate(c.id) : null;
      if (result) setGeneration(prev => ({...prev, [c.id]: result}));
      if(name==="pause") await api.pauseCampaign(c.id);
      if(name==="resume") await api.startCampaign(c.id);
      await load(); onChanged();
      if(name==="generate") onNavigate("approvals");
    } catch(e:any){setError(e.message);} finally{setBusy(false);}
  };

  const eligible = contacts.filter(c => {
    const text=`${c.email} ${c.first_name||""} ${c.company||""} ${(c.tags||[]).join(" ")} ${(c.segments||[]).join(" ")}`.toLowerCase();
    return (!search||text.includes(search.toLowerCase()))&&(!category||c.category===category)&&(!segmentFilters.size||(c.segments||[]).some((value:string)=>segmentFilters.has(value)));
  });
  const segmentOptions=Array.from(new Set(contacts.flatMap(c=>c.segments||[]))).sort((a:any,b:any)=>String(a).localeCompare(String(b)));
  const toggleSegment=(value:string)=>setSegmentFilters(prev=>{const next=new Set(prev);next.has(value)?next.delete(value):next.add(value);return next;});

  return <div className="space-y-4">
    <div className="flex items-center gap-2"><h2 className="text-lg font-semibold">{zh?"营销活动":"Campaigns"}</h2><button className="btn-primary flex items-center gap-1" onClick={()=>setForm({...EMPTY})}><Plus size={14}/>{zh?"创建活动":"Create Campaign"}</button></div>
    {error&&<div className="bg-danger/10 text-danger text-sm rounded p-2">{error}</div>}
    {campaigns.length===0?<div className="text-center py-16 text-muted"><Send className="mx-auto mb-2"/><p>{zh?"还没有营销活动":"No campaigns yet"}</p></div>:
      <div className="space-y-3">{campaigns.map(c=><div className="card space-y-3" key={c.id}>
        <div className="flex items-start justify-between gap-3"><div><h3 className="font-medium">{c.name}</h3><p className="text-xs text-muted">{c.objective||c.product_description||"-"}</p></div><span className={`text-xs px-2 py-0.5 rounded ${c.status==="active"?"bg-ok/10 text-ok":c.status==="paused"?"bg-warn/10 text-warn":"bg-panel2 text-muted"}`}>{c.status}</span></div>
        <div className="grid grid-cols-3 gap-2 text-sm"><div className="bg-panel2 rounded p-2"><b>{counts[c.id]||0}</b><div className="text-xs text-muted">{zh?"当前参与联系人":"Active contacts"}</div></div><div className="bg-panel2 rounded p-2" title={zh?"本 Campaign 每天首封和跟进邮件的合计上限":"Daily total of first sends and follow-ups for this Campaign"}><b>{c.daily_send_limit}</b><div className="text-xs text-muted">{zh?"每日发送上限":"Daily send limit"}</div><div className="text-[11px] text-muted">{zh?"首封 + 跟进合计":"First sends + follow-ups"}</div></div><div className="bg-panel2 rounded p-2" title={zh?"每位联系人在首封后最多追加的跟进次数":"Maximum follow-ups per contact after the first email"}><b>{c.max_follow_ups}</b><div className="text-xs text-muted">{zh?"每位联系人最大跟进次数":"Max follow-ups per contact"}</div><div className="text-[11px] text-muted">{zh?"不包含首封":"Excludes first email"}</div></div></div>
         {generation[c.id]?.status && generation[c.id].status !== "idle" && <div className={`text-xs rounded px-2 py-1 ${generation[c.id].status === "failed" ? "bg-danger/10 text-danger" : generation[c.id].status === "partial" ? "bg-warn/10 text-warn" : "bg-panel2 text-muted"}`}>
           {zh ? "邮件生成" : "Email generation"}: {generation[c.id].status} · {zh ? "成功" : "generated"} {generation[c.id].generated || 0} · {zh ? "失败" : "failed"} {generation[c.id].failed || 0}
           {generation[c.id].send_status && generation[c.id].send_status !== "not_requested" && ` · ${zh ? "Agent发送" : "Agent send"}: ${generation[c.id].send_status} · ${zh ? "已发送" : "sent"} ${generation[c.id].sent || 0} · ${zh ? "发送失败" : "send failed"} ${generation[c.id].send_failed || 0}`}
           {generation[c.id].run_id ? ` · Run ${generation[c.id].run_id}` : ""}
         </div>}
        <div className="flex gap-2 flex-wrap">
          <button className="btn flex items-center gap-1" onClick={()=>openPicker(c)}><UserPlus size={13}/>{zh?"管理联系人":"Manage Contacts"}</button>
          <button className="btn flex items-center gap-1" disabled={!counts[c.id]||busy||c.status!=="active"} onClick={()=>action(c,"generate")}><Bot size={13}/>{zh?"AI 生成邮件":"Generate Emails"}</button>
          <button className="btn flex items-center gap-1" onClick={()=>onNavigate("approvals")}><Send size={13}/>{zh?"审核与发送":"Review & Send"}</button>
          <button className="btn p-1.5" title={zh?"编辑":"Edit"} onClick={()=>setForm({...c})}><Edit size={13}/></button>
          {c.status==="active"?<button className="btn p-1.5" title={zh?"暂停":"Pause"} onClick={()=>action(c,"pause")}><Pause size={13}/></button>:c.status!=="archived"?<button className="btn p-1.5" title={zh?"启用":"Activate"} onClick={()=>action(c,"resume")}><Play size={13}/></button>:null}
          <button className="btn p-1.5 text-danger" title={zh?"删除":"Delete"} onClick={async()=>{if(confirm(zh?"归档此营销活动？":"Archive this campaign?")){await api.deleteCampaign(c.id);await load();}}}><Trash2 size={13}/></button>
        </div>
      </div>)}</div>}

    <CampaignAutomationPanel onChanged={onChanged} onNavigate={() => onNavigate("campaigns")} />

    {form&&<div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4"><div className="card max-w-xl w-full space-y-3"><h3 className="font-semibold">{form.id?(zh?"编辑营销活动":"Edit Campaign"):(zh?"创建营销活动":"Create Campaign")}</h3>
      <label className="text-xs text-muted">{zh?"活动名称 *":"Campaign name *"}<input className="input w-full mt-1" value={form.name||""} onChange={e=>setForm({...form,name:e.target.value})}/></label>
      <label className="text-xs text-muted">{zh?"目标":"Objective"}<input className="input w-full mt-1" value={form.objective||""} onChange={e=>setForm({...form,objective:e.target.value})}/></label>
      <label className="text-xs text-muted">{zh?"产品或服务介绍":"Product or service"}<textarea className="input w-full mt-1" value={form.product_description||""} onChange={e=>setForm({...form,product_description:e.target.value})}/></label>
      <label className="text-xs text-muted">{zh?"目标客户描述":"Target audience"}<textarea className="input w-full mt-1" value={form.target_audience||""} onChange={e=>setForm({...form,target_audience:e.target.value})}/></label>
      <div className="grid grid-cols-2 gap-2"><label className="text-xs text-muted">{zh?"发件人姓名":"Sender name"}<input className="input w-full mt-1" value={form.sender_name||""} onChange={e=>setForm({...form,sender_name:e.target.value})}/></label><label className="text-xs text-muted">{zh?"发件公司":"Sender company"}<input className="input w-full mt-1" value={form.sender_company||""} onChange={e=>setForm({...form,sender_company:e.target.value})}/></label></div>
      <div className="grid grid-cols-2 gap-2">
        <label className="text-xs text-muted">{zh ? "每日发送上限" : "Daily send limit"}<input type="number" min={1} step={1} className="input w-full mt-1" value={form.daily_send_limit ?? 20} onChange={e=>setForm({...form,daily_send_limit:Number(e.target.value)})}/><span className="block mt-1">{zh ? "每天首封与跟进邮件的合计上限" : "Daily total of first sends and follow-ups"}</span></label>
        <label className="text-xs text-muted">{zh ? "每位联系人最大跟进次数" : "Max follow-ups per contact"}<input type="number" min={0} step={1} className="input w-full mt-1" value={form.max_follow_ups ?? 2} onChange={e=>setForm({...form,max_follow_ups:Number(e.target.value)})}/><span className="block mt-1">{zh ? "不包含首封；0 表示不自动跟进" : "Excludes first email; 0 disables follow-ups"}</span></label>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <label className="text-xs text-muted">{zh ? "跟进间隔（天）" : "Follow-up intervals (days)"}<input className="input w-full mt-1" value={form.follow_up_intervals_days ?? "3,4"} onChange={e=>setForm({...form,follow_up_intervals_days:e.target.value})}/></label>
        <label className="text-xs text-muted">{zh ? "时区" : "Timezone"}<input className="input w-full mt-1" value={form.timezone ?? "Asia/Shanghai"} onChange={e=>setForm({...form,timezone:e.target.value})}/></label>
      </div>
      <div className="grid grid-cols-2 gap-2">
        <label className="text-xs text-muted">{zh ? "发送窗口开始" : "Sending window start"}<input type="number" min={0} max={23} className="input w-full mt-1" value={form.sending_window_start ?? 9} onChange={e=>setForm({...form,sending_window_start:Number(e.target.value)})}/></label>
        <label className="text-xs text-muted">{zh ? "发送窗口结束" : "Sending window end"}<input type="number" min={1} max={24} className="input w-full mt-1" value={form.sending_window_end ?? 18} onChange={e=>setForm({...form,sending_window_end:Number(e.target.value)})}/></label>
      </div>
      <div className="flex gap-2"><button className="btn-primary" onClick={save}>{zh?"保存":"Save"}</button><button className="btn" onClick={()=>setForm(null)}>{zh?"取消":"Cancel"}</button></div>
    </div></div>}

    {picker&&<div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4"><div className="card max-w-4xl w-full space-y-3 max-h-[90vh] flex flex-col"><div><h3 className="font-semibold">{zh?"管理 Campaign 联系人":"Manage Campaign Contacts"}</h3><p className="text-xs text-muted">{picker.name}</p></div>
      <section className="space-y-2"><h4 className="text-sm font-medium">{zh?"当前参与联系人":"Active contacts"} ({members.filter((m:any)=>m.membership_active).length})</h4><div className="max-h-48 overflow-auto border border-border rounded">{members.filter((m:any)=>m.membership_active).length===0?<div className="p-4 text-sm text-muted">{zh?"当前没有参与联系人":"No active contacts"}</div>:members.filter((m:any)=>m.membership_active).map((m:any)=>{const name=[m.first_name,m.last_name].filter(Boolean).join(" ");return <div key={m.id} className="p-3 border-b border-border last:border-0 flex items-center gap-3"><div className="flex-1 min-w-0"><div className="font-medium truncate">{[name,m.company].filter(Boolean).join(" · ")||m.email}</div><div className="text-xs text-muted truncate">{m.email} · {m.campaign_contact_status} · {zh?"已发送":"sent"} {m.sent_count} · {zh?"已跟进":"follow-ups"} {m.assigned_follow_ups}{m.pending_approval?` · ${zh?"有待审批":"pending approval"}`:""}</div></div><button className="btn text-danger" disabled={busy} onClick={()=>removeMember(m)}>{zh?"移出 Campaign":"Remove"}</button></div>})}</div></section>
      {members.some((m:any)=>!m.membership_active)&&<details className="text-sm"><summary className="cursor-pointer text-muted">{zh?"已移出历史联系人":"Removed history"} ({members.filter((m:any)=>!m.membership_active).length})</summary><div className="mt-2 max-h-32 overflow-auto border border-border rounded">{members.filter((m:any)=>!m.membership_active).map((m:any)=><div key={m.id} className="p-2 border-b border-border last:border-0 flex items-center gap-2"><span className="flex-1">{[m.first_name,m.last_name,m.company].filter(Boolean).join(" · ")||m.email}<span className="block text-xs text-muted">{m.email} · {m.removed_reason||"removed"}</span></span><button className="btn" disabled={busy} onClick={()=>addMember(m.id)}>{zh?"重新加入":"Re-add"}</button></div>)}</div></details>}
      <section className="space-y-2 min-h-0 flex flex-col"><h4 className="text-sm font-medium">{zh?"添加联系人":"Add contacts"}</h4><div className="flex gap-2"><div className="input flex flex-1 gap-2 items-center"><Search size={14}/><input className="bg-transparent outline-none w-full" value={search} onChange={e=>setSearch(e.target.value)} placeholder={zh?"搜索联系人、标签或客户分群":"Search contacts, tags or segments"}/></div><select className="input" value={category} onChange={e=>setCategory(e.target.value)}><option value="">{zh?"全部系统分类":"All system categories"}</option>{["prospect","qualified","customer","partner","won"].map(v=><option key={v}>{v}</option>)}</select></div>
      {segmentOptions.length>0&&<div className="flex gap-1 flex-wrap"><span className="text-xs text-muted self-center mr-1">{zh?"客户分群（任一匹配）：":"Segments (match any):"}</span>{segmentOptions.map(value=><button type="button" key={value} onClick={()=>toggleSegment(value)} className={`text-xs border rounded px-2 py-1 ${segmentFilters.has(value)?"border-accent bg-accent/10 text-accent":"border-border text-muted"}`}>{value}</button>)}{segmentFilters.size>0&&<button type="button" onClick={()=>setSegmentFilters(new Set())} className="text-xs text-muted px-1">{zh?"清除":"Clear"}</button>}</div>}
      <div className="overflow-auto border border-border rounded flex-1 min-h-24">{eligible.length===0?<div className="p-6 text-center text-muted">{zh?"没有可添加的联系人":"No eligible contacts to add."}</div>:eligible.map(c=>{const name=[c.first_name,c.last_name].filter(Boolean).join(" ");const headline=[name,c.company].filter(Boolean).join(" · ")||c.email;return <button key={c.id} onClick={()=>setSelected(prev=>{const n=new Set(prev);n.has(c.id)?n.delete(c.id):n.add(c.id);return n;})} className="w-full text-left p-3 border-b border-border last:border-0 flex items-center gap-3 hover:bg-panel2"><span className={`w-5 h-5 border rounded flex items-center justify-center ${selected.has(c.id)?"bg-accent border-accent text-white":"border-border"}`}>{selected.has(c.id)&&<Check size={13}/>}</span><div className="flex-1"><div className="text-sm font-medium">{headline}</div><div className="text-xs text-muted">{c.email} · {c.category} · {(c.segments||[]).join(", ")} · {(c.tags||[]).join(", ")}</div></div></button>})}</div></section>
      <div className="flex gap-2"><button className="btn-primary" disabled={!selected.size||busy} onClick={addSelected}>{zh?`加入 ${selected.size} 位联系人`:`Add ${selected.size} contacts`}</button><button className="btn" onClick={()=>setPicker(null)}>{zh?"关闭":"Close"}</button><button className="text-sm text-accent ml-auto" onClick={()=>{setPicker(null);onNavigate("contacts")}}>{zh?"前往联系人页面":"Open Contacts"}</button></div>
    </div></div>}
  </div>;
}
