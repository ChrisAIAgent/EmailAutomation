// Frontend API client. All data comes from the backend (DB + real runs).
declare global { interface Window { __EMAIL_AUTOMATION_RUNTIME__?: { apiUrl?: string; tacworkUrl?: string; tacworkServerUrl?: string; version?: string }; emailAutomation?: { openExternal: (url: string) => Promise<unknown>; openLogs: () => Promise<unknown>; repair: () => Promise<unknown>; quitAndStop: () => Promise<unknown>; getRuntimeStatus: () => Promise<unknown> } } }
const runtimeConfig = typeof window !== "undefined" ? window.__EMAIL_AUTOMATION_RUNTIME__ : undefined;
export const API_BASE = runtimeConfig?.apiUrl || process.env.NEXT_PUBLIC_API_URL || "http://127.0.0.1:18000";

// Keep ordinary dashboard reads snappy, but real Gmail/model operations often
// take 20-60s in a live demo. Those long-running actions pass an explicit
// timeout below so the UI does not report a false failure while the backend is
// still doing real work.
const DEFAULT_TIMEOUT_MS = 10_000;
const LONG_ACTION_TIMEOUT_MS = 90_000;

// file upload: no Content-Type header (browser auto-sets multipart/form-data boundary)
async function uploadReq<T>(path: string, body: FormData): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), 30_000);  // longer timeout for uploads
  try {
    const res = await fetch(API_BASE + path, { method: "POST", body, signal: controller.signal });
    if (!res.ok) { const text = await res.text(); throw new Error(`${res.status}: ${text}`); }
    return res.json() as Promise<T>;
  } finally { clearTimeout(timer); }
}

async function req<T>(path: string, init?: RequestInit, timeoutMs: number = DEFAULT_TIMEOUT_MS): Promise<T> {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const res = await fetch(API_BASE + path, {
      headers: { "Content-Type": "application/json" },
      signal: controller.signal,
      ...init,
    });
    if (!res.ok) {
      const text = await res.text();
      throw new Error(`${res.status}: ${text}`);
    }
    return res.json() as Promise<T>;
  } catch (e: any) {
    if (e?.name === "AbortError") {
      throw new Error(`Request timeout (${Math.round(timeoutMs / 1000)}s): ${path}`);
    }
    throw e;
  } finally {
    clearTimeout(timer);
  }
}

export function parseApiError(e: any): { status: number | null; code: string | null; raw: string } {
  const raw = String(e?.message ?? e ?? "");
  const match = raw.match(/^(\d{3}):\s*([\s\S]*)$/);
  const status = match ? Number(match[1]) : null;
  const body = match ? match[2] : raw;
  let code: string | null = null;
  try {
    const parsed = JSON.parse(body);
    code = parsed?.detail ?? parsed?.error?.code ?? null;
  } catch {
    // Unknown non-JSON errors retain their original text below.
  }
  return { status, code: code ? String(code) : null, raw };
}

export function friendlyError(e: any, zh: boolean): string {
  const { status, code, raw } = parseApiError(e);
  if (raw.startsWith("Request timeout")) return raw;
  const key = (code ?? "").toLowerCase();
  if (key === "initial_import_required") {
    return zh ? "需先完成首次历史导入，再执行首次历史分拣。" : "Complete the first mail history import before running first history triage.";
  }
  if (key === "initial_triage_no_failed_items") {
    return zh ? "当前没有失败项可重试。" : "There are no failed items to retry.";
  }
  if (key.startsWith("initial_triage_is_")) {
    const state = key.replace("initial_triage_is_", "");
    return zh ? `任务当前状态为 ${state}，无法执行该操作。` : `The run is ${state}; this action is not allowed.`;
  }
  if (key === "initial_triage_not_found" || key === "initial_triage_action_not_found") {
    return zh ? "未找到对应的首次分拣任务或操作。" : "First-triage run or action not found.";
  }
  if (key === "gmail_sync_failed") {
    return zh ? "Gmail 同步暂时遇到网络连接中断，请稍后重试；已同步的邮件不会丢失。" : "Gmail sync was interrupted by a network connection issue. Retry shortly; already-synced mail is preserved.";
  }
  if (status && code) return `${status}: ${code}`;
  return raw;
}

