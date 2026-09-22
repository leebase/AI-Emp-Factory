package httpapi

import (
	"bytes"
	"context"
	"crypto/sha256"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"log"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"agent-board/internal/auth"
	"agent-board/internal/config"
	"agent-board/internal/domain"
	"agent-board/internal/services"
	"agent-board/internal/store"
)

type authIdentityKey struct{}

type dashboardAuthorizedKey struct{}

const maxArtifactUploadBytes = 10 << 20

type Server struct {
	cfg          config.Config
	board        *services.Board
	mux          *http.ServeMux
	pollRequests atomic.Uint64
	authn        auth.AuthenticationProvider
}

type auditResponseWriter struct {
	http.ResponseWriter
	status      int
	wroteHeader bool
}

func (w *auditResponseWriter) WriteHeader(status int) {
	if w.wroteHeader {
		return
	}
	w.status = status
	w.wroteHeader = true
	w.ResponseWriter.WriteHeader(status)
}

func (w *auditResponseWriter) Write(body []byte) (int, error) {
	if !w.wroteHeader {
		w.WriteHeader(http.StatusOK)
	}
	return w.ResponseWriter.Write(body)
}

func (w *auditResponseWriter) Flush() {
	if !w.wroteHeader {
		w.WriteHeader(http.StatusOK)
	}
	if flusher, ok := w.ResponseWriter.(http.Flusher); ok {
		flusher.Flush()
	}
}

func (s *Server) recordAudit(r *http.Request, identity domain.AuthIdentity, response *auditResponseWriter) {
	if identity.ActorID == "" || identity.ActorType == "" {
		return
	}
	if err := s.board.RecordAudit(context.WithoutCancel(r.Context()), store.AuditRecordInput{
		Method:       r.Method,
		Path:         r.URL.Path,
		Status:       response.status,
		ActorType:    identity.ActorType,
		ActorID:      identity.ActorID,
		Roles:        identity.Roles,
		AuthProvider: identity.AuthProvider,
	}); err != nil {
		log.Printf("audit record failed for %s %s: %v", r.Method, r.URL.Path, err)
	}
}

// New constructs the server. The provider supplies identity; authorization
// remains in this package and continues to consume roles.
func New(cfg config.Config, board *services.Board, authn auth.AuthenticationProvider) *Server {
	server := &Server{
		cfg:   cfg,
		board: board,
		mux:   http.NewServeMux(),
		authn: authn,
	}
	server.routes()
	return server
}

func (s *Server) Handler() http.Handler {
	return s.auth(s.mux)
}

func (s *Server) StartStaleSweeper(ctx context.Context) {
	if s.cfg.StaleInterval <= 0 {
		return
	}
	go func() {
		ticker := time.NewTicker(s.cfg.StaleInterval)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				s.sweepStaleState(ctx)
			}
		}
	}()
}

func (s *Server) sweepStaleState(ctx context.Context) {
	// Lease expiry and presence decay run independently: a lease-sweep failure
	// must not skip presence decay (else /snapshot online counts stay stale).
	if expired, err := s.board.ExpireStaleLeases(ctx); err != nil {
		log.Printf("stale lease sweep failed: %v", err)
	} else if expired > 0 {
		log.Printf("expired %d stale leases", expired)
	}
	if machines, agents, err := s.board.DecayPresence(ctx, s.cfg.EffectivePresenceWindow()); err != nil {
		log.Printf("presence decay failed: %v", err)
	} else if machines > 0 || agents > 0 {
		log.Printf("marked %d machines and %d agents offline after presence decay", machines, agents)
	}
}

func (s *Server) routes() {
	if s.authn != nil {
		s.authn.RegisterRoutes(s.mux)
		if _, ok := s.authn.(auth.ReloadableAuthenticationProvider); ok {
			s.mux.HandleFunc("POST /auth/admin/reload", s.reloadAuthentication)
		}
	}
	s.mux.HandleFunc("GET /{$}", s.dashboard)
	s.mux.HandleFunc("GET /dashboard", s.dashboard)
	s.mux.HandleFunc("GET /health", s.health)
	s.mux.HandleFunc("GET /api/session", s.sessionInfo)
	s.mux.HandleFunc("GET /status", s.status)
	s.mux.HandleFunc("GET /snapshot", s.snapshot)
	s.mux.HandleFunc("GET /employees", s.employees)
	s.mux.HandleFunc("GET /api/employee-roster", s.employeeRoster)
	s.mux.HandleFunc("GET /api/employee-roster/{name}", s.employeeRosterDetail)
	s.mux.HandleFunc("GET /api/staff-view", s.staffView)
	s.mux.HandleFunc("GET /projects/state", s.projectStates)
	s.mux.HandleFunc("GET /metrics", s.metrics)
	s.mux.HandleFunc("GET /api/auto-orch/cycle-reports", s.listAutoOrchCycleReports)
	s.mux.HandleFunc("PUT /api/auto-orch/cycle-reports/{mission}/{cycle}", s.upsertAutoOrchCycleReport)
	s.mux.HandleFunc("GET /api/review-tasks/{mission}/{run}/{gate}", s.getBoardReviewTask)
	s.mux.HandleFunc("PUT /api/review-tasks/{mission}/{run}/{gate}", s.upsertBoardReviewTask)
	s.mux.HandleFunc("POST /api/review-tasks/{mission}/{run}/{gate}/{action}", s.applyBoardReviewDisposition)
	s.mux.HandleFunc("POST /api/operations/observations", s.ingestObservation)
	s.mux.HandleFunc("GET /api/operations/employees", s.operationsEmployees)
	s.mux.HandleFunc("GET /api/operations/employees/{employee_id}", s.operationsEmployee)
	s.mux.HandleFunc("GET /api/operations/managerial/employees", s.managerialEmployees)
	s.mux.HandleFunc("GET /api/operations/managerial/employees/{employee_id}", s.managerialEmployee)
	s.mux.HandleFunc("GET /api/operations/active-state", s.activeState)
	s.mux.HandleFunc("GET /machines", s.machines)
	s.mux.HandleFunc("GET /agents", s.agents)
	s.mux.HandleFunc("POST /machines/register", s.registerMachine)
	s.mux.HandleFunc("POST /agents/register", s.registerAgent)
	s.mux.HandleFunc("POST /tasks", s.createTask)
	s.mux.HandleFunc("GET /tasks", s.listTasks)
	s.mux.HandleFunc("GET /tasks/{id}", s.showTask)
	s.mux.HandleFunc("POST /tasks/{id}/review", s.reviewTask)
	s.mux.HandleFunc("POST /tasks/{id}/approve", s.approveTask)
	s.mux.HandleFunc("POST /tasks/{id}/send-back", s.sendBackTask)
	s.mux.HandleFunc("POST /tasks/{id}/complete", s.completeTask)
	s.mux.HandleFunc("POST /tasks/{id}/block", s.blockTask)
	s.mux.HandleFunc("POST /tasks/{id}/fail", s.failTask)
	s.mux.HandleFunc("POST /tasks/{id}/cancel", s.cancelTask)
	s.mux.HandleFunc("GET /tasks/{id}/tree", s.taskTree)
	s.mux.HandleFunc("GET /tasks/{id}/events", s.taskEvents)
	s.mux.HandleFunc("GET /tasks/{id}/messages", s.taskMessages)
	s.mux.HandleFunc("GET /tasks/{id}/artifacts", s.taskArtifacts)
	s.mux.HandleFunc("GET /events", s.events)
	s.mux.HandleFunc("GET /leases/active", s.activeLeases)
	s.mux.HandleFunc("GET /leases/stale", s.staleLeases)
	s.mux.HandleFunc("POST /messages", s.createMessage)
	s.mux.HandleFunc("POST /messages/{id}/ack", s.ackMessage)
	s.mux.HandleFunc("GET /inbox", s.inbox)
	s.mux.HandleFunc("POST /artifacts", s.addArtifact)
	s.mux.HandleFunc("POST /artifacts/upload", s.uploadArtifact)
	s.mux.HandleFunc("GET /artifacts/{id}/download", s.downloadArtifact)
	s.mux.HandleFunc("POST /poll", s.poll)
	s.mux.HandleFunc("POST /leases/{id}/renew", s.renewLease)
	s.mux.HandleFunc("POST /stale/expire", s.expireStale)
}

