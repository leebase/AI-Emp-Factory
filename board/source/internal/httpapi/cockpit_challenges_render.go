package httpapi

// cockpitChallengesRenderJS renders the two Sprint 7 landing sections. Like
// the rest of the cockpit it builds every node with createElement and
// textContent: a hostile rule id, message, owner, observation id or evidence
// path is displayed as inert characters. Nothing here creates an href, a
// navigation, an HTML handler attribute or a filesystem read, and the only
// button is the existing employee-detail opener.
const cockpitChallengesRenderJS = `
    function challengeSourceText(source) {
      if (!source) return MC_UNKNOWN;
      return textOr(source.owner, MC_UNKNOWN) + " / " + textOr(source.observation_id, MC_UNKNOWN) +
        " observed " + textOr(source.observed_at, MC_UNKNOWN);
    }

    function challengeRowNode(row, currency, focusKey) {
      // The row is the existing employee-detail opener and nothing else. It
      // carries no decision, no acknowledgement and no repair action.
      var button = el("button", "mc-gate");
      button.type = "button";
      button.setAttribute("data-employee", row.employeeID === MC_UNKNOWN ? "" : row.employeeID);
      // The focus identity travels as an attribute value only. It is compared
      // with === after the rebuild, never used to build a selector, so a
      // hostile employee or rule id cannot become selector syntax.
      button.setAttribute("data-challenge-key", focusKey);
      button.setAttribute("data-challenge-category", row.category);
      button.setAttribute("data-challenge-severity", row.severity);
      var head = el("div", "mc-badges");
      head.appendChild(badge(
        Object.prototype.hasOwnProperty.call(MC_CHALLENGE_SEVERITY_LABEL, row.severity)
          ? MC_CHALLENGE_SEVERITY_LABEL[row.severity]
          : "Severity unknown to this view",
        Object.prototype.hasOwnProperty.call(MC_CHALLENGE_SEVERITY_TONE, row.severity)
          ? MC_CHALLENGE_SEVERITY_TONE[row.severity] : "warn"));
      head.appendChild(badge(
        row.category === "unknown"
          ? "Category unknown to this view: " + textOr(row.rawCategory, MC_UNKNOWN)
          : MC_CHALLENGE_CATEGORY_LABEL[row.category],
        "warn"));
      button.appendChild(head);
      button.appendChild(el("strong", null, row.employeeID));
      button.appendChild(el("p", null, row.message));
      button.appendChild(el("p", "mc-count", "Rule " + row.ruleID + " version " + row.ruleVersion +
        ". Recorded severity " + textOr(row.rawSeverity, MC_UNKNOWN) + "."));
      if (row.sources.length === 0) {
        button.appendChild(el("p", "mc-count", "No source observation recorded for this finding."));
      }
      for (var s = 0; s < row.sources.length; s++) {
        button.appendChild(el("p", "mc-count", "Source: " + challengeSourceText(row.sources[s])));
      }
      // Every row repeats the panel's currency claim, so a retained row can
      // never be read as current after a denied or failed refresh.
      button.appendChild(el("p", "mc-foot", currency === "current"
        ? "Current as of the held artifact."
        : "Last known, not current."));
      return button;
    }

    function renderChallengeFilters() {
      var host = document.querySelector("#challenge-filters");
      if (!host) return;
      var counts = challengeCounts();
      // Rebuilding the controls under a focused button destroys focus
      // silently, so the focused preset is restored after the rebuild.
      var focused = host.contains && document.activeElement && host.contains(document.activeElement)
        ? document.activeElement.getAttribute("data-challenge-filter") : null;
      var nodes = [];
      for (var i = 0; i < MC_CHALLENGE_PRESETS.length; i++) {
        var preset = MC_CHALLENGE_PRESETS[i];
        var button = el("button", null, preset.label + " (" + counts[preset.key] + ")");
        button.type = "button";
        button.setAttribute("data-challenge-filter", preset.key);
        button.setAttribute("aria-pressed", String(preset.key === cockpit.challengeFilter));
        nodes.push(button);
      }
      replaceChildren(host, nodes);
      if (focused) {
        var restore = host.querySelector("button[data-challenge-filter='" + focused + "']");
        if (restore) restore.focus();
      }
    }

    // Rebuilding the panel under a focused row destroys focus silently: the
    // browser drops it on <body> and the next Tab restarts at the top of the
    // document. A currency tick and a refresh of the same artifact both
    // rebuild, so the focused row's identity is captured first and restored
    // afterwards. Focus is only ever restored when it was already inside this
    // panel, so another panel and an open employee detail keep theirs.
    function challengeHeldFocusKey(host) {
      if (!host || !host.contains || !document.activeElement) return null;
      if (!host.contains(document.activeElement)) return null;
      return document.activeElement.getAttribute
        ? document.activeElement.getAttribute("data-challenge-key") : null;
    }

    function challengeScrollState(host) {
      return {
        host: host && typeof host.scrollTop === "number" ? host.scrollTop : null,
        x: (typeof window !== "undefined" && typeof window.scrollX === "number") ? window.scrollX : null,
        y: (typeof window !== "undefined" && typeof window.scrollY === "number") ? window.scrollY : null
      };
    }

    // The key is matched by comparing attribute values, never by building a
    // selector, so hostile employee or rule text cannot select another row.
    // If the finding is gone, or its recorded identity cannot be told apart
    // from another finding's, the fallback is the first row still rendered,
    // and with no rows at all it is the panel's own visible filter control:
    // both are inside Challenges and both are actually usable. A fallback is
    // never silent - renderChallenges states in the panel that the held
    // finding was not matched, so a moved focus is never read as continuity.
    function restoreChallengeFocus(host, key, scroll) {
      var target = null;
      var children = host.children || [];
      for (var i = 0; i < children.length; i++) {
        var candidate = children[i].getAttribute ? children[i].getAttribute("data-challenge-key") : null;
        if (candidate === null) continue;
        if (candidate === key) { target = children[i]; break; }
        if (!target) target = children[i];
      }
      if (!target) {
        var filters = document.querySelector("#challenge-filters");
        var presets = (filters && filters.children) ? filters.children : [];
        for (var f = 0; f < presets.length; f++) {
          if (!target) target = presets[f];
          if (presets[f].getAttribute && presets[f].getAttribute("aria-pressed") === "true") {
            target = presets[f];
            break;
          }
        }
      }
      if (!target || !target.focus) return;
      // Focusing scrolls the target into view by default, which moves a
      // reader who had not asked to move. The prior scroll position is the
      // one that is restored.
      try { target.focus({ preventScroll: true }); } catch (err) { target.focus(); }
      if (scroll.host !== null && typeof host.scrollTop === "number") host.scrollTop = scroll.host;
      if (scroll.x !== null && typeof window !== "undefined" && window.scrollTo &&
        (window.scrollX !== scroll.x || window.scrollY !== scroll.y)) {
        window.scrollTo(scroll.x, scroll.y);
      }
    }

    function renderChallenges() {
      var host = document.querySelector("#challenges");
      if (!host) return;
      var currency = cockpitCurrency();
      var rows = challengeFilteredRows();
      var heldKey = challengeHeldFocusKey(host);
      var scroll = challengeScrollState(host);
      var count = document.querySelector("#challenge-count");
      if (count) count.textContent = plural(rows.length, "finding");
      renderChallengeFilters();
      var nodes = [];
      var banner = el("p", "mc-foot", challengeCurrencyText());
      banner.setAttribute("data-currency", currency);
      nodes.push(banner);
      var truncation = challengeTruncationText();
      if (truncation !== "") nodes.push(el("p", "mc-foot", truncation));
      nodes.push(el("p", "mc-foot",
        "These are recorded rule findings, not decisions. A warning here is not a Needs Lee gate; " +
        "required human decisions stay in the Needs Lee section above and are listed there even while an employee is burning."));
      if (rows.length === 0) {
        nodes.push(el("p", "mc-empty", challengeEmptyText()));
      }
      var keys = challengeFocusKeys(rows);
      // Whether the held finding is still matched here is decided from the
      // rebuilt keys themselves, before the panel is replaced, so the
      // disclosure and the focus that is actually restored cannot disagree.
      var heldMatched = false;
      for (var k = 0; k < keys.length; k++) {
        if (keys[k] === heldKey) heldMatched = true;
      }
      if (heldKey !== null && !heldMatched) {
        nodes.push(el("p", "mc-foot", challengeFocusFallbackText(rows.length)));
      }
      for (var i = 0; i < rows.length; i++) nodes.push(challengeRowNode(rows[i], currency, keys[i]));
      replaceChildren(host, nodes);
      if (heldKey !== null) restoreChallengeFocus(host, heldKey, scroll);
    }

    // The watchlist restates the supplied non-critical findings. It never
    // relabels a blocking human decision as handled, and it never claims that
    // an employee's live activity proves a repair will succeed.
    function renderWatchlist() {
      var host = document.querySelector("#watchlist");
      if (!host) return;
      var rows = challengeOrderedRows();
      var currency = cockpitCurrency();
      var nodes = [];
      var handled = [];
      var watching = [];
      var critical = 0;
      for (var i = 0; i < rows.length; i++) {
        if (rows[i].severity === "critical") { critical++; continue; }
        watching.push(rows[i]);
      }
      var cards = (cockpit.state && Array.isArray(cockpit.state.cards)) ? cockpit.state.cards : [];
      for (var c = 0; c < cards.length; c++) {
        if (placementKey(cards[c]) !== "running") continue;
        var work = cards[c].context && cards[c].context.work;
        if (!work) continue;
        handled.push({ id: textOr(cards[c].employee_id, MC_UNKNOWN), work: workText(cards[c]) });
      }
      nodes.push(el("p", "mc-foot", currency === "current"
        ? "Watchlist from the held artifact."
        : "Watchlist rows are last known, not current."));
      nodes.push(el("h3", "mc-group-title", "Being handled (" + handled.length + ")"));
      nodes.push(el("p", "mc-foot",
        "Being handled means only: the employee is recorded as Running with live work context. " +
        "It is not proof a repair will succeed, and it never means a required Lee decision has been answered. " +
        "A required gate can and does coexist with live work."));
      if (handled.length === 0) {
        nodes.push(el("p", "mc-empty", "No employee is recorded as Running with live work context in this artifact."));
      }
      for (var h = 0; h < handled.length; h++) {
        nodes.push(el("p", "mc-count", handled[h].id + " - " + handled[h].work));
      }
      nodes.push(el("h3", "mc-group-title", "Watching (" + watching.length + ")"));
      nodes.push(el("p", "mc-foot",
        "Supplied non-critical findings, plus anything whose source truth is unknown. Nothing here is a decision."));
      if (watching.length === 0) {
        // Watching is not narrowed by the Challenges question filter, so it
        // states its own unfiltered scope rather than borrowing that filter's
        // empty wording.
        nodes.push(el("p", "mc-empty", watchlistEmptyText(critical)));
      }
      for (var w = 0; w < watching.length; w++) {
        nodes.push(el("p", "mc-count", watching[w].employeeID + " - " +
          (watching[w].category === "unknown"
            ? "category unknown to this view (" + textOr(watching[w].rawCategory, MC_UNKNOWN) + ")"
            : MC_CHALLENGE_CATEGORY_LABEL[watching[w].category]) +
          " - " + watching[w].message + " [rule " + watching[w].ruleID + " v" + watching[w].ruleVersion + "]"));
      }
      replaceChildren(host, nodes);
    }

    function renderCapacity() {
      var host = document.querySelector("#capacity");
      if (!host) return;
      var view = capacityView();
      var nodes = [];
      nodes.push(el("p", "mc-empty", view.headline));
      nodes.push(el("p", "mc-foot", view.detail));
      nodes.push(el("p", "mc-count", view.sourceText));
      nodes.push(el("p", "mc-foot", view.authorityText));
      replaceChildren(host, nodes);
    }
`
