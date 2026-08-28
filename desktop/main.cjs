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
    if (fs.existsSync(path.join(candidate, "VERSION")) && fs.existsSync(path.join(candidate, "scripts", "start-demo.ps1"))) return candidate;
  }
  throw new Error("runtime_integrity_failed:application_root_not_found");
}
const canListen = (port) => new Promise((resolve) => {
  const server = net.createServer();
  server.once("error", () => resolve(false));
  server.once("listening", () => server.close(() => resolve(true)));
  server.listen(port, "127.0.0.1");
});
async function choosePorts() {
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
      writeDiagnostic("powershell_exit", { script: path.basename(script), code });
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
    if (state.api === `http://127.0.0.1:${selectedPorts.backend}` && state.text.length > 80 && !/Loading dashboard/i.test(state.text)) return state;
    await new Promise((resolve) => setTimeout(resolve, 300));
  }
  throw new Error("runtime_integrity_failed:frontend_hydration_timeout");
}
async function startServices() {
  runtimeStatus = { stage: "port_check", error: null };
  selectedPorts = await choosePorts();
  writeDiagnostic("ports_selected", { ports: selectedPorts });
  runtimeStatus = { stage: "services_starting", ports: selectedPorts, error: null };
  await runPowerShell(path.join(appRoot, "scripts", "start-demo.ps1"), ["-Root", appRoot, "-SkipFrontend"]);
  writeDiagnostic("launcher_complete", { ports: selectedPorts });
  await waitForReady();
  runtimeStatus = { stage: "ready", ports: selectedPorts, error: null };
}
async function stopServices() { if (selectedPorts) try { await runPowerShell(path.join(appRoot, "scripts", "stop-demo.ps1"), ["-Root", appRoot]); } catch {} }
function createWindow() {
  mainWindow = new BrowserWindow({ width: 1500, height: 920, minWidth: 980, minHeight: 680, show: false, backgroundColor: "#080d16", webPreferences: { preload: path.join(__dirname, "preload.cjs"), contextIsolation: true, nodeIntegration: false, sandbox: true } });
  mainWindow.webContents.setWindowOpenHandler(({ url }) => { if (/^https?:\/\//i.test(url)) void shell.openExternal(url); return { action: "deny" }; });
  mainWindow.webContents.on("did-fail-load", (_event, code, description, url) => writeDiagnostic("did_fail_load", { code, description, url }));
  mainWindow.webContents.on("did-finish-load", () => writeDiagnostic("did_finish_load", { url: mainWindow.webContents.getURL() }));
  mainWindow.webContents.on("console-message", (_event, level, message) => writeDiagnostic("renderer_console", { level, message }));
  mainWindow.webContents.on("will-navigate", (event, url) => { if (!url.startsWith("app://email-automation")) { event.preventDefault(); if (/^https?:\/\//i.test(url)) void shell.openExternal(url); } });
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
  await mainWindow.loadURL(`data:text/html;charset=utf-8,${encodeURIComponent(`<!doctype html><meta charset="utf-8"><style>body{font:16px system-ui;background:#080d16;color:#e8edf7;padding:48px}button{padding:10px 16px;margin-right:10px}</style><h1>Email Automation could not start</h1><p>${escaped}</p><p>Logs: ${dataRoot}\\logs</p><button onclick="emailAutomation.openLogs()">Open logs</button><button onclick="emailAutomation.repair()">Repair application</button>`)}`);
  mainWindow.show();
}
ipcMain.handle("open-external", (_event, url) => /^https?:\/\//i.test(url) ? shell.openExternal(url) : false);
ipcMain.handle("open-logs", () => shell.openPath(path.join(dataRoot, "logs")));
ipcMain.handle("runtime-status", () => runtimeStatus);
ipcMain.handle("repair-runtime", async () => {
  const installer = path.join(appRoot, "repair", "Email-Automation-Repair.exe");
  if (!fs.existsSync(installer)) return { ok: false, reason: "repair_source_unavailable" };
  const error = await shell.openPath(installer);
  return error ? { ok: false, reason: error } : { ok: true };
});
ipcMain.handle("quit-and-stop", async () => { quitting = true; await stopServices(); app.quit(); });

if (!app.requestSingleInstanceLock()) app.quit();
app.on("second-instance", () => { if (mainWindow) { mainWindow.show(); mainWindow.focus(); } });
app.whenReady().then(async () => {
  appRoot = rootPath();
  dataRoot = process.env.EMAIL_AUTOMATION_DATA_DIR || path.join(process.env.LOCALAPPDATA || app.getPath("userData"), "TAC AISolution", "Email Automation");
  fs.mkdirSync(dataRoot, { recursive: true });
  writeDiagnostic("app_ready", { appRoot, dataRoot });
  registerAppProtocol(); createWindow();
  try {
    verifyRuntime();
    writeDiagnostic("runtime_verified");
    await startServices();
    writeDiagnostic("services_ready", { ports: selectedPorts });
    await Promise.race([
      mainWindow.loadURL("app://email-automation/"),
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