func (s *Server) auth(next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		// GET /health is the reporter's public health exception. Keep anonymous
		// HEAD health probes working, but let bearer-authenticated reporters pass
		// through authorization so HEAD is not an additional reporter route.
		if r.URL.Path == "/health" && (r.Method == http.MethodGet || (r.Method == http.MethodHead && !auth.HasBearerCredential(r))) {
			next.ServeHTTP(w, r)
			return
		}
		// Cookie-authenticated mutations, login and logout included, must be
		// same-origin. The bearer machine seam is exempt inside this check.
		if err := enforceSameOriginMutation(r); err != nil {
			writeError(w, http.StatusForbidden, err)
			return
		}
		if s.authn != nil && strings.HasPrefix(r.URL.Path, "/auth/") {
			// The login flow itself must be reachable unauthenticated — that's
			// the entire point. /auth/login establishes a session; /auth/logout
			// only ever clears one, harmless either way.
			if identity, authErr := s.authenticate(r); authErr == nil {
				if s.denyMachineHumanAdmin(w, r, identity) {
					return
				}
				if isReporter(identity) {
					writeError(w, http.StatusForbidden, errReporterForbidden)
					return
				}
				r = r.WithContext(context.WithValue(r.Context(), authIdentityKey{}, identity))
				response := &auditResponseWriter{ResponseWriter: w, status: http.StatusOK}
				defer s.recordAudit(r, identity, response)
				next.ServeHTTP(response, r)
				return
			}
			next.ServeHTTP(w, r)
			return
		}
		if isDashboardShell(r) {
			// The dashboard shell itself carries no board data — every panel is
			// populated by its own authenticated fetch() call from client-side JS,
			// and its sign-in link to the local-auth login form is how a human
			// establishes a session in the first place; the shell never accepts a
			// credential itself. Always render the shell so an unauthenticated
			// browser can
			// reach it at all; the response status still reports whether the
			// request itself was authorized (401 by default, matching every
			// other route) so non-browser clients see the same signal.
			identity, authErr := s.authenticate(r)
			participant := authErr == nil && isParticipant(identity)
			reporter := authErr == nil && isReporter(identity)
			authorized := (authErr == nil && !participant) || (errors.Is(authErr, auth.ErrNoCredentials) && s.cfg.PublicRead)
			r = r.WithContext(context.WithValue(r.Context(), dashboardAuthorizedKey{}, authorized))
			if reporter {
				writeError(w, http.StatusForbidden, errReporterForbidden)
				return
			}
			if authErr == nil && !participant {
				r = r.WithContext(context.WithValue(r.Context(), authIdentityKey{}, identity))
				response := &auditResponseWriter{ResponseWriter: w, status: http.StatusOK}
				defer s.recordAudit(r, identity, response)
				next.ServeHTTP(response, r)
				return
			}
			next.ServeHTTP(w, r)
			return
		}
		identity, err := s.authenticate(r)
		if errors.Is(err, auth.ErrNoCredentials) && s.cfg.PublicRead && isPublicReadEndpoint(r) {
			next.ServeHTTP(w, r)
			return
		}
		if err != nil {
			writeError(w, http.StatusUnauthorized, err)
			return
		}
		response := &auditResponseWriter{ResponseWriter: w, status: http.StatusOK}
		defer s.recordAudit(r, identity, response)
		w = response
		if s.denyMachineHumanAdmin(w, r, identity) {
			return
		}
		if isReporter(identity) {
			if !reporterRouteAllowed(r, identity) {
				writeError(w, http.StatusForbidden, errReporterForbidden)
				return
			}
		} else if legacyAgentCycleReportDenied(r, identity) {
			writeError(w, http.StatusForbidden, errors.New("legacy agent identity is not authorized to publish cycle reports"))
			return
		} else if isParticipant(identity) {
			// A verified participant is governed only by its explicit
			// resource scope; it must never inherit the human role hierarchy or
			// the empty-Role trusted-machine bypass.
			if !participantRouteAllowed(r) {
				writeError(w, http.StatusForbidden, errParticipantForbidden)
				return
			}
		} else if isScopedProducer(identity) {
			// A granted observation producer is restricted to its ingestion
			// write seam and never gains generic operator write privileges or
			// access to the fleet read model.
			if !scopedProducerRouteAllowed(r) {
				writeError(w, http.StatusForbidden, errScopedProducerForbidden)
				return
			}
		} else if identity.Role != "" {
			if isClientReviewer(identity) && !clientReviewerRouteAllowed(r) {
				writeError(w, http.StatusForbidden, errors.New("client reviewer may access assigned review evidence only"))
				return
			}
			minRole := auth.RoleOperator
			if r.Method == http.MethodGet || r.Method == http.MethodHead {
				minRole = auth.RoleViewer
			}
			if !auth.RoleAtLeast(identity.Role, minRole) {
				writeError(w, http.StatusForbidden, fmt.Errorf("role %q may not %s %s", identity.Role, r.Method, r.URL.Path))
				return
			}
		}
		r = r.WithContext(context.WithValue(r.Context(), authIdentityKey{}, identity))
		next.ServeHTTP(w, r)
	})
}

