package httpapi

import (
	"errors"
	"net/http"
	"strings"

	"agent-board/internal/domain"
)

var errReporterForbidden = errors.New("reporter identity is authorized only for its own mission cycle reports")

const reporterCycleReportsPath = "/api/auto-orch/cycle-reports/"
const reporterCycleReportsCollectionPath = "/api/auto-orch/cycle-reports"

// isReporter reports whether the identity carries the server-derived reporter
// marker. A malformed marked identity remains restricted by default-deny; local
// auth guarantees the role and grant fields before issuing this marker.
func isReporter(identity domain.AuthIdentity) bool {
	return identity.Reporter
}

// reporterCyclePath returns the only path values a reporter is permitted to
// address. It parses URL.Path because auth middleware runs outside ServeMux,
// before ServeMux has populated PathValue.
func reporterCyclePath(r *http.Request) (string, string, bool) {
	if r.Method != http.MethodPut || !strings.HasPrefix(r.URL.Path, reporterCycleReportsPath) {
		return "", "", false
	}
	parts := strings.Split(strings.TrimPrefix(r.URL.Path, reporterCycleReportsPath), "/")
	if len(parts) != 2 {
		return "", "", false
	}
	mission := parts[0]
	cycle := parts[1]
	if mission == "" || cycle == "" || mission != strings.TrimSpace(mission) || cycle != strings.TrimSpace(cycle) {
		return "", "", false
	}
	return mission, cycle, true
}

// reporterRouteAllowed is the reporter's default-deny route policy. Mission
// matching is done before the handler runs, while employee matching is
// enforced by the report handler after decoding its payload.
func reporterRouteAllowed(r *http.Request, identity domain.AuthIdentity) bool {
	if mission, _, ok := reporterCyclePath(r); ok {
		return mission == identity.ReporterMission
	}
	if r.Method != http.MethodGet || r.URL.Path != reporterCycleReportsCollectionPath {
		return false
	}
	missions, ok := r.URL.Query()["mission"]
	return ok && len(missions) == 1 && missions[0] == identity.ReporterMission
}

// legacyAgentCycleReportDenied preserves the existing route table while
// closing the old per-agent bearer-token path for cycle-report publication.
// Operator/admin human identities continue through their existing policy.
func legacyAgentCycleReportDenied(r *http.Request, identity domain.AuthIdentity) bool {
	_, _, isCycleReport := reporterCyclePath(r)
	return isCycleReport && identity.ActorType == "agent"
}
