package operations

import (
	"fmt"
	"time"
)

// Sprint 3A quick shared state artifact. One versioned canonical object is built
// from the accepted ManagerialFleet and nothing else: evidence, unknowns,
// nullable metrics, independent NeedsLee gates and same-time conflicts are
// carried through unchanged. The compact Markdown in active_state_markdown.go
// renders this exact value, so both representations always describe the same
// snapshot. The snapshot id is the content digest, which makes a mixed or
// partially written artifact detectable rather than merely unlikely.
const (
	// ActiveStateSchemaVersion versions the artifact envelope. It is distinct
	// from the managerial DTO version, which is carried alongside it.
	ActiveStateSchemaVersion = "ai-employee-active-state/1.0"
	snapshotIDPrefix         = "sha256:"
)

// ActiveStateCard is the compact per-employee card. NeedsLee stays independent
// of Condition, and NonDeliveryCount stays nullable, exactly as derived.
type ActiveStateCard struct {
	EmployeeID        string                    `json:"employee_id"`
	Placement         Placement                 `json:"placement"`
	PlacementReason   Reason                    `json:"placement_reason"`
	Condition         Condition                 `json:"condition"`
	ConditionReason   Reason                    `json:"condition_reason"`
	LatestValue       *DeliveredValue           `json:"latest_value"`
	NonDeliveryCount  *int                      `json:"non_delivery_count"`
	DeliveryTruncated bool                      `json:"delivery_truncated"`
	NeedsLee          *NeedsLee                 `json:"needs_lee"`
	Contradictions    []Contradiction           `json:"contradictions,omitempty"`
	Findings          []ManagerialFinding       `json:"findings,omitempty"`
	SourceFreshness   map[SourceOwner]Freshness `json:"source_freshness,omitempty"`
	ObservedAt        time.Time                 `json:"observed_at"`
	// Context is the Sprint 5 additive card context (current work, next
	// checkpoint, bench readiness, cadence). It is purely selected from fresh
	// unconflicted owner facts and is omitted when none exist, so an artifact
	// written before Sprint 5 still re-derives its own snapshot id unchanged.
	Context *CardContext `json:"context,omitempty"`
}

// ActiveStateNeedsLee is one fleet-level entry in the independent Needs-Lee
// list; the gate itself is preserved verbatim, including required=false.
type ActiveStateNeedsLee struct {
	EmployeeID string   `json:"employee_id"`
	Gate       NeedsLee `json:"gate"`
}

// ActiveStateFinding is one deterministic challenge finding attributed to its
// employee. Rule id/version and source references are preserved.
type ActiveStateFinding struct {
	EmployeeID string            `json:"employee_id"`
	Finding    ManagerialFinding `json:"finding"`
}

// ActiveStateSource is one owner's read state for one employee. ExpiresAt is
// populated only for a currently fresh observation and is the instant that
// observation stops being fresh under its own owner rule.
type ActiveStateSource struct {
	EmployeeID        string      `json:"employee_id"`
	Owner             SourceOwner `json:"owner"`
	Status            string      `json:"status"`
	ComputedFreshness Freshness   `json:"computed_freshness"`
	ObservationID     string      `json:"observation_id,omitempty"`
	ObservedAt        *time.Time  `json:"observed_at"`
	ExpiresAt         *time.Time  `json:"expires_at"`
	// RunStatus is the original governed-run status, carried only because it is
	// the field that selects the active (2m) versus terminal (24h) Agent-Orch
	// limit. It exists so a persisted artifact's bound can be recomputed from
	// source truth, never asserted by the artifact itself.
	RunStatus              string   `json:"run_status,omitempty"`
	SameTimeConflict       bool     `json:"same_time_conflict"`
	ConflictObservationIDs []string `json:"conflict_observation_ids,omitempty"`
	Truncated              bool     `json:"truncated,omitempty"`
}

// ActiveStateError is an explicit collection error or absence receipt. Absence
// is reported as an error entry rather than silently omitted.
type ActiveStateError struct {
	EmployeeID string      `json:"employee_id"`
	Owner      SourceOwner `json:"owner"`
	Code       string      `json:"code"`
	Message    string      `json:"message"`
	ObservedAt *time.Time  `json:"observed_at"`
}

