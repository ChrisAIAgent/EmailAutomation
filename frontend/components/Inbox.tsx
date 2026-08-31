"use client";

import { useCallback, useEffect, useState } from "react";
import {
  AlertTriangle, Bot, ChevronLeft, Download, Mail, MessageCircle,
  Pause, Play, RefreshCw, Search, Sparkles, UserPlus, X,
} from "lucide-react";
import { api, API_BASE, friendlyError } from "@/lib/api";
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
  const [importState, setImportState] = useState<any>(null);
  const [importBusy, setImportBusy] = useState(false);
  const [initialTriage, setInitialTriage] = useState<any>(null);
  const [dailyTriage, setDailyTriage] = useState<any>(null);
  const [triageBusy, setTriageBusy] = useState(false);
  const [sorting, setSorting] = useState(false);
  const [replyLoading, setReplyLoading] = useState(false);
  const [reply, setReply] = useState<any>(null);
  const [reviewBusy, setReviewBusy] = useState<string | null>(null);
  const [contactForm, setContactForm] = useState<any | null>(null);
  const [tagEditor, setTagEditor] = useState(false);
  const [tagText, setTagText] = useState("");
  const [error, setError] = useState("");
  const [refreshing, setRefreshing] = useState(false);
  const [completionNotice, setCompletionNotice] = useState("");
  const [reviewNotice, setReviewNotice] = useState("");

  const load = useCallback(async () => {
    try {
      const [rows, inboxStats, gmailStatus, initialImport, triageState, dailyState] = await Promise.all([
        api.inboxCustomers(), api.inboxStats(), api.gmailStatus(), api.gmailImportStatus(), api.initialTriageStatus(), api.dailyTriageStatus(),
      ]);
      const customerRows = Array.isArray(rows) ? rows : [];
      setCustomers(customerRows);
      // Sender-level actions can resolve every review under the same email.  Keep
      // the detail view attached to the freshly returned row so the review card
      // and its controls disappear immediately after a successful resolution.
      setSelected((current: any | null) => current
        ? customerRows.find((item: any) => item.email === current.email) || null
        : null);
      setStats(inboxStats); setGmail(gmailStatus); setImportState(initialImport); setInitialTriage(triageState); setDailyTriage(dailyState);
    } catch (e: any) { setError(e?.message || String(e)); }
  }, []);
  // Import and first-history triage need only their progress, not a complete
  // reconstruction of every inbox row.  Keeping this separate prevents a
  // 2-second progress refresh from timing out on a large history import.
  const refreshOperationalState = useCallback(async () => {
    try {
      const [gmailStatus, initialImport, triageState, dailyState] = await Promise.all([
        api.gmailStatus(), api.gmailImportStatus(), api.initialTriageStatus(), api.dailyTriageStatus(),
      ]);
      setGmail(gmailStatus); setImportState(initialImport); setInitialTriage(triageState); setDailyTriage(dailyState);
    } catch (e: any) { setError(e?.message || String(e)); }
  }, []);
  useEffect(() => { load(); }, [load]);
  useEffect(() => {
    const importStatus = importState?.run?.status;
    const triageStatus = initialTriage?.run?.status;
    const dailyStatus = dailyTriage?.run?.status;
    if (!["queued", "running"].includes(importStatus) && !["queued", "running"].includes(triageStatus) && !["queued", "running"].includes(dailyStatus)) return;
    const timer = window.setInterval(() => { refreshOperationalState(); }, 2000);
    return () => window.clearInterval(timer);
  }, [importState?.run?.status, initialTriage?.run?.status, dailyTriage?.run?.status, refreshOperationalState]);

  // Live updates: when the Agent (TACWork chat) or any background job sorts,
  // syncs or analyzes the inbox, the backend publishes an SSE event. Re-fetch so
  // the left list reflects the new triage without a manual page reload.
  useEffect(() => {
    const es = new EventSource(`${API_BASE}/api/events`);
    es.onmessage = () => {
      const importStatus = importState?.run?.status;
      const triageStatus = initialTriage?.run?.status;
      const dailyStatus = dailyTriage?.run?.status;
      if (["queued", "running"].includes(importStatus) || ["queued", "running"].includes(triageStatus) || ["queued", "running"].includes(dailyStatus)) {
        refreshOperationalState();
      } else {
        load();
      }
    };
    return () => es.close();
  }, [importState?.run?.status, initialTriage?.run?.status, dailyTriage?.run?.status, load, refreshOperationalState]);
  useEffect(() => {
    const run = [initialTriage?.run, dailyTriage?.run].find((item:any) => item?.status === "completed");
    if (!run || typeof window === "undefined") return;
    const key = `email-automation-triage-complete-${run.id}`;
    if (window.sessionStorage.getItem(key)) return;
    window.sessionStorage.setItem(key, "1");
    setCompletionNotice(zh
      ? `${run.kind === "initial_history" ? "首次历史" : "日常增量"}分拣已完成：${run.terminal_threads || 0} 个会话已处理。`
      : `${run.kind === "initial_history" ? "First-history" : "Daily"} triage completed: ${run.terminal_threads || 0} threads processed.`);
    const timer = window.setTimeout(() => setCompletionNotice(""), 8000);
    return () => window.clearTimeout(timer);
  }, [initialTriage?.run?.id, initialTriage?.run?.status, dailyTriage?.run?.id, dailyTriage?.run?.status, zh]);

  const sync = async () => {
    setSyncing(true); setError("");
    try { await api.gmailSync(); await load(); onChanged(); }
    catch (e: any) { setError(e?.message || String(e)); }
    finally { setSyncing(false); }
  };
  const startImport = async () => {
    setImportBusy(true); setError("");
    try { setImportState(await api.gmailImportStart()); await refreshOperationalState(); }
    catch (e: any) { setError(e?.message || String(e)); }
    finally { setImportBusy(false); }
  };
  const controlImport = async (action: "pause" | "resume" | "cancel") => {
    const id = importState?.run?.id;
    if (!id) return;
    setImportBusy(true); setError("");
    try {
      const result = action === "pause" ? await api.gmailImportPause(id)
        : action === "resume" ? await api.gmailImportResume(id)
        : await api.gmailImportCancel(id);
      setImportState(result); await refreshOperationalState();
    } catch (e: any) { setError(e?.message || String(e)); }
    finally { setImportBusy(false); }
  };
  const startInitialTriage = async () => {
    setTriageBusy(true); setError("");
    try { setInitialTriage(await api.startInitialTriage()); await refreshOperationalState(); onChanged(); }
    catch (e: any) { setError(friendlyError(e, zh)); }
    finally { setTriageBusy(false); }
  };
  const controlInitialTriage = async (action: "pause" | "resume" | "cancel") => {
    const id = initialTriage?.run?.id;
    if (!id) return;
    setTriageBusy(true); setError("");
    try { setInitialTriage(await api.controlInitialTriage(id, action)); await refreshOperationalState(); onChanged(); }
    catch (e: any) { setError(friendlyError(e, zh)); }
    finally { setTriageBusy(false); }
  };
  const retryInitialTriage = async () => {
    const id = initialTriage?.run?.id;
    if (!id) return;
    setTriageBusy(true); setError("");
    try { setInitialTriage(await api.retryInitialTriageFailed(id)); await refreshOperationalState(); onChanged(); }
    catch (e: any) { setError(friendlyError(e, zh)); }
    finally { setTriageBusy(false); }
  };
  const startDailyTriage = async () => {
    setSorting(true); setError("");
    try { setDailyTriage(await api.startDailyTriage()); await refreshOperationalState(); onChanged(); }
    catch (e: any) { setError(friendlyError(e, zh)); }
    finally { setSorting(false); }
  };
  const controlDailyTriage = async (action: "pause" | "resume" | "cancel") => {
    const id = dailyTriage?.run?.id;
    if (!id) return;
    setTriageBusy(true); setError("");
    try { setDailyTriage(await api.controlDailyTriage(id, action)); await refreshOperationalState(); onChanged(); }
    catch (e: any) { setError(friendlyError(e, zh)); }
    finally { setTriageBusy(false); }
  };
  const retryDailyTriage = async () => {
    const id = dailyTriage?.run?.id;
    if (!id) return;
    setTriageBusy(true); setError("");
    try { setDailyTriage(await api.retryDailyTriageFailed(id)); await refreshOperationalState(); onChanged(); }
    catch (e: any) { setError(friendlyError(e, zh)); }
    finally { setTriageBusy(false); }
  };
  const sortAll = async () => {
    await startDailyTriage();
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
    const targetThreadId = contactForm?.threadId || thread?.id;
    if (!targetThreadId || !contactForm) return;
    if (!contactForm.first_name.trim() && !contactForm.company.trim()) {
      setError(zh ? "请填写姓名或公司名称" : "Name or company is required"); return;
    }
    try {
      await api.addInboxContact(targetThreadId, contactForm);
      setContactForm(null); await load(); onChanged();
      setReviewNotice(zh ? "联系人已创建，当前发件人的准入审核已关闭。" : "Contact created; this sender's admission reviews are closed.");
    } catch (e: any) { setError(e?.message || String(e)); }
  };
  const resolveReviewThread = async (reviewThread: any, decision: "approve" | "reject" | "agent_decide") => {
    // Unknown senders always require an explicit Contact record.  Treat legacy
    // content_uncertain rows the same way while an upgraded backend refreshes
    // their review kind, so Approve can never be misreported as no-action.
    if (decision === "approve" && !selected?.contact_id && ["contact_admission_uncertain", "content_uncertain"].includes(reviewThread.review_kind)) {
      setContactForm({ first_name:"", company:"", category:"qualified", tags:[], threadId: reviewThread.id });
      return;
    }
    setReviewBusy(decision); setError("");
    try {
      const result = await api.resolveHumanReview(reviewThread.id, decision);
      if (result?.resolved === false) {
        throw new Error(result?.message || (zh ? "当前审核尚未完成" : "The review was not resolved"));
      }
      await load(); onChanged();
      setReviewNotice(decision === "reject"
        ? (zh ? "已过滤此邮箱：当前审核已关闭，后续邮件将自动过滤，不再重复询问。" : "Sender filtered. Current reviews are closed and future mail will be filtered.")
        : decision === "agent_decide"
          ? (zh ? "Agent 已完成当前发件人的审核处理。" : "Agent completed this sender's review handling.")
          : (zh ? "当前会话已标记为无需动作。" : "The current conversation is marked no action."));
      if (thread?.id === reviewThread.id) setThread(await api.threadDetail(reviewThread.id));
    } catch (e: any) { setError(e?.message || String(e)); }
    finally { setReviewBusy(null); }
  };
  const resolveReview = async (decision: "approve" | "reject" | "agent_decide") => {
    if (thread) await resolveReviewThread(thread, decision);
  };
  const enableContentReviewIgnore = async () => {
    if (!selected) return;
    setReviewBusy("content_ignore"); setError("");
    try {
      await api.enableSenderContentReviewIgnore(selected.email);
      await load(); onChanged();
      setReviewNotice(zh ? "已启用长期内容忽略规则；当前同类审核已关闭。" : "Permanent content-ignore rule enabled; current matching reviews are closed.");
      if (thread) setThread(await api.threadDetail(thread.id));
    } catch (e: any) { setError(e?.message || String(e)); }
    finally { setReviewBusy(null); }
  };
  const removeContentReviewIgnore = async () => {
    if (!selected) return;
    setReviewBusy("content_ignore"); setError("");
    try {
      await api.removeSenderContentReviewIgnore(selected.email);
      await load(); onChanged();
      setReviewNotice(zh ? "已撤销长期内容忽略规则。" : "Permanent content-ignore rule revoked.");
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

  const reviewCount = stats?.human_review_senders || 0;
  const visible = customers.filter(item => {
    const haystack = `${item.first_name || ""} ${item.last_name || ""} ${item.company || ""} ${item.email} ${(item.tags || []).join(" ")} ${item.latest_subject || ""}`.toLowerCase();
    if (search && !haystack.includes(search.toLowerCase())) return false;
    if (filter === "action") return item.needs_reply || (item.lifecycle_stage !== "stopped" && item.category !== "invalid" && (item.next_action === "reply" || item.next_action === "follow_up"));
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
    unclassified: zh ? "未建立联系人" : "No contact yet",
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
    const reviewThreads = orderedThreads.filter((item:any) => item.pending_action === "human_review");
    const admissionThreads = reviewThreads.filter((item:any) => item.review_kind !== "opt_out_confirmation");
    const contentReviewThreads = reviewThreads.filter((item:any) => item.review_kind === "content_uncertain");
    const primaryAdmissionThread = admissionThreads[0];
    return <div className="space-y-4">
      <button className="text-sm text-muted flex items-center gap-1" onClick={() => { setSelected(null); setThread(null); setReply(null); }}>
        <ChevronLeft size={14}/>{zh ? "返回客户队列" : "Back to customer queue"}
      </button>
      <div className="flex items-start justify-between gap-4 border-b border-border pb-4">
        <div><h2 className="text-lg font-semibold">{id.headline}</h2>{id.showEmail && <p className="text-sm text-muted">{selected.email}</p>}</div>
        <div className="text-right"><div className="text-sm font-medium">{stageText(selected.lifecycle_stage)}</div><div className="text-xs text-accent">{actionText(selected.next_action)}</div></div>
      </div>
      {reviewNotice && <div className="bg-ok/10 text-ok text-sm rounded p-2">{reviewNotice}</div>}
      {reviewThreads.length > 0 && <section className="border border-warn/40 bg-warn/10 rounded p-4 space-y-3">
        <div className="flex items-start justify-between gap-3 flex-wrap"><div className="flex items-start gap-2"><AlertTriangle className="text-warn shrink-0 mt-0.5" size={18}/><div><h3 className="font-semibold">{zh ? `待人工处理：${reviewThreads.length} 个会话` : `Needs review: ${reviewThreads.length} conversations`}</h3><p className="text-sm text-muted mt-1">{zh ? "按发件人汇总；可在这里处理当前审核，无需逐封查找。" : "Grouped by sender; handle the current reviews here without searching message by message."}</p></div></div><span className="text-xs text-warn">{zh ? "发件人级审核" : "Sender-level review"}</span></div>
        <div className="space-y-2">
          {reviewThreads.map((item:any) => <div key={item.id} className="flex items-center justify-between gap-3 text-sm border-t border-warn/20 pt-2"><div className="min-w-0"><div className="font-medium truncate">{item.subject || (zh ? "无主题" : "No subject")}</div><div className="text-xs text-muted mt-0.5">{formatDate(item.latest_message_at || item.updated_at)} · {item.review_kind === "content_uncertain" ? (zh ? "内容需要人工确认" : "Content review") : item.review_kind === "opt_out_confirmation" ? (zh ? "退订确认" : "Opt-out confirmation") : (zh ? "联系人准入" : "Contact admission")}</div></div><button className="text-xs text-accent hover:underline shrink-0" onClick={()=>openThread(item.id)}>{zh ? "查看依据" : "View evidence"}</button></div>)}
        </div>
        {selected.contact_id && contentReviewThreads.length > 0 && <div className="flex flex-wrap gap-2"><button className="btn-primary" disabled={!!reviewBusy} onClick={enableContentReviewIgnore}>{reviewBusy === "content_ignore" ? (zh ? "处理中…" : "Working…") : (zh ? "长期忽略此类内容" : "Ignore this content type permanently")}</button><button className="btn" disabled={!!reviewBusy} onClick={()=>primaryAdmissionThread && resolveReviewThread(primaryAdmissionThread, "approve")}>{zh ? "仅本次标记无需动作" : "Mark current items no action"}</button><p className="w-full text-xs text-muted">{zh ? "长期规则只关闭该邮箱后续的内容确认；正常业务邮件、联系人准入和退订确认仍会照常处理。" : "The permanent rule only closes future content reviews; business mail, admission and opt-out checks still run."}</p></div>}
        {!selected.contact_id && primaryAdmissionThread && <div className="flex flex-wrap gap-2"><button className="btn-primary" disabled={!!reviewBusy} onClick={()=>resolveReviewThread(primaryAdmissionThread, "approve")}>{zh ? "Approve · 创建联系人" : "Approve · Create contact"}</button><button className="btn" disabled={!!reviewBusy} onClick={()=>resolveReviewThread(primaryAdmissionThread, "reject")}>{reviewBusy === "reject" ? (zh ? "处理中…" : "Working…") : (zh ? "Reject · 过滤此邮箱" : "Reject · Filter sender")}</button><button className="btn" disabled={!!reviewBusy} onClick={()=>resolveReviewThread(primaryAdmissionThread, "agent_decide")}>{reviewBusy === "agent_decide" ? (zh ? "判断中…" : "Deciding…") : (zh ? "Agent Decide · 自动建联打标" : "Agent Decide · Create & tag")}</button><p className="w-full text-xs text-muted">{zh ? "Reject 会关闭当前同类审核，并让该邮箱后续邮件自动过滤、不再重复询问。" : "Reject resolves the current admission reviews and filters future mail from this sender."}</p></div>}
        {selected.content_review_ignore_enabled && <div className="flex items-center gap-2 text-xs text-ok"><span>{zh ? "已启用长期内容忽略规则" : "Permanent content-ignore rule enabled"}</span><button className="text-accent hover:underline" disabled={!!reviewBusy} onClick={removeContentReviewIgnore}>{zh ? "撤销" : "Revoke"}</button></div>}
      </section>}
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
              {["content_uncertain", "contact_admission_uncertain"].includes(thread.review_kind) ? <div className="text-xs text-muted">{zh ? "请在此发件人详情顶部的“待人工处理”卡中统一处理同一邮箱的审核会话。" : "Use the sender-level review card at the top of this detail page to resolve this sender's review conversations together."}</div> : thread.review_kind === "stale_review" ? <div className="flex flex-wrap gap-2">
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
            </div> : !reply && selected.lifecycle_stage !== "stopped" && selected.category !== "invalid" && thread.category !== "rejected" && thread.category !== "awaiting_reply" && <button className="btn-primary flex items-center gap-1.5" disabled={replyLoading || thread.pending_action !== "reply"} onClick={generateReply}><MessageCircle size={14}/>{replyLoading ? (zh ? "正在生成…" : "Generating…") : (zh ? "AI 生成回复" : "AI Draft Reply")}</button>}
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
            {selected.content_review_ignore_enabled && <div><div className="text-xs text-muted mb-1">{zh ? "内容审核规则" : "Content review rule"}</div><div className="text-sm text-ok">{zh ? "长期忽略已启用" : "Permanent ignore enabled"}</div><button className="text-xs text-accent hover:underline mt-1" disabled={!!reviewBusy} onClick={removeContentReviewIgnore}>{zh ? "撤销规则" : "Revoke rule"}</button></div>}
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
  const importRun = importState?.run;
  const initialImportCompleted = !!importState?.initial_import_completed;
  const importActive = importRun?.status === "queued" || importRun?.status === "running";
  const initialRun = initialTriage?.run;
  const dailyRun = dailyTriage?.run;
  const activeRun = [initialRun, dailyRun].find((run:any) => ["queued", "running", "paused", "recovery_pending"].includes(run?.status));
  const triageRun = activeRun || [initialRun, dailyRun].filter(Boolean).sort((a:any, b:any) => b.id - a.id)[0];
  const triageActive = triageRun?.status === "queued" || triageRun?.status === "running";
  const triageCompleted = triageRun?.status === "completed";
  const triageCanStart = initialImportCompleted && (!initialRun || initialRun.status === "cancelled");
  return <div className="space-y-4">
    <div className="flex items-center gap-2 flex-wrap">
      <h2 className="text-lg font-semibold">{zh ? "智能收件箱" : "Smart Inbox"}</h2>
      <button className="btn flex items-center gap-1.5" disabled={refreshing||syncing||!gmail?.connected} onClick={refresh}><RefreshCw size={14} className={refreshing?"animate-spin":""}/>{refreshing?(zh?"刷新中…":"Refreshing…"):(zh?"刷新":"Refresh")}</button>
      {initialImportCompleted ?
        <button className="btn flex items-center gap-1.5" title={zh ? "仅检查 Gmail 自上次同步后的新增或变化；不会进行 AI 分拣。" : "Checks Gmail changes since the last sync; it does not run AI triage."} disabled={syncing||!gmail?.connected} onClick={sync}><RefreshCw size={14} className={syncing?"animate-spin":""}/>{syncing?(zh?"正在检查 Gmail 新变化…":"Checking Gmail changes…"):(zh?"检查 Gmail 新变化":"Check Gmail Changes")}</button>
        : <button className="btn flex items-center gap-1.5" disabled={importBusy||importActive||!gmail?.connected} onClick={startImport}><Download size={14}/>{importActive?(zh?"正在导入历史邮件…":"Importing history…"):(zh?"导入全部历史邮件":"Import All Mail History")}</button>}
      <button className="btn-primary flex items-center gap-1.5 ml-auto" disabled={sorting||!!activeRun||!gmail?.connected||!stats?.unprocessed} onClick={sortAll}><Sparkles size={14}/>{sorting?(zh?"正在创建分拣任务…":"Creating triage run…"):(zh?`AI 分拣新增邮件（${stats?.unprocessed || 0} 个会话）`:`AI Triage New Mail (${stats?.unprocessed || 0} threads)`)}</button>
    </div>
    {error&&<div className="bg-danger/10 text-danger text-sm rounded p-2 flex gap-2"><AlertTriangle size={14}/>{error}</div>}
    {completionNotice&&<div className="bg-ok/10 text-ok text-sm rounded p-2">{completionNotice}</div>}
    {!initialImportCompleted && gmail?.connected && <div className="card text-sm space-y-2">
      <div className="font-medium">{zh ? "首次历史导入" : "First Mail History Import"}</div>
      <p className="text-muted">{zh ? "导入全部可访问邮件（不含垃圾邮件和垃圾箱）。数据仅保存到本机；不会自动 AI 分拣、创建联系人、生成草稿或发送邮件。" : "Imports all accessible mail except Spam and Trash. Data stays local; this does not run AI sorting, create contacts or drafts, or send mail."}</p>
      {importRun && <div className="flex flex-wrap items-center gap-3">
        <span>{zh ? "状态" : "Status"}: {importRun.status}</span>
        <span>{zh ? "线程" : "Threads"}: {importRun.threads_scanned || 0}</span>
        <span>{zh ? "新邮件" : "New messages"}: {importRun.new_messages || 0}</span>
        <span>{zh ? "失败" : "Failures"}: {importRun.failures || 0}</span>
        {importRun.can_pause && <button className="btn text-xs" disabled={importBusy} onClick={()=>controlImport("pause")}>{zh ? "暂停" : "Pause"}</button>}
        {importRun.can_resume && <button className="btn text-xs" disabled={importBusy} onClick={()=>controlImport("resume")}>{zh ? "继续" : "Resume"}</button>}
        {importRun.can_cancel && <button className="btn text-xs" disabled={importBusy} onClick={()=>controlImport("cancel")}>{zh ? "取消" : "Cancel"}</button>}
      </div>}
    </div>}
    {initialImportCompleted && <div className="card text-sm space-y-3 border border-accent/30">
      {!initialRun && <>
        <div className="font-medium">{zh ? "首次历史分拣" : "First History Triage"}</div>
        <p className="text-muted">{zh ? `历史邮件已导入。首次分拣将按每批 50 个会话处理当前 ${stats?.unprocessed || 0} 个未分拣会话；仅分类、过滤和联系人准入，不会创建草稿、审批或发送邮件。` : `Mail history is imported. The first triage processes the current ${stats?.unprocessed || 0} untriaged threads in batches of 50. It only classifies mail and never drafts or sends.`}</p>
        <p className="text-muted">{zh ? "点击开始后会立即冻结当前未分拣快照，之后到达的新邮件不会纳入本次分拣。" : "Starting it freezes the current untriaged snapshot immediately; mail arriving afterwards is not included in this triage."}</p>
        <button className="btn-primary flex items-center gap-1.5" disabled={!triageCanStart || triageBusy || !stats?.unprocessed} onClick={startInitialTriage}><Sparkles size={14}/>{zh ? `开始首次历史分拣（${stats?.unprocessed || 0} 个会话）` : `Start First History Triage (${stats?.unprocessed || 0} threads)`}</button>
      </>}
      {triageRun && <>
        <div className="flex items-center justify-between gap-3 flex-wrap">
          <div><div className="font-medium">{triageActive ? (triageRun.kind === "daily_incremental" ? (zh ? "日常增量分拣" : "Daily Incremental Triage") : (zh ? "首次历史分拣" : "First History Triage")) : (zh ? "最近一次分拣结果" : "Most Recent Triage Result")}</div><div className="text-muted mt-1">{triageActive ? (zh ? `正在处理第 ${Math.max(triageRun.current_batch || 1, 1)} / ${triageRun.total_batches || 1} 批` : `Processing batch ${Math.max(triageRun.current_batch || 1, 1)} / ${triageRun.total_batches || 1}`) : (triageCompleted ? (zh ? `${triageRun.kind === "initial_history" ? "首次历史" : "日常增量"}分拣已完成；后续新增会话会在下次日常分拣中处理。` : "Completed. Newly synced threads wait for the next daily triage.") : `${zh ? "状态" : "Status"}: ${triageRun.status}`)}</div></div>
          <div className="text-lg font-semibold">{triageRun.progress_percent || 0}%</div>
        </div>
        <div className="h-2 rounded bg-panel2 overflow-hidden"><div className="h-full bg-accent transition-all" style={{width: `${Math.max(0, Math.min(100, triageRun.progress_percent || 0))}%`}} /></div>
        <div className="grid grid-cols-2 md:grid-cols-4 gap-x-4 gap-y-2 text-muted">
          <span>{zh ? "已处理" : "Processed"}: {triageRun.terminal_threads || 0} / {triageRun.total_threads || 0}</span><span>{zh ? "自动过滤" : "Filtered"}: {triageRun.auto_filtered || 0}</span><span>{zh ? "本次人工审核" : "This-run review"}: {triageRun.human_review || 0} {zh ? "个会话" : "threads"}</span><span>{zh ? "业务会话" : "Business"}: {triageRun.business_threads || 0}</span>
          <span>{zh ? "无需动作" : "No action"}: {triageRun.no_action || 0}</span><span>{zh ? "已跳过" : "Skipped"}: {triageRun.skipped_threads || 0}</span><span>{zh ? "失败" : "Failed"}: {triageRun.failed_threads || 0}</span><span>{zh ? "剩余" : "Remaining"}: {triageRun.remaining_threads || 0}</span>
        </div>
        {triageRun.last_progress_at && <div className="text-xs text-muted">{zh ? "最近进度更新" : "Last progress"}: {formatDate(triageRun.last_progress_at)}</div>}
        {triageRun.kind === "initial_history" && triageRun.source_import_threads != null && <div className="text-xs text-muted">{zh ? `历史导入 ${triageRun.source_import_threads} 个会话；本次快照 ${triageRun.total_threads} 个，是启动时仍未分拣的会话，二者可以不同。` : `History import: ${triageRun.source_import_threads}; this snapshot: ${triageRun.total_threads} untriaged threads at start.`}</div>}
        {triageActive && <div className="text-xs text-muted">{zh ? "首次分拣正在后台进行。可以继续浏览；建议不要同时启动常规分拣。" : "First triage is running in the background. You can keep browsing; avoid overlapping routine triage."}</div>}
        {triageRun.error && <div className="text-xs text-danger">{triageRun.error}</div>}
        <div className="flex flex-wrap gap-2">
          {triageRun.can_pause && <button className="btn text-xs flex items-center gap-1" disabled={triageBusy} onClick={()=>triageRun.kind === "daily_incremental" ? controlDailyTriage("pause") : controlInitialTriage("pause")}><Pause size={13}/>{zh ? "暂停" : "Pause"}</button>}
          {triageRun.can_resume && <button className="btn text-xs flex items-center gap-1" disabled={triageBusy} onClick={()=>triageRun.kind === "daily_incremental" ? controlDailyTriage("resume") : controlInitialTriage("resume")}><Play size={13}/>{zh ? "继续" : "Resume"}</button>}
          {triageRun.can_cancel && <button className="btn text-xs flex items-center gap-1" disabled={triageBusy} onClick={()=>triageRun.kind === "daily_incremental" ? controlDailyTriage("cancel") : controlInitialTriage("cancel")}><X size={13}/>{zh ? "取消" : "Cancel"}</button>}
          {triageRun.can_retry_failed && <button className="btn text-xs flex items-center gap-1" disabled={triageBusy} onClick={()=>triageRun.kind === "daily_incremental" ? retryDailyTriage() : retryInitialTriage()}>{zh ? "重试失败项" : "Retry failures"}</button>}
          {triageRun.status === "cancelled" && triageRun.kind === "initial_history" && <button className="btn-primary text-xs" disabled={triageBusy} onClick={startInitialTriage}>{zh ? "重新建立首次分拣" : "Start a new first triage"}</button>}
        </div>
      </>}
    </div>}
    <div className="flex gap-2 flex-wrap">
      {orderedFilters.map(item=>{
        const label = zh ? item.zh : item.en;
        const badge = item.key === "human_review" && reviewCount ? ` (${reviewCount}${zh ? " 位 / " + (stats?.human_review_threads || 0) + " 会话" : " senders / " + (stats?.human_review_threads || 0) + " threads"})` : "";
        return <button key={item.key} className={`text-xs px-2.5 py-1 rounded-full border ${filter===item.key?"bg-accent text-white border-accent":"border-border text-muted"}`} onClick={()=>setFilter(item.key)}>{label}{badge}</button>;
      })}
      <div className="ml-auto input flex items-center gap-1"><Search size={14}/><input className="bg-transparent outline-none w-48" value={search} onChange={e=>setSearch(e.target.value)} placeholder={zh?"搜索姓名、公司、邮箱或标签":"Search name, company, email or tag"}/></div>
    </div>
    {visible.length===0?<div className="py-20 text-center text-muted"><Bot className="mx-auto mb-2"/><p>{zh?"当前分类没有客户":"No customers in this view"}</p></div>:
    <div className="border border-border rounded overflow-hidden">
      <div className="grid grid-cols-[1.4fr_1fr_1fr_100px_100px] gap-3 px-3 py-2 bg-panel2 text-xs text-muted"><span>{zh?"联系人":"Contact"}</span><span>{zh?"阶段与标签":"Stage & Tags"}</span><span>{zh?"最近动态 / 下一步":"Latest / Next"}</span><span>{zh?"会话":"Activity"}</span><span>{zh?"最后时间":"Last Active"}</span></div>
      {visible.map(item=>{const id=identity(item);return <button key={item.email} onClick={()=>{setSelected(item);setThread(null)}} className="w-full text-left grid grid-cols-[1.4fr_1fr_1fr_100px_100px] gap-3 px-3 py-3 border-t border-border hover:bg-panel2 items-center">
        <div className="min-w-0"><div className="font-medium truncate">{id.headline}</div>{id.showEmail&&<div className="text-xs text-muted truncate">{item.email}</div>}</div>
        <div className="min-w-0"><div className="text-sm">{stageLabel(item.lifecycle_stage)}</div><div className="flex gap-1 mt-1 truncate">{(item.tags||[]).slice(0,2).map((tag:string)=><span key={tag} className="text-xs border border-border px-1 rounded">{tag}</span>)}</div>{item.review_thread_count > 0 && <div className="text-xs text-warn mt-1">{zh ? `待人工判断：${item.review_thread_count} 个会话` : `Review: ${item.review_thread_count}`}</div>}{item.filtered_thread_count > 0 && <div className="text-xs text-muted mt-1">{zh ? `已过滤：${item.filtered_thread_count} 个会话` : `Filtered: ${item.filtered_thread_count}`}</div>}{item.unprocessed_thread_count > 0 && <div className="text-xs text-accent mt-1">{zh ? `待分拣：${item.unprocessed_thread_count} 个会话` : `Untriaged: ${item.unprocessed_thread_count}`}</div>}</div>
        <div className="min-w-0"><div className="text-sm truncate">{item.latest_subject||"-"}</div><div className="text-xs text-accent truncate">{actionText(item.next_action)}</div></div>
        <div className="text-xs text-muted">{item.message_count} {zh?"封":"mail"}<br/>{item.thread_count} {zh?"个会话":"threads"}</div>
        <div className="text-xs text-muted">{formatDate(item.last_activity_at)}</div>
      </button>})}
    </div>}
  </div>;
}
