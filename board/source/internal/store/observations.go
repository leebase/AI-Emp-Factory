package store

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"

	"agent-board/internal/operations"
)

// ErrObservationConflict reports that the owner+employee+observation key
// already exists with different content or a different authentic producer. The
// existing append-only record is never overwritten.
var ErrObservationConflict = errors.New("observation already exists with different content or producer")

// observationTimeLayout is a fixed nine-fraction UTC timestamp. Fixed width
// keeps lexical SQLite ordering identical to chronological ordering: an exact
// second ("...00.000000000Z") no longer sorts after a half second
// ("...00.500000000Z"). The stored value remains valid RFC3339Nano, and every
// value is normalized to UTC so an equivalent timezone offset cannot distort
// order.
const observationTimeLayout = "2006-01-02T15:04:05.000000000Z07:00"

func formatObservationTime(t time.Time) string {
	return t.UTC().Format(observationTimeLayout)
}

// InsertObservationInput is the server-derived persistence input. ContentSHA256
// and EnvelopeJSON are produced from the deterministic, validated envelope, and
// ProducerPrincipal is the authenticated identity, never a payload claim.
type InsertObservationInput struct {
	Owner             operations.SourceOwner
	EmployeeID        string
	ObservationID     string
	ProducerPrincipal string
	SchemaVersion     string
	CollectionResult  string
	ObservedAt        time.Time
	ReceivedAt        time.Time
	ContentSHA256     string
	EnvelopeJSON      string
}

// InsertObservation appends one immutable observation. An exact retry by the
// same producer with the same content is idempotent (existing=true). A changed
// content or producer under the same key returns ErrObservationConflict.
func (s *Store) InsertObservation(ctx context.Context, in InsertObservationInput) (operations.StoredObservation, bool, error) {
	owner := strings.TrimSpace(string(in.Owner))
	employeeID := strings.TrimSpace(in.EmployeeID)
	observationID := strings.TrimSpace(in.ObservationID)
	producer := strings.TrimSpace(in.ProducerPrincipal)
	if owner == "" || employeeID == "" || observationID == "" || producer == "" {
		return operations.StoredObservation{}, false, errors.New("owner, employee_id, observation_id, and producer_principal are required")
	}
	if !operations.IsValidSourceOwner(owner) {
		return operations.StoredObservation{}, false, fmt.Errorf("unknown source owner %q", owner)
	}
	if in.EnvelopeJSON == "" || in.ContentSHA256 == "" {
		return operations.StoredObservation{}, false, errors.New("envelope_json and content_sha256 are required")
	}
	observed := formatObservationTime(in.ObservedAt)
	received := formatObservationTime(in.ReceivedAt)

	result, err := s.db.ExecContext(ctx, `
INSERT INTO operations_observations(owner, employee_id, observation_id, producer_principal, schema_version, collection_result, observed_at, received_at, content_sha256, envelope_json)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(owner, employee_id, observation_id) DO NOTHING`,
		owner, employeeID, observationID, producer, in.SchemaVersion, in.CollectionResult, observed, received, in.ContentSHA256, in.EnvelopeJSON)
	if err != nil {
		return operations.StoredObservation{}, false, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return operations.StoredObservation{}, false, err
	}
	existing, err := s.getObservation(ctx, owner, employeeID, observationID)
	if err != nil {
		return operations.StoredObservation{}, false, err
	}
	if affected == 0 {
		if existing.ProducerPrincipal != producer || existing.ContentSHA256 != in.ContentSHA256 {
			return operations.StoredObservation{}, false, ErrObservationConflict
		}
		return existing, true, nil
	}
	return existing, false, nil
}

func (s *Store) getObservation(ctx context.Context, owner, employeeID, observationID string) (operations.StoredObservation, error) {
	row := s.db.QueryRowContext(ctx, `
SELECT owner, employee_id, observation_id, producer_principal, schema_version, observed_at, received_at, content_sha256, envelope_json
FROM operations_observations
WHERE owner = ? AND employee_id = ? AND observation_id = ?`, owner, employeeID, observationID)
	return scanObservation(row)
}

// ObservationWindow is the bounded result of a read. Records are bounded in SQL
// per (employee, owner) to the newest historyLimit distinct instants plus the
// most recent known success, and, for a fleet read, to employeeLimit employees.
type ObservationWindow struct {
	Records            []operations.StoredObservation
	TotalEmployees     int
	EmployeesTruncated bool
}

