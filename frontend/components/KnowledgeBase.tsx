"use client";

import { DragEvent, useEffect, useMemo, useRef, useState } from "react";
import { BookOpen, FileText, FlaskConical, Plus, Search, Upload, X } from "lucide-react";
import { api } from "@/lib/api";
import { useLang } from "@/lib/i18n";

type KnowledgeDocument = {
  id: number;
  title: string;
  category: string;
  content: string;
  source_type: string;
  source_name?: string;
  status: "draft" | "published" | "disabled";
  version: number;
  characters: number;
  updated_at: string;
};

const EMPTY_FORM = { title: "", category: "general", content: "", publish: true };
const RECOMMENDED_CATEGORIES = [
  { value: "company_service_overview", label: "Company / Service Overview" },
  { value: "pricing", label: "Pricing" },
  { value: "onboarding", label: "Onboarding" },
  { value: "faq", label: "FAQ" },
  { value: "contact", label: "Contact" },
  { value: "team", label: "Team" },
  { value: "segments_served", label: "Segments Served" },
  { value: "case_studies", label: "Case Studies" },
];
const REPLY_STRATEGY_TEMPLATE = {
  zh: `# 全局回复策略

- 回复最新一封客户来信，并结合完整会话避免重复已经解释过的内容。
- 先回答已经确认的事实；未知信息明确说明需要确认，不得编造。
- 价格咨询不直接承诺数字，先确认范围并给出一个明确下一步。
- Demo 请求提供简洁的预约建议；CRM 或集成能力只引用已发布知识。
- 使用客户的语言，保持简洁自然，每封邮件只给出一个主要下一步。`,
  en: `# Global reply strategy

- Reply to the latest customer message and use the full conversation to avoid repetition.
- Answer confirmed facts first. State clearly when a detail needs confirmation; never invent it.
- For pricing questions, confirm scope and provide one clear next step instead of inventing a figure.
- For demo requests, propose a concise scheduling step. Mention CRM or integrations only from published facts.
- Match the customer's language, stay concise and natural, and give one primary next step.`,
};

