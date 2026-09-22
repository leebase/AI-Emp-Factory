package httpapi

// cockpitHistoryLifecycleJS renders the lifecycle assertion half of the
// per-owner history sections defined in cockpit_history_lineage.go, split out
// only to keep each file inside the source budget. The invariants recorded
// there govern both halves; in particular lifecycle is assertion, never an
// inferred event, and a same-instant disagreement is a contradiction that
// keeps every side.
//
// Sprint 6B1 repacket: presentation folding of repeated assertions is removed
// outright. Every captured record is a separate timestamped owner assertion,
// so no timestamp can be hidden from contradiction matching by a fold.
const cockpitHistoryLifecycleJS = `
    function lifecycleAssertionText(a) {
      return "Owner asserted lifecycle state " + a.state + ", commissioned " + historyBool(a.commissioned) +
        ", ready " + historyBool(a.ready) + ", owner-stated commissioning time " +
        (a.commissionedAt === "" ? "not reported" : a.commissionedAt) +
        ". Asserted at source observation time " + a.sourceObservedAt + ", observation " + a.observationID + ".";
    }

    function lifecycleAssertionsEqual(a, b) {
      return a.state === b.state && a.commissioned === b.commissioned &&
        a.ready === b.ready && a.commissionedAt === b.commissionedAt;
    }

    function historyLifecycleAssertions(entry, employeeID) {
      var out = [];
      var rows = historyLineageRows(entry);
      for (var i = 0; i < rows.length; i++) {
        var fo = rows[i];
        var obs = fo && fo.observation;
        if (!obs || obs.employee_id !== employeeID || obs.owner !== entry.owner.owner) continue;
        var life = obs.dimensions && obs.dimensions.commissioning;
        if (!life) continue;
        out.push({
          state: textOr(life.lifecycle_state, MC_UNKNOWN),
          commissioned: life.commissioned,
          ready: life.ready,
          commissionedAt: optionalText(life.commissioned_at),
          sourceObservedAt: textOr(obs.observed_at, MC_UNKNOWN),
          sourceInstant: parseInstant(obs.observed_at),
          observationID: textOr(fo.observation_id, MC_UNKNOWN),
          conflicted: false
        });
      }
      // No folding. An identical repeat at a different observation id or time
      // is a separate source assertion and is rendered as one, so every
      // timestamp reaches contradiction matching below.
      markLifecycleConflicts(out);
      return out;
    }

    // Contradictions are searched across the whole bounded capture, not only
    // between neighbours: an owner may return to an instant with an unrelated
    // assertion in between, and that is still one instant with two states.
    // Marking is symmetric and order-independent: every row on either side of
    // a same-instant disagreement is marked, and no state is preferred.
    function markLifecycleConflicts(rows) {
      for (var i = 0; i < rows.length; i++) {
        if (!isFinite(rows[i].sourceInstant)) continue;
        for (var j = i + 1; j < rows.length; j++) {
          if (rows[i].sourceInstant !== rows[j].sourceInstant) continue;
          if (lifecycleAssertionsEqual(rows[i], rows[j])) continue;
          rows[i].conflicted = true;
          rows[j].conflicted = true;
        }
      }
    }

    function historyLifecycle(entry, employeeID) {
      var assertions = historyLifecycleAssertions(entry, employeeID);
      if (assertions.length === 0) return null;
      var box = el("div", "mc-history-lifecycle");
      box.appendChild(el("h5", null, "Lifecycle assertions"));
      box.appendChild(el("p", "mc-count",
        "Each line is what this owner asserted at its own source observation time. The Board does not infer " +
        "commissioned, abandoned or superseded state, and a difference between two assertions is a change of " +
        "assertion rather than an observed lifecycle event."));
      var shown = 0;
      for (var i = 0; i < assertions.length; i++) {
        if (shown >= MC_LIFECYCLE_LIMIT) break;
        var a = assertions[i];
        box.appendChild(el("p", null, lifecycleAssertionText(a) +
          (a.conflicted ? " This assertion shares a source instant with a different assertion from the same owner; both are kept and neither is preferred." : "")));
        shown++;
      }
      box.appendChild(el("p", "mc-count", shown < assertions.length
        ? "Lifecycle history is bounded and incomplete: " + shown + " of " + assertions.length +
          " source assertion(s) rendered before the finite cap of " + MC_LIFECYCLE_LIMIT + "."
        : "Lifecycle history is bounded: " + shown +
          " source assertion(s), every one the captured read carried for this owner."));
      return box;
    }

`
