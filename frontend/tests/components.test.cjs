const assert = require("node:assert/strict");
const fs = require("node:fs");
const Module = require("node:module");
const path = require("node:path");
const test = require("node:test");
const React = require("react");
const TestRenderer = require("react-test-renderer");
const ts = require("typescript");
globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const api = {};
const iconStub = () => null;
const translation = key => key;
const formatDate = value => value;
let campaignRunComponent;
const originalLoad = Module._load;
Module._load = function (request, parent, isMain) {
  if (request === "@/lib/api") return { api };
  if (request === "@/lib/i18n") return { useLang: () => ({ lang: "en", t: translation, formatDate }) };
  if (request === "lucide-react") return new Proxy({}, { get: () => iconStub });
  if (request === "./CampaignRun" && parent?.filename.endsWith("CampaignAutomation.tsx")) return campaignRunComponent;
  return originalLoad.call(this, request, parent, isMain);
};

function loadTsx(relativePath) {
  const filename = path.join(__dirname, relativePath);
  const source = fs.readFileSync(filename, "utf8");
  const compiled = ts.transpileModule(source, {
    compilerOptions: {
      module: ts.ModuleKind.CommonJS,
      jsx: ts.JsxEmit.ReactJSX,
      esModuleInterop: true,
      target: ts.ScriptTarget.ES2020,
    },
  }).outputText;
  const loadedModule = new Module(filename, module.parent);
  loadedModule.filename = filename;
  loadedModule.paths = Module._nodeModulePaths(path.dirname(filename));
  loadedModule._compile(compiled, filename);
  return loadedModule.exports.default;
}

const CampaignRun = loadTsx("../components/CampaignRun.tsx");
campaignRunComponent = CampaignRun;
const CampaignAutomationPanel = loadTsx("../components/CampaignAutomation.tsx");

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((res, rej) => { resolve = res; reject = rej; });
  return { promise, resolve, reject };
}

function textOf(value) {
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value)) return value.map(textOf).join("");
  if (React.isValidElement(value)) return textOf(value.props.children);
  return "";
}

function button(renderer, label) {
  return renderer.root.findAllByType("button").find(item => textOf(item.props.children).includes(label));
}

async function flush() {
  await new Promise(resolve => setTimeout(resolve, 15));
}

function runFixture(overrides = {}) {
  return {
    id: 101,
    automation_id: 10,
    mode: "semi_auto",
    status: "awaiting_confirmation",
    summary: "Prepared one item",
    error: null,
    send_plan: [{ approval_id: 501, to_email: "fixture@example.test", subject: "Fixture subject", body_text: "Complete fixture body." }],
    ...overrides,
  };
}

function resetApi() {
  for (const key of Object.keys(api)) delete api[key];
  Object.assign(api, {
    agentRunDetail: async () => runFixture(),
    confirmAgentRun: async () => ({ status: "confirmed" }),
    cancelAgentRun: async () => ({ status: "cancelled" }),
    automations: async () => ({ items: [] }),
    campaigns: async () => [{ id: 7, name: "Fixture Campaign", status: "active" }],
    automationDetail: async () => ({ runs: [] }),
    createAutomation: async () => ({ id: 42 }),
    enableAutomation: async () => ({ status: "enabled" }),
    pauseAutomation: async () => ({ status: "paused" }),
    agentRun: async () => ({ run_id: 101, mode: "semi_auto" }),
    updateAutomationSchedule: async () => ({}),
    deleteAutomation: async () => ({}),
  });
}

test("Campaign Run polls past running and shows the final effective mode", { timeout: 8000 }, async () => {
  resetApi();
  const statuses = [runFixture({ status: "running" }), runFixture({ status: "completed", send_plan: [] })];
  let reads = 0;
  api.agentRunDetail = async () => statuses[Math.min(reads++, statuses.length - 1)];
  let renderer;
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(CampaignRun, { runId: 101, onChanged() {} }));
    await flush();
  });
  assert.match(renderer.root.findByType("h4").children.join(""), /semi_auto/);
  assert.match(renderer.root.findByType("h4").children.join(""), /running/);

  await TestRenderer.act(async () => { await new Promise(resolve => setTimeout(resolve, 2100)); });
  assert.equal(reads, 2);
  assert.match(renderer.root.findByType("h4").children.join(""), /completed/);
  renderer.unmount();
});