export default function KnowledgeBaseView() {
  const { lang } = useLang();
  const zh = lang === "zh";
  const [documents, setDocuments] = useState<KnowledgeDocument[]>([]);
  const [form, setForm] = useState(EMPTY_FORM);
  const [editing, setEditing] = useState<KnowledgeDocument | null>(null);
  const [showEditor, setShowEditor] = useState(false);
  const [saving, setSaving] = useState(false);
  const [dragging, setDragging] = useState(false);
  const [error, setError] = useState("");
  const [query, setQuery] = useState("");
  const [results, setResults] = useState<any[]>([]);
  const [testing, setTesting] = useState(false);
  const [uploadCategory, setUploadCategory] = useState("general");
  const fileInput = useRef<HTMLInputElement>(null);

  const load = async () => {
    try {
      setError("");
      setDocuments(await api.knowledgeDocuments());
    } catch (e: any) {
      setError(e?.message || (zh ? "知识库加载失败" : "Failed to load knowledge"));
    }
  };

  useEffect(() => { load(); }, []);

  const stats = useMemo(() => ({
    published: documents.filter((d) => d.status === "published").length,
    draft: documents.filter((d) => d.status === "draft").length,
    disabled: documents.filter((d) => d.status === "disabled").length,
  }), [documents]);

  const openCreate = () => {
    setEditing(null);
    setForm(EMPTY_FORM);
    setShowEditor(true);
  };

  const openReplyStrategy = () => {
    setEditing(null);
    setForm({
      title: zh ? "全局回复策略" : "Global reply strategy",
      category: "reply_strategy",
      content: zh ? REPLY_STRATEGY_TEMPLATE.zh : REPLY_STRATEGY_TEMPLATE.en,
      publish: false,
    });
    setShowEditor(true);
  };

  const openEdit = (document: KnowledgeDocument) => {
    setEditing(document);
    setForm({
      title: document.title,
      category: document.category,
      content: document.content,
      publish: document.status === "published",
    });
    setShowEditor(true);
  };

  const save = async () => {
    if (!form.title.trim() || !form.content.trim()) {
      setError(zh ? "请填写标题和知识内容" : "Title and content are required");
      return;
    }
    setSaving(true);
    setError("");
    try {
      if (editing) {
        await api.updateKnowledge(editing.id, {
          title: form.title,
          category: form.category,
          content: form.content,
        });
        if (form.publish && editing.status !== "published") await api.publishKnowledge(editing.id);
        if (!form.publish && editing.status === "published") await api.disableKnowledge(editing.id);
      } else {
        await api.createKnowledge(form);
      }
      setShowEditor(false);
      await load();
    } catch (e: any) {
      setError(e?.message || (zh ? "保存失败" : "Save failed"));
    } finally {
      setSaving(false);
    }
  };

  const uploadFile = async (file: File) => {
    setSaving(true);
    setError("");
    try {
      await api.uploadKnowledge(file, "", uploadCategory.trim() || "general", true);
      await load();
    } catch (e: any) {
      setError(e?.message || (zh ? "文件上传失败" : "Upload failed"));
    } finally {
      setSaving(false);
      setDragging(false);
      if (fileInput.current) fileInput.current.value = "";
    }
  };

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    const file = event.dataTransfer.files?.[0];
    if (file) uploadFile(file);
  };

  const setStatus = async (document: KnowledgeDocument) => {
    if (document.status === "published") await api.disableKnowledge(document.id);
    else await api.publishKnowledge(document.id);
    await load();
  };

  const remove = async (document: KnowledgeDocument) => {
    if (!window.confirm(zh
      ? `永久删除“${document.title}”？此操作不可恢复。`
      : `Permanently delete "${document.title}"? This cannot be undone.`)) return;
    await api.deleteKnowledge(document.id);
    await load();
  };

  const testRetrieval = async () => {
    if (!query.trim()) return;
    setTesting(true);
    setError("");
    try {
      const response = await api.searchKnowledge(query);
      setResults(response.results || []);
    } catch (e: any) {
      setError(e?.message || (zh ? "检索检查失败" : "Retrieval check failed"));
    } finally {
      setTesting(false);
    }
  };

  return (
    <div className="space-y-4">
      <div className="flex flex-col gap-3 md:flex-row md:items-start md:justify-between">
        <div>
          <div className="flex items-center gap-2">
            <BookOpen size={22} className="text-brand" />
            <h1 className="text-xl font-semibold">{zh ? "知识库" : "Knowledge Base"}</h1>
          </div>
          <p className="mt-1 text-sm text-muted max-w-3xl">
            {zh
              ? "上传或粘贴业务资料。只有已发布内容会被 LangGraph 检索并用于邮件回复。"
              : "Upload or paste business material. LangGraph uses published content only when preparing email replies."}
          </p>
        </div>
        <div className="flex flex-wrap gap-2">
          <button className="btn flex items-center gap-2" onClick={openReplyStrategy}>
            <FileText size={15} /> {zh ? "添加回复策略" : "Add reply strategy"}
          </button>
          <button className="btn-primary flex items-center gap-2" onClick={openCreate}>
            <Plus size={15} /> {zh ? "添加知识" : "Add knowledge"}
          </button>
        </div>
      </div>

      <div className="grid grid-cols-3 gap-3">
        <Stat label={zh ? "已发布" : "Published"} value={stats.published} tone="text-ok" />
        <Stat label={zh ? "草稿" : "Draft"} value={stats.draft} tone="text-warn" />
        <Stat label={zh ? "已停用" : "Disabled"} value={stats.disabled} tone="text-muted" />
      </div>

      <div
        className={`card border-2 border-dashed text-center py-7 transition-colors ${dragging ? "border-brand bg-brand/5" : "border-border"}`}
        onDragOver={(event) => { event.preventDefault(); setDragging(true); }}
        onDragLeave={() => setDragging(false)}
        onDrop={onDrop}
      >
        <Upload className="mx-auto text-muted mb-2" size={24} />
        <div className="font-medium">{zh ? "拖拽知识文件到这里" : "Drop a knowledge file here"}</div>
        <div className="text-xs text-muted mt-1">
          {zh ? "支持 UTF-8 Markdown、TXT、CSV，单文件不超过 2MB" : "UTF-8 Markdown, TXT, or CSV up to 2MB"}
        </div>
        <label className="block max-w-md mx-auto mt-3 text-left text-xs text-muted">
          {zh ? "推荐类别（可直接改成您的行业类别）" : "Suggested category (you may enter any industry-specific category)"}
          <input
            className="input w-full mt-1 text-sm"
            list="knowledge-category-suggestions"
            value={uploadCategory}
            onChange={(event) => setUploadCategory(event.target.value)}
            placeholder="general"
          />
        </label>
        <button className="btn mt-3" disabled={saving} onClick={() => fileInput.current?.click()}>
          {saving ? (zh ? "处理中…" : "Processing…") : (zh ? "选择文件" : "Choose file")}
        </button>
        <input
          ref={fileInput}
          type="file"
          className="hidden"
          accept=".md,.markdown,.txt,.csv,text/markdown,text/plain,text/csv"
          onChange={(event) => event.target.files?.[0] && uploadFile(event.target.files[0])}
        />
      </div>

      {error && <div className="card border-danger text-danger text-sm">{error}</div>}

      <div className="card">
        <div className="flex items-center gap-2 mb-3">
          <FlaskConical size={16} />
          <h2 className="font-medium">{zh ? "检查 LangGraph 检索" : "Check LangGraph retrieval"}</h2>
        </div>
        <div className="flex gap-2">
          <div className="input flex-1 flex items-center gap-2">
            <Search size={14} className="text-muted" />
            <input
              className="w-full bg-transparent outline-none"
              value={query}
              onChange={(event) => setQuery(event.target.value)}
              onKeyDown={(event) => event.key === "Enter" && testRetrieval()}
              placeholder={zh ? "例如：你们如何处理 Gmail 自动回复？" : "Example: How do you handle Gmail auto replies?"}
            />
          </div>
          <button className="btn-primary" disabled={testing || !query.trim()} onClick={testRetrieval}>
            {testing ? (zh ? "检索中…" : "Checking…") : (zh ? "检查检索" : "Check")}
          </button>
        </div>
        {results.length > 0 && (
          <div className="mt-3 space-y-2">
            {results.map((result) => (
              <div key={result.id} className="rounded-lg bg-panel2 p-3 text-sm">
                <div className="flex justify-between gap-2">
                  <span className="font-medium">{result.title}</span>
                  <span className="text-xs text-muted">score {result.score ?? "—"}</span>
                </div>
                <p className="text-xs text-muted mt-1 whitespace-pre-wrap">{String(result.content).slice(0, 600)}</p>
                <div className="text-[11px] text-muted mt-2">{result.source} · v{result.version || 1}</div>
              </div>
            ))}
          </div>
        )}
      </div>

      <div>
        <h2 className="font-medium mb-2">{zh ? "知识文档" : "Knowledge documents"}</h2>
        <div className="space-y-2">
          {documents.length === 0 && (
            <div className="card text-center text-sm text-muted py-8">
              {zh ? "还没有用户知识。上传文件或粘贴 Markdown 即可开始。" : "No user knowledge yet. Upload a file or paste Markdown to begin."}
            </div>
          )}
          {documents.map((document) => (
            <div key={document.id} className="card flex items-start gap-3">
              <div className="h-9 w-9 rounded-lg bg-panel2 flex items-center justify-center shrink-0">
                <FileText size={17} />
              </div>
              <button className="flex-1 min-w-0 text-left" onClick={() => openEdit(document)}>
                <div className="flex items-center gap-2">
                  <span className="font-medium truncate">{document.title}</span>
                  <Status status={document.status} zh={zh} />
                </div>
                <div className="text-xs text-muted mt-1">
                  {document.category} · {document.characters.toLocaleString()} {zh ? "字符" : "chars"} · v{document.version}
                  {document.source_name ? ` · ${document.source_name}` : ""}
                </div>
                <p className="text-xs text-muted mt-2 line-clamp-2">{document.content.slice(0, 240)}</p>
              </button>
              <div className="flex gap-2 shrink-0">
                <button className="btn text-xs" onClick={() => setStatus(document)}>
                  {document.status === "published" ? (zh ? "停用" : "Disable") : (zh ? "发布" : "Publish")}
                </button>
                <button className="btn-danger text-xs" onClick={() => remove(document)}>
                  {zh ? "删除" : "Delete"}
                </button>
              </div>
            </div>
          ))}
        </div>
      </div>

      {showEditor && (
        <div className="fixed inset-0 bg-black/60 flex items-center justify-center p-4 z-50" onClick={() => setShowEditor(false)}>
          <div className="card max-w-3xl w-full max-h-[92vh] overflow-auto" onClick={(event) => event.stopPropagation()}>
            <div className="flex justify-between items-center mb-3">
              <h2 className="font-semibold">{editing ? (zh ? "编辑知识" : "Edit knowledge") : (zh ? "添加知识" : "Add knowledge")}</h2>
              <button className="btn p-2" onClick={() => setShowEditor(false)}><X size={15} /></button>
            </div>
            <div className="grid md:grid-cols-2 gap-3">
              <label className="text-sm">
                <span className="text-muted">{zh ? "标题" : "Title"}</span>
                <input className="input w-full mt-1" value={form.title} onChange={(event) => setForm({ ...form, title: event.target.value })} />
              </label>
              <label className="text-sm">
                <span className="text-muted">{zh ? "分类" : "Category"}</span>
                <input
                  className="input w-full mt-1"
                  list="knowledge-category-suggestions"
                  value={form.category}
                  onChange={(event) => setForm({ ...form, category: event.target.value })}
                  placeholder="general"
                />
                <span className="block text-xs text-muted mt-1">
                  {zh ? "可选择建议类别，也可输入您的行业专属类别。" : "Choose a suggested category or enter your own industry-specific category."}
                </span>
              </label>
            </div>
            <label className="text-sm block mt-3">
              <span className="text-muted">{zh ? "知识内容（支持 Markdown）" : "Knowledge content (Markdown supported)"}</span>
              <textarea
                className="input w-full mt-1 min-h-[320px] font-mono text-sm"
                value={form.content}
                onChange={(event) => setForm({ ...form, content: event.target.value })}
                placeholder={zh ? "# 产品介绍\n\n粘贴产品、FAQ、服务流程或限制条件…" : "# Product overview\n\nPaste product, FAQ, process, or policy content…"}
              />
            </label>
            <label className="mt-3 flex items-center gap-2 text-sm">
              <input type="checkbox" checked={form.publish} onChange={(event) => setForm({ ...form, publish: event.target.checked })} />
              {zh ? "保存后立即发布给 LangGraph 使用" : "Publish for LangGraph immediately"}
            </label>
            <div className="flex justify-end gap-2 mt-4">
              <button className="btn" onClick={() => setShowEditor(false)}>{zh ? "取消" : "Cancel"}</button>
              <button className="btn-primary" disabled={saving} onClick={save}>
                {saving ? (zh ? "保存中…" : "Saving…") : (zh ? "保存知识" : "Save knowledge")}
              </button>
            </div>
          </div>
        </div>
      )}
      <datalist id="knowledge-category-suggestions">
        <option value="general">General</option>
        {RECOMMENDED_CATEGORIES.map((category) => (
          <option key={category.value} value={category.value}>{category.label}</option>
        ))}
      </datalist>
    </div>
  );
}

function Stat({ label, value, tone }: { label: string; value: number; tone: string }) {
  return <div className="card"><div className="text-xs text-muted">{label}</div><div className={`text-xl font-semibold mt-1 ${tone}`}>{value}</div></div>;
}

function Status({ status, zh }: { status: string; zh: boolean }) {
  const label = status === "published" ? (zh ? "已发布" : "Published") : status === "draft" ? (zh ? "草稿" : "Draft") : (zh ? "已停用" : "Disabled");
  const tone = status === "published" ? "text-ok border-ok" : status === "draft" ? "text-warn border-warn" : "text-muted border-border";
  return <span className={`badge ${tone}`}>{label}</span>;
}