func (s *Server) reloadAuthentication(w http.ResponseWriter, r *http.Request) {
	identity, ok := authIdentity(r)
	if !ok {
		writeError(w, http.StatusUnauthorized, auth.ErrNoCredentials)
		return
	}
	if identity.ActorType != "human" || identity.Role != auth.RoleAdmin {
		writeError(w, http.StatusForbidden, errors.New("authentication reload requires an admin identity"))
		return
	}
	reloader, ok := s.authn.(auth.ReloadableAuthenticationProvider)
	if !ok {
		writeError(w, http.StatusNotImplemented, errors.New("authentication provider does not support reload"))
		return
	}
	if err := reloader.Reload(); err != nil {
		writeError(w, http.StatusInternalServerError, fmt.Errorf("authentication reload failed: %w", err))
		return
	}
	writeJSON(w, http.StatusOK, map[string]string{"status": "reloaded"})
}

func (s *Server) authenticate(r *http.Request) (domain.AuthIdentity, error) {
	if s.authn == nil {
		return domain.AuthIdentity{}, auth.ErrNoCredentials
	}
	identity, err := s.authn.AuthenticateRequest(r.Context(), r)
	if err != nil {
		return domain.AuthIdentity{}, err
	}
	if identity.AuthProvider == "" {
		identity.AuthProvider = s.authn.Name()
	}
	return identity, nil
}

func isDashboardShell(r *http.Request) bool {
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		return false
	}
	return r.URL.Path == "/" || r.URL.Path == "/dashboard"
}

func isPublicReadEndpoint(r *http.Request) bool {
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		return false
	}
	if r.URL.Path == "/status" || r.URL.Path == "/snapshot" || r.URL.Path == "/employees" || r.URL.Path == "/api/employee-roster" || strings.HasPrefix(r.URL.Path, "/api/employee-roster/") || r.URL.Path == "/projects/state" || r.URL.Path == "/metrics" || r.URL.Path == "/api/auto-orch/cycle-reports" {
		return true
	}
	if r.URL.Path == "/machines" || r.URL.Path == "/agents" || r.URL.Path == "/events" || r.URL.Path == "/leases/active" || r.URL.Path == "/leases/stale" {
		return true
	}
	if r.URL.Path == "/tasks" || strings.HasPrefix(r.URL.Path, "/tasks/") {
		return true
	}
	if strings.HasPrefix(r.URL.Path, "/artifacts/") && strings.HasSuffix(r.URL.Path, "/download") {
		return true
	}
	return false
}

func isClientReviewer(identity domain.AuthIdentity) bool {
	// A higher role may legitimately include "reviewer" as one of its
	// capabilities (the local Lee example is admin/operator/reviewer). The
	// client-review boundary applies only when the effective role is the
	// read-only viewer role; membership in the full role set alone must not
	// downgrade an operator or administrator.
	return identity.ActorType == "human" && identity.Role == auth.RoleViewer
}

func clientReviewerRouteAllowed(r *http.Request) bool {
	if r.Method != http.MethodGet && r.Method != http.MethodHead {
		return false
	}
	// Reading your own identity is not estate access. /api/session answers only
	// "who am I, and what may I not do?" for the credential the caller already
	// holds, so refusing it told a valid signed-in reviewer it was not signed in
	// at all. It stays the single self-identity exception: no board, fleet or
	// review state is reachable through it.
	if r.URL.Path == "/api/session" {
		return true
	}
	if r.URL.Path == "/tasks" || r.URL.Path == "/api/auto-orch/cycle-reports" {
		return true
	}
	if strings.HasPrefix(r.URL.Path, "/api/review-tasks/") {
		return len(strings.Split(strings.Trim(r.URL.Path, "/"), "/")) == 5
	}
	parts := strings.Split(strings.Trim(r.URL.Path, "/"), "/")
	if len(parts) == 2 && parts[0] == "tasks" {
		return true
	}
	return len(parts) == 3 && parts[0] == "tasks" && parts[2] == "artifacts"
}

func (s *Server) dashboard(w http.ResponseWriter, r *http.Request) {
	status := http.StatusOK
	if authorized, ok := r.Context().Value(dashboardAuthorizedKey{}).(bool); ok && !authorized {
		status = http.StatusUnauthorized
	}
	w.Header().Set("Content-Type", "text/html; charset=utf-8")
	w.WriteHeader(status)
	_, _ = w.Write([]byte(renderDashboard(s.authn != nil)))
}

// renderDashboard assembles the single embedded dashboard document from its
// server-side parts. The cockpit stylesheet and scripts live in their own Go
// files purely so each stays readable; there is still exactly one page, one
// asset pipeline and no second frontend stack.
func renderDashboard(authEnabled bool) string {
	return strings.NewReplacer(
		"%%AUTH_ENABLED%%", strconv.FormatBool(authEnabled),
		"%%COCKPIT_CSS%%", cockpitStyleSheet(),
		"%%COCKPIT_JS%%", cockpitScriptAssets(),
	).Replace(dashboardHTML)
}

func (s *Server) health(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, map[string]string{"status": "ok"})
}

func (s *Server) status(w http.ResponseWriter, r *http.Request) {
	status, err := s.board.Status(r.Context())
	writeResult(w, status, err)
}

func (s *Server) snapshot(w http.ResponseWriter, r *http.Request) {
	snapshot, err := s.board.Snapshot(r.Context())
	writeResult(w, snapshot, err)
}

func (s *Server) employees(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.board.ListEmployees())
}

func (s *Server) employeeRoster(w http.ResponseWriter, r *http.Request) {
	writeJSON(w, http.StatusOK, s.board.EmployeeRoster(r.Context()))
}

func (s *Server) employeeRosterDetail(w http.ResponseWriter, r *http.Request) {
	name := strings.TrimSpace(r.PathValue("name"))
	detail, err := s.board.EmployeeDetail(r.Context(), name)
	if err != nil {
		writeError(w, http.StatusNotFound, err)
		return
	}
	writeJSON(w, http.StatusOK, detail)
}

func (s *Server) projectStates(w http.ResponseWriter, r *http.Request) {
	states, err := s.board.ProjectStates(r.Context(), r.URL.Query().Get("project_id"))
	writeResult(w, states, err)
}

