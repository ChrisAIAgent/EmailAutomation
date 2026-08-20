"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useLang } from "@/lib/i18n";
import { Edit, Send, X, AlertTriangle, CheckCircle, Bot, Loader2 } from "lucide-react";

export default function ApprovalsView({ onChanged }: { onChanged: () => void }) {
  const { t, lang, formatDate } = useLang();
  const [items, setItems] = useState<any[]>([]);
  const [open, setOpen] = useState<number | null>(null);
  const [confirmId, setConfirmId] = useState<number | null>(null);
  const [realSend, setRealSend] = useState(false);
  const [approvalMode, setApprovalMode] = useState<"human_review" | "agent_review">("human_review");
  const [modeBusy, setModeBusy] = useState(false);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [editedSubject, setEditedSubject] = useState("");
  const [editedBody, setEditedBody] = useState("");
  const [loading, setLoading] = useState(true);

  const load = async () => {
    setLoading(true);
    try {
      const data = await api.approvals();
      // This screen is the pending-work queue. Approved/rejected records belong
      // in history, not in the list that asks the user to act.
      setItems(Array.isArray(data) ? data.filter((item: any) => item.status === "pending") : []);
    } catch (e: any) {
      setError(e?.message || t('err_loading_approvals'));
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    load();
    api.health().then(h => setRealSend(!!h?.real_send));
    api.agentProfile().then(profile => {
      if (profile?.approval_mode === "agent_review" || profile?.approval_mode === "human_review") {
        setApprovalMode(profile.approval_mode);
      }
    }).catch(() => { /* the approval queue remains usable if profile loading fails */ });
  }, []);

  const setGlobalApprovalMode = async (mode: "human_review" | "agent_review") => {
    if (mode === approvalMode) return;
    setModeBusy(true);
    setError("");
    try {
      const profile = await api.updateApprovalMode(mode);
      setApprovalMode(profile.approval_mode);
    } catch (e: any) {
      setError(e?.message || (lang === "zh" ? "审批模式更新失败" : "Failed to update approval mode"));
    } finally {
      setModeBusy(false);
    }
  };

  const decide = async (id: number, decision: "approve" | "reject", edited = false) => {
    setError("");
    setBusy(true);
    try {
      await api.decideApproval(id, {
        decision,
        edited_subject: edited ? editedSubject : undefined,
        edited_body_text: edited ? editedBody : undefined,
      });
    } catch (e: any) {
      const msg = e?.message || String(e);
      if (msg.includes("draft-only")) {
        setError(lang === 'zh'
          ? '邮件尚未发送。审批已保留，请检查邮箱连接后重试。'
          : 'The email was not sent. Your approval was saved; check the mailbox connection and try again.');
      } else {
        setError(msg);
      }
      setConfirmId(null);
      setBusy(false);
      return;
    }
    setOpen(null);
    setConfirmId(null);
    setItems(prev => prev.filter(item => item.id !== id));
    setBusy(false);
    await load();
    onChanged();
  };

  if (loading) {
    return <div className="text-center py-16 text-muted"><Loader2 size={20} className="animate-spin mx-auto mb-2" />{t('common_loading')}</div>;
  }

  const confirmTarget = items.find(a => a.id === confirmId);

  return (
    <div className="space-y-4">
      <h2 className="text-lg font-semibold">{t('appr_title')}</h2>

      <section className="card space-y-3" aria-label={lang === "zh" ? "全局审批模式" : "Global approval mode"}>
        <div>
          <h3 className="text-sm font-medium">{lang === "zh" ? "全局审批模式" : "Global approval mode"}</h3>
          <p className="text-xs text-muted mt-1">
            {approvalMode === "human_review"
              ? (lang === "zh" ? "人工审核：Agent 只能准备邮件；后续发送必须由人工确认。" : "Human review: Agent may prepare email only; a person must confirm any later send.")
              : (lang === "zh" ? "Agent 审核：仅在全部既有安全规则通过时，Agent 可按自动化设置直接发送。" : "Agent review: the Agent may send only when every existing safety rule passes.")}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button className={approvalMode === "human_review" ? "btn-primary text-xs" : "btn text-xs"} disabled={modeBusy} onClick={() => setGlobalApprovalMode("human_review")}>
            {lang === "zh" ? "人工审核" : "Human review"}
          </button>
          <button className={approvalMode === "agent_review" ? "btn-primary text-xs" : "btn text-xs"} disabled={modeBusy} onClick={() => setGlobalApprovalMode("agent_review")}>
            {lang === "zh" ? "Agent 审核" : "Agent review"}
          </button>
        </div>
      </section>

      {error && (
        <div className="flex items-center gap-2 text-sm text-danger bg-danger/10 rounded p-2">
          <AlertTriangle size={14} /> {error}
          <button className="underline ml-auto text-xs" onClick={() => setError("")}>✕</button>
        </div>
      )}

      {items.length === 0 ? (
        <div className="text-center py-16 text-muted">
          <Send size={32} className="mx-auto mb-2 opacity-40" />
          <p className="text-sm">{t('appr_no_approvals')}</p>
          <p className="text-xs">{t('appr_no_approvals_hint')}</p>
        </div>
      ) : (
        <div className="space-y-2">
          {items.map(a => {
            const isSent = a.status === "sent" || a.status === "delivered";
            return (
              <div key={a.id} className="card space-y-2">
                {/* row: recipient, subject, status */}
                <div className="flex items-center justify-between flex-wrap gap-2">
                  <div className="min-w-0">
                    <div className="text-sm font-medium truncate">
                      {t('appr_recipient')}: {[a.contact_name, a.contact_company].filter(Boolean).join(" · ") || a.to_email}
                    </div>
                    {(a.contact_name || a.contact_company) && <div className="text-xs text-muted truncate">{a.to_email}</div>}
                    <div className="text-xs text-muted truncate">{t('appr_subject')}: {a.subject}</div>
                  </div>
                  <div className="flex items-center gap-2 shrink-0">
                    {isSent ? (
                      <span className="text-xs text-ok bg-ok/10 px-2 py-0.5 rounded">{t('status_sent')}</span>
                    ) : (
                      <span className="text-xs text-warn bg-warn/10 px-2 py-0.5 rounded">{t('status_pending_review')}</span>
                    )}
                    <span className="text-xs text-muted">{formatDate(a.created_at)}</span>
                  </div>
                </div>

                {/* row: AI badge + quality */}
                <div className="flex items-center gap-2 text-xs">
                  <span className="flex items-center gap-1 text-accent">
                    <Bot size={12} /> {t('appr_ai_content')}
                  </span>
                  {a.quality_json && (
                    <span className="text-muted">{t('appr_quality')}: {(() => { try { return JSON.parse(a.quality_json).overall_score ?? '—'; } catch { return '—'; } })()}</span>
                  )}
                </div>

                {/* buttons */}
                {!isSent && (
                  <div className="flex gap-2">
                    <button className="btn text-xs" onClick={() => { setOpen(open === a.id ? null : a.id); setEditedSubject(a.subject); setEditedBody(a.body_text); }}>
                      <Edit size={12} className="mr-1" /> {t('appr_review')}
                    </button>
                  </div>
                )}

                {/* expanded review */}
                {open === a.id && !isSent && (
                  <div className="mt-2 border-t border-border pt-3 space-y-2">
                    <div className="text-xs text-muted">{t('appr_edit_allowed')}</div>
                    <div className="space-y-1">
                      <label className="text-xs text-muted">{t('appr_subject')}</label>
                      <input className="input w-full text-sm" value={editedSubject} onChange={e => setEditedSubject(e.target.value)} />
                    </div>
                    <div className="space-y-1">
                      <label className="text-xs text-muted">{t('appr_body')}</label>
                      <textarea className="input w-full text-sm min-h-[120px]" value={editedBody} onChange={e => setEditedBody(e.target.value)} />
                    </div>
                    <div className="flex gap-2">
                      <button className="btn-primary text-xs flex items-center gap-1" disabled={busy} onClick={() => setConfirmId(a.id)}>
                        <Send size={12} /> {busy ? t('common_processing') : t('appr_approve_send')}
                      </button>
                      <button className="btn-danger text-xs flex items-center gap-1" disabled={busy} onClick={() => decide(a.id, "reject")}>
                        <X size={12} /> {t('appr_reject')}
                      </button>
                    </div>
                  </div>
                )}

                {/* sent indicator */}
                {isSent && (
                  <div className="flex items-center gap-2 text-xs text-ok">
                    <CheckCircleIcon />
                    {t('status_sent')}
                  </div>
                )}
              </div>
            );
          })}

          {/* confirm modal */}
          {confirmId !== null && confirmTarget && (
            <div className="fixed inset-0 bg-black/50 flex items-center justify-center z-50">
              <div className="card max-w-md w-full mx-4 space-y-3">
                <h2 className="text-lg font-semibold">{t('appr_confirm_send_title')}</h2>
                {realSend && (
                  <div className="bg-danger/10 border border-danger/30 rounded p-2 text-sm text-danger">
                    {t('appr_confirm_send_desc')}
                  </div>
                )}
                <div className="text-sm space-y-1">
                  <div><span className="text-muted">{t('appr_recipient')}:</span> {[confirmTarget.contact_name, confirmTarget.contact_company].filter(Boolean).join(" · ") || confirmTarget.to_email}</div>
                  {(confirmTarget.contact_name || confirmTarget.contact_company) && <div className="text-xs text-muted">{confirmTarget.to_email}</div>}
                  <div><span className="text-muted">{t('appr_subject')}:</span> {confirmTarget.subject}</div>
                </div>
                <div className="flex gap-2">
                  <button className="btn" disabled={busy} onClick={() => setConfirmId(null)}>{t('common_cancel')}</button>
                  <button
                    className="btn-primary flex-1"
                    disabled={busy}
                    onClick={() => decide(confirmTarget.id, "approve", editedSubject !== confirmTarget.subject || editedBody !== confirmTarget.body_text)}
                  >
                    {busy ? t('common_sending') : t('appr_confirm_send')}
                  </button>
                </div>
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
}

function CheckCircleIcon() {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
      <path d="M22 11.08V12a10 10 0 1 1-5.93-9.14"/><polyline points="22 4 12 14.01 9 11.01"/>
    </svg>
  );
}
