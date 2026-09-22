package operations

import "time"

// Sprint 6B2 bounded native employee-associated Board history.
//
// This is a read-only capture of Board's own mutable records, kept deliberately
// apart from the immutable owner observations the managerial projection is
// derived from. Nothing in this file participates in placement, condition,
// value, the decision gate, the challenge findings or the quick state; it is
// carried on the detail response and read by detail presentation only.
//
// Invariants this DTO exists to preserve:
//
//   - Association is the native stored employee_id column, and nothing else. A
//     mission name, an agent id, an assigned-agent field or a task title is
//     never an association. A generic task carries no employee id and is
//     reachable only through an authentic BoardReviewTask task_id.
//   - Board record clocks stay Board record clocks. CreatedAt and UpdatedAt are
//     when Board wrote the row; they are never an execution time, an acceptance
//     time or a source revision time.
//   - A recorded disposition is a review decision with a stored actor
//     attribution. It is not an accepted value, not a producer certification,
//     not authority to say the employee delivered, and ActorID is stored
//     attribution rather than a verified producer identity.
//   - Payload, review instructions, gate results and artifact references are
//     untrusted inert source text. They are carried, never interpreted, and
//     never turned into a navigable target.
//   - The window is bounded in SQL before materialization and truncation is
//     explicit. An empty window means no associated record was returned by this
//     bounded query, not that the employee did no work.
const (
	// BoardHistorySchemaVersion versions this additive detail-only section. It
	// is independent of the managerial DTO schema so neither can weaken the
	// other's validators.
	BoardHistorySchemaVersion = "ai-employee-board-history/1.0"

	// DefaultBoardHistoryLimit is the per-kind row bound applied in SQL.
	DefaultBoardHistoryLimit = 20
	// MaxBoardHistoryLimit is the hard ceiling a caller may not exceed.
	MaxBoardHistoryLimit = 50
)

// BoardCycleReport is one captured auto-orchestrator cycle report row that
// carries this employee's native employee id.
type BoardCycleReport struct {
	MissionName string    `json:"mission_name"`
	CycleID     string    `json:"cycle_id"`
	ActorID     string    `json:"actor_id,omitempty"`
	CreatedAt   time.Time `json:"created_at"`
	UpdatedAt   time.Time `json:"updated_at"`
	// ClockUnparsed marks a stored clock the Board could not parse. The row is
	// still shown; the time is not invented.
	ClockUnparsed bool `json:"clock_unparsed,omitempty"`
}

// BoardReviewDisposition is the stored decision on one review task, associated
// through the authentic task_id.
type BoardReviewDisposition struct {
	ID        string    `json:"id"`
	TaskID    string    `json:"task_id"`
	Action    string    `json:"action"`
	Reason    string    `json:"reason,omitempty"`
	ActorType string    `json:"actor_type"`
	ActorID   string    `json:"actor_id"`
	ActorRole string    `json:"actor_role,omitempty"`
	CreatedAt time.Time `json:"created_at"`
}

// BoardReviewRecord is one captured review task row carrying this employee's
// native employee id, plus its disposition when one is recorded and the native
// facts of the task it was raised on.
//
// The task fields come from the task the review row already names through its
// authentic task_id foreign key. A generic task carries no employee id, so this
// is the only path by which any task fact may appear here: it is never matched
// by title, mission or assigned agent. TaskStatus is the task's own stored
// status and is never inferred from a disposition, and the task clocks are the
// task row's own clocks, kept separate from the review row's clocks.
type BoardReviewRecord struct {
	TaskID            string                  `json:"task_id"`
	TaskTitle         string                  `json:"task_title,omitempty"`
	TaskStatus        string                  `json:"task_status,omitempty"`
	TaskCreatedAt     time.Time               `json:"task_created_at,omitempty"`
	TaskUpdatedAt     time.Time               `json:"task_updated_at,omitempty"`
	MissionName       string                  `json:"mission_name"`
	RunID             string                  `json:"run_id"`
	ReviewGate        string                  `json:"review_gate"`
	SourcePDDVersion  string                  `json:"source_pdd_version,omitempty"`
	ArtifactLinkCount int                     `json:"artifact_link_count"`
	GateResultCount   int                     `json:"gate_result_count"`
	Disposition       *BoardReviewDisposition `json:"disposition,omitempty"`
	CreatedAt         time.Time               `json:"created_at"`
	UpdatedAt         time.Time               `json:"updated_at"`
	ClockUnparsed     bool                    `json:"clock_unparsed,omitempty"`
}

// EmployeeBoardHistory is the bounded, deterministic, read-only window. It is
// additive on the managerial detail response and absent everywhere else; an
// absent section means unavailable, never zero associated records.
type EmployeeBoardHistory struct {
	SchemaVersion string `json:"schema_version"`
	EmployeeID    string `json:"employee_id"`
	// Limit is the per-kind bound the SQL query actually applied.
	Limit                 int                 `json:"limit"`
	CycleReports          []BoardCycleReport  `json:"cycle_reports"`
	CycleReportsTruncated bool                `json:"cycle_reports_truncated"`
	Reviews               []BoardReviewRecord `json:"reviews"`
	ReviewsTruncated      bool                `json:"reviews_truncated"`
	// CapturedAt is when this window was read from Board. It is the read clock,
	// not an execution, acceptance or record clock.
	CapturedAt time.Time `json:"captured_at"`
}

// BoardHistoryLimit clamps a requested per-kind bound into the supported range.
func BoardHistoryLimit(limit int) int {
	if limit <= 0 {
		return DefaultBoardHistoryLimit
	}
	if limit > MaxBoardHistoryLimit {
		return MaxBoardHistoryLimit
	}
	return limit
}
