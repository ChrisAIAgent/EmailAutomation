'use client';
import React, { createContext, useContext, useEffect, useState, useCallback } from 'react';

// --------------- type ---------------
export type Lang = 'zh' | 'en';

// --------------- full dictionary ---------------
const DICT: Record<Lang, Record<string, any>> = {

zh: {

// --- nav ---
nav_dashboard: '仪表盘',
nav_inbox: '收件箱',
nav_contacts: '联系人',
nav_campaigns: '营销活动',
nav_approvals: '审核发送',
nav_automation: '自动跟进',
nav_knowledge: '知识库',
nav_agent_profile: 'Agent 设置',
nav_activity: '操作记录',
nav_agent_lab: 'Agent 实验室',

// --- common buttons / actions ---
common_save: '保存',
common_cancel: '取消',
common_confirm: '确认',
common_edit: '编辑',
common_delete: '删除',
common_create: '创建',
common_close: '关闭',
common_back: '返回',
common_loading: '加载中…',
common_processing: '处理中…',
common_sending: '发送中…',
common_refresh: '刷新',
common_search: '搜索',
common_filter: '筛选',
common_all: '全部',
common_no_data: '暂无数据',
common_error: '出错了',
common_retry: '重试',
common_success: '操作成功',
common_failed: '操作失败',
common_next: '下一步',
common_done: '完成',
common_confirm_delete: '确认删除',
common_delete_warning: '此操作不可撤销，确定要删除吗？',
common_copied: '已复制',

// --- status labels ---
status_draft: '草稿',
status_pending: '待审核',
status_queued: '等待执行',
status_running: '执行中',
status_success: '成功',
status_failed: '失败',
status_paused: '已暂停',
status_enabled: '已启用',
status_disabled: '已禁用',
status_active: '进行中',
status_archived: '已归档',
status_stopped: '已停止',
inbox_stopped_notice: '客户已要求停止触达，系统不会再发送跟进邮件。',
status_pending_review: '待审核',
status_sent: '已发送',
status_delivered: '已送达',
status_bounced: '退信',
status_replied: '已回复',
status_unsubscribed: '已退订',
status_not_interested: '暂不考虑',
status_outreach_generated: '已生成',
status_imported: '已导入',
status_synced: '已同步',

// --- dashboard ---
dash_title: '邮件自动化工作台',
dash_email_automation: '邮件自动化',
dash_connected_emails: '已连接邮箱',
dash_active_campaigns: '进行中活动',
dash_pending_approvals: '待审核',
dash_sent_emails: '已发送邮件',
dash_total_contacts: '联系人总数',
dash_scheduled_followups: '待跟进',
dash_recent_activity: '最近操作',
dash_no_recent: '暂无最近操作',

// --- inbox ---
inbox_title: '智能收件箱',
inbox_sync: '同步收件箱',
inbox_syncing: '正在同步…',
inbox_sync_done: '同步完成',
inbox_sort_btn: '一键 AI 分拣',
inbox_sort_btn_count: '一键 AI 分拣 {count} 封邮件',
inbox_sorting: 'AI 正在分拣…',
inbox_sort_done: '分拣完成',
inbox_no_threads: '没有邮件',
inbox_no_threads_hint: '点击"同步收件箱"从 Gmail 获取邮件',
inbox_unsorted: '未分拣',
inbox_valid_customer: '有效客户',
inbox_needs_reply: '需要回复',
inbox_has_interest: '有兴趣',
inbox_not_now: '暂不考虑',
inbox_rejected: '拒绝/退订',
inbox_irrelevant: '无关邮件',
inbox_unprocessed: '未处理',
inbox_ai_judgment: 'AI 判断',
inbox_judgment_reason: '判断理由',
inbox_suggested_action: '建议操作',
inbox_generate_reply: 'AI 生成回复',
inbox_reply_draft_ready: '回复草稿已生成，请到审核页确认发送',
inbox_detail_loading: '加载邮件详情…',
inbox_no_detail: '邮件不存在',
inbox_contact: '联系人',
inbox_subject: '主题',
inbox_date: '日期',
inbox_category: '分类',
inbox_thread_count: '{count} 条对话',

// --- campaign ---
camp_title: '营销活动',
camp_create: '创建任务',
camp_create_new: '新建营销任务',
camp_edit: '编辑任务',
camp_import: '导入客户',
camp_generate: 'AI 生成获客邮件',
camp_go_approve: '去审核发送',
camp_name: '任务名称',
camp_objective: '目标',
camp_product: '产品描述',
camp_audience: '目标受众',
camp_sender_name: '发件人名称',
camp_sender_company: '发件人公司',
camp_tone: '邮件风格',
camp_daily_limit: '每日发送上限',
camp_send_window: '发送时段',
camp_timezone: '时区',
camp_max_followups: '最大跟进次数',
camp_followup_days: '跟进间隔(天)',
camp_approval_mode: '审核模式',
camp_status: '状态',
camp_actions: '操作',
camp_next_step: '下一步：导入客户',
camp_no_campaigns: '还没有营销任务',
camp_create_first: '创建你的第一个营销任务',
camp_confirm_delete: '确定要删除此任务吗？已有发送记录的将归档保留。',
camp_delete_ok: '任务已删除/归档',
camp_pause: '暂停',
camp_resume: '恢复',
camp_paused_hint: '已暂停，不会发送新邮件',
camp_edit_tooltip: '编辑此任务的名称与配置',
camp_delete_tooltip: '删除此任务（有记录时归档）',
camp_import_tooltip: '从 CSV 文件导入联系人',
camp_generate_tooltip: '使用 AI 为所有联系人生成个性化获客邮件',
camp_name_placeholder: '例如：Q3 北美市场拓展',
camp_objective_placeholder: '例如：介绍新产品、预约产品演示',

// --- approval ---
appr_title: '审核发送',
appr_approve_send: '通过并发送',
appr_reject: '拒绝',
appr_review: '审核',
appr_send: '发送',
appr_confirm_send: '确认发送',
appr_confirm_send_title: '确认发送',
appr_confirm_send_desc: '这将向收件人发送一封真实邮件。',
appr_recipient: '收件人',
appr_subject: '主题',
appr_body: '正文',
appr_generated_at: '生成时间',
appr_ai_content: 'AI 生成内容',
appr_edit_allowed: '您可以编辑主题和正文后再发送',
appr_no_approvals: '没有待审核的邮件',
appr_no_approvals_hint: '在 Campaign 中生成获客邮件后，审批内容会出现在这里',
appr_quality: '质量评分',
appr_idempotent: '已发送，不可重复发送',

// --- automation ---
auto_title: '自动跟进',
auto_create_title: '创建自动跟进',
auto_review_note: '系统会自动准备跟进邮件，发送前仍需您审核确认。',
auto_select_campaign: '选择营销任务',
auto_no_campaign: '请先创建营销任务并导入客户',
auto_no_campaign_hint: '需要先有 Campaign 和客户数据，才能设置自动跟进',
auto_first_email: '首封邮件',
auto_immediate: '立即发送',
auto_scheduled: '指定时间',
auto_followup_after: '未回复 N 天后跟进',
auto_followup_after_days: '{days} 天后跟进',
auto_max_followups: '最多跟进次数',
auto_max_followups_count: '{count} 次',
auto_send_window: '发送时间段',
auto_send_window_format: '{start}:00 - {end}:00',
auto_stop_on_reply: '收到回复后停止',
auto_stop_on_reject: '拒绝/退订后停止',
auto_save_enable: '保存并启用',
auto_save: '保存',
auto_configured: '已配置',
auto_customer_count: '当前客户',
auto_awaiting_reply: '等待回复',
auto_pending_followup: '待跟进',
auto_next_run: '下次执行',
auto_last_result: '最近结果',
auto_run_now: '立即运行',
auto_pause: '暂停',
auto_resume: '恢复',
auto_ai_help: 'AI 帮我配置',
auto_ai_generating: 'AI 正在生成配置…',
auto_no_automations: '还没有自动跟进',
auto_create_first: '创建一个自动跟进任务，AI 会帮你持续跟进未回复的客户',
auto_enabled_label: '已启用',
auto_paused_label: '已暂停',
auto_status: '状态',
auto_actions: '操作',

// --- activity ---
act_title: '操作记录',
act_ai_completed: 'AI 完成了',
act_result: '结果',
act_no_activities: '暂无操作记录',

// --- gmail ---
gmail_connected: 'Gmail 已连接',
gmail_not_connected: 'Gmail 未连接',
gmail_connect: '连接 Gmail',
gmail_disconnect: '断开连接',
gmail_syncing: '正在同步 Gmail…',
gmail_sync_complete: 'Gmail 同步完成',
gmail_sync_error: 'Gmail 同步失败',
gmail_configured: 'Gmail 已配置',
gmail_not_configured: '未配置 Gmail 凭证',
gmail_account: '已连接账号',

// --- error / loading ---
err_loading: '加载失败',
err_loading_dashboard: '加载仪表盘失败',
err_loading_inbox: '加载收件箱失败',
err_loading_campaigns: '加载营销活动失败',
err_loading_approvals: '加载审批数据失败',
err_loading_automation: '加载自动跟进失败',
err_retry: '点击重试',

// --- time ---
time_just_now: '刚刚',
time_minutes_ago: '{n} 分钟前',
time_hours_ago: '{n} 小时前',
time_days_ago: '{n} 天前',
time_today_at: '今天 {time}',
time_yesterday_at: '昨天 {time}',
time_format: '{date} {time}',

}, // end zh

en: {

// --- nav ---
nav_dashboard: 'Dashboard',
nav_inbox: 'Inbox',
nav_contacts: 'Contacts',
nav_campaigns: 'Campaigns',
nav_approvals: 'Approvals',
nav_automation: 'Automation',
nav_knowledge: 'Knowledge Base',
nav_agent_profile: 'Agent Settings',
nav_activity: 'Activity',
nav_agent_lab: 'Agent Lab',

// --- common buttons / actions ---
common_save: 'Save',
common_cancel: 'Cancel',
common_confirm: 'Confirm',
common_edit: 'Edit',
common_delete: 'Delete',
common_create: 'Create',
common_close: 'Close',
common_back: 'Back',
common_loading: 'Loading…',
common_processing: 'Processing…',
common_sending: 'Sending…',
common_refresh: 'Refresh',
common_search: 'Search',
common_filter: 'Filter',
common_all: 'All',
common_no_data: 'No data',
common_error: 'Error',
common_retry: 'Retry',
common_success: 'Success',
common_failed: 'Failed',
common_next: 'Next',
common_done: 'Done',
common_confirm_delete: 'Confirm Delete',
common_delete_warning: 'This action cannot be undone. Are you sure?',
common_copied: 'Copied',

// --- status labels ---
status_draft: 'Draft',
status_pending: 'Pending Review',
status_queued: 'Queued',
status_running: 'Running',
status_success: 'Successful',
status_failed: 'Failed',
status_paused: 'Paused',
status_enabled: 'Enabled',
status_disabled: 'Disabled',
status_active: 'Active',
status_archived: 'Archived',
status_stopped: 'Stopped',
inbox_stopped_notice: 'This customer asked to stop outreach. No further follow-ups will be sent.',
status_pending_review: 'Pending Review',
status_sent: 'Sent',
status_delivered: 'Delivered',
status_bounced: 'Bounced',
status_replied: 'Replied',
status_unsubscribed: 'Unsubscribed',
status_not_interested: 'Not Interested',
status_outreach_generated: 'Generated',
status_imported: 'Imported',
status_synced: 'Synced',

// --- dashboard ---
dash_title: 'Email Automation Workbench',
dash_email_automation: 'Email Automation',
dash_connected_emails: 'Connected Emails',
dash_active_campaigns: 'Active Campaigns',
dash_pending_approvals: 'Pending Approvals',
dash_sent_emails: 'Sent Emails',
dash_total_contacts: 'Total Contacts',
dash_scheduled_followups: 'Scheduled Follow-ups',
dash_recent_activity: 'Recent Activity',
dash_no_recent: 'No recent activity',

// --- inbox ---
inbox_title: 'Smart Inbox',
inbox_sync: 'Sync Inbox',
inbox_syncing: 'Syncing…',
inbox_sync_done: 'Sync complete',
inbox_sort_btn: 'AI Sort',
inbox_sort_btn_count: 'AI Sort {count} Emails',
inbox_sorting: 'AI is sorting…',
inbox_sort_done: 'Sorting complete',
inbox_no_threads: 'No emails',
inbox_no_threads_hint: 'Click "Sync Inbox" to fetch emails from Gmail',
inbox_unsorted: 'Unsorted',
inbox_valid_customer: 'Valid Customers',
inbox_needs_reply: 'Needs Reply',
inbox_has_interest: 'Interested',
inbox_not_now: 'Not Now',
inbox_rejected: 'Rejected',
inbox_irrelevant: 'Irrelevant',
inbox_unprocessed: 'Unprocessed',
inbox_ai_judgment: 'AI Judgment',
inbox_judgment_reason: 'Reasoning',
inbox_suggested_action: 'Suggested Action',
inbox_generate_reply: 'AI Generate Reply',
inbox_reply_draft_ready: 'Reply draft generated. Confirm in the Approvals tab.',
inbox_detail_loading: 'Loading email…',
inbox_no_detail: 'Email not found',
inbox_contact: 'Contact',
inbox_subject: 'Subject',
inbox_date: 'Date',
inbox_category: 'Category',
inbox_thread_count: '{count} threads',

// --- campaign ---
camp_title: 'Campaigns',
camp_create: 'Create Campaign',
camp_create_new: 'New Campaign',
camp_edit: 'Edit Campaign',
camp_import: 'Import Customers',
camp_generate: 'AI Generate Outreach',
camp_go_approve: 'Review & Send',
camp_name: 'Campaign Name',
camp_objective: 'Objective',
camp_product: 'Product Description',
camp_audience: 'Target Audience',
camp_sender_name: 'Sender Name',
camp_sender_company: 'Sender Company',
camp_tone: 'Email Tone',
camp_daily_limit: 'Daily Send Limit',
camp_send_window: 'Sending Window',
camp_timezone: 'Timezone',
camp_max_followups: 'Max Follow-ups',
camp_followup_days: 'Follow-up Interval (days)',
camp_approval_mode: 'Approval Mode',
camp_status: 'Status',
camp_actions: 'Actions',
camp_next_step: 'Next: Import Customers',
camp_no_campaigns: 'No campaigns yet',
camp_create_first: 'Create your first campaign',
camp_confirm_delete: 'Delete this campaign? Existing send records will be archived.',
camp_delete_ok: 'Campaign deleted/archived',
camp_pause: 'Pause',
camp_resume: 'Resume',
camp_paused_hint: 'Paused — no new emails will be sent',
camp_edit_tooltip: 'Edit campaign name and settings',
camp_delete_tooltip: 'Delete campaign (archive if has send records)',
camp_import_tooltip: 'Import contacts from a CSV file',
camp_generate_tooltip: 'Use AI to generate personalized outreach emails for all contacts',
camp_name_placeholder: 'e.g. Q3 North America Outreach',
camp_objective_placeholder: 'e.g. Introduce new product, book product demos',

// --- approval ---
appr_title: 'Approvals',
appr_approve_send: 'Approve & Send',
appr_reject: 'Reject',
appr_review: 'Review',
appr_send: 'Send',
appr_confirm_send: 'Confirm Send',
appr_confirm_send_title: 'Confirm Send',
appr_confirm_send_desc: 'This will send a real email to the recipient.',
appr_recipient: 'Recipient',
appr_subject: 'Subject',
appr_body: 'Body',
appr_generated_at: 'Generated At',
appr_ai_content: 'AI Generated Content',
appr_edit_allowed: 'You may edit the subject and body before sending',
appr_no_approvals: 'No pending approvals',
appr_no_approvals_hint: 'After generating outreach emails in Campaigns, approval items will appear here',
appr_quality: 'Quality Score',
appr_idempotent: 'Already sent — cannot send again',

// --- automation ---
auto_title: 'Follow-up Automation',
auto_create_title: 'Create Follow-up Automation',
auto_review_note: 'The system prepares follow-ups automatically. You review and send every email.',
auto_select_campaign: 'Select Campaign',
auto_no_campaign: 'Create a campaign and import customers first',
auto_no_campaign_hint: 'You need a campaign with customer data before setting up auto follow-up',
auto_first_email: 'First Email',
auto_immediate: 'Send Immediately',
auto_scheduled: 'Schedule',
auto_followup_after: 'Follow up if no reply after N days',
auto_followup_after_days: 'After {days} days',
auto_max_followups: 'Max Follow-ups',
auto_max_followups_count: '{count} times',
auto_send_window: 'Sending Window',
auto_send_window_format: '{start}:00 – {end}:00',
auto_stop_on_reply: 'Stop when reply received',
auto_stop_on_reject: 'Stop on rejection/unsubscribe',
auto_save_enable: 'Save & Enable',
auto_save: 'Save',
auto_configured: 'Configured',
auto_customer_count: 'Customers',
auto_awaiting_reply: 'Awaiting Reply',
auto_pending_followup: 'Pending Follow-up',
auto_next_run: 'Next Run',
auto_last_result: 'Last Result',
auto_run_now: 'Run Now',
auto_pause: 'Pause',
auto_resume: 'Resume',
auto_ai_help: 'AI Help Me Configure',
auto_ai_generating: 'AI generating configuration…',
auto_no_automations: 'No automations yet',
auto_create_first: 'Create an automation to prepare follow-ups for customers who have not replied',
auto_enabled_label: 'Enabled',
auto_paused_label: 'Paused',
auto_status: 'Status',
auto_actions: 'Actions',

// --- activity ---
act_title: 'Activity Log',
act_ai_completed: 'AI completed',
act_result: 'Result',
act_no_activities: 'No activity yet',

// --- gmail ---
gmail_connected: 'Gmail Connected',
gmail_not_connected: 'Gmail Not Connected',
gmail_connect: 'Connect Gmail',
gmail_disconnect: 'Disconnect',
gmail_syncing: 'Syncing Gmail…',
gmail_sync_complete: 'Gmail sync complete',
gmail_sync_error: 'Gmail sync failed',
gmail_configured: 'Gmail Configured',
gmail_not_configured: 'Gmail Not Configured',
gmail_account: 'Connected Account',

// --- error / loading ---
err_loading: 'Loading failed',
err_loading_dashboard: 'Failed to load dashboard',
err_loading_inbox: 'Failed to load inbox',
err_loading_campaigns: 'Failed to load campaigns',
err_loading_approvals: 'Failed to load approvals',
err_loading_automation: 'Failed to load automation',
err_retry: 'Click to retry',

// --- time ---
time_just_now: 'Just now',
time_minutes_ago: '{n} min ago',
time_hours_ago: '{n}h ago',
time_days_ago: '{n}d ago',
time_today_at: 'Today at {time}',
time_yesterday_at: 'Yesterday at {time}',
time_format: '{date} {time}',

}, // end en

}; // end DICT

