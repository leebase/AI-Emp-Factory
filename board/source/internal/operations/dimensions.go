package operations

import (
	"errors"
	"fmt"
	"strings"
	"time"
)

// validateOpaqueFact keeps a not-yet-standardized native-owner state string safe
func validateOpaqueFact(field, v string) error {
	if v == "" {
		return nil
	}
	if len(v) > 512 {
		return fmt.Errorf("%s opaque fact exceeds 512 bytes", field)
	}
	if strings.ContainsAny(v, "\x00\n\r\t") {
		return fmt.Errorf("%s opaque fact contains control characters", field)
	}
	return nil
}
func validateOpaqueFacts(dim string, fields map[string]string) error {
	for name, v := range fields {
		if err := validateOpaqueFact(dim+"."+name, v); err != nil {
			return err
		}
	}
	return nil
}

type MissionDimension struct {
	MissionRef     string `json:"mission_ref"`
	AdmissionState string `json:"admission_state,omitempty"`
	LoopState      string `json:"loop_state,omitempty"`
	Paused         *bool  `json:"paused"`
	PauseReason    string `json:"pause_reason,omitempty"`
	LastOutcome    string `json:"last_outcome,omitempty"`
	// Optional typed managerial facts owned by Auto-Orch (Sprint 3). These are
	// explicit semantic fields, never parsed from opaque strings, and they are
	// strictly optional so Sprint 1/2 producers remain compatible.
	Assignment *AssignmentFact `json:"assignment,omitempty"`
	Cadence    *CadenceFact    `json:"cadence,omitempty"`
	Cycles     []CycleFact     `json:"cycles,omitempty"`
	HumanGate  *HumanGateFact  `json:"human_gate,omitempty"`
	Blocker    *BlockerFact    `json:"blocker,omitempty"`
	Limitation *LimitationFact `json:"limitation,omitempty"`
}

func (d *MissionDimension) validate() error {
	if strings.TrimSpace(d.MissionRef) == "" {
		return errors.New("mission dimension: mission_ref must be non-empty")
	}
	if d.Paused == nil {
		return errors.New("mission dimension: paused must be present (explicit true or false, never an omitted default)")
	}
	if err := validateOpaqueFacts("mission", map[string]string{
		"admission_state": d.AdmissionState, "loop_state": d.LoopState,
		"pause_reason": d.PauseReason, "last_outcome": d.LastOutcome,
	}); err != nil {
		return err
	}
	if d.Assignment != nil {
		if err := d.Assignment.Validate(); err != nil {
			return err
		}
	}
	if d.Cadence != nil {
		if err := d.Cadence.Validate(); err != nil {
			return err
		}
	}
	if d.Blocker != nil {
		if err := d.Blocker.Validate(); err != nil {
			return err
		}
	}
	if d.Limitation != nil {
		if err := d.Limitation.Validate(); err != nil {
			return err
		}
	}
	if d.HumanGate != nil {
		if err := d.HumanGate.Validate(); err != nil {
			return err
		}
	}
	return validateList("mission cycles", len(d.Cycles), func(i int) error { return d.Cycles[i].Validate() })
}

type GovernedRunDimension struct {
	RunID       string `json:"run_id"`
	RunStatus   string `json:"run_status,omitempty"`
	OutcomeID   string `json:"outcome_id,omitempty"`
	EvidenceRef string `json:"evidence_ref,omitempty"`
}

func (d *GovernedRunDimension) validate() error {
	if strings.TrimSpace(d.RunID) == "" {
		return errors.New("governed_run dimension: run_id must be non-empty")
	}
	if d.EvidenceRef != "" {
		if err := validateEvidenceURI(d.EvidenceRef); err != nil {
			return fmt.Errorf("governed_run dimension evidence_ref: %w", err)
		}
	}
	return validateOpaqueFact("governed_run.run_status", d.RunStatus)
}

type CommissioningDimension struct {
	LifecycleState string `json:"lifecycle_state"`
	CommissionedAt string `json:"commissioned_at,omitempty"`
	// Optional typed commissioning facts owned by Factory (Sprint 3). They are
	// explicit semantic fields so readiness/commissioning is never inferred from
	// the opaque lifecycle_state string.
	Commissioned *bool `json:"commissioned,omitempty"`
	Ready        *bool `json:"ready,omitempty"`
}

