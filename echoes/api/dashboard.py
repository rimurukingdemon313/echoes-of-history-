"""The operator dashboard.

Presentation only. It shows state and offers the two controls that can only
*reduce* activity -- pause and stop -- which is why those need no token. The
controls that start or widen activity are not here: they would need a token
typed into a phone, and a dashboard is not where that should happen.

Built mobile-first and served as one self-contained file. There is no build
step, no CDN and no framework, because an operator checking on a failed
production at midnight should not be waiting on a bundle to load, and a
dashboard that breaks when a CDN is unreachable is a dashboard that breaks
exactly when it is needed.
"""

from __future__ import annotations

DASHBOARD_HTML = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Echoes of History</title>
<style>
  :root {
    --bg: #12100e; --panel: #1b1815; --line: #322c26; --ink: #ece5da;
    --dim: #9d9287; --gold: #c4a060; --ok: #6fbf73; --warn: #d9a441;
    --bad: #d2695e; --tap: 38px;
  }
  @media (prefers-color-scheme: light) {
    :root:not([data-theme="dark"]) {
      --bg: #f6f2ec; --panel: #fffdf9; --line: #e0d8cc; --ink: #241f1a;
      --dim: #6d6459; --gold: #8a6a28;
    }
  }
  * { box-sizing: border-box; }
  body {
    margin: 0; background: var(--bg); color: var(--ink);
    font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
    padding: env(safe-area-inset-top) env(safe-area-inset-right)
             env(safe-area-inset-bottom) env(safe-area-inset-left);
  }
  .wrap { max-width: 720px; margin: 0 auto; padding: 16px; }
  h1 { font-size: 19px; margin: 0 0 2px; letter-spacing: .01em; }
  .sub { color: var(--dim); font-size: 13px; margin-bottom: 18px; }
  .card {
    background: var(--panel); border: 1px solid var(--line);
    border-radius: 12px; padding: 14px; margin-bottom: 12px;
  }
  .card h2 {
    font-size: 11px; text-transform: uppercase; letter-spacing: .09em;
    color: var(--dim); margin: 0 0 10px; font-weight: 600;
  }
  .row {
    display: flex; justify-content: space-between; gap: 12px;
    padding: 7px 0; border-bottom: 1px solid var(--line);
  }
  .row:last-child { border-bottom: 0; }
  .row .k { color: var(--dim); flex: 0 0 auto; }
  .row .v { text-align: right; word-break: break-word; }
  .pill {
    display: inline-block; padding: 2px 9px; border-radius: 999px;
    font-size: 12px; font-weight: 600; border: 1px solid transparent;
  }
  .ok   { background: color-mix(in srgb, var(--ok) 18%, transparent);  color: var(--ok);  border-color: var(--ok); }
  .warn { background: color-mix(in srgb, var(--warn) 18%, transparent); color: var(--warn); border-color: var(--warn); }
  .bad  { background: color-mix(in srgb, var(--bad) 18%, transparent);  color: var(--bad);  border-color: var(--bad); }
  .grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 8px; }
  .stat { text-align: center; padding: 10px 4px; background: var(--bg);
          border: 1px solid var(--line); border-radius: 9px; }
  .stat b { display: block; font-size: 21px; font-variant-numeric: tabular-nums; }
  .stat span { font-size: 11px; color: var(--dim); }
  button {
    width: 100%; min-height: var(--tap); border-radius: 9px; font-size: 15px;
    font-weight: 600; border: 1px solid var(--line); background: var(--panel);
    color: var(--ink); cursor: pointer; margin-top: 8px;
  }
  button.danger { border-color: var(--bad); color: var(--bad); }
  button:active { transform: translateY(1px); }
  .scroll { overflow-x: auto; -webkit-overflow-scrolling: touch; }
  table { border-collapse: collapse; width: 100%; font-size: 13px; }
  th, td { text-align: left; padding: 7px 10px 7px 0; white-space: nowrap;
           border-bottom: 1px solid var(--line); }
  /* Truncate the long column rather than letting it push status off-screen.
     A status you have to scroll sideways to read is a status nobody reads. */
  td.topic, th.topic { max-width: 40vw; overflow: hidden;
                       text-overflow: ellipsis; }
  th { color: var(--dim); font-weight: 600; font-size: 11px;
       text-transform: uppercase; letter-spacing: .06em; }
  .note { color: var(--dim); font-size: 12px; margin-top: 10px; }
