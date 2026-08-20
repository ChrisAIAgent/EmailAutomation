"use client";

import { useEffect, useMemo, useState } from "react";
import { Beaker, FlaskConical, ShieldCheck } from "lucide-react";
import { api } from "@/lib/api";
import { useLang } from "@/lib/i18n";

type LabAgent = {
  agent: string;
  configured?: boolean;
  reachable?: boolean;
  detail?: string;
};

const DISPLAY_NAMES: Record<string, string> = {
  langgraph: "Production Agent",
  openclaw: "Candidate Agent",
};

function displayName(agentId: string) {
  return DISPLAY_NAMES[agentId] || agentId;
}

export default function ComparisonView({
  onChanged,
  agents = [],
}: {
  onChanged: () => void;
  agents?: LabAgent[];
}) {
  const { lang } = useLang();
  const zh = lang === "zh";
  const [list, setList] = useState<any[]>([]);
  const [metrics, setMetrics] = useState<any>(null);
  const [detail, setDetail] = useState<any>(null);
  const [error, setError] = useState("");
  const agentIds = useMemo(
    () => Array.from(new Set(["langgraph", "openclaw", ...agents.map((a) => a.agent).filter(Boolean)])),
    [agents],
  );
  const [primaryAgent, setPrimaryAgent] = useState("langgraph");
  const [candidateAgent, setCandidateAgent] = useState("openclaw");

  const load = async () => {
    try {
      setError("");
      const [comparisons, summary] = await Promise.all([
        api.comparisons(),
        api.comparisonMetrics(),
      ]);
      setList(comparisons);
      setMetrics(summary);
    } catch (e: any) {
      setError(e?.message || "Agent Lab unavailable");
    }
  };

  useEffect(() => {
    load();
  }, []);

  const open = async (key: string) => {
    try {
      setDetail(await api.comparisonDetail(key));
    } catch (e: any) {
      setError(e?.message || "Comparison unavailable");
    }
  };

  const select = async (key: string, selected: string) => {
    await api.selectComparison(key, { selected });
    setDetail(null);
    await load();
    onChanged();
  };

  const primaryDecision = detail?.[primaryAgent] ?? detail?.langgraph;
  const candidateDecision = detail?.[candidateAgent] ?? detail?.openclaw;
  const primaryHealth = agents.find((a) => a.agent === primaryAgent);
  const candidateHealth = agents.find((a) => a.agent === candidateAgent);

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-2 md:flex-row md:items-start md:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <Beaker size={22} className="text-brand" />
            <h1 className="text-xl font-semibold">{zh ? "Agent 实验室" : "Agent Lab"}</h1>
            <span className="badge text-warn border-warn">{zh ? "仅供内部实验" : "Internal only"}</span>
          </div>
          <p className="mt-1 max-w-3xl text-sm text-muted">
            {zh
              ? "对比生产 Agent 与候选 Agent 的判断和邮件建议。候选 Agent 以 Shadow 模式运行，不具备 Gmail 写入或发送权限。"
              : "Compare production and candidate Agent decisions. Candidate Agents run in shadow mode without Gmail write or send permission."}
          </p>
        </div>
      </div>

      <div className="card">
        <div className="flex items-center gap-2 mb-3">
          <FlaskConical size={17} />
          <h2 className="font-medium">{zh ? "实验配置" : "Experiment setup"}</h2>
        </div>
        <div className="grid gap-3 md:grid-cols-2">
          <AgentSelector
            label={zh ? "生产基准 Agent" : "Production baseline"}
            value={primaryAgent}
            options={agentIds}
            health={primaryHealth}
            onChange={(value: string) => {
              setPrimaryAgent(value);
              if (value === candidateAgent) {
                setCandidateAgent(agentIds.find((id) => id !== value) || value);
              }
            }}
          />
          <AgentSelector
            label={zh ? "候选 Shadow Agent" : "Candidate shadow"}
            value={candidateAgent}
            options={agentIds.filter((id) => id !== primaryAgent)}
            health={candidateHealth}
            onChange={setCandidateAgent}
          />
        </div>
        <div className="mt-3 flex items-center gap-2 text-xs text-muted">
          <ShieldCheck size={15} className="text-ok" />
          {zh
            ? "切换仅影响实验结果展示与采用标记，不会改变 Global、Campaign 或当前生产 Agent。"
            : "Lab selection only changes comparison display and adoption labels; it never switches Global, Campaign, or the production Agent."}
        </div>
      </div>

      {metrics && (
        <div className="grid grid-cols-2 gap-3 md:grid-cols-4">
          <Metric label={zh ? "实验总数" : "Experiments"} value={metrics.total} />
          <Metric label={zh ? "意图分歧率" : "Intent disagreement"} value={metrics.intent_disagreement_rate} />
          <Metric label={zh ? "候选失败率" : "Candidate failure"} value={metrics.openclaw_failure_rate} />
          <Metric label={zh ? "已采用结果" : "Adopted results"} value={sumAdoption(metrics.adoption)} />
        </div>
      )}

      {error && <div className="card border-danger text-danger text-sm">{error}</div>}

      <div>
        <h2 className="font-medium mb-2">{zh ? "Compare 记录" : "Compare history"}</h2>
        <div className="space-y-2">
          {list.length === 0 && (
            <div className="card text-muted text-sm">
              {zh
                ? "暂无实验记录。使用内部 Compare 模式分析邮件后，结果会显示在这里。"
                : "No experiments yet. Results appear here after an internal Compare run."}
            </div>
          )}
          {list.map((comparison) => (
            <div key={comparison.comparison_key} className="card flex justify-between items-center gap-3">
              <div>
                <div className="font-medium">{taskLabel(comparison.task_type)}</div>
                <div className="text-xs text-muted mt-1">
                  {zh ? "意图一致" : "Intent match"}: {yesNo(comparison.intent_agree, zh)}
                  {" · "}
                  {zh ? "动作一致" : "Action match"}: {yesNo(comparison.action_agree, zh)}
                  {" · "}
                  {zh ? "采用" : "Selected"}: {selectionLabel(comparison.selected, zh)}
                </div>
              </div>
              <button className="btn shrink-0" onClick={() => open(comparison.comparison_key)}>
                {zh ? "查看对比" : "Compare"}
              </button>
            </div>
          ))}
        </div>
      </div>

      {detail && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center p-4 z-50" onClick={() => setDetail(null)}>
          <div className="card max-w-4xl w-full max-h-[90vh] overflow-auto" onClick={(event) => event.stopPropagation()}>
            <div className="flex justify-between items-start gap-3 mb-3">
              <div>
                <div className="text-xs uppercase tracking-wide text-muted">Agent Lab · Compare</div>
                <h2 className="font-semibold">{taskLabel(detail.task_type)}</h2>
              </div>
              <button className="btn" onClick={() => setDetail(null)}>{zh ? "关闭" : "Close"}</button>
            </div>
            <div className="grid md:grid-cols-2 gap-3">
              <AgentCard title={`${displayName(primaryAgent)} · Primary`} decision={primaryDecision} />
              <AgentCard title={`${displayName(candidateAgent)} · Shadow`} decision={candidateDecision} shadow />
            </div>
            <div className="flex flex-wrap gap-2 mt-3">
              <button className="btn-primary" onClick={() => select(detail.comparison_key, primaryAgent)}>
                {zh ? "采用生产 Agent" : "Adopt production result"}
              </button>
              <button className="btn-primary" disabled={!candidateDecision} onClick={() => select(detail.comparison_key, candidateAgent)}>
                {zh ? "采用候选 Agent" : "Adopt candidate result"}
              </button>
              <button className="btn" onClick={() => select(detail.comparison_key, "edited")}>
                {zh ? "采用编辑版本" : "Adopt edited"}
              </button>
              <button className="btn-danger" onClick={() => select(detail.comparison_key, "none")}>
                {zh ? "全部拒绝" : "Reject all"}
              </button>
            </div>
            <p className="text-xs text-muted mt-3">
              {zh
                ? "“采用”只记录实验结论，不会自动切换生产 Agent 或触发邮件发送。"
                : "Adoption records an experiment outcome only. It does not switch the production Agent or send email."}
            </p>
          </div>
        </div>
      )}
    </div>
  );
}

