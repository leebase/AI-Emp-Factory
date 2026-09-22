package services

import (
	"context"
	"time"

	"agent-board/internal/operations"
	"agent-board/internal/store"
)

// IngestObservationInput carries a validated envelope plus the server-derived
// authentic producer and receipt time. The digest and canonical JSON are
// recomputed here, never accepted from the caller.
type IngestObservationInput struct {
	Envelope          operations.ObservationEnvelope
	ProducerPrincipal string
	ReceivedAt        time.Time
}

// IngestObservation persists one immutable source observation through the
// append-only store. The returned bool reports whether the call was an
// idempotent retry of an identical record. The public service seam re-validates
// the typed envelope so an embedder that bypasses the HTTP strict decoder
// cannot persist fabricated or malformed content; the digest and receipt
// metadata are always server-derived.
func (b *Board) IngestObservation(ctx context.Context, in IngestObservationInput) (operations.StoredObservation, bool, error) {
	received := in.ReceivedAt
	if received.IsZero() {
		received = time.Now().UTC()
	}
	if err := in.Envelope.Validate(received); err != nil {
		return operations.StoredObservation{}, false, err
	}
	canonical, err := in.Envelope.CanonicalJSON()
	if err != nil {
		return operations.StoredObservation{}, false, err
	}
	digest, err := in.Envelope.SHA256()
	if err != nil {
		return operations.StoredObservation{}, false, err
	}
	return b.store.InsertObservation(ctx, store.InsertObservationInput{
		Owner:             in.Envelope.Observation.Owner,
		EmployeeID:        in.Envelope.Observation.EmployeeID,
		ObservationID:     in.Envelope.ObservationID,
		ProducerPrincipal: in.ProducerPrincipal,
		SchemaVersion:     in.Envelope.SchemaVersion,
		CollectionResult:  string(in.Envelope.Observation.CollectionResult),
		ObservedAt:        in.Envelope.Observation.ObservedAt,
		ReceivedAt:        received,
		ContentSHA256:     digest,
		EnvelopeJSON:      string(canonical),
	})
}

// OperationsFleet returns the bounded, versioned fleet read model. The store
// query applies the history and employee bounds in SQL, so a warm read never
// materializes the whole table.
func (b *Board) OperationsFleet(ctx context.Context, historyLimit int) (operations.OperationsFleet, error) {
	window, err := b.store.ListObservations(ctx, "", historyLimit, operations.MaxFleetEmployees)
	if err != nil {
		return operations.OperationsFleet{}, err
	}
	fleet := operations.BuildFleet(window.Records, time.Now().UTC(), historyLimit)
	fleet.TotalEmployees = window.TotalEmployees
	fleet.EmployeesTruncated = window.EmployeesTruncated
	return fleet, nil
}

// OperationsEmployee returns one employee's explicit owner-by-owner view. An
// employee with no stored observations still returns every owner as missing.
func (b *Board) OperationsEmployee(ctx context.Context, employeeID string, historyLimit int) (operations.FleetEmployee, error) {
	window, err := b.store.ListObservations(ctx, employeeID, historyLimit, operations.MaxFleetEmployees)
	if err != nil {
		return operations.FleetEmployee{}, err
	}
	return operations.BuildEmployeeFleet(employeeID, window.Records, time.Now().UTC(), historyLimit), nil
}

// ManagerialFleet returns the versioned deterministic managerial fleet,
// derived from the same bounded authenticated read window as OperationsFleet.
// The derivation is a pure projection of the Sprint 2 read model: the raw
// observations and provenance stay embedded in every employee response.
func (b *Board) ManagerialFleet(ctx context.Context, historyLimit int) (operations.ManagerialFleet, error) {
	window, err := b.store.ListObservations(ctx, "", historyLimit, operations.MaxFleetEmployees)
	if err != nil {
		return operations.ManagerialFleet{}, err
	}
	fleet := operations.BuildFleet(window.Records, time.Now().UTC(), historyLimit)
	fleet.TotalEmployees = window.TotalEmployees
	fleet.EmployeesTruncated = window.EmployeesTruncated
	return operations.DeriveFleet(fleet, fleet.GeneratedAt), nil
}

// ManagerialEmployee derives the deterministic managerial projection for one
// employee from its bounded owner-by-owner view.
func (b *Board) ManagerialEmployee(ctx context.Context, employeeID string, historyLimit int) (operations.ManagerialEmployee, error) {
	window, err := b.store.ListObservations(ctx, employeeID, historyLimit, operations.MaxFleetEmployees)
	if err != nil {
		return operations.ManagerialEmployee{}, err
	}
	now := time.Now().UTC()
	emp := operations.BuildEmployeeFleet(employeeID, window.Records, now, historyLimit)
	derived := operations.DeriveEmployee(emp, now)
	// Sprint 6B2: the bounded Board-record window is attached here, on the
	// detail path only, after the projection is already complete. It is an
	// additive field on this existing authenticated response and no input to
	// any rule above it; the fleet path does not attach it, so the fleet
	// payload is unchanged. A failed Board read fails this detail read rather
	// than returning a successful response with an empty window.
	history, err := b.EmployeeBoardHistory(ctx, employeeID, operations.DefaultBoardHistoryLimit)
	if err != nil {
		return operations.ManagerialEmployee{}, err
	}
	derived.BoardHistory = &history
	return derived, nil
}
