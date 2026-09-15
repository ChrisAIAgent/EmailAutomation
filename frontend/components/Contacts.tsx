"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { Download, Edit, FileSpreadsheet, Lock, Plus, Search, Trash2, Users } from "lucide-react";
import { api } from "@/lib/api";
import { useLang } from "@/lib/i18n";

const EMPTY = {
  email:"", first_name:"", last_name:"", company:"", title:"", phone:"",
  website:"", timezone:"", custom_fields:null as Record<string, unknown>|null,
  category:"prospect", intent_level:"unknown", tags:[] as string[], segments:[] as string[], notes:"",
  source:"manual", lifecycle_stage:"new_customer", next_action:"review", manual_lock:false,
};
const CATEGORIES = ["prospect","qualified","customer","partner","won","invalid"];
const INTENTS = ["unknown","high","medium","low"];
const STAGES = ["new_customer","contacted","awaiting_reply","needs_reply","following_up","won","stopped"];
const ACTIONS = ["review","send_intro","reply","follow_up","human_review","none"];

export default function ContactsView({ onChanged }: { onChanged:()=>void }) {
  const { lang } = useLang(); const zh=lang==="zh";
  const [contacts,setContacts]=useState<any[]>([]);
  const [search,setSearch]=useState(""); const [category,setCategory]=useState(""); const [segment,setSegment]=useState("");
  const [editing,setEditing]=useState<any|null>(null); const [form,setForm]=useState<any>(EMPTY);
  const [tagText,setTagText]=useState(""); const [segmentText,setSegmentText]=useState(""); const [customFieldsText,setCustomFieldsText]=useState(""); const [error,setError]=useState("");
  const [preview,setPreview]=useState<any>(null); const fileRef=useRef<File|null>(null);
  const load=useCallback(async()=>{try{setContacts(await api.contacts());}catch(e:any){setError(e.message);}},[]);
  useEffect(()=>{load();},[load]);
  const open=(item?:any)=>{const value=item?{...EMPTY,...item}:{...EMPTY,tags:[],segments:[]};setEditing(item||{});setForm(value);setTagText((value.tags||[]).join(", "));setSegmentText((value.segments||[]).join(", "));setCustomFieldsText(value.custom_fields?JSON.stringify(value.custom_fields):"");setError("");};
  const save=async()=>{
    if(!form.email.trim()){setError(zh?"邮箱为必填项":"Email is required");return;}
    if(!editing?.id&&!form.first_name.trim()&&!form.last_name.trim()){setError(zh?"手动创建联系人必须填写姓名":"A name is required for manual contacts");return;}
    let custom_fields: Record<string, unknown>|null=null;
    try { custom_fields=customFieldsText.trim()?JSON.parse(customFieldsText):null; if(custom_fields!==null&&(Array.isArray(custom_fields)||typeof custom_fields!=="object")){throw new Error("invalid");} } catch { setError(zh?"自定义字段必须是 JSON 对象":"Custom fields must be a JSON object"); return; }
    const parseList=(value:string)=>value.split(/[,，;；]/).map(v=>v.trim()).filter(Boolean);
    const body={...form,tags:parseList(tagText),segments:parseList(segmentText),custom_fields};
    try{editing?.id?await api.updateContact(editing.id,body):await api.createContact(body);setEditing(null);await load();onChanged();}catch(e:any){setError(e.message);}
  };
  const segmentOptions=Array.from(new Set(contacts.flatMap(c=>c.segments||[]))).sort((a:any,b:any)=>String(a).localeCompare(String(b)));
  const filtered=contacts.filter(c=>{const text=`${c.email} ${c.first_name||""} ${c.last_name||""} ${c.company||""} ${(c.tags||[]).join(" ")} ${(c.segments||[]).join(" ")}`.toLowerCase();return(!search||text.includes(search.toLowerCase()))&&(!category||c.category===category)&&(!segment||(c.segments||[]).includes(segment));});
  const identity=(c:any)=>{const name=[c.first_name,c.last_name].filter(Boolean).join(" ");return{headline:[name,c.company].filter(Boolean).join(" · ")||c.email,showEmail:Boolean(name||c.company)};};
  const chooseFile=()=>document.getElementById("contacts-file")?.click();
  const downloadCsvTemplate=()=>{
    const csv=[
      ["email","first_name","last_name","company","title","phone","website","segments","tags","timezone","notes","custom_fields","source"],
      ["alex@example.com","Alex","Chen","Example Company","Founder","+1 555 0100","https://example.com","Finance; IT","priority; conference","Asia/Shanghai","Met at the 2026 summit","{\"account_tier\":\"enterprise\"}","csv_import"],
    ].map(row=>row.map(value=>`"${value.replace(/"/g,'""')}"`).join(",")).join("\r\n");
    // Emit a real UTF-8 BOM; older versions wrote the literal characters \\uFEFF.
    const blob=new Blob(["\uFEFF"+csv],{type:"text/csv;charset=utf-8;"});
    const url=URL.createObjectURL(blob); const anchor=document.createElement("a");
    anchor.href=url; anchor.download="contacts-import-template.csv"; anchor.click(); URL.revokeObjectURL(url);
  };
  const downloadExcelTemplate=async()=>{try{const blob=await api.downloadContactsTemplate();const url=URL.createObjectURL(blob);const anchor=document.createElement("a");anchor.href=url;anchor.download="contacts-import-template.xlsx";anchor.click();URL.revokeObjectURL(url);}catch(e:any){setError(e.message);}};
  const readFile=async(file:File)=>{fileRef.current=file;setPreview((await api.importContacts(file,false)).preview);};
  const confirmImport=async()=>{if(!fileRef.current)return;try{await api.importContacts(fileRef.current,true);setPreview(null);fileRef.current=null;await load();onChanged();}catch(e:any){setError(e.message);}};

  return <div className="space-y-4">
    <p className="text-xs text-muted">{zh?"Excel/CSV 必须填写 Email，以及名字或姓氏中的至少一项；其他字段均为可选。":"Excel/CSV required: Email and at least one of First name or Last name. All other fields are optional."}</p>
    <div className="text-sm text-muted">{zh?`共 ${contacts.length} 位联系人`:`${contacts.length} contacts`}</div>
    <div className="flex items-center gap-2 flex-wrap"><h2 className="text-lg font-semibold">{zh?"联系人":"Contacts"}</h2><button className="btn-primary flex items-center gap-1" onClick={()=>open()}><Plus size={14}/>{zh?"新建联系人":"New Contact"}</button><button className="btn flex items-center gap-1" onClick={downloadExcelTemplate}><Download size={14}/>{zh?"下载 Excel 模板":"Excel Template"}</button><button className="btn flex items-center gap-1" onClick={downloadCsvTemplate}><Download size={14}/>{zh?"下载 CSV 模板":"CSV Template"}</button><button className="btn flex items-center gap-1" onClick={chooseFile}><FileSpreadsheet size={14}/>{zh?"导入 Excel/CSV":"Import Excel/CSV"}</button><input id="contacts-file" className="hidden" type="file" accept=".xlsx,.csv" onChange={e=>e.target.files?.[0]&&readFile(e.target.files[0])}/></div>
    {error&&<div className="bg-danger/10 text-danger rounded p-2 text-sm">{error}</div>}
    <div className="flex gap-2"><div className="input flex-1 flex items-center gap-2"><Search size={14}/><input className="bg-transparent outline-none w-full" value={search} onChange={e=>setSearch(e.target.value)} placeholder={zh?"搜索姓名、公司、邮箱、标签或客户分群":"Search name, company, email, tag or segment"}/></div><select className="input" value={segment} onChange={e=>setSegment(e.target.value)}><option value="">{zh?"全部客户分群":"All segments"}</option>{segmentOptions.map(v=><option key={v}>{v}</option>)}</select><select className="input" value={category} onChange={e=>setCategory(e.target.value)}><option value="">{zh?"全部系统分类":"All system categories"}</option>{CATEGORIES.map(v=><option key={v}>{v}</option>)}</select></div>
    {filtered.length===0?<div className="py-16 text-center text-muted"><Users className="mx-auto mb-2"/>{zh?"还没有联系人":"No contacts yet"}</div>:
    <div className="border border-border rounded overflow-hidden">
      <div className="grid grid-cols-[1.4fr_1fr_1fr_1fr_1fr_90px] gap-3 px-3 py-2 bg-panel2 text-xs text-muted"><span>{zh?"联系人":"Contact"}</span><span>{zh?"阶段 / 下一步":"Stage / Next"}</span><span>{zh?"系统分类 / 意向":"System category / Intent"}</span><span>{zh?"客户分群":"Segments"}</span><span>{zh?"标签":"Tags"}</span><span>{zh?"操作":"Actions"}</span></div>
      {filtered.map(c=>{const id=identity(c);return <div key={c.id} className="grid grid-cols-[1.4fr_1fr_1fr_1fr_1fr_90px] gap-3 px-3 py-3 border-t border-border items-center text-sm">
        <div className="min-w-0"><div className="font-medium truncate">{id.headline}</div>{id.showEmail&&<div className="text-xs text-muted truncate">{c.email}</div>}</div>
        <div><div>{c.lifecycle_stage||"new_customer"}{c.manual_lock&&<Lock size={11} className="inline ml-1 text-warn"/>}</div><div className="text-xs text-accent">{c.next_action||"-"}</div></div>
        <div><span className="text-xs bg-panel2 px-1.5 py-0.5 rounded">{c.category}</span><span className="text-xs text-muted ml-1">{c.intent_level}</span></div>
        <div className="flex gap-1 flex-wrap">{(c.segments||[]).slice(0,3).map((v:string)=><span key={v} className="text-xs border border-accent/50 rounded px-1.5 text-accent">{v}</span>)}</div>
        <div className="flex gap-1 flex-wrap">{(c.tags||[]).slice(0,3).map((v:string)=><span key={v} className="text-xs border border-border rounded px-1.5">{v}</span>)}</div>
        <div className="flex gap-1"><button className="btn p-1.5" title={zh?"编辑":"Edit"} onClick={()=>open(c)}><Edit size={13}/></button><button className="btn p-1.5 text-danger" title={zh?"删除或归档":"Delete or archive"} onClick={async()=>{if(confirm(zh?"删除或归档此联系人？":"Delete or archive this contact?")){await api.deleteContact(c.id);await load();}}}><Trash2 size={13}/></button></div>
      </div>})}
    </div>}

    {editing&&<div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4"><div className="card max-w-2xl w-full space-y-3 max-h-[90vh] overflow-y-auto">
      <h3 className="font-semibold">{editing.id?(zh?"编辑联系人":"Edit Contact"):(zh?"新建联系人":"New Contact")}</h3>
      <div className="grid grid-cols-2 gap-3">
        <Field label={zh?"邮箱 *":"Email *"} value={form.email} set={(v:string)=>setForm({...form,email:v})}/>
        <Field label={zh?"名字":"First name"} value={form.first_name} set={(v:string)=>setForm({...form,first_name:v})}/>
        <Field label={zh?"姓氏":"Last name"} value={form.last_name} set={(v:string)=>setForm({...form,last_name:v})}/>
        <Field label={zh?"公司名称":"Company"} value={form.company} set={(v:string)=>setForm({...form,company:v})}/>
        <Field label={zh?"职位":"Job title"} value={form.title} set={(v:string)=>setForm({...form,title:v})}/>
        <Field label={zh?"电话":"Phone"} value={form.phone} set={(v:string)=>setForm({...form,phone:v})}/>
        <Field label={zh?"网站":"Website"} value={form.website} set={(v:string)=>setForm({...form,website:v})}/>
        <Field label={zh?"时区":"Timezone"} value={form.timezone} set={(v:string)=>setForm({...form,timezone:v})}/>
        <Field label={zh?"来源":"Source"} value={form.source} set={(v:string)=>setForm({...form,source:v})}/>
        <Field label={zh?"标签（逗号分隔）":"Tags (comma separated)"} value={tagText} set={setTagText}/>
        <Field label={zh?"客户分群（逗号或分号分隔）":"Segments (comma or semicolon separated)"} value={segmentText} set={setSegmentText}/>
        <Select label={zh?"分类":"Category"} value={form.category} values={CATEGORIES} set={(v:string)=>setForm({...form,category:v})}/>
        <Select label={zh?"意向等级":"Intent"} value={form.intent_level} values={INTENTS} set={(v:string)=>setForm({...form,intent_level:v})}/>
        <Select label={zh?"销售阶段":"Sales stage"} value={form.lifecycle_stage} values={STAGES} set={(v:string)=>setForm({...form,lifecycle_stage:v})}/>
        <Select label={zh?"下一步动作":"Next action"} value={form.next_action} values={ACTIONS} set={(v:string)=>setForm({...form,next_action:v})}/>
      </div>
      <label className="flex items-center gap-2 text-sm"><input type="checkbox" checked={form.manual_lock} onChange={e=>setForm({...form,manual_lock:e.target.checked})}/><Lock size={13}/>{zh?"锁定人工状态，禁止 Agent 自动覆盖":"Lock manual status so Agent cannot overwrite it"}</label>
      <label className="text-xs text-muted">{zh?"备注":"Notes"}<textarea className="input w-full mt-1 min-h-20" value={form.notes||""} onChange={e=>setForm({...form,notes:e.target.value})}/></label>
      <label className="text-xs text-muted">{zh?"自定义字段 JSON":"Custom fields JSON"}<textarea className="input w-full mt-1 min-h-16 font-mono" value={customFieldsText} onChange={e=>setCustomFieldsText(e.target.value)} placeholder='{"account_tier":"enterprise"}'/></label>
      <p className="text-xs text-muted">{zh?"手动创建需要 Email 和名字或姓氏；公司为可选。":"Manual contacts require Email and at least one name field; Company is optional."}</p>
      <div className="flex gap-2"><button className="btn-primary" onClick={save}>{zh?"保存":"Save"}</button><button className="btn" onClick={()=>setEditing(null)}>{zh?"取消":"Cancel"}</button></div>
    </div></div>}

    {preview&&<div className="fixed inset-0 bg-black/50 z-50 flex items-center justify-center p-4"><div className="card max-w-lg w-full space-y-3"><h3 className="font-semibold">{zh?"导入预览":"Import Preview"}</h3><p>{preview.filename}</p><div className="grid grid-cols-4 text-center"><b>{preview.total}</b><b className="text-ok">{preview.valid}</b><b className="text-danger">{preview.invalid}</b><b className="text-warn">{preview.duplicates}</b><span className="text-xs">Total</span><span className="text-xs">Valid</span><span className="text-xs">Invalid</span><span className="text-xs">Duplicate</span></div><div className="flex gap-2"><button className="btn-primary" disabled={!preview.valid} onClick={confirmImport}>{zh?`确认导入 ${preview.valid} 位联系人`:`Import ${preview.valid} contacts`}</button><button className="btn" onClick={()=>setPreview(null)}>{zh?"取消":"Cancel"}</button></div></div></div>}
  </div>;
}

function Field({label,value,set}:{label:string,value:string,set:(v:string)=>void}){return <label className="text-xs text-muted">{label}<input className="input w-full mt-1" value={value||""} onChange={e=>set(e.target.value)}/></label>;}
function Select({label,value,values,set}:{label:string,value:string,values:string[],set:(v:string)=>void}){return <label className="text-xs text-muted">{label}<select className="input w-full mt-1" value={value||values[0]} onChange={e=>set(e.target.value)}>{values.map(v=><option key={v}>{v}</option>)}</select></label>;}
