"use client";

import { useState } from "react";
import { CheckCircle, Clock, Inbox as InboxIcon, Mail, Send, ThumbsUp, AlertTriangle, Users } from "lucide-react";
import { useLang } from "@/lib/i18n";

export default function Overview({ metrics, gmail, health, readiness, onRefresh }: any) {
  const { t, lang } = useLang();
  const [refreshing, setRefreshing] = useState(false);

  const refresh = async () => {
    setRefreshing(true);
    try { await onRefresh?.(); }
    finally { setRefreshing(false); }
  };

  // Fresh-takeover / empty-workspace detection: no outreach activity recorded yet.
  const isEmpty =
    !metrics?.total_contacts && !metrics?.active_campaigns && !metrics?.sent_today &&
    !metrics?.pending_approvals && !metrics?.replies;

  const cards = [
    { key: "dash_connected_emails", value: metrics?.connected_emails, icon: Mail, color: "text-accent" },
    { key: "dash_total_contacts", value: metrics?.total_contacts, icon: Users, color: "text-accent" },
    { key: "dash_active_campaigns", value: metrics?.active_campaigns, icon: Users, color: "text-accent" },
    { key: "dash_sent_emails", value: metrics?.sent_today, icon: Send, color: "text-ok" },
    { key: "replies_label", fallback: lang === 'zh' ? '回复' : 'Replies', value: metrics?.replies, icon: InboxIcon, color: "text-accent" },
    { key: "positive_replies_label", fallback: lang === 'zh' ? '正向回复' : 'Positive Replies', value: metrics?.positive_replies, icon: ThumbsUp, color: "text-ok" },
    { key: "inbox_needs_reply", fallback: lang === 'zh' ? '需要回复' : 'Needs Reply', value: metrics?.needs_reply, icon: InboxIcon, color: "text-warn" },
    { key: "dash_pending_approvals", value: metrics?.pending_approvals, icon: Clock, color: "text-warn" },
    { key: "dash_scheduled_followups", value: metrics?.scheduled_follow_ups, icon: Clock, color: "text-accent" },
    { key: "failed_tasks_label", fallback: lang === 'zh' ? '失败任务' : 'Failed Tasks', value: metrics?.failed_tasks, icon: AlertTriangle, color: "text-danger" },
  ];

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <h1 className="text-xl font-semibold">{t('nav_dashboard')}</h1>
        <button type="button" className="btn" onClick={refresh} disabled={refreshing} aria-busy={refreshing}>
          {refreshing ? (lang === "zh" ? "刷新中…" : "Refreshing…") : t('common_refresh')}
        </button>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-2 2xl:grid-cols-4 gap-3">
        {cards.map((c) => {
          const Icon = c.icon;
          const label = t(c.key) !== c.key ? t(c.key) : c.fallback;
          return (
            <div key={c.key} className="card flex flex-col gap-2">
              <div className="flex items-center justify-between text-muted text-sm">
                <span>{label}</span>
                <Icon size={16} className={c.color} />
              </div>
              <div className="text-3xl font-bold">{c.value ?? 0}</div>
            </div>
          );
        })}
      </div>
      {readiness && (
        <div className={`card mt-4 border-l-4 ${readiness.status === "ready" ? "border-ok" : "border-warn"}`}>
          <div className="flex flex-col sm:flex-row sm:items-center justify-between gap-3">
            <div>
              <div className="font-medium">
                {lang === "zh" ? "接管就绪状态" : "Takeover readiness"}: {readiness.status === "ready" ? (lang === "zh" ? "可继续" : "Ready") : (lang === "zh" ? "需处理" : "Action needed")}
              </div>
              <p className="text-sm text-muted mt-1">
                {lang === "zh" ? "下一步：" : "Next: "}{readiness.next_action || "-"}
              </p>
            </div>
            <div className="text-xs text-muted text-right">
              <div>{lang === "zh" ? "待审批" : "Pending approvals"}: {readiness.pending_approvals ?? 0}</div>
              <div>{lang === "zh" ? "冻结批次" : "Frozen runs"}: {readiness.awaiting_confirmation_runs?.length ?? 0}</div>
            </div>
          </div>
          {!!readiness.blockers?.length && (
            <p className="text-sm text-warn mt-2">{lang === "zh" ? "阻止项：" : "Blockers: "}{readiness.blockers.join(", ")}</p>
          )}
        </div>
      )}
      {isEmpty && (
        <div className="card mt-4 border-accent/40">
          <div className="flex items-center gap-2 text-accent mb-2">
            <CheckCircle size={16} />
            <span className="font-medium">{lang === 'zh' ? '尚未开始运营' : 'No activity yet'}</span>
          </div>
          <p className="text-muted text-sm mb-2">
            {lang === 'zh'
              ? '当前工作区还没有联系人、Campaign 或已发送邮件。按以下步骤开始：'
              : 'This workspace has no contacts, campaigns, or sent emails yet. Get started:'}
          </p>
          <ol className="list-decimal list-inside text-sm text-muted space-y-1">
            <li>{lang === 'zh' ? (gmail?.connected ? '同步 Gmail 收件箱，让 Smart Inbox 完成分拣' : '连接 Gmail 并同步收件箱') : (gmail?.connected ? 'Sync your Gmail inbox for Smart Inbox triage' : 'Connect Gmail and sync your inbox')}</li>
            <li>{lang === 'zh' ? '在 Smart Inbox 查看分拣与真人判断结果' : 'Review Smart Inbox triage and human-review results'}</li>
            <li>{lang === 'zh' ? '创建首个 Campaign 并生成获客邮件' : 'Create your first Campaign and generate outreach'}</li>
          </ol>
        </div>
      )}
      {!gmail?.connected && (
        <div className="card mt-4 border-warn text-warn text-sm">
          {lang === 'zh'
            ? 'Gmail 尚未连接。请连接您的邮箱后再同步邮件或发送邮件。'
            : 'Gmail is not connected. Connect your mailbox before syncing or sending email.'}
        </div>
      )}
    </div>
  );
}
