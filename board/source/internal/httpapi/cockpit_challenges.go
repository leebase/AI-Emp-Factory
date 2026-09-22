package httpapi

// cockpitChallengesJS is the data half of the Sprint 7 managerial landing
// sections: the deterministic challenge panel and the capacity statement. It
// reads only the accepted active-state artifact that the cockpit already
// holds. It adds no request, no rule and no inference.
//
// Invariants enforced here, stated before the rendering half consumes them:
//
//   - A conclusion is only ever an accepted server rule. A row carries the
//     server's own rule_id/rule_version, category, severity, message and
//     source owner/observation identity, and nothing is re-classified,
//     re-scored, merged or deduplicated on the client.
//   - An unrecognised category is retained under All with its raw value shown
//     as unknown-to-this-view, never dropped and never folded into a known
//     bucket.
//   - A warning finding is not a human decision. Needs Lee stays the
//     required===true gate list computed in cockpit_state.go; a finding of any
//     severity, including unjustified_gate, never enters or leaves it, and a
//     burning employee never hides a required gate.
//   - Currency is a property of the whole panel. When the held artifact is
//     stale, unavailable or denied, every row is explicitly last-known and the
//     panel may not say "no problems".
//   - Emptiness is bounded. An empty current non-truncated list means only
//     that no matching finding was recorded in this artifact.
//   - Capacity is unavailable, not zero. The accepted contract carries no
//     subscription or dispatch observation, so no percentage is invented and
//     no control implying spend authority exists.
const cockpitChallengesJS = `
    // The presets are a closed local vocabulary. They filter rows that are
    // already on screen; they never request anything, never re-place an
    // employee on the roster and never change card truth or currency.
    var MC_CHALLENGE_PRESETS = [
      { key: "all", label: "All" },
      { key: "burn_loop", label: "Burn loop" },
      { key: "long_repair_lineage", label: "Long repair lineage" },
      { key: "stale_truth", label: "Stale truth" },
      { key: "conflicting_sources", label: "Conflicting sources" },
      { key: "activity_without_value", label: "Activity without value" }
    ];
    var MC_CHALLENGE_CATEGORY_LABEL = {
      burn_loop: "Burn loop",
      long_repair_lineage: "Long repair lineage",
      stale_truth: "Stale truth",
      conflicting_sources: "Conflicting sources",
      activity_without_value: "Activity without value",
      unjustified_gate: "Unjustified gate",
      retained_pause: "Retained pause"
    };
    // Severity ordering is the server's own closed vocabulary. An unrecognised
    // severity sorts last and is never promoted to a known one.
    var MC_CHALLENGE_SEVERITY_ORDER = { critical: 0, warn: 1, info: 2 };
    var MC_CHALLENGE_SEVERITY_LABEL = { critical: "Critical", warn: "Warning", info: "Informational" };
    var MC_CHALLENGE_SEVERITY_TONE = { critical: "bad", warn: "warn", info: "warn" };

    cockpit.challengeFilter = "all";
    // Focus identity is versioned so a stored key from an older shape can
    // never be mistaken for a current one, and the ambiguous form carries its
    // own kind so it is recognised by value rather than by position.
    var MC_CHALLENGE_FOCUS_KIND = "challenge-finding/2";
    var MC_CHALLENGE_FOCUS_AMBIGUOUS_KIND = "challenge-finding-indistinguishable/1";
    var MC_CHALLENGE_FOCUS_AMBIGUOUS_PREFIX = JSON.stringify([MC_CHALLENGE_FOCUS_AMBIGUOUS_KIND]).slice(0, -1);
    cockpit.challengeFocusRender = 0;

    function challengeSeverityKey(finding) {
      var raw = typeof (finding && finding.severity) === "string" ? finding.severity : "";
      return Object.prototype.hasOwnProperty.call(MC_CHALLENGE_SEVERITY_ORDER, raw) ? raw : "unknown";
    }

    function challengeCategoryKey(finding) {
      var raw = typeof (finding && finding.category) === "string" ? finding.category : "";
      return Object.prototype.hasOwnProperty.call(MC_CHALLENGE_CATEGORY_LABEL, raw) ? raw : "unknown";
    }

    // challengeRows aggregates every supplied state.findings entry, in the
    // artifact's own order, before any ordering or filtering is applied. Each
    // row keeps its position so ordering can stay stable for equal keys, and
    // keeps the raw category/severity so an unknown vocabulary is displayed as
    // the server actually wrote it.
    function challengeRows() {
      var supplied = (cockpit.state && Array.isArray(cockpit.state.findings)) ? cockpit.state.findings : [];
      var rows = [];
      for (var i = 0; i < supplied.length; i++) {
        var entry = supplied[i];
        if (!entry || !entry.finding) continue;
        var finding = entry.finding;
        rows.push({
          index: i,
          employeeID: textOr(entry.employee_id, MC_UNKNOWN),
          ruleID: textOr(finding.rule_id, MC_UNKNOWN),
          ruleVersion: textOr(finding.rule_version, MC_UNKNOWN),
          category: challengeCategoryKey(finding),
          rawCategory: optionalText(finding.category),
          severity: challengeSeverityKey(finding),
          rawSeverity: optionalText(finding.severity),
          message: textOr(finding.message, "no message recorded"),
          sources: Array.isArray(finding.sources) ? finding.sources : []
        });
      }
      return rows;
    }

    // Ordering: a critical burn finding is the first thing Lee sees. After
    // that the order is the server's severity, then employee, then rule, then
    // the artifact's own position, so the same artifact always renders in the
    // same order. No severity is inferred to achieve this.
    function challengeRank(row) {
      if (row.severity === "critical" && row.category === "burn_loop") return 0;
      return 1;
    }

    function compareChallengeRows(a, b) {
      var ra = challengeRank(a), rb = challengeRank(b);
      if (ra !== rb) return ra - rb;
      var sa = Object.prototype.hasOwnProperty.call(MC_CHALLENGE_SEVERITY_ORDER, a.severity)
        ? MC_CHALLENGE_SEVERITY_ORDER[a.severity] : 9;
      var sb = Object.prototype.hasOwnProperty.call(MC_CHALLENGE_SEVERITY_ORDER, b.severity)
        ? MC_CHALLENGE_SEVERITY_ORDER[b.severity] : 9;
      if (sa !== sb) return sa - sb;
      if (a.employeeID !== b.employeeID) return a.employeeID < b.employeeID ? -1 : 1;
      if (a.ruleID !== b.ruleID) return a.ruleID < b.ruleID ? -1 : 1;
      return a.index - b.index;
    }

    function challengeOrderedRows() {
      var rows = challengeRows();
      rows.sort(compareChallengeRows);
      return rows;
    }

    // A preset matches on the server's category only. "all" retains every row,
    // including a category this view does not know.
    function challengeMatches(row, preset) {
      return preset === "all" ? true : row.category === preset;
    }

    function challengeFilteredRows() {
      var rows = challengeOrderedRows();
      var preset = cockpit.challengeFilter;
      var matched = [];
      for (var i = 0; i < rows.length; i++) {
        if (challengeMatches(rows[i], preset)) matched.push(rows[i]);
      }
      return matched;
    }

    // Counts are computed from the same ordered row list the panel renders, so
    // a preset's count can never contradict the rows shown when it is chosen.
    function challengeCounts() {
      var rows = challengeOrderedRows();
      var counts = {};
      for (var p = 0; p < MC_CHALLENGE_PRESETS.length; p++) {
        var preset = MC_CHALLENGE_PRESETS[p].key;
        var total = 0;
        for (var i = 0; i < rows.length; i++) {
          if (challengeMatches(rows[i], preset)) total++;
        }
        counts[preset] = total;
      }
      return counts;
    }

    // The panel's own currency sentence. It never says "no problems": when the
    // artifact is missing, denied or stale the rows are last-known, and an
    // empty list is only an empty recorded list.
    function challengeCurrencyText() {
      var currency = cockpitCurrency();
      if (currency === "loading") return "Loading challenge findings...";
      if (currency === "unavailable") {
        // 401 and 403 are stated separately: one is "nobody is signed in",
        // the other is "this identity is refused". Neither may leave a
        // retained row looking current.
        var base = cockpit.authStatus === 401
          ? "Challenge findings are unavailable: this browser is not signed in (HTTP 401)."
          : (cockpit.authStatus === 403
            ? "Challenge findings are unavailable: this identity is not allowed to read them (HTTP 403)."
            : "Challenge findings are unavailable: " + cockpit.unavailable + ".");
        return cockpit.state
          ? base + " The findings below are last-known and are NOT current."
          : base + " No finding is being shown; this is not a statement that no problem exists.";
      }
      if (currency === "stale") {
        return "STALE last-known findings, not current truth." +
          (cockpit.refreshError ? " Last refresh failed: " + cockpit.refreshError + "." : "");
      }
      return "Current findings from the held artifact.";
    }

    function challengeTruncationText() {
      if (!cockpit.state) return "";
      return cockpit.state.employees_truncated === true
        ? "Fleet read TRUNCATED: employees beyond the captured window contributed no finding to this list."
        : "Fleet read not truncated in this artifact.";
    }

    // challengeEmptyText distinguishes the three reasons a list can be empty.
    // Only a current, non-truncated, present artifact may say that nothing
    // matching was recorded, and even then it says nothing about the estate.
    function challengeEmptyText() {
      if (!cockpit.state) return "No challenge findings are available; this is not a statement that no problem exists.";
      var currency = cockpitCurrency();
      if (currency !== "current") {
        return "No matching finding in this last-known artifact. It is not current, so this is not a statement that no problem exists now.";
      }
      if (cockpit.state.employees_truncated === true) {
        return "No matching finding in the captured window, which was TRUNCATED. Employees outside it were not read.";
      }
      return cockpit.challengeFilter === "all"
        ? "No finding was recorded in this artifact. That is the absence of a recorded finding, not a confirmation that nothing is wrong."
        : "No recorded finding matches this question in this artifact.";
    }

    // watchlistEmptyText is the Watching list's own sentence. Watching shows
    // every recorded non-critical finding, so it is not narrowed by the
    // Challenges question filter and must never borrow that filter's wording:
    // doing so told Lee a preset had excluded rows Watching had never
    // filtered. It discloses the same bounds the rest of the cockpit does -
    // no artifact, not current, TRUNCATED window - plus the case where
    // findings were recorded but every one of them is critical and therefore
    // belongs to Challenges above rather than here.
    function watchlistEmptyText(criticalCount) {
      var scope = " Watching lists every recorded non-critical finding and is not narrowed by the Challenges question filter.";
      if (!cockpit.state) {
        return "No finding is available at all, so no non-critical finding can be listed; this is not a statement that no problem exists." + scope;
      }
      if (cockpitCurrency() !== "current") {
        return "No non-critical finding in this last-known artifact. It is not current, so this is not a statement that no problem exists now." + scope;
      }
      if (cockpit.state.employees_truncated === true) {
        return "No non-critical finding in the captured window, which was TRUNCATED. Employees outside it were not read." + scope;
      }
      if (criticalCount > 0) {
        return "Every finding recorded in this artifact is critical, so none is listed here; the " + criticalCount +
          " critical finding" + (criticalCount === 1 ? " is" : "s are") + " in Challenges above." + scope;
      }
      return "No non-critical finding was recorded in this artifact. That is the absence of a recorded finding, not a confirmation that nothing is wrong." + scope;
    }

    // A finding's focus identity, used only to return focus to the same
    // recorded finding after the panel is rebuilt. Position is not identity:
    // an earlier finding that disappears must never hand its seat, and its
    // focus, to another owner's finding of the same rule.
    //
    // The identity is built only from fields the server actually records for
    // the finding. The discriminator production really provides for same-rule
    // findings is the source owner: internal/operations/managerial_condition.go
    // emits one stale-truth and one conflicting-sources finding per source
    // owner, so employee + rule + version + category + the finding's own set
    // of source owners is what separates them. Observation ids and observation
    // times are deliberately excluded: the same finding is re-observed on
    // every refresh, so folding that volatile evidence into the key would look
    // precise and drop focus on every tick. Keys are compared with === and
    // never interpolated into a selector, so a hostile employee, rule or owner
    // id stays inert characters.
    function challengeIdentityField(value) {
      // A recorded string and an absent field must not collapse into the same
      // identity, and neither may two different unmapped raw categories that
      // both display as "unknown", so the type is carried with the value.
      return typeof value === "string" ? ["s", value] : ["x", typeof value];
    }

    // challengeSourceOwners is the finding's canonical owner set: sorted and
    // de-duplicated, so a producer that re-orders or repeats its source list
    // does not rename the finding, and a source that records no owner stays an
    // explicitly unidentified member instead of merging into a named one.
    function challengeSourceOwners(row) {
      var sources = Array.isArray(row && row.sources) ? row.sources : [];
      var seen = {};
      var owners = [];
      for (var i = 0; i < sources.length; i++) {
        var source = sources[i];
        var owner = (source && typeof source.owner === "string" && source.owner !== "")
          ? JSON.stringify(["owner", source.owner])
          : JSON.stringify(["owner-unrecorded"]);
        if (Object.prototype.hasOwnProperty.call(seen, owner)) continue;
        seen[owner] = true;
        owners.push(owner);
      }
      owners.sort();
      return owners;
    }

    function challengeFocusKey(row) {
      return JSON.stringify([MC_CHALLENGE_FOCUS_KIND,
        challengeIdentityField(row.employeeID), challengeIdentityField(row.ruleID),
        challengeIdentityField(row.ruleVersion), challengeIdentityField(row.category),
        challengeIdentityField(row.rawCategory), challengeSourceOwners(row)]);
    }

    // Two rendered findings whose recorded identity is identical cannot be
    // told apart truthfully. Numbering them would be position identity again,
    // so every indistinguishable row gets a key that is unique to this render
    // and can never match a key held from an earlier one. The panel then falls
    // back visibly rather than claiming a continuity it cannot prove.
    function challengeFocusKeyIsAmbiguous(key) {
      return typeof key === "string" && key.indexOf(MC_CHALLENGE_FOCUS_AMBIGUOUS_PREFIX) === 0;
    }

    function challengeFocusKeys(rows) {
      var bases = [];
      var counts = {};
      var keys = [];
      for (var i = 0; i < rows.length; i++) {
        var base = challengeFocusKey(rows[i]);
        bases.push(base);
        counts[base] = Object.prototype.hasOwnProperty.call(counts, base) ? counts[base] + 1 : 1;
      }
      cockpit.challengeFocusRender = (typeof cockpit.challengeFocusRender === "number"
        ? cockpit.challengeFocusRender : 0) + 1;
      for (var r = 0; r < rows.length; r++) {
        keys.push(counts[bases[r]] === 1
          ? bases[r]
          : JSON.stringify([MC_CHALLENGE_FOCUS_AMBIGUOUS_KIND, cockpit.challengeFocusRender, r]));
      }
      return keys;
    }

    // The sentence shown when a held focus could not be matched. It states only
    // the narrow fact this view knows - the previously focused finding was not
    // uniquely matched here, and where focus went instead. It does not say the
    // finding is absent or resolved, and it does not attribute the failure to
    // any particular other finding: neither claim is established by a failed
    // match.
    function challengeFocusFallbackText(rowCount) {
      return "The previously focused finding could not be uniquely matched after this refresh. " +
        (rowCount === 0
          ? "Focus moved to this panel's own filter control."
          : "Focus moved to the first finding listed below.");
    }

    // Capacity is stated, never estimated. The accepted captured contract has
    // no subscription, seat, quota or dispatch observation of any kind, so the
    // only truthful answer is unavailable: not zero, not a percentage, and
    // never an authority to dispatch or spend.
    function capacityView() {
      return {
        available: false,
        headline: "Capacity is unavailable in the captured accepted contract.",
        detail: "No subscription, seat, quota or dispatch observation is carried by this artifact. " +
          "Absent capacity is unavailable, not zero and not a percentage.",
        sourceText: "Source unknown. Observation time unknown.",
        authorityText: "Observational only. This view carries no authority to dispatch work or to spend, and offers no control that would."
      };
    }
`
