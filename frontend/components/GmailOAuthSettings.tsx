"use client";

import { ChangeEvent, useEffect, useState } from "react";
import { FileKey2, Mail, ShieldCheck } from "lucide-react";
import { api } from "@/lib/api";
import { useLang } from "@/lib/i18n";

export default function GmailOAuthSettings() {
  const { lang } = useLang();
  const zh = lang === "zh";
  const [config, setConfig] = useState<any>(null);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");

  const load = async () => {
    try { setConfig(await api.gmailOauthConfig()); }
    catch (error: any) { setMessage(error?.message || (zh ? "读取 Gmail 配置失败" : "Failed to read Gmail configuration")); }
  };
  useEffect(() => { void load(); }, []);

  const importDesktopConfig = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = "";
    if (!file) return;
    setBusy(true); setMessage("");
    try {
      const parsed = JSON.parse(await file.text());
      const result = await api.gmailOauthSave(parsed);
      setConfig(result);
      setMessage(zh
        ? "Desktop OAuth 配置已安全保存。请点击页面顶部“连接 Gmail”，并在系统浏览器完成授权。"
        : "Desktop OAuth configuration saved securely. Click Connect Gmail and finish authorization in your system browser.");
    } catch (error: any) {
      setMessage(error instanceof SyntaxError
        ? (zh ? "文件不是有效的 JSON。" : "The selected file is not valid JSON.")
        : (error?.message || (zh ? "导入失败" : "Import failed")));
    } finally { setBusy(false); }
  };

  const state = config?.oauth_state || (config?.configured ? "oauth_not_connected" : "oauth_not_configured");
  return (
    <div className="card space-y-4">
      <div className="flex items-start gap-2">
        <Mail size={19} className="text-accent mt-0.5" />
        <div>
          <h2 className="font-semibold">{zh ? "Gmail 客户自有 OAuth" : "Customer-owned Gmail OAuth"}</h2>
          <p className="text-sm text-muted mt-1">{zh
            ? "从客户自己的 Google Cloud 项目导入 Desktop app credentials.json。配置、Token 与邮件数据只保存在当前电脑，不需要 API Key，也不需要手工填写 Redirect URI。"
            : "Import a Desktop app credentials.json from the customer's own Google Cloud project. Configuration, tokens, and mail data stay on this computer; no API key or manual redirect URI is required."}</p>
        </div>
      </div>

      <div className="flex items-center gap-2 text-sm">
        <span className={`badge ${state === "credential_key_unavailable" ? "text-danger border-danger" : config?.configured ? "text-ok border-ok" : "text-warn border-warn"}`}>
          {state === "credential_key_unavailable"
            ? (zh ? "凭据无法解密，需要重新导入" : "Credentials unavailable; import again")
            : config?.configured
              ? `${zh ? "已导入 Desktop OAuth" : "Desktop OAuth imported"}${config.client_id_hint ? ` (…${config.client_id_hint})` : ""}`
              : (zh ? "尚未导入" : "Not imported")}
        </span>
      </div>

      <ol className="text-sm text-muted list-decimal pl-5 space-y-1">
        <li>{zh ? "在 Google Cloud 启用 Gmail API 并配置 OAuth 同意屏幕。" : "Enable Gmail API and configure the OAuth consent screen in Google Cloud."}</li>
        <li>{zh ? "创建 OAuth Client ID，Application type 选择 Desktop app。" : "Create an OAuth Client ID with Application type set to Desktop app."}</li>
        <li>{zh ? "下载 credentials.json，并在下方导入。Web application 类型会被拒绝。" : "Download credentials.json and import it below. Web application credentials are rejected."}</li>
      </ol>

      <label className={`btn-primary inline-flex w-fit items-center gap-2 ${busy ? "opacity-60 pointer-events-none" : ""}`}>
        <FileKey2 size={15} /> {busy ? (zh ? "正在导入…" : "Importing…") : (zh ? "导入 Google Desktop OAuth 配置" : "Import Google Desktop OAuth configuration")}
        <input type="file" accept="application/json,.json" className="hidden" onChange={importDesktopConfig} disabled={busy} />
      </label>

      <div className="flex items-center gap-2 text-xs text-muted">
        <ShieldCheck size={14} className="text-ok" />
        {zh ? "配置使用当前 Windows 用户的 DPAPI 加密；读取接口不会返回 Client Secret 或 Token。" : "Configuration is protected with current-user Windows DPAPI; read APIs never return the client secret or tokens."}
      </div>
      {config?.redirect_uri && <div className="text-xs text-muted">Loopback callback: {config.redirect_uri}</div>}
      {message && <div className="text-sm text-muted">{message}</div>}
    </div>
  );
}