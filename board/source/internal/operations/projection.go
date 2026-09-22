package operations

import (
	"errors"
	"fmt"
	"strings"
	"time"
)

type Contradiction struct {
	DimensionA  string              `json:"dimension_a"`
	DimensionB  string              `json:"dimension_b"`
	Description string              `json:"description"`
	ObservedAt  time.Time           `json:"observed_at"`
	Evidence    []EvidenceReference `json:"evidence"`
}

func (c Contradiction) Validate() error {
	a, b := strings.TrimSpace(c.DimensionA), strings.TrimSpace(c.DimensionB)
	v := &acc{}
	v.bad(a == "" || b == "", "contradiction requires dimension_a and dimension_b").
		bad(a == b, "contradiction dimensions must differ, both %q", a).
		bad(strings.TrimSpace(c.Description) == "", "contradiction requires a description").
		bad(c.ObservedAt.IsZero(), "contradiction observed_at must be non-zero").
		bad(len(c.Evidence) == 0, "contradiction must be evidence-backed with at least one evidence reference").
		then(func() error { return validateEvidenceList("contradiction.evidence", c.Evidence) })
	return v.err
}

type NeedsLee struct {
	Required   bool                `json:"required"`
	DecisionID string              `json:"decision_id"`
	Scope      string              `json:"scope"`
	Title      string              `json:"title"`
	Reason     string              `json:"reason"`
	RaisedAt   time.Time           `json:"raised_at"`
	Owner      SourceOwner         `json:"owner"`
	Evidence   []EvidenceReference `json:"evidence,omitempty"`
}

// Validate checks a Needs-Lee gate: a positive, evidence-backed decision gate
// identifying the affected employee (or explicit fleet scope). required=false is
func (n NeedsLee) Validate() error {
	a := &acc{}
	a.bad(!n.Required, "needs_lee item must have required=true (absence of a gate is represented by omission)").
		bad(strings.TrimSpace(n.DecisionID) == "", "needs_lee requires a stable decision_id").
		then(n.validateScope).
		bad(strings.TrimSpace(n.Title) == "", "needs_lee requires a title").
		bad(strings.TrimSpace(n.Reason) == "", "needs_lee requires a reason").
		bad(n.RaisedAt.IsZero(), "needs_lee raised_at must be non-zero").
		bad(!isValidSourceOwner(n.Owner), "needs_lee owner is not a native source owner: %q", n.Owner).
		bad(len(n.Evidence) == 0, "needs_lee requires evidence").
		then(func() error { return validateEvidenceList("needs_lee.evidence", n.Evidence) })
	return a.err
}

// validateScope requires the gate to name the affected employee or the explicit
func (n NeedsLee) validateScope() error {
	if n.Scope == FleetScope {
		return nil
	}
	if err := validateEmployeeID(n.Scope); err != nil {
		return fmt.Errorf("needs_lee scope must be an employee_id or %q: %w", FleetScope, err)
	}
	return nil
}

type ChallengeFinding struct {
	RuleID      string              `json:"rule_id"`
	RuleVersion string              `json:"rule_version"`
	Category    ChallengeCategory   `json:"category"`
	Severity    ChallengeSeverity   `json:"severity"`
	Message     string              `json:"message"`
	ObservedAt  time.Time           `json:"observed_at"`
	Evidence    []EvidenceReference `json:"evidence,omitempty"`
}

func (f ChallengeFinding) Validate() error {
	a := &acc{}
	a.bad(strings.TrimSpace(f.RuleID) == "", "challenge_finding requires rule_id").
		bad(strings.TrimSpace(f.RuleVersion) == "", "challenge_finding requires rule_version").
		bad(!isValidChallengeCategory(f.Category), "unsupported challenge category: %q", f.Category).
		bad(!isValidChallengeSeverity(f.Severity), "unsupported challenge severity: %q", f.Severity).
		bad(strings.TrimSpace(f.Message) == "", "challenge_finding requires a message").
		bad(f.ObservedAt.IsZero(), "challenge_finding observed_at must be non-zero").
		bad(len(f.Evidence) == 0, "challenge_finding must be evidence-backed").
		then(func() error { return validateEvidenceList("challenge_finding.evidence", f.Evidence) })
	return a.err
}

