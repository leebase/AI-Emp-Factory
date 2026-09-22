package httpapi

// cockpitHistoryJS renders the per-employee chronological history section of
// employee detail. It is an additive view over the captured managerial detail
// response (cockpit.detailView), read once at open and never re-read, so a
// newer fleet poll cannot reach it. History is never reconstructed from the
// fleet view.
//
// Enforced invariants (mirrored in SPRINT6A-IMPLEMENTATION.md):
//
//   - Boundary. A row belongs to exactly one owner and one employee; a row that
//     disagrees with the block it arrived in is dropped and the drop reported.
//   - Lineage. Retries group only by a native explicit run identity scoped to
//     owner and employee. A mission_ref is a mission name, not a retry identity.
//     Records with no native run id are listed individually and say so, and
//     exact same-key retries render once.
//   - Clocks. Source observation, Board receipt and native execution/acceptance
//     times are separate labelled clauses; none replaces another.
//   - Freshness binds the captured detail evaluation clock: the instant the
//     server evaluated this projection (detailView.observed_at), never the held
//     fleet artifact or browser clock. Only a successful or partial collection
//     can be fresh at all; a semantic disagreement shows both verdicts.
//   - History is history. No row feeds current placement, condition, value or
//     burn. Absent optional facts read "not reported", an unknown explicit
//     status stays unknown, and same-time conflicts stay marked.
//   - Boundedness. The cap is tested before a record is appended, so the shown
//     count never overshoots. Last-known-success is claimed only when appended.
//   - Safety. Every node is createElement + textContent. Evidence is opaque
//     allowlisted identifiers printed as inert text only: no anchor, href,
//     path, URL, file or fetch endpoint. Board task/review links have no native
//     employee association and stay explicitly unavailable.
const cockpitHistoryJS = `
    var MC_HISTORY_LIMIT = 120;

    // The only evidence references history prints. Anything else is inert text
    // labelled rejected: no scheme here can address a path or a host.
    var MC_HISTORY_EVIDENCE = /^(?:sha256|artifact|run|cycle|board):[A-Za-z0-9][A-Za-z0-9._-]{0,254}$/;

    // MC_LINEAGE_SEP joins owner, employee and native run id into one scoped
    // lineage key; it cannot occur inside those values.
    var MC_LINEAGE_SEP = " >> ";

    // detailEvaluationNow is the captured detail evaluation clock: the instant
    // the server evaluated this managerial projection. Every freshness
    // statement in history is measured against it. heldServerNow() describes a
    // different artifact and is deliberately not used here.
    function detailEvaluationNow() {
      var view = cockpit.detailView;
      return view ? parseInstant(view.observed_at) : NaN;
    }

    function detailEvaluationText() {
      var view = cockpit.detailView;
      return textOr(view && view.observed_at, MC_UNKNOWN);
    }

    // Reads only the captured detail response. Rejected counts the rows the
    // boundary guard refused, so a dropped row is reported, not vanished.
    function historyBlocksFor(employeeID) {
      var view = cockpit.detailView;
      if (!view || !view.sources || !Array.isArray(view.sources.owners)) return null;
      if (view.sources.employee_id !== employeeID) return null;
      var blocks = [];
      for (var i = 0; i < view.sources.owners.length; i++) {
        var owner = view.sources.owners[i];
        if (!owner) continue;
        var recent = Array.isArray(owner.recent) ? owner.recent : [];
        var seen = {};
        var rows = [];
        var rejected = 0;
        for (var j = 0; j < recent.length; j++) {
          var fo = recent[j];
          var obs = fo && fo.observation;
          if (!obs || obs.employee_id !== employeeID || obs.owner !== owner.owner) { rejected++; continue; }
          var key = textOr(fo.observation_id, "");
          if (key === "" || Object.prototype.hasOwnProperty.call(seen, key)) { rejected++; continue; }
          seen[key] = true;
          rows.push(fo);
        }
        // Last-known-success may sit outside the bounded recent window. It is
        // carried as a separate, separately labelled candidate; it is claimed
        // only where it is actually appended.
        var extra = null;
        var success = owner.last_known_success;
        var sobs = success && success.observation;
        var sid = textOr(success && success.observation_id, "");
        if (sobs && sobs.employee_id === employeeID && sobs.owner === owner.owner && sid !== "" &&
            !Object.prototype.hasOwnProperty.call(seen, sid)) {
          extra = success;
        }
        if (rows.length === 0 && extra === null && rejected === 0) continue;
        blocks.push({ owner: owner, rows: rows, outsideWindowSuccess: extra, rejected: rejected });
      }
      return blocks;
    }

    // historyLineageKey is the one identity a record may be grouped by: its own
    // native governed-run id, scoped to this owner and this employee. A mission
    // reference is a mission name and is never a retry identity here.
    function historyLineageKey(owner, employeeID, obs) {
      var dims = obs && obs.dimensions ? obs.dimensions : {};
      var run = dims.governed_run;
      if (!run || textOr(run.run_id, "") === "") return "";
      return textOr(owner, MC_UNKNOWN) + MC_LINEAGE_SEP + textOr(employeeID, MC_UNKNOWN) +
        MC_LINEAGE_SEP + "run:" + textOr(run.run_id, "");
    }

    function historyLineageLabel(owner, employeeID, obs) {
      var run = obs && obs.dimensions && obs.dimensions.governed_run;
      return "Lineage: owner " + textOr(owner, MC_UNKNOWN) + ", employee " + textOr(employeeID, MC_UNKNOWN) +
        ", native run " + textOr(run && run.run_id, MC_UNKNOWN);
    }

    // Records in the captured window keep the order the server returned them.
    // Consecutive records sharing one native lineage form one group; a lineage
    // that reappears later is marked as a continuation rather than reordered.
    function historyGroups(entry, employeeID) {
      var groups = [];
      var appeared = {};
      for (var i = 0; i < entry.rows.length; i++) {
        var obs = entry.rows[i].observation;
        var key = historyLineageKey(entry.owner.owner, employeeID, obs);
        var last = groups.length > 0 ? groups[groups.length - 1] : null;
        if (key !== "" && last && last.key === key) {
          last.records.push(entry.rows[i]);
          continue;
        }
        groups.push({
          key: key,
          label: key === "" ? "" : historyLineageLabel(entry.owner.owner, employeeID, obs),
          continued: key !== "" && appeared[key] === true,
          records: [entry.rows[i]]
        });
        if (key !== "") appeared[key] = true;
      }
      return groups;
    }

    // The client side of the canonical ComputedFreshness rule in
    // internal/operations/fleet.go: a collection that did not succeed or partly
    // succeed carries no fresh data, so it is unknown whatever its timestamp
    // says. Checked before age is looked at at all.
    var MC_COLLECTED = { success: true, partial: true };

    // Recomputed from the record's own collection result, observed_at and owner
    // max age against the captured detail evaluation clock. A server value is
    // never the verdict; malformed input fails closed to unknown.
    function historyFreshness(fo) {
      var obs = fo && fo.observation;
      if (!obs) return MC_UNKNOWN;
      var result = typeof obs.collection_result === "string" ? obs.collection_result : "";
      if (!Object.prototype.hasOwnProperty.call(MC_COLLECTED, result)) return MC_UNKNOWN;
      var at = parseInstant(obs.observed_at);
      var maxAge = fo.max_age_seconds;
      var evaluated = detailEvaluationNow();
      if (!isFinite(at) || !isFinite(evaluated)) return MC_UNKNOWN;
      if (typeof maxAge !== "number" || !isFinite(maxAge) || maxAge < 0) return MC_UNKNOWN;
      if (at > evaluated) return MC_UNKNOWN;
      return evaluated <= at + maxAge * 1000 ? "fresh" : "stale";
    }

    // Verdicts compare as verdicts, not strings: "Unknown" and "unknown" are one
    // statement, and printing them as a conflict would invent one.
    function freshnessVerdict(value) {
      var text = typeof value === "string" ? value.trim().toLowerCase() : "";
      return text === "fresh" || text === "stale" ? text : "unknown";
    }

    // Both verdicts are shown when they genuinely differ; picking one silently
    // would hide a real contradiction.
    function historyFreshnessText(fo) {
      var recomputed = historyFreshness(fo);
      // optionalText, not textOr: an absent carried freshness is an absence,
      // and textOr would assert "Unknown" and then read as a disagreement.
      var carried = optionalText(fo && fo.computed_freshness);
      var text = "Freshness " + recomputed + ", recomputed against the captured detail evaluation clock " +
        detailEvaluationText();
      if (carried !== "" && freshnessVerdict(carried) !== freshnessVerdict(recomputed)) {
        text += "; the captured response carried " + carried + " instead, and the disagreement is not resolved here";
      }
      return text + ".";
    }

    function historyBool(value) {
      if (value === true) return "yes";
      if (value === false) return "no";
      return MC_UNKNOWN;
    }

    function historyResultLabel(result) {
      var text = textOr(result, "");
      if (text === "success") return "Collected";
      if (text === "partial") return "Partially collected";
      if (text === "failed") return "Collection failed";
      if (text === "unavailable") return "Collection unavailable";
      if (text === "unknown") return "Collection result unknown";
      return "Collection result " + textOr(result, MC_UNKNOWN);
    }

    // The observation clock and the Board receipt clock, stated separately.
    function historyTimeClauses(fo) {
      var obs = fo && fo.observation;
      return "Source observed " + textOr(obs && obs.observed_at, MC_UNKNOWN) +
        ". Board received " + textOr(fo && fo.received_at, "not reported") + ".";
    }

    // Native execution and acceptance clocks live inside the dimensions and are
    // labelled as themselves; none of them is the observation clock.
    function historyMissionClauses(m) {
      var parts = ["mission reference " + textOr(m.mission_ref, MC_UNKNOWN) +
        " (a mission name, not a retry identity)", "paused " + historyBool(m.paused)];
      if (textOr(m.last_outcome, "") !== "") parts.push("last outcome " + textOr(m.last_outcome, MC_UNKNOWN));
      if (m.assignment && (m.assignment.active === true || m.assignment.active === false)) {
        parts.push("assignment " + (m.assignment.active ? "active" : "inactive") +
          (textOr(m.assignment.ref, "") !== "" ? " (" + textOr(m.assignment.ref, MC_UNKNOWN) + ")" : ""));
      }
      if (m.cadence && (m.cadence.enabled === true || m.cadence.enabled === false)) {
        parts.push("cadence " + (m.cadence.enabled ? "enabled" : "disabled"));
      }
      if (m.human_gate) {
        parts.push("human gate " + textOr(m.human_gate.title, MC_UNKNOWN) + " (" +
          textOr(m.human_gate.decision_id, MC_UNKNOWN) + ", blocking " + historyBool(m.human_gate.blocking) + ")");
      }
      if (m.blocker && m.blocker.active === true) parts.push("blocker " + textOr(m.blocker.description, "reported"));
      if (m.limitation && m.limitation.active === true) parts.push("limitation " + textOr(m.limitation.description, "reported"));
      if (Array.isArray(m.cycles)) {
        for (var i = 0; i < m.cycles.length; i++) {
          var c = m.cycles[i];
          parts.push("cycle " + textOr(c.cycle_id, MC_UNKNOWN) + ": delivered " + historyBool(c.delivered) +
            ", accepted " + historyBool(c.accepted) +
            (textOr(c.value, "") !== "" ? ", value " + textOr(c.value, MC_UNKNOWN) : "") +
            ", cycle execution time " + textOr(c.observed_at, "not reported") +
            ", acceptance time " + (c.accepted === true ? textOr(c.accepted_at, "not reported") : "not accepted"));
        }
      }
      return parts;
    }

    function historyDimensionClauses(obs) {
      var dims = obs && obs.dimensions ? obs.dimensions : {};
      var parts = [];
      if (dims.governed_run) {
        parts.push("native run " + textOr(dims.governed_run.run_id, MC_UNKNOWN) +
          ", run status " + textOr(dims.governed_run.run_status, "not reported") +
          ", outcome " + textOr(dims.governed_run.outcome_id, "not reported"));
      }
      if (dims.mission) parts = parts.concat(historyMissionClauses(dims.mission));
      if (dims.commissioning) {
        parts.push("lifecycle " + textOr(dims.commissioning.lifecycle_state, MC_UNKNOWN) +
          ", commissioned " + historyBool(dims.commissioning.commissioned) +
          ", ready " + historyBool(dims.commissioning.ready) +
          ", commissioned at " + textOr(dims.commissioning.commissioned_at, "not reported"));
      }
      if (dims.schedule) {
        parts.push("schedule enabled " + historyBool(dims.schedule.enabled) +
          ", next fire " + textOr(dims.schedule.next_fire, "not reported") +
          ", cron " + textOr(dims.schedule.cron_expr, "not reported"));
      }
      if (dims.process) {
        parts.push("process live " + historyBool(dims.process.live) +
          ", status " + textOr(dims.process.status_text, "not reported"));
      }
      if (dims.board) {
        parts.push("board counts: active tasks " + countText(dims.board.active_tasks) +
          ", review tasks " + countText(dims.board.review_tasks) +
          ", blocked tasks " + countText(dims.board.blocked_tasks) +
          ", stale leases " + countText(dims.board.stale_leases) +
          ", last event " + textOr(dims.board.last_event_at, "not reported") +
          "; individual task and review history is unavailable here because this response carries no native " +
          "employee-to-task association, and no task is inferred from a name");
      }
      return parts;
    }

    // Evidence is inert text. No history reference ever becomes an anchor, an
    // href or any other addressable target.
    function historyEvidenceText(uri) {
      var text = textOr(uri, MC_UNKNOWN);
      if (!MC_HISTORY_EVIDENCE.test(text)) {
        return "rejected non-opaque reference, shown as text only: " + text;
      }
      return text;
    }

    function historyRecordNode(fo, entry, note) {
      var obs = fo && fo.observation;
      var node = el("div", "mc-history-record");
      var conflicts = Array.isArray(entry.owner.conflict_observation_ids) ? entry.owner.conflict_observation_ids : [];
      var conflicted = conflicts.indexOf(textOr(fo && fo.observation_id, "")) >= 0;
      node.appendChild(el("p", null, historyResultLabel(obs && obs.collection_result) +
        " - history only, at its own source time. " + historyFreshnessText(fo) +
        (conflicted ? " This record shares an instant with a contradicting record from the same owner; both are kept." : "")));
      node.appendChild(el("p", "mc-count", historyTimeClauses(fo)));
      if (note) node.appendChild(el("p", "mc-count", note));
      if (obs && obs.error) {
        node.appendChild(el("p", "mc-count", "Error receipt " + textOr(obs.error.code, MC_UNKNOWN) + ": " +
          textOr(obs.error.message, MC_UNKNOWN)));
      }
      var dims = historyDimensionClauses(obs);
      if (dims.length > 0) node.appendChild(el("p", null, dims.join(". ") + "."));
      node.appendChild(el("p", "mc-count", "observation " + textOr(fo && fo.observation_id, MC_UNKNOWN) +
        ", producer " + textOr(fo && fo.producer_principal, MC_UNKNOWN) + "."));
      if (obs && Array.isArray(obs.evidence) && obs.evidence.length > 0) {
        var line = el("p", "mc-count", "Evidence (opaque references, not retrievable from here): ");
        for (var i = 0; i < obs.evidence.length; i++) {
          var ref = obs.evidence[i] || {};
          line.appendChild(el("span", null, (i > 0 ? "; " : "") + historyEvidenceText(ref.uri) +
            " (owner asserts: " + textOr(ref.verification_status, MC_UNKNOWN) +
            "; the Board did not independently verify this)"));
        }
        node.appendChild(line);
      }
      return node;
    }

    // Appends at most budget records and reports how many it appended, so the
    // cap is tested before each append and the shown count cannot overshoot.
    function historyOwnerBlock(entry, employeeID, budget) {
      var owner = entry.owner;
      var box = el("div", "mc-history-owner");
      box.appendChild(el("h4", null, textOr(owner.owner, MC_UNKNOWN) + " - current owner read state " +
        textOr(owner.status, MC_UNKNOWN) + (owner.same_time_conflict ? " - SAME-TIME CONFLICT" : "")));
      var used = 0;
      var capped = false;
      var groups = historyGroups(entry, employeeID);
      for (var i = 0; i < groups.length && !capped; i++) {
        var group = groups[i];
        var header = null;
        for (var j = 0; j < group.records.length; j++) {
          if (used >= budget) { capped = true; break; }
          if (header === null) {
            header = el("p", "mc-history-group", group.key === ""
              ? "No native run identity on this record: listed individually, never grouped by mission name."
              : group.label + (group.continued ? " (same lineage, continued below an unrelated record)" : ""));
            box.appendChild(header);
          }
          box.appendChild(historyRecordNode(group.records[j], entry, null));
          used++;
        }
      }
      var successShown = false;
      if (!capped && entry.outsideWindowSuccess && used < budget) {
        box.appendChild(historyRecordNode(entry.outsideWindowSuccess, entry,
          "Most recent known success for this owner, outside the bounded recent window and shown because of that."));
        used++;
        successShown = true;
      } else if (entry.outsideWindowSuccess) {
        capped = true;
      }
      if (used === 0) {
        box.appendChild(el("p", "mc-count", "No record from this owner survived the owner and employee boundary check."));
      }
      // Native cycle repair lineage and lifecycle assertions, derived from the
      // same boundary-checked captured records and separately bounded. They are
      // placed after the records so they read as history about those records.
      historyOwnerLineage(box, entry, employeeID);
      if (entry.rejected > 0) {
        box.appendChild(el("p", "mc-count", entry.rejected +
          " record(s) offered under this owner were dropped as duplicate or as not belonging to this owner and employee."));
      }
      if (owner.truncated) {
        box.appendChild(el("p", "mc-count", "This owner has older records beyond the captured read window; they are not present in this response."));
      }
      if (owner.tied_records_truncated) {
        box.appendChild(el("p", "mc-count", "A same-instant group at the window cutoff was cut by the finite row cap; not every record sharing that instant is present."));
      }
      return { node: box, used: used, capped: capped, successShown: successShown };
    }

    function detailHistory(list, employeeID) {
      var blocks = historyBlocksFor(employeeID);
      if (blocks === null) {
        definition(list, "History",
          "Unavailable: no live detail read was adopted for this employee, so no captured history exists. " +
          "History is never reconstructed from a newer fleet view.");
        return;
      }
      var box = el("div", "mc-history");
      box.appendChild(el("p", "mc-count",
        "Historical records from the captured detail read only. Nothing below changes the current placement, " +
        "condition, latest accepted value or attempt count above, and an older failure never becomes current."));
      var shown = 0;
      var capped = false;
      var successShown = false;
      for (var i = 0; i < blocks.length; i++) {
        if (shown >= MC_HISTORY_LIMIT) { capped = true; break; }
        var built = historyOwnerBlock(blocks[i], employeeID, MC_HISTORY_LIMIT - shown);
        box.appendChild(built.node);
        shown += built.used;
        if (built.successShown) successShown = true;
        if (built.capped) { capped = true; break; }
      }
      if (shown === 0 && !capped) {
        definition(list, "History", "No accepted observation history is recorded for this employee in the captured detail read.");
        return;
      }
      box.appendChild(el("p", "mc-count", capped
        ? "History is bounded and incomplete: " + shown + " record(s) shown, and the finite cap of " +
          MC_HISTORY_LIMIT + " stopped this list before every captured record was rendered."
        : "History is bounded: " + shown + " record(s) shown, every record the captured read returned." +
          (successShown ? " A most-recent-known-success older than a bounded window is included above and labelled as such." : "")));
      definition(list, "History", box);
    }
`
