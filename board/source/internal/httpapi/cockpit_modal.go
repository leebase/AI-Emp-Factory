package httpapi

// cockpitModalJS owns the employee-detail modal lifecycle.
//
// The governing idea is that an open dialog is a reading session, and a reading
// session owns its own DOM. It captures the artifact material it will show at
// the instant it opens, reads manager detail exactly once, writes its body
// exactly once when that read settles, and then does not touch that body again.
// Fleet polling, currency ticks and relative-age drift all keep happening
// underneath; none of them may write into what Lee is in the middle of reading,
// not even when the underlying content genuinely changed. Closing and reopening
// is how a newer artifact is adopted.
//
// The single exception is the dialog's own currency badge, which is the only
// thing in the dialog whose truth changes while it is being read.
const cockpitModalJS = `
    function heldRowsFor(collection, employeeID) {
      var rows = Array.isArray(collection) ? collection : [];
      var mine = [];
      for (var i = 0; i < rows.length; i++) {
        if (rows[i] && rows[i].employee_id === employeeID) mine.push(rows[i]);
      }
      return mine;
    }

    // captureHeldArtifact freezes the artifact side of a reading session and
    // binds it to one employee. Note what is deliberately not carried forward
    // as a verdict: the server's stale flag and refresh error are kept only as
    // the history of how this artifact arrived. Freshness is recomputed later
    // from the held read time and the held deadline, so a newer successful poll
    // can never make this snapshot look fresh and a newer failure can never be
    // charged against it.
    function captureHeldArtifact(employeeID) {
      var state = cockpit.state;
      var cards = (state && Array.isArray(state.cards)) ? state.cards : [];
      var card = null;
      for (var i = 0; i < cards.length; i++) {
        if (cards[i] && cards[i].employee_id === employeeID) { card = cards[i]; break; }
      }
      return {
        employeeID: employeeID,
        card: card,
        sources: heldRowsFor(state && state.sources, employeeID),
        errors: heldRowsFor(state && state.errors, employeeID),
        evidence: heldRowsFor(state && state.evidence, employeeID),
        snapshotID: cockpit.snapshotID,
        generatedAt: (state && typeof state.generated_at === "string") ? state.generated_at : "",
        validUntil: (state && typeof state.valid_until === "string") ? state.valid_until : "",
        ageSeconds: cockpit.ageSeconds,
        readAt: cockpit.readAt,
        expiresAt: cockpit.expiresAt,
        arrivedStale: cockpit.serverStale === true,
        arrivedRefreshError: cockpit.refreshError
      };
    }

    // Every age printed inside the dialog is measured against the clock of the
    // artifact being read, not against whatever has arrived since.
    function heldServerNow() {
      var held = cockpit.held;
      if (!held) return NaN;
      var generated = parseInstant(held.generatedAt);
      if (!isFinite(generated) || held.ageSeconds === null) return NaN;
      return generated + held.ageSeconds * 1000 + (Date.now() - held.readAt);
    }

    function heldTime(value) { return relativeTime(value, heldServerNow()); }

    // The detail read is bounded and sequenced: a slow answer for a previously
    // selected employee can never render over a newer selection.
    async function openEmployeeDetail(employeeID, focusOrigin) {
      if (!employeeID) return;
      var seq = ++cockpit.detailSeq;
      cockpit.detailEmployee = employeeID;
      cockpit.held = captureHeldArtifact(employeeID);
      cockpit.detailView = null;
      cockpit.detailError = "";
      cockpit.detailLoading = true;
      cockpit.detailWritten = false;
      cockpit.detailInteracted = false;
      cockpit.returnFocus = focusOrigin || null;
      renderDetail();
      await readEmployeeDetail(seq, employeeID);
    }

    async function readEmployeeDetail(seq, employeeID) {
      try {
        var response = await sessionFetch("/api/operations/managerial/employees/" + encodeURIComponent(employeeID));
        if (seq !== cockpit.detailSeq) return;
        if (!response.ok) {
          cockpit.detailError = (response.status === 401 || response.status === 403)
            ? authMessage(response.status, "this employee")
            : "Employee detail failed with HTTP " + response.status + ".";
        } else {
          var body = await response.json();
          if (seq !== cockpit.detailSeq) return;
          cockpit.detailView = body;
        }
      } catch (error) {
        if (seq !== cockpit.detailSeq) return;
        cockpit.detailError = "Employee detail request failed: " + ((error && error.message) || "unknown error");
      } finally {
        if (seq === cockpit.detailSeq) {
          cockpit.detailLoading = false;
          renderDetail();
        }
      }
    }

    function closeEmployeeDetail() {
      var employeeID = cockpit.detailEmployee;
      var opener = cockpit.returnFocus;
      cockpit.detailSeq++;
      cockpit.detailEmployee = null;
      cockpit.held = null;
      cockpit.detailView = null;
      cockpit.detailError = "";
      cockpit.detailLoading = false;
      cockpit.detailWritten = false;
      cockpit.detailInteracted = false;
      cockpit.returnFocus = null;
      renderDetail();
      flushDeferredRoster();
      restoreCockpitFocus(employeeID, opener);
    }

    // A currency tick that arrived while the dialog was open was deferred so it
    // could not rebuild the roster under the user. It is settled here, on the
    // close, so no card is left asserting a currency the banner has withdrawn.
    function flushDeferredRoster() {
      if (cockpit.rosterCurrency !== cockpitCurrency()) renderRoster();
    }

    // Closing must land focus on something a keyboard can actually use. The
    // opener may have been replaced by a poll or by the deferred roster render,
    // so the current card for the same employee is preferred, then the original
    // opener if it survived, then a real roster control. Never the body.
    function restoreCockpitFocus(employeeID, opener) {
      if (employeeID) {
        var scopes = [document.querySelector("#roster"), document];
        for (var s = 0; s < scopes.length; s++) {
          if (!scopes[s]) continue;
          var buttons = scopes[s].querySelectorAll("button[data-employee]");
          for (var i = 0; i < buttons.length; i++) {
            if (buttons[i].dataset.employee === employeeID) { buttons[i].focus(); return; }
          }
        }
      }
      if (opener && document.contains(opener)) { opener.focus(); return; }
      var fallback = document.querySelector("#roster-search") ||
        document.querySelector("#roster-filters button[data-group]") ||
        document.querySelector("#cockpit-refresh");
      if (fallback) fallback.focus();
    }

    // The lifecycle gate. A real open builds the dialog; a real close tears it
    // down; the first settled read writes the body once. Every other caller,
    // and in particular every fleet poll, reaches the ownership guard below and
    // does nothing. Focus moves exactly twice: onto Close when the dialog really
    // opens, and back to the roster when it really closes.
    function renderDetail() {
      var host = document.querySelector("#employee-detail");
      if (!cockpit.detailEmployee) {
        cockpit.detailPanelFor = null;
        host.hidden = true;
        replaceChildren(host, []);
        return;
      }
      if (cockpit.detailPanelFor !== cockpit.detailEmployee) {
        cockpit.detailPanelFor = cockpit.detailEmployee;
        host.hidden = false;
        replaceChildren(host, [buildDetailShell()]);
        writeDetailStatus();
        updateDetailCurrency();
        var close = document.querySelector("#employee-detail-close");
        if (close) close.focus();
        return;
      }
      // The reading session owns this DOM. A newer snapshot, genuinely changed
      // content and a drifting relative age are all real, and none of them is a
      // reason to rebuild what Lee is reading. Close and reopen adopts a newer
      // artifact; the badge below says when that is worth doing.
      if (cockpit.detailWritten || cockpit.detailLoading) return;
      settleDetailReading();
    }

    // The shell holds everything whose identity must survive the whole reading
    // session: the dialog, its heading, Close, the live currency badge, the
    // status line, the vintage attribution and the body.
    function buildDetailShell() {
      var panel = el("div", "mc-detail");
      panel.setAttribute("role", "dialog");
      panel.setAttribute("aria-modal", "true");
      panel.setAttribute("aria-labelledby", "employee-detail-title");
      var heading = el("h3", null, cockpit.detailEmployee);
      heading.id = "employee-detail-title";
      panel.appendChild(heading);
      var close = el("button", "mc-detail-close", "Close");
      close.type = "button";
      close.id = "employee-detail-close";
      panel.appendChild(close);
      var currency = el("p", "mc-currency");
      currency.id = "employee-detail-currency";
      currency.setAttribute("role", "status");
      panel.appendChild(currency);
      var status = el("p", "mc-empty");
      status.id = "employee-detail-status";
      status.setAttribute("role", "status");
      status.hidden = true;
      panel.appendChild(status);
      var vintage = el("p", "mc-count");
      vintage.id = "employee-detail-vintage";
      panel.appendChild(vintage);
      var body = el("div");
      body.id = "employee-detail-body";
      panel.appendChild(body);
      return panel;
    }

    function writeDetailStatus() {
      var status = document.querySelector("#employee-detail-status");
      if (!status) return;
      var message = cockpit.detailError || (cockpit.detailLoading ? "Loading employee detail..." : "");
      if (status.textContent === message) return;
      status.textContent = message;
      status.hidden = message === "";
    }

    function writeDetailVintage() {
      var node = document.querySelector("#employee-detail-vintage");
      if (node) node.textContent = detailFreshnessLine();
    }

    // The first screen gets one short line in plain words, not the vintage
    // paragraph. It still names the two observations separately, because a
    // freshly checked status and a saved assignment can never share an age.
    // The snapshot id, the absolute instants and the stated validity bound
    // stay in the evidence disclosure.
    function detailFreshnessLine() {
      var view = cockpit.detailView;
      var live = view
        ? "Status checked " + relativeTime(view.observed_at)
        : (cockpit.detailLoading ? "Status is being checked now" : "Status not checked");
      var held = cockpit.held || {};
      var context = held.generatedAt
        ? "assignment and schedule saved " + heldTime(held.generatedAt)
        : "assignment and schedule saved at an unknown time";
      return live + "; " + context + ".";
    }

    // The one body write of a reading session. It happens when the initial
    // detail read settles, whether it succeeded or failed, and it is the last
    // time this session touches the body. If Lee has already started reading,
    // it must not take the scroll position, the focus or a live selection away.
    function settleDetailReading() {
      cockpit.detailWritten = true;
      writeDetailStatus();
      writeDetailVintage();
      updateDetailCurrency();
      var body = document.querySelector("#employee-detail-body");
      if (!body) return;
      var host = document.querySelector("#employee-detail");
      var position = readingPosition(host, body);
      replaceChildren(body, [detailFacts()]);
      restoreReadingPosition(host, position);
    }

    // A live selection counts as having started reading even when no control
    // was ever focused and nothing was ever scrolled.
    function readingPosition(host, body) {
      var live = typeof getSelection === "function" ? getSelection() : null;
      var selecting = !!(live && live.rangeCount > 0 && live.isCollapsed === false &&
        host && host.contains && live.anchorNode && host.contains(live.anchorNode));
      var active = document.activeElement;
      return {
        interacted: cockpit.detailInteracted || selecting,
        scrollTop: host ? host.scrollTop : 0,
        focused: (active && body.contains && body.contains(active)) ? active : null
      };
    }

    function restoreReadingPosition(host, position) {
      if (!position.interacted) return;
      // preventScroll matters here: focusing a control is what silently drags a
      // dialog back to the top, and the reading position is what Lee loses.
      if (position.focused && document.contains && document.contains(position.focused)) {
        position.focused.focus({ preventScroll: true });
      }
      if (host) host.scrollTop = position.scrollTop;
    }

`
