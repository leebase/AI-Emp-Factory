package operations

import (
	"sort"
	"time"
)

// Sprint 3 deterministic managerial projection. Pure rule evaluation with no
// side effects: every positive conclusion cites a rule id/version and the raw
// source observations it was derived from. Missing, stale, conflicting, or
// truncated inputs fail closed to UNKNOWN and nullable unknowns; nothing is
// ever inferred from opaque strings or invented defaults. The placement,
// cycle/value, and condition/findings derivations live in the sibling
// managerial_placement.go, managerial_value.go, and managerial_condition.go
// files; this file owns the versioned DTO, the entry points, and validation.

const (
	// ManagerialSchemaVersion versions the managerial fleet/detail DTO; it is
	// distinct from the Sprint 1 ManagerialProjection contract so old
	// validators are never weakened.
	ManagerialSchemaVersion = "ai-employee-managerial/1.0"
	// ManagerialRuleVersion is the derivation ruleset version. A rule change
	// bumps this, not the DTO schema.
	ManagerialRuleVersion = "1.0"
)

// Rule IDs. Placement and condition rules cite these on every reason; challenge
// findings cite them with a category, severity, and source references.
const (
	RulePlacementRunning         = "placement.running"
	RulePlacementScheduled       = "placement.scheduled"
	RulePlacementBench           = "placement.on_the_bench"
	RulePlacementPaused          = "placement.paused"
	RulePlacementNotCommissioned = "placement.not_commissioned"
	RulePlacementUnknown         = "placement.unknown"

	RuleConditionBurning  = "condition.burning"
	RuleConditionNeedsLee = "condition.needs_lee"
	RuleConditionBlocked  = "condition.blocked"
	RuleConditionDegraded = "condition.degraded"
	RuleConditionHealthy  = "condition.healthy"
	RuleConditionUnknown  = "condition.unknown"

	RuleChallengeBurn          = "challenge.burn_loop"
	RuleChallengeActivity      = "challenge.activity_without_value"
	RuleChallengeRepair        = "challenge.repair_lineage"
	RuleChallengeStale         = "challenge.stale_truth"
	RuleChallengeConflict      = "challenge.conflicting_sources"
	RuleChallengeGate          = "challenge.gate_check"
	RuleChallengeNextMove      = "challenge.next_move"
	RuleChallengeRetainedPause = "challenge.retained_pause"
)

// SourceRef points at one raw source observation behind a conclusion.
type SourceRef struct {
	Owner         SourceOwner `json:"owner"`
	ObservationID string      `json:"observation_id"`
	ObservedAt    time.Time   `json:"observed_at"`
}

// Reason explains a placement or condition conclusion with rule identity and
// source observation references.
type Reason struct {
	RuleID      string      `json:"rule_id"`
	RuleVersion string      `json:"rule_version"`
	Message     string      `json:"message"`
	Sources     []SourceRef `json:"sources,omitempty"`
}

// ManagerialFinding is one deterministic challenge finding.
type ManagerialFinding struct {
	RuleID      string            `json:"rule_id"`
	RuleVersion string            `json:"rule_version"`
	Category    ChallengeCategory `json:"category"`
	Severity    ChallengeSeverity `json:"severity"`
	Message     string            `json:"message"`
	Sources     []SourceRef       `json:"sources,omitempty"`
}

// DeliveredValue is the newest explicitly accepted native outcome, bound to the
// employee, the outcome (cycle) identity, and the acceptance time.
type DeliveredValue struct {
	Value      string      `json:"value"`
	OutcomeID  string      `json:"outcome_id"`
	AcceptedAt time.Time   `json:"accepted_at"`
	Sources    []SourceRef `json:"sources,omitempty"`
}