test("confirmation shows the complete frozen plan and ignores duplicate clicks", async () => {
  resetApi();
  const request = deferred();
  let confirms = 0;
  api.confirmAgentRun = () => { confirms += 1; return request.promise; };
  let renderer;
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(CampaignRun, { runId: 101, onChanged() {} }));
    await flush();
  });
  assert.match(renderer.root.findAllByType("article")[0].children.map(child => child.children?.join("") || "").join(" "), /Complete fixture body\./);
  const confirmButton = button(renderer, "Confirm & execute");
  assert.ok(confirmButton);
  let first;
  await TestRenderer.act(async () => {
    first = confirmButton.props.onClick();
    confirmButton.props.onClick();
    await flush();
  });
  assert.equal(confirms, 1);
  request.resolve({ status: "confirmed" });
  await TestRenderer.act(async () => { await first; await flush(); });
  renderer.unmount();
});

test("failed Run read disables both decisions until a successful retry", async () => {
  resetApi();
  const retryRead = deferred();
  let reads = 0;
  api.agentRunDetail = async id => {
    reads += 1;
    if (id === 101) return runFixture();
    if (reads === 2) throw new Error("temporary read failure");
    return retryRead.promise;
  };
  let renderer;
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(CampaignRun, { runId: 101, onChanged() {} }));
    await flush();
  });
  await TestRenderer.act(async () => {
    renderer.update(React.createElement(CampaignRun, { runId: 102, onChanged() {} }));
    await flush();
  });
  assert.ok(button(renderer, "Retry status"));
  assert.equal(button(renderer, "Confirm & execute").props.disabled, true);

  await TestRenderer.act(async () => { button(renderer, "Retry status").props.onClick(); await flush(); });
  assert.equal(button(renderer, "Retry status").props.disabled, true);
  retryRead.resolve(runFixture());
  await TestRenderer.act(async () => { await flush(); });
  assert.equal(button(renderer, "Confirm & execute").props.disabled, false);
  assert.equal(button(renderer, "Cancel run").props.disabled, false);
  renderer.unmount();
});

test("reopening Automation recovers the same awaiting Run from its persisted detail", async () => {
  resetApi();
  api.automations = async () => ({ items: [{ id: 10, campaign_id: 7, scope: "campaign", status: "enabled", plan: {} }] });
  api.automationDetail = async () => ({ runs: [{ id: 101, status: "awaiting_confirmation", execution_mode: "semi_auto" }] });
  let detailsReads = 0;
  api.agentRunDetail = async id => { detailsReads += 1; assert.equal(id, 101); return runFixture(); };
  let renderer;
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(CampaignAutomationPanel, { onChanged() {} }));
    await flush();
  });
  assert.equal(detailsReads, 1);
  assert.match(renderer.root.findAllByType("h4").map(item => item.children.join("")).join(" "), /Run #101/);
  assert.match(renderer.root.findByType("pre").children.join(""), /Complete fixture body\./);
  renderer.unmount();
});

test("execution mode selection remains independent for each Automation", async () => {
  resetApi();
  api.automations = async () => ({ items: [
    { id: 10, campaign_id: 7, scope: "campaign", status: "enabled", execution_mode: "full_auto", plan: {} },
    { id: 11, campaign_id: 7, scope: "campaign", status: "enabled", execution_mode: "full_auto", plan: {} },
  ] });
  api.automationDetail = async () => ({ runs: [] });
  let renderer;
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(CampaignAutomationPanel, { onChanged() {} }));
    await flush();
  });
  const modes = () => renderer.root.findAllByType("select").filter(item => item.props["aria-label"] === "Mode for this run");
  assert.deepEqual(modes().map(item => item.props.value), ["full_auto", "full_auto"]);
  await TestRenderer.act(async () => { modes()[0].props.onChange({ target: { value: "semi_auto" } }); });
  assert.deepEqual(modes().map(item => item.props.value), ["semi_auto", "full_auto"]);
  renderer.unmount();
});

test("enable retry reuses the saved Automation ID and never creates twice", async () => {
  resetApi();
  let creates = 0;
  const enabledIds = [];
  api.createAutomation = async () => { creates += 1; return { id: 42 }; };
  api.enableAutomation = async id => {
    enabledIds.push(id);
    if (enabledIds.length === 1) throw new Error("temporary enable failure");
    return { status: "enabled" };
  };
  let renderer;
  await TestRenderer.act(async () => {
    renderer = TestRenderer.create(React.createElement(CampaignAutomationPanel, { onChanged() {} }));
    await flush();
  });
  await TestRenderer.act(async () => { button(renderer, "Create Campaign Automation").props.onClick(); await flush(); });
  await TestRenderer.act(async () => { button(renderer, "auto_save_enable").props.onClick(); await flush(); });
  assert.equal(creates, 1);
  assert.deepEqual(enabledIds, [42]);
  assert.ok(button(renderer, "Retry enabling saved automation"));

  await TestRenderer.act(async () => { button(renderer, "Retry enabling saved automation").props.onClick(); await flush(); });
  assert.equal(creates, 1);
  assert.deepEqual(enabledIds, [42, 42]);
  renderer.unmount();
});
