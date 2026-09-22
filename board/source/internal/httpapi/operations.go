package httpapi

import (
	"errors"
	"fmt"
	"io"
	"net/http"
	"strconv"
	"strings"
	"time"

	"agent-board/internal/auth"
	"agent-board/internal/domain"
	"agent-board/internal/operations"
	"agent-board/internal/services"
	"agent-board/internal/store"
)

var errScopedProducerForbidden = errors.New("observation producer identity is authorized only for its ingestion write seam")

// isScopedProducer reports whether the identity is a verified, granted
// observation producer. It is deliberately strict: the identity must be a
// machine, must not be a participant, and must carry a server-derived owner
// grant. A human session, legacy machine, participant, or anonymous request can
// never satisfy it.
func isScopedProducer(identity domain.AuthIdentity) bool {
	return identity.ActorType == "machine" && !isParticipant(identity) && identity.IngestOwner != ""
}

// scopedProducerRouteAllowed is a default-deny allowance list: a scoped producer
// may only reach its own ingestion write seam and never the fleet read model or
// any generic write.
func scopedProducerRouteAllowed(r *http.Request) bool {
	return r.Method == http.MethodPost && r.URL.Path == "/api/operations/observations"
}

func (s *Server) ingestObservation(w http.ResponseWriter, r *http.Request) {
	identity, ok := authIdentity(r)
	if !ok {
		writeError(w, http.StatusUnauthorized, auth.ErrNoCredentials)
		return
	}
	if !isScopedProducer(identity) {
		writeError(w, http.StatusForbidden, errScopedProducerForbidden)
		return
	}
	body, ok := readObservationBody(w, r)
	if !ok {
		return
	}
	now := time.Now().UTC()
	envelope, err := operations.DecodeObservationEnvelope(body, now)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	owner := envelope.Observation.Owner
	if operations.IsBoardOwner(string(owner)) || !operations.IngestOwnerGranted(identity.IngestOwner, owner) {
		writeError(w, http.StatusForbidden, fmt.Errorf("authenticated producer is not granted owner %q", owner))
		return
	}
	record, idempotent, err := s.board.IngestObservation(r.Context(), services.IngestObservationInput{
		Envelope:          envelope,
		ProducerPrincipal: identity.ActorID,
		ReceivedAt:        now,
	})
	if errors.Is(err, store.ErrObservationConflict) {
		writeError(w, http.StatusConflict, err)
		return
	}
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	status := "created"
	code := http.StatusCreated
	if idempotent {
		status = "idempotent"
		code = http.StatusOK
	}
	writeJSON(w, code, map[string]any{
		"status":             status,
		"owner":              record.Owner,
		"employee_id":        record.EmployeeID,
		"observation_id":     record.ObservationID,
		"producer_principal": record.ProducerPrincipal,
		"content_sha256":     record.ContentSHA256,
		"observed_at":        record.ObservedAt,
		"received_at":        record.ReceivedAt,
	})
}

func readObservationBody(w http.ResponseWriter, r *http.Request) ([]byte, bool) {
	r.Body = http.MaxBytesReader(w, r.Body, operations.MaxEnvelopeBytes)
	body, err := io.ReadAll(r.Body)
	if err != nil {
		var maxErr *http.MaxBytesError
		if errors.As(err, &maxErr) {
			writeError(w, http.StatusRequestEntityTooLarge, fmt.Errorf("request body exceeds %d bytes", operations.MaxEnvelopeBytes))
			return nil, false
		}
		writeError(w, http.StatusBadRequest, errors.New("unable to read request body"))
		return nil, false
	}
	return body, true
}

func (s *Server) operationsEmployees(w http.ResponseWriter, r *http.Request) {
	limit, err := operationsHistoryLimit(r)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	fleet, err := s.board.OperationsFleet(r.Context(), limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusOK, fleet)
}

func (s *Server) operationsEmployee(w http.ResponseWriter, r *http.Request) {
	employeeID := strings.TrimSpace(r.PathValue("employee_id"))
	if err := operations.ValidateEmployeeID(employeeID); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	limit, err := operationsHistoryLimit(r)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	view, err := s.board.OperationsEmployee(r.Context(), employeeID, limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusOK, view)
}

// managerialEmployees is the authenticated manager-only managerial fleet read.
// Scoped producers are denied by the default-deny seam, and the route is not in
// the public-read allow list, so it never leaks without a session identity.
func (s *Server) managerialEmployees(w http.ResponseWriter, r *http.Request) {
	limit, err := operationsHistoryLimit(r)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	fleet, err := s.board.ManagerialFleet(r.Context(), limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusOK, fleet)
}

func (s *Server) managerialEmployee(w http.ResponseWriter, r *http.Request) {
	employeeID := strings.TrimSpace(r.PathValue("employee_id"))
	if err := operations.ValidateEmployeeID(employeeID); err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	limit, err := operationsHistoryLimit(r)
	if err != nil {
		writeError(w, http.StatusBadRequest, err)
		return
	}
	view, err := s.board.ManagerialEmployee(r.Context(), employeeID, limit)
	if err != nil {
		writeError(w, http.StatusInternalServerError, err)
		return
	}
	writeJSON(w, http.StatusOK, view)
}

func operationsHistoryLimit(r *http.Request) (int, error) {
	raw := strings.TrimSpace(r.URL.Query().Get("history"))
	if raw == "" {
		return operations.DefaultHistoryLimit, nil
	}
	limit, err := strconv.Atoi(raw)
	if err != nil || limit < 1 || limit > operations.MaxHistoryLimit {
		return 0, fmt.Errorf("history must be an integer between 1 and %d", operations.MaxHistoryLimit)
	}
	return limit, nil
}
