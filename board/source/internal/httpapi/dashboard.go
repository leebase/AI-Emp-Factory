package httpapi

const dashboardHTML = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <title>agent-board</title>
  <script>window.__AUTH_ENABLED__ = %%AUTH_ENABLED%%;</script>
  <style>
    :root {
      color-scheme: light;
      --bg: #f7f8fb;
      --panel: #ffffff;
      --ink: #1f2633;
      --muted: #667085;
      --line: #d8dee9;
      --ready: #0f766e;
      --running: #2563eb;
      --review: #7c3aed;
      --blocked: #b45309;
      --failed: #b91c1c;
      --done: #15803d;
      --shadow: 0 1px 3px rgba(31, 38, 51, 0.1);
    }

    * { box-sizing: border-box; }

    body {
      margin: 0;
      min-width: 320px;
      background: var(--bg);
      color: var(--ink);
      font: 14px/1.45 system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      letter-spacing: 0;
    }

    header {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      padding: 18px clamp(16px, 4vw, 40px);
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }

    h1, h2, p { margin: 0; }

    h1 {
      font-size: 22px;
      font-weight: 700;
    }

    h2 {
      font-size: 15px;
      font-weight: 700;
    }

    main {
      width: min(1280px, 100%);
      margin: 0 auto;
      padding: 20px clamp(16px, 4vw, 40px) 36px;
    }

    button, input, select {
      min-height: 36px;
      border: 1px solid var(--line);
      border-radius: 6px;
      background: var(--panel);
      color: var(--ink);
      font: inherit;
    }

    button {
      padding: 0 12px;
      cursor: pointer;
      font-weight: 650;
    }

    button:hover { border-color: #98a2b3; }

    input, select {
      width: min(280px, 100%);
      padding: 0 10px;
    }

    input.small {
      width: 96px;
    }

    .toolbar {
      display: flex;
      align-items: center;
      justify-content: flex-end;
      gap: 10px;
      flex-wrap: wrap;
    }

    .meta {
      color: var(--muted);
      font-size: 13px;
      white-space: nowrap;
    }

    .grid {
      display: grid;
      gap: 16px;
    }

    .stats {
      grid-template-columns: repeat(8, minmax(112px, 1fr));
      margin-bottom: 16px;
    }

    .stat, .panel {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      box-shadow: var(--shadow);
    }

    .stat {
      min-height: 88px;
      padding: 12px;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      border-top: 4px solid #8a94a6;
      text-align: left;
    }

    .stat strong {
      font-size: 28px;
      line-height: 1;
    }

    .stat span, .empty, .subtle {
      color: var(--muted);
      font-size: 12px;
    }

    button.stat {
      width: 100%;
      font-weight: 400;
    }

    button.stat.active {
      border-color: #475467;
      box-shadow: 0 0 0 2px rgba(71, 84, 103, 0.16), var(--shadow);
    }

    .ready { border-top-color: var(--ready); }
    .running { border-top-color: var(--running); }
    .review { border-top-color: var(--review); }
    .blocked { border-top-color: var(--blocked); }
    .failed { border-top-color: var(--failed); }
    .done { border-top-color: var(--done); }

    .columns {
      grid-template-columns: minmax(0, 1.2fr) minmax(320px, 0.8fr);
      align-items: start;
    }

    .panel {
      min-width: 0;
      overflow: hidden;
    }

    .panel-head {
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 12px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
    }

    .list {
      display: grid;
      gap: 0;
    }

    .row {
      display: grid;
      grid-template-columns: minmax(0, 1fr) auto;
      gap: 12px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      min-height: 58px;
    }

    .row:last-child { border-bottom: 0; }

    .row-actions {
      display: flex;
      gap: 8px;
      flex-wrap: wrap;
      margin-top: 10px;
    }

    .row-actions button {
      min-height: 30px;
      padding: 0 9px;
      font-size: 12px;
    }

    .row-actions a {
      min-height: 30px;
      padding: 5px 9px;
      border: 1px solid var(--line);
      border-radius: 6px;
      color: var(--ink);
      text-decoration: none;
      font-size: 12px;
      font-weight: 650;
    }

    .filters {
      display: grid;
      grid-template-columns: minmax(180px, 1.4fr) repeat(4, minmax(120px, 1fr));
      gap: 10px;
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      background: #fbfcfe;
    }

    button.danger {
      border-color: #fecaca;
      color: #991b1b;
    }

    .title {
      overflow-wrap: anywhere;
      font-weight: 650;
    }

    .pill {
      align-self: start;
      min-width: 70px;
      padding: 3px 8px;
      border-radius: 999px;
      background: #eef2f7;
      color: #344054;
      text-align: center;
      font-size: 12px;
      font-weight: 700;
      white-space: nowrap;
    }

    .pill.ready { background: #ccfbf1; color: #115e59; border: 0; }
    .pill.claimed { background: #dbeafe; color: #1d4ed8; }
    .pill.review { background: #ede9fe; color: #6d28d9; }
    .pill.blocked { background: #fef3c7; color: #92400e; }
    .pill.failed { background: #fee2e2; color: #991b1b; }
    .pill.done { background: #dcfce7; color: #166534; }

    .split {
      grid-template-columns: repeat(2, minmax(0, 1fr));
    }

    .entity {
      padding: 12px 14px;
      border-bottom: 1px solid var(--line);
      min-height: 56px;
    }

    .entity:last-child { border-bottom: 0; }

    .entity strong { display: block; overflow-wrap: anywhere; }

    .notice {
      display: none;
      margin-bottom: 16px;
      padding: 12px 14px;
      border: 1px solid #fbbf24;
      border-radius: 8px;
      background: #fffbeb;
      color: #78350f;
    }

    .empty { padding: 16px 14px; }

    .detail {
      display: none;
      max-height: 280px;
      margin: 0;
      padding: 12px 14px;
      overflow: auto;
      border-top: 1px solid var(--line);
      background: #111827;
      color: #f9fafb;
      font: 12px/1.45 ui-monospace, SFMono-Regular, Menlo, Consolas, monospace;
      white-space: pre-wrap;
    }

    @media (max-width: 1080px) {
      .stats { grid-template-columns: repeat(4, minmax(112px, 1fr)); }
      .columns { grid-template-columns: 1fr; }
    }

    @media (max-width: 640px) {
      header { align-items: flex-start; flex-direction: column; }
      .toolbar { width: 100%; justify-content: flex-start; }
      .stats, .split, .filters { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .row { grid-template-columns: 1fr; }
      .pill { justify-self: start; }
      input, input.small, select { width: 100%; }
    }
%%COCKPIT_CSS%%
  </style>
</head>
<body class="mc-staff-mode">
  <header>
    <div>
      <h1>agent-board</h1>
      <p class="meta" id="generated">Snapshot pending</p>
    </div>
    <div class="toolbar" id="session-toolbar">
      <span class="meta" id="session-state">Checking sign-in status…</span>
      <input class="small" id="refresh-seconds" name="refresh-seconds" type="number" min="0" step="1" value="10">
      <button type="button" id="refresh">Refresh</button>
      <a id="login-link" href="/auth/login?return_to=/dashboard" style="display:none">Sign in</a>
      <button type="button" id="logout-button" style="display:none">Sign out</button>
    </div>
  </header>
  <nav class="mc-viewnav" aria-label="Dashboard views">
    <button type="button" id="view-cockpit" aria-pressed="false">Mission Control</button>
    <button type="button" id="view-staff" aria-pressed="true">Staff</button>
    <button type="button" id="view-advanced" aria-pressed="false">Advanced board view</button>
  </nav>
  <main>
    <div class="notice" id="notice"></div>
    <section id="cockpit-view" aria-label="Mission Control employee cockpit" hidden>
      <p class="mc-currency" id="cockpit-currency" data-currency="loading" role="status">Loading estate state...</p>
      <div class="mc-viewnav" style="padding-left:0">
        <button type="button" id="cockpit-refresh">Refresh employee state</button>
        <a id="cockpit-signin" href="/auth/login?return_to=/dashboard" hidden>Sign in</a>
      </div>
      <section class="mc-section" aria-labelledby="needs-lee-title">
        <div class="mc-section-head">
          <h2 id="needs-lee-title">Needs Lee</h2>
          <span class="mc-count" id="needs-lee-count">0 decisions</span>
        </div>
        <div id="needs-lee"></div>
      </section>
      <section class="mc-section" aria-labelledby="burn-title">
        <div class="mc-section-head">
          <h2 id="burn-title">Burning</h2>
          <span class="mc-count" id="burn-count">0 employees</span>
        </div>
        <div id="burn-loops"></div>
      </section>
      <section class="mc-section" aria-labelledby="roster-title">
        <div class="mc-section-head">
          <h2 id="roster-title">Employees</h2>
          <span class="mc-count" id="roster-count">0 shown</span>
        </div>
        <div class="mc-filters" id="roster-filters" role="group" aria-label="Filter employees by placement">
          <button type="button" data-group="all" aria-pressed="true">All</button>
          <button type="button" data-group="running" aria-pressed="false">Running</button>
          <button type="button" data-group="scheduled" aria-pressed="false">Scheduled</button>
          <button type="button" data-group="on_the_bench" aria-pressed="false">On the Bench</button>
          <button type="button" data-group="paused" aria-pressed="false">Paused / Unavailable</button>
          <button type="button" data-group="not_commissioned" aria-pressed="false">Not commissioned</button>
          <button type="button" data-group="unknown" aria-pressed="false">Placement unknown</button>
          <label class="mc-count" for="roster-search">Find employee</label>
          <input id="roster-search" type="search" placeholder="Employee id">
        </div>
        <div id="roster"></div>
      </section>
      <section class="mc-section" aria-labelledby="challenge-title">
        <div class="mc-section-head">
          <h2 id="challenge-title">Challenges</h2>
          <span class="mc-count" id="challenge-count">0 findings</span>
        </div>
        <div class="mc-filters" id="challenge-filters" role="group" aria-label="Filter challenge findings by recorded category"></div>
        <div id="challenges"></div>
      </section>
      <section class="mc-section" aria-labelledby="watchlist-title">
        <div class="mc-section-head">
          <h2 id="watchlist-title">Watchlist</h2>
        </div>
        <div id="watchlist"></div>
      </section>
      <section class="mc-section" aria-labelledby="capacity-title">
        <div class="mc-section-head">
          <h2 id="capacity-title">Capacity</h2>
        </div>
        <div id="capacity"></div>
      </section>
    </section>
    <div class="mc-detail-backdrop" id="employee-detail" hidden></div>
    <section id="staff-view" aria-label="Staff: employees as recorded on disk">
      <div class="mc-staff-top">
        <div class="mc-staff-heading">
          <h2 id="staff-title">Staff</h2>
          <button type="button" id="staff-refresh">Refresh</button>
        </div>
        <p id="staff-status" class="mc-staff-status" role="status" hidden></p>
        <details class="mc-staff-about">
          <summary>About this view</summary>
          <div id="staff-about-text"></div>
          <p class="mc-staff-about-meta" id="staff-about-meta"></p>
          <div class="mc-staff-filters" id="staff-filters" role="group" aria-label="Filter recorded employee attention"></div>
          <p class="mc-staff-about-meta" id="staff-count"></p>
          <div class="mc-staff-modes">
            <button type="button" data-view="cockpit">Mission Control</button>
            <button type="button" data-view="advanced">Advanced board view</button>
          </div>
        </details>
      </div>
      <div id="staff-roster" aria-label="Employees"></div>
      <div id="staff-person" hidden></div>
    </section>
    <div id="advanced-view" hidden>
    <div class="notice" id="snapshot-notice"></div>
    <section class="grid stats" id="stats"></section>
    <section class="panel" id="kpi-detail-panel" style="display: none; margin-bottom: 16px;">
      <div class="panel-head">
        <h2 id="kpi-detail-title">KPI Detail</h2>
        <button type="button" id="clear-kpi">Clear</button>
      </div>
      <div class="list" id="kpi-detail"></div>
    </section>
    <section class="panel" style="margin-bottom: 16px;">
      <div class="panel-head">
        <h2>Projects</h2>
        <span class="subtle" id="project-count">0 projects</span>
      </div>
      <div class="notice" id="projects-notice"></div>
      <div class="filters">
        <input id="project-search" type="search" placeholder="Search projects">
        <select id="project-status-filter">
          <option value="">All project states</option>
          <option value="active">Active</option>
          <option value="completed">Completed</option>
        </select>
        <select id="project-task-filter">
          <option value="">All task states</option>
        </select>
        <select id="project-agent-filter">
          <option value="">All agents/machines</option>
        </select>
        <select id="project-attention-filter">
          <option value="">All attention states</option>
          <option value="attention">Needs attention</option>
          <option value="blocked">Blocked</option>
          <option value="recent">Recently updated</option>
        </select>
      </div>
      <div class="list" id="projects"></div>
      <pre class="detail" id="project-detail"></pre>
    </section>
    <section class="panel" style="margin-bottom: 16px;">
      <div class="panel-head">
        <h2>Auto-orch Reports</h2>
        <span class="subtle" id="auto-orch-count">0 reports</span>
      </div>
      <div class="notice" id="auto-orch-notice"></div>
      <div class="list" id="auto-orch-reports"></div>
    </section>
    <section class="grid columns">
      <div class="grid">
        <section class="panel">
          <div class="panel-head">
            <h2>Human Attention</h2>
            <span class="subtle" id="attention-count">0 tasks</span>
          </div>
          <div class="list" id="attention"></div>
        </section>
        <section class="panel">
          <div class="panel-head">
            <h2>Active Leases</h2>
            <span class="subtle" id="lease-count">0 active</span>
          </div>
          <div class="list" id="leases"></div>
        </section>
      </div>
      <div class="grid">
        <section class="panel">
          <div class="panel-head">
            <h2>Machines</h2>
            <span class="subtle" id="machine-count">0 online</span>
          </div>
          <div id="machines"></div>
        </section>
        <section class="panel">
          <div class="panel-head">
            <h2>Agents</h2>
            <span class="subtle" id="agent-count">0 online</span>
          </div>
          <div id="agents"></div>
        </section>
        <section class="panel">
          <div class="panel-head">
            <h2>Recent Events</h2>
            <span class="subtle" id="event-count">0 events</span>
          </div>
          <div class="list" id="events"></div>
        </section>
      </div>
    </section>
    </div>
  </main>
  <script>
    const refreshSecondsInput = document.querySelector("#refresh-seconds");
    const sessionState = document.querySelector("#session-state");
    const loginLink = document.querySelector("#login-link");
    const logoutButton = document.querySelector("#logout-button");
    const notice = document.querySelector("#notice");
    let refreshTimer = 0;
    let projectStates = [];
    let latestSnapshot = null;
    let activeKPI = null;
    // Session identity is server-truth from the HttpOnly cookie. The browser
    // never holds, stores, or sends a credential of its own.
    let session = null;

    // Only non-secret display preferences are persisted in the browser. No
    // token, password, actor identity, or session value is ever stored here.
    refreshSecondsInput.value = localStorage.getItem("agentBoardRefreshSeconds") || "10";

    // A Sprint0 build of this origin persisted a machine token under this key.
    // Erase that stale credential on startup. It is never read or reused: this
    // dashboard authenticates solely through the HttpOnly session cookie.
    localStorage.removeItem("agentBoardToken");

    logoutButton.addEventListener("click", async () => {
      // Sign-out is a session mutation: same-origin, cookie-only, no body.
      try {
        await fetch("/auth/logout", { method: "POST", credentials: "same-origin" });
      } catch (_) {}
      window.location.assign("/auth/login?return_to=/dashboard");
    });

    document.querySelector("#refresh").addEventListener("click", reload);
    refreshSecondsInput.addEventListener("change", () => {
      localStorage.setItem("agentBoardRefreshSeconds", refreshSecondsInput.value || "10");
      configureAutoRefresh();
      reload();
    });
    for (const selector of ["#project-search", "#project-status-filter", "#project-task-filter", "#project-agent-filter", "#project-attention-filter"]) {
      document.querySelector(selector).addEventListener("input", renderProjectRows);
      document.querySelector(selector).addEventListener("change", renderProjectRows);
    }

    // Every request rides the same-origin HttpOnly session cookie. There is no
    // Authorization header and no client-held credential anywhere below.
    function sessionFetch(path, options) {
      return fetch(path, Object.assign({ credentials: "same-origin" }, options || {}));
    }

    // One honest message per authorization outcome, never a generic failure.
    function authMessage(status, label) {
      if (status === 401) {
        return session
          ? "Your session expired or was signed out. Sign in again to see " + label.toLowerCase() + "."
          : "You are not signed in. Sign in to see " + label.toLowerCase() + ".";
      }
      if (status === 403) {
        const who = session && session.actor_id ? session.actor_id : "this identity";
        return "Signed in as " + who + ", but this identity is not permitted to access " + label.toLowerCase() + ".";
      }
      return label + " failed with HTTP " + status + ".";
    }

    async function loadSession() {
      try {
        const response = await sessionFetch("/api/session");
        if (response.status === 401) {
          setSignedOut("You are not signed in.");
          return;
        }
        if (!response.ok) {
          setSignedOut("Sign-in status unavailable (HTTP " + response.status + ").");
          return;
        }
        session = await response.json();
        const role = session.role || session.actor_type || "unknown role";
        sessionState.textContent = "Signed in as " + (session.display_name || session.actor_id) + " (" + role + ")";
        loginLink.style.display = "none";
        logoutButton.style.display = "";
      } catch (error) {
        setSignedOut("Sign-in status request failed: " + error.message);
      }
    }

    function setSignedOut(message) {
      session = null;
      sessionState.textContent = message;
      loginLink.style.display = window.__AUTH_ENABLED__ ? "" : "none";
      logoutButton.style.display = "none";
    }

    async function reload() {
      await loadSession();
      await loadCockpit(false);
      await loadSnapshot();
    }

    async function loadSnapshot() {
      notice.style.display = "none";
      await Promise.allSettled([
        loadSource("/snapshot", "snapshot-notice", "Snapshot", renderSnapshot),
        loadSource("/projects/state", "projects-notice", "Project state", renderProjectPortfolio),
        loadSource("/api/auto-orch/cycle-reports?limit=25", "auto-orch-notice", "Auto-orch reports", renderAutoOrchReports)
      ]);
    }

    async function loadSource(path, noticeID, label, renderSource) {
      const sourceNotice = document.querySelector("#" + noticeID);
      sourceNotice.style.display = "none";
      try {
        const response = await sessionFetch(path);
        if (response.status === 401 || response.status === 403) {
          throw new Error(authMessage(response.status, label));
        }
        if (!response.ok) {
          throw new Error("HTTP " + response.status + ".");
        }
        renderSource(await response.json());
      } catch (error) {
        sourceNotice.textContent = label + " failed: " + error.message;
        sourceNotice.style.display = "block";
      }
    }

    async function actOnTask(taskID, action, confirmText) {
      if (confirmText && !confirm(confirmText)) return;
      notice.style.display = "none";
      try {
        // The acting identity is derived server-side from the session. The
        // browser cannot name an actor.
        const response = await sessionFetch("/tasks/" + encodeURIComponent(taskID) + "/" + action, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ reason: "dashboard" })
        });
        if (response.status === 401 || response.status === 403) {
          showNotice(authMessage(response.status, "this action"));
          return;
        }
        if (!response.ok) {
          let detail = "HTTP " + response.status;
          try {
            const body = await response.json();
            if (body.error) detail = body.error;
          } catch (_) {}
          showNotice("Action failed: " + detail);
          return;
        }
        await loadSnapshot();
      } catch (error) {
        showNotice("Action request failed: " + error.message);
      }
    }

    async function inspectProjectDetail(path, label) {
      notice.style.display = "none";
      const detail = document.querySelector("#project-detail");
      try {
        const response = await sessionFetch(path);
        if (response.status === 401 || response.status === 403) {
          showNotice(authMessage(response.status, label));
          return;
        }
        if (!response.ok) {
          showNotice(label + " failed with HTTP " + response.status + ".");
          return;
        }
        detail.textContent = label + "\n" + JSON.stringify(await response.json(), null, 2);
        detail.style.display = "block";
      } catch (error) {
        showNotice(label + " request failed: " + error.message);
      }
    }

    function showNotice(message) {
      notice.textContent = message;
      notice.style.display = "block";
    }

    function renderSnapshot(snapshot) {
      latestSnapshot = snapshot;
      const status = snapshot.status || {};
      document.querySelector("#generated").textContent = "Generated " + formatTime(snapshot.generated_at);
      renderStats(status, snapshot);
      renderTasks("#attention", snapshot.human_attention || [], "No review, blocked, or failed tasks.", true);
      document.querySelector("#attention-count").textContent = plural((snapshot.human_attention || []).length, "task");
      renderLeases(snapshot.active_leases || []);
      renderEntities("#machines", snapshot.machines || [], "No machines registered.");
      renderEntities("#agents", snapshot.agents || [], "No agents registered.");
      renderEvents(snapshot.recent_events || []);
      document.querySelector("#machine-count").textContent = number(status.online_machines) + " online";
      document.querySelector("#agent-count").textContent = number(status.online_agents) + " online";
      refreshKPIDetail();
    }

    function renderStats(status, snapshot) {
      const stats = [
        { label: "Ready", value: status.ready_tasks, className: "ready", kind: "tasks", status: "ready" },
        { label: "Running", value: status.claimed_tasks, className: "running", kind: "tasks", status: "claimed" },
        { label: "Review", value: status.review_tasks, className: "review", kind: "tasks", status: "review" },
        { label: "Blocked", value: status.blocked_tasks, className: "blocked", kind: "tasks", status: "blocked" },
        { label: "Failed", value: status.failed_tasks, className: "failed", kind: "tasks", status: "failed" },
        { label: "Waiting", value: status.waiting_tasks, className: "", kind: "tasks", status: "waiting" },
        { label: "Stale Leases", value: (snapshot.stale_leases || []).length, className: "failed", kind: "stale", status: "stale" },
        { label: "Done", value: status.done_tasks, className: "done", kind: "tasks", status: "done" },
      ];
      document.querySelector("#stats").innerHTML = stats.map((item) => (
        '<button type="button" class="stat ' + escapeHTML(item.className) + (isActiveKPI(item) ? " active" : "") + '" data-kpi-kind="' + escapeAttr(item.kind) + '" data-kpi-status="' + escapeAttr(item.status) + '" data-kpi-label="' + escapeAttr(item.label) + '"><span>' + escapeHTML(item.label) + '</span><strong>' + number(item.value) + '</strong></button>'
      )).join("");
    }

    function isActiveKPI(item) {
      return activeKPI && activeKPI.kind === item.kind && activeKPI.status === item.status;
    }

    async function refreshKPIDetail() {
      if (!activeKPI) return;
      document.querySelector("#kpi-detail-panel").style.display = "block";
      document.querySelector("#kpi-detail-title").textContent = activeKPI.label;
      if (activeKPI.kind === "stale") {
        renderKPILeases(latestSnapshot ? (latestSnapshot.stale_leases || []) : []);
        return;
      }
      await loadKPITasks(activeKPI.status, activeKPI.label);
    }

    async function loadKPITasks(status, label) {
      const target = document.querySelector("#kpi-detail");
      try {
        const response = await sessionFetch("/tasks?status=" + encodeURIComponent(status));
        if (response.status === 401 || response.status === 403) {
          showNotice(authMessage(response.status, label + " tasks"));
          return;
        }
        if (!response.ok) {
          showNotice(label + " tasks failed with HTTP " + response.status + ".");
          return;
        }
        const tasks = await response.json();
        renderTasks("#kpi-detail", tasks || [], "No " + label.toLowerCase() + " tasks.", true);
      } catch (error) {
        target.innerHTML = '<div class="empty">' + escapeHTML(label + " tasks request failed: " + error.message) + '</div>';
      }
    }

    function renderKPILeases(leases) {
      const target = document.querySelector("#kpi-detail");
      if (!leases.length) {
        target.innerHTML = '<div class="empty">No stale leases.</div>';
        return;
      }
      target.innerHTML = leases.map((item) => {
        const lease = item.lease || {};
        const task = item.task || {};
        return '<div class="row"><div><div class="title">#' + number(task.display_number) + ' ' + escapeHTML(task.title || lease.task_id) + '</div>' +
          '<div class="subtle">' + escapeHTML(lease.agent_id || "") + ' on ' + escapeHTML(lease.machine_id || "") + '</div></div>' +
          '<span class="pill failed">stale</span></div>';
      }).join("");
    }

    function renderAutoOrchReports(reports) {
      document.querySelector("#auto-orch-count").textContent = plural(reports.length, "report");
      const target = document.querySelector("#auto-orch-reports");
      if (!reports.length) {
        target.innerHTML = '<div class="empty">No auto-orch cycle reports published.</div>';
        return;
      }
      target.innerHTML = reports.map((report) => {
        const payload = parseReportPayload(report.payload_json);
        const mission = payload.mission || {};
        const cycle = payload.cycle || {};
        const run = payload.run || {};
        const selected = payload.selected_item || {};
        const links = payload.links || {};
        const missionName = report.mission_name || mission.name || "unknown";
        const cycleID = report.cycle_id || cycle.id || "unknown";
        const outcome = cycle.outcome || "unknown";
        const runStatus = run.status || "none";
        const passed = run.passed === true ? "passed" : (run.passed === false ? "not passed" : "unknown");
        const summary = [
          "cycle " + cycleID,
          "outcome " + outcome,
          "run " + runStatus,
          passed,
          "updated " + formatTime(report.updated_at)
        ].join(" · ");
        const selectedTitle = selected.title || selected.id || "No selected item recorded.";
        return '<div class="row"><div><div class="title">' + escapeHTML(mission.label || missionName) + '</div>' +
          '<div class="subtle">' + escapeHTML(summary) + '</div>' +
          '<div class="subtle">Selected: ' + escapeHTML(selectedTitle) + '</div>' +
          reportLinks(links, run) + '</div>' +
          '<span class="pill ' + reportPillClass(outcome, run.passed) + '">' + escapeHTML(outcome) + '</span></div>';
      }).join("");
    }

    function parseReportPayload(raw) {
      if (!raw) return {};
      try {
        return JSON.parse(raw);
      } catch (_) {
        return {};
      }
    }

    function reportPillClass(outcome, passed) {
      if (passed === true || outcome === "success") return "done";
      if (outcome === "failed" || passed === false) return "failed";
      if (String(outcome || "").includes("deferred")) return "blocked";
      return "review";
    }

    function reportLinks(links, run) {
      const values = [
        ["Dashboard", run.dashboard_path || links.dashboard_path],
        ["Report", links.cycle_report_path],
        ["Progress", links.progress_path],
        ["Evidence", links.evidence_manifest_path]
      ].filter(([, value]) => value);
      if (!values.length) return "";
      return '<div class="row-actions">' + values.map(([label, value]) => (
        '<button type="button" data-copy="' + escapeAttr(value) + '">' + escapeHTML(label) + '</button>'
      )).join("") + '</div>';
    }

    function renderProjectPortfolio(states) {
      projectStates = states;
      updateProjectFilterOptions();
      renderProjectRows();
    }

    function updateProjectFilterOptions() {
      const selectedTask = document.querySelector("#project-task-filter").value;
      const selectedAgent = document.querySelector("#project-agent-filter").value;
      const taskStatuses = new Set();
      const agents = new Set();
      for (const state of projectStates) {
        for (const status of Object.keys(state.task_counts_by_status || {})) taskStatuses.add(status);
        if (state.agent) agents.add(state.agent);
        if (state.host) agents.add(state.host);
      }
      setOptions("#project-task-filter", "All task states", Array.from(taskStatuses).sort(), selectedTask);
      setOptions("#project-agent-filter", "All agents/machines", Array.from(agents).sort(), selectedAgent);
    }

    function setOptions(selector, emptyLabel, values, selected) {
      const target = document.querySelector(selector);
      target.innerHTML = '<option value="">' + escapeHTML(emptyLabel) + '</option>' + values.map((value) => (
        '<option value="' + escapeAttr(value) + '">' + escapeHTML(value) + '</option>'
      )).join("");
      target.value = values.includes(selected) ? selected : "";
    }

    function renderProjectRows() {
      const target = document.querySelector("#projects");
      const filtered = filteredProjects();
      document.querySelector("#project-count").textContent = plural(filtered.length, "project");
      if (!filtered.length) {
        target.innerHTML = '<div class="empty">No projects match the current filters.</div>';
        return;
      }
      target.innerHTML = filtered.map((state) => {
        const task = state.current_task || {};
        const counts = Object.entries(state.task_counts_by_status || {})
          .sort(([a], [b]) => a.localeCompare(b))
          .map(([status, count]) => status + ":" + count)
          .join(" ");
        const detail = [
          state.mode || "",
          state.agent ? "agent " + state.agent : "",
          state.host ? "host " + state.host : "",
          state.needs_human_attention ? "needs attention" : "",
          state.blocked ? "blocked" : "",
          "updated " + formatTime(state.last_activity_at)
        ].filter(Boolean).join(" · ");
        return '<div class="row"><div><div class="title">' + escapeHTML(state.project_id) + '</div>' +
          '<div class="subtle">' + escapeHTML(detail) + '</div>' +
          '<div class="subtle">Current: #' + number(task.display_number) + ' ' + escapeHTML(task.title || state.next_action || "none") + '</div>' +
          '<div class="subtle">' + escapeHTML(counts) + '</div>' +
          projectLinks(task) + '</div>' +
          '<span class="pill ' + (state.project_status === "completed" ? "done" : "claimed") + '">' + escapeHTML(state.project_status) + '</span></div>';
      }).join("");
    }

    function filteredProjects() {
      const query = document.querySelector("#project-search").value.trim().toLowerCase();
      const projectStatus = document.querySelector("#project-status-filter").value;
      const taskStatus = document.querySelector("#project-task-filter").value;
      const agent = document.querySelector("#project-agent-filter").value;
      const attention = document.querySelector("#project-attention-filter").value;
      const recentCutoff = Date.now() - 7 * 24 * 60 * 60 * 1000;
      return projectStates.filter((state) => {
        const task = state.current_task || {};
        const haystack = [state.project_id, state.next_action, task.title, task.status, state.agent, state.host].join(" ").toLowerCase();
        if (query && !haystack.includes(query)) return false;
        if (projectStatus && state.project_status !== projectStatus) return false;
        if (taskStatus && !(state.task_counts_by_status || {})[taskStatus]) return false;
        if (agent && state.agent !== agent && state.host !== agent) return false;
        if (attention === "attention" && !state.needs_human_attention) return false;
        if (attention === "blocked" && !state.blocked) return false;
        if (attention === "recent") {
          const updated = new Date(state.last_activity_at).getTime();
          if (Number.isNaN(updated) || updated < recentCutoff) return false;
        }
        return true;
      });
    }

    function projectLinks(task) {
      if (!task.id) return "";
      const id = escapeAttr(task.id);
      return '<div class="row-actions">' +
        '<button type="button" data-inspect="/tasks/' + id + '" data-label="Task">Task</button>' +
        '<button type="button" data-inspect="/tasks/' + id + '/events" data-label="Events">Events</button>' +
        '<button type="button" data-inspect="/tasks/' + id + '/artifacts" data-label="Artifacts">Artifacts</button>' +
        '</div>';
    }

    function renderTasks(selector, tasks, emptyText, withActions) {
      const target = document.querySelector(selector);
      if (!tasks.length) {
        target.innerHTML = '<div class="empty">' + escapeHTML(emptyText) + '</div>';
        return;
      }
      target.innerHTML = tasks.map((task) => (
        '<div class="row"><div><div class="title">#' + task.display_number + ' ' + escapeHTML(task.title) + '</div>' +
        '<div class="subtle">' + escapeHTML(taskMeta(task)) + '</div>' +
        (withActions ? taskActions(task) : '') + '</div>' +
        '<span class="pill ' + escapeHTML(task.status) + '">' + escapeHTML(task.status) + '</span></div>'
      )).join("");
    }

    function taskMeta(task) {
      const parts = [task.priority || "", task.mission || ""].filter(Boolean);
      if (task.review_target_type && task.review_target_id) {
        parts.push("review: " + task.review_target_type + ":" + task.review_target_id);
      }
      return parts.join(" ");
    }

    function taskActions(task) {
      const id = escapeAttr(task.id);
      const buttons = [];
      if (task.status === "review") {
        buttons.push(actionButton(id, "approve", "Approve", ""));
        buttons.push(actionButton(id, "complete", "Complete", ""));
        buttons.push(actionButton(id, "send-back", "Send Back", "Send this task back to ready?"));
        buttons.push(actionButton(id, "block", "Block", "Block this task?"));
      }
      if (task.status === "blocked" || task.status === "waiting") {
        buttons.push(actionButton(id, "send-back", "Ready", "Move this task back to ready?"));
      }
      if (task.status !== "cancelled" && task.status !== "done") {
        buttons.push(actionButton(id, "cancel", "Cancel", "Cancel this task?", true));
      }
      if (!buttons.length) return "";
      return '<div class="row-actions">' + buttons.join("") + '</div>';
    }

    function actionButton(id, action, label, confirmText, danger) {
      return '<button type="button" class="' + (danger ? "danger" : "") + '" data-task="' + id + '" data-action="' + escapeAttr(action) + '" data-confirm="' + escapeAttr(confirmText || "") + '">' + escapeHTML(label) + '</button>';
    }

    function renderLeases(leases) {
      document.querySelector("#lease-count").textContent = plural(leases.length, "active lease");
      const target = document.querySelector("#leases");
      if (!leases.length) {
        target.innerHTML = '<div class="empty">No active leases.</div>';
        return;
      }
      target.innerHTML = leases.map((item) => {
        const lease = item.lease || {};
        const task = item.task || {};
        const expiry = item.is_stale ? "stale" : formatDuration(item.seconds_until_expiry);
        return '<div class="row"><div><div class="title">#' + task.display_number + ' ' + escapeHTML(task.title || lease.task_id) + '</div>' +
          '<div class="subtle">' + escapeHTML(lease.agent_id || "") + ' on ' + escapeHTML(lease.machine_id || "") + '</div></div>' +
          '<span class="pill ' + (item.is_stale ? "failed" : "claimed") + '">' + escapeHTML(expiry) + '</span></div>';
      }).join("");
    }

    function renderEntities(selector, items, emptyText) {
      const target = document.querySelector(selector);
      if (!items.length) {
        target.innerHTML = '<div class="empty">' + escapeHTML(emptyText) + '</div>';
        return;
      }
      target.innerHTML = items.map((item) => (
        '<div class="entity"><strong>' + escapeHTML(item.id) + '</strong>' +
        '<div class="subtle">' + escapeHTML(item.status || "") + ' ' + escapeHTML((item.capabilities || []).join(", ")) + '</div></div>'
      )).join("");
    }

    function renderEvents(events) {
      document.querySelector("#event-count").textContent = plural(events.length, "event");
      const target = document.querySelector("#events");
      if (!events.length) {
        target.innerHTML = '<div class="empty">No events recorded.</div>';
        return;
      }
      target.innerHTML = events.slice(0, 12).map((event) => (
        '<div class="row"><div><div class="title">' + escapeHTML(event.event_type) + '</div>' +
        '<div class="subtle">' + escapeHTML(event.actor_type) + ':' + escapeHTML(event.actor_id) + '</div></div>' +
        '<span class="pill">' + escapeHTML(formatTime(event.created_at)) + '</span></div>'
      )).join("");
    }

    function formatTime(value) {
      if (!value) return "unknown";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return "unknown";
      return date.toLocaleString();
    }

    function formatDuration(seconds) {
      if (seconds == null) return "unknown";
      if (seconds < 60) return seconds + "s";
      const minutes = Math.floor(seconds / 60);
      if (minutes < 60) return minutes + "m";
      return Math.floor(minutes / 60) + "h";
    }

    function plural(count, word) {
      return number(count) + " " + word + (count === 1 ? "" : "s");
    }

    function number(value) {
      return Number(value || 0).toLocaleString();
    }

    function escapeHTML(value) {
      return String(value ?? "").replace(/[&<>"']/g, (char) => ({
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;"
      }[char]));
    }

    function escapeAttr(value) {
      return escapeHTML(value).replace(new RegExp(String.fromCharCode(96), "g"), "&#96;");
    }

    function configureAutoRefresh() {
      if (refreshTimer) clearInterval(refreshTimer);
      const seconds = Number(refreshSecondsInput.value || 0);
      if (seconds > 0) {
        // Each refresh re-checks the session, so an expired or signed-out
        // session is reported honestly instead of silently showing stale data.
        refreshTimer = setInterval(reload, seconds * 1000);
      }
    }

    document.querySelector("#attention").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-task]");
      if (!button) return;
      actOnTask(button.dataset.task, button.dataset.action, button.dataset.confirm);
    });
    document.querySelector("#projects").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-inspect]");
      if (!button) return;
      inspectProjectDetail(button.dataset.inspect, button.dataset.label);
    });
    document.querySelector("#auto-orch-reports").addEventListener("click", async (event) => {
      const button = event.target.closest("button[data-copy]");
      if (!button) return;
      const value = button.dataset.copy || "";
      try {
        await navigator.clipboard.writeText(value);
        showNotice("Copied path: " + value);
      } catch (_) {
        showNotice(value);
      }
    });
    document.querySelector("#stats").addEventListener("click", (event) => {
      const button = event.target.closest("button[data-kpi-kind]");
      if (!button) return;
      activeKPI = { kind: button.dataset.kpiKind, status: button.dataset.kpiStatus, label: button.dataset.kpiLabel };
      renderStats((latestSnapshot && latestSnapshot.status) || {}, latestSnapshot || {});
      refreshKPIDetail();
    });
    document.querySelector("#clear-kpi").addEventListener("click", () => {
      activeKPI = null;
      document.querySelector("#kpi-detail-panel").style.display = "none";
      document.querySelector("#kpi-detail").innerHTML = "";
      renderStats((latestSnapshot && latestSnapshot.status) || {}, latestSnapshot || {});
    });

%%COCKPIT_JS%%

    configureAutoRefresh();
    wireCockpit();
    reload();
  </script>
</body>
</html>`
