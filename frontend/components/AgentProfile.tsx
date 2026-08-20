"use client";

import { useEffect, useState } from "react";
import { Bot, KeyRound, Save, ShieldCheck, Wifi } from "lucide-react";
import { api } from "@/lib/api";
import { useLang } from "@/lib/i18n";
import GmailOAuthSettings from "./GmailOAuthSettings";

export default function AgentProfileView() {
  const { lang } = useLang();
  const zh = lang === "zh";
  const [profile, setProfile] = useState<any>(null);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");

  const [emailAi, setEmailAi] = useState<any>(null);
  const [emailKey, setEmailKey] = useState("");
  const [busy, setBusy] = useState<"email" | null>(null);
  const [aiMessage, setAiMessage] = useState("");

  const load = async () => {
    try {
      setMessage("");
      const [nextProfile, eAi] = await Promise.all([
        api.agentProfile(),
        api.emailAiConfig(),
      ]);
      setProfile(nextProfile);
      setEmailAi(eAi);
    } catch (e: any) {
      setMessage(e?.message || (zh ? "Agent Profile 加载失败" : "Failed to load Agent Profile"));
    }
  };

  useEffect(() => { load(); }, []);

  const update = (key: string, value: any) => setProfile((p: any) => ({ ...p, [key]: value }));
  const updateEmail = (key: string, value: any) => setEmailAi((c: any) => ({ ...c, [key]: value }));

  const emailPayload = () => ({
    provider_name: emailAi.provider_name,
    base_url: emailAi.base_url,
    model: emailAi.model,
    api_key: emailKey || null,
  });

  const testEmail = async () => {
    if (!window.confirm(`${zh ? "API Key 将仅发送到以下 Provider 进行连接检查：" : "The API key will be sent only to this provider for a connection check:"}\n${emailAi.base_url}`)) return;
    setBusy("email"); setAiMessage("");
    try { const r = await api.testEmailAiConfig(emailPayload()); setAiMessage(`${zh ? "邮件 Agent 连接成功" : "Email Agent connected"}: ${r.model}`); }
    catch (e: any) { setAiMessage(e?.message || (zh ? "连接检查失败" : "Connection check failed")); }
    finally { setBusy(null); }
  };
  const saveEmail = async () => {
    setBusy("email"); setAiMessage("");
    try {
      setEmailAi(await api.updateEmailAiConfig(emailPayload()));
      setEmailKey("");
      setAiMessage(zh ? "邮件 Agent AI 配置已加密保存。请重启服务后生效。" : "Email Agent AI config saved. Restart services to apply.");
    }
    catch (e: any) { setAiMessage(e?.message || (zh ? "保存失败" : "Save failed")); }
    finally { setBusy(null); }
  };

  const save = async () => {
    setSaving(true);
    setMessage("");
    try {
      const {
        agent_name, company_name, role, tone, language_policy, signature_text,
        forbidden_claims, unknown_answer_policy, allow_campaign_override, is_active,
      } = profile;
      setProfile(await api.updateAgentProfile({
        agent_name, company_name, role, tone, language_policy, signature_text,
        forbidden_claims, unknown_answer_policy, allow_campaign_override, is_active,
      }));
      setMessage(zh ? "Agent Profile 已保存并将用于下一次 LangGraph 运行。" : "Agent Profile saved for the next LangGraph run.");
    } catch (e: any) {
      setMessage(e?.message || (zh ? "保存失败" : "Save failed"));
    } finally {
      setSaving(false);
    }
  };

  if (!profile || !emailAi) return <div className="card text-sm text-muted">{message || (zh ? "正在加载…" : "Loading…")}</div>;

  return (
    <div className="space-y-4 max-w-5xl">
      <div>
        <div className="flex items-center gap-2">
          <Bot size={22} className="text-brand" />
          <h1 className="text-xl font-semibold">{zh ? "全局 Agent Profile" : "Global Agent Profile"}</h1>
        </div>
        <p className="mt-1 text-sm text-muted">
          {zh
            ? "这里控制全局邮件与 Campaign 共用的人设、语言、语气和强制签名。知识库只提供事实，不替代这些运行配置。"
            : "Controls identity, language, tone, and mandatory signature. Knowledge provides facts; it does not replace runtime settings."}
        </p>
      </div>

      <div className="card grid gap-4 md:grid-cols-2">
        <Field label={zh ? "Agent 名称" : "Agent name"} value={profile.agent_name} onChange={(v) => update("agent_name", v)} />
        <Field label={zh ? "公司名称" : "Company"} value={profile.company_name} onChange={(v) => update("company_name", v)} />
        <Field label={zh ? "角色" : "Role"} value={profile.role} onChange={(v) => update("role", v)} />
        <Field label={zh ? "语气" : "Tone"} value={profile.tone} onChange={(v) => update("tone", v)} />
        <label className="text-sm">
          <span className="block text-muted mb-1">{zh ? "语言策略" : "Language policy"}</span>
          <select className="input w-full" value={profile.language_policy} onChange={(e) => update("language_policy", e.target.value)}>
            <option value="match_customer">{zh ? "跟随客户语言" : "Match customer"}</option>
            <option value="chinese">{zh ? "固定中文" : "Chinese"}</option>
            <option value="english">{zh ? "固定英文" : "English"}</option>
          </select>
        </label>
        <div className="flex items-end">
          <label className="flex items-center gap-2 text-sm">
            <input type="checkbox" checked={profile.allow_campaign_override} onChange={(e) => update("allow_campaign_override", e.target.checked)} />
            {zh ? "允许 Campaign 覆盖姓名、公司和语气" : "Allow Campaign identity/tone override"}
          </label>
        </div>
        <Area label={zh ? "强制签名" : "Required signature"} value={profile.signature_text} onChange={(v) => update("signature_text", v)} />
        <Area label={zh ? "禁止承诺" : "Forbidden claims"} value={profile.forbidden_claims} onChange={(v) => update("forbidden_claims", v)} />
        <div className="md:col-span-2">
          <Area label={zh ? "未知问题处理规则" : "Unknown-answer policy"} value={profile.unknown_answer_policy} onChange={(v) => update("unknown_answer_policy", v)} />
        </div>
      </div>

      <div className="card space-y-4">
        <div className="flex items-start gap-2"><KeyRound size={19} className="text-accent mt-0.5" /><div><h2 className="font-semibold">{zh ? "邮件 Agent AI（LLM）" : "Email Agent AI (LLM)"}</h2><p className="text-sm text-muted mt-1">{zh ? "Email Automation 分拣、回复与自动化所用的唯一 LLM。TACWork 对话 AI 请在其自身的 Web 端 AI Providers 中配置，此处不重复。" : "The only LLM used by Email Automation for triage, replies and automation. Configure TACWork's conversation AI in its own Web UI (AI Providers); it is not set here."}</p></div></div>
        <div className="grid gap-3 md:grid-cols-2">
          <Field label={zh ? "Provider 名称" : "Provider name"} value={emailAi.provider_name} onChange={(v) => updateEmail("provider_name", v)} />
          <Field label={zh ? "模型 ID / Endpoint ID" : "Model / endpoint ID"} value={emailAi.model} onChange={(v) => updateEmail("model", v)} />
          <div className="md:col-span-2"><Field label={zh ? "API Base URL（HTTPS）" : "API base URL (HTTPS)"} value={emailAi.base_url} onChange={(v) => updateEmail("base_url", v)} /></div>
          <label className="text-sm md:col-span-2"><span className="block text-muted mb-1">API Key</span><input type="password" autoComplete="new-password" className="input w-full" value={emailKey} onChange={e => setEmailKey(e.target.value)} placeholder={emailAi.api_key_configured ? (zh ? "已配置；留空保持不变" : "Configured; leave blank to keep") : (zh ? "输入 API Key" : "Enter API key")} /></label>
        </div>
        <div className="flex flex-wrap gap-2">
          <button className="btn flex items-center gap-2" disabled={!!busy} onClick={testEmail}><Wifi size={14} />{busy === "email" ? (zh ? "检查中…" : "Checking…") : (zh ? "检查邮件 Agent 连接" : "Check Email Agent connection")}</button>
          <button className="btn-primary flex items-center gap-2" disabled={!!busy} onClick={saveEmail}><Save size={14} />{busy === "email" ? (zh ? "保存中…" : "Saving…") : (zh ? "加密保存配置" : "Save encrypted config")}</button>
        </div>
      </div>

      <div className="text-xs text-muted">{zh ? "Key 使用 Workspace 加密密钥保存，浏览器和读取 API 不会回显。连接检查会先显示目标域名并要求确认。" : "Keys are encrypted locally and never returned by read APIs. Connection checks show and confirm the target first."}</div>
      {aiMessage && <div className="text-sm text-muted">{aiMessage}</div>}

      <GmailOAuthSettings />

      <div className="card flex flex-wrap items-center justify-between gap-3">
        <div className="flex items-center gap-2 text-sm text-muted">
          <ShieldCheck size={16} className="text-ok" />
          {zh ? `当前版本 v${profile.version}；生成后会强制校验签名。` : `Version ${profile.version}; signature is enforced after generation.`}
        </div>
        <button className="btn-primary flex items-center gap-2" disabled={saving} onClick={save}>
          <Save size={15} /> {saving ? (zh ? "保存中…" : "Saving…") : (zh ? "保存设置" : "Save settings")}
        </button>
      </div>
      {message && <div className="text-sm text-muted">{message}</div>}
    </div>
  );
}

function Field({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  return <label className="text-sm"><span className="block text-muted mb-1">{label}</span><input className="input w-full" value={value || ""} onChange={(e) => onChange(e.target.value)} /></label>;
}

function Area({ label, value, onChange }: { label: string; value: string; onChange: (value: string) => void }) {
  return <label className="text-sm"><span className="block text-muted mb-1">{label}</span><textarea className="input w-full min-h-28" value={value || ""} onChange={(e) => onChange(e.target.value)} /></label>;
}
