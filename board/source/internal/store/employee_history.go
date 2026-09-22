package store

import (
	"context"
	"database/sql"
	"encoding/json"
	"errors"
	"strings"
	"time"

	"agent-board/internal/operations"
)

// Sprint 6B2 bounded native employee-associated Board history reads.
//
// Both queries filter on the native employee_id column that migration 010 added
// to auto_orch_cycle_reports and board_review_tasks. There is no join on
// mission name, agent id or assigned agent, and no inferred association of any
// kind. A generic task has no employee id at all and is reachable here only
// through an authentic board_review_tasks.task_id.
//
// The bound is applied in SQL with LIMIT n+1 before any row is materialized, so
// a large table is never scanned into memory to be trimmed afterwards, and the
// extra row is what makes truncation an observed fact rather than a guess.
//
// Ordering note: Board writes these clocks with time.RFC3339Nano, which is
// variable width, so raw lexical ordering is not chronological ordering
// (".500000000Z" sorts before a bare "Z"). The queries therefore sort on a
// fixed-width normalized key: the second-precision prefix followed by the
// fractional digits right-padded to nine. Padding to nine rather than
// millisecond-truncating is deliberate — truncation would make two genuinely
// different sub-millisecond update instants tie, and the ordering tie-break
// could then drop the newer row at the SQL bound.
//
// A value that is not a UTC RFC3339 instant normalizes to NULL, which sorts
// last under DESC, and its row is marked ClockUnparsed rather than being given
// an invented time. Equal instants are broken by the native identifier so one
// window always renders in one order.
func boardHistoryOrderKey(column string) string {
	return `CASE WHEN strftime('%Y-%m-%dT%H:%M:%f', ` + column + `) IS NOT NULL
             AND substr(` + column + `, length(` + column + `), 1) = 'Z'
        THEN substr(` + column + `, 1, 19) || substr(
               (CASE WHEN substr(` + column + `, 20, 1) = '.'
                     THEN substr(` + column + `, 21, length(` + column + `) - 21)
                     ELSE '' END) || '000000000', 1, 9)
        ELSE NULL END`
}

// ListEmployeeBoardHistory returns the bounded, deterministic Board-record
// window for exactly one employee. A blank employee id is refused rather than
// treated as a wildcard: unassociated rows belong to no employee and must never
// be folded into one. A query failure is returned as an error; it is never
// reported as a successful empty window.
func (s *Store) ListEmployeeBoardHistory(ctx context.Context, employeeID string, limit int) (operations.EmployeeBoardHistory, error) {
	employeeID = strings.TrimSpace(employeeID)
	if employeeID == "" {
		return operations.EmployeeBoardHistory{}, errors.New("employee_id is required for a Board history read")
	}
	limit = operations.BoardHistoryLimit(limit)

	history := operations.EmployeeBoardHistory{
		SchemaVersion: operations.BoardHistorySchemaVersion,
		EmployeeID:    employeeID,
		Limit:         limit,
		CycleReports:  []operations.BoardCycleReport{},
		Reviews:       []operations.BoardReviewRecord{},
		CapturedAt:    time.Now().UTC(),
	}

	reports, reportsTruncated, err := s.listEmployeeCycleReports(ctx, employeeID, limit)
	if err != nil {
		return operations.EmployeeBoardHistory{}, err
	}
	history.CycleReports = reports
	history.CycleReportsTruncated = reportsTruncated

	reviews, reviewsTruncated, err := s.listEmployeeReviewTasks(ctx, employeeID, limit)
	if err != nil {
		return operations.EmployeeBoardHistory{}, err
	}
	history.Reviews = reviews
	history.ReviewsTruncated = reviewsTruncated
	return history, nil
}

func (s *Store) listEmployeeCycleReports(ctx context.Context, employeeID string, limit int) ([]operations.BoardCycleReport, bool, error) {
	rows, err := s.db.QueryContext(ctx, `
SELECT mission_name, cycle_id, actor_id, created_at, updated_at
FROM auto_orch_cycle_reports
WHERE employee_id = ?
ORDER BY `+boardHistoryOrderKey("updated_at")+` DESC, cycle_id ASC
LIMIT ?`, employeeID, limit+1)
	if err != nil {
		return nil, false, err
	}
	defer rows.Close()

	out := []operations.BoardCycleReport{}
	truncated := false
	for rows.Next() {
		var (
			record              operations.BoardCycleReport
			created, updated    string
			createdOK, updated2 bool
		)
		if err := rows.Scan(&record.MissionName, &record.CycleID, &record.ActorID, &created, &updated); err != nil {
			return nil, false, err
		}
		if len(out) == limit {
			truncated = true
			break
		}
		record.CreatedAt, createdOK = parseBoardClock(created)
		record.UpdatedAt, updated2 = parseBoardClock(updated)
		record.ClockUnparsed = !createdOK || !updated2
		out = append(out, record)
	}
	if err := rows.Err(); err != nil {
		return nil, false, err
	}
	return out, truncated, nil
}

