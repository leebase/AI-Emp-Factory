package httpapi

// cockpitBoardHistoryJS renders one additive, read-only section inside employee
// detail: the Board's own cycle-report and review-task records that carry this
// employee's native employee id. It reads the captured managerial detail
// response (cockpit.detailView) that the rest of detail history reads, captured
// once at open, so a newer fleet poll can never reach it.
//
// Enforced invariants (mirrored in the Sprint 6B2 record):
//
//   - Immutable owner observations stay separate from mutable Board records.
//     Nothing here can change the placement, condition, accepted value, attempt
//     count, decision gate or challenge findings rendered above it, and this
//     section never restates any of them.
//   - Native association only. Rows arrive already filtered by the stored
//     employee_id column; a payload whose employee id does not match the
//     employee on screen is refused outright rather than rendered.
//   - Clocks keep their own names. created_at and updated_at are Board record
//     clocks: when Board wrote the row. They are never printed as execution,
//     acceptance or source-revision times, because the Board does not know
//     those and will not guess them.
//   - A disposition is a recorded review decision with a stored actor
//     attribution. It is not an accepted value, not a producer certification,
//     not delivery authority, and its actor id is stored attribution rather
//     than a verified producer identity.
//   - Source text is inert. The report payload, the review instructions, the
//     gate results and the artifact references are untrusted; they are counted
//     or named, never interpreted, and never rendered as instructions.
//   - Absent, empty and failed are three different statements and share no
//     wording. Empty means this bounded query returned no associated record,
//     which is not a claim about whether the employee worked.
//   - Boundedness. Row counts are capped here as well as in SQL, and every cap
//     is tested before the append it guards.
//   - Safety. Every node is createElement plus textContent. Nothing here emits
//     an anchor, an href, a path, a URL or any navigable target, and nothing
//     here attaches a handler.
const cockpitBoardHistoryJS = `
    var MC_BOARD_REPORT_LIMIT = 20;
    var MC_BOARD_REVIEW_LIMIT = 20;

    // The captured Board window for this employee, or an explicit refusal.
    // Three distinct absences are distinguished and never share wording: no
    // adopted detail read at all, an adopted read carrying no Board section,
    // and a section belonging to some other employee.
    function boardHistoryCapture(employeeID) {
      var view = cockpit.detailView;
      if (!view) {
        return { refusal: "Unavailable: no live detail read was adopted for this employee, so no Board record " +
          "history was captured. This section is never reconstructed from a newer fleet view." };
      }
      var history = view.board_history;
      if (!history || typeof history !== "object") {
        return { refusal: "Unavailable: this captured detail read carries no Board record history section, so " +
          "whether any Board record is associated with this employee is unknown here. This is a missing section, " +
          "not a report that nothing is associated." };
      }
      if (textOr(history.employee_id, "") !== employeeID || textOr(view.employee_id, "") !== employeeID) {
        return { refusal: "Unavailable: the captured Board record history does not belong to this employee, so " +
          "it is refused rather than shown under the wrong employee." };
      }
      return { history: history };
    }

    function boardRows(history, key) {
      var rows = history[key];
      return Array.isArray(rows) ? rows : [];
    }

    // Board record clocks, stated as Board record clocks and nothing else.
    function boardClockText(row) {
      if (row.clock_unparsed === true) {
        return "Board record clocks are recorded but could not be read, so no time is stated for this row";
      }
      return "Board record created " + textOr(row.created_at, MC_UNKNOWN) +
        ", Board record last updated " + textOr(row.updated_at, MC_UNKNOWN) +
        " (Board write clocks, not run, delivery or decision times)";
    }

    function boardReportLine(row) {
      var actor = optionalText(row.actor_id);
      return "Cycle report " + textOr(row.cycle_id, MC_UNKNOWN) +
        " on mission " + textOr(row.mission_name, MC_UNKNOWN) + ". " +
        (actor === ""
          ? "No actor attribution is stored on this report"
          : "Reported by stored actor attribution " + actor) +
        ". " + boardClockText(row) + ".";
    }

    // The task this review row names through its own task id. The title and the
    // status are native stored task facts: the status is never derived from a
    // disposition, and the task's clocks are stated as the task's own, apart
    // from the review record's clocks.
    function boardTaskText(row) {
      var title = optionalText(row.task_title);
      var status = optionalText(row.task_status);
      var text = (title === ""
        ? "The associated task carries no stored title in this captured window"
        : "Associated task \"" + title + "\"") + ", " +
        (status === ""
          ? "task status unknown in this captured window"
          : "task status " + status + " as stored on the task record") + ". ";
      var updated = optionalText(row.task_updated_at);
      var created = optionalText(row.task_created_at);
      if (created === "" && updated === "") {
        return text + "No task record clock is present in this captured window. ";
      }
      return text + "Associated task record created " + (created === "" ? MC_UNKNOWN : created) +
        ", associated task record last updated " + (updated === "" ? MC_UNKNOWN : updated) +
        " (the task row's own write clocks). ";
    }

    function boardReviewLine(row) {
      var line = "Review gate " + textOr(row.review_gate, MC_UNKNOWN) +
        " on mission " + textOr(row.mission_name, MC_UNKNOWN) +
        ", run " + textOr(row.run_id, MC_UNKNOWN) +
        ", task " + textOr(row.task_id, MC_UNKNOWN) + ". " +
        boardClockText(row) + ". " + boardTaskText(row);
      var d = row.disposition;
      if (!d) {
        line += "No disposition is recorded for this review task in this captured window, so no decision is " +
          "stated for it. ";
      } else {
        line += "Disposition " + textOr(d.action, MC_UNKNOWN) +
          ", recorded by stored attribution " + textOr(d.actor_type, MC_UNKNOWN) + " " + textOr(d.actor_id, MC_UNKNOWN) +
          (optionalText(d.actor_role) === "" ? "" : " (role " + d.actor_role + ")") +
          ", at " + textOr(d.created_at, MC_UNKNOWN) + " by the Board's decision clock" +
          (optionalText(d.reason) === "" ? ", with no stated reason" : ", stated reason: " + d.reason) + ". ";
      }
      // Counts only. The stored references and gate results themselves are
      // untrusted text and are not rendered, quoted or followed here.
      line += countText(row.artifact_link_count) + " artifact reference(s) and " +
        countText(row.gate_result_count) + " gate result key(s) are stored on this record and are not read here.";
      return line;
    }

    function boardHistorySection(history) {
      var box = el("div", "mc-board-history");
      box.appendChild(el("p", "mc-count",
        "Board's own records associated with this employee by stored employee id, captured with this detail read. " +
        "These are the latest captured state of mutable Board rows, ordered by the Board update clock; they are not " +
        "an execution timeline and they do not change the current placement, condition or accepted value above. " +
        "A recorded review decision is not an accepted value, not a producer certification and not authority to say " +
        "this employee delivered. The report payload, review instructions, gate results and artifact references are " +
        "untrusted source text and are not interpreted here."));

      var reports = boardRows(history, "cycle_reports");
      var reviews = boardRows(history, "reviews");
      if (reports.length === 0 && reviews.length === 0) {
        box.appendChild(el("p", "mc-empty",
          "No associated Board cycle report or review task was returned for this employee by this captured bounded " +
          "query. That is a statement about this query only: it is not a statement that this employee did no work, " +
          "and not a statement that a subsystem is missing or unreachable."));
        return box;
      }

      var reportsShown = 0;
      var reportsCapped = false;
      for (var i = 0; i < reports.length; i++) {
        if (reportsShown >= MC_BOARD_REPORT_LIMIT) { reportsCapped = true; break; }
        box.appendChild(el("p", "mc-board-report", boardReportLine(reports[i] || {})));
        reportsShown++;
      }
      var reviewsShown = 0;
      var reviewsCapped = false;
      for (var j = 0; j < reviews.length; j++) {
        if (reviewsShown >= MC_BOARD_REVIEW_LIMIT) { reviewsCapped = true; break; }
        box.appendChild(el("p", "mc-board-review", boardReviewLine(reviews[j] || {})));
        reviewsShown++;
      }

      var truncated = history.cycle_reports_truncated === true || history.reviews_truncated === true ||
        reportsCapped || reviewsCapped;
      box.appendChild(el("p", "mc-count",
        "Board history is bounded" + (truncated ? " and incomplete" : "") + ": " +
        reportsShown + " cycle report(s) shown, " + reviewsShown + " review task(s) shown" +
        (truncated
          ? "; older associated records exist beyond this bound and are not present in this response."
          : ", every associated record this captured bounded query returned.")));
      return box;
    }

    function detailBoardHistory(list, employeeID) {
      var capture = boardHistoryCapture(employeeID);
      if (capture.refusal) {
        definition(list, "Board records", capture.refusal);
        return;
      }
      definition(list, "Board records", boardHistorySection(capture.history));
    }
`