func (s *Server) metrics(w http.ResponseWriter, r *http.Request) {
	snapshot, err := s.board.Snapshot(r.Context())
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	body := prometheusMetrics(snapshot, s.pollRequests.Load())
	w.Header().Set("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
	w.WriteHeader(http.StatusOK)
	_, _ = w.Write(body)
}

func (s *Server) upsertAutoOrchCycleReport(w http.ResponseWriter, r *http.Request) {
	missionName := strings.TrimSpace(r.PathValue("mission"))
	cycleID := strings.TrimSpace(r.PathValue("cycle"))
	if missionName == "" || cycleID == "" {
		writeError(w, http.StatusBadRequest, errors.New("mission and cycle path values are required"))
		return
	}
	identity, hasIdentity := authIdentity(r)
	if hasIdentity && isReporter(identity) && !reporterRouteAllowed(r, identity) {
		writeError(w, http.StatusForbidden, errReporterForbidden)
		return
	}

	var payload map[string]any
	if !decode(w, r, &payload) {
		return
	}
	if hasIdentity && isReporter(identity) {
		if payload == nil {
			payload = make(map[string]any)
		}
		rawEmployeeID, present := payload["employee_id"]
		if present {
			employeeID, ok := rawEmployeeID.(string)
			if !ok || strings.TrimSpace(employeeID) != identity.ReporterEmployeeID {
				writeError(w, http.StatusForbidden, errors.New("reporter employee_id does not match its grant"))
				return
			}
		}
		// The stored identity is always the trusted grant, including when the
		// caller supplied an equivalent value with surrounding whitespace.
		payload["employee_id"] = identity.ReporterEmployeeID
	}
	payloadJSON, err := json.Marshal(payload)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	idempotencyKey := strings.TrimSpace(r.Header.Get("Idempotency-Key"))
	if idempotencyKey == "" {
		if value, ok := payload["idempotency_key"].(string); ok {
			idempotencyKey = strings.TrimSpace(value)
		}
	}
	actorID := ""
	if identity, ok := authIdentity(r); ok {
		actorID = identity.ActorID
	}
	employeeID, _ := payload["employee_id"].(string)
	result, err := s.board.UpsertAutoOrchCycleReport(r.Context(), store.UpsertAutoOrchCycleReportInput{
		MissionName:    missionName,
		EmployeeID:     strings.TrimSpace(employeeID),
		CycleID:        cycleID,
		IdempotencyKey: idempotencyKey,
		PayloadJSON:    string(payloadJSON),
		ActorID:        actorID,
	})
	writeResult(w, result, err)
}

func (s *Server) listAutoOrchCycleReports(w http.ResponseWriter, r *http.Request) {
	limit := 50
	if rawLimit := strings.TrimSpace(r.URL.Query().Get("limit")); rawLimit != "" {
		parsed, err := strconv.Atoi(rawLimit)
		if err != nil {
			writeError(w, http.StatusBadRequest, fmt.Errorf("invalid limit %q", rawLimit))
			return
		}
		limit = parsed
	}
	mission := r.URL.Query().Get("mission")
	if identity, ok := authIdentity(r); ok && isReporter(identity) {
		// reporterRouteAllowed has already required one matching mission query
		// value. Use the authenticated grant again here so the list operation can
		// never be widened by a caller-controlled filter.
		mission = identity.ReporterMission
	}
	if identity, ok := authIdentity(r); ok && isClientReviewer(identity) {
		result, err := s.listClientReviewerCycleReports(r.Context(), identity, mission, limit)
		writeResult(w, result, err)
		return
	}
	result, err := s.board.ListAutoOrchCycleReports(r.Context(), mission, limit)
	if err == nil && result == nil {
		// An empty collection is a JSON list, never null: reporters and other
		// clients parse this body as a list (D346 B5 preflight/readback).
		result = []domain.AutoOrchCycleReport{}
	}
	writeResult(w, result, err)
}

func assignedClientReviewTask(task domain.BoardReviewTask, identity domain.AuthIdentity) bool {
	return task.Task.Status == domain.TaskStatusReview &&
		task.Task.ReviewTargetType == "human" &&
		task.Task.ReviewTargetID != "" &&
		task.Task.ReviewTargetID == identity.ActorID
}

func (s *Server) listClientReviewerCycleReports(ctx context.Context, identity domain.AuthIdentity, mission string, limit int) ([]domain.AutoOrchCycleReport, error) {
	if limit <= 0 || limit > 100 {
		limit = 50
	}
	reviewTasks, err := s.board.ListAssignedBoardReviewTasks(ctx, "human", identity.ActorID)
	if err != nil {
		return nil, err
	}
	allowedRuns := make(map[string]struct{}, len(reviewTasks))
	for _, reviewTask := range reviewTasks {
		if mission != "" && reviewTask.MissionName != mission {
			continue
		}
		allowedRuns[reviewTask.MissionName+"\x00"+reviewTask.RunID] = struct{}{}
	}

	// Fetch before filtering so an unrelated recent report cannot displace a
	// reviewer's linked report merely because it sorted ahead of it.
	reports, err := s.board.ListAutoOrchCycleReports(ctx, mission, 100)
	if err != nil {
		return nil, err
	}
	filtered := make([]domain.AutoOrchCycleReport, 0, len(reports))
	for _, report := range reports {
		var payload struct {
			Run struct {
				ID string `json:"id"`
			} `json:"run"`
		}
		if err := json.Unmarshal([]byte(report.PayloadJSON), &payload); err != nil {
			continue
		}
		if _, ok := allowedRuns[report.MissionName+"\x00"+payload.Run.ID]; ok {
			filtered = append(filtered, sanitizeClientCycleReport(report))
		}
	}
	if limit < len(filtered) {
		filtered = filtered[:limit]
	}
	return filtered, nil
}

func sanitizeClientCycleReport(report domain.AutoOrchCycleReport) domain.AutoOrchCycleReport {
	report.ActorID = ""
	var payload any
	if err := json.Unmarshal([]byte(report.PayloadJSON), &payload); err != nil {
		report.PayloadJSON = "{}"
		return report
	}
	sanitized := scrubClientReportValue(payload)
	encoded, err := json.Marshal(sanitized)
	if err != nil {
		report.PayloadJSON = "{}"
	} else {
		report.PayloadJSON = string(encoded)
	}
	return report
}

func scrubClientReportValue(value any) any {
	switch typed := value.(type) {
	case map[string]any:
		result := make(map[string]any, len(typed))
		for key, item := range typed {
			if clientReportSensitiveKey(key) {
				continue
			}
			result[key] = scrubClientReportValue(item)
		}
		return result
	case []any:
		result := make([]any, len(typed))
		for index, item := range typed {
			result[index] = scrubClientReportValue(item)
		}
		return result
	default:
		return value
	}
}

func clientReportSensitiveKey(key string) bool {
	normalized := strings.ToLower(strings.NewReplacer("-", "", "_", "", " ", "").Replace(key))
	for _, fragment := range []string{"secret", "token", "password", "credential", "authorization", "apikey", "prompt", "environment"} {
		if strings.Contains(normalized, fragment) {
			return true
		}
	}
	return normalized == "env" || normalized == "config" || normalized == "machine"
}

func (s *Server) getBoardReviewTask(w http.ResponseWriter, r *http.Request) {
	result, err := s.board.GetBoardReviewTask(r.Context(), r.PathValue("mission"), r.PathValue("run"), r.PathValue("gate"))
	if identity, ok := authIdentity(r); ok && isClientReviewer(identity) {
		if err != nil || !assignedClientReviewTask(result, identity) {
			writeError(w, http.StatusNotFound, errors.New("review task not found"))
			return
		}
	}
	writeResult(w, result, err)
}

func (s *Server) upsertBoardReviewTask(w http.ResponseWriter, r *http.Request) {
	var input store.UpsertBoardReviewTaskInput
	if !decode(w, r, &input) {
		return
	}
	input.MissionName = r.PathValue("mission")
	input.RunID = r.PathValue("run")
	input.ReviewGate = r.PathValue("gate")
	if input.IdempotencyKey == "" {
		input.IdempotencyKey = strings.TrimSpace(r.Header.Get("Idempotency-Key"))
	}
	if identity, ok := authIdentity(r); ok {
		input.ActorType = identity.ActorType
		input.ActorID = identity.ActorID
	} else {
		input.ActorType = "agent"
		input.ActorID = "auto-orch"
	}
	result, err := s.board.UpsertBoardReviewTask(r.Context(), input)
	writeResult(w, result, err)
}

func (s *Server) applyBoardReviewDisposition(w http.ResponseWriter, r *http.Request) {
	identity, ok := authIdentity(r)
	if !ok || identity.ActorType != "human" || (identity.Role != "operator" && identity.Role != "admin") {
		writeError(w, http.StatusForbidden, errors.New("review disposition requires an authenticated human operator or admin"))
		return
	}
	var input store.ApplyReviewDispositionInput
	if !decodeOptional(w, r, &input) {
		return
	}
	input.MissionName = r.PathValue("mission")
	input.RunID = r.PathValue("run")
	input.ReviewGate = r.PathValue("gate")
	input.Action = r.PathValue("action")
	input.ActorType = identity.ActorType
	input.ActorID = identity.ActorID
	input.ActorRole = identity.Role
	result, err := s.board.ApplyReviewDisposition(r.Context(), input)
	if errors.Is(err, store.ErrReviewDispositionConflict) {
		writeError(w, http.StatusConflict, err)
		return
	}
	writeResult(w, result, err)
}

func (s *Server) machines(w http.ResponseWriter, r *http.Request) {
	result, err := s.board.ListMachines(r.Context())
	writeResult(w, result, err)
}

func (s *Server) agents(w http.ResponseWriter, r *http.Request) {
	result, err := s.board.ListAgents(r.Context())
	writeResult(w, result, err)
}

func (s *Server) registerMachine(w http.ResponseWriter, r *http.Request) {
	var input store.RegisterMachineInput
	if !decode(w, r, &input) {
		return
	}
	result, err := s.board.RegisterMachine(r.Context(), input)
	writeResult(w, result, err)
}

func (s *Server) registerAgent(w http.ResponseWriter, r *http.Request) {
	var input store.RegisterAgentInput
	if !decode(w, r, &input) {
		return
	}
	// A participant may only (re)register the single Board agent id bound to
	// it, on its bound physical machine. Forgery of another id or host fails.
	if identity, ok := authIdentity(r); ok && isParticipant(identity) {
		if strings.TrimSpace(input.MachineID) == "" {
			writeError(w, http.StatusForbidden, errors.New("participant registration requires its bound machine"))
			return
		}
		if input.ID != "" && input.ID != identity.AgentID {
			writeError(w, http.StatusForbidden, errors.New("participant cannot register another agent identity"))
			return
		}
		if input.MachineID != "" && input.MachineID != identity.MachineID {
			writeError(w, http.StatusForbidden, errors.New("participant cannot register on another machine"))
			return
		}
		// kind/role/capabilities are the immutable operator-approved grant; the
		// caller can neither forge nor widen them.
		input.ID = identity.AgentID
		input.MachineID = identity.MachineID
		input.Kind = identity.AgentKind
		input.Role = identity.AgentRole
		input.Capabilities = append([]string(nil), identity.AgentCapabilities...)
	}
	// Only a legacy per-agent bearer-token identity self-registers/self-constrains
	// here -- a human session's ActorID is a username, not an
	// agent ID, and must never be forced into this field or compared
	// against it (an operator registering an agent on the fleet's behalf
	// must not be treated as "the agent").
	if identity, ok := authIdentity(r); ok && identity.ActorType == "agent" && input.ID != "" && input.ID != identity.ActorID {
		writeError(w, http.StatusForbidden, errors.New("agent token cannot register another agent identity"))
		return
	}
	if identity, ok := authIdentity(r); ok && identity.ActorType == "agent" && input.ID == "" {
		input.ID = identity.ActorID
	}
	result, err := s.board.RegisterAgent(r.Context(), input)
	writeResult(w, result, err)
}

func (s *Server) createTask(w http.ResponseWriter, r *http.Request) {
	var input store.CreateTaskInput
	if !decode(w, r, &input) {
		return
	}
	if identity, ok := authIdentity(r); ok {
		input.ActorType = identity.ActorType
		input.ActorID = identity.ActorID
	}
	result, err := s.board.CreateTask(r.Context(), input)
	writeResult(w, result, err)
}

func (s *Server) listTasks(w http.ResponseWriter, r *http.Request) {
	result, err := s.board.ListTasks(r.Context(), r.URL.Query().Get("status"), r.URL.Query().Get("project_id"))
	if identity, ok := authIdentity(r); ok && isClientReviewer(identity) {
		filtered := result[:0]
		for _, task := range result {
			if assignedClientReviewTask(domain.BoardReviewTask{Task: task}, identity) {
				filtered = append(filtered, task)
			}
		}
		result = filtered
	}
	if identity, ok := authIdentity(r); ok && isParticipant(identity) {
		result = filterParticipantTasks(result, identity)
	}
	writeResult(w, result, err)
}

func (s *Server) showTask(w http.ResponseWriter, r *http.Request) {
	result, err := s.board.GetTask(r.Context(), r.PathValue("id"))
	if identity, ok := authIdentity(r); ok && isClientReviewer(identity) {
		if err != nil || !assignedClientReviewTask(domain.BoardReviewTask{Task: result}, identity) {
			writeError(w, http.StatusNotFound, errors.New("review task not found"))
			return
		}
	}
	if identity, ok := authIdentity(r); ok && isParticipant(identity) {
		if err != nil || result.AssignedAgentID == "" || result.AssignedAgentID != identity.AgentID {
			writeError(w, http.StatusNotFound, errors.New("task not found"))
			return
		}
	}
	writeResult(w, result, err)
}

func (s *Server) reviewTask(w http.ResponseWriter, r *http.Request) {
	s.taskAction(w, r, s.board.SubmitTaskForReview)
}

func (s *Server) approveTask(w http.ResponseWriter, r *http.Request) {
	s.taskAction(w, r, func(ctx context.Context, input services.TaskActionInput) (domain.Task, error) {
		return s.board.ApproveTask(ctx, input, s.cfg.ReviewSingleApproval)
	})
}

func (s *Server) sendBackTask(w http.ResponseWriter, r *http.Request) {
	s.taskAction(w, r, s.board.SendTaskBack)
}

func (s *Server) completeTask(w http.ResponseWriter, r *http.Request) {
	s.taskAction(w, r, s.board.CompleteTask)
}

func (s *Server) blockTask(w http.ResponseWriter, r *http.Request) {
	s.taskAction(w, r, s.board.BlockTask)
}

func (s *Server) failTask(w http.ResponseWriter, r *http.Request) {
	s.taskAction(w, r, s.board.FailTask)
}

func (s *Server) cancelTask(w http.ResponseWriter, r *http.Request) {
	s.taskAction(w, r, s.board.CancelTask)
}

func (s *Server) taskAction(w http.ResponseWriter, r *http.Request, action func(context.Context, services.TaskActionInput) (domain.Task, error)) {
	var input services.TaskActionInput
	if !decodeOptional(w, r, &input) {
		return
	}
	input.TaskID = r.PathValue("id")
	if identity, ok := authIdentity(r); ok {
		input.ActorType = identity.ActorType
		input.ActorID = identity.ActorID
	}
	if identity, ok := authIdentity(r); ok && isParticipant(identity) {
		task, owned := s.participantOwnsTask(r.Context(), identity, input.TaskID)
		if !owned {
			writeError(w, http.StatusNotFound, errors.New("task not found"))
			return
		}
		// A participant may only act through a genuine claim: the task must be
		// in claimed state and an active, unexpired lease must match the exact
		// task, bound agent, and bound machine. The lease id is server-derived;
		// a caller-supplied lease_id is never trusted.
		if task.Status != domain.TaskStatusClaimed {
			writeError(w, http.StatusConflict, errors.New("participant must claim the task before acting on it"))
			return
		}
		lease, err := s.board.ActiveLease(r.Context(), input.TaskID, identity.AgentID, identity.MachineID)
		if err != nil {
			writeError(w, http.StatusConflict, errors.New("participant has no active unexpired lease for this task"))
			return
		}
		// A participant acts only as its bound agent and may never assign or
		// route review on its own behalf; any manager-set review target is
		// preserved by the service when the caller supplies none.
		input.ActorType, input.ActorID = participantActor(identity)
		input.LeaseID = lease.ID
		input.RequireActiveLease = true
		input.LeaseAgentID = identity.AgentID
		input.LeaseMachineID = identity.MachineID
		input.AssignType = ""
		input.AssignTo = ""
		input.ReviewTargetType = ""
		input.ReviewTargetID = ""
	}
	result, err := action(r.Context(), input)
	if errors.Is(err, store.ErrParticipantLeaseNotActive) {
		writeError(w, http.StatusConflict, errors.New("participant lease is no longer active"))
		return
	}
	writeResult(w, result, err)
}

func (s *Server) taskTree(w http.ResponseWriter, r *http.Request) {
	result, err := s.board.GetTaskTree(r.Context(), r.PathValue("id"))
	if identity, ok := authIdentity(r); ok && isParticipant(identity) {
		// Deny the whole tree unless every node, including nested descendants,
		// is assigned to the participant. No unowned node is ever returned.
		if err != nil || !treeFullyOwned(result, identity.AgentID) {
			writeError(w, http.StatusNotFound, errors.New("task not found"))
			return
		}
	}
	writeResult(w, result, err)
}

func (s *Server) taskEvents(w http.ResponseWriter, r *http.Request) {
	if identity, ok := authIdentity(r); ok && isParticipant(identity) {
		if _, owned := s.participantOwnsTask(r.Context(), identity, r.PathValue("id")); !owned {
			writeError(w, http.StatusNotFound, errors.New("task not found"))
			return
		}
	}
	result, err := s.board.ListEvents(r.Context(), r.PathValue("id"))
	writeResult(w, result, err)
}

func (s *Server) taskMessages(w http.ResponseWriter, r *http.Request) {
	if identity, ok := authIdentity(r); ok && isParticipant(identity) {
		if _, owned := s.participantOwnsTask(r.Context(), identity, r.PathValue("id")); !owned {
			writeError(w, http.StatusNotFound, errors.New("task not found"))
			return
		}
	}
	result, err := s.board.ListTaskMessages(r.Context(), r.PathValue("id"))
	writeResult(w, result, err)
}

func (s *Server) taskArtifacts(w http.ResponseWriter, r *http.Request) {
	if identity, ok := authIdentity(r); ok && isClientReviewer(identity) {
		task, err := s.board.GetTask(r.Context(), r.PathValue("id"))
		if err != nil || !assignedClientReviewTask(domain.BoardReviewTask{Task: task}, identity) {
			writeError(w, http.StatusNotFound, errors.New("review task not found"))
			return
		}
	}
	if identity, ok := authIdentity(r); ok && isParticipant(identity) {
		if _, owned := s.participantOwnsTask(r.Context(), identity, r.PathValue("id")); !owned {
			writeError(w, http.StatusNotFound, errors.New("task not found"))
			return
		}
	}
	result, err := s.board.ListArtifacts(r.Context(), r.PathValue("id"))
	writeResult(w, result, err)
}

func (s *Server) events(w http.ResponseWriter, r *http.Request) {
	result, err := s.board.ListEvents(r.Context(), r.URL.Query().Get("task"))
	writeResult(w, result, err)
}

func (s *Server) activeLeases(w http.ResponseWriter, r *http.Request) {
	result, err := s.board.ListActiveLeases(r.Context())
	writeResult(w, result, err)
}

func (s *Server) staleLeases(w http.ResponseWriter, r *http.Request) {
	result, err := s.board.ListStaleLeases(r.Context())
	writeResult(w, result, err)
}

func (s *Server) createMessage(w http.ResponseWriter, r *http.Request) {
	var input store.CreateMessageInput
	if !decode(w, r, &input) {
		return
	}
	if identity, ok := authIdentity(r); ok {
		input.FromActorType = identity.ActorType
		input.FromActorID = identity.ActorID
	}
	if identity, ok := authIdentity(r); ok && isParticipant(identity) {
		// The sender is always the bound agent, and every participant message
		// must be attached to a task the participant owns. Taskless sends are
		// refused so a participant cannot message arbitrary actors.
		input.FromActorType, input.FromActorID = participantActor(identity)
		if strings.TrimSpace(input.TaskID) == "" {
			writeError(w, http.StatusBadRequest, errors.New("participant messages require a task_id on owned work"))
			return
		}
		if _, owned := s.participantOwnsTask(r.Context(), identity, input.TaskID); !owned {
			writeError(w, http.StatusNotFound, errors.New("task not found"))
			return
		}
	}
	result, err := s.board.CreateMessage(r.Context(), input)
	writeResult(w, result, err)
}

func (s *Server) ackMessage(w http.ResponseWriter, r *http.Request) {
	var input struct {
		ActorID string `json:"actor_id"`
	}
	if !decode(w, r, &input) {
		return
	}
	actorType := ""
	if identity, ok := authIdentity(r); ok {
		actorType = identity.ActorType
		input.ActorID = identity.ActorID
	}
	if identity, ok := authIdentity(r); ok && isParticipant(identity) {
		message, err := s.board.GetMessage(r.Context(), r.PathValue("id"))
		if err != nil {
			writeError(w, http.StatusNotFound, errors.New("message not found"))
			return
		}
		if message.ToActorType != "agent" || message.ToActorID != identity.AgentID {
			writeError(w, http.StatusForbidden, errors.New("participant may only acknowledge messages addressed to it"))
			return
		}
		actorType = "agent"
		input.ActorID = identity.AgentID
	}
	result, err := s.board.AcknowledgeMessage(r.Context(), r.PathValue("id"), actorType, input.ActorID)
	writeResult(w, result, err)
}

func (s *Server) inbox(w http.ResponseWriter, r *http.Request) {
	toType := r.URL.Query().Get("to_type")
	toID := r.URL.Query().Get("to")
	if agentID := r.URL.Query().Get("agent"); agentID != "" {
		toType = "agent"
		toID = agentID
	}
	if toType == "" {
		toType = "agent"
	}
	if identity, ok := authIdentity(r); ok && isParticipant(identity) {
		// A participant may read only direct messages addressed to it; query
		// parameters cannot redirect the lookup and broadcasts not addressed to
		// it are excluded.
		result, err := s.board.ListDirectInbox(r.Context(), "agent", identity.AgentID)
		writeResult(w, result, err)
		return
	}
	result, err := s.board.ListInbox(r.Context(), toType, toID)
	writeResult(w, result, err)
}

func (s *Server) addArtifact(w http.ResponseWriter, r *http.Request) {
	var input store.AddArtifactInput
	if !decode(w, r, &input) {
		return
	}
	// AgentID is a foreign key to a specific agent -- only stamp it from a
	// per-agent bearer-token identity, never from a human session's username.
	if identity, ok := authIdentity(r); ok && identity.ActorType == "agent" {
		input.AgentID = identity.ActorID
	}
	if identity, ok := authIdentity(r); ok && isParticipant(identity) {
		input.AgentID = identity.AgentID
		if _, owned := s.participantOwnsTask(r.Context(), identity, input.TaskID); !owned {
			writeError(w, http.StatusNotFound, errors.New("task not found"))
			return
		}
	}
	result, err := s.board.AddArtifact(r.Context(), input)
	writeResult(w, result, err)
}

func (s *Server) uploadArtifact(w http.ResponseWriter, r *http.Request) {
	if strings.TrimSpace(s.cfg.ArtifactDir) == "" {
		writeError(w, http.StatusBadRequest, errors.New("artifact directory is not configured"))
		return
	}
	r.Body = http.MaxBytesReader(w, r.Body, maxArtifactUploadBytes)
	if err := r.ParseMultipartForm(1 << 20); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	file, header, err := r.FormFile("file")
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	defer file.Close()

	agentID := strings.TrimSpace(r.FormValue("agent_id"))
	if identity, ok := authIdentity(r); ok && identity.ActorType == "agent" {
		agentID = identity.ActorID
	}
	taskID := strings.TrimSpace(r.FormValue("task_id"))
	if identity, ok := authIdentity(r); ok && isParticipant(identity) {
		agentID = identity.AgentID
		if _, owned := s.participantOwnsTask(r.Context(), identity, taskID); !owned {
			writeError(w, http.StatusNotFound, errors.New("task not found"))
			return
		}
	}
	kind := strings.TrimSpace(r.FormValue("kind"))
	if kind == "" {
		kind = "report"
	}
	summary := strings.TrimSpace(r.FormValue("summary"))
	if summary == "" {
		summary = "uploaded artifact: " + filepath.Base(header.Filename)
	}

	artifactID := store.NewID()
	if err := os.MkdirAll(s.cfg.ArtifactDir, 0o755); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	destPath := filepath.Join(s.cfg.ArtifactDir, artifactID)
	dest, err := os.OpenFile(destPath, os.O_CREATE|os.O_EXCL|os.O_WRONLY, 0o644)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	hash := sha256.New()
	if _, err := io.Copy(dest, io.TeeReader(file, hash)); err != nil {
		_ = dest.Close()
		_ = os.Remove(destPath)
		writeError(w, http.StatusBadRequest, err)
		return
	}
	if err := dest.Close(); err != nil {
		_ = os.Remove(destPath)
		writeError(w, http.StatusBadRequest, err)
		return
	}

	artifact, err := s.board.AddArtifact(r.Context(), store.AddArtifactInput{
		ID:        artifactID,
		TaskID:    strings.TrimSpace(r.FormValue("task_id")),
		AgentID:   agentID,
		Kind:      kind,
		PathOrURL: "/artifacts/" + artifactID + "/download",
		Summary:   summary,
		Hash:      fmt.Sprintf("sha256:%x", hash.Sum(nil)),
	})
	if err != nil {
		_ = os.Remove(destPath)
		writeError(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, http.StatusOK, artifact)
}

func (s *Server) downloadArtifact(w http.ResponseWriter, r *http.Request) {
	if strings.TrimSpace(s.cfg.ArtifactDir) == "" {
		writeError(w, http.StatusBadRequest, errors.New("artifact directory is not configured"))
		return
	}
	id := cleanArtifactID(r.PathValue("id"))
	if id == "" {
		writeError(w, http.StatusBadRequest, errors.New("artifact id is required"))
		return
	}
	artifact, err := s.board.GetArtifact(r.Context(), id)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	if identity, ok := authIdentity(r); ok && isParticipant(identity) {
		if _, owned := s.participantOwnsTask(r.Context(), identity, artifact.TaskID); !owned {
			writeError(w, http.StatusNotFound, errors.New("artifact not found"))
			return
		}
	}
	path := filepath.Join(s.cfg.ArtifactDir, artifact.ID)
	file, err := os.Open(path)
	if err != nil {
		writeError(w, http.StatusNotFound, err)
		return
	}
	defer file.Close()
	stat, err := file.Stat()
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	w.Header().Set("Content-Type", "text/markdown; charset=utf-8")
	w.Header().Set("Content-Disposition", fmt.Sprintf(`attachment; filename="agent-board-artifact-%s.md"`, artifact.ID))
	http.ServeContent(w, r, artifact.ID, stat.ModTime(), file)
}

func (s *Server) poll(w http.ResponseWriter, r *http.Request) {
	s.pollRequests.Add(1)
	var input store.PollInput
	if !decode(w, r, &input) {
		return
	}
	if identity, ok := authIdentity(r); ok && identity.ActorType == "agent" {
		if input.AgentID != "" && input.AgentID != identity.ActorID {
			writeError(w, http.StatusForbidden, errors.New("agent token cannot poll as another agent"))
			return
		}
		input.AgentID = identity.ActorID
	}
	if identity, ok := authIdentity(r); ok && isParticipant(identity) {
		if strings.TrimSpace(input.MachineID) == "" {
			writeError(w, http.StatusForbidden, errors.New("participant poll requires its bound machine"))
			return
		}
		if input.AgentID != "" && input.AgentID != identity.AgentID {
			writeError(w, http.StatusForbidden, errors.New("participant cannot poll as another agent"))
			return
		}
		if input.MachineID != "" && input.MachineID != identity.MachineID {
			writeError(w, http.StatusForbidden, errors.New("participant cannot poll as another machine"))
			return
		}
		// Identity, host, and capability matching are all derived from the
		// immutable verified grant, never from the request body.
		input.AgentID = identity.AgentID
		input.MachineID = identity.MachineID
		input.Capabilities = append([]string(nil), identity.AgentCapabilities...)
		// A participant may only claim work already assigned to it.
		input.AssignedOnly = true
	}
	result, err := s.board.PollAndClaim(r.Context(), input)
	if errors.Is(err, store.ErrNoTask) {
		writeJSON(w, http.StatusOK, map[string]any{"task": nil})
		return
	}
	writeResult(w, result, err)
}

func (s *Server) renewLease(w http.ResponseWriter, r *http.Request) {
	var input struct {
		LeaseSeconds int `json:"lease_seconds"`
	}
	if !decode(w, r, &input) {
		return
	}
	if identity, ok := authIdentity(r); ok && isParticipant(identity) {
		lease, err := s.board.GetLease(r.Context(), r.PathValue("id"))
		if err != nil || lease.AgentID != identity.AgentID || lease.MachineID != identity.MachineID || lease.Status != domain.LeaseStatusActive || !lease.ExpiresAt.After(time.Now().UTC()) {
			writeError(w, http.StatusNotFound, errors.New("lease not found"))
			return
		}
		result, err := s.board.RenewParticipantLease(r.Context(), lease.ID, lease.TaskID, identity.AgentID, identity.MachineID, input.LeaseSeconds)
		if errors.Is(err, store.ErrParticipantLeaseNotActive) {
			writeError(w, http.StatusNotFound, errors.New("lease not found"))
			return
		}
		writeResult(w, result, err)
		return
	}
	result, err := s.board.RenewLease(r.Context(), r.PathValue("id"), input.LeaseSeconds)
	writeResult(w, result, err)
}

func (s *Server) expireStale(w http.ResponseWriter, r *http.Request) {
	count, err := s.board.ExpireStaleLeases(r.Context())
	writeResult(w, map[string]int64{"expired": count}, err)
}

func decode(w http.ResponseWriter, r *http.Request, target any) bool {
	defer r.Body.Close()
	if err := json.NewDecoder(r.Body).Decode(target); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return false
	}
	return true
}

func decodeOptional(w http.ResponseWriter, r *http.Request, target any) bool {
	defer r.Body.Close()
	if r.Body == nil || r.ContentLength == 0 {
		return true
	}
	if err := json.NewDecoder(r.Body).Decode(target); err != nil && !errors.Is(err, io.EOF) {
		writeError(w, http.StatusBadRequest, err)
		return false
	}
	return true
}

func writeResult(w http.ResponseWriter, value any, err error) {
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	writeJSON(w, http.StatusOK, value)
}

func writeError(w http.ResponseWriter, status int, err error) {
	writeJSON(w, status, map[string]string{"error": err.Error()})
}

func writeJSON(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func authIdentity(r *http.Request) (domain.AuthIdentity, bool) {
	identity, ok := r.Context().Value(authIdentityKey{}).(domain.AuthIdentity)
	return identity, ok
}

func cleanArtifactID(value string) string {
	for _, r := range value {
		if !(r >= 'a' && r <= 'z' || r >= 'A' && r <= 'Z' || r >= '0' && r <= '9' || r == '-' || r == '_') {
			return ""
		}
	}
	return value
}

func prometheusMetrics(snapshot domain.BoardSnapshot, pollRequests uint64) []byte {
	var out bytes.Buffer
	writeMetric(&out, "agent_board_tasks_ready_total", nil, snapshot.Status.ReadyTasks)
	writeMetric(&out, "agent_board_tasks_running_total", nil, snapshot.Status.ClaimedTasks)
	writeMetric(&out, "agent_board_tasks_waiting_total", nil, snapshot.Status.WaitingTasks)
	writeMetric(&out, "agent_board_tasks_blocked_total", nil, snapshot.Status.BlockedTasks)
	writeMetric(&out, "agent_board_tasks_review_total", nil, snapshot.Status.ReviewTasks)
	writeMetric(&out, "agent_board_tasks_failed_total", nil, snapshot.Status.FailedTasks)
	writeMetric(&out, "agent_board_tasks_done_total", nil, snapshot.Status.DoneTasks)
	for _, item := range snapshot.ReadyByPriority {
		writeMetric(&out, "agent_board_tasks_ready_by_priority_total", map[string]string{"priority": item.Label}, item.Count)
	}
	writeMetric(&out, "agent_board_agents_online_total", nil, snapshot.Status.OnlineAgents)
	writeMetric(&out, "agent_board_machines_online_total", nil, snapshot.Status.OnlineMachines)
	writeMetric(&out, "agent_board_leases_active_total", nil, len(snapshot.ActiveLeases))
	writeMetric(&out, "agent_board_leases_stale_total", nil, len(snapshot.StaleLeases))
	writeMetric(&out, "agent_board_poll_requests_total", nil, pollRequests)
	return out.Bytes()
}

func writeMetric(out *bytes.Buffer, name string, labels map[string]string, value any) {
	if len(labels) == 0 {
		fmt.Fprintf(out, "%s %v\n", name, value)
		return
	}
	parts := make([]string, 0, len(labels))
	for key, value := range labels {
		parts = append(parts, fmt.Sprintf(`%s="%s"`, key, escapeMetricLabel(value)))
	}
	fmt.Fprintf(out, "%s{%s} %v\n", name, strings.Join(parts, ","), value)
}

func escapeMetricLabel(value string) string {
	value = strings.ReplaceAll(value, `\`, `\\`)
	value = strings.ReplaceAll(value, "\n", `\n`)
	return strings.ReplaceAll(value, `"`, `\"`)
}