function AgentSelector({ label, value, options, health, onChange }: any) {
  return (
    <label className="text-sm">
      <span className="block text-muted mb-1">{label}</span>
      <select className="input w-full" value={value} onChange={(event) => onChange(event.target.value)}>
        {options.map((agentId: string) => (
          <option key={agentId} value={agentId}>{displayName(agentId)} ({agentId})</option>
        ))}
      </select>
      <span className={`mt-1 block text-xs ${health?.configured ? "text-ok" : "text-muted"}`}>
        {health
          ? `${health.configured ? "Configured" : "Not configured"}${health.reachable === false ? " · Unreachable" : ""}`
          : "Adapter registered"}
      </span>
    </label>
  );
}

function Metric({ label, value }: { label: string; value: any }) {
  return (
    <div className="card">
      <div className="text-xs text-muted">{label}</div>
      <div className="text-xl font-semibold mt-1">{value ?? "—"}</div>
    </div>
  );
}

function AgentCard({ title, decision, shadow = false }: any) {
  if (!decision) {
    return (
      <div className="p-3 rounded-lg bg-panel2 text-sm">
        <div className="font-medium mb-1">{title}</div>
        <div className="text-muted">No result. The adapter may be unconfigured or unreachable.</div>
      </div>
    );
  }
  return (
    <div className="p-3 rounded-lg bg-panel2 text-sm">
      <div className="flex justify-between gap-2">
        <span className="font-medium">{title}</span>
        {shadow && <span className="badge text-muted">read only</span>}
      </div>
      <div className="mt-2">Intent: <b>{decision.intent}</b></div>
      <div>Confidence: {Math.round((decision.confidence || 0) * 100)}%</div>
      <div>Action: {decision.recommended_action}</div>
      <div>Risk: {decision.risk_level} · {decision.model || "unknown"} · {decision.latency_ms ?? "—"}ms</div>
      {decision.draft && (
        <div className="mt-2 border-t border-border pt-2">
          <div className="font-medium">{decision.draft.subject}</div>
          <div className="whitespace-pre-wrap text-xs mt-1">{decision.draft.body_text?.slice(0, 500)}</div>
        </div>
      )}
      <div className="text-xs text-muted mt-2">{decision.reasoning_summary}</div>
    </div>
  );
}

function sumAdoption(adoption: Record<string, number> | undefined) {
  if (!adoption) return 0;
  return Object.entries(adoption)
    .filter(([key]) => key !== "none")
    .reduce((sum, [, value]) => sum + Number(value || 0), 0);
}

function yesNo(value: boolean, zh: boolean) {
  return value ? (zh ? "是" : "Yes") : (zh ? "否" : "No");
}

function selectionLabel(selected: string | null, zh: boolean) {
  if (!selected) return zh ? "未选择" : "None";
  if (selected === "langgraph") return zh ? "生产 Agent" : "Production Agent";
  if (selected === "openclaw") return zh ? "候选 Agent" : "Candidate Agent";
  if (selected === "edited") return zh ? "编辑版本" : "Edited";
  return zh ? "未采用" : "Rejected";
}

function taskLabel(task: string) {
  return String(task || "experiment").replaceAll("_", " ");
}
