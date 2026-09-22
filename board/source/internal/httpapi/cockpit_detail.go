package httpapi

// cockpitDetailJS presents employee detail, attributed.
//
// The first screen is a staff brief, not a record dump. It answers four
// questions about one person, in short sentences, and nothing else:
//
//  1. What are they doing for me?
//  2. What useful progress is there - the last accepted result?
//  3. Do they need me? (a required decision gate only; an obstacle is a
//     separate, explicitly non-gate statement)
//  4. What is next, and when?
//
// Everything that used to be stacked into that view - rule identity, per-owner
// read state, contradictions, receipts, history, Board records, evidence
// references and the full vintage paragraph - still exists, unchanged, inside
// one collapsed native <details> disclosure. It is closed when the dialog
// opens, so no answer is buried under mechanics.
//
// Two different observations are on screen at once and they are never the same
// vintage: the live manager-derived detail read, and the artifact this reading
// session has held since it opened. Every answer carries a short qualifier
// saying which of the two it came from, and neither borrows the other's age.
// Artifact-derived rows are read from the held capture rather than from
// cockpit.state, so a fleet poll that lands while Lee is reading cannot change
// what a row says.
const cockpitDetailJS = `
    function definition(list, term, value) {
      list.appendChild(el("dt", null, term));
      if (value instanceof Node) {
        var wrap = el("dd");
        wrap.appendChild(value);
        list.appendChild(wrap);
        return;
      }
      list.appendChild(el("dd", null, value));
    }

    // heldCard is the card this reading session captured at open. With no
    // session open there is nothing to protect, so the live artifact is read
    // directly; that is the only path on which cockpit.state reaches a row.
    function heldCard(employeeID) {
      if (cockpit.held && cockpit.held.employeeID === employeeID) return cockpit.held.card;
      var cards = (cockpit.state && Array.isArray(cockpit.state.cards)) ? cockpit.state.cards : [];
      for (var i = 0; i < cards.length; i++) {
        if (cards[i].employee_id === employeeID) return cards[i];
      }
      return null;
    }

    function heldRows(kind, employeeID) {
      if (cockpit.held && cockpit.held.employeeID === employeeID) return cockpit.held[kind];
      return heldRowsFor(cockpit.state && cockpit.state[kind], employeeID);
    }

    // The two vintages, stated separately and never merged.
    function detailVintageText() {
      var view = cockpit.detailView;
      var live = view
        ? "Live detail read, observed " + relativeTime(view.observed_at) +
          " (" + textOr(view.observed_at, MC_UNKNOWN) + ")"
        : (cockpit.detailLoading
          ? "Live detail read: still in flight, nothing adopted yet"
          : "Live detail read: unavailable, so no live vintage exists");
      var held = cockpit.held || {};
      var age = held.generatedAt
        ? "generated " + heldTime(held.generatedAt) + " (" + held.generatedAt + ")"
        : "generation time unknown";
      var bound = held.validUntil
        ? ", stated valid until " + held.validUntil
        : ", with no stated validity";
      return live + ". Held artifact snapshot " + textOr(held.snapshotID, MC_UNKNOWN) + ", " + age + bound +
        ". Context, source-owner, receipt and evidence rows below are read from that held artifact at that " +
        "vintage, not from the live detail read, and they do not change while this dialog is open. The badge " +
        "above states whether the held artifact has since expired; close and reopen to read a newer one.";
    }

    // The short source qualifiers, in plain words. A reader needs to know
    // which of the two observations an answer came from and roughly how old it
    // is, and nothing else. The full vintage paragraph, snapshot id and stated
    // validity bound stay in the evidence disclosure.
    function heldSourceQualifier() {
      var held = cockpit.held || {};
      return held.generatedAt
        ? "Saved " + heldTime(held.generatedAt)
        : "Saved at an unknown time";
    }

    function liveSourceQualifier() {
      var view = cockpit.detailView;
      if (view) return "Status checked " + relativeTime(view.observed_at);
      return heldSourceQualifier() + "; status not checked";
    }

    // Assignment. A reference is an identifier, not a description of the work,
    // so it is reported as an assignment whose description this record does not
    // carry and the reference itself stays in evidence. Bench readiness is not
    // an assignment and never stands in for one.
    function answerWorkText(card) {
      if (!card) return "Unknown: this employee is not in the saved record, so nothing was checked.";
      var work = card.context && card.context.work;
      if (work) {
        var doing = work.kind === "governed_run" ? "Running a governed task" : "Working an assignment";
        // An unreported status is left unsaid rather than printed as the word
        // Unknown in the middle of a sentence about what they are doing.
        var status = textOr(work.status, "");
        if (status === MC_UNKNOWN) status = "";
        return doing + (status ? ", " + status : "") + "; its description is not provided.";
      }
      if (placementKey(card) === "on_the_bench") {
        var readiness = card.context && card.context.readiness;
        if (readiness && readiness.ready === true) return "Not assigned; ready for work.";
        if (readiness && readiness.ready === false) return "Not assigned; not ready for work.";
      }
      return "Unknown: nobody reported an assignment recently enough to trust.";
    }

    // The accepted result, and only that. The attempt count is effort rather
    // than a result, so it stays in evidence: a manager asking what came out of
    // this week does not need a lecture about activity to read the answer.
    function answerDeliveryText(subject) {
      var value = subject.latest_value;
      if (!value) return "Nothing has been accepted as finished in this record.";
      return textOr(value.value, MC_UNKNOWN) + ", accepted " + relativeTime(value.accepted_at) + ".";
    }

    // Only a recorded gate marked required is a claim on Lee's time. The empty
    // answer is scoped to this record on purpose: nothing here can know whether
    // a person needs him, only whether a decision was asked for. A finding is
    // never promoted into a gate.
    function answerGateText(subject) {
      var gate = subject.needs_lee;
      if (!gate) return "No decision requested in this record.";
      if (gate.required === true) {
        return "YES: " + textOr(gate.title, MC_UNKNOWN) + ", raised " + relativeTime(gate.raised_at) + ". " +
          textOr(gate.reason, "No reason recorded.");
      }
      return "No decision requested. A note is recorded but not required: " + textOr(gate.title, MC_UNKNOWN) + ".";
    }

    // The obstacle is a separate sentence from the gate answer and says so.
    // It is drawn from the same subject as the gate, so the two never mix
    // vintages under one qualifier. Empty means no obstacle was recorded, and
    // the line is then not shown at all rather than asserting an all-clear.
    function answerObstacleText(subject) {
      var parts = [];
      if (conditionKey(subject) === "burning") {
        parts.push("burning - " + textOr(subject.condition_reason && subject.condition_reason.message, "no reason recorded"));
      }
      if (Array.isArray(subject.contradictions) && subject.contradictions.length > 0) {
        parts.push(plural(subject.contradictions.length, "source contradiction"));
      }
      if (parts.length === 0) return "";
      return "Obstacle: " + parts.join("; ") + ". Recorded state only - nothing here asks you to decide.";
    }

    function answerNextText(card) {
      if (!card) return "Unknown: this employee is not in the saved record, so nothing was checked.";
      var checkpoint = card.context && card.context.checkpoint;
      if (!checkpoint) return "Unknown: no recent schedule was reported.";
      // An explicitly disabled schedule wins over a next_fire the record still
      // carries. Both fields are permitted and preserved by the accepted
      // contract, and a retained upcoming time on a switched-off schedule is a
      // leftover, not a run that is going to happen. Announcing it would be
      // this view inventing a future the record does not promise.
      if (checkpoint.enabled === false) {
        return checkpoint.next_fire
          ? "Schedule disabled: no next run. A previously scheduled time is still recorded; see evidence."
          : "Schedule disabled: no next run.";
      }
      if (checkpoint.next_fire) {
        // Relative first, then a clock a person reads. Both are the same
        // recorded instant; the exact instant stays in evidence.
        var at = parseInstant(checkpoint.next_fire);
        return isFinite(at)
          ? relativeTime(checkpoint.next_fire) + ", at " + new Date(at).toLocaleString()
          : relativeTime(checkpoint.next_fire);
      }
      if (checkpoint.enabled === true) return "Scheduled, but no next run time was observed.";
      return "Unknown: a schedule was reported but it states no next run.";
    }

    function answerBlock(host, question, value, qualifier, note) {
      var section = el("section", "mc-answer");
      section.appendChild(el("h4", "mc-answer-q", question));
      section.appendChild(el("p", "mc-answer-a", value));
      if (note) section.appendChild(el("p", "mc-answer-note", note));
      section.appendChild(el("p", "mc-answer-src", qualifier));
      host.appendChild(section);
    }

    // The first screen. Four questions, four short answers, each carrying the
    // one qualifier that says where it came from.
    function detailAnswers(card, subject) {
      var answers = el("div", "mc-answers");
      answerBlock(answers, "Working on", answerWorkText(card), heldSourceQualifier());
      answerBlock(answers, "Last useful result", answerDeliveryText(subject), liveSourceQualifier());
      answerBlock(answers, "Needs you", answerGateText(subject), liveSourceQualifier(), answerObstacleText(subject));
      answerBlock(answers, "Next", answerNextText(card), heldSourceQualifier());
      return answers;
    }

    // Everything below the answers, closed on open. The rows are the original
    // rows, built by the original functions from the original held and captured
    // data; only their place in the hierarchy changed.
    function detailEvidenceDisclosure(employeeID, card, subject) {
      var box = el("details", "mc-evidence");
      box.id = "employee-detail-evidence";
      var summary = el("summary", "mc-evidence-summary", "Show evidence");
      summary.id = "employee-detail-evidence-summary";
      box.appendChild(summary);
      var view = cockpit.detailView;
      var list = el("dl");
      definition(list, "Vintage", detailVintageText());
      definition(list, "Rows below are read from", view
        ? "the live detail read, for placement, condition, value, gate, contradiction and finding rows"
        : "the held artifact only; no live detail read was adopted for this employee");
      var placement = placementKey(subject);
      var condition = conditionKey(subject);
      definition(list, "Placement", MC_PLACEMENT_LABEL[placement] + " - " +
        textOr(subject.placement_reason && subject.placement_reason.message, "no reason recorded") +
        " [" + textOr(subject.placement_reason && subject.placement_reason.rule_id, MC_UNKNOWN) + " v" +
        textOr(subject.placement_reason && subject.placement_reason.rule_version, MC_UNKNOWN) + "] " +
        sourceRefText(subject.placement_reason && subject.placement_reason.sources));
      definition(list, "Condition", MC_CONDITION_LABEL[condition] + " - " +
        textOr(subject.condition_reason && subject.condition_reason.message, "no reason recorded") +
        " [" + textOr(subject.condition_reason && subject.condition_reason.rule_id, MC_UNKNOWN) + " v" +
        textOr(subject.condition_reason && subject.condition_reason.rule_version, MC_UNKNOWN) + "]");
      definition(list, "Latest accepted value", latestValueText(subject));
      definition(list, "Attempts since delivery", countText(subject.non_delivery_count) +
        (subject.delivery_truncated ? " (history truncated)" : ""));
      var gate = subject.needs_lee;
      definition(list, "Needs Lee", gate
        ? (gate.required === true ? "REQUIRED: " : "Recorded but not required: ") +
          textOr(gate.title, MC_UNKNOWN) + " - " + textOr(gate.reason, MC_UNKNOWN) +
          " (" + textOr(gate.decision_id, MC_UNKNOWN) + ", raised " + relativeTime(gate.raised_at) + ")"
        : "No decision gate recorded.");
      detailContext(list, card);
      detailList(list, "Contradictions", subject.contradictions, function (item) {
        return textOr(item.dimension_a, MC_UNKNOWN) + " vs " + textOr(item.dimension_b, MC_UNKNOWN) +
          ": " + textOr(item.description, MC_UNKNOWN);
      }, "No contradiction between sources was recorded.");
      detailList(list, "Findings", subject.findings, function (item) {
        return textOr(item.severity, MC_UNKNOWN).toUpperCase() + " " + textOr(item.category, MC_UNKNOWN) +
          ": " + textOr(item.message, MC_UNKNOWN) + " [" + textOr(item.rule_id, MC_UNKNOWN) + "]";
      }, "No challenge finding was recorded.");
      detailOwners(list, employeeID);
      // History is read from the captured detail response only, and is placed
      // after the current rows so an older failure reads as history and can
      // never be mistaken for the current placement, condition or value above.
      detailHistory(list, employeeID);
      // Board's own mutable records, captured with this same detail read and
      // kept below owner history so they can never be read as owner truth.
      detailBoardHistory(list, employeeID);
      detailEvidence(list, employeeID);
      box.appendChild(list);
      return box;
    }

    function detailFacts() {
      var employeeID = cockpit.detailEmployee;
      var card = heldCard(employeeID);
      var subject = cockpit.detailView || card;
      var wrap = el("div", "mc-brief");
      if (!subject) {
        wrap.appendChild(el("p", "mc-empty", "No accepted state for this employee is available."));
        return wrap;
      }
      wrap.appendChild(detailAnswers(card, subject));
      wrap.appendChild(detailEvidenceDisclosure(employeeID, card, subject));
      return wrap;
    }

    // Missing context and evaluated-empty context are different statements and
    // must not share wording. If the employee has no card in the held artifact
    // then nothing about its owners was evaluated at all: it may sit outside a
    // truncated fleet, or have been added or removed since the snapshot was
    // generated. Reporting that as "no owner reported anything" would invent an
    // evaluation that never happened and would slander an employee that is
    // simply out of frame.
    function detailContext(list, card) {
      if (!card) {
        definition(list, "Current work and next checkpoint",
          "Unavailable: this employee has no card in the held artifact, so no owner context was evaluated for it. " +
          "It may lie outside a truncated fleet, or have been added or removed since this snapshot was generated. " +
          "This is context the held artifact does not carry, not a report about what its owners said.");
        return;
      }
      var context = card.context;
      if (!context) {
        definition(list, "Current work and next checkpoint",
          "Unknown: this employee's card was evaluated and no fresh, unconflicted owner reported assignment, schedule or readiness.");
        return;
      }
      definition(list, "Current work", context.work
        ? workText(card) + " (" + sourceRefText(context.work.sources) + ")"
        : "Unknown: no fresh owner reported an active run or assignment.");
      definition(list, "Next checkpoint", context.checkpoint
        ? checkpointText(card) + (context.checkpoint.cron_expr ? " cron " + context.checkpoint.cron_expr : "") +
          " (" + sourceRefText(context.checkpoint.sources) + ")"
        : "Unknown: no fresh schedule observation.");
      var readiness = context.readiness;
      definition(list, "Bench readiness", readiness
        ? textOr(readiness.lifecycle_state, MC_UNKNOWN) +
          ", commissioned " + (readiness.commissioned === null || readiness.commissioned === undefined ? MC_UNKNOWN : String(readiness.commissioned)) +
          ", ready " + (readiness.ready === null || readiness.ready === undefined ? MC_UNKNOWN : String(readiness.ready)) +
          " (" + sourceRefText(readiness.sources) + ")"
        : "Unknown: no fresh commissioning observation.");
      definition(list, "Cadence", context.cadence && context.cadence.enabled !== null && context.cadence.enabled !== undefined
        ? (context.cadence.enabled ? "enabled" : "disabled") + " (" + sourceRefText(context.cadence.sources) + ")"
        : "Unknown: no fresh cadence policy fact.");
    }

    function detailList(list, term, items, describe, empty) {
      if (!Array.isArray(items) || items.length === 0) {
        definition(list, term, empty);
        return;
      }
      var box = el("div");
      for (var i = 0; i < items.length; i++) box.appendChild(el("p", null, describe(items[i])));
      definition(list, term, box);
    }
`
