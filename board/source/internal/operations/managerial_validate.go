package operations

// DTO validation for the versioned managerial projection. The Sprint 3 DTO is
// validated here without touching the Sprint 1 ManagerialProjection validators.

import (
	"fmt"
	"strings"
)

// Validate checks the versioned per-employee DTO shape.
func (e ManagerialEmployee) Validate() error {
	a := &acc{}
	a.bad(e.SchemaVersion != ManagerialSchemaVersion, "unsupported or missing schema_version: %q", e.SchemaVersion).
		then(func() error { return validateEmployeeID(e.EmployeeID) }).
		bad(!isValidPlacement(e.Placement), "unsupported placement: %q", e.Placement).
		bad(!isValidCondition(e.Condition), "unsupported condition: %q", e.Condition).
		bad(e.ObservedAt.IsZero(), "observed_at must be non-zero").
		then(e.validateReasons).
		then(e.validateNonDelivery).
		then(e.validateNeedsLee).
		then(func() error {
			return validateList("contradictions", len(e.Contradictions), func(i int) error { return e.Contradictions[i].Validate() })
		}).
		then(func() error { return validateManagerialFindings(e.Findings) })
	return a.err
}

func (e ManagerialEmployee) validateReasons() error {
	for name, r := range map[string]Reason{"placement_reason": e.PlacementReason, "condition_reason": e.ConditionReason} {
		if strings.TrimSpace(r.RuleID) == "" || strings.TrimSpace(r.RuleVersion) == "" {
			return fmt.Errorf("%s must carry rule id and version", name)
		}
	}
	if isPositivePlacement(e.Placement) && len(e.PlacementReason.Sources) == 0 {
		return fmt.Errorf("positive placement %q requires placement_reason sources", e.Placement)
	}
	if isPositiveCondition(e.Condition) && len(e.ConditionReason.Sources) == 0 {
		return fmt.Errorf("positive condition %q requires condition_reason sources", e.Condition)
	}
	return nil
}

func (e ManagerialEmployee) validateNonDelivery() error {
	if e.NonDeliveryCount != nil && *e.NonDeliveryCount < 0 {
		return fmt.Errorf("non_delivery_count must be nonnegative, got %d", *e.NonDeliveryCount)
	}
	return nil
}

func (e ManagerialEmployee) validateNeedsLee() error {
	if e.NeedsLee == nil {
		return nil
	}
	return e.NeedsLee.Validate()
}

func validateManagerialFindings(list []ManagerialFinding) error {
	for i, f := range list {
		if strings.TrimSpace(f.RuleID) == "" || strings.TrimSpace(f.RuleVersion) == "" {
			return fmt.Errorf("findings[%d] must carry rule id and version", i)
		}
		if !isValidChallengeCategory(f.Category) {
			return fmt.Errorf("findings[%d] has unsupported category %q", i, f.Category)
		}
		if !isValidChallengeSeverity(f.Severity) {
			return fmt.Errorf("findings[%d] has unsupported severity %q", i, f.Severity)
		}
		if len(f.Sources) == 0 {
			return fmt.Errorf("findings[%d] must cite source observations", i)
		}
	}
	return nil
}

// Validate checks the shape of the managerial fleet DTO.
func (f ManagerialFleet) Validate() error {
	a := &acc{}
	a.bad(f.SchemaVersion != ManagerialSchemaVersion, "unsupported or missing schema_version: %q", f.SchemaVersion).
		bad(f.RuleVersion == "", "missing rule_version").
		bad(f.GeneratedAt.IsZero() || f.ValidUntil.IsZero(), "missing generated_at or valid_until").
		bad(f.ValidUntil.Before(f.GeneratedAt), "valid_until must not be before generated_at")
	if a.err != nil {
		return a.err
	}
	seen := map[string]bool{}
	for i, e := range f.Employees {
		if err := e.Validate(); err != nil {
			return fmt.Errorf("employees[%d]: %w", i, err)
		}
		if seen[e.EmployeeID] {
			return fmt.Errorf("duplicate employee projection for %q", e.EmployeeID)
		}
		seen[e.EmployeeID] = true
	}
	if f.TotalEmployees != len(f.Employees) {
		return fmt.Errorf("total_employees %d != derived employee count %d", f.TotalEmployees, len(f.Employees))
	}
	return nil
}