// --------------- helpers ---------------
const LS_KEY = 'ea_lang';

/**
 * API timestamps without an offset originate from SQLite, whose datetime
 * adapter drops timezone metadata. They are UTC by contract, so normalise
 * them before the browser converts them to the current computer timezone.
 */
function parseApiDate(value: string): Date {
  const normalized = value.includes('T') ? value : value.replace(' ', 'T');
  const hasOffset = /(?:Z|[+-]\d{2}:?\d{2})$/i.test(normalized);
  return new Date(hasOffset ? normalized : `${normalized}Z`);
}

function resolveDict(lang: Lang): Record<string, any> {
  return DICT[lang] ?? DICT.zh;
}

// --------------- context ---------------
interface LangCtx {
  lang: Lang;
  t: (key: string, vars?: Record<string, string | number>) => string;
  setLang: (l: Lang) => void;
  formatDate: (iso: string | null | undefined, fallback?: string) => string;
}

const LangContext = createContext<LangCtx>({
  lang: 'zh',
  t: (k) => k,
  setLang: () => {},
  formatDate: () => '',
});

export function useLang() {
  return useContext(LangContext);
}

// --------------- provider ---------------
export function LangProvider({ children }: { children: React.ReactNode }) {
  const [lang, setLangState] = useState<Lang>('zh');

  // init from localStorage on mount
  useEffect(() => {
    try {
      const stored = localStorage.getItem(LS_KEY);
      if (stored === 'en' || stored === 'zh') {
        setLangState(stored);
      }
    } catch {}
  }, []);

  const setLang = useCallback((l: Lang) => {
    setLangState(l);
    try { localStorage.setItem(LS_KEY, l); } catch {}
  }, []);

  const t = useCallback(
    (key: string, vars?: Record<string, string | number>) => {
      const dict = resolveDict(lang);
      let val = key.split('.').reduce((obj: any, k) => obj?.[k], dict);
      if (val === undefined) val = key; // fallback to key
      if (vars) {
        for (const [k, v] of Object.entries(vars)) {
          val = String(val).replace(`{${k}}`, String(v));
        }
      }
      return String(val);
    },
    [lang],
  );

  const formatDate = useCallback(
    (iso: string | null | undefined, fallback = '—') => {
      if (!iso) return fallback;
      try {
        const d = parseApiDate(iso);
        if (isNaN(d.getTime())) return fallback;
        const locale = lang === 'zh' ? 'zh-CN' : 'en-US';
        return d.toLocaleString(locale, {
          month: 'short', day: 'numeric',
          hour: '2-digit', minute: '2-digit',
          timeZoneName: 'short',
        });
      } catch {
        return fallback;
      }
    },
    [lang],
  );

  return React.createElement(
    LangContext.Provider,
    { value: { lang, t, setLang, formatDate } },
    children,
  );
}

// non-hook t() for use outside components (rarely needed)
let _currentLang: Lang = 'zh';
try {
  const stored = typeof localStorage !== 'undefined' ? localStorage.getItem(LS_KEY) : null;
  if (stored === 'en' || stored === 'zh') _currentLang = stored;
} catch {}

export function getT(lang?: Lang) {
  const l = lang ?? _currentLang;
  return (key: string, vars?: Record<string, string | number>) => {
    const dict = resolveDict(l);
    let val = key.split('.').reduce((obj: any, k) => obj?.[k], dict);
    if (val === undefined) val = key;
    if (vars) {
      for (const [k, v] of Object.entries(vars)) {
        val = String(val).replace(`{${k}}`, String(v));
      }
    }
    return String(val);
  };
}
