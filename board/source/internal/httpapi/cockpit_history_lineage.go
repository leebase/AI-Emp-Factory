package httpapi

// cockpitHistoryLineageJS renders two additive per-owner history sections
// inside employee detail: native mission cycle repair lineage, and explicit
// lifecycle assertion history. Both read the same captured managerial detail
// response the rest of history reads (cockpit.detailView), captured once at
// open, so a newer fleet poll cannot reach them.
//
// Enforced invariants (mirrored in the Sprint 6B record):
//
//   - Native identity only. A repair edge exists when, and only when, a native
//     cycle fact carries repair_of naming another native cycle_id seen under
//     the same owner and the same employee in this captured window. A mission
//     reference, an agent id, mission text or any inferred identity is never a
//     lineage edge here.
//   - Unknown beats invented. A dangling parent, a malformed cycle reference, a
//     repair chain that loops, and contradicting same-instant revisions of one
//     cycle_id all render as explicitly unknown or conflicted. No such case is
//     ever resolved into a delivered or accepted outcome.
//   - Clocks stay apart. Source revision observation time, Board receipt time,
//     cycle execution time and acceptance time are four separately labelled
//     clauses. None of them stands in for another, and an acceptance time is
//     printed only where the owner explicitly asserted accepted=true.
//   - Supersession is revision, not erasure. For one cycle_id the newest source
//     revision is the current statement; earlier revisions are retained and
//     labelled as earlier source revisions rather than deleted or merged.
//   - Lifecycle is assertion. Each line is what this owner asserted at its own
//     source observation time. The Board does not infer commissioned, abandoned
//     or superseded state, and a difference between two assertions is a change
//     of assertion, not an observed lifecycle event.
//   - Boundedness. Cycle count, chain depth and lifecycle count are capped, and
//     each cap is tested before the append it guards.
//   - Safety. Every node is createElement plus textContent. Nothing here emits
//     an anchor, an href, a path, a URL or any navigable target.
const cockpitHistoryLineageJS = `
    var MC_CYCLE_LIMIT = 60;
    var MC_CHAIN_DEPTH_LIMIT = 12;
    var MC_LIFECYCLE_LIMIT = 40;

    // The only shape a native cycle identity may take here. Anything else is
    // refused as an identity and printed as inert text, so a hostile string can
    // never become a lineage key or a rendered link.
    var MC_CYCLE_ID = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;

    // Every record this owner block already accepted, in the order the server
    // returned it, including the separately labelled outside-window success.
    // Reusing the boundary-checked rows is deliberate: lineage may never read a
    // record the owner and employee boundary refused.
    function historyLineageRows(entry) {
      var rows = entry.rows.slice();
      if (entry.outsideWindowSuccess) rows.push(entry.outsideWindowSuccess);
      return rows;
    }

    // Execution semantics are part of the fact. Two revisions that agree on
    // delivery but name different acceptance or execution instants disagree
    // about when the work happened, which is a contradiction, not a preference.
    function cycleFactsEqual(a, b) {
      return a.delivered === b.delivered && a.accepted === b.accepted &&
        a.value === b.value && a.repairOf === b.repairOf &&
        a.acceptedAt === b.acceptedAt && a.cycleObservedAt === b.cycleObservedAt;
    }

    // Collects native cycle facts, scoped again to this owner and employee, and
    // groups them by native cycle_id. Malformed identities are counted and kept
    // out of the graph entirely; they cannot become nodes or parents.
    function historyCycleIndex(entry, employeeID) {
      var index = { order: [], byID: {}, malformed: [], rejected: 0 };
      var rows = historyLineageRows(entry);
      for (var i = 0; i < rows.length; i++) {
        var fo = rows[i];
        var obs = fo && fo.observation;
        if (!obs || obs.employee_id !== employeeID || obs.owner !== entry.owner.owner) { index.rejected++; continue; }
        var mission = obs.dimensions && obs.dimensions.mission;
        var cycles = mission && Array.isArray(mission.cycles) ? mission.cycles : [];
        for (var j = 0; j < cycles.length; j++) {
          var c = cycles[j] || {};
          // optionalText, not textOr: an absent cycle id is an absence, and
          // textOr would substitute the literal Unknown, which would pass the
          // identifier test and become a fabricated cycle node and a parent.
          var id = optionalText(c.cycle_id);
          if (!MC_CYCLE_ID.test(id)) {
            if (index.malformed.length < MC_CYCLE_LIMIT) index.malformed.push(id === "" ? MC_UNKNOWN : id);
            continue;
          }
          // Same reason: an absent repair_of means this cycle declares no
          // repair at all, never a repair of a cycle called Unknown.
          var parent = optionalText(c.repair_of);
          var revision = {
            id: id,
            repairOf: parent,
            repairOfMalformed: parent !== "" && !MC_CYCLE_ID.test(parent),
            delivered: c.delivered,
            accepted: c.accepted,
            value: textOr(c.value, ""),
            acceptedAt: optionalText(c.accepted_at),
            cycleObservedAt: optionalText(c.observed_at),
            sourceObservedAt: textOr(obs.observed_at, MC_UNKNOWN),
            sourceInstant: parseInstant(obs.observed_at),
            receivedAt: optionalText(fo.received_at),
            observationID: textOr(fo.observation_id, MC_UNKNOWN)
          };
          if (!Object.prototype.hasOwnProperty.call(index.byID, id)) {
            index.byID[id] = { id: id, revisions: [] };
            index.order.push(id);
          }
          index.byID[id].revisions.push(revision);
        }
      }
      for (var k = 0; k < index.order.length; k++) resolveCycleNode(index.byID[index.order[k]]);
      return index;
    }

    // The newest source revision is the current statement. Two revisions that
    // share the newest source instant and disagree leave the cycle conflicted:
    // conflicted cycles assert no outcome at all, and never an accepted one.
    function resolveCycleNode(node) {
      var current = null;
      var conflicted = false;
      var undated = 0;
      for (var i = 0; i < node.revisions.length; i++) {
        var rev = node.revisions[i];
        if (!isFinite(rev.sourceInstant)) { undated++; continue; }
        if (current === null || rev.sourceInstant > current.sourceInstant) { current = rev; conflicted = false; continue; }
        if (rev.sourceInstant === current.sourceInstant && !cycleFactsEqual(rev, current)) conflicted = true;
      }
      node.current = current;
      node.conflicted = conflicted || current === null;
      node.undated = undated;
      node.twins = current === null ? [] : cycleTwins(node, current);
      node.superseded = current === null ? 0 : node.revisions.length - node.twins.length - undated;
      if (node.superseded < 0) node.superseded = 0;
    }

    // Every revision sharing the newest source instant, the current statement
    // included. A conflicted cycle has more than one, and none of them may be
    // preferred over another.
    function cycleTwins(node, current) {
      var twins = [];
      for (var i = 0; i < node.revisions.length; i++) {
        var rev = node.revisions[i];
        if (isFinite(rev.sourceInstant) && rev.sourceInstant === current.sourceInstant) twins.push(rev);
      }
      return twins;
    }

    // A parent link only exists when the named cycle is present in this
    // captured window under this same owner and employee.
    function cycleParentID(index, node) {
      var current = node.current;
      // A conflicted cycle selects no preferred edge: preferring one twin's
      // repair_of would nest it under an arbitrarily chosen parent.
      if (!current || node.conflicted || current.repairOf === "" || current.repairOfMalformed) return "";
      return Object.prototype.hasOwnProperty.call(index.byID, current.repairOf) ? current.repairOf : "";
    }

    function cycleRepairClause(index, node) {
      var current = node.current;
      if (!current) return "repair relation unknown: no dated source revision of this cycle is present";
      if (node.conflicted) {
        var raw = [];
        for (var t = 0; t < node.twins.length; t++) {
          raw.push(node.twins[t].repairOf === "" ? "no repair declared" : node.twins[t].repairOf);
        }
        return "repair relation conflicted: contradicting source revisions share this cycle id at one source " +
          "instant, so the parent link is unknown here and no depth is asserted; the raw repair_of assertions " +
          "are kept as inert text only: " + raw.join("; ");
      }
      if (current.repairOf === "") return "not declared a repair of any cycle";
      if (current.repairOfMalformed) {
        return "declares a repair of a malformed cycle reference, shown as inert text only: " +
          current.repairOf + "; the parent link is unknown and no lineage is inferred from it";
      }
      if (!Object.prototype.hasOwnProperty.call(index.byID, current.repairOf)) {
        return "declares a repair of cycle " + current.repairOf +
          ", which is not present in this captured window, so the parent link is unknown here";
      }
      return "a declared repair of cycle " + current.repairOf;
    }

    function cycleOutcomeClause(node) {
      if (node.conflicted) {
        return "Outcome conflicted: this owner published contradicting facts for this cycle id, so no delivered " +
          "or accepted outcome is asserted here";
      }
      var current = node.current;
      var text = "Delivered " + historyBool(current.delivered) + ", accepted " + historyBool(current.accepted);
      if (current.accepted === true) {
        text += current.value !== "" ? ", value " + current.value : ", value not reported";
      }
      return text;
    }

    // Four clocks, four labels. An acceptance time is only ever printed for an
    // explicitly accepted cycle; otherwise the clause says it was not accepted.
    function cycleClockClause(node) {
      var current = node.current;
      if (!current) return "No dated source revision, so no clock is reported.";
      if (node.conflicted) return conflictedClockClause(node);
      return "Source revision observed " + current.sourceObservedAt +
        "; Board received " + (current.receivedAt === "" ? "not reported" : current.receivedAt) +
        "; cycle execution time " + (current.cycleObservedAt === "" ? "not reported" : current.cycleObservedAt) +
        "; acceptance time " + (current.accepted === true
          ? (current.acceptedAt === "" ? "not reported" : current.acceptedAt)
          : "not accepted") +
        "; carried by observation " + current.observationID + ".";
    }

    // A conflicted cycle certifies no clock and no value. Source revision and
    // receipt metadata survive only as raw individual assertions, each carrying
    // the conflict with it; execution and acceptance clocks are not reported at
    // all, because reporting one of them would be preferring one twin.
    function conflictedClockClause(node) {
      var parts = [];
      for (var i = 0; i < node.twins.length; i++) {
        var rev = node.twins[i];
        parts.push("source revision observed " + rev.sourceObservedAt + ", Board received " +
          (rev.receivedAt === "" ? "not reported" : rev.receivedAt) + ", observation " + rev.observationID);
      }
      return "Clocks conflicted: " + node.twins.length + " contradicting source revisions share one source " +
        "instant for this cycle id, so no execution or acceptance clock is asserted here. The contradicting " +
        "source revisions, as raw assertions only: " + parts.join("; ") + ".";
    }

    function cycleNode(index, node, depth) {
      var box = el("div", "mc-history-cycle");
      box.appendChild(el("p", null, "Cycle " + node.id + " (repair depth " +
        (node.conflicted ? "unknown" : depth) + "), " +
        cycleRepairClause(index, node) + ". " + cycleOutcomeClause(node) + "."));
      box.appendChild(el("p", "mc-count", cycleClockClause(node)));
      if (node.superseded > 0) {
        box.appendChild(el("p", "mc-count", node.superseded +
          " earlier source revision(s) of this cycle id are retained as history; the newest source revision is the " +
          "current statement and no earlier revision changes the current placement, condition or value above."));
      }
      if (node.undated > 0) {
        box.appendChild(el("p", "mc-count", node.undated +
          " revision(s) of this cycle carry no readable source observation time and are not ordered here."));
      }
      return box;
    }

    // Depth-first over native repair edges only, with an explicit path guard.
    // A chain that returns to a cycle already on the current path is reported
    // as a loop and stopped: a loop is a conflicted lineage, not an order.
    function appendCycleChain(box, index, id, depth, path, state) {
      if (state.used >= MC_CYCLE_LIMIT) { state.capped = true; return; }
      if (Object.prototype.hasOwnProperty.call(path, id)) {
        box.appendChild(el("p", "mc-count", "Repair chain loops back to cycle " + id +
          ": the native repair links contradict each other, so no repair order is asserted for this chain."));
        state.loops++;
        return;
      }
      if (depth > MC_CHAIN_DEPTH_LIMIT) {
        box.appendChild(el("p", "mc-count", "Repair chain is deeper than the rendering bound of " +
          MC_CHAIN_DEPTH_LIMIT + "; the remainder of this chain is not rendered here."));
        state.capped = true;
        return;
      }
      box.appendChild(cycleNode(index, index.byID[id], depth));
      state.used++;
      state.seen[id] = true;
      path[id] = true;
      for (var i = 0; i < index.order.length; i++) {
        var childID = index.order[i];
        if (childID === id) continue;
        if (cycleParentID(index, index.byID[childID]) !== id) continue;
        if (state.used >= MC_CYCLE_LIMIT) { state.capped = true; break; }
        appendCycleChain(box, index, childID, depth + 1, path, state);
      }
      delete path[id];
    }

    function historyCycleLineage(entry, employeeID) {
      var index = historyCycleIndex(entry, employeeID);
      if (index.order.length === 0 && index.malformed.length === 0) return null;
      var box = el("div", "mc-history-lineage");
      box.appendChild(el("h5", null, "Mission cycle repair lineage"));
      box.appendChild(el("p", "mc-count",
        "Links below come only from native cycle_id and repair_of facts published by this owner for this employee. " +
        "No link is inferred from a mission name, an agent id or any other text, and nothing below changes the " +
        "current placement, condition or latest accepted value above."));
      var state = { used: 0, capped: false, loops: 0, seen: {} };
      for (var i = 0; i < index.order.length && !state.capped; i++) {
        var id = index.order[i];
        if (state.seen[id] === true) continue;
        if (cycleParentID(index, index.byID[id]) !== "") continue;
        appendCycleChain(box, index, id, 0, {}, state);
      }
      // Anything still unreached has a present parent but was never reached
      // from a root: that is only possible inside a closed repair loop, so it
      // is reported as conflicted rather than silently dropped.
      var orphans = 0;
      for (var j = 0; j < index.order.length && !state.capped; j++) {
        var rest = index.order[j];
        if (state.seen[rest] === true) continue;
        if (orphans === 0) {
          box.appendChild(el("p", "mc-count",
            "The cycles below declare repairs that form a closed loop with no starting cycle, so their order is " +
            "unknown and conflicted; no repair order is asserted for them."));
        }
        orphans++;
        appendCycleChain(box, index, rest, 0, {}, state);
      }
      if (index.malformed.length > 0) {
        box.appendChild(el("p", "mc-count", index.malformed.length +
          " cycle fact(s) carried a cycle id that is not a native identifier and were refused as identities; " +
          "they are shown as inert text only: " + index.malformed.join("; ") + "."));
      }
      if (index.rejected > 0) {
        box.appendChild(el("p", "mc-count", index.rejected +
          " record(s) were refused by the owner and employee boundary before any cycle fact was read."));
      }
      box.appendChild(el("p", "mc-count", state.capped
        ? "Lineage is bounded and incomplete: " + state.used +
          " cycle(s) rendered before a finite rendering bound stopped the list."
        : "Lineage is bounded: " + state.used + " cycle(s) rendered, " + index.order.length +
          " native cycle id(s) present in this captured window, " + state.loops + " contradicting loop(s) reported."));
      return box;
    }

    // Both sections derive from the captured records of this owner block, which
    // include records the record cap above may not have rendered; they are
    // separately bounded and say so rather than borrowing that count.
    function historyOwnerLineage(box, entry, employeeID) {
      var lineage = historyCycleLineage(entry, employeeID);
      if (lineage) box.appendChild(lineage);
      var lifecycle = historyLifecycle(entry, employeeID);
      if (lifecycle) box.appendChild(lifecycle);
    }
`
