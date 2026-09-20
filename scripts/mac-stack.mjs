#!/usr/bin/env node
// macOS source-runtime launcher. Formal packaging remains a later phase.
import { spawn, spawnSync } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

if (process.platform !== "darwin") {
  throw new Error("mac_stack_requires_darwin");
}

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const defaultRoot = path.resolve(scriptDir, "..");
const command = process.argv[2] || "start";
const rootArg = process.argv.indexOf("--root");
const root = path.resolve(rootArg >= 0 && process.argv[rootArg + 1] ? process.argv[rootArg + 1] : defaultRoot);
const development = process.argv.includes("--development");
const productDir = development ? "Email Automation Dev" : "Email Automation";
const dataRoot = path.resolve(process.env.EMAIL_AUTOMATION_DATA_DIR || path.join(os.homedir(), "Library", "Application Support", "TAC AISolution", productDir));
const runDir = path.join(dataRoot, "run");
const logsDir = path.join(dataRoot, "logs");
const statePath = path.join(runDir, "services-mac.json");
const ports = {
  backend: Number(process.env.EMAIL_AUTOMATION_BACKEND_PORT || (development ? 28000 : 18000)),
  frontend: Number(process.env.EMAIL_AUTOMATION_FRONTEND_PORT || (development ? 28001 : 18001)),
  tacworkServer: Number(process.env.TACWORK_SERVER_PORT || (development ? 28002 : 18002)),
  tacworkWeb: Number(process.env.TACWORK_WEB_PORT || (development ? 28003 : 18003)),
};

function processCommand(pid) {
  const result = spawnSync("/bin/ps", ["-p", String(pid), "-o", "command="], { encoding: "utf8" });
  return result.status === 0 ? result.stdout.trim() : "";
}

function stopRecorded() {
  if (!fs.existsSync(statePath)) return;
  let state;
  try { state = JSON.parse(fs.readFileSync(statePath, "utf8")); } catch { throw new Error("mac_stack_state_invalid"); }
  if (state.platform !== "darwin" || path.resolve(state.root || "") !== root) throw new Error("mac_stack_state_owner_mismatch");
  for (const service of [...(state.services || [])].reverse()) {
    const actual = processCommand(service.pid);
    if (!actual || !actual.includes(service.marker)) continue;
    try { process.kill(service.pid, "SIGTERM"); } catch {}
  }
  fs.rmSync(statePath, { force: true });
}

if (command === "stop") {
  stopRecorded();
  process.exit(0);
}
if (command !== "start") throw new Error("usage: mac-stack.mjs start|stop [--root PATH] [--development]");

fs.mkdirSync(runDir, { recursive: true });
fs.mkdirSync(logsDir, { recursive: true });
if (fs.existsSync(statePath)) throw new Error("mac_stack_already_started");

const pythonCandidates = [
  process.env.EMAIL_AUTOMATION_PYTHON,
  path.join(root, "backend", ".venv", "bin", "python"),
  path.join(root, ".venv", "bin", "python"),
  "python3",
].filter(Boolean);
const python = pythonCandidates.find((candidate) => !candidate.includes(path.sep) || fs.existsSync(candidate));
if (!python) throw new Error("mac_python_not_found");

const tacworkRoot = path.resolve(process.env.TACWORK_ROOT || path.join(path.dirname(root), "TACWork"));
const triple = process.arch === "arm64" ? "aarch64-apple-darwin" : "x86_64-apple-darwin";
const tacworkServer = process.env.TACWORK_SERVER_BIN || path.join(tacworkRoot, "apps", "server", "dist", "bin", "openwork-server");
const tacworkEngine = process.env.TACWORK_ENGINE_BIN || path.join(tacworkRoot, "apps", "desktop", "resources", "sidecars", `opencode-${triple}`);
const mcpConfig = path.join(root, "opencode.jsonc");
for (const [name, target] of [["tacwork_server", tacworkServer], ["tacwork_engine", tacworkEngine], ["mcp_config", mcpConfig]]) {
  if (!fs.existsSync(target)) throw new Error(`${name}_missing:${target}`);
}