func (s *Store) listEmployeeReviewTasks(ctx context.Context, employeeID string, limit int) ([]operations.BoardReviewRecord, bool, error) {
	// The disposition arrives on this same bounded query through the authentic
	// board_review_tasks.task_id foreign key. Nothing is fetched per row after
	// the fact, so no helper is called while these rows are still open.
	// The disposition and the task facts both arrive on this same bounded query
	// through the authentic board_review_tasks.task_id foreign key. Nothing is
	// fetched per row after the fact, so no helper is called while these rows
	// are still open and no second unbounded scan is issued.
	rows, err := s.db.QueryContext(ctx, `
SELECT br.task_id, br.mission_name, br.run_id, br.review_gate, br.source_pdd_version,
       br.artifact_links_json, br.gate_results_json, br.created_at, br.updated_at,
       t.title, t.status, t.created_at, t.updated_at,
       d.id, d.action, d.reason, d.actor_type, d.actor_id, d.actor_role, d.created_at
FROM board_review_tasks br
LEFT JOIN tasks t ON t.id = br.task_id
LEFT JOIN board_review_dispositions d ON d.task_id = br.task_id
WHERE br.employee_id = ?
ORDER BY `+boardHistoryOrderKey("br.updated_at")+` DESC, br.task_id ASC
LIMIT ?`, employeeID, limit+1)
	if err != nil {
		return nil, false, err
	}
	defer rows.Close()

	out := []operations.BoardReviewRecord{}
	truncated := false
	for rows.Next() {
		var (
			record                               operations.BoardReviewRecord
			artifactJSON, gateJSON               string
			created, updated                     string
			dispID, dispAction, dispReason       sql.NullString
			dispActorType, dispActorID, dispRole sql.NullString
			dispCreated                          sql.NullString
			taskTitle, taskStatus                sql.NullString
			taskCreated, taskUpdated             sql.NullString
		)
		if err := rows.Scan(&record.TaskID, &record.MissionName, &record.RunID, &record.ReviewGate,
			&record.SourcePDDVersion, &artifactJSON, &gateJSON, &created, &updated,
			&taskTitle, &taskStatus, &taskCreated, &taskUpdated,
			&dispID, &dispAction, &dispReason, &dispActorType, &dispActorID, &dispRole, &dispCreated); err != nil {
			return nil, false, err
		}
		if len(out) == limit {
			truncated = true
			break
		}
		createdAt, createdOK := parseBoardClock(created)
		updatedAt, updatedOK := parseBoardClock(updated)
		record.CreatedAt, record.UpdatedAt = createdAt, updatedAt
		record.ClockUnparsed = !createdOK || !updatedOK
		// Counts only. The stored artifact references and gate results are
		// untrusted source text and are never carried into the UI as
		// navigable targets or interpreted claims.
		record.ArtifactLinkCount = countJSONObjectKeys(artifactJSON)
		record.GateResultCount = countJSONObjectKeys(gateJSON)
		// Native task facts, carried exactly as the task row stores them. The
		// status is the task's own stored status; it is never derived from the
		// disposition, and the task clocks stay separate from the review
		// record's clocks above.
		record.TaskTitle = taskTitle.String
		record.TaskStatus = taskStatus.String
		record.TaskCreatedAt, _ = parseBoardClock(taskCreated.String)
		record.TaskUpdatedAt, _ = parseBoardClock(taskUpdated.String)
		if dispID.Valid && strings.TrimSpace(dispID.String) != "" {
			dispositionAt, _ := parseBoardClock(dispCreated.String)
			record.Disposition = &operations.BoardReviewDisposition{
				ID:        dispID.String,
				TaskID:    record.TaskID,
				Action:    dispAction.String,
				Reason:    dispReason.String,
				ActorType: dispActorType.String,
				ActorID:   dispActorID.String,
				ActorRole: dispRole.String,
				CreatedAt: dispositionAt,
			}
		}
		out = append(out, record)
	}
	if err := rows.Err(); err != nil {
		return nil, false, err
	}
	return out, truncated, nil
}

// parseBoardClock reports whether the stored Board record clock could be read.
// An unreadable clock stays zero and is disclosed; it is never replaced with the
// read time or with any other row's time.
func parseBoardClock(raw string) (time.Time, bool) {
	raw = strings.TrimSpace(raw)
	if raw == "" {
		return time.Time{}, false
	}
	parsed, err := time.Parse(time.RFC3339Nano, raw)
	if err != nil {
		return time.Time{}, false
	}
	return parsed.UTC(), true
}

func countJSONObjectKeys(raw string) int {
	var decoded map[string]json.RawMessage
	if err := json.Unmarshal([]byte(raw), &decoded); err != nil {
		return 0
	}
	return len(decoded)
}