// ActiveStateEvidence references one evidence item behind the projection. The
// artifact carries references only; it never copies or certifies content.
type ActiveStateEvidence struct {
	EmployeeID    string            `json:"employee_id"`
	ObservationID string            `json:"observation_id"`
	Reference     EvidenceReference `json:"reference"`
}

// ActiveState is the versioned canonical artifact published atomically and
// rendered to Markdown without a second collection.
type ActiveState struct {
	SchemaVersion           string                `json:"schema_version"`
	SnapshotID              string                `json:"snapshot_id"`
	RuleVersion             string                `json:"rule_version"`
	ManagerialSchemaVersion string                `json:"managerial_schema_version"`
	GeneratedAt             time.Time             `json:"generated_at"`
	ValidUntil              time.Time             `json:"valid_until"`
	Cards                   []ActiveStateCard     `json:"cards"`
	NeedsLee                []ActiveStateNeedsLee `json:"needs_lee"`
	Findings                []ActiveStateFinding  `json:"findings"`
	Sources                 []ActiveStateSource   `json:"sources"`
	Errors                  []ActiveStateError    `json:"errors"`
	Evidence                []ActiveStateEvidence `json:"evidence"`
	TotalEmployees          int                   `json:"total_employees"`
	EmployeesTruncated      bool                  `json:"employees_truncated"`
}

// BuildActiveState projects the accepted managerial fleet into the artifact. It
// is a pure restructuring: no fact is recomputed, defaulted, or dropped.
func BuildActiveState(fleet ManagerialFleet) (ActiveState, error) {
	if fleet.SchemaVersion != ManagerialSchemaVersion {
		return ActiveState{}, fmt.Errorf("active state requires managerial schema %q, got %q", ManagerialSchemaVersion, fleet.SchemaVersion)
	}
	if fleet.GeneratedAt.IsZero() {
		return ActiveState{}, fmt.Errorf("active state requires a non-zero generated_at")
	}
	state := ActiveState{
		SchemaVersion:           ActiveStateSchemaVersion,
		RuleVersion:             fleet.RuleVersion,
		ManagerialSchemaVersion: fleet.SchemaVersion,
		GeneratedAt:             fleet.GeneratedAt.UTC(),
		Cards:                   make([]ActiveStateCard, 0, len(fleet.Employees)),
		NeedsLee:                []ActiveStateNeedsLee{},
		Findings:                []ActiveStateFinding{},
		Sources:                 []ActiveStateSource{},
		Errors:                  []ActiveStateError{},
		Evidence:                []ActiveStateEvidence{},
		TotalEmployees:          fleet.TotalEmployees,
		EmployeesTruncated:      fleet.EmployeesTruncated,
	}
	for _, e := range fleet.Employees {
		state.Cards = append(state.Cards, ActiveStateCard{
			EmployeeID: e.EmployeeID, Placement: e.Placement, PlacementReason: e.PlacementReason,
			Condition: e.Condition, ConditionReason: e.ConditionReason, LatestValue: e.LatestValue,
			NonDeliveryCount: e.NonDeliveryCount, DeliveryTruncated: e.DeliveryTruncated,
			NeedsLee: e.NeedsLee, Contradictions: e.Contradictions, Findings: e.Findings,
			SourceFreshness: e.SourceFreshness, ObservedAt: e.ObservedAt,
			Context: DeriveCardContext(e),
		})
		if e.NeedsLee != nil {
			state.NeedsLee = append(state.NeedsLee, ActiveStateNeedsLee{EmployeeID: e.EmployeeID, Gate: *e.NeedsLee})
		}
		for _, f := range e.Findings {
			state.Findings = append(state.Findings, ActiveStateFinding{EmployeeID: e.EmployeeID, Finding: f})
		}
		state.appendOwnerState(e)
	}
	state.ValidUntil = deriveValidUntil(state, fleet.ValidUntil)
	id, err := ComputeSnapshotID(state)
	if err != nil {
		return ActiveState{}, err
	}
	state.SnapshotID = id
	return state, nil
}

