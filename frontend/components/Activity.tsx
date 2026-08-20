"use client";

import { useEffect, useState } from "react";
import { api } from "@/lib/api";
import { useLang } from "@/lib/i18n";

const COLORS: any = {
  agent_run: "text-accent",
  tool_exec: "text-warn",
  approval: "text-ok",
  audit: "text-muted",
};

export default function ActivityView() {
  const { t, formatDate } = useLang();
  const [items, setItems] = useState<any[]>([]);
  useEffect(() => { api.activity().then(setItems); }, []);

  return (
    <div>
      <h1 className="text-xl font-semibold mb-3">{t('act_title')}</h1>
      <div className="space-y-1">
        {items.length === 0 && <div className="card text-muted">{t('act_no_activities')}</div>}
        {items.map((it, i) => (
          <div key={i} className="card flex items-center gap-3 py-2">
            <span className={`badge ${COLORS[it.category] || "text-muted"}`}>{it.category}</span>
            <span className="text-sm flex-1">{it.summary}</span>
            {it.status && <span className="text-xs text-muted">{it.status}</span>}
            <span className="text-xs text-muted">{formatDate(it.created_at)}</span>
          </div>
        ))}
      </div>
    </div>
  );
}
