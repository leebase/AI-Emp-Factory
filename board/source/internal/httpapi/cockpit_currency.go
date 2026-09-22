package httpapi

// cockpitCurrencyJS is the open dialog's own currency and authorization badge.
//
// It exists as its own responsibility because it is the one thing in a reading
// session that is allowed to keep changing while Lee reads, and because getting
// it wrong is the failure mode that matters most: a held snapshot presented as
// fresh. It answers two independent questions and never lets one answer the
// other.
//
//  1. Has the artifact Lee is actually holding passed its own deadline? That is
//     recomputed on every ask from the held read time and the held deadline,
//     which were derived once from the artifact's own valid_until through the
//     server's stated age. It is never read from a cached freshness boolean and
//     never from cockpitCurrency(), which describes the fleet, not the hold.
//  2. Can this session still reach the estate at all? Transport and
//     authorization failures are reported as themselves, in their own clause.
//
// A newer successful fleet response is a statement about a different artifact.
// It therefore contributes no clause here at all, and can never relabel the
// held snapshot as current.
const cockpitCurrencyJS = `
    function updateDetailCurrency() {
      var node = document.querySelector("#employee-detail-currency");
      if (!node) return;
      var text = heldCurrencyText();
      node.setAttribute("data-currency", heldExpiry());
      if (node.textContent === text) return;
      node.textContent = text;
    }

    function heldExpiry() {
      var held = cockpit.held;
      if (!held) return "unavailable";
      if (held.arrivedStale || held.arrivedRefreshError) return "stale";
      if (!isFinite(held.expiresAt)) return "stale";
      return Date.now() > held.expiresAt ? "stale" : "current";
    }

    function heldCurrencyText() {
      var held = cockpit.held;
      if (!held) return "";
      var transport = transportClause();
      return heldExpiryClause(held) + (transport ? " " + transport : "");
    }

    function heldExpiryClause(held) {
      var label = "Saved context";
      if (held.arrivedStale || held.arrivedRefreshError) {
        return label + " was already STALE - out of date - when this was opened" +
          (held.arrivedRefreshError ? " after a failed refresh (" + held.arrivedRefreshError + ")" : "") +
          ". It is not current.";
      }
      if (!isFinite(held.expiresAt)) {
        return label + " has no known expiry, so it is not current.";
      }
      var remaining = Math.round((held.expiresAt - Date.now()) / 1000);
      if (remaining <= 0) {
        return label + " EXPIRED " + Math.abs(remaining) + "s ago while you were reading: it is out of date, " +
          "not current. Close and reopen for a newer one.";
      }
      return label + ": current for another " + remaining + "s.";
    }

    function transportClause() {
      if (cockpit.authStatus === 401) {
        return "This browser is no longer signed in, so no newer state can be read.";
      }
      if (cockpit.authStatus) {
        return "This identity is not allowed to read employee state (HTTP " + cockpit.authStatus +
          "), so no newer state can be read.";
      }
      if (cockpit.unavailable) {
        return "Estate reads are failing (" + cockpit.unavailable + "), so no newer state can be read.";
      }
      if (cockpit.refreshError) {
        return "The most recent refresh failed (" + cockpit.refreshError +
          "); that describes the newer read, not the artifact above.";
      }
      return "";
    }
`
