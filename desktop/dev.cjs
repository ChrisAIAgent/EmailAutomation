/*
 * Development-only Electron entrypoint.
 *
 * It launches Electron against the isolated source stack on 28000-28003.
 * Next dev serves 28001, so source edits hot-refresh without touching the
 * formal application's runtime, ports, or customer data.
 */
const { spawn } = require("node:child_process");
const fs = require("node:fs");
const path = require("node:path");

const root = path.resolve(__dirname, "..");
const electron = path.join(__dirname, "node_modules", "electron", "dist", "electron.exe");
if (!fs.existsSync(electron)) {
  console.error("dev_dependency_missing: run npm ci in the desktop directory on the development machine.");
  process.exit(1);
}

const child = spawn(electron, [__dirname], {
  cwd: root,
  stdio: "inherit",
  env: { ...process.env, EMAIL_AUTOMATION_APP_ROOT: root, EMAIL_AUTOMATION_DESKTOP_DEV: "1" },
});
child.on("exit", (code) => process.exit(code ?? 1));
