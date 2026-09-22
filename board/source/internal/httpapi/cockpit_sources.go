package httpapi

// cockpitSourcesJS renders the source-owner, receipt and evidence half of
// employee detail.
//
// Everything here is read from the artifact held by the reading session, at the
// vintage that session captured, and every age is measured against that
// artifact's own clock. That is what keeps a per-owner freshness line honest:
// it describes the owners of the snapshot Lee is holding, not the owners of
// whatever has arrived since.
//
// Only an http or https evidence reference becomes a link. Any other scheme is
// printed as text, so a javascript:, data: or file: URI is never clickable.
const cockpitSourcesJS = `
    function sourceRefText(sources) {
      if (!Array.isArray(sources) || sources.length === 0) return "no source reference";
      var parts = [];
      for (var i = 0; i < sources.length; i++) {
        parts.push(textOr(sources[i].owner, MC_UNKNOWN) + " " + textOr(sources[i].observation_id, MC_UNKNOWN) +
          " observed " + heldTime(sources[i].observed_at));
      }
      return parts.join("; ");
    }

    // Per-owner read state comes from the held artifact's own source entries,
    // which already carry status, recomputed freshness, observation id and
    // expiry. Nothing here is derived from the fleet's current currency.
    function detailOwners(list, employeeID) {
      var sources = heldRows("sources", employeeID);
      var box = el("div");
      for (var i = 0; i < sources.length; i++) {
        var source = sources[i];
        box.appendChild(el("p", null, textOr(source.owner, MC_UNKNOWN) + ": status " + textOr(source.status, MC_UNKNOWN) +
          ", freshness " + textOr(source.computed_freshness, MC_UNKNOWN) +
          ", observed " + (source.observed_at ? heldTime(source.observed_at) : MC_UNKNOWN) +
          (source.expires_at ? ", fresh until " + source.expires_at : ", no freshness window") +
          (source.same_time_conflict ? " - SAME-TIME CONFLICT" : "")));
      }
      definition(list, "Source owners", sources.length > 0 ? box : "No owner read state was recorded.");
      var errors = heldRows("errors", employeeID);
      var errorBox = el("div");
      for (var j = 0; j < errors.length; j++) {
        errorBox.appendChild(el("p", null, textOr(errors[j].owner, MC_UNKNOWN) + " " + textOr(errors[j].code, MC_UNKNOWN) +
          ": " + textOr(errors[j].message, MC_UNKNOWN)));
      }
      // An empty receipt list is the absence of receipts and nothing more.
      // Reading it as proof of freshness would let this line contradict the
      // stale and unknown owners printed immediately above it.
      definition(list, "Collection receipts", errors.length > 0
        ? errorBox
        : "No collection error receipt was recorded for this employee. That is an absence of receipts only, " +
          "and is not evidence about owner freshness; the per-owner read state above is the only statement of freshness here.");
    }

    var MC_EVIDENCE_LIMIT = 20;

    function detailEvidence(list, employeeID) {
      var mine = heldRows("evidence", employeeID);
      var box = el("div", "mc-secondary");
      var limit = Math.min(mine.length, MC_EVIDENCE_LIMIT);
      for (var j = 0; j < limit; j++) {
        var reference = mine[j].reference || {};
        var line = el("p", null, textOr(reference.owner, MC_UNKNOWN) + " (" +
          textOr(reference.verification_status, MC_UNKNOWN) + "): ");
        line.appendChild(evidenceNode(reference.uri));
        box.appendChild(line);
      }
      if (mine.length > limit) {
        box.appendChild(el("p", "mc-count", "Showing " + limit + " of " + mine.length + " evidence references."));
      }
      definition(list, "Evidence", mine.length > 0 ? box : "No evidence reference was recorded.");
      var advanced = el("button", "mc-detail-close", "Open Advanced board view");
      advanced.type = "button";
      advanced.id = "employee-detail-advanced";
      definition(list, "Advanced", advanced);
    }
`