func (d *CommissioningDimension) validate() error {
	if strings.TrimSpace(d.LifecycleState) == "" {
		return errors.New("commissioning dimension: lifecycle_state must be non-empty")
	}
	if err := validateOpaqueFact("commissioning.lifecycle_state", d.LifecycleState); err != nil {
		return err
	}
	return validateOptionalRFC3339("commissioning.commissioned_at", d.CommissionedAt)
}

type ScheduleDimension struct {
	Enabled  *bool  `json:"enabled"`
	CronExpr string `json:"cron_expr,omitempty"`
	NextFire string `json:"next_fire,omitempty"`
}

func (d *ScheduleDimension) validate() error {
	if d.Enabled == nil {
		return errors.New("schedule dimension: enabled must be present (explicit true or false, never an omitted default)")
	}
	return validateOptionalRFC3339("schedule.next_fire", d.NextFire)
}

type ProcessDimension struct {
	Live       *bool  `json:"live"`
	StatusText string `json:"status_text,omitempty"`
}

func (d *ProcessDimension) validate() error {
	if d.Live == nil {
		return errors.New("process dimension: live must be present (explicit true or false, never an omitted default)")
	}
	return validateOpaqueFact("process.status_text", d.StatusText)
}

type BoardDimension struct {
	ActiveTasks  *int   `json:"active_tasks"`
	ReviewTasks  *int   `json:"review_tasks"`
	BlockedTasks *int   `json:"blocked_tasks"`
	StaleLeases  *int   `json:"stale_leases"`
	LastEventAt  string `json:"last_event_at,omitempty"`
}

func (d *BoardDimension) validate() error {
	for name, n := range map[string]*int{
		"active_tasks": d.ActiveTasks, "review_tasks": d.ReviewTasks,
		"blocked_tasks": d.BlockedTasks, "stale_leases": d.StaleLeases,
	} {
		if n == nil {
			return fmt.Errorf("board dimension: %s must be present (an omitted counter must not become an asserted zero)", name)
		}
		if *n < 0 {
			return fmt.Errorf("board dimension: %s must be nonnegative, got %d", name, *n)
		}
	}
	return validateOptionalRFC3339("board.last_event_at", d.LastEventAt)
}

type SourceDimensions struct {
	Mission       *MissionDimension       `json:"mission,omitempty"`
	GovernedRun   *GovernedRunDimension   `json:"governed_run,omitempty"`
	Commissioning *CommissioningDimension `json:"commissioning,omitempty"`
	Schedule      *ScheduleDimension      `json:"schedule,omitempty"`
	Process       *ProcessDimension       `json:"process,omitempty"`
	Board         *BoardDimension         `json:"board,omitempty"`
}

// ownedDimension pairs a populated dimension with the sole native owner allowed
type ownedDimension struct {
	name  string
	owner SourceOwner
	value interface{ validate() error }
}

// a typed-nil interface value is never produced.
func (d SourceDimensions) present() []ownedDimension {
	var out []ownedDimension
	add := func(name string, owner SourceOwner, present bool, v interface{ validate() error }) {
		if present {
			out = append(out, ownedDimension{name, owner, v})
		}
	}
	add("mission", OwnerAutoOrch, d.Mission != nil, d.Mission)
	add("governed_run", OwnerAgentOrch, d.GovernedRun != nil, d.GovernedRun)
	add("commissioning", OwnerFactory, d.Commissioning != nil, d.Commissioning)
	add("schedule", OwnerSchedule, d.Schedule != nil, d.Schedule)
	add("process", OwnerProcess, d.Process != nil, d.Process)
	add("board", OwnerBoard, d.Board != nil, d.Board)
	return out
}
func (d SourceDimensions) validate() error {
	for _, od := range d.present() {
		if err := od.value.validate(); err != nil {
			return err
		}
	}
	return nil
}

func validateOptionalRFC3339(field, value string) error {
	if value == "" {
		return nil
	}
	if _, err := time.Parse(time.RFC3339, value); err != nil {
		return fmt.Errorf("%s must be RFC3339, got %q", field, value)
	}
	return nil
}