// ListObservations returns a bounded, deterministic slice of stored
// observations. An empty employeeID requests the fleet view, which is capped at
// employeeLimit employees with explicit truncation metadata. Every owner's
// window retains the newest perOwnerLimit distinct observation instants (all
// same-time ties included) plus the most recent successful record, so a long
// outage cannot hide the last known success.
func (s *Store) ListObservations(ctx context.Context, employeeID string, perOwnerLimit, employeeLimit int) (ObservationWindow, error) {
	if perOwnerLimit <= 0 {
		perOwnerLimit = operations.DefaultHistoryLimit
	}
	if perOwnerLimit > operations.MaxHistoryLimit {
		perOwnerLimit = operations.MaxHistoryLimit
	}
	if employeeLimit <= 0 {
		employeeLimit = operations.MaxFleetEmployees
	}
	scoped := strings.TrimSpace(employeeID)

	window := ObservationWindow{}
	var err error
	if window.TotalEmployees, err = s.countObservationEmployees(ctx, scoped); err != nil {
		return ObservationWindow{}, err
	}
	window.EmployeesTruncated = window.TotalEmployees > employeeLimit

	query := `
WITH scoped AS (
  SELECT owner, employee_id, observation_id, producer_principal, schema_version,
         observed_at, received_at, content_sha256, envelope_json, collection_result
  FROM operations_observations
  WHERE (? = '' OR employee_id = ?)
),
bounded AS (
  SELECT * FROM scoped
  WHERE employee_id IN (
    SELECT DISTINCT employee_id FROM scoped ORDER BY employee_id ASC LIMIT ?
  )
),
ranked AS (
  SELECT *,
         ROW_NUMBER() OVER (PARTITION BY employee_id, owner ORDER BY observed_at DESC, observation_id ASC) AS recency_rank,
         COUNT(*) OVER (PARTITION BY employee_id, owner) AS owner_rows
  FROM bounded
),
marked AS (
  SELECT *,
         MAX(CASE WHEN recency_rank = ? THEN observed_at END) OVER (PARTITION BY employee_id, owner) AS cap_instant,
         MAX(CASE WHEN recency_rank = ? + 1 THEN observed_at END) OVER (PARTITION BY employee_id, owner) AS next_instant
  FROM ranked
),
successes AS (
  SELECT *,
         ROW_NUMBER() OVER (PARTITION BY employee_id, owner ORDER BY observed_at DESC, observation_id ASC) AS success_rank
  FROM marked
  WHERE collection_result = 'success'
)
SELECT owner, employee_id, observation_id, producer_principal, schema_version,
       observed_at, received_at, content_sha256, envelope_json,
       (owner_rows > ?) AS window_truncated,
       CASE
         WHEN cap_instant IS NOT NULL AND next_instant IS NOT NULL AND cap_instant = next_instant THEN 1
         ELSE 0
       END AS tie_truncated
FROM marked WHERE recency_rank <= ?
UNION
SELECT owner, employee_id, observation_id, producer_principal, schema_version,
       observed_at, received_at, content_sha256, envelope_json,
       (owner_rows > ?) AS window_truncated,
       CASE
         WHEN cap_instant IS NOT NULL AND next_instant IS NOT NULL AND cap_instant = next_instant THEN 1
         ELSE 0
       END AS tie_truncated
FROM successes WHERE success_rank = 1
ORDER BY employee_id ASC, owner ASC, observed_at DESC, observation_id ASC`
	args := []any{
		scoped, scoped, employeeLimit,
		perOwnerLimit, perOwnerLimit, perOwnerLimit, perOwnerLimit, perOwnerLimit,
	}

	rows, err := s.db.QueryContext(ctx, query, args...)
	if err != nil {
		return ObservationWindow{}, err
	}
	defer rows.Close()

	for rows.Next() {
		record, err := scanObservationWindow(rows)
		if err != nil {
			return ObservationWindow{}, err
		}
		window.Records = append(window.Records, record)
	}
	if err := rows.Err(); err != nil {
		return ObservationWindow{}, err
	}
	return window, nil
}

func (s *Store) countObservationEmployees(ctx context.Context, employeeID string) (int, error) {
	var total int
	err := s.db.QueryRowContext(ctx, `
SELECT COUNT(DISTINCT employee_id) FROM operations_observations WHERE (? = '' OR employee_id = ?)`,
		employeeID, employeeID).Scan(&total)
	if err != nil {
		return 0, err
	}
	return total, nil
}

type observationScanner interface {
	Scan(dest ...any) error
}

func scanObservationWindow(scanner observationScanner) (operations.StoredObservation, error) {
	var (
		record                          operations.StoredObservation
		owner, schemaVersion            string
		observed, received, envelopeRAW string
		windowTruncated, tieTruncated   bool
	)
	if err := scanner.Scan(
		&owner, &record.EmployeeID, &record.ObservationID, &record.ProducerPrincipal,
		&schemaVersion, &observed, &received, &record.ContentSHA256, &envelopeRAW, &windowTruncated, &tieTruncated,
	); err != nil {
		return operations.StoredObservation{}, err
	}
	record.Owner = operations.SourceOwner(owner)
	record.SchemaVersion = schemaVersion
	record.WindowTruncated = windowTruncated
	record.TieTruncated = tieTruncated
	if err := decodeObservationTimes(&record, observed, received); err != nil {
		return operations.StoredObservation{}, err
	}
	if err := json.Unmarshal([]byte(envelopeRAW), &record.Envelope); err != nil {
		return operations.StoredObservation{}, err
	}
	return record, nil
}

func scanObservation(scanner observationScanner) (operations.StoredObservation, error) {
	var record operations.StoredObservation
	var owner, schemaVersion, observed, received, envelopeJSON string
	if err := scanner.Scan(
		&owner, &record.EmployeeID, &record.ObservationID, &record.ProducerPrincipal,
		&schemaVersion, &observed, &received, &record.ContentSHA256, &envelopeJSON,
	); err != nil {
		return operations.StoredObservation{}, err
	}
	record.Owner = operations.SourceOwner(owner)
	record.SchemaVersion = schemaVersion
	if err := decodeObservationTimes(&record, observed, received); err != nil {
		return operations.StoredObservation{}, err
	}
	if err := json.Unmarshal([]byte(envelopeJSON), &record.Envelope); err != nil {
		return operations.StoredObservation{}, err
	}
	return record, nil
}

func decodeObservationTimes(record *operations.StoredObservation, observed, received string) error {
	var err error
	if record.ObservedAt, err = time.Parse(time.RFC3339Nano, observed); err != nil {
		return err
	}
	if record.ReceivedAt, err = time.Parse(time.RFC3339Nano, received); err != nil {
		return err
	}
	return nil
}
