"use client";

import { useEffect, useState } from "react";
import { Mail, Save, ShieldCheck } from "lucide-react";
import { api } from "@/lib/api";
import { useLang } from "@/lib/i18n";

export default function GmailOAuthSettings() {
  const { lang } = useLang();
  const zh = lang === "zh";
  const [configured, setConfigured] = useState<boolean | null>(null);
  const [clientIdHint, setClientIdHint] = useState("");
  const [redirectUri, setRedirectUri] = useState("http://127.0.0.1:8000/api/gmail/oauth/callback");
  const [clientId, setClientId] = useState("");
  const [clientSecret, setClientSecret] = useState("");
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const load = async () => {
    try {
      const cfg = await api.gmailOauthConfig();
      setConfigured(!!cfg.configured);
      setClientIdHint(cfg.client_id_hint || "");
      if (cfg.redirect_uri) setRedirectUri(cfg.redirect_uri);
    } catch (e: any) {
      setMessage(e?.message || (zh ? "读取 Gmail 配置失败" : "Failed to read Gmail config"));
    }
  };

  useEffect(() => { load(); }, []);

  const save = async () => {
    setBusy(true);
    setMessage("");
    try {
      await api.gmailOauthSave({ client_id: clientId, client_secret: clientSecret, redirect_uri: redirectUri });
      setConfigured(true);
      setClientIdHint(clientId.slice(-4));
      setClientSecret("");
      setMessage(zh
        ? "Google OAuth 凭证已保存。现在可以点击顶部「连接 Gmail」完成授权。"
        : "Google OAuth credentials saved. You can now click \"Connect Gmail\" to authorize.");
    } catch (e: any) {
      setMessage(e?.message || (zh ? "保存失败" : "Save failed"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="card space-y-4">
      <div className="flex items-start gap-2">
        <Mail size={19} className="text-accent mt-0.5" />
        <div>
          <h2 className="font-semibold">{zh ? "Gmail OAuth 凭证" : "Gmail OAuth Credentials"}</h2>
          <p className="text-sm text-muted mt-1">
            {zh
              ? "新电脑首次运行需在此填写 Google Cloud 控制台取得的 OAuth 客户端 ID 与密钥。填写后才能在设置向导里连接 Gmail。凭证仅保存在本机 backend/.env，不会外传。"
              : "On a fresh PC, paste the OAuth Client ID and Secret from Google Cloud Console here. After saving, use \"Connect Gmail\" to authorize. Credentials are stored only in this machine's backend/.env."}
          </p>
        </div>
      </div>

      <div className="flex items-center gap-2 text-sm">
        {configured === null ? (
          <span className="badge text-muted border-border">{zh ? "检查中…" : "Checking…"}</span>
        ) : configured ? (
          <span className="badge text-ok border-ok">{zh ? "已配置" : "Configured"}{clientIdHint ? ` (…${clientIdHint})` : ""}</span>
        ) : (
          <span className="badge text-warn border-warn">{zh ? "未配置" : "Not configured"}</span>
        )}
      </div>

      <div className="grid gap-3 md:grid-cols-2">
        <Field label={zh ? "Google Client ID" : "Google Client ID"} value={clientId} onChange={setClientId} placeholder={zh ? "例如 123456-abc.apps.googleusercontent.com" : "e.g. 123456-abc.apps.googleusercontent.com"} />
        <Field label={zh ? "Google Client Secret" : "Google Client Secret"} value={clientSecret} onChange={setClientSecret} password placeholder={zh ? "留空表示不修改（仅首次必填）" : "Blank to keep (required on first setup)"} />
      </div>

      <label className="text-sm">
        <span className="block text-muted mb-1">{zh ? "重定向 URI（需在 Google Cloud 授权回调中登记）" : "Redirect URI (must be registered in Google Cloud Console)"}</span>
        <input className="input w-full" value={redirectUri} onChange={(e) => setRedirectUri(e.target.value)} />
      </label>

      <div className="flex items-center gap-2 text-xs text-muted">
        <ShieldCheck size={14} className="text-ok" />
        {zh ? "凭证使用本机加密保存，读取接口不会回显密钥。" : "Credentials are stored locally; read APIs never return the secret."}
      </div>

      <div className="flex flex-wrap gap-2">
        <button className="btn-primary flex items-center gap-2" disabled={busy} onClick={save}>
          <Save size={14} /> {busy ? (zh ? "保存中…" : "Saving…") : (zh ? "保存 OAuth 凭证" : "Save OAuth credentials")}
        </button>
      </div>

      {message && <div className="text-sm text-muted">{message}</div>}
    </div>
  );
}

function Field({ label, value, onChange, placeholder, password }: { label: string; value: string; onChange: (value: string) => void; placeholder?: string; password?: boolean }) {
  return (
    <label className="text-sm">
      <span className="block text-muted mb-1">{label}</span>
      <input type={password ? "password" : "text"} autoComplete="new-password" className="input w-full" value={value} placeholder={placeholder} onChange={(e) => onChange(e.target.value)} />
    </label>
  );
}