type ManagerialProjection struct {
	SchemaVersion        string                    `json:"schema_version"`
	EmployeeID           string                    `json:"employee_id"`
	Placement            Placement                 `json:"placement"`
	PlacementEvidence    []EvidenceReference       `json:"placement_evidence,omitempty"`
	Condition            Condition                 `json:"condition"`
	ConditionEvidence    []EvidenceReference       `json:"condition_evidence,omitempty"`
	LatestDeliveredValue string                    `json:"latest_delivered_value,omitempty"`
	LatestDeliveredAt    *time.Time                `json:"latest_delivered_at,omitempty"`
	LatestValueEvidence  []EvidenceReference       `json:"latest_value_evidence,omitempty"`
	NonDeliveryCount     *int                      `json:"non_delivery_count"`
	NeedsLee             *NeedsLee                 `json:"needs_lee,omitempty"`
	Contradictions       []Contradiction           `json:"contradictions,omitempty"`
	ChallengeFindings    []ChallengeFinding        `json:"challenge_findings,omitempty"`
	SourceFreshness      map[SourceOwner]Freshness `json:"source_freshness,omitempty"`
	SourceErrors         []SourceError             `json:"source_errors,omitempty"`
	Dimensions           SourceDimensions          `json:"dimensions"`
	ObservedAt           time.Time                 `json:"observed_at"`
}

func (p ManagerialProjection) Validate() error {
	a := &acc{}
	a.bad(p.SchemaVersion != ContractSchemaVersion, "unsupported or missing schema_version: %q", p.SchemaVersion).
		then(func() error { return validateEmployeeID(p.EmployeeID) }).
		bad(!isValidPlacement(p.Placement), "unsupported placement: %q", p.Placement).
		bad(!isValidCondition(p.Condition), "unsupported condition: %q", p.Condition).
		bad(p.ObservedAt.IsZero(), "missing or zero observed_at").
		then(p.validateNonDeliveryCount).
		bad(isPositivePlacement(p.Placement) && len(p.PlacementEvidence) == 0, "positive placement %q requires placement_evidence", p.Placement).
		bad(isPositiveCondition(p.Condition) && len(p.ConditionEvidence) == 0, "positive condition %q requires condition_evidence", p.Condition).
		bad((p.LatestDeliveredValue != "") != (p.LatestDeliveredAt != nil), "latest_delivered_value and latest_delivered_at must be set together or not at all").
		bad(p.LatestDeliveredAt != nil && p.LatestDeliveredAt.IsZero(), "latest_delivered_at must be non-zero when set").
		bad(p.LatestDeliveredValue != "" && len(p.LatestValueEvidence) == 0, "a latest delivered value claim requires latest_value_evidence").
		bad(p.Condition == ConditionNeedsLee && p.NeedsLee == nil, "condition needs_lee requires a needs_lee payload").
		then(p.validateNeedsLee).
		then(p.validateSourceFreshness).
		then(func() error {
			return validateList("contradictions", len(p.Contradictions), func(i int) error { return p.Contradictions[i].Validate() })
		}).
		then(func() error {
			return validateList("challenge_findings", len(p.ChallengeFindings), func(i int) error { return p.ChallengeFindings[i].Validate() })
		}).
		then(func() error { return validateSourceErrorList("source_errors", p.SourceErrors) }).
		then(p.Dimensions.validate).
		then(func() error { return validateEvidenceList("placement_evidence", p.PlacementEvidence) }).
		then(func() error { return validateEvidenceList("condition_evidence", p.ConditionEvidence) }).
		then(func() error { return validateEvidenceList("latest_value_evidence", p.LatestValueEvidence) })
	return a.err
}
func (p ManagerialProjection) validateNonDeliveryCount() error {
	if p.NonDeliveryCount == nil {
		return errors.New("non_delivery_count must be present (an omitted count must not become an asserted zero; state explicit zero or a positive value)")
	}
	if *p.NonDeliveryCount < 0 {
		return fmt.Errorf("non_delivery_count must be nonnegative, got %d", *p.NonDeliveryCount)
	}
	return nil
}
func (p ManagerialProjection) validateNeedsLee() error {
	if p.NeedsLee == nil {
		return nil
	}
	if p.Condition != ConditionNeedsLee {
		return fmt.Errorf("needs_lee payload present but condition is %q, not needs_lee", p.Condition)
	}
	return p.NeedsLee.Validate()
}
func (p ManagerialProjection) validateSourceFreshness() error {
	for owner, f := range p.SourceFreshness {
		if !isValidSourceOwner(owner) {
			return fmt.Errorf("source_freshness key %q is not a native source owner", owner)
		}
		if !isValidFreshness(f) {
			return fmt.Errorf("source_freshness[%q] has unsupported value %q", owner, f)
		}
	}
	return nil
}