// appendOwnerState records every owner of one employee: its read status, its
// conflicts, an explicit error/absence receipt, and its evidence references.
func (s *ActiveState) appendOwnerState(e ManagerialEmployee) {
	for _, owner := range e.Sources.Owners {
		entry := ActiveStateSource{
			EmployeeID: e.EmployeeID, Owner: owner.Owner, Status: owner.Status,
			ComputedFreshness: freshnessFor(e, owner), SameTimeConflict: owner.SameTimeConflict,
			ConflictObservationIDs: owner.ConflictObservationIDs, Truncated: owner.Truncated,
		}
		if owner.Latest != nil {
			observed := owner.Latest.Observation.ObservedAt
			entry.ObservationID = owner.Latest.ObservationID
			entry.ObservedAt = &observed
			if run := owner.Latest.Observation.Dimensions.GovernedRun; run != nil {
				entry.RunStatus = run.RunStatus
			}
			if entry.ComputedFreshness == FreshnessFresh {
				expires := observed.Add(MaxAgeForObservation(owner.Latest.Observation))
				entry.ExpiresAt = &expires
			}
			for _, ref := range owner.Latest.Observation.Evidence {
				s.Evidence = append(s.Evidence, ActiveStateEvidence{
					EmployeeID: e.EmployeeID, ObservationID: owner.Latest.ObservationID, Reference: ref,
				})
			}
		}
		s.Sources = append(s.Sources, entry)
		if err := ownerErrorReceipt(e.EmployeeID, owner); err != nil {
			s.Errors = append(s.Errors, *err)
		}
	}
}

// freshnessFor prefers the managerial evaluation-time freshness map, which is
// recomputed from raw observed_at values, over the read model's cached value.
func freshnessFor(e ManagerialEmployee, owner FleetOwner) Freshness {
	if f, ok := e.SourceFreshness[owner.Owner]; ok {
		return f
	}
	return owner.ComputedFreshness
}

// ownerErrorReceipt turns a missing, failed, unavailable, unknown or stale
// owner into an explicit receipt. A healthy fresh owner produces none.
func ownerErrorReceipt(employeeID string, owner FleetOwner) *ActiveStateError {
	if owner.Latest == nil {
		return &ActiveStateError{EmployeeID: employeeID, Owner: owner.Owner, Code: OwnerStatusMissing,
			Message: "no observation has ever been recorded for this owner"}
	}
	observed := owner.Latest.Observation.ObservedAt
	if e := owner.Latest.Observation.Error; e != nil {
		return &ActiveStateError{EmployeeID: employeeID, Owner: owner.Owner, Code: e.Code, Message: e.Message, ObservedAt: &observed}
	}
	switch owner.Status {
	case OwnerStatusFresh:
		return nil
	default:
		return &ActiveStateError{EmployeeID: employeeID, Owner: owner.Owner, Code: owner.Status,
			Message: "owner truth is not currently fresh", ObservedAt: &observed}
	}
}

// deriveValidUntil never extends source freshness. The artifact is valid only
// until the earliest currently fresh observation stops being fresh under its own
// owner rule, bounded by the read window's own validity. With no fresh source at
// all the artifact is already stale at generation rather than inventing a cache
// lifetime.
func deriveValidUntil(state ActiveState, fleetValidUntil time.Time) time.Time {
	valid := state.GeneratedAt
	if !fleetValidUntil.IsZero() && fleetValidUntil.After(valid) {
		valid = fleetValidUntil.UTC()
	}
	earliest := time.Time{}
	for _, src := range state.Sources {
		expires := src.recomputedExpiry()
		if expires == nil {
			continue
		}
		if earliest.IsZero() || expires.Before(earliest) {
			earliest = expires.UTC()
		}
	}
	if earliest.IsZero() {
		return state.GeneratedAt
	}
	if earliest.Before(valid) {
		return earliest
	}
	return valid
}

// recomputedExpiry is the instant this source stops being fresh, derived from
// its own observed_at and the authoritative owner limit. The stored ExpiresAt is
// never an input, so a rewritten artifact cannot lengthen its own authority.
func (src ActiveStateSource) recomputedExpiry() *time.Time {
	if src.ComputedFreshness != FreshnessFresh || src.ObservedAt == nil || src.ObservedAt.IsZero() {
		return nil
	}
	expires := src.ObservedAt.UTC().Add(MaxAgeForOwner(src.Owner, src.RunStatus))
	return &expires
}

// RecomputeValidUntil derives the bound an artifact is entitled to purely from
// its source timestamps and the owner limits, bounded by the read window. A
// self-consistent digest over a longer window does not survive this.
func RecomputeValidUntil(s ActiveState) time.Time {
	return deriveValidUntil(s, s.GeneratedAt.Add(FleetValidFor))
}
