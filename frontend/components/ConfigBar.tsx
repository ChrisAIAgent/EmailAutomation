"use client";
import { useLang } from "@/lib/i18n";

export default function ConfigBar({ metrics, gmail, health, paused, onDisconnect, onConnect }: any) {
  const { t, lang } = useLang();
  const badges = [];

  // Gmail status
  if (!gmail?.connected) {
    badges.push(
      <span key="g" className="badge text-warn border-warn">
        {t('gmail_not_connected')}
      </span>
    );
  } else {
    badges.push(
      <span key="g" className="badge text-ok border-ok">
        {t('gmail_account')}: {gmail.email}
      </span>
    );
  }

  // Send mode
  if (!metrics) {
    badges.push(
      <span key="d" className="badge text-muted border-border">
        {lang === 'zh' ? '检查发送安全…' : 'Checking send safety…'}
      </span>
    );
  } else if (metrics.draft_only) {
    badges.push(
      <span key="d" className="badge text-warn border-warn">
        {lang === 'zh' ? '发送待启用' : 'Sending unavailable'}
      </span>
    );
  } else {
    badges.push(
      <span key="d" className="badge text-danger border-danger">
        {lang === 'zh' ? '真实发送已开启' : 'Real Send Enabled'}
      </span>
    );
  }

  // LangGraph agent health only (OpenClaw hidden per user request)
  const lg = health?.find((h: any) => h.agent === "langgraph");
  if (lg) {
    badges.push(
      <span key="lg" className={`badge ${lg.configured ? "text-ok border-ok" : "text-muted border-border"}`}>
        {lang === 'zh' ? 'AI 模型' : 'AI Model'}: {lg.configured ? (lang === 'zh' ? '已配置' : 'Ready') : (lang === 'zh' ? '未配置' : 'Not Configured')}
      </span>
    );
  }

  // Global pause
  if (paused) {
    badges.push(
      <span key="p" className="badge text-danger border-danger">
        {lang === 'zh' ? '全局已暂停' : 'Globally Paused'}
      </span>
    );
  }

  return (
    <div className="flex flex-wrap gap-2 items-center">
      {badges}
      {gmail?.connected ? (
        <button className="btn text-xs" onClick={onDisconnect}>
          {t('gmail_disconnect')}
        </button>
      ) : (
        <button className="btn-primary text-xs" onClick={onConnect}>
          {t('gmail_connect')}
        </button>
      )}
    </div>
  );
}
