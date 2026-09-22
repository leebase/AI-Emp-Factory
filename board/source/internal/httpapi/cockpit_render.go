package httpapi

// cockpitRenderJS renders the fleet: the Needs-Lee list, the burning list and
// the grouped roster, plus the shared node helpers the other cockpit assets
// build on. Employee detail lives in its own assets; nothing here writes into
// an open dialog.
//
// Two rules hold everywhere below:
//
//   - Every node is built with createElement and textContent. There is no
//     innerHTML, no HTML string concatenation and no attribute built from
//     employee-supplied text, so a hostile observation value is displayed as
//     characters and can never become markup or script.
//   - Only an http(s) evidence reference becomes a link. Any other scheme is
//     printed as text, so a javascript:, data: or file: URI is never clickable.
const cockpitRenderJS = `
    function el(tag, className, text) {
      var node = document.createElement(tag);
      if (className) node.className = className;
      if (text !== undefined && text !== null) node.textContent = String(text);
      return node;
    }

    function badge(text, tone) {
      var node = el("span", "mc-badge", text);
      node.setAttribute("data-tone", tone);
      return node;
    }

    // emptyStateText picks between four distinct claims: no artifact at all, a
    // last-known recorded absence, a current absence inside a TRUNCATED
    // employee window, and a current absence across the whole captured window.
    // None of them is a claim of complete operational knowledge: even the last
    // one is only the absence of a recorded row in the captured artifact, and a
    // truncated capture additionally cannot speak for the employees it never
    // read.
    function emptyStateText(captured, truncated, lastKnown, missing) {
      if (!cockpit.state) return missing;
      if (cockpitCurrency() !== "current") return lastKnown;
      return cockpit.state.employees_truncated === true ? truncated : captured;
    }

    // capturedCountText keeps a count honest about what it counted: rows
    // recorded in the captured window, never a factual zero for the estate.
    // With no artifact at all there is no captured window and nothing was
    // counted: reporting "0 recorded in the captured window" would claim a
    // successful capture that never happened, so the count is stated as
    // unknown. The captured and TRUNCATED wording below stays scoped to the
    // records actually held.
    function capturedCountText(count, word) {
      if (!cockpit.state) return "Unknown: no employee window was captured, so no " + word + " count exists";
      var text = plural(count, word) + " recorded in the captured window";
      if (cockpit.state && cockpit.state.employees_truncated === true) text += " (window TRUNCATED)";
      return text;
    }

    function metric(label, value) {
      var wrap = el("div", "mc-metric");
      wrap.appendChild(el("span", "mc-metric-label", label));
      wrap.appendChild(el("span", "mc-metric-value", value));
      return wrap;
    }

    function evidenceNode(uri) {
      var text = textOr(uri, MC_UNKNOWN);
      if (!/^https?:\/\//i.test(text)) return el("span", null, text);
      var link = el("a", null, text);
      link.setAttribute("href", text);
      link.setAttribute("rel", "noopener noreferrer");
      return link;
    }

    function replaceChildren(node, children) {
      while (node.firstChild) node.removeChild(node.firstChild);
      for (var i = 0; i < children.length; i++) node.appendChild(children[i]);
    }

    function renderCockpit() {
      var currency = cockpitCurrency();
      var banner = document.querySelector("#cockpit-currency");
      banner.setAttribute("data-currency", currency);
      banner.textContent = cockpitCurrencyText();
      // A session that can no longer read the estate must not keep a control
      // on screen that implies it can.
      var authorized = !cockpit.authStatus;
      document.querySelector("#cockpit-refresh").hidden = !authorized;
      // 401 means "nobody is signed in", which a sign-in link can fix. 403
      // means "this identity is not allowed", which signing in again cannot
      // fix; offering the link there invites Lee to retry an identity that has
      // already been refused.
      document.querySelector("#cockpit-signin").hidden =
        cockpit.authStatus !== 401 || !window.__AUTH_ENABLED__;
      renderGates();
      renderBurning();
      renderChallenges();
      renderWatchlist();
      renderCapacity();
      renderRoster();
      renderDetail();
    }

    function renderGates() {
      var gates = cockpitRequiredGates();
      document.querySelector("#needs-lee-count").textContent = capturedCountText(gates.length, "decision");
      var nodes = [];
      if (gates.length === 0) {
        // An empty list only means "no required gate is recorded in the
        // artifact this view is holding". A stale, denied or absent artifact
        // cannot speak for the estate now; a truncated capture cannot speak
        // for the employees outside its window; and even a complete current
        // capture states a recorded absence rather than a global all-clear.
        nodes.push(el("p", "mc-empty", emptyStateText(
          "No required decision is recorded in this captured employee window. That is a recorded absence in this artifact, not a guarantee that nothing needs Lee.",
          "The captured employee window is TRUNCATED: no required decision is recorded inside it, and employees outside that window were not read and are unknown.",
          "No required decision was recorded in this last-known artifact, which is not current.",
          "Needs Lee is unknown: no employee state is available.")));
      }
      for (var i = 0; i < gates.length; i++) {
        var entry = gates[i];
        var button = el("button", "mc-gate");
        button.type = "button";
        button.setAttribute("data-employee", textOr(entry.employee_id, ""));
        button.appendChild(el("strong", null, textOr(entry.gate.title, "Untitled decision")));
        button.appendChild(el("p", null, textOr(entry.employee_id, MC_UNKNOWN) + " - " + textOr(entry.gate.reason, "no reason given")));
        button.appendChild(el("p", "mc-count", "Raised " + relativeTime(entry.gate.raised_at) +
          " by " + textOr(entry.gate.owner, MC_UNKNOWN) + ". Decision " + textOr(entry.gate.decision_id, MC_UNKNOWN) + "."));
        nodes.push(button);
      }
      replaceChildren(document.querySelector("#needs-lee"), nodes);
    }

    function renderBurning() {
      var burning = cockpitBurning();
      document.querySelector("#burn-count").textContent = capturedCountText(burning.length, "employee");
      var nodes = [];
      if (burning.length === 0) {
        nodes.push(el("p", "mc-empty", emptyStateText(
          "No burning employee is recorded in this captured employee window. That is a recorded absence in this artifact, not a guarantee that nothing is burning.",
          "The captured employee window is TRUNCATED: no burning employee is recorded inside it, and employees outside that window were not read and are unknown.",
          "No burning employee was recorded in this last-known artifact, which is not current.",
          "Burn state is unknown: no employee state is available.")));
      }
      for (var i = 0; i < burning.length; i++) {
        var card = burning[i];
        var button = el("button", "mc-burn");
        button.type = "button";
        button.setAttribute("data-employee", textOr(card.employee_id, ""));
        button.appendChild(el("strong", null, textOr(card.employee_id, MC_UNKNOWN) + " - Burning"));
        button.appendChild(el("p", null, textOr(card.condition_reason && card.condition_reason.message, "no reason recorded")));
        button.appendChild(el("p", "mc-count", plural(cockpitFindingsFor(card, "burn_loop").length, "burn finding") +
          ". Attempts since last accepted delivery: " + countText(card.non_delivery_count) + "."));
        nodes.push(button);
      }
      replaceChildren(document.querySelector("#burn-loops"), nodes);
    }

    function renderRoster() {
      // Every card below repeats this currency claim, so it is recorded here
      // and compared later: the roster must never outlive the claim it made.
      cockpit.rosterCurrency = cockpitCurrency();
      var groups = cockpitGroupedCards();
      var total = 0;
      for (var g = 0; g < groups.length; g++) total += groups[g].cards.length;
      var known = (cockpit.state && typeof cockpit.state.total_employees === "number")
        ? cockpit.state.total_employees : null;
      document.querySelector("#roster-count").textContent = total + " shown of " + countText(known) +
        ((cockpit.state && cockpit.state.employees_truncated) ? " (truncated)" : "");
      var nodes = [];
      if (groups.length === 0) {
        nodes.push(el("p", "mc-empty", cockpit.state
          ? "No employee matches this filter."
          : "No employee roster is available."));
      }
      for (var i = 0; i < groups.length; i++) {
        nodes.push(el("h3", "mc-group-title", groups[i].label + " (" + groups[i].cards.length + ")"));
        var grid = el("div", "mc-grid");
        for (var j = 0; j < groups[i].cards.length; j++) grid.appendChild(employeeCard(groups[i].cards[j]));
        nodes.push(grid);
      }
      replaceChildren(document.querySelector("#roster"), nodes);
    }

    function employeeCard(card) {
      var placement = placementKey(card);
      var condition = conditionKey(card);
      var button = el("button", "mc-card");
      button.type = "button";
      button.setAttribute("data-employee", textOr(card.employee_id, ""));
      button.setAttribute("data-placement", placement);
      button.setAttribute("data-condition", condition);
      button.appendChild(el("div", "mc-card-name", textOr(card.employee_id, MC_UNKNOWN)));
      var badges = el("div", "mc-badges");
      badges.appendChild(badge(MC_PLACEMENT_LABEL[placement], MC_PLACEMENT_TONE[placement]));
      badges.appendChild(badge(MC_CONDITION_LABEL[condition], MC_CONDITION_TONE[condition]));
      if (Array.isArray(card.contradictions) && card.contradictions.length > 0) {
        badges.appendChild(badge(plural(card.contradictions.length, "source contradiction"), "warn"));
      }
      button.appendChild(badges);
      button.appendChild(el("p", "mc-reason",
        textOr(card.placement_reason && card.placement_reason.message, "placement reason unknown") + " " +
        textOr(card.condition_reason && card.condition_reason.message, "condition reason unknown")));
      var metrics = el("div", "mc-metrics");
      metrics.appendChild(metric("Latest value", latestValueText(card)));
      metrics.appendChild(metric("Working on", workText(card)));
      metrics.appendChild(metric("Next checkpoint", checkpointText(card)));
      metrics.appendChild(metric("Attempts since delivery", countText(card.non_delivery_count) +
        (card.delivery_truncated ? " (truncated)" : "")));
      button.appendChild(metrics);
      button.appendChild(el("p", "mc-foot", freshnessText(card) + " Observed " + relativeTime(card.observed_at) +
        ". " + (cockpitCurrency() === "current" ? "Current." : "Last known, not current.")));
      return button;
    }

    function latestValueText(card) {
      var value = card && card.latest_value;
      if (!value) return "No accepted delivery recorded";
      return textOr(value.value, MC_UNKNOWN) + " (accepted " + relativeTime(value.accepted_at) + ")";
    }

    function workText(card) {
      var context = card && card.context;
      var work = context && context.work;
      if (work) {
        var kind = work.kind === "governed_run" ? "Governed run" : "Mission assignment";
        return kind + " " + textOr(work.ref, "(reference unknown)") +
          (work.status ? " - " + work.status : "");
      }
      // Readiness answers "could this employee be given work", which is only
      // the same question as "what is it doing" when it is actually on the
      // bench. A scheduled, paused, not-commissioned or unknown employee with a
      // ready flag is not benched, so its current work stays unknown.
      if (placementKey(card) === "on_the_bench") {
        var readiness = context && context.readiness;
        if (readiness && readiness.ready === true) return "Bench: ready for work";
        if (readiness && readiness.ready === false) return "Bench: not ready";
      }
      return MC_UNKNOWN;
    }

    function checkpointText(card) {
      var checkpoint = card && card.context && card.context.checkpoint;
      if (!checkpoint) return MC_UNKNOWN;
      if (checkpoint.next_fire) return relativeTime(checkpoint.next_fire) + " (" + checkpoint.next_fire + ")";
      if (checkpoint.enabled === false) return "Schedule disabled";
      if (checkpoint.enabled === true) return "Scheduled, next run unknown";
      return MC_UNKNOWN;
    }

    function freshnessText(card) {
      var freshness = (card && card.source_freshness) || {};
      var fresh = 0, stale = 0, unknown = 0, owners = Object.keys(freshness);
      for (var i = 0; i < owners.length; i++) {
        var value = freshness[owners[i]];
        if (value === "fresh") fresh++;
        else if (value === "stale") stale++;
        else unknown++;
      }
      if (owners.length === 0) return "No source freshness reported.";
      return "Sources: " + fresh + " fresh, " + stale + " stale, " + unknown + " unknown.";
    }

`
