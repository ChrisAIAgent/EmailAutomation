"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api";
import { useLang } from "@/lib/i18n";

const RUNNING = new Set(["queued", "running", "confirmed"]);

/** One Run owns its polling and confirmation state; unmount cancels polling. */
export default function CampaignRun({ runId, onChanged }: { runId: number; onChanged: () => void }) {
  const { lang } = useLang();
  const zh = lang === "zh";
  const [run, setRun] = useState<any>(null);
  const [error, setError] = useState("");
  const [checking, setChecking] = useState(true);
  const [busy, setBusy] = useState(false);
  const [revision, setRevision] = useState(0);
  const actionLock = useRef(false);
  const mounted = useRef(false);
  const generation = useRef(0);

  useEffect(() => {
    mounted.current = true;
    return () => { mounted.current = false; };
  }, []);

  useEffect(() => {
    let disposed = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const requestGeneration = ++generation.current;
    setChecking(true);
    const poll = async () => {
      try {
        const next = await api.agentRunDetail(runId);
        if (disposed || requestGeneration !== generation.current) return;
        setRun(next);
        setError("");
        setChecking(false);
        if (RUNNING.has(next.status)) timer = setTimeout(poll, 2000);
      } catch (e: any) {
        if (!disposed && requestGeneration === generation.current) {
          setError(e?.message || "Run status unavailable");
          setChecking(false);
        }
      }
    };
    void poll();
    return () => { disposed = true; if (timer) clearTimeout(timer); };
  }, [runId, revision]);

  const decide = async (action: "confirm" | "cancel") => {
    if (actionLock.current || !run || run.status !== "awaiting_confirmation") return;
    actionLock.current = true;
    ++generation.current;
    setBusy(true);
    setError("");
    try {
      const result = action === "confirm" ? await api.confirmAgentRun(runId) : await api.cancelAgentRun(runId);
      if (!mounted.current) return;
      setRun((previous: any) => ({ ...previous, status: result.status }));
      setRevision(value => value + 1);
      onChanged();
    } catch (e: any) {
      // A transport failure is not permission to submit the same send again.
      // Disable decisions until the operator re-reads the current server state.
      if (mounted.current) setError(e?.message || "Run decision failed; refresh status");
    } finally {
      actionLock.current = false;
      if (mounted.current) setBusy(false);
    }
  };

  return <div className="border-t border-border pt-3 space-y-3" data-run-id={runId}>
    <h4 className="font-semibold">Run #{runId} · {run?.mode || "…"} · {run?.status || (zh ? "读取中" : "Loading")}</h4>
    {error && <div role="alert" className="text-danger text-sm">{error}
      <button className="btn ml-2" disabled={busy || checking} onClick={() => { setChecking(true); setRevision(value => value + 1); }}>{zh ? "重新读取状态" : "Retry status"}</button>
    </div>}
    {run?.error && <p className="text-sm text-danger">{run.error}</p>}
    {run?.summary && <p className="text-sm">{run.summary}</p>}
    {run?.status === "recovery_pending" && <button className="btn" disabled={checking} onClick={() => { setChecking(true); setRevision(value => value + 1); }}>{zh ? "重新读取恢复状态" : "Refresh recovery status"}</button>}
    {run?.status === "awaiting_confirmation" && <>
      <h5>{zh ? "发送计划待确认" : "Send plan awaiting confirmation"}</h5>
      <p className="text-sm text-muted">{zh ? "请核对以下冻结内容；确认后不会重新生成。" : "Review the frozen content below; confirmation will not regenerate it."}</p>
      {(run.send_plan || []).map((item: any) => <article key={item.approval_id} className="border border-border rounded p-3 space-y-2">
        <div>{item.to_email}</div><div className="font-medium">{item.subject}</div>
        <pre className="whitespace-pre-wrap break-words font-sans text-sm">{item.body_text}</pre>
      </article>)}
      <div className="flex gap-2">
        <button className="btn-primary" disabled={busy || checking || !!error || !run.send_plan?.length} onClick={() => decide("confirm")}>{zh ? "确认并执行" : "Confirm & execute"}</button>
        <button className="btn" disabled={busy || checking || !!error} onClick={() => decide("cancel")}>{zh ? "取消本轮" : "Cancel run"}</button>
      </div>
    </>}
  </div>;
}