// ManagerialEmployee is the versioned per-employee projection. NeedsLee is kept
// independent of Condition so a real human gate can coexist with burning
// without hiding either decision. Sources embeds the raw Sprint 2 owner read
// model (with producer provenance) so the response never loses source truth.
type ManagerialEmployee struct {
	SchemaVersion     string                    `json:"schema_version"`
	EmployeeID        string                    `json:"employee_id"`
	Placement         Placement                 `json:"placement"`
	PlacementReason   Reason                    `json:"placement_reason"`
	Condition         Condition                 `json:"condition"`
	ConditionReason   Reason                    `json:"condition_reason"`
	LatestValue       *DeliveredValue           `json:"latest_value,omitempty"`
	NonDeliveryCount  *int                      `json:"non_delivery_count"`
	DeliveryTruncated bool                      `json:"delivery_truncated"`
	NeedsLee          *NeedsLee                 `json:"needs_lee,omitempty"`
	Contradictions    []Contradiction           `json:"contradictions,omitempty"`
	Findings          []ManagerialFinding       `json:"findings,omitempty"`
	SourceFreshness   map[SourceOwner]Freshness `json:"source_freshness,omitempty"`
	Sources           FleetEmployee             `json:"sources"`
	// BoardHistory is the Sprint 6B2 bounded read-only capture of Board's own
	// mutable records for this employee. It is attached on the detail path
	// only, is never an input to any rule above, and is omitted entirely from
	// the fleet payload so that payload stays byte-identical.
	BoardHistory *EmployeeBoardHistory `json:"board_history,omitempty"`
	ObservedAt   time.Time             `json:"observed_at"`
}

// ManagerialFleet is the versioned fleet managerial view.
type ManagerialFleet struct {
	SchemaVersion      string               `json:"schema_version"`
	RuleVersion        string               `json:"rule_version"`
	GeneratedAt        time.Time            `json:"generated_at"`
	ValidUntil         time.Time            `json:"valid_until"`
	Employees          []ManagerialEmployee `json:"employees"`
	TotalEmployees     int                  `json:"total_employees"`
	EmployeesTruncated bool                 `json:"employees_truncated"`
}

// DeriveFleet derives every employee and carries the same generated/valid
// window and explicit truncation metadata as the underlying bounded read.
func DeriveFleet(fleet OperationsFleet, now time.Time) ManagerialFleet {
	if now.IsZero() {
		now = time.Now().UTC()
	}
	employees := make([]ManagerialEmployee, 0, len(fleet.Employees))
	for _, fe := range fleet.Employees {
		employees = append(employees, DeriveEmployee(fe, now))
	}
	return ManagerialFleet{
		SchemaVersion:      ManagerialSchemaVersion,
		RuleVersion:        ManagerialRuleVersion,
		GeneratedAt:        fleet.GeneratedAt,
		ValidUntil:         fleet.ValidUntil,
		Employees:          employees,
		TotalEmployees:     len(employees),
		EmployeesTruncated: fleet.EmployeesTruncated,
	}
}

