package httpapi

// cockpitWiringJS wires the cockpit's interactions and its one timer.
//
// Interaction rules: a card is a real button, so Enter and Space work and the
// target is never smaller than the card; Escape closes detail and returns focus
// to whatever opened it; Tab is contained inside the open dialog; nothing is
// revealed by hover only.
//
// It also records that Lee has started acting inside an open dialog. That fact
// is what stops a late initial detail completion from taking the scroll
// position or the focus away from someone who is already reading.
const cockpitWiringJS = `
    // Hiding the view that currently holds focus destroys focus silently: the
    // browser drops it on <body> and the next Tab restarts at the top of the
    // document. That is the existing detail -> Advanced defect, because closing
    // the dialog first returns focus to a card inside #cockpit-view and the
    // switch then hides that card.
    //
    // So the switch records where focus was, performs the switch, and only
    // then moves focus onto the nav button for the view that is now shown. The
    // order matters: moving focus before the switch aims it at something that
    // is about to be hidden. The nav lives outside both views, so the target is
    // always actually visible and actually focusable.
    // The views are mutually exclusive by construction. Staff is a third one
    // rather than a section of the cockpit, because the cockpit shows the
    // captured operations artifact and the staff view shows what owner files
    // actually record: mixing them on one screen is exactly the blend that
    // would make a captured synthetic card look like a real employee.
    var MC_VIEWS = [
      { name: "cockpit", section: "#cockpit-view", button: "#view-cockpit" },
      { name: "staff", section: "#staff-view", button: "#view-staff" },
      { name: "advanced", section: "#advanced-view", button: "#view-advanced" }
    ];

    function showCockpitView(name) {
      var held = false;
      var target = null;
      for (var i = 0; i < MC_VIEWS.length; i++) {
        var view = MC_VIEWS[i];
        var section = document.querySelector(view.section);
        var button = document.querySelector(view.button);
        var selected = view.name === name;
        if (selected) target = button;
        if (!selected && section && section.contains && section.contains(document.activeElement)) held = true;
        if (section) section.hidden = !selected;
        if (button) button.setAttribute("aria-pressed", String(selected));
      }
      // Staff mode collapses the legacy poll input and the view chooser out of
      // the first screen. The class is the only switch: every other mode keeps
      // its existing chrome exactly as it was.
      document.body.classList.toggle("mc-staff-mode", name === "staff");
      // The selected Staff nav button is deliberately inside the collapsed
      // chooser. Focus the visible Refresh control instead, so a successful
      // capability read never strands focus in a hidden view switch.
      if (name === "staff") {
        var staffRefresh = document.querySelector("#staff-refresh");
        if (staffRefresh) target = staffRefresh;
      }
      var stranded = held || !document.activeElement || document.activeElement === document.body;
      if (stranded && target) target.focus();
    }

    function markDetailInteraction() {
      if (cockpit.detailEmployee) cockpit.detailInteracted = true;
    }

    // A control inside a collapsed <details> is not rendered and the browser
    // does not make it a tab stop. The dialog's trap is hand-rolled, so it has
    // to honour the same rule itself: without this it would cycle focus into
    // evidence controls nobody can see, which is exactly the trap leaking. The
    // disclosure's own <summary> stays reachable while closed, because that is
    // the control that opens it.
    function trapCanFocus(panel, node) {
      if (node.getAttribute("tabindex") === "-1" || node.disabled) return false;
      for (var parent = node.parentNode; parent && parent !== panel; parent = parent.parentNode) {
        if (parent.tagName !== "DETAILS" || parent.open) continue;
        if (node.tagName === "SUMMARY" && node.parentNode === parent) continue;
        return false;
      }
      return true;
    }

    function wireCockpit() {
      document.querySelector("#view-cockpit").addEventListener("click", function () { showCockpitView("cockpit"); });
      document.querySelector("#view-staff").addEventListener("click", function () { showCockpitView("staff"); });
      document.querySelector("#staff-refresh").addEventListener("click", function () {
        loadStaffView().then(settleStaffCapability);
      });
      document.querySelector("#staff-view .mc-staff-modes").addEventListener("click", function (event) {
        var button = event.target.closest("button[data-view]");
        if (button) showCockpitView(button.dataset.view);
      });
      // One capability read at startup, not a poll. A successful 200 makes
      // Staff the landing view; every denied, disabled or failed read restores
      // the already accepted cockpit instead of leaving two views visible.
      function settleStaffCapability() {
        var staffButton = document.querySelector("#view-staff");
        staffButton.hidden = !staffView.enabled;
        showCockpitView(staffView.enabled ? "staff" : "cockpit");
      }
      loadStaffView().then(settleStaffCapability);
      document.querySelector("#view-advanced").addEventListener("click", function () { showCockpitView("advanced"); });
      document.querySelector("#cockpit-refresh").addEventListener("click", function () { loadCockpit(true); });
      document.querySelector("#roster-search").addEventListener("input", function (event) {
        cockpit.query = event.target.value || "";
        renderRoster();
      });
      document.querySelector("#roster-filters").addEventListener("click", function (event) {
        var button = event.target.closest("button[data-group]");
        if (!button) return;
        cockpit.group = button.dataset.group;
        var buttons = document.querySelectorAll("#roster-filters button[data-group]");
        for (var i = 0; i < buttons.length; i++) {
          buttons[i].setAttribute("aria-pressed", String(buttons[i].dataset.group === cockpit.group));
        }
        renderRoster();
      });
      // The challenge filter is a local view filter. It re-renders the panel
      // it belongs to and nothing else: roster placement, card truth, the
      // currency banner and the held detail are all untouched, and no request
      // is made.
      document.querySelector("#challenge-filters").addEventListener("click", function (event) {
        var button = event.target.closest("button[data-challenge-filter]");
        if (!button) return;
        cockpit.challengeFilter = button.dataset.challengeFilter;
        renderChallenges();
      });
      var openers = ["#needs-lee", "#burn-loops", "#challenges", "#watchlist", "#roster"];
      for (var i = 0; i < openers.length; i++) {
        document.querySelector(openers[i]).addEventListener("click", function (event) {
          var button = event.target.closest("button[data-employee]");
          if (!button) return;
          openEmployeeDetail(button.dataset.employee, button);
        });
      }
      document.querySelector("#staff-roster").addEventListener("click", function (event) {
        var button = event.target.closest("button[data-employee]");
        if (!button) return;
        openStaffPerson(button.dataset.employee, button);
      });
      var detail = document.querySelector("#employee-detail");
      // Deliberately not focusin: opening the dialog moves focus onto Close
      // itself, and that is the cockpit acting, not Lee.
      var acts = ["keydown", "pointerdown", "mousedown", "touchstart", "scroll", "wheel"];
      for (var a = 0; a < acts.length; a++) detail.addEventListener(acts[a], markDetailInteraction, true);
      detail.addEventListener("click", function (event) {
        if (event.target.id === "employee-detail") { closeEmployeeDetail(); return; }
        if (event.target.id === "employee-detail-close") { closeEmployeeDetail(); return; }
        if (event.target.id === "employee-detail-advanced") { closeEmployeeDetail(); showCockpitView("advanced"); }
      });
      document.addEventListener("keydown", function (event) {
        if (!cockpit.detailEmployee) return;
        if (event.key === "Escape") {
          event.preventDefault();
          closeEmployeeDetail();
          return;
        }
        // A modal that leaks Tab into the page behind it is not modal. Focus is
        // cycled inside the dialog until it is actually closed.
        if (event.key !== "Tab") return;
        var panel = document.querySelector("#employee-detail .mc-detail");
        if (!panel) return;
        var focusable = panel.querySelectorAll("button, summary, a[href], input, select, textarea, [tabindex]");
        var usable = [];
        for (var f = 0; f < focusable.length; f++) {
          if (!trapCanFocus(panel, focusable[f])) continue;
          usable.push(focusable[f]);
        }
        if (usable.length === 0) return;
        var first = usable[0];
        var last = usable[usable.length - 1];
        if (!panel.contains(document.activeElement)) { event.preventDefault(); first.focus(); return; }
        if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last.focus(); return; }
        if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first.focus(); }
      });
      // Currency is re-evaluated on a timer so an artifact that simply ages out
      // stops presenting itself as current even if no poll ever succeeds again.
      setInterval(tickCockpit, 1000);
    }

    var mcLastCurrency = "";

    function tickCockpit() {
      var currency = cockpitCurrency();
      var banner = document.querySelector("#cockpit-currency");
      banner.setAttribute("data-currency", currency);
      banner.textContent = cockpitCurrencyText();
      if (currency !== mcLastCurrency) {
        mcLastCurrency = currency;
        // The roster repeats the banner's currency claim on every card, so it
        // has to move with the banner. While a dialog is open the rebuild is
        // deferred, never skipped: skipping is what left cards saying Current
        // behind a stale banner once the dialog closed. closeEmployeeDetail
        // settles the debt through flushDeferredRoster.
        if (!cockpit.detailEmployee) renderRoster();
        // Every landing section repeats the banner's currency claim, so all of
        // them move with the banner: an artifact that simply ages out must not
        // leave a gate, a burn row, a challenge row or a watchlist row still
        // presenting itself as current. These sections live outside the
        // dialog, so they are rebuilt even while a reading session is open;
        // the held detail itself is never rebuilt here.
        renderGates();
        renderBurning();
        renderChallenges();
        renderWatchlist();
      }
      // The open dialog states its own currency, computed from the artifact it
      // is holding. The fleet's currency is never copied into it.
      if (cockpit.detailEmployee) updateDetailCurrency();
    }
`
