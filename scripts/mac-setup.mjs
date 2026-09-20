#!/usr/bin/env node
// Prepare a copied source tree for native macOS development. This script only
// writes ignored development state and dependency folders; it never imports
// Windows credentials, databases, logs, or runtime binaries.
import { spawnSync } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

if (process.platform !== "darwin") throw new Error("mac_setup_requires_darwin");
if (process.arch !== "arm64" && process.env.MAC_ALLOW_ROSETTA !== "1") {
  throw new Error("mac_setup_requires_native_arm64_node:run_with_native_arm64_node_or_set_MAC_ALLOW_ROSETTA=1");
}

const scriptDir = path.dirname(fileURLToPath(import.meta.url));
const defaultRoot = path.resolve(scriptDir, "..");
const rootArg = process.argv.indexOf("--root");
const root = path.resolve(rootArg >= 0 && process.argv[rootArg + 1] ? process.argv[rootArg + 1] : defaultRoot);
const forceInstall = process.argv.includes("--force-install");
const dataRoot = path.resolve(process.env.EMAIL_AUTOMATION_DATA_DIR || path.join(os.homedir(), "Library", "Application Support", "TAC AISolution", "Email Automation Dev"));
const configDir = path.join(dataRoot, "config");
const envPath = path.join(configDir, ".env");
const venvDir = path.join(root, "backend", ".venv");
const venvPython = path.join(venvDir, "bin", "python");

function fail(message) { throw new Error(message); }

function run(command, args, cwd = root) {
  const result = spawnSync(command, args, { cwd, stdio: "inherit", env: process.env });
  if (result.error) fail(`${command}_unavailable:${result.error.message}`);
  if (result.status !== 0) fail(`${command}_failed:${result.status}`);
}

function commandWorks(command, args = ["--version"]) {
  const result = spawnSync(command, args, { stdio: "ignore" });
  return !result.error && result.status === 0;
}

function version(command, args = ["--version"]) {
  const result = spawnSync(command, args, { encoding: "utf8" });
  if (result.error || result.status !== 0) return "";
  return `${result.stdout || ""}${result.stderr || ""}`.trim();
}

function ensureMajorAtLeast(label, value, minimum) {
  const major = Number(String(value).match(/(\d+)/)?.[1] || 0);
  if (major < minimum) fail(`${label}_version_too_old:${value}`);
}

function newFernetKey() {
  return crypto.randomBytes(32).toString("base64").replace(/\+/g, "-").replace(/\//g, "_");
}

function newSecret() {
  return crypto.randomBytes(32).toString("hex");
}

function writeInitialEnv() {
  fs.mkdirSync(configDir, { recursive: true });
  if (fs.existsSync(envPath)) return false;
  const contents = [
    "APP_ENV=development",
    "APP_URL=http://127.0.0.1:28001",
    "API_URL=http://127.0.0.1:28000",
    "GOOGLE_REDIRECT_URI=http://127.0.0.1:28000/api/gmail/oauth/callback",
    "TACWORK_SERVER_URL=http://127.0.0.1:28002",
    "TACWORK_WEB_URL=http://127.0.0.1:28003",
    "CORS_ORIGINS=app://email-automation,http://127.0.0.1:28001,http://localhost:28001",
    `APP_ENCRYPTION_KEY=${newFernetKey()}`,
    `SECRET_KEY=${newSecret()}`,
    "ENABLE_REAL_SEND=false",
    "ENABLE_SCHEDULER=true",
    "",
    "# Fill in the local LLM settings before asking the Agent to generate content.",
    "# LLM_PROVIDER=openai",
    "# LLM_BASE_URL=https://your-llm-endpoint.example.com/v1",
    "# LLM_API_KEY=",
    "# LLM_MODEL=your-model-name",
    "",
  ].join("\n");
  fs.writeFileSync(envPath, contents, { encoding: "utf8", mode: 0o600 });
  return true;
}

function ensureOpencodeConfig() {
  const target = path.join(root, "opencode.jsonc");
  const example = path.join(root, "opencode.jsonc.example");
  if (fs.existsSync(target)) return false;
  if (!fs.existsSync(example)) fail(`opencode_config_example_missing:${example}`);
  fs.copyFileSync(example, target, fs.constants.COPYFILE_EXCL);
  return true;
}

function ensurePython() {
  if (!commandWorks("python3")) fail("python3_missing:install_python_3_11_or_newer");
  ensureMajorAtLeast("python3", version("python3"), 3);
  const minor = Number(version("python3").match(/\d+\.(\d+)/)?.[1] || 0);
  if (minor < 11) fail(`python3_version_too_old:${version("python3")}`);
  if (!fs.existsSync(venvPython)) run("python3", ["-m", "venv", venvDir]);
  if (!fs.existsSync(venvPython)) fail(`venv_python_missing:${venvPython}`);
  if (forceInstall || !fs.existsSync(path.join(venvDir, ".email-automation-requirements-installed"))) {
    run(venvPython, ["-m", "pip", "install", "-r", path.join(root, "backend", "requirements.txt")]);
    fs.writeFileSync(path.join(venvDir, ".email-automation-requirements-installed"), new Date().toISOString());
  }
}

function ensureNodeDependencies() {
  ensureMajorAtLeast("node", process.version, 18);
  if (!commandWorks("npm")) fail("npm_missing:install_node_18_or_newer");
  for (const directory of ["frontend", "desktop"]) {
    const packageRoot = path.join(root, directory);
    const marker = path.join(packageRoot, "node_modules");
    if (forceInstall || !fs.existsSync(marker)) run("npm", ["ci"], packageRoot);
  }
}

function checkTacwork() {
  const tacworkRoot = path.resolve(process.env.TACWORK_ROOT || path.join(path.dirname(root), "TACWork"));
  const triple = process.arch === "arm64" ? "aarch64-apple-darwin" : "x86_64-apple-darwin";
  const server = process.env.TACWORK_SERVER_BIN || path.join(tacworkRoot, "apps", "server", "dist", "bin", "openwork-server");
  const engine = process.env.TACWORK_ENGINE_BIN || path.join(tacworkRoot, "apps", "desktop", "resources", "sidecars", `opencode-${triple}`);
  if (!fs.existsSync(server)) fail(`tacwork_server_missing:${server}`);
  if (!fs.existsSync(engine)) fail(`tacwork_engine_missing:${engine}`);
  try { fs.accessSync(server, fs.constants.X_OK); } catch { fail(`tacwork_server_not_executable:${server}`); }
  try { fs.accessSync(engine, fs.constants.X_OK); } catch { fail(`tacwork_engine_not_executable:${engine}`); }
  if (!commandWorks(process.env.PNPM_BIN || "pnpm")) fail("pnpm_missing:install_pnpm_for_tacwork_web");
  return { tacworkRoot, server, engine };
}

const envCreated = writeInitialEnv();
const opencodeCreated = ensureOpencodeConfig();
ensurePython();
ensureNodeDependencies();
const tacwork = checkTacwork();

console.log(JSON.stringify({
  status: "ready",
  architecture: process.arch,
  root,
  dataRoot,
  envPath,
  envCreated,
  opencodeCreated,
  tacwork,
  next: "cd desktop && npm run dev",
}, null, 2));
