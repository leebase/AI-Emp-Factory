package httpapi

// cockpitStaffJS renders the read-only staff view: Lee's actual employees as
// they are recorded on disk, one canonical card each.
//
// The rules it holds to, all of them visible in the rendered text:
//
//   - One card per employee. Runs, findings, warnings and relationships are
//     nested inside that card's own disclosure. A second run never becomes a
//     second employee, and there is no parallel repeated list of the same
//     people anywhere on this screen.
//   - The server decides what is known. The client prints server text and adds
//     no classification of its own: it never turns an unknown into offline, a
//     failure into a delivery, or a team edge into a live handoff.
//   - The attention filter narrows the same card list it counts. It is not a
//     separate panel of findings, so acting on a count lands on the employee.
//   - Every node is createElement plus textContent, and no recorded string
//     becomes markup, an attribute or an href. Source references are printed
//     as inert text.
//   - There is no timer. The view is read when it is opened and when Lee asks
//     for it, so the browser never polls owner files in the background.
const cockpitStaffJS = `
    var staffView = { loaded: false, loading: false, view: null, filter: "all", error: "", enabled: false, readAt: null };

    // The read clock is when the files were read. It is shown in local time
    // because that is the only form a person can judge; an unreadable value is
    // stated as unrecorded rather than guessed at.
    function staffReadTime(value) {
      if (!value) return "at an unrecorded time";
      var at = new Date(value);
      if (isNaN(at.getTime())) return "at " + value;
      return "at " + at.toLocaleTimeString() + " today";
    }

    function staffText(fact, fallback) {
      if (!fact || typeof fact.text !== "string" || fact.text === "") return fallback;
      return fact.text;
    }

    // staffFactLine prints the recorded text, then the source date and the age
    // of the record. The source date is never rewritten and the age is always
    // stated as the age of the record, not as the freshness of the estate.
    function staffFactLine(label, fact) {
      var wrap = el("div", "mc-staff-fact");
      wrap.appendChild(el("span", "mc-staff-label", label));
      wrap.appendChild(el("p", null, staffText(fact, "Not recorded.")));
      var parts = [];
      if (fact && fact.recorded_at) parts.push("Source date " + fact.recorded_at);
      if (fact && fact.age_text) parts.push(fact.age_text);
      if (parts.length) wrap.appendChild(el("p", "mc-count", parts.join(" - ")));
      if (fact && fact.source_refs && fact.source_refs.length) {
        wrap.appendChild(el("p", "mc-count", "Read from: " + fact.source_refs.join(", ")));
      }
      return wrap;
    }

    function staffAttentionCounts(employees) {
      var counts = { all: employees.length, decision: 0, blocker: 0, paused: 0, warning: 0 };
      for (var i = 0; i < employees.length; i++) {
        var kinds = {};
        var items = employees[i].attention || [];
        for (var j = 0; j < items.length; j++) kinds[items[j].kind] = true;
        if (kinds.decision) counts.decision++;
        if (kinds.blocker) counts.blocker++;
        if (kinds.paused) counts.paused++;
        if (kinds.warning) counts.warning++;
      }
      return counts;
    }

    // staffBoardCounts keeps Board coverage explicit. Board asks are counted
    // only for employees actually bound to the configured source; everyone
    // else stays unknown rather than being counted as zero.
    function staffBoardCounts(employees) {
      var connected = 0;
      var asks = 0;
      for (var i = 0; i < employees.length; i++) {
        var board = employees[i].board;
        if (!board) continue;
        if (board.state === "connected") connected++;
        asks += (board.asks || []).length;
      }
      return { connected: connected, asks: asks, total: employees.length };
    }

    function staffMatchesFilter(employee, filter) {
      if (filter === "all") return true;
      if (filter === "board-asks") {
        var board = employee.board;
        return !!(board && (board.asks || []).length);
      }
      if (filter === "board-connected") {
        var connected = employee.board;
        return !!(connected && connected.state === "connected");
      }
      var items = employee.attention || [];
      for (var i = 0; i < items.length; i++) if (items[i].kind === filter) return true;
      return false;
    }

    // staffStoppedItems surfaces the specific thing standing in the way, so the
    // card names it instead of making Lee open the disclosure to find out.
    //
    // A pause is NOT a blocker. Somebody decided it on purpose and recorded a
    // reason; printing "Blocked" over that reports a deliberate decision as a
    // fault. The two are kept apart and, when an employee carries both, each
    // gets its own line rather than one label standing in for the other.
    function staffStoppedItems(employee) {
      var items = employee.attention || [];
      var found = { blocker: null, paused: null };
      for (var i = 0; i < items.length; i++) {
        if (items[i].kind === "blocker" && !found.blocker) found.blocker = items[i];
        if (items[i].kind === "paused" && !found.paused) found.paused = items[i];
      }
      return found;
    }

    // staffGoToButton moves to the counterpart's own existing card. It is a
    // local scroll and focus, not a second read and not a duplicate card: there
    // is still exactly one card per employee on this screen.
    function staffGoToButton(employeeID, label) {
      var button = el("button", null, "Go to " + label);
      button.type = "button";
      button.setAttribute("data-goto", employeeID);
      button.addEventListener("click", function () {
        if (staffView.filter !== "all") { staffView.filter = "all"; renderStaffView(); }
        var card = document.querySelector(".mc-staff-card[data-employee=\"" + employeeID.replace(/"/g, "") + "\"]");
        if (!card) return;
        card.scrollIntoView({ block: "start" });
        var heading = card.querySelector(".mc-staff-name");
        heading.setAttribute("tabindex", "-1");
        heading.focus();
      });
      return button;
    }

    // No source in this slice records a structured decision gate, so this view
    // has nothing to say about what is waiting on Lee. It shows the recorded
    // blockers, pauses and warnings it does have, and states once - in the
    // caveat, not on every card - that decisions are unavailable. A zero would
    // be a coverage claim it cannot make.
    function staffAttentionBlock(employee) {
      var wrap = el("div", "mc-staff-attention");
      var items = employee.attention || [];
      wrap.appendChild(el("span", "mc-staff-label", "Recorded state"));
      if (!items.length) {
        wrap.appendChild(el("p", null, "Nothing is recorded against this employee in the files read."));
      }
      for (var i = 0; i < items.length; i++) wrap.appendChild(staffAttentionItem(items[i]));
      return wrap;
    }

    function staffAttentionItem(item) {
      var row = el("div", "mc-staff-item");
      row.setAttribute("data-kind", item.kind);
      var label = { decision: "Waiting on you", blocker: "Blocked", paused: "Paused on purpose", warning: "Warning" }[item.kind] || "Recorded";
      row.appendChild(badge(label, item.kind === "warning" || item.kind === "paused" ? "warn" : "alert"));
      row.appendChild(el("p", null, item.text || "No text recorded."));
      var parts = [];
      if (item.actor) parts.push("Recorded by " + item.actor);
      if (item.recorded_at) parts.push("Source date " + item.recorded_at);
      if (item.source_refs && item.source_refs.length) parts.push("Read from: " + item.source_refs.join(", "));
      if (parts.length) row.appendChild(el("p", "mc-count", parts.join(" - ")));
      return row;
    }

    // staffRuns nests every recorded run under its employee. A run carries its
    // own findings; it is never lifted out into the roster as an identity.
    function staffRuns(employee) {
      var wrap = el("div", "mc-staff-runs");
      var runs = employee.runs || [];
      wrap.appendChild(el("span", "mc-staff-label",
        runs.length ? "Recorded runs (" + runs.length + " of " + employee.run_count + ")" : "Recorded runs"));
      if (!runs.length) {
        wrap.appendChild(el("p", null, "No run records were read for this employee."));
        return wrap;
      }
      for (var i = 0; i < runs.length; i++) {
        var run = runs[i];
        var row = el("div", "mc-staff-run");
        row.appendChild(el("strong", null, run.title || run.run_ref || "Unidentified run"));
        row.appendChild(el("p", null, (run.outcome ? run.outcome + " - " : "") + (run.delivery_text || "delivery not recorded")));
        if (run.ended_at) row.appendChild(el("p", "mc-count", "Ended " + run.ended_at));
        if (run.title) row.appendChild(el("p", "mc-count", "Cycle " + run.run_ref));
        if (run.run_status) row.appendChild(el("p", "mc-count", "Run status " + run.run_status));
        row.appendChild(el("p", "mc-count", run.finding_count + (run.finding_count === 1 ? " finding recorded" : " findings recorded")));
        var findings = run.findings || [];
        for (var f = 0; f < findings.length; f++) row.appendChild(el("p", "mc-staff-finding", findings[f]));
        if (run.source_ref) row.appendChild(el("p", "mc-count", "Read from: " + run.source_ref));
        wrap.appendChild(row);
      }
      return wrap;
    }

    // staffRelationships renders compact chips. Each chip's provenance - the
    // file, the date, who verified it, and what it does NOT establish - is one
    // disclosure away rather than asserted as a live connection.
    function staffRelationships(employee) {
      var wrap = el("div", "mc-staff-relationships");
      var edges = employee.relationships || [];
      wrap.appendChild(el("span", "mc-staff-label", "Recorded relationships"));
      if (!edges.length) {
        wrap.appendChild(el("p", null, "No relationship is recorded for this employee in the configured file. That is an absence of a record, not an absence of relationships."));
        return wrap;
      }
      for (var i = 0; i < edges.length; i++) {
        var edge = edges[i];
        var chip = el("details", "mc-staff-chip");
        var summary = el("summary", null,
          (edge.direction === "to" ? "-> " : "<- ") + (edge.counterpart_name || edge.counterpart_id) + " - " + (edge.kind || "recorded relationship"));
        chip.appendChild(summary);
        chip.appendChild(el("p", null, edge.label || "No label recorded."));
        chip.appendChild(el("p", null, edge.means || ""));
        if (!edge.counterpart_in_view) {
          chip.appendChild(el("p", "mc-count", "This counterpart is not in the current read, so nothing is shown about its state."));
        } else {
          chip.appendChild(staffGoToButton(edge.counterpart_id, edge.counterpart_name || edge.counterpart_id));
        }
        var provenance = [];
        if (edge.source_ref) provenance.push("Source: " + edge.source_ref);
        if (edge.recorded_on) provenance.push("Recorded on " + edge.recorded_on);
        if (edge.verified_by) provenance.push("Verified by " + edge.verified_by);
        chip.appendChild(el("p", "mc-count", provenance.join(" - ")));
        wrap.appendChild(chip);
      }
      return wrap;
    }

    // The card answers four short questions and then stops. Everything that
    // qualifies those answers - source files, dates, runs, findings, the
    // caveats behind an unknown - is inside the one disclosure below them.
    // staffBoardBlock leads the card with the native Board facts when this
    // employee is bound to a configured source. It never prints a zero for an
    // employee the source does not cover: coverage is stated in words.
    function staffBoardBlock(employee) {
      var board = employee.board;
      if (!board) return null;
      // A non-connected employee keeps its Board coverage note in the
      // disclosure, so an unbound card is not padded with a repeated unknown
      // on the first screen. Coverage for the whole read is stated separately.
      if (board.state !== "connected") return null;
      var wrap = el("div", "mc-staff-board");
      wrap.appendChild(el("span", "mc-staff-label", "Board"));
      // The first visible Board line honors a partial read. A bounded read that
      // could not confirm assignment must never print a no-work all-clear.
      var work = "No active Board assignment for this employee in the configured source.";
      if (board.assigned) {
        work = "Board work: " + (board.assigned.title || board.assigned.task_id) + " (" + board.assigned.status + ")";
      } else if (board.execution === "partial") {
        work = board.execution_text || "A bounded read could not confirm whether Board work is assigned; this is not a no-work conclusion.";
      }
      wrap.appendChild(el("p", "mc-staff-board-work", work));
      // The execution detail is only printed when it adds something beyond the
      // work line; a no-work line already says there is no active assignment.
      if (board.execution_text && board.execution !== "none" && board.execution !== "partial") {
        wrap.appendChild(el("p", "mc-count", board.execution_text));
      }
      if (board.result_text) {
        wrap.appendChild(el("p", null, board.result_text));
      } else if (board.milestone) {
        wrap.appendChild(el("p", "mc-count", "Latest recorded Board event: " + board.milestone.meaning + " - " + board.milestone.recorded_at + " (" + board.milestone.actor + ")"));
      }
      var asks = board.asks || [];
      for (var i = 0; i < asks.length; i++) {
        var row = el("div", "mc-staff-item");
        row.setAttribute("data-kind", "decision");
        row.appendChild(badge("Waiting on you", "alert"));
        row.appendChild(el("p", null, asks[i].text || "A Board ask is recorded."));
        var parts = [];
        if (asks[i].recorded_at) parts.push("Source date " + asks[i].recorded_at);
        if (asks[i].expires_at) parts.push("Expires " + asks[i].expires_at);
        if (parts.length) row.appendChild(el("p", "mc-count", parts.join(" - ")));
        wrap.appendChild(row);
      }
      return wrap;
    }

    // staffBoardDetail is the bounded evidence behind the Board lines,
    // including the source binding, the partial-read disclosure and the dated
    // work timeline. The timeline is what the source recorded, not a live
    // claim and not the DB read time.
    function staffBoardDetail(employee) {
      var board = employee.board;
      if (!board) return null;
      var wrap = el("div", "mc-staff-board-detail");
      wrap.appendChild(el("span", "mc-staff-label", "Board source"));
      var meta = [];
      if (board.agent_id) meta.push("agent " + board.agent_id);
      if (board.machine_id) meta.push("machine " + board.machine_id);
      if (board.board_ref) meta.push(board.board_ref);
      if (meta.length) wrap.appendChild(el("p", "mc-count", meta.join(" - ")));
      if (board.note) wrap.appendChild(el("p", "mc-count", board.note));
      if (board.partial_text) wrap.appendChild(el("p", "mc-staff-finding", board.partial_text));
      if (board.assigned) {
        wrap.appendChild(el("p", null, "Current Board assignment: " + (board.assigned.title || board.assigned.task_id) + " (" + board.assigned.status + "), updated " + (board.assigned.updated_at || "unrecorded")));
      }
      if (board.last_result_task) {
        wrap.appendChild(el("p", null, "Latest terminal Board task: " + (board.last_result_task.title || board.last_result_task.task_id) + " (" + board.last_result_task.status + ")"));
      }
      if (board.result_text) wrap.appendChild(el("p", null, board.result_text));
      var timeline = board.timeline || [];
      wrap.appendChild(el("p", "mc-staff-label", "Recorded Board timeline (" + timeline.length + " of " + board.timeline_count + ")"));
      for (var i = 0; i < timeline.length; i++) {
        var event = timeline[i];
        wrap.appendChild(el("p", "mc-count", event.recorded_at + " - " + event.meaning + " (" + event.actor + ")"));
      }
      return wrap;
    }

    function staffNormalize(value) {
      return String(value || "").replace(/\s+/g, " ").trim();
    }

    function staffMissionSentence(employee) {
      var fact = employee && employee.standing_remit;
      if (!fact || fact.known !== true) return "mission not recorded";
      var text = staffNormalize(fact.text);
      var match = text.match(/^(.+?[.!?])(?:\s|$)/);
      return match ? match[1] : "mission not recorded";
    }

    function staffMonogram(name) {
      var words = staffNormalize(name).split(" ").filter(Boolean);
      var first = (words[0] || "").replace(/[^A-Za-z0-9]/g, "");
      var last = (words[words.length - 1] || "").replace(/[^A-Za-z0-9]/g, "");
      if (!first) return "?";
      if (words.length === 1) return (first.slice(0, 2) || "?").toUpperCase();
      return ((first.charAt(0) || "") + (last.charAt(0) || "")).toUpperCase() || "?";
    }

    function staffCurrentWork(employee) {
      var board = employee && employee.board;
      if (board && board.state === "connected" && board.assigned && staffNormalize(board.assigned.title)) {
        return staffNormalize(board.assigned.title);
      }
      var assignment = employee && employee.assignment;
      if (assignment && assignment.known === true && staffNormalize(assignment.text)) return staffNormalize(assignment.text);
      return "no recent record";
    }

    function staffLastFinished(employee) {
      var board = employee && employee.board;
      if (board && board.state === "connected" && board.last_result_task && staffNormalize(board.last_result_task.title)) {
        return staffNormalize(board.last_result_task.title);
      }
      var runs = (employee && employee.runs) || [];
      for (var i = 0; i < runs.length; i++) {
        if (staffNormalize(runs[i].title) && staffNormalize(runs[i].ended_at)) return staffNormalize(runs[i].title);
      }
      return "no recent record";
    }

    function staffAsks(employee) {
      var board = employee && employee.board;
      return board && board.state === "connected" && Array.isArray(board.asks) ? board.asks : [];
    }

    function staffEvidenceBlock(parent, label, value) {
      if (value === undefined || value === null || value === "") return;
      var block = el("div", "mc-staff-evidence-block");
      block.appendChild(el("strong", null, label));
      block.appendChild(el("p", null, value));
      parent.appendChild(block);
    }

    function staffEvidenceFact(parent, label, fact) {
      if (!fact) return;
      var value = fact.known === true ? "recorded" : "not recorded";
      if (fact.text) value += ": " + fact.text;
      if (fact.recorded_at) value += " - source date " + fact.recorded_at;
      if (fact.age_text) value += " - " + fact.age_text;
      if (fact.source_refs && fact.source_refs.length) value += " - source refs " + fact.source_refs.join(", ");
      staffEvidenceBlock(parent, label, value);
    }

    function staffEvidenceEmployee(employee, view) {
      var evidence = el("div", "mc-staff-evidence-body");
      staffEvidenceBlock(evidence, "Owner-file read", view && view.observed_at ? view.observed_at : "observed time not recorded");
      var sourceNotes = ((view && view.source_notes) || []).concat((view && view.relationship_notes) || []);
      for (var n = 0; n < sourceNotes.length; n++) staffEvidenceBlock(evidence, "Coverage note", sourceNotes[n]);
      if (view && view.board_observed_at) staffEvidenceBlock(evidence, "Board source read", view.board_observed_at);
      var boardNotes = (view && view.board_notes) || [];
      for (var bn = 0; bn < boardNotes.length; bn++) staffEvidenceBlock(evidence, "Board coverage", boardNotes[bn]);
      var board = employee.board;
      if (board) {
        staffEvidenceBlock(evidence, "Board state", board.state);
        staffEvidenceBlock(evidence, "Board note", board.note);
        staffEvidenceBlock(evidence, "Board partial read", board.partial_text);
        staffEvidenceBlock(evidence, "Board binding", [board.agent_id, board.machine_id, board.board_ref].filter(Boolean).join(" - "));
        staffEvidenceBlock(evidence, "Board active assignment count", board.active_count);
        staffEvidenceBlock(evidence, "Board execution state", board.execution);
        if (board.assigned) staffEvidenceBlock(evidence, "Board assignment", [board.assigned.title, board.assigned.status, board.assigned.task_id, board.assigned.created_at, board.assigned.updated_at].filter(Boolean).join(" - "));
        else staffEvidenceBlock(evidence, "Board assignment", "No active Board assignment for this employee in the configured source.");
        if (board.execution_text) staffEvidenceBlock(evidence, "Board execution", board.execution_text);
        if (board.last_result_task) staffEvidenceBlock(evidence, "Board last-result task", [board.last_result_task.title, board.last_result_task.status, board.last_result_task.task_id].filter(Boolean).join(" - "));
        staffEvidenceBlock(evidence, "Board result", board.result_text);
        if (board.accepted) staffEvidenceBlock(evidence, "Board acceptance", [board.accepted.meaning, board.accepted.actor, board.accepted.recorded_at, board.accepted.task_ref].filter(Boolean).join(" - "));
        var asks = staffAsks(employee);
        for (var a = 0; a < asks.length; a++) staffEvidenceBlock(evidence, "Board ask", [asks[a].text, asks[a].recorded_at, asks[a].expires_at, asks[a].task_ref].filter(Boolean).join(" - "));
        var timeline = board.timeline || [];
        staffEvidenceBlock(evidence, "Board timeline", timeline.length + " of " + (board.timeline_count || timeline.length));
        for (var t = 0; t < timeline.length; t++) staffEvidenceBlock(evidence, "Timeline event", [timeline[t].meaning, timeline[t].actor, timeline[t].recorded_at, timeline[t].task_ref].filter(Boolean).join(" - "));
      }
      if (employee.activity_state === "unknown" || (employee.activity && employee.activity.known === false)) staffEvidenceBlock(evidence, "Activity", "No live activity signal was read. Activity is unknown: not idle, not offline, not broken, not benched, and not clear.");
      staffEvidenceFact(evidence, "Assignment", employee.assignment);
      staffEvidenceFact(evidence, "Latest task", employee.latest_task);
      staffEvidenceFact(evidence, "Standing remit", employee.standing_remit);
      staffEvidenceFact(evidence, "Last result", employee.last_result);
      staffEvidenceFact(evidence, "Schedule", employee.schedule);
      var attention = employee.attention || [];
      for (var i = 0; i < attention.length; i++) staffEvidenceBlock(evidence, "Recorded attention (" + attention[i].kind + ")", [attention[i].text, attention[i].actor, attention[i].recorded_at, (attention[i].source_refs || []).join(", ")].filter(Boolean).join(" - "));
      var relationships = employee.relationships || [];
      for (var r = 0; r < relationships.length; r++) staffEvidenceBlock(evidence, "Relationship", [relationships[r].direction, relationships[r].kind, relationships[r].counterpart_name || relationships[r].counterpart_id, relationships[r].label, relationships[r].means, relationships[r].source_ref, relationships[r].recorded_on, relationships[r].verified_by].filter(Boolean).join(" - "));
      var runs = employee.runs || [];
      staffEvidenceBlock(evidence, "Runs", runs.length + " of " + (employee.run_count || runs.length));
      for (var j = 0; j < runs.length; j++) staffEvidenceBlock(evidence, "Run", [runs[j].title, runs[j].outcome, runs[j].delivery, runs[j].delivery_text, runs[j].ended_at, runs[j].run_status, runs[j].run_ref, runs[j].finding_count + " findings", runs[j].source_ref, (runs[j].findings || []).join(" - ")].filter(Boolean).join(" - "));
      for (var s = 0; s < (employee.source_refs || []).length; s++) staffEvidenceBlock(evidence, "Employee source ref", employee.source_refs[s]);
      return evidence;
    }

    function staffCard(employee) {
      var card = el("button", "mc-staff-card");
      card.type = "button";
      card.setAttribute("data-employee", employee.employee_id || "");
      var face = el("span", "mc-staff-face", staffMonogram(employee.name));
      face.setAttribute("aria-hidden", "true");
      card.appendChild(face);
      var copy = el("span", "mc-staff-copy");
      copy.appendChild(el("strong", "mc-staff-name", staffNormalize(employee.name) || "name not recorded"));
      copy.appendChild(el("span", "mc-staff-role", staffMissionSentence(employee)));
      var work = el("span", "mc-staff-work", staffCurrentWork(employee));
      if (work.textContent === "no recent record") work.className += " mc-staff-missing";
      copy.appendChild(work);
      var asks = staffAsks(employee);
      if (asks.length) {
        var nudge = el("span", "mc-staff-nudge");
        nudge.appendChild(el("span", "mc-staff-nudge-dot", ""));
        nudge.lastChild.setAttribute("aria-hidden", "true");
        nudge.appendChild(el("span", null, "Needs you · " + (asks[0].text || "recorded Board ask")));
        copy.appendChild(nudge);
      }
      card.appendChild(copy);
      return card;
    }

    function staffPersonEmployee(employeeID) {
      var employees = (staffView.view && staffView.view.employees) || [];
      for (var i = 0; i < employees.length; i++) if (employees[i].employee_id === employeeID) return employees[i];
      return null;
    }

    function closeStaffPerson() {
      var person = document.querySelector("#staff-person");
      var roster = document.querySelector("#staff-roster");
      var held = staffView.person || {};
      person.hidden = true;
      roster.hidden = false;
      if (typeof window.scrollTo === "function") window.scrollTo(0, held.scrollTop || 0);
      if (held.opener && (!document.contains || document.contains(held.opener))) {
        if (held.opener.focus) held.opener.focus({ preventScroll: true });
      }
      if (typeof window.scrollTo === "function") window.scrollTo(0, held.scrollTop || 0);
      staffView.person = null;
    }

    function openStaffPerson(employeeID, opener) {
      var employee = staffPersonEmployee(employeeID);
      if (!employee) return;
      staffView.person = { employeeID: employeeID, opener: opener, scrollTop: window.scrollY || window.pageYOffset || 0 };
      document.querySelector("#staff-roster").hidden = true;
      var person = document.querySelector("#staff-person");
      person.hidden = false;
      renderStaffPerson(employee);
      var back = person.querySelector(".mc-staff-back");
      if (back && back.focus) back.focus();
      if (typeof window.scrollTo === "function") window.scrollTo(0, 0);
    }

    function renderStaffPerson(employee) {
      var person = document.querySelector("#staff-person");
      if (!person || !employee) return;
      person.textContent = "";
      var header = el("header", "mc-staff-person-header");
      var back = el("button", "mc-staff-back", "Back to staff");
      back.type = "button";
      back.addEventListener("click", closeStaffPerson);
      header.appendChild(back);
      var face = el("span", "mc-staff-person-face", staffMonogram(employee.name));
      face.setAttribute("aria-hidden", "true");
      header.appendChild(face);
      var heading = el("div", "mc-staff-person-heading");
      heading.appendChild(el("h3", null, staffNormalize(employee.name) || "name not recorded"));
      heading.appendChild(el("p", null, staffMissionSentence(employee)));
      header.appendChild(heading);
      person.appendChild(header);
      var doing = el("section", "mc-staff-answer");
      doing.appendChild(el("h4", null, "Doing now"));
      doing.appendChild(el("p", null, staffCurrentWork(employee)));
      person.appendChild(doing);
      var finished = el("section", "mc-staff-answer");
      finished.appendChild(el("h4", null, "Last finished"));
      finished.appendChild(el("p", null, staffLastFinished(employee)));
      person.appendChild(finished);
      var needs = el("section", "mc-staff-answer");
      needs.appendChild(el("h4", null, "Needs you"));
      var asks = staffAsks(employee);
      if (!asks.length) needs.appendChild(el("p", null, "no recent record"));
      for (var i = 0; i < asks.length; i++) needs.appendChild(el("p", null, asks[i].text || "recorded Board ask"));
      person.appendChild(needs);
      var evidence = el("details", "mc-staff-evidence");
      evidence.open = false;
      evidence.appendChild(el("summary", null, "Evidence"));
      evidence.appendChild(staffEvidenceEmployee(employee, staffView.view));
      person.appendChild(evidence);
    }

    function renderStaffView() {
      var roster = document.querySelector("#staff-roster");
      var status = document.querySelector("#staff-status");
      if (!roster || !status) return;
      roster.textContent = "";
      if (staffView.error) {
        status.hidden = false;
        status.textContent = staffView.error;
        return;
      }
      if (!staffView.view) {
        status.hidden = !staffView.loading;
        status.textContent = staffView.loading ? "Reading the recorded employee files..." : "";
        return;
      }
      status.hidden = true;
      var employees = staffView.view.employees || [];
      var about = document.querySelector("#staff-about-text");
      var meta = document.querySelector("#staff-about-meta");
      var filters = document.querySelector("#staff-filters");
      var count = document.querySelector("#staff-count");
      var aboutText = "These are one card per employee from the configured owner-file and native Board reads. They are recorded facts, not a live claim. Uncovered employees remain unknown; no current assignment or ask is reported as no recent record. Mission Control remains the captured operations artifact, and Advanced board view remains the board's task and project surface.";
      var notes = (staffView.view.source_notes || []).concat(staffView.view.relationship_notes || [], staffView.view.board_notes || []);
      if (notes.length) aboutText += " Read-scope notes: " + notes.join(" ");
      if (about) about.textContent = aboutText;
      if (meta) meta.textContent = "Read " + staffReadTime(staffView.view.observed_at) + " · " + employees.length + (employees.length === 1 ? " employee" : " employees");
      if (filters) {
        filters.textContent = "";
        var counts = staffAttentionCounts(employees);
        var options = [
          {key:"all", label:"All", count:counts.all},
          {key:"blocker", label:"Blocked", count:counts.blocker},
          {key:"paused", label:"Paused", count:counts.paused},
          {key:"warning", label:"Warnings", count:counts.warning}
        ];
        var boardCounts = staffBoardCounts(employees);
        if (boardCounts.asks) options.push({key:"board-asks", label:"Needs you (Board)", count:boardCounts.asks});
        if (boardCounts.connected) options.push({key:"board-connected", label:"Board-connected", count:boardCounts.connected});
        for (var f = 0; f < options.length; f++) (function(option) {
          var button = el("button", null, option.label + " (" + option.count + ")");
          button.type = "button";
          button.setAttribute("data-filter", option.key);
          button.setAttribute("aria-pressed", String(staffView.filter === option.key));
          button.addEventListener("click", function() { staffView.filter = option.key; renderStaffView(); });
          filters.appendChild(button);
        })(options[f]);
        filters.appendChild(el("span", "mc-count mc-staff-unavailable", "Decisions: unavailable"));
      }
      var shown = 0;
      for (var i = 0; i < employees.length; i++) {
        if (!staffMatchesFilter(employees[i], staffView.filter)) continue;
        roster.appendChild(staffCard(employees[i]));
        shown++;
      }
      if (count) count.textContent = shown + (shown === 1 ? " employee shown" : " employees shown");
      if (!shown) roster.appendChild(el("p", "mc-staff-empty", "no recent record"));
      if (staffView.person) {
        var selected = staffPersonEmployee(staffView.person.employeeID);
        if (selected) renderStaffPerson(selected);
      }
    }

    // loadStaffView reads once, on demand. A 404 means this deployment did not
    // opt in, which is why the Staff control stays hidden until a real 200.
    function loadStaffView() {
      if (staffView.loading) return Promise.resolve();
      staffView.loading = true;
      staffView.error = "";
      renderStaffView();
      return fetch("/api/staff-view", { credentials: "same-origin", headers: { "Accept": "application/json" } })
        .then(function (response) {
          if (response.status === 404) { staffView.enabled = false; staffView.error = "The staff view is not enabled on this deployment."; return null; }
          if (response.status === 401) { staffView.enabled = false; staffView.error = "Sign in to read the staff view."; return null; }
          if (response.status === 403) { staffView.enabled = false; staffView.error = "This signed-in identity is not permitted to read the staff view."; return null; }
          if (!response.ok) { staffView.enabled = false; staffView.error = "The staff view could not be read (HTTP " + response.status + ")."; return null; }
          staffView.enabled = true;
          return response.json();
        })
        .then(function (body) { if (body) { staffView.view = body; staffView.loaded = true; } })
        .catch(function () { staffView.enabled = false; staffView.error = "The staff view could not be read."; })
        .then(function () { staffView.loading = false; renderStaffView(); });
    }
`
