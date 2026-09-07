"use client";

import { useState } from "react";
import { AlertTriangle, CheckCircle2, Info, Play, Search, Stethoscope, XCircle } from "lucide-react";
import { api, DiagnosticReport, InvestigateItemReport } from "@/lib/api";
import { useLang } from "@/lib/i18n";

// Diagnostics are deliberately manual. There is no on-mount fetch and no
// polling anywhere in this component: a health check only runs when the
// operator clicks it, which keeps idle installations free of recurring load.

const STATUS_META: Record<string, { color: string; icon: any }> = {
  ok: { color: "text-ok", icon: CheckCircle2 },
  warn: { color: "text-warn", icon: AlertTriangle },
  error: { color: "text-danger", icon: XCircle },
  info: { color: "text-muted", icon: Info },
};

const INVESTIGATE_STATUS: Record<string, string> = {
  confirmed: "text-danger",
  suspected: "text-warn",
  healthy: "text-ok",
  unknown: "text-muted",
};

function overallLabel(overall: string | undefined, zh: boolean): { text: string; cls: string } {
  if (overall === "error") return { text: zh ? "存在异常" : "Issues found", cls: "border-danger text-danger" };
  if (overall === "degraded") return { text: zh ? "存在告警" : "Warnings", cls: "border-warn text-warn" };
  if (overall === "unknown") return { text: zh ? "无法确认（总览诊断失败）" : "Unknown (overview failed)", cls: "border-muted text-muted" };
  return { text: zh ? "全部正常" : "All healthy", cls: "border-ok text-ok" };
}

