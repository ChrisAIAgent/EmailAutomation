const { app, BrowserWindow, dialog, ipcMain, protocol, shell } = require("electron");
const { spawn } = require("node:child_process");
const crypto = require("node:crypto");
const fs = require("node:fs");
const originalFs = require("original-fs");
const net = require("node:net");
const path = require("node:path");

protocol.registerSchemesAsPrivileged([{ scheme: "app", privileges: { standard: true, secure: true, supportFetchAPI: true, corsEnabled: true } }]);
let mainWindow, selectedPorts, appRoot, dataRoot;
let quitting = false;
let runtimeStatus = { stage: "starting", error: null };
const developmentMode = process.env.EMAIL_AUTOMATION_DESKTOP_DEV === "1";
// Raw stderr from the most recent PowerShell launcher call. Retained purely so a
// startup failure can be investigated after the fact.
let lastLauncherStderr = "";
function platformDataRoot(productName) {
  if (process.platform === "darwin") {
    return path.join(app.getPath("appData"), "TAC AISolution", productName);
  }
  return path.join(process.env.LOCALAPPDATA || app.getPath("userData"), "TAC AISolution", productName);
}
const developmentDataRoot = process.env.EMAIL_AUTOMATION_DATA_DIR || path.join(
  platformDataRoot("Email Automation Dev"),
);

// Electron's single-instance lock is scoped to userData.  Give the developer
// shell its own location before requesting that lock, so it can run alongside
// the formal application without sharing Chromium state or suppressing launch.
if (developmentMode) app.setPath("userData", path.join(developmentDataRoot, "electron-shell"));
function writeDiagnostic(event, detail = {}) {
  if (!dataRoot) return;
  const record = { time: new Date().toISOString(), event, ...detail };
  fs.appendFileSync(path.join(dataRoot, "desktop.log"), `${JSON.stringify(record)}\n`);
}

function rootPath() {
  const candidates = [
    process.env.EMAIL_AUTOMATION_APP_ROOT,
    path.resolve(__dirname, ".."),
    path.resolve(process.resourcesPath, "..", "..", ".."),
  ].filter(Boolean);
  for (const candidate of candidates) {
    const launcher = process.platform === "darwin" ? "mac-stack.mjs" : "start-stack.ps1";
    if (fs.existsSync(path.join(candidate, "VERSION")) && fs.existsSync(path.join(candidate, "scripts", launcher))) return candidate;
  }
  throw new Error("runtime_integrity_failed:application_root_not_found");
}
const canListen = (port) => new Promise((resolve) => {
  const server = net.createServer();
  server.once("error", () => resolve(false));
  server.once("listening", () => server.close(() => resolve(true)));
  server.listen(port, "127.0.0.1");
});

// --- On-demand diagnostics (never polled) -----------------------------------
// Investigation runs only when an operator asks for it. Nothing here is on a
// timer, so an idle installation pays zero diagnostic cost.
let lastDiagnostics = null;
const MAX_EVIDENCE = 40;
const BACKEND_PROBE_TIMEOUT_MS = 5000;

const isZhLocale = () => String(app.getLocale() || "").toLowerCase().startsWith("zh");

