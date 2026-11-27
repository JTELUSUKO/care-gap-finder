// Runs the real <script> from static/index.html against a tiny stand-in for
// the browser, plays one scenario, and prints what happened as JSON.
// Used by tests/test_review_fixes.py:  node ui_harness.js <index.html> <scenario>
const fs = require("fs");
const vm = require("vm");
const [, , htmlPath, scenario] = process.argv;
const script = fs.readFileSync(htmlPath, "utf8").match(/<script>([\s\S]*)<\/script>/)[1];

// Elements by id. "draft-alert" only exists once the results card holds it.
const els = {};
function el(id) {
  return els[id] || (els[id] = { id, innerHTML: "", textContent: "", className: "",
                                 style: {}, appendChild() {} });
}
let clickListener = null;
const document = {
  getElementById(id) {
    if (id === "draft-alert" && !el("results").innerHTML.includes('id="draft-alert"')) return null;
    return el(id);
  },
  createElement: () => ({ classList: { add() {}, remove() {} } }),
  querySelector: () => ({ value: "mock" }),
  querySelectorAll: () => [],
  addEventListener: (type, fn) => { if (type === "click") clickListener = fn; },
};

const alert1 = { alert_id: "ALERT-1", patient_id: "patient-p001", patient_name: "Maria",
                 created_at: "2026-11-24", gaps: [{ severity: "soon", title: "BP" }] };
let serverPending = [alert1];
let promptAnswer = "";
let decisionReply = { ok: true, status: 200, body: {} };
let decisionGate = null;   // set to a promise to hold a decision in flight
const posts = [];

async function fetch(url, options) {
  const reply = (body, ok = true, status = 200) => ({ ok, status, json: async () => body });
  if (!options) {
    if (url === "/api/patients") return reply({ patients: [{ id: "p001", name: "Maria" }] });
    return reply({ pending: serverPending.slice(), unreadable: [] });
  }
  posts.push({ url, body: options.body });
  if (url.startsWith("/api/run/")) {
    return reply({ patient_id: "patient-p001", patient_name: "Maria", alert: alert1,
                   gaps: [{ title: "BP", severity: "soon" }] });
  }
  if (decisionGate) await decisionGate;
  if (decisionReply.notJson) {
    return { ok: false, status: 500, json: async () => { throw new SyntaxError("bad"); } };
  }
  if (decisionReply.ok) serverPending = [];
  return reply(decisionReply.body, decisionReply.ok, decisionReply.status);
}

const settle = () => new Promise(r => setTimeout(r, 20));
function click(action) {
  const button = { dataset: { action, alertId: "ALERT-1" } };
  clickListener({ target: { closest: () => button } });
}
const decisions = () => posts.filter(p => p.url.startsWith("/api/alerts/"));
const snapshot = () => ({
  decisions: decisions(),
  draft: el("draft-alert").innerHTML, results: el("results").innerHTML,
  pending: el("pending-list").innerHTML,
  status: el("decision-status").textContent, statusClass: el("decision-status").className,
});

const context = { document, fetch, prompt: () => promptAnswer, console, setTimeout };
vm.createContext(context);
vm.runInContext(script, context);

(async () => {
  await settle();
  const out = {};
  if (scenario === "cancel") {
    promptAnswer = null;
    click("reject");
    await settle();
  } else if (scenario === "blank-reason") {
    promptAnswer = "";
    click("reject");
    await settle();
  } else if (scenario === "approve-from-card") {
    await context.runAgent();
    await settle();
    let release;
    decisionGate = new Promise(r => { release = r; });
    click("approve");
    click("approve");            // a double click
    await settle();
    out.during = snapshot();
    release();
    await settle();
  } else if (scenario === "decided-elsewhere") {
    await context.runAgent();
    await settle();
    serverPending = [];
    decisionReply = { ok: false, status: 400, body: { detail: "Alert ALERT-1 is already sent" } };
    click("approve");
    await settle();
  } else if (scenario === "server-crash") {
    decisionReply = { notJson: true };
    click("approve");
    await settle();
  } else {
    throw new Error("unknown scenario " + scenario);
  }
  out.after = snapshot();
  console.log(JSON.stringify(out));
})();