export default function Diagnostics() {
  const { t, lang } = useLang();
  const zh = lang === "zh";

  const [overview, setOverview] = useState<DiagnosticReport | null>(null);
  const [checking, setChecking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [reports, setReports] = useState<Record<string, InvestigateItemReport>>({});
  const [investigating, setInvestigating] = useState<string | null>(null);
  const [investigateError, setInvestigateError] = useState<string | null>(null);

  const runCheck = async () => {
    setChecking(true);
    setError(null);
    try {
      const data = await api.diagnostics();
      setOverview(data);
      setReports({});
    } catch (e: any) {
      setError(e?.message || (zh ? "体检失败：请确认后端服务已启动。" : "Health check failed: confirm the backend is running."));
    } finally {
      setChecking(false);
    }
  };

  const runInvestigate = async (target: string) => {
    setInvestigating(target);
    setInvestigateError(null);
    try {
      const data = await api.investigate(target);
      const first = data?.reports?.[0];
      if (first) {
        setReports((prev) => ({ ...prev, [target]: { ...first, investigated_at: data.generated_at } }));
      }
    } catch (e: any) {
      // Keep the previous report on screen; only surface a failure banner.
      setInvestigateError(e?.message || "");
    } finally {
      setInvestigating(null);
    }
  };

  const ov = overallLabel(overview?.overall, zh);
  const failing = (overview?.items || []).filter((i) => i.status === "error" || i.status === "warn");

  return (
    <div>
      <div className="flex items-center justify-between mb-3">
        <h1 className="text-xl font-semibold flex items-center gap-2">
          <Stethoscope size={20} /> {t("nav_diagnostics")}
        </h1>
        <div className="flex items-center gap-2">
          {overview && (
            <button type="button" className="btn" onClick={runCheck} disabled={checking} aria-busy={checking}>
              <Play size={14} /> {checking ? (zh ? "体检中…" : "Checking…") : (zh ? "重新体检" : "Re-check")}
            </button>
          )}
          <button type="button" className="btn text-xs" onClick={() => window.emailAutomation?.openLogs?.()}>
            {zh ? "打开日志目录" : "Open logs"}
          </button>
        </div>
      </div>

      {error && (
        <div className="mb-3 rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger">{error}</div>
      )}

      {investigateError && (
        <div className="mb-3 rounded-lg border border-warn/40 bg-warn/10 px-3 py-2 text-sm text-warn">
          {t("diag_failed_keep")}{investigateError ? `：${investigateError}` : ""}
        </div>
      )}

      {!overview && (
        <div className="card">
          <div className="font-medium mb-1">{zh ? "按需诊断，不常驻运行" : "On-demand diagnostics only"}</div>
          <p className="text-sm text-muted mb-3">
            {zh
              ? "本页不会自动轮询，也不会在后台占用接口。只在你发现异常时，手动体检一次以定位出问题的环节。"
              : "This page never polls and adds no background load. Run a check manually only when you observe a problem, to locate the failing link."}
          </p>
          <button type="button" className="btn-primary" onClick={runCheck} disabled={checking} aria-busy={checking}>
            <Play size={14} /> {checking ? (zh ? "体检中…" : "Checking…") : t("diag_start_check")}
          </button>
        </div>
      )}

      {overview && (
        <>
          <div className={`card border-l-4 ${ov.cls}`}>
            <div className="flex flex-wrap items-center gap-3">
              <span className="text-lg font-bold">{ov.text}</span>
              <span className="text-sm text-muted">
                {zh ? "生成于" : "Generated"}: {overview.generated_at}
              </span>
              <span className="ml-auto flex gap-3 text-xs font-medium">
                <span className="text-ok">OK {overview.counts.ok}</span>
                <span className="text-warn">WARN {overview.counts.warn}</span>
                <span className="text-danger">ERR {overview.counts.error}</span>
                <span className="text-muted">INFO {overview.counts.info}</span>
              </span>
            </div>
            {failing.length === 0 && (
              <p className="text-sm text-ok mt-2">{t("diag_no_issue")}</p>
            )}
          </div>

          {failing.length > 0 && (
            <div className="card mt-3 border-warn/40">
              <div className="text-sm font-medium mb-1">{zh ? "建议优先调查以下环节" : "Investigate these links first"}</div>
              <div className="flex flex-wrap gap-2">
                {failing.map((item) => (
                  <button
                    key={item.id}
                    type="button"
                    className="btn text-xs"
                    onClick={() => runInvestigate(item.id)}
                    disabled={investigating === item.id}
                  >
                    <Search size={13} />
                    {investigating === item.id ? (zh ? "诊断中…" : "Investigating…") : `${t("diag_this")}: ${item.label}`}
                  </button>
                ))}
              </div>
            </div>
          )}

          <div className="mt-4 grid grid-cols-1 lg:grid-cols-2 gap-3">
            {(overview.items || []).map((item) => {
              const meta = STATUS_META[item.status] || STATUS_META.info;
              const Icon = meta.icon;
              const report = reports[item.id];
              return (
                <div key={item.id} className="card flex flex-col gap-2">
                  <div className="flex items-center justify-between gap-2">
                    <div className="flex items-center gap-2 min-w-0">
                      <Icon size={16} className={meta.color} />
                      <span className="font-medium truncate">{item.label}</span>
                    </div>
                    <span className={`text-xs font-semibold uppercase ${meta.color}`}>{item.status}</span>
                  </div>

                  {item.detail && <p className="text-sm text-muted break-words">{item.detail}</p>}
                  {item.remedy && (
                    <p className="text-xs text-warn border border-warn/30 bg-warn/10 rounded px-2 py-1 break-words">
                      {zh ? "建议：" : "Remedy: "}{item.remedy}
                    </p>
                  )}

                  <div className="flex items-center justify-between gap-2">
                    <span className="text-[11px] text-muted/70">{item.category}</span>
                    {item.status !== "ok" && (
                      <button
                        type="button"
                        className="btn text-xs"
                        onClick={() => runInvestigate(item.id)}
                        disabled={investigating === item.id}
                      >
                        <Search size={13} />
                        {investigating === item.id ? (zh ? "诊断中…" : "Investigating…") : t("diag_this")}
                      </button>
                    )}
                  </div>

                  {report && (
                    <div className="mt-2 border-t border-border pt-2">
                      <div className="flex items-center gap-2 flex-wrap mb-1">
                        <span className={`text-xs font-bold uppercase ${INVESTIGATE_STATUS[report.status] || "text-muted"}`}>
                          {report.status}
                        </span>
                        <span className="text-sm font-medium">
                          {t("diag_root_cause")}: {report.root_cause_label || report.root_cause || "-"}
                        </span>
                        {report.confidence && (
                          <span className="text-[11px] text-muted">
                            {t("diag_confidence")}: {report.confidence}
                          </span>
                        )}
                      </div>
                      <p className="text-sm text-muted break-words mb-2">{report.summary}</p>

                      {report.status === "unknown" && (
                        <p className="text-xs text-warn border border-warn/30 bg-warn/10 rounded px-2 py-1 break-words mb-2">
                          {t("diag_unknown_note")}
                        </p>
                      )}
                      {report.investigated_at && (
                        <p className="text-[11px] text-muted/70 mb-2">
                          {t("diag_investigated_at")}: {report.investigated_at}
                        </p>
                      )}

                      {!!report.evidence?.length && (
                        <div className="mb-2">
                          <div className="text-[11px] uppercase tracking-wide text-muted mb-1">{t("diag_evidence")}</div>
                          <div className="space-y-1">
                            {report.evidence.map((ev, idx) => (
                              <div key={idx} className="text-xs flex gap-2">
                                <span className="text-muted shrink-0">{ev.source}:</span>
                                <span className="break-words">{ev.finding}</span>
                              </div>
                            ))}
                          </div>
                        </div>
                      )}

                      {!!report.remedies?.length && (
                        <div className="mb-2">
                          <div className="text-[11px] uppercase tracking-wide text-muted mb-1">{t("diag_remedy")}</div>
                          <ul className="text-xs space-y-1">
                            {report.remedies.map((r, idx) => (
                              <li key={idx} className="break-words">
                                • {r.label}
                                {r.requires_confirmation && (
                                  <span className="text-muted"> （{zh ? "需确认后执行" : "requires confirmation"}）</span>
                                )}
                              </li>
                            ))}
                          </ul>
                        </div>
                      )}

                      {!!report.next_checks?.length && (
                        <div className="mb-2">
                          <div className="text-[11px] uppercase tracking-wide text-muted mb-1">{t("diag_next_checks")}</div>
                          <ul className="text-xs text-muted space-y-1">
                            {report.next_checks.map((c, idx) => (<li key={idx}>• {c}</li>))}
                          </ul>
                        </div>
                      )}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </>
      )}
    </div>
  );
}