</style>
</head>
<body>
<div class="wrap">
  <h1>Echoes of History</h1>
  <div class="sub" id="sub">loading&hellip;</div>

  <div class="card">
    <h2>System</h2>
    <div id="system"></div>
  </div>

  <div class="card">
    <h2>Current job</h2>
    <div id="current">nothing running</div>
  </div>

  <div class="card">
    <h2>Totals</h2>
    <div class="grid" id="counters"></div>
  </div>

  <div class="card">
    <h2>Recent productions</h2>
    <div class="scroll"><table id="recent">
      <thead><tr><th>#</th><th class="topic">Topic</th><th>Status</th><th>Stage</th></tr></thead>
      <tbody></tbody>
    </table></div>
  </div>

  <div class="card">
    <h2>Controls</h2>
    <button id="pause">Pause scheduler</button>
    <button id="stop" class="danger">Stop &mdash; no new productions</button>
    <p class="note">
      These only ever reduce activity, so they need no token and work from any
      device. Starting, resuming or widening publication is done through the
      API with a token, never from here.
    </p>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, c =>
  ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));

function row(k, v) { return `<div class="row"><span class="k">${esc(k)}</span><span class="v">${v}</span></div>`; }
function pill(text, cls) { return `<span class="pill ${cls}">${esc(text)}</span>`; }

async function refresh() {
  let s;
  try {
    const r = await fetch("/status", {cache: "no-store"});
    if (!r.ok) throw new Error("status " + r.status);
    s = await r.json();
  } catch (e) {
    $("sub").textContent = "cannot reach the service \\u2014 " + e.message;
    $("system").innerHTML = row("Service", pill("unreachable", "bad"));
    return;
  }

  $("sub").textContent = "updated " + new Date().toLocaleTimeString();
  const dbOk = s.database === "up";
  const mode = s.dry_run ? pill("DRY RUN", "warn") : pill(s.publish_mode, "ok");

  $("system").innerHTML =
      row("Database", dbOk ? pill("up", "ok") : pill("down", "bad"))
    + row("Mode", mode)
    + row("Scheduler", s.scheduler_enabled ? pill("on", "ok") : pill("off", "warn"))
    + row("Timezone", esc(s.timezone))
    + row("Narration", esc(s.providers?.tts))
    + row("Model", esc(s.providers?.llm))
    + row("Storage", esc(s.providers?.storage)
        + (s.providers?.storage_durable ? "" : " " + pill("not durable", "warn")))
    + row("Uploads in doubt", s.uploads_in_doubt
        ? pill(s.uploads_in_doubt, "bad") : pill("0", "ok"));

  $("current").innerHTML = s.current_job
    ? row("Job", "#" + esc(s.current_job.id))
      + row("Topic", esc(s.current_job.topic))
      + row("Stage", pill(s.current_job.stage || "starting", "warn"))
    : '<span class="pill ok">idle</span>';

  const c = s.counters || {};
  $("counters").innerHTML = [
    ["published", c.published], ["succeeded", c.succeeded], ["failed", c.failed],
    ["running", c.running], ["uploads", c.uploads], ["ideas", c.ideas],
  ].map(([k, v]) => `<div class="stat"><b>${v ?? 0}</b><span>${k}</span></div>`).join("");

  const cls = {SUCCEEDED: "ok", PUBLISHED: "ok", FAILED: "bad",
               NEEDS_ATTENTION: "bad", RUNNING: "warn"};
  $("recent").querySelector("tbody").innerHTML = (s.recent || []).map(j =>
    `<tr><td>${esc(j.id)}</td><td class="topic" title="${esc(j.topic)}">${esc(j.topic)}</td>`
    + `<td>${pill(j.status, cls[j.status] || "warn")}</td>`
    + `<td>${esc(j.stage || "\\u2014")}</td></tr>`).join("")
    || '<tr><td colspan="4">no productions yet</td></tr>';
}

async function post(path, button, label) {
  const original = button.textContent;
  button.textContent = "working\\u2026"; button.disabled = true;
  try {
    const r = await fetch(path, {method: "POST"});
    button.textContent = r.ok ? label : "failed \\u2014 " + r.status;
  } catch (e) {
    button.textContent = "failed \\u2014 " + e.message;
  }
  setTimeout(() => { button.textContent = original; button.disabled = false; }, 2500);
  refresh();
}

$("pause").onclick = (e) => post("/scheduler/pause", e.target, "paused");
$("stop").onclick = (e) => {
  if (confirm("Stop the system? No new production will start.")) {
    post("/stop", e.target, "stopped");
  }
};

refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
"""
