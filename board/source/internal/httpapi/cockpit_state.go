package httpapi

// cockpitStateJS is the data half of the Sprint 5 cockpit: it reads the accepted
// active-state artifact, deciding how current it is, and grouping/ordering the
// cards. The rendering half is in cockpit_render.go.
//
// Invariants enforced here, in the order they matter to Lee:
//
//   - Nothing is derived from a raw string. Placement, condition, freshness and
//     gates are read as the closed enums the server already published; an
//     unrecognised enum becomes an explicit unknown, never a guess.
//   - A response that is not the newest in flight is dropped. Every fleet read
//     and every detail read carries a sequence number, so a slow earlier answer
//     can never overwrite a newer selection or a newer accepted state.
//   - Currency is bounded by the client clock. The artifact's own valid_until
//     is translated into a local deadline at read time (using the server's own
//     age, so client/server clock skew cannot extend it) and the view degrades
//     to stale as time passes even when no poll ever succeeds again.
//   - A failed or unauthorised refresh never leaves the previous cards claiming
//     to be current, and never leaves a control on screen implying authority
//     the session no longer has.
const cockpitStateJS = `
    var MC_GROUPS = [
      { key: "running", label: "Running" },
      { key: "scheduled", label: "Scheduled" },
      { key: "on_the_bench", label: "On the Bench" },
      { key: "paused", label: "Paused / Unavailable" },
      { key: "not_commissioned", label: "Not commissioned" },
      { key: "unknown", label: "Placement unknown" }
    ];
    var MC_PLACEMENT_LABEL = {};
    for (var gi = 0; gi < MC_GROUPS.length; gi++) { MC_PLACEMENT_LABEL[MC_GROUPS[gi].key] = MC_GROUPS[gi].label; }
    var MC_CONDITION_ORDER = { burning: 0, blocked: 1, needs_lee: 2, degraded: 3, unknown: 4, healthy: 5 };
    var MC_CONDITION_LABEL = {
      burning: "Burning", blocked: "Blocked", needs_lee: "Needs Lee",
      degraded: "Degraded", unknown: "Condition unknown", healthy: "Healthy"
    };
    var MC_CONDITION_TONE = {
      burning: "bad", blocked: "bad", needs_lee: "warn",
      degraded: "warn", unknown: "warn", healthy: "good"
    };
    var MC_PLACEMENT_TONE = {
      running: "good", scheduled: "good", on_the_bench: "warn",
      paused: "warn", not_commissioned: "warn", unknown: "warn"
    };
    var MC_UNKNOWN = "Unknown";

    var cockpit = {
      seq: 0, detailSeq: 0, state: null, snapshotID: "", serverStale: false,
      ageSeconds: null, readAt: 0, expiresAt: NaN, refreshError: "",
      unavailable: "", authStatus: 0, loading: false,
      group: "all", query: "", detailEmployee: null, detailView: null,
      detailError: "", detailLoading: false, returnFocus: null,
      // detailPanelFor is the employee the dialog element in the DOM was built
      // for, which is how a real open is told apart from anything else.
      detailPanelFor: null,
      // held is the artifact material captured at open and bound to that one
      // employee. Every artifact-derived row in an open dialog is read from
      // here and never from cockpit.state, so a later fleet response cannot
      // change what Lee is already reading.
      held: null,
      // detailWritten records that this reading session already wrote its body.
      // A session writes that body once and then owns it until close or reopen.
      detailWritten: false,
      // detailInteracted records that Lee has already acted inside the open
      // dialog, so a late initial completion may not move scroll or focus.
      detailInteracted: false,
      // rosterCurrency is the currency the roster cards were last rendered
      // with. It is compared against the live currency so a card can never be
      // left claiming Current behind a stale banner.
      rosterCurrency: ""
    };

    // enumOr keeps presentation honest about vocabularies it does not know.
    function enumOr(value, table) {
      var key = typeof value === "string" ? value : "";
      return Object.prototype.hasOwnProperty.call(table, key) ? key : "unknown";
    }

    function placementKey(card) { return enumOr(card && card.placement, MC_PLACEMENT_LABEL); }
    function conditionKey(card) { return enumOr(card && card.condition, MC_CONDITION_LABEL); }

    // countText never prints a confident zero for an unknown count.
    function countText(value) {
      return typeof value === "number" && isFinite(value) ? String(value) : MC_UNKNOWN;
    }

    function textOr(value, fallback) {
      var text = typeof value === "string" ? value.trim() : "";
      return text === "" ? (fallback || MC_UNKNOWN) : text;
    }

    // optionalText is for fields whose absence is simply absence, not an
    // unknown fact: an omitted refresh_error must never be reported to Lee as
    // a refresh that failed for an unknown reason.
    function optionalText(value) {
      return typeof value === "string" ? value.trim() : "";
    }

    function parseInstant(value) {
      if (typeof value !== "string" || value === "") return NaN;
      var parsed = Date.parse(value);
      return isFinite(parsed) ? parsed : NaN;
    }

    // relativeTime describes an instant against the estate's own read time, so
    // ages stay meaningful when the browser clock disagrees with the server.
    // An explicit base lets a held snapshot state ages against its own clock
    // rather than against whatever artifact has arrived since.
    function relativeTime(value, base) {
      var at = parseInstant(value);
      if (!isFinite(at)) return MC_UNKNOWN;
      var serverNow = isFinite(base) ? base : cockpitServerNow();
      if (!isFinite(serverNow)) return value;
      var seconds = Math.round((serverNow - at) / 1000);
      var ago = seconds >= 0;
      var magnitude = Math.abs(seconds);
      var unit = "s";
      if (magnitude >= 86400) { magnitude = Math.round(magnitude / 86400); unit = "d"; }
      else if (magnitude >= 3600) { magnitude = Math.round(magnitude / 3600); unit = "h"; }
      else if (magnitude >= 60) { magnitude = Math.round(magnitude / 60); unit = "m"; }
      return ago ? magnitude + unit + " ago" : "in " + magnitude + unit;
    }

    function cockpitServerNow() {
      if (!cockpit.state) return NaN;
      var generated = parseInstant(cockpit.state.generated_at);
      if (!isFinite(generated) || cockpit.ageSeconds === null) return NaN;
      return generated + cockpit.ageSeconds * 1000 + (Date.now() - cockpit.readAt);
    }

    async function loadCockpit(refresh) {
      var seq = ++cockpit.seq;
      cockpit.loading = true;
      renderCockpit();
      try {
        var response = await sessionFetch("/api/operations/active-state" + (refresh ? "?refresh=1" : ""));
        if (seq !== cockpit.seq) return;
        if (response.status === 401 || response.status === 403) { cockpitDenied(response.status); return; }
        if (!response.ok) { cockpitUnavailable("HTTP " + response.status); return; }
        var body = await response.json();
        if (seq !== cockpit.seq) return;
        adoptActiveState(body);
      } catch (error) {
        if (seq !== cockpit.seq) return;
        cockpitUnavailable((error && error.message) || "request failed");
      } finally {
        if (seq === cockpit.seq) {
          cockpit.loading = false;
          renderCockpit();
        }
      }
    }

    function adoptActiveState(body) {
      var state = body && body.active_state;
      if (!state || !Array.isArray(state.cards)) {
        cockpitUnavailable("the response carried no active-state artifact");
        return;
      }
      cockpit.state = state;
      cockpit.snapshotID = optionalText(body.snapshot_id || state.snapshot_id);
      cockpit.serverStale = body.stale === true;
      cockpit.ageSeconds = typeof body.age_seconds === "number" ? body.age_seconds : null;
      cockpit.refreshError = optionalText(body.refresh_error);
      cockpit.readAt = Date.now();
      cockpit.unavailable = "";
      cockpit.authStatus = 0;
      // Translate the artifact's own bound into a local deadline using the
      // server's stated age. If either is missing the artifact is treated as
      // already expired rather than trusted indefinitely.
      var validUntil = parseInstant(state.valid_until);
      var generated = parseInstant(state.generated_at);
      if (isFinite(validUntil) && isFinite(generated) && cockpit.ageSeconds !== null) {
        cockpit.expiresAt = cockpit.readAt + (validUntil - (generated + cockpit.ageSeconds * 1000));
      } else {
        cockpit.expiresAt = NaN;
      }
    }

    function cockpitUnavailable(message) {
      cockpit.unavailable = message;
      cockpit.loading = false;
    }

    // A denied refresh keeps whatever was last known, but the whole view stops
    // claiming to be current and the refresh control stops claiming authority.
    function cockpitDenied(status) {
      cockpit.authStatus = status;
      cockpit.unavailable = "";
      cockpit.loading = false;
      renderCockpit();
    }

    function cockpitCurrency() {
      if (cockpit.authStatus || cockpit.unavailable) return "unavailable";
      if (!cockpit.state) return cockpit.loading ? "loading" : "unavailable";
      if (cockpit.loading) return "loading";
      if (cockpit.serverStale || cockpit.refreshError) return "stale";
      if (!isFinite(cockpit.expiresAt) || Date.now() > cockpit.expiresAt) return "stale";
      return "current";
    }

    function cockpitCurrencyText() {
      var currency = cockpitCurrency();
      if (currency === "loading") return "Loading estate state...";
      if (currency === "unavailable") {
        var base = cockpit.authStatus
          ? authMessage(cockpit.authStatus, "employee state")
          : "Employee state is unavailable: " + cockpit.unavailable;
        return cockpit.state
          ? base + " The cards below are the last state this browser saw and are NOT current."
          : base + " No employee state is being shown.";
      }
      var snapshot = cockpit.snapshotID ? " Snapshot " + cockpit.snapshotID + "." : "";
      if (currency === "stale") {
        var why = cockpit.refreshError ? " Last refresh failed: " + cockpit.refreshError + "." : "";
        return "STALE last-known state, not current truth." + why + snapshot;
      }
      var remaining = Math.max(0, Math.round((cockpit.expiresAt - Date.now()) / 1000));
      return "Current state, valid for another " + remaining + "s." + snapshot;
    }

    function cockpitVisibleCards() {
      var cards = (cockpit.state && Array.isArray(cockpit.state.cards)) ? cockpit.state.cards : [];
      var query = cockpit.query.trim().toLowerCase();
      var visible = [];
      for (var i = 0; i < cards.length; i++) {
        var card = cards[i];
        var id = typeof card.employee_id === "string" ? card.employee_id : "";
        if (query !== "" && id.toLowerCase().indexOf(query) < 0) continue;
        if (cockpit.group !== "all" && placementKey(card) !== cockpit.group) continue;
        visible.push(card);
      }
      return visible;
    }

    // Ordering is severity first, then employee id, so the same fleet always
    // renders in the same order and the worst condition is never buried.
    function compareCards(a, b) {
      var ca = MC_CONDITION_ORDER[conditionKey(a)];
      var cb = MC_CONDITION_ORDER[conditionKey(b)];
      if (ca !== cb) return ca - cb;
      var ia = typeof a.employee_id === "string" ? a.employee_id : "";
      var ib = typeof b.employee_id === "string" ? b.employee_id : "";
      return ia < ib ? -1 : (ia > ib ? 1 : 0);
    }

    function cockpitGroupedCards() {
      var visible = cockpitVisibleCards();
      var groups = [];
      for (var i = 0; i < MC_GROUPS.length; i++) {
        var group = MC_GROUPS[i];
        var members = [];
        for (var j = 0; j < visible.length; j++) {
          if (placementKey(visible[j]) === group.key) members.push(visible[j]);
        }
        members.sort(compareCards);
        if (members.length > 0) groups.push({ key: group.key, label: group.label, cards: members });
      }
      return groups;
    }

    // Only genuinely required gates reach the landing list. required=false is
    // preserved in the artifact and shown in detail, never promoted here.
    function cockpitRequiredGates() {
      var entries = (cockpit.state && Array.isArray(cockpit.state.needs_lee)) ? cockpit.state.needs_lee : [];
      var required = [];
      for (var i = 0; i < entries.length; i++) {
        if (entries[i] && entries[i].gate && entries[i].gate.required === true) required.push(entries[i]);
      }
      required.sort(function (a, b) {
        var ra = parseInstant(a.gate.raised_at);
        var rb = parseInstant(b.gate.raised_at);
        if (isFinite(ra) && isFinite(rb) && ra !== rb) return ra - rb;
        var ia = textOr(a.employee_id, ""), ib = textOr(b.employee_id, "");
        return ia < ib ? -1 : (ia > ib ? 1 : 0);
      });
      return required;
    }

    function cockpitBurning() {
      var cards = (cockpit.state && Array.isArray(cockpit.state.cards)) ? cockpit.state.cards : [];
      var burning = [];
      for (var i = 0; i < cards.length; i++) {
        if (conditionKey(cards[i]) === "burning") burning.push(cards[i]);
      }
      burning.sort(compareCards);
      return burning;
    }

    function cockpitFindingsFor(card, category) {
      var findings = Array.isArray(card && card.findings) ? card.findings : [];
      var matched = [];
      for (var i = 0; i < findings.length; i++) {
        if (findings[i] && findings[i].category === category) matched.push(findings[i]);
      }
      return matched;
    }
`