// DeriveEmployee produces the deterministic managerial projection for one
// employee from the bounded read model. Freshness is recomputed at evaluation
// time from raw observed_at values, never from producer-supplied fields.
func DeriveEmployee(fleet FleetEmployee, now time.Time) ManagerialEmployee {
	if now.IsZero() {
		now = time.Now().UTC()
	}
	views := map[SourceOwner]*FleetOwner{}
	for i := range fleet.Owners {
		views[fleet.Owners[i].Owner] = &fleet.Owners[i]
	}
	// Freshness is recomputed per-owner from the validated raw latest
	// SourceObservation at this evaluation instant, never from a cached
	// ComputedFreshness: a read model built while an owner was fresh must not
	// stay fresh later, and a forged cached value cannot revive stale raw data.
	// Owner/employee correspondence is verified so a mismatched latest can never
	// establish a positive state.
	freshness := map[SourceOwner]Freshness{}
	fresh := map[SourceOwner]*FleetObservation{}
	for _, owner := range allSourceOwners {
		v := views[owner]
		if v == nil {
			continue
		}
		computed := FreshnessUnknown
		if v.Latest != nil && v.Latest.Observation.Owner == owner && v.Latest.Observation.EmployeeID == fleet.EmployeeID &&
			v.Latest.Observation.Validate() == nil {
			computed = ComputedFreshness(v.Latest.Observation, now)
		}
		freshness[owner] = computed
		if computed == FreshnessFresh {
			fresh[owner] = v.Latest
		}
	}

	var contradictions []Contradiction
	var findings []ManagerialFinding
	placement, pReason := derivePlacement(fresh, views, now)
	condition, cReason, nonDelivery, deliveryTruncated, needsLee, latest :=
		deriveCondition(fleet.EmployeeID, fresh, views, now, &contradictions, &findings)
	// Product plan: verified live work is placement-first. When an employee is
	// running while Auto-Orch retains an explicit pause, observable activity is
	// not hidden — the pause stays in the raw sources and is explained by a
	// versioned finding instead of a contradictory placement.
	if placement == PlacementRunning {
		if fo := fresh[OwnerAutoOrch]; fo != nil {
			if m := fo.Observation.Dimensions.Mission; m != nil && m.Paused != nil && *m.Paused {
				findings = append(findings, ManagerialFinding{RuleID: RuleChallengeRetainedPause, RuleVersion: ManagerialRuleVersion,
					Category: ChallengeRetainedPause, Severity: ChallengeSeverityInfo,
					Message: "employee is running while Auto-Orch retains an explicit pause; observable activity is not hidden",
					Sources: srcRefs(fo)})
			}
		}
	}

	e := ManagerialEmployee{
		SchemaVersion:     ManagerialSchemaVersion,
		EmployeeID:        fleet.EmployeeID,
		Placement:         placement,
		PlacementReason:   pReason,
		Condition:         condition,
		ConditionReason:   cReason,
		LatestValue:       latest,
		NonDeliveryCount:  nonDelivery,
		DeliveryTruncated: deliveryTruncated,
		NeedsLee:          needsLee,
		Contradictions:    contradictions,
		Findings:          findings,
		SourceFreshness:   freshness,
		Sources:           fleet,
		ObservedAt:        now,
	}
	sort.SliceStable(e.Findings, func(i, j int) bool { return e.Findings[i].RuleID < e.Findings[j].RuleID })
	return e
}

func conflicted(views map[SourceOwner]*FleetOwner, owner SourceOwner) bool {
	v := views[owner]
	return v != nil && v.SameTimeConflict
}

func srcRefs(obs ...*FleetObservation) []SourceRef {
	out := make([]SourceRef, 0, len(obs))
	for _, fo := range obs {
		if fo == nil {
			continue
		}
		out = append(out, SourceRef{Owner: fo.Observation.Owner, ObservationID: fo.ObservationID, ObservedAt: fo.Observation.ObservedAt})
	}
	return out
}

func reason(rule, message string, srcs []SourceRef) Reason {
	return Reason{RuleID: rule, RuleVersion: ManagerialRuleVersion, Message: message, Sources: srcs}
}

func fireInFuture(next string, now time.Time) bool {
	if next == "" {
		return false
	}
	t, err := time.Parse(time.RFC3339, next)
	if err != nil {
		return false
	}
	return t.After(now)
}

func agentRunTerminalOrAbsent(fo *FleetObservation) bool {
	d := fo.Observation.Dimensions.GovernedRun
	return d == nil || isTerminalRunStatus(d.RunStatus)
}

func missingOwners(fresh map[SourceOwner]*FleetObservation, owners ...SourceOwner) []string {
	var out []string
	for _, o := range owners {
		if fresh[o] == nil {
			out = append(out, string(o))
		}
	}
	return out
}

// AllSourceOwners exposes the deterministic owner order to consumers/tests.
func AllSourceOwners() []SourceOwner { return append([]SourceOwner(nil), allSourceOwners...) }