function redactText(value) {
  let out = String(value == null ? "" : value);
  // 1. Well-known roots keep their meaning but lose their location. Both the
  // raw form and the JSON-escaped form (doubled backslashes) are covered,
  // because evidence often embeds paths inside JSON.stringify output.
  try {
    const jsonEscaped = (s) => s.replace(/\\/g, "\\\\");
    for (const [name, root] of [["appRoot", appRoot], ["dataRoot", dataRoot]]) {
      if (!root) continue;
      out = out.split(root).join(`<${name}>`);
      const escaped = jsonEscaped(root);
      if (escaped !== root) out = out.split(escaped).join(`<${name}>`);
    }
  } catch {}
  // 2. OAuth URL parameters, then generic secrets.
  out = out.replace(/([?&](?:code|state|access_token|refresh_token|id_token|token|client_secret)=)[^&\s'"]+/gi, "$1***REDACTED***");
  out = out
    .replace(/(bearer\s+)[A-Za-z0-9._-]+/gi, "$1***REDACTED***")
    .replace(/((?:api[_-]?key|client[_-]?secret|secret|access[_-]?token|refresh[_-]?token)\s*[:=]\s*)[^\s,'"]+/gi, "$1***REDACTED***");
  // 3. PII and filesystem layout (user dirs before generic paths).
  out = out
    .replace(/[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}/g, (m) => {
      const at = m.indexOf("@");
      return `${m.slice(0, Math.min(3, at))}***@${m.slice(at + 1)}`;
    })
    .replace(/((?:[A-Za-z]:)?[\\/]*(?:Users|home|Documents and Settings)[\\/]+)[^\s,'"/\\]+/gi, "$1***")
    .replace(/[A-Za-z]:[\\/]+(?:[\w.\- ]+[\\/]+)*[\w.\- ]+/g, "<path>")
    .replace(/\/(?:usr|etc|var|opt|tmp|home|root)(?:\/[\w.\-]+)+/g, "<path>");
  return out.slice(0, 220);
}

function tailTextLog(filePath, maxLines = 5) {
  try {
    if (!fs.existsSync(filePath)) return [];
    const text = fs.readFileSync(filePath).toString("utf8").slice(-65536);
    return text.split(/\r?\n/).map((l) => l.trim()).filter(Boolean).slice(-maxLines).map(redactText);
  } catch {
    return [];
  }
}

// desktop.log is JSONL. The event sequence is the single most valuable clue for
// a startup failure: it shows exactly which step the launch died on.
// One over-long field (a data: URL, a stack trace) must not crowd out the rest
// of the event line.
const clip = (value) => {
  const text = typeof value === "object" ? JSON.stringify(value) : String(value);
  return text.length > 160 ? `${text.slice(0, 160)}…` : text;
};

function tailDesktopLog(maxEvents = 25) {
  try {
    const target = path.join(dataRoot, "desktop.log");
    if (!fs.existsSync(target)) return [];
    const lines = fs.readFileSync(target, "utf8").split(/\r?\n/).filter(Boolean);
    const events = [];
    for (const line of lines.slice(-maxEvents * 3)) {
      try {
        const rec = JSON.parse(line);
        const extra = rec.script ? ` script=${rec.script}` : "";
        const code = rec.code !== undefined ? ` code=${rec.code}` : "";
        const ports = rec.ports ? ` ports=${JSON.stringify(rec.ports)}` : "";
        // Everything else carries the payload that actually explains the
        // failure: did_fail_load.description, renderer_console.message, ...
        const detail = Object.keys(rec)
          .filter((k) => !["time", "event", "script", "code", "ports"].includes(k))
          .map((k) => `${k}=${clip(rec[k])}`)
          .join(" ");
        events.push(`${rec.time || "?"} ${rec.event}${extra}${code}${ports}${detail ? ` ${detail}` : ""}`);
      } catch {
        // Skip malformed lines rather than failing the whole investigation.
      }
    }
    return events.slice(-maxEvents).map(redactText);
  } catch {
    return [];
  }
}

function envFacts() {
  let version = "unknown";
  try { version = fs.readFileSync(path.join(appRoot, "VERSION"), "utf8").trim(); } catch {}
  return {
    version, appRoot, dataRoot,
    mode: developmentMode ? "dev" : "formal",
    electron: (process.versions && process.versions.electron) || "n/a",
  };
}

function startupRootCause(errorText) {
  const e = String(errorText || "");
  if (e.includes("application_root_not_found")) return "app_root_missing";
  if (e.includes("stale_runtime")) return "runtime_version_mismatch";
  if (e.includes("manifest_missing") || e.includes(":missing:") || e.includes(":hash_mismatch:") || e.includes(":invalid_manifest_entry:")) return "runtime_manifest_invalid";
  if (e.includes("port_conflict")) return "port_group_occupied";
  if (e.includes("service_start_failed:exit_")) return "launcher_failed";
  if (e.includes("health_timeout")) return "backend_unreachable";
  if (e.includes("frontend_load_timeout") || e.includes("frontend_hydration_timeout")) return "frontend_load_failed";
  return "unknown";
}

const ROOT_CAUSE_LABEL = {
  app_root_missing: { zh: "未找到应用根目录", en: "Application root not found" },
  runtime_version_mismatch: { zh: "runtime 清单与 VERSION 不一致", en: "Runtime manifest/version mismatch" },
  runtime_manifest_invalid: { zh: "runtime 清单校验失败", en: "Runtime manifest invalid" },
  port_group_occupied: { zh: "端口组被占用", en: "Port group occupied" },
  launcher_failed: { zh: "启动脚本执行失败", en: "Launcher script failed" },
  backend_unreachable: { zh: "后端服务未能就绪", en: "Backend never became ready" },
  frontend_load_failed: { zh: "前端页面加载失败", en: "Frontend failed to load" },
  unknown: { zh: "未分类启动失败", en: "Unclassified startup failure" },
};

const ROOT_CAUSE_REMEDIES = {
  app_root_missing: [{ action: "reinstall", risk: "medium", requires_confirmation: true }],
  runtime_version_mismatch: [{ action: "reinstall", risk: "medium", requires_confirmation: true }],
  runtime_manifest_invalid: [{ action: "repair", risk: "low", requires_confirmation: true }],
  port_group_occupied: [{ action: "free_ports", risk: "low", requires_confirmation: true }],
  launcher_failed: [{ action: "inspect_launcher_stderr", risk: "low", requires_confirmation: true }],
  backend_unreachable: [{ action: "inspect_backend_logs", risk: "low", requires_confirmation: true }],
  frontend_load_failed: [{ action: "reload", risk: "low", requires_confirmation: true }],
  unknown: [{ action: "open_logs", risk: "low", requires_confirmation: true }],
};

const REMEDY_LABEL = {
  reinstall: { zh: "重新安装本版本", en: "Reinstall this version" },
  repair: { zh: "运行「修复应用程序」重建 runtime", en: "Run “Repair application” to rebuild the runtime" },
  free_ports: { zh: "关闭占用 18000/18002/18003 的程序后重试", en: "Close whatever holds 18000/18002/18003, then retry" },
  inspect_launcher_stderr: { zh: "查看启动器 stderr 与 logs/backend-error.log", en: "Inspect launcher stderr and logs/backend-error.log" },
  inspect_backend_logs: { zh: "查看 logs/backend-error.log 与 consumer-error.log", en: "Inspect logs/backend-error.log and consumer-error.log" },
  reload: { zh: "重新加载窗口；持续失败则运行修复", en: "Reload the window; run repair if it keeps failing" },
  open_logs: { zh: "打开日志目录并回传 diagnostics-report.json", en: "Open the logs folder and send diagnostics-report.json" },
};

async function collectLocalEvidence() {
  const evidence = [];
  const stage = (runtimeStatus && runtimeStatus.stage) || "unknown";
  const errText = String((runtimeStatus && runtimeStatus.error) || "");
  evidence.push({
    source: "electron.runtime-status",
    finding: redactText(`stage=${stage}; error=${errText || "none"}; ports=${JSON.stringify((runtimeStatus && runtimeStatus.ports) || selectedPorts || null)}`),
  });
  evidence.push({ source: "electron.env", finding: redactText(JSON.stringify(envFacts())) });
  // Raw launcher stderr is NEVER emitted — it routinely contains absolute
  // paths, user names and occasionally credentials. Only its existence and
  // size are reported; the operator is pointed at the log file instead.
  if (lastLauncherStderr) {
    evidence.push({ source: "launcher.stderr", finding: `captured ${lastLauncherStderr.length} bytes (content withheld; see logs/backend-error.log)` });
  }

  const events = tailDesktopLog(25);
  if (events.length) for (const e of events) evidence.push({ source: "electron.desktop.log", finding: e });
  else evidence.push({ source: "electron.desktop.log", finding: "missing or empty" });

  if (selectedPorts) {
    for (const [name, port] of Object.entries({
      backend: selectedPorts.backend,
      tacworkServer: selectedPorts.tacworkServer,
      tacworkWeb: selectedPorts.tacworkWeb,
    })) {
      const free = await canListen(port);
      evidence.push({ source: `port.${name}`, finding: `${port} ${free ? "free" : "OCCUPIED"}` });
    }
  }

  const logsDir = path.join(dataRoot, "logs");
  for (const name of ["backend-error.log", "consumer-error.log", "frontend-error.log", "tacwork-server-error.log"]) {
    for (const line of tailTextLog(path.join(logsDir, name), 5)) {
      evidence.push({ source: `log.${name}`, finding: line });
    }
  }
  return evidence.slice(0, MAX_EVIDENCE);
}

async function investigateDiagnostics(target = "system", incidentId = null) {
  if (!dataRoot) return { status: "unavailable", summary: "Electron runtime is not initialized" };
  const incident_id = incidentId || `electron-${Date.now()}`;
  const zh = isZhLocale();

  const evidence = await collectLocalEvidence();
  const errText = String((runtimeStatus && runtimeStatus.error) || "");
  const root_cause = startupRootCause(errText);

  // Try the backend, but never block on it: a startup failure usually means the
  // backend is exactly what is broken.
  let backendUp = false;
  let reports = [];
  if (selectedPorts) {
    try {
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), BACKEND_PROBE_TIMEOUT_MS);
      const res = await fetch(`http://127.0.0.1:${selectedPorts.backend}/api/system/diagnostics/investigate`, {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-Request-ID": incident_id },
        body: JSON.stringify({ target, incident_id }),
        signal: controller.signal,
      });
      clearTimeout(timer);
      if (!res.ok) throw new Error(`http ${res.status}`);
      const data = await res.json();
      backendUp = true;
      if (Array.isArray(data && data.reports)) reports = data.reports;
    } catch (e) {
      evidence.push({ source: "backend.probe", finding: redactText(`unreachable: ${String((e && e.message) || e)}`) });
    }
  }

  const label = ROOT_CAUSE_LABEL[root_cause] || ROOT_CAUSE_LABEL.unknown;
  const remedies = (ROOT_CAUSE_REMEDIES[root_cause] || ROOT_CAUSE_REMEDIES.unknown).map((r) => {
    const rl = REMEDY_LABEL[r.action] || { zh: r.action, en: r.action };
    return { ...r, label: zh ? rl.zh : rl.en };
  });

  const localReport = {
    target: "electron.startup",
    status: root_cause === "unknown" ? "suspected" : "confirmed",
    root_cause,
    root_cause_label: zh ? label.zh : label.en,
    confidence: backendUp ? "high" : "medium",
    summary: zh
      ? `启动阶段 ${(runtimeStatus && runtimeStatus.stage) || "unknown"} 失败${backendUp ? "（后端仍可达，已并入后端结果）" : "（后端不可达，仅本地证据）"}。`
      : `Startup failed at stage ${(runtimeStatus && runtimeStatus.stage) || "unknown"}${backendUp ? " (backend reachable; backend results merged)" : " (backend unreachable; local evidence only)"}.`,
    evidence,
    remedies,
    next_checks: [zh ? "回传 logs/diagnostics-report.json 以便进一步定位" : "Send logs/diagnostics-report.json for deeper analysis"],
  };

  const out = {
    incident_id,
    target,
    generated_at: new Date().toISOString(),
    trace_id: incident_id,
    backend_up: backendUp,
    reports: [localReport, ...reports],
  };

  try {
    const logsDir = path.join(dataRoot, "logs");
    fs.mkdirSync(logsDir, { recursive: true });
    fs.writeFileSync(path.join(logsDir, "diagnostics-report.json"), JSON.stringify(out, null, 2), "utf8");
  } catch {}

  lastDiagnostics = out;
  writeDiagnostic("diagnostics_investigated", { incident_id, target, root_cause, backend_up: backendUp });
  return out;
}
async function choosePorts() {
  if (developmentMode) return { backend: 28000, frontend: 28001, tacworkServer: 28002, tacworkWeb: 28003 };
  for (let base = 18000; base <= 18900; base += 10) {
    const ports = { backend: base, frontend: base + 1, tacworkServer: base + 2, tacworkWeb: base + 3 };
    if ((await canListen(ports.backend)) && (await canListen(ports.tacworkServer)) && (await canListen(ports.tacworkWeb))) return ports;
  }
  throw new Error("port_conflict:no_complete_local_port_group_available");
}
function runtimeConfigSource() {
  const config = { apiUrl: `http://127.0.0.1:${selectedPorts.backend}`, tacworkUrl: `http://127.0.0.1:${selectedPorts.tacworkWeb}`, tacworkServerUrl: `http://127.0.0.1:${selectedPorts.tacworkServer}`, version: fs.readFileSync(path.join(appRoot, "VERSION"), "utf8").trim() };
  return `window.__EMAIL_AUTOMATION_RUNTIME__ = ${JSON.stringify(config)};`;
}
function verifyRuntime() {
  runtimeStatus = { stage: "runtime_check", error: null };
  const manifestPath = path.join(appRoot, "runtime", "runtime-manifest.json");
  if (!fs.existsSync(manifestPath)) throw new Error("runtime_integrity_failed:manifest_missing");
  const manifest = JSON.parse(fs.readFileSync(manifestPath, "utf8").replace(/^\uFEFF/, ""));
  const version = fs.readFileSync(path.join(appRoot, "VERSION"), "utf8").trim();
  if (manifest.version !== version) throw new Error("stale_runtime:manifest_version_mismatch");
  const forbidden = /(^|\/)(frontend\.pre-|\.frontend-repair-|\.frontend-build|\.frontend-runtime-next)/i;
  for (const item of manifest.files || []) {
    const relative = String(item.path || "").replace(/\\/g, "/");
    if (!relative.startsWith("runtime/") || forbidden.test(relative)) throw new Error(`runtime_integrity_failed:invalid_manifest_entry:${relative}`);
    const target = path.resolve(appRoot, ...relative.split("/"));
    if (!target.startsWith(path.resolve(appRoot, "runtime") + path.sep) || !fs.existsSync(target)) throw new Error(`runtime_integrity_failed:missing:${relative}`);
    const digest = crypto.createHash("sha256").update(originalFs.readFileSync(target)).digest("hex").toUpperCase();
    if (digest !== String(item.sha256 || "").toUpperCase()) throw new Error(`runtime_integrity_failed:hash_mismatch:${relative}`);
  }
  for (const required of ["runtime/frontend-static/index.html", "runtime/electron/Email Automation.exe", "runtime/electron/resources/app/main.cjs", "runtime/python-packages"]) {
    if (!fs.existsSync(path.join(appRoot, ...required.split("/")))) throw new Error(`runtime_integrity_failed:missing:${required}`);
  }
}
function registerAppProtocol() {
  const staticRoot = path.resolve(appRoot, "runtime", "frontend-static");
  const contentTypes = { ".html": "text/html; charset=utf-8", ".js": "application/javascript; charset=utf-8", ".css": "text/css; charset=utf-8", ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon", ".woff2": "font/woff2" };
  protocol.handle("app", async (request) => {
    const url = new URL(request.url);
    let relative = decodeURIComponent(url.pathname).replace(/^\/+/, "") || "index.html";
    if (relative === "runtime-config.js") return new Response(runtimeConfigSource(), { headers: { "content-type": "application/javascript; charset=utf-8" } });
    let target = path.resolve(staticRoot, relative);
    if (target !== staticRoot && !target.startsWith(staticRoot + path.sep)) return new Response("Forbidden", { status: 403 });
    if (!fs.existsSync(target) || fs.statSync(target).isDirectory()) {
      const nested = path.join(target, "index.html");
      target = fs.existsSync(nested) ? nested : path.join(staticRoot, "404.html");
    }
    if (!fs.existsSync(target)) return new Response("Not found", { status: 404 });
    return new Response(fs.readFileSync(target), { headers: { "content-type": contentTypes[path.extname(target).toLowerCase()] || "application/octet-stream" } });
  });
}
function runPowerShell(script, args = []) {
  return new Promise((resolve, reject) => {
    writeDiagnostic("powershell_start", { script: path.basename(script), args });
    const child = spawn("powershell.exe", ["-NoProfile", "-ExecutionPolicy", "Bypass", "-File", script, ...args], { cwd: appRoot, windowsHide: true, env: { ...process.env, EMAIL_AUTOMATION_DATA_DIR: dataRoot, EMAIL_AUTOMATION_BACKEND_PORT: String(selectedPorts.backend), EMAIL_AUTOMATION_FRONTEND_PORT: String(selectedPorts.frontend), TACWORK_SERVER_PORT: String(selectedPorts.tacworkServer), TACWORK_WEB_PORT: String(selectedPorts.tacworkWeb) } });
    let stderr = "";
    child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
    child.once("error", reject);
    child.once("exit", (code) => {
      // Keep the raw stderr: when startup fails it is the most direct evidence
      // of why the launcher died (consumed by investigateDiagnostics).
      lastLauncherStderr = stderr.trim();
      writeDiagnostic("powershell_exit", { script: path.basename(script), code });
      code === 0 ? resolve() : reject(new Error(stderr.trim() || `service_start_failed:exit_${code}`));
    });
  });
}
function runMacStack(action) {
  return new Promise((resolve, reject) => {
    const script = path.join(appRoot, "scripts", "mac-stack.mjs");
    const args = [script, action, "--root", appRoot, ...(developmentMode ? ["--development"] : [])];
    writeDiagnostic("mac_stack_start", { action });
    const child = spawn(process.env.EMAIL_AUTOMATION_NODE_BIN || "node", args, {
      cwd: appRoot,
      env: {
        ...process.env,
        EMAIL_AUTOMATION_DATA_DIR: dataRoot,
        EMAIL_AUTOMATION_BACKEND_PORT: String(selectedPorts.backend),
        EMAIL_AUTOMATION_FRONTEND_PORT: String(selectedPorts.frontend),
        TACWORK_SERVER_PORT: String(selectedPorts.tacworkServer),
        TACWORK_WEB_PORT: String(selectedPorts.tacworkWeb),
      },
    });
    let stderr = "";
    child.stderr.on("data", (chunk) => { stderr += chunk.toString(); });
    child.once("error", reject);
    child.once("exit", (code) => {
      lastLauncherStderr = stderr.trim();
      writeDiagnostic("mac_stack_exit", { action, code });
      code === 0 ? resolve() : reject(new Error(stderr.trim() || `service_start_failed:exit_${code}`));
    });
  });
}
async function waitForReady() {
  const deadline = Date.now() + 90000;
  while (Date.now() < deadline) {
    try {
      const [health, tacServer, tacWeb] = await Promise.all([fetch(`http://127.0.0.1:${selectedPorts.backend}/api/health`).then((r) => r.json()), fetch(`http://127.0.0.1:${selectedPorts.tacworkServer}/health`).then((r) => r.json()), fetch(`http://127.0.0.1:${selectedPorts.tacworkWeb}`).then((r) => r.status)]);
      if (health.status === "ok" && health.consumer?.healthy === true && tacServer.ok === true && tacWeb === 200) return;
    } catch {}
    await new Promise((resolve) => setTimeout(resolve, 750));
  }
  throw new Error("service_start_failed:health_timeout");
}
async function waitForUiReady() {
  const deadline = Date.now() + 20000;
  while (Date.now() < deadline) {
    const state = await mainWindow.webContents.executeJavaScript(`({ text: document.body.innerText.slice(0, 500), api: window.__EMAIL_AUTOMATION_RUNTIME__?.apiUrl || "" })`, true);
    const expectedApi = `http://127.0.0.1:${selectedPorts.backend}`;
    // The formal shell reads its dynamic configuration from app://.  The
    // development shell deliberately uses source Next dev, whose public API
    // URL is compiled from its isolated process environment instead.
    const apiReady = developmentMode ? !state.api || state.api === expectedApi : state.api === expectedApi;
    if (apiReady && state.text.length > 80 && !/Loading dashboard/i.test(state.text)) return state;
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  throw new Error("runtime_integrity_failed:frontend_hydration_timeout");
}
async function startServices() {
  runtimeStatus = { stage: "port_check", error: null };
  selectedPorts = await choosePorts();
  writeDiagnostic("ports_selected", { ports: selectedPorts });
  runtimeStatus = { stage: "services_starting", ports: selectedPorts, error: null };
  if (process.platform === "darwin") {
    await runMacStack("start");
  } else {
    const script = developmentMode ? "dev-stack.ps1" : "start-stack.ps1";
    const args = developmentMode ? ["-Root", appRoot] : ["-Root", appRoot, "-SkipFrontend"];
    await runPowerShell(path.join(appRoot, "scripts", script), args);
  }
  writeDiagnostic("launcher_complete", { ports: selectedPorts });
  await waitForReady();
  runtimeStatus = { stage: "ready", ports: selectedPorts, error: null };
}
async function stopServices() {
  if (!selectedPorts) return;
  try {
    if (process.platform === "darwin") {
      await runMacStack("stop");
    } else {
      await runPowerShell(
        path.join(appRoot, "scripts", developmentMode ? "dev-stop.ps1" : "stop-stack.ps1"),
        ["-Root", appRoot],
      );
    }
  } catch {}
}
function createWindow() {
  // Brand window/taskbar icon. `__dirname` is desktop/ in development and
  // runtime/electron/resources/app/ once packaged, so one relative path covers
  // both layouts. Guarded: a missing file degrades to the default icon instead
  // of failing window creation.
  const windowIcon = path.join(__dirname, "brand", "app-icon.ico");
  mainWindow = new BrowserWindow({ width: 1500, height: 920, minWidth: 980, minHeight: 680, show: false, backgroundColor: "#080d16", icon: fs.existsSync(windowIcon) ? windowIcon : undefined, webPreferences: { preload: path.join(__dirname, "preload.cjs"), contextIsolation: true, nodeIntegration: false, sandbox: true } });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => { if (/^https?:\/\//i.test(url)) void shell.openExternal(url); return { action: "deny" }; });
  mainWindow.webContents.on("did-fail-load", (_event, code, description, url) => writeDiagnostic("did_fail_load", { code, description, url }));
  mainWindow.webContents.on("did-finish-load", () => writeDiagnostic("did_finish_load", { url: mainWindow.webContents.getURL() }));
  mainWindow.webContents.on("console-message", (_event, level, message) => writeDiagnostic("renderer_console", { level, message }));
  mainWindow.webContents.on("will-navigate", (event, url) => {
    const isFormalUi = url.startsWith("app://email-automation");
    const isDevelopmentUi = developmentMode && url.startsWith(`http://127.0.0.1:${selectedPorts.frontend}`);
    if (!isFormalUi && !isDevelopmentUi) {
      event.preventDefault();
      if (/^https?:\/\//i.test(url)) void shell.openExternal(url);
    }
  });
  mainWindow.on("close", async (event) => {
    if (quitting) return;
    event.preventDefault();
    const choice = await dialog.showMessageBox(mainWindow, { type: "question", buttons: ["Keep running", "Exit and stop services", "Cancel"], defaultId: 0, cancelId: 2, message: "Close Email Automation", detail: "Keep services running in the background, or exit and stop this application's service tree." });
    if (choice.response === 0) mainWindow.hide();
    if (choice.response === 1) { quitting = true; await stopServices(); app.quit(); }
  });
}
async function showFatal(error) {
  runtimeStatus = { ...runtimeStatus, stage: "failed", error: String(error?.message || error) };
  const escaped = runtimeStatus.error.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const zh = isZhLocale();
  const t = {
    title: zh ? "Email Automation 无法启动" : "Email Automation could not start",
    logsLabel: "Logs: " + dataRoot + "\\logs",
    openLogs: zh ? "打开日志目录" : "Open logs",
    repair: zh ? "修复应用程序" : "Repair application",
    diagBtn: zh ? "查看诊断报告" : "View diagnostic report",
    diagRunning: zh ? "正在诊断…" : "Investigating…",
    diagTitle: zh ? "诊断报告" : "Diagnostic report",
    diagTime: zh ? "诊断时间" : "Generated at",
    cause: zh ? "根因" : "Root cause",
    unknownNote: zh ? "无法确认根因，需要重试或查看日志" : "Root cause could not be confirmed; retry or check the logs",
    evidence: zh ? "证据" : "Evidence",
    remedy: zh ? "修复建议" : "Remedies",
    confirm: zh ? "需确认后执行" : "requires confirmation",
    failed: zh ? "本次调查失败" : "Investigation failed",
    kept: zh ? "已保留上一次结果" : "previous result kept",
  };

  // Built with string concatenation (no template literals / ${}) so it can be
  // embedded verbatim inside the outer template literal below.
  const pageJs = [
    "function esc(s){return String(s==null?'':s).replace(/[&<>\"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c];});}",
    "async function runDiag(){",
    "  var b=document.getElementById('diagbtn');var o=document.getElementById('diagout');",
    "  b.disabled=true;b.textContent='DIAG_RUNNING';o.textContent='DIAG_RUNNING';",
    "  try{",
    "    var r=await emailAutomation.investigate();",
    "    var h='';",
    "    h+='<div style=\"margin-top:18px;border:1px solid #2a3550;border-radius:10px;padding:14px\">';",
    "    h+='<div style=\"font-weight:700;margin-bottom:6px\">DIAG_TITLE &mdash; '+esc(r.incident_id)+'</div>';",
    "    h+='<div style=\"font-size:13px;color:#93a1bd;margin-bottom:10px\">DIAG_TIME: '+esc(r.generated_at||'-')+' &nbsp;&middot;&nbsp; backend_up='+esc(r.backend_up)+'</div>';",
    "    (r.reports||[]).forEach(function(rep){",
    "      h+='<div style=\"margin-bottom:14px;padding-bottom:12px;border-bottom:1px solid #1c2438\">';",
    "      h+='<div><b>'+esc(rep.target)+'</b> &nbsp;<span style=\"color:#ff8f8f\">'+esc(rep.status)+'</span></div>';",
    "      h+='<div style=\"margin-top:4px\"><b>DIAG_CAUSE:</b> '+esc(rep.root_cause_label||rep.root_cause||'-')+' &nbsp;<span style=\"color:#93a1bd\">('+esc(rep.confidence||'-')+')</span></div>';",
    "      h+='<div style=\"margin-top:4px;color:#c7d2e5\">'+esc(rep.summary)+'</div>';",
    "      if(rep.status==='unknown'){h+='<div style=\"margin-top:6px;color:#ffd479\">&#9888; DIAG_UNKNOWN</div>';}",
    "      if(rep.evidence&&rep.evidence.length){",
    "        h+='<div style=\"margin-top:8px;font-weight:600\">DIAG_EVIDENCE</div>';",
    "        h+='<div style=\"margin-top:4px;font-family:ui-monospace,Consolas,monospace;font-size:12px;color:#93a1bd;max-height:260px;overflow:auto\">';",
    "        rep.evidence.forEach(function(e){h+='<div>&middot; '+esc(e.source)+': '+esc(e.finding)+'</div>';});",
    "        h+='</div>';",
    "      }",
    "      if(rep.remedies&&rep.remedies.length){",
    "        h+='<div style=\"margin-top:8px;font-weight:600\">DIAG_REMEDY</div>';",
    "        h+='<ul style=\"margin:4px 0 0 18px;font-size:13px\">';",
    "        rep.remedies.forEach(function(x){h+='<li>'+esc(x.label)+(x.requires_confirmation?' <span style=\"color:#93a1bd\">(DIAG_CONFIRM)</span>':'')+'</li>';});",
    "        h+='</ul>';",
    "      }",
    "      h+='</div>';",
    "    });",
    "    h+='</div>';",
    "    o.innerHTML=h;",
    "  }catch(e){",
    "    var banner='<div style=\"margin:10px 0;padding:8px 12px;border:1px solid #7a3b3b;border-radius:8px;color:#ff9c9c\">DIAG_FAILED: '+esc((e&&e.message)||e)+(o.innerHTML?'<div style=\"font-size:12px;color:#93a1bd;margin-top:4px\">DIAG_KEPT</div>':'')+'</div>';",
    "    o.innerHTML=banner+o.innerHTML;",
    "  }",
    "  b.disabled=false;b.textContent='DIAG_BTN';",
    "}",
  ].join("\n")
    .replace(/DIAG_RUNNING/g, t.diagRunning)
    .replace(/DIAG_TITLE/g, t.diagTitle)
    .replace(/DIAG_TIME/g, t.diagTime)
    .replace(/DIAG_UNKNOWN/g, t.unknownNote)
    .replace(/DIAG_CAUSE/g, t.cause)
    .replace(/DIAG_EVIDENCE/g, t.evidence)
    .replace(/DIAG_REMEDY/g, t.remedy)
    .replace(/DIAG_CONFIRM/g, t.confirm)
    .replace(/DIAG_KEPT/g, t.kept)
    .replace(/DIAG_FAILED/g, t.failed)
    .replace(/DIAG_BTN/g, t.diagBtn);

  await mainWindow.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(`<!doctype html><meta charset="utf-8"><style>body{font:16px system-ui;background:#080d16;color:#e8edf7;padding:48px}button{padding:10px 16px;margin-right:10px}</style><h1>${t.title}</h1><p>${escaped}</p><p>${t.logsLabel}</p><button onclick="emailAutomation.openLogs()">${t.openLogs}</button><button onclick="emailAutomation.repair()">${t.repair}</button><button id="diagbtn" onclick="runDiag()">${t.diagBtn}</button><div id="diagout"></div><script>${pageJs}</script>`)}`);
  mainWindow.show();
}
ipcMain.handle("open-external", (_event, url) => /^https?:\/\//i.test(url) ? shell.openExternal(url) : false);
ipcMain.handle("open-logs", () => shell.openPath(path.join(dataRoot, "logs")));
ipcMain.handle("runtime-status", () => runtimeStatus);
ipcMain.handle("diagnostics", () => lastDiagnostics);
ipcMain.handle("diagnostics-investigate", async (_event, target) => await investigateDiagnostics(target || "system"));
ipcMain.handle("repair-runtime", async () => {
  const installer = path.join(appRoot, "repair", "Email-Automation-Repair.exe");
  if (!fs.existsSync(installer)) return { ok: false, reason: "repair_source_unavailable" };
  const error = await shell.openPath(installer);
  return error ? { ok: false, reason: error } : { ok: true };
});
ipcMain.handle("quit-and-stop", async () => { quitting = true; await stopServices(); app.quit(); });
ipcMain.handle("reload-window", () => { if (mainWindow) mainWindow.webContents.reloadIgnoringCache(); });

if (!app.requestSingleInstanceLock()) app.quit();
app.on("second-instance", () => { if (mainWindow) { mainWindow.show(); mainWindow.focus(); } });
app.whenReady().then(async () => {
  appRoot = rootPath();
  dataRoot = process.env.EMAIL_AUTOMATION_DATA_DIR || platformDataRoot("Email Automation");
  fs.mkdirSync(dataRoot, { recursive: true });
  writeDiagnostic("app_ready", { appRoot, dataRoot });
  registerAppProtocol(); createWindow();
  try {
    if (developmentMode) {
      writeDiagnostic("development_shell_ready");
    } else {
      verifyRuntime();
      writeDiagnostic("runtime_verified");
    }
    await startServices();
    writeDiagnostic("services_ready", { ports: selectedPorts });
    await Promise.race([
      mainWindow.loadURL(developmentMode ? `http://127.0.0.1:${selectedPorts.frontend}/` : "app://email-automation/"),
      new Promise((_, reject) => setTimeout(() => reject(new Error("runtime_integrity_failed:frontend_load_timeout")), 20000)),
    ]);
    const ui = await waitForUiReady();
    writeDiagnostic("frontend_ready", { textLength: ui.text.length });
    if (process.env.EMAIL_AUTOMATION_DESKTOP_SMOKE === "1") {
      const completedStatus = { ...runtimeStatus, ui_loaded: true };
      quitting = true;
      await stopServices();
      fs.writeFileSync(path.join(dataRoot, "desktop-smoke.json"), JSON.stringify(completedStatus));
      app.quit(); return;
    }
    mainWindow.show();
  }
  catch (error) { await showFatal(error); }
});
app.on("window-all-closed", () => { if (process.platform !== "darwin" && quitting) app.quit(); });
