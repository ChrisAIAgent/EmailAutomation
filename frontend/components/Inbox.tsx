"use client";

import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle, Bot, ChevronLeft, Download, Mail, MessageCircle,
  RefreshCw, Search, Sparkles, UserPlus,
} from "lucide-react";
import { api, API_BASE } from "@/lib/api";
import { useLang } from "@/lib/i18n";

type Filter = "action" | "new" | "waiting" | "followup" | "stopped" | "filtered" | "all" | "human_review";

export default function InboxView({ onChanged }: { onChanged: () => void }) {
  const { lang, formatDate } = useLang();
  const zh = lang === "zh";
  const [customers, setCustomers] = useState<any[]>([]);
  const [stats, setStats] = useState<any>(null);
  const [gmail, setGmail] = useState<any>(null);
  const [filter, setFilter] = useState<Filter>("action");
  const [search, setSearch] = useState("");
  const [selected, setSelected] = useState<any | null>(null);
  const [thread, setThread] = useState<any | null>(null);
  const [syncing, setSyncing] = useState(false);
  const [fullScanning, setFullScanning] = useState(false);
  const [sorting, setSorting] = useState(false);
  const [replyLoading, setReplyLoading] = useState(false);
  const [reply, setReply] = useState<any>(null);
  const [reviewBusy, setReviewBusy] = useState<"approve" | "reject" | "agent_decide" | null>(null);
  const [contactForm, setContactForm] = useState<any | null>(null);
  const [tagEditor, setTagEditor] = useState(false);
  const [tagText, setTagText] = useState("");
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);

  const load = useCallback(async () => {
    try {
      const [rows, inboxStats, gmailStatus] = await Promise.all([
        api.inboxCustomers(), api.inboxStats(), api.gmailStatus(),
      ]);
      setCustomers(Array.isArray(rows) ? rows : []);
      setStats(inboxStats); setGmail(gmailStatus);
    } catch (e: any) { setError(e?.message || String(e)); }
  }, []);
  useEffect(() => { load(); }, [load]);

  // Live updates: when the Agent (TACWork chat) or any background job sorts,
  // syncs or analyzes the inbox, the backend publishes an SSE event. Re-fetch so
  // the left list reflects the new triage without a manual page reload.
  useEffect(() => {
    const es = new EventSource(`${API_BASE}/api/events`);
    es.onmessage = () => { load(); };
    return () => es.close();
  }, [load]);

  const sync = async (full = false) => {
    full ? setFullScanning(true) : setSyncing(true); setError("");
    try { await api.gmailSync(full); await load(); onChanged(); }
    catch (e: any) { setError(e?.message || String(e)); }
    finally { full ? setFullScanning(false) : setSyncing(false); }
  };
  const sortAll = async () => {
    setSorting(true); setError("");
    try { await api.sortInbox(); await load(); setFilter("action"); onChanged(); }
    catch (e: any) { setError(e?.message || String(e)); }
    finally { setSorting(false); }
  };
  const refresh = async () => {
    setRefreshing(true); setError("");
    try { await load(); onChanged(); }
    catch (e: any) { setError(e?.message || String(e)); }
    finally { setRefreshing(false); }
  };
  const openThread = async (id: number) => {
    try { setReply(null); setThread(await api.threadDetail(id)); }
    catch (e: any) { setError(e?.message || String(e)); }
  };
  const generateReply = async () => {
    if (!thread) return;
    setReplyLoading(true);
    try { setReply(await api.generateReply(thread.id)); onChanged(); }
    catch (e: any) { setError(e?.message || String(e)); }
    finally { setReplyLoading(false); }
  };
  const addContact = async () => {
    if (!thread || !contactForm) return;
    if (!contactForm.first_name.trim() && !contactForm.company.trim()) {
      setError(zh ? "请填写姓名或公司名称" : "Name or company is required"); return;
    }
    try {
      await api.addInboxContact(thread.id, contactForm);
      setContactForm(null); await load(); onChanged();
      const latest = await api.inboxCustomers();
      setSelected(latest.find((item: any) => item.email === selected.email) || selected);
    } catch (e: any) { setError(e?.message || String(e)); }
  };
  const resolveReview = async (decision: "approve" | "reject" | "agent_decide") => {
    if (!thread) return;
    if (decision === "approve" && thread.review_kind === "contact_admission_uncertain") {
      setContactForm({ first_name:"", company:"", category:"qualified", tags:[] });
      return;
    }
    setReviewBusy(decision); setError("");
    try {
      await api.resolveHumanReview(thread.id, decision);
      await load(); onChanged();
      setThread(await api.threadDetail(thread.id));
    } catch (e: any) { setError(e?.message || String(e)); }
    finally { setReviewBusy(null); }
  };
  const clearStaleReview = async () => {
    if (!thread) return;
    setReviewBusy("agent_decide"); setError("");
    try {
      await api.clearStaleInboxReview(thread.id);
      await load(); onChanged();
      setThread(await api.threadDetail(thread.id));
    } catch (e: any) { setError(e?.message || String(e)); }
    finally { setReviewBusy(null); }
  };
  const saveTags = async () => {
    if (!selected?.contact_id) return;
    const tags = tagText.split(/[,，]/).map((value:string) => value.trim()).filter(Boolean);
    try {
      await api.updateContact(selected.contact_id, { tags });
      setTagEditor(false); await load(); onChanged();
      const latest = await api.inboxCustomers();
      setSelected(latest.find((item:any) => item.email === selected.email) || selected);
    } catch (e:any) { setError(e?.message || String(e)); }
  };

  const reviewCount = customers.filter((c:any) => Boolean(c.threads?.some((t:any) => t.pending_action === "human_review"))).length;
  const visible = customers.filter(item => {
    const haystack = `${item.first_name || ""} ${item.last_name || ""} ${item.company || ""} ${item.email} ${(item.tags || []).join(" ")} ${item.latest_subject || ""}`.toLowerCase();
    if (search && !haystack.includes(search.toLowerCase())) return false;
    if (filter === "action") return item.threads?.some((t:any) => !t.processed) || item.needs_reply || item.next_action === "reply";
    if (filter === "new") return Boolean(item.contact_id) && item.lifecycle_stage === "new_customer";
    if (filter === "waiting") return Boolean(item.contact_id) && item.next_action === "waiting_for_customer";
    if (filter === "followup") return Boolean(item.contact_id) && item.next_action === "follow_up" && Boolean(item.next_follow_up_at);
    if (filter === "stopped") return Boolean(item.contact_id) && item.lifecycle_stage === "stopped";
    if (filter === "human_review") return Boolean(item.threads?.some((t:any) => t.pending_action === "human_review"));
    if (filter === "filtered") return !item.contact_id && ["filtered", "irrelevant"].includes(item.latest_category);
    // A Contact with at least one Gmail thread belongs to the CRM customer
    // queue even when its latest thread was previously classified as filtered.
    // Manual Contact creation is an explicit human override of that exclusion.
    if (filter === "all") return Boolean(item.contact_id);
    return true;
  });

  const identity = (item: any) => {
    const name = [item.first_name, item.last_name].filter(Boolean).join(" ");
    return { headline: [name, item.company].filter(Boolean).join(" · ") || item.email, showEmail: Boolean(name || item.company) };
  };
  const stageText = (stage: string) => ({
    new_customer: zh ? "新客户" : "New customer",
    contacted: zh ? "已联系" : "Contacted",
    awaiting_reply: zh ? "等待回复" : "Awaiting reply",
    needs_reply: zh ? "客户已回复" : "Needs reply",
    following_up: zh ? "跟进中" : "Following up",
    won: zh ? "已成交" : "Won",
    stopped: zh ? "已停止" : "Stopped",
    unclassified: zh ? "待分拣" : "Unclassified",
  } as Record<string,string>)[stage] || stage;
  const actionText = (action: string) => ({
    reply: zh ? "生成客户回复" : "Draft reply",
    follow_up: zh ? "安排跟进" : "Follow up",
    waiting_for_customer: zh ? "等待客户回复" : "Waiting for customer",
    review: zh ? "人工判断" : "Review",
    send_intro: zh ? "发送首次邮件" : "Send introduction",
    human_review: zh ? "需要人工处理" : "Human review",
    none: zh ? "无需操作" : "No action",
  } as Record<string,string>)[action] || action || (zh ? "待判断" : "Review");

  if (selected) {
    const id = identity(selected);
    const orderedThreads = [...(selected.threads || [])].sort((a:any, b:any) =>
      new Date(b.latest_message_at || b.updated_at).getTime() - new Date(a.latest_message_at || a.updated_at).getTime()
    );
    return <div className="space-y-4">
      <button className="text-sm text-muted flex items-center gap-1" onClick={() => { setSelected(null); setThread(null); setReply(null); }}>
        <ChevronLeft size={14}/>{zh ? "返回客户队列" : "Back to customer queue"}
      </button>
      <div className="flex items-start justify-between gap-4 border-b border-border pb-4">
        <div><h2 className="text-lg font-semibold">{id.headline}</h2>{id.showEmail && <p className="text-sm text-muted">{selected.email}</p>}</div>
        <div className="text-right"><div className="text-sm font-medium">{stageText(selected.lifecycle_stage)}</div><div className="text-xs text-accent">{actionText(selected.next_action)}</div></div>
      </div>
      <div className="grid grid-cols-[220px_minmax(0,1fr)_260px] gap-4">
        <section className="space-y-2">
          <h3 className="text-xs uppercase text-muted">{zh ? `会话 (${selected.thread_count})` : `Conversations (${selected.thread_count})`}</h3>
          {orderedThreads.map((item:any) => <button key={item.id} onClick={() => openThread(item.id)} className={`w-full text-left p-2 rounded border ${thread?.id === item.id ? "border-accent bg-accent/10" : "border-border hover:bg-panel2"}`}>
            <div className="text-sm font-medium truncate">{item.subject || (zh ? "无主题" : "No subject")}</div>
            <div className="text-xs text-muted mt-1">{item.message_count} {zh ? "封邮件" : "messages"} · {formatDate(item.latest_message_at || item.updated_at)}</div>
          </button>)}
        </section>
        <section className="min-w-0">
          {!thread ? <div className="py-20 text-center text-muted"><Mail className="mx-auto mb-2"/><p className="text-sm">{zh ? "选择左侧会话查看完整邮件" : "Select a conversation to view messages"}</p></div> :
          <div className="space-y-3">
            <div><h3 className="font-semibold">{thread.subject || (zh ? "无主题" : "No subject")}</h3><p className="text-xs text-muted">{thread.messages?.length || 0} {zh ? "封邮件" : "messages"}</p></div>
            <div className="space-y-3 max-h-[55vh] overflow-y-auto pr-1">
              {thread.messages?.map((message:any) => <div key={message.id} className={`p-3 rounded ${message.is_incoming ? "bg-panel2" : "bg-accent/10"}`}>
                <div className="flex justify-between gap-2 text-xs text-muted mb-2"><span>{message.is_incoming ? selected.email : (zh ? "我方发送" : "Sent by us")}</span><span>{formatDate(message.received_at)}</span></div>
                <div className="text-sm whitespace-pre-wrap">{message.body_text || message.snippet}</div>
              </div>)}
            </div>
            {thread.review_guidance ? <div className="border border-warn/40 bg-warn/10 rounded p-4 space-y-3">
              <div className="flex items-start gap-2"><AlertTriangle className="text-warn shrink-0 mt-0.5" size={17}/><div><h4 className="font-semibold">{thread.review_guidance.title}</h4><p className="text-sm text-muted mt-1">{thread.review_guidance.reason}</p></div></div>
              <div className="text-sm"><span className="text-muted">{zh ? "Agent 建议：" : "Agent recommendation: "}</span>{thread.review_guidance.recommendation}</div>
              {thread.review_kind === "stale_review" ? <div className="flex flex-wrap gap-2">
                <button className="btn-primary" disabled={!!reviewBusy} onClick={clearStaleReview}>{reviewBusy ? (zh ? "处理中…" : "Working…") : (zh ? "清理过期判断" : "Clear stale review")}</button>
              </div> : thread.review_kind === "opt_out_confirmation" ? <div className="flex flex-wrap gap-2">
                <button className="btn-primary" disabled={!!reviewBusy} onClick={()=>resolveReview("approve")}>{zh ? "批准停止联系" : "Approve stop request"}</button>
                <button className="btn" disabled={!!reviewBusy} onClick={()=>resolveReview("reject")}>{zh ? "拒绝停止联系" : "Reject stop request"}</button>
              </div> : thread.review_kind === "content_uncertain" && thread.is_contact ? <div className="flex flex-wrap gap-2">
                <button className="btn-primary" disabled={!!reviewBusy} onClick={()=>resolveReview("approve")}>{reviewBusy ? (zh ? "处理中…" : "Working…") : (zh ? "确认无需动作，保留联系人" : "Confirm no action; keep Contact")}</button>
              </div> : <>
                <div className="text-xs text-muted">{zh ? "这一层只决定是否进入联系人。Approve 由你录入；Reject 后同一邮箱将在分拣层过滤；Agent Decide 仅在身份和业务信息明确时自动建联打标。不会生成回复或发送邮件。" : "This gate only controls Contact admission. Approve opens manual entry; Reject filters this sender; Agent Decide creates and tags only with clear evidence. It never drafts or sends."}</div>
                <div className="flex flex-wrap gap-2">
                  <button className="btn-primary" disabled={!!reviewBusy} onClick={()=>resolveReview("approve")}>{zh?"Approve · 我来录入":"Approve · Enter manually"}</button>
                  <button className="btn" disabled={!!reviewBusy} onClick={()=>resolveReview("reject")}>{reviewBusy==="reject" ? (zh?"处理中…":"Working…") : (zh?"Reject · 不进入联系人":"Reject · Filter sender")}</button>
                  <button className="btn" disabled={!!reviewBusy} onClick={()=>resolveReview("agent_decide")}>{reviewBusy==="agent_decide" ? (zh?"判断中…":"Deciding…") : (zh?"Agent Decide · 自动建联打标":"Agent Decide · Create & tag")}</button>
                </div>
              </>}
            </div> : !reply && thread.category !== "rejected" && thread.category !== "awaiting_reply" && <button className="btn-primary flex items-center gap-1.5" disabled={replyLoading || thread.pending_action !== "reply"} onClick={generateReply}><MessageCircle size={14}/>{replyLoading ? (zh ? "正在生成…" : "Generating…") : (zh ? "AI 生成回复" : "AI Draft Reply")}</button>}
            {reply && <div className="border border-ok/30 bg-ok/10 rounded p-3 text-sm"><b>{zh ? "回复已进入审批" : "Reply sent to approval"}</b><div className="mt-2">{reply.subject}</div></div>}
          </div>}
        </section>
        <aside className="border-l border-border pl-4 space-y-4">
          <div><h3 className="text-xs uppercase text-muted mb-2">{zh ? "联系人资料" : "Contact"}</h3>
            <div className="text-sm space-y-1"><div>{selected.first_name || selected.last_name || selected.company || selected.email}</div>{selected.company && <div>{selected.company}</div>}<div className="text-muted">{selected.email}</div></div>
          </div>
          {selected.contact_id ? <>
            <div><div className="text-xs text-muted mb-1">{zh ? "分类与意向" : "Category & intent"}</div><div className="text-sm">{selected.category || "-"} · {selected.intent_level}</div></div>
            <div><div className="flex items-center justify-between gap-2 text-xs text-muted mb-1"><span>{zh ? "标签" : "Tags"}</span><button className="text-accent hover:underline" onClick={()=>{setTagText((selected.tags||[]).join(", "));setTagEditor(true);}}>{zh ? "编辑标签" : "Edit tags"}</button></div><div className="flex flex-wrap gap-1">{(selected.tags || []).length ? (selected.tags || []).map((tag:string)=><span key={tag} className="text-xs border border-border px-1.5 rounded">{tag}</span>) : <span className="text-xs text-muted">{zh ? "暂无标签" : "No tags"}</span>}</div></div>
            <div><div className="text-xs text-muted mb-1">{zh ? "销售阶段" : "Sales stage"}</div><div className="text-sm">{stageText(selected.lifecycle_stage)}{selected.manual_lock && <span className="ml-2 text-xs text-warn">{zh ? "人工锁定" : "Locked"}</span>}</div></div>
            <div><div className="text-xs text-muted mb-1">{zh ? "下一步" : "Next action"}</div><div className="text-sm text-accent">{actionText(selected.next_action)}</div></div>
          </> : <button className="btn flex items-center gap-1" disabled={!thread} onClick={() => setContactForm({ first_name:"", company:"", category:"qualified", tags:[] })}><UserPlus size={13}/>{zh ? "加入联系人" : "Add to Contacts"}</button>}
          <div><div className="text-xs text-muted mb-1">{zh ? "关联活动" : "Campaigns"}</div><div className="text-sm">{selected.campaigns?.map((c:any)=>c.name).join(", ") || "-"}</div></div>
        </aside>
      </div>
      {contactForm && <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4"><div className="card max-w-md w-full space-y-3">
        <h3 className="font-semibold">{zh ? "加入联系人" : "Add to Contacts"}</h3><p className="text-sm text-muted">{selected.email}</p>
        <input className="input w-full" placeholder={zh ? "姓名" : "Name"} value={contactForm.first_name} onChange={e=>setContactForm({...contactForm,first_name:e.target.value})}/>
        <input className="input w-full" placeholder={zh ? "公司名称" : "Company"} value={contactForm.company} onChange={e=>setContactForm({...contactForm,company:e.target.value})}/>
        <div className="flex gap-2"><button className="btn-primary" onClick={addContact}>{zh ? "保存" : "Save"}</button><button className="btn" onClick={()=>setContactForm(null)}>{zh ? "取消" : "Cancel"}</button></div>
      </div></div>}
      {tagEditor && <div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4"><div className="card max-w-md w-full space-y-3">
        <h3 className="font-semibold">{zh ? "编辑联系人标签" : "Edit contact tags"}</h3><p className="text-sm text-muted">{zh ? "用逗号分隔；删除文字即可移除标签。Agent 标签会随最新邮件更新，自定义标签会保留。" : "Separate with commas. Remove text to delete. Agent tags follow the latest email; custom tags are retained."}</p>
        <input className="input w-full" value={tagText} onChange={e=>setTagText(e.target.value)} placeholder={zh ? "例如：重点客户, SaaS" : "e.g. priority, SaaS"}/>
        <div className="flex gap-2"><button className="btn-primary" onClick={saveTags}>{zh ? "保存标签" : "Save tags"}</button><button className="btn" onClick={()=>setTagEditor(false)}>{zh ? "取消" : "Cancel"}</button></div>
      </div></div>}
    </div>;
  }

  const filters: {key:Filter; zh:string; en:string}[] = [
    {key:"waiting",zh:"等待客户回复",en:"Waiting for Customer"},
    {key:"action",zh:"今日待处理",en:"Needs Action"}, {key:"new",zh:"新客户",en:"New Customers"},
    {key:"followup",zh:"待跟进",en:"Follow-up"}, {key:"stopped",zh:"已停止",en:"Stopped"},
    {key:"human_review",zh:"需要人工处理",en:"Needs Review"},
    {key:"filtered",zh:"已过滤",en:"Filtered"}, {key:"all",zh:"全部客户",en:"All"},
  ];
  const orderedFilters = (["action", "human_review", "all", "new", "waiting", "followup", "stopped", "filtered"] as Filter[])
    .map(key => filters.find(item => item.key === key)!)
    .filter(Boolean);
  const stageLabel = (stage: string) => stage === "filtered"
    ? (zh ? "已过滤" : "Filtered")
    : stageText(stage);
  return <div className="space-y-4">
    <div className="flex items-center gap-2 flex-wrap">
      <h2 className="text-lg font-semibold">{zh ? "智能收件箱" : "Smart Inbox"}</h2>
      <button className="btn flex items-center gap-1.5" disabled={refreshing||syncing||fullScanning||!gmail?.connected} onClick={refresh}><RefreshCw size={14} className={refreshing?"animate-spin":""}/>{refreshing?(zh?"刷新中…":"Refreshing…"):(zh?"刷新":"Refresh")}</button>
      <button className="btn flex items-center gap-1.5" disabled={syncing||fullScanning||!gmail?.connected} onClick={()=>sync(false)}><RefreshCw size={14} className={syncing?"animate-spin":""}/>{syncing?(zh?"正在同步…":"Syncing…"):(zh?"同步新邮件":"Sync New Mail")}</button>
      <button className="btn flex items-center gap-1.5" disabled={syncing||fullScanning||!gmail?.connected} onClick={()=>sync(true)}><Download size={14}/>{fullScanning?(zh?"正在扫描…":"Scanning…"):(zh?"全量扫描邮箱":"Full Mailbox Scan")}</button>
      <button className="btn-primary flex items-center gap-1.5 ml-auto" disabled={sorting||!gmail?.connected||!stats?.unprocessed} onClick={sortAll}><Sparkles size={14}/>{sorting?(zh?"AI 正在分拣…":"AI sorting…"):(zh?`AI 分拣 ${stats?.unprocessed||0} 封新邮件`:`AI Sort ${stats?.unprocessed||0} New`)}</button>
    </div>
    {error&&<div className="bg-danger/10 text-danger text-sm rounded p-2 flex gap-2"><AlertTriangle size={14}/>{error}</div>}
    <div className="flex gap-2 flex-wrap">
      {orderedFilters.map(item=>{
        const label = zh ? item.zh : item.en;
        const badge = item.key === "human_review" && reviewCount ? ` (${reviewCount})` : "";
        return <button key={item.key} className={`text-xs px-2.5 py-1 rounded-full border ${filter===item.key?"bg-accent text-white border-accent":"border-border text-muted"}`} onClick={()=>setFilter(item.key)}>{label}{badge}</button>;
      })}
      <div className="ml-auto input flex items-center gap-1"><Search size={14}/><input className="bg-transparent outline-none w-48" value={search} onChange={e=>setSearch(e.target.value)} placeholder={zh?"搜索姓名、公司、邮箱或标签":"Search name, company, email or tag"}/></div>
    </div>
    {visible.length===0?<div className="py-20 text-center text-muted"><Bot className="mx-auto mb-2"/><p>{zh?"当前分类没有客户":"No customers in this view"}</p></div>:
    <div className="border border-border rounded overflow-hidden">
      <div className="grid grid-cols-[1.4fr_1fr_1fr_100px_100px] gap-3 px-3 py-2 bg-panel2 text-xs text-muted"><span>{zh?"联系人":"Contact"}</span><span>{zh?"阶段与标签":"Stage & Tags"}</span><span>{zh?"最近动态 / 下一步":"Latest / Next"}</span><span>{zh?"会话":"Activity"}</span><span>{zh?"最后时间":"Last Active"}</span></div>
      {visible.map(item=>{const id=identity(item);return <button key={item.email} onClick={()=>{setSelected(item);setThread(null)}} className="w-full text-left grid grid-cols-[1.4fr_1fr_1fr_100px_100px] gap-3 px-3 py-3 border-t border-border hover:bg-panel2 items-center">
        <div className="min-w-0"><div className="font-medium truncate">{id.headline}</div>{id.showEmail&&<div className="text-xs text-muted truncate">{item.email}</div>}</div>
        <div className="min-w-0"><div className="text-sm">{stageLabel(item.lifecycle_stage)}</div><div className="flex gap-1 mt-1 truncate">{(item.tags||[]).slice(0,2).map((tag:string)=><span key={tag} className="text-xs border border-border px-1 rounded">{tag}</span>)}</div></div>
        <div className="min-w-0"><div className="text-sm truncate">{item.latest_subject||"-"}</div><div className="text-xs text-accent truncate">{actionText(item.next_action)}</div></div>
        <div className="text-xs text-muted">{item.message_count} {zh?"封":"mail"}<br/>{item.thread_count} {zh?"个会话":"threads"}</div>
        <div className="text-xs text-muted">{formatDate(item.last_activity_at)}</div>
      </button>})}
    </div>}
  </div>;
}