// Every method declares a concrete return type so callers never receive `unknown`.
export const api = {
  health: () => req<any>("/api/health"),
  gmailStatus: () => req<any>("/api/gmail/status"),
  gmailStart: () => req<any>("/api/gmail/oauth/start"),
  gmailDisconnect: () => req<any>("/api/gmail/disconnect", { method: "POST" }),
  gmailOauthConfig: () => req<any>("/api/gmail/oauth-config"),
  gmailOauthSave: (credentials: Record<string, unknown>) =>
    req<any>("/api/gmail/oauth-config", { method: "PUT", body: JSON.stringify({ credentials }) }),
  gmailSync: () => req<any>("/api/gmail/sync", { method: "POST" }, LONG_ACTION_TIMEOUT_MS),
  gmailImportStatus: () => req<any>("/api/gmail/imports/current"),
  gmailImportStart: () => req<any>("/api/gmail/imports", { method: "POST" }),
  gmailImportPause: (id: number) => req<any>(`/api/gmail/imports/${id}/pause`, { method: "POST" }),
  gmailImportResume: (id: number) => req<any>(`/api/gmail/imports/${id}/resume`, { method: "POST" }),
  gmailImportCancel: (id: number) => req<any>(`/api/gmail/imports/${id}/cancel`, { method: "POST" }),

  contacts: (query = "") => req<any[]>(`/api/contacts${query ? `?${query}` : ""}`),
  createContact: (body: any) => req<any>("/api/contacts", { method: "POST", body: JSON.stringify(body) }),
  updateContact: (id: number, body: any) => req<any>(`/api/contacts/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  deleteContact: (id: number) => req<any>(`/api/contacts/${id}`, { method: "DELETE" }),
  importContacts: (file: File, confirm: boolean) =>
    uploadReq<any>(`/api/contacts/import?confirm=${confirm}`, (() => { const fd = new FormData(); fd.append("file", file); return fd; })()),

  campaigns: () => req<any[]>("/api/campaigns"),
  createCampaign: (body: any) =>
    req<any>("/api/campaigns", { method: "POST", body: JSON.stringify(body) }),
  updateCampaign: (id: number, body: any) =>
    req<any>(`/api/campaigns/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  campaignContacts: (id: number, includeRemoved = false) =>
    req<any[]>(`/api/campaigns/${id}/contacts?include_removed=${includeRemoved}`),
  addCampaignContacts: (id: number, contactIds: number[]) =>
    req<any>(`/api/campaigns/${id}/contacts`, { method: "POST", body: JSON.stringify({ contact_ids: contactIds }) }),
  removeCampaignContact: (id: number, contactId: number) =>
    req<any>(`/api/campaigns/${id}/contacts/${contactId}`, { method: "DELETE" }),
  importCsv: (id: number, body: any) =>
    req<any>(`/api/campaigns/${id}/import-csv`, { method: "POST", body: JSON.stringify(body) }),
  // file upload: XLSX/CSV preview + import
  uploadContacts: (id: number, file: File, confirm: boolean) =>
    uploadReq<any>(`/api/campaigns/${id}/upload-contacts?confirm=${confirm}`, (() => { const fd = new FormData(); fd.append("file", file); return fd; })()),
  generate: (id: number) => req<any>(`/api/campaigns/${id}/generate`, { method: "POST" }, LONG_ACTION_TIMEOUT_MS),
  startCampaign: (id: number) => req<any>(`/api/campaigns/${id}/start`, { method: "POST" }),
  pauseCampaign: (id: number) => req<any>(`/api/campaigns/${id}/pause`, { method: "POST" }),
  stopCampaign: (id: number) => req<any>(`/api/campaigns/${id}/stop`, { method: "POST" }),
  deleteCampaign: (id: number) => req<any>(`/api/campaigns/${id}`, { method: "DELETE" }),

  inboxThreads: () => req<any[]>("/api/inbox/threads"),
  inboxCustomers: () => req<any[]>("/api/inbox/customers"),
  inboxStats: () => req<any>("/api/inbox/stats"),
  initialTriageStatus: () => req<any>("/api/inbox/initial-triage/current"),
  startInitialTriage: () => req<any>("/api/inbox/initial-triage", { method: "POST" }),
  controlInitialTriage: (id: number, action: "pause" | "resume" | "cancel") =>
    req<any>(`/api/inbox/initial-triage/${id}/${action}`, { method: "POST" }),
  retryInitialTriageFailed: (id: number) =>
    req<any>(`/api/inbox/initial-triage/${id}/retry-failed`, { method: "POST" }),
  dailyTriageStatus: () => req<any>("/api/inbox/daily-triage/current"),
  recentTriageResult: () => req<any>("/api/inbox/triage/recent"),
  startDailyTriage: () => req<any>("/api/inbox/daily-triage", { method: "POST" }),
  controlDailyTriage: (id: number, action: "pause" | "resume" | "cancel") =>
    req<any>(`/api/inbox/daily-triage/${id}/${action}`, { method: "POST" }),
  retryDailyTriageFailed: (id: number) =>
    req<any>(`/api/inbox/daily-triage/${id}/retry-failed`, { method: "POST" }),
  sortInbox: () => req<any>("/api/inbox/sort", { method: "POST" }, LONG_ACTION_TIMEOUT_MS),
  threadDetail: (id: number) => req<any>(`/api/inbox/threads/${id}`),
  analyzeThread: (id: number) => req<any>(`/api/inbox/threads/${id}/analyze`, { method: "POST" }, LONG_ACTION_TIMEOUT_MS),
  generateReply: (id: number) => req<any>(`/api/inbox/threads/${id}/generate-reply`, { method: "POST" }, LONG_ACTION_TIMEOUT_MS),
  addInboxContact: (id: number, body: any) =>
    req<any>(`/api/inbox/threads/${id}/contact`, { method: "POST", body: JSON.stringify(body) }),
  resolveHumanReview: (id: number, decision: "approve" | "reject" | "agent_decide") =>
    req<any>(`/api/inbox/threads/${id}/human-review`, { method: "POST", body: JSON.stringify({ decision }) }),
  enableSenderContentReviewIgnore: (email: string) =>
    req<any>(`/api/inbox/senders/${encodeURIComponent(email)}/content-review/ignore`, { method: "POST" }),
  removeSenderContentReviewIgnore: (email: string) =>
    req<any>(`/api/inbox/senders/${encodeURIComponent(email)}/content-review/ignore`, { method: "DELETE" }),
  clearStaleInboxReview: (id: number) =>
    req<any>(`/api/inbox/threads/${id}/clear-stale-review`, { method: "POST" }),
  nonCustomerFilters: () => req<any[]>("/api/inbox/non-customer-filters"),
  removeNonCustomerFilter: (id: number) => req<any>(`/api/inbox/non-customer-filters/${id}`, { method: "DELETE" }),

  approvals: (kind?: string, status = "pending") =>
    req<any[]>(`/api/approvals?status=${status}${kind ? `&kind=${kind}` : ""}`),
  decideApproval: (id: number, body: any) =>
    req<any>(`/api/approvals/${id}/decision`, { method: "POST", body: JSON.stringify(body) }),

  comparisons: () => req<any[]>("/api/comparisons"),
  comparisonDetail: (key: string) => req<any>(`/api/comparisons/${key}`),
  comparisonMetrics: () => req<any>("/api/comparisons/metrics/summary"),
  selectComparison: (key: string, body: any) =>
    req<any>(`/api/comparisons/${key}/select`, { method: "POST", body: JSON.stringify(body) }),

  knowledgeDocuments: () => req<any[]>("/api/knowledge"),
  createKnowledge: (body: any) =>
    req<any>("/api/knowledge", { method: "POST", body: JSON.stringify(body) }),
  updateKnowledge: (id: number, body: any) =>
    req<any>(`/api/knowledge/${id}`, { method: "PUT", body: JSON.stringify(body) }),
  uploadKnowledge: (file: File, title: string, category: string, publish: boolean) =>
    uploadReq<any>("/api/knowledge/upload", (() => {
      const form = new FormData();
      form.append("file", file);
      if (title) form.append("title", title);
      form.append("category", category || "general");
      form.append("publish", String(publish));
      return form;
    })()),
  publishKnowledge: (id: number) =>
    req<any>(`/api/knowledge/${id}/publish`, { method: "POST" }),
  disableKnowledge: (id: number) =>
    req<any>(`/api/knowledge/${id}/disable`, { method: "POST" }),
  deleteKnowledge: (id: number) =>
    req<any>(`/api/knowledge/${id}`, { method: "DELETE" }),
  searchKnowledge: (query: string, limit = 4) =>
    req<any>("/api/knowledge/search", { method: "POST", body: JSON.stringify({ query, limit }) }),
  agentProfile: () => req<any>("/api/agent-profile"),
  updateAgentProfile: (body: any) =>
    req<any>("/api/agent-profile", { method: "PUT", body: JSON.stringify(body) }),
  updateApprovalMode: (approval_mode: "human_review" | "agent_review") =>
    req<any>("/api/agent-profile/approval-mode", { method: "PUT", body: JSON.stringify({ approval_mode }) }),
  emailAiConfig: () => req<any>("/api/agent-profile/email-ai-config"),
  testEmailAiConfig: (body: any) => req<any>("/api/agent-profile/email-ai-config/test", { method: "POST", body: JSON.stringify(body) }, 30_000),
  updateEmailAiConfig: (body: any) => req<any>("/api/agent-profile/email-ai-config", { method: "PUT", body: JSON.stringify(body) }),

  metrics: () => req<Metrics>("/api/dashboard/metrics"),
  readiness: () => req<any>("/api/dashboard/readiness"),
  activity: () => req<any[]>("/api/dashboard/activity"),

  agentHealth: () => req<any[]>("/api/agent/health"),
  pauseStatus: () => req<any>("/api/system/pause"),
  setPause: (paused: boolean) =>
    req<any>(`/api/system/pause?paused=${paused}`, { method: "POST" }),

  agentTakeover: () => req<any>("/api/agent-takeover"),
  updateAgentTakeover: (enabled: boolean, intervalMinutes: number, displayTimezone?: string) =>
    req<any>("/api/agent-takeover", { method: "POST", body: JSON.stringify({ enabled, interval_minutes: intervalMinutes, display_timezone: displayTimezone }) }),
  updateWorkspaceDisplayTimezone: (displayTimezone: string) =>
    req<any>("/api/agent-takeover/display-timezone", { method: "POST", body: JSON.stringify({ display_timezone: displayTimezone }) }),
  runAgentTakeoverNow: () =>
    req<any>("/api/agent-takeover/run-now", { method: "POST" }, LONG_ACTION_TIMEOUT_MS),

  // Automation
  automations: () => req<any>("/api/automation"),
  automationSchedulerStatus: () => req<any>("/api/automation/scheduler/status"),
  runDueAutomations: () => req<any>("/api/automation/scheduler/run-due", { method: "POST" }),
  globalAutomation: () => req<any>("/api/automation/global"),
  updateGlobalAutomation: (body: { enabled: boolean; mode?: "full_auto" | "semi_auto"; tick_interval_minutes?: number; takeover_scope?: "recent_days" | "all_business" | "future_only"; takeover_days?: number }) =>
    req<any>("/api/automation/global", { method: "POST", body: JSON.stringify(body) }),
  automationDetail: (id: number) => req<any>(`/api/automation/${id}`),
  generateAutomation: (campaignId: number | null, prompt: string, scope = "campaign") =>
    req<any>("/api/automation/generate", { method: "POST", body: JSON.stringify({ campaign_id: campaignId, prompt, scope }) }, LONG_ACTION_TIMEOUT_MS),
  createAutomation: (campaignId: number | null, prompt: string, plan: any, name?: string, scope = "campaign") =>
    req<any>("/api/automation", { method: "POST", body: JSON.stringify({ campaign_id: campaignId, prompt, plan, name, scope }) }),
  enableAutomation: (id: number) => req<any>(`/api/automation/${id}/enable`, { method: "POST" }),
  pauseAutomation: (id: number) => req<any>(`/api/automation/${id}/pause`, { method: "POST" }),
  updateAutomationSchedule: (id: number, tickIntervalMinutes: number) =>
    req<any>(`/api/automation/${id}/schedule`, { method: "POST", body: JSON.stringify({ tick_interval_minutes: tickIntervalMinutes }) }),
  runNowAutomation: (id: number) => req<any>(`/api/automation/${id}/run-now`, { method: "POST" }, LONG_ACTION_TIMEOUT_MS),
  deleteAutomation: (id: number) => req<any>(`/api/automation/${id}`, { method: "DELETE" }),

  agentRun: (automationId: number, mode: "full_auto" | "semi_auto") =>
    req<any>("/api/agent-runs", { method: "POST", body: JSON.stringify({ automation_id: automationId, mode }) }, LONG_ACTION_TIMEOUT_MS),
  agentRunDetail: (runId: number) => req<any>(`/api/agent-runs/${runId}`),
  confirmAgentRun: (runId: number, confirmedBy?: string) =>
    req<any>(`/api/agent-runs/${runId}/confirm`, { method: "POST", body: JSON.stringify({ confirmed_by: confirmedBy }) }, LONG_ACTION_TIMEOUT_MS),
  cancelAgentRun: (runId: number) => req<any>(`/api/agent-runs/${runId}/cancel`, { method: "POST" }),
};

export type Metrics = {
  connected_emails: number;
  total_contacts: number;
  active_campaigns: number;
  sent_today: number;
  replies: number;
  positive_replies: number;
  needs_reply: number;
  pending_approvals: number;
  scheduled_follow_ups: number;
  failed_tasks: number;
  real_send_enabled: boolean;
  draft_only: boolean;
  openclaw_connected: boolean;
  langgraph_configured: boolean;
};