const commonEnv = {
  ...process.env,
  EMAIL_AUTOMATION_DATA_DIR: dataRoot,
  EMAIL_AUTOMATION_BACKEND_PORT: String(ports.backend),
  EMAIL_AUTOMATION_FRONTEND_PORT: String(ports.frontend),
  TACWORK_SERVER_PORT: String(ports.tacworkServer),
  TACWORK_WEB_PORT: String(ports.tacworkWeb),
  API_URL: `http://127.0.0.1:${ports.backend}`,
  APP_URL: `http://127.0.0.1:${ports.frontend}`,
  GOOGLE_REDIRECT_URI: `http://127.0.0.1:${ports.backend}/api/gmail/oauth/callback`,
  TACWORK_SERVER_URL: `http://127.0.0.1:${ports.tacworkServer}`,
  TACWORK_WEB_URL: `http://127.0.0.1:${ports.tacworkWeb}`,
  NEXT_PUBLIC_API_URL: `http://127.0.0.1:${ports.backend}`,
  NEXT_PUBLIC_TACWORK_URL: `http://127.0.0.1:${ports.tacworkWeb}`,
  PATH: `${path.join(root, "scripts")}:${process.env.PATH || ""}`,
};

const services = [];
function startService(name, executable, args, cwd, env, marker) {
  const stdout = fs.openSync(path.join(logsDir, `${name}.log`), "a");
  const stderr = fs.openSync(path.join(logsDir, `${name}-error.log`), "a");
  const child = spawn(executable, args, { cwd, env, detached: true, stdio: ["ignore", stdout, stderr] });
  child.unref();
  services.push({ name, pid: child.pid, marker });
}

try {
  startService("backend", python, ["-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(ports.backend)], path.join(root, "backend"), commonEnv, "app.main:app");
  startService("consumer", python, ["run_consumer.py"], path.join(root, "backend"), commonEnv, "run_consumer.py");
  startService("frontend", process.env.NPM_BIN || "npm", ["run", "dev", "--", "--hostname", "127.0.0.1", "--port", String(ports.frontend)], path.join(root, "frontend"), commonEnv, "next");

  const hostToken = crypto.randomBytes(32).toString("hex");
  const tacworkEnv = {
    ...commonEnv,
    OPENWORK_MANAGE_OPENCODE: "1",
    OPENWORK_OPENCODE_BIN: tacworkEngine,
    OPENWORK_SERVER_CONFIG: path.join(runDir, "tacwork-server.json"),
    OPENWORK_TOKEN_STORE: path.join(runDir, "tacwork-tokens.json"),
  };
  startService("tacwork-server", tacworkServer, [
    "--workspace", root,
    "--host", "127.0.0.1",
    "--port", String(ports.tacworkServer),
    "--token", "email-automation-local-v1",
    "--host-token", hostToken,
    "--approval", "auto",
    "--cors", `http://127.0.0.1:${ports.tacworkWeb},http://localhost:${ports.tacworkWeb},http://127.0.0.1:${ports.frontend},http://localhost:${ports.frontend}`,
  ], tacworkRoot, tacworkEnv, path.basename(tacworkServer));
  startService("tacwork-web", process.env.PNPM_BIN || "pnpm", ["--filter", "@openwork/app", "exec", "vite", "--host", "127.0.0.1", "--port", String(ports.tacworkWeb), "--strictPort"], tacworkRoot, {
    ...commonEnv,
    VITE_OPENWORK_URL: `http://127.0.0.1:${ports.tacworkServer}`,
    VITE_OPENWORK_PORT: String(ports.tacworkServer),
    VITE_OPENWORK_TOKEN: "email-automation-local-v1",
  }, "vite");

  fs.writeFileSync(statePath, JSON.stringify({ platform: "darwin", root, dataRoot, ports, services }, null, 2), "utf8");
} catch (error) {
  for (const service of services.reverse()) { try { process.kill(service.pid, "SIGTERM"); } catch {} }
  throw error;
}

const deadline = Date.now() + 90_000;
let ready = false;
while (Date.now() < deadline) {
  try {
    const [backend, tacwork, web] = await Promise.all([
      fetch(`http://127.0.0.1:${ports.backend}/api/health`).then((response) => response.json()),
      fetch(`http://127.0.0.1:${ports.tacworkServer}/health`).then((response) => response.json()),
      fetch(`http://127.0.0.1:${ports.tacworkWeb}`).then((response) => response.status),
    ]);
    if (backend.status === "ok" && backend.consumer?.healthy === true && tacwork.ok === true && web === 200) { ready = true; break; }
  } catch {}
  await new Promise((resolve) => setTimeout(resolve, 750));
}
if (!ready) {
  stopRecorded();
  throw new Error("mac_stack_health_timeout");
}

console.log(JSON.stringify({ status: "ready", dataRoot, ports }));
