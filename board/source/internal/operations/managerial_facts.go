package operations

// Sprint 3 optional typed semantic facts owned by Auto-Orch. These extend the

import (
	"errors"
	"fmt"
	"strings"
)

// Sprint 1 contract additively: they are optional, strictly validated, and
// never parsed from opaque strings, so Sprint 1/2 producers remain compatible.

// AssignmentFact is Auto-Orch's explicit current assignment fact. Active must be
// explicit so "no active assignment" is asserted, never inferred from absence.
type AssignmentFact struct {
	Active *bool  `json:"active"`
	Ref    string `json:"ref,omitempty"`
}

func (a *AssignmentFact) Validate() error {
	if a.Active == nil {
		return errors.New("assignment: active must be present (an omitted default must not become an asserted false)")
	}
	return validateOpaqueFact("assignment.ref", a.Ref)
}

// CadenceFact is Auto-Orch's explicit cadence-policy fact.
type CadenceFact struct {
	Enabled *bool `json:"enabled"`
}

func (c *CadenceFact) Validate() error {
	if c.Enabled == nil {
		return errors.New("cadence: enabled must be present")
	}
	return nil
}

// CycleFact is Auto-Orch's raw cycle identity/lineage/outcome fact. Accepted is
// the explicit delivery oracle: accepted=true requires the delivered value and
// time. A bare evidence reference is never a delivery oracle, and the Board
// never re-certifies the owner's accepted flag.
type CycleFact struct {
	CycleID    string `json:"cycle_id"`
	RepairOf   string `json:"repair_of,omitempty"`
	Delivered  *bool  `json:"delivered"`
	Accepted   *bool  `json:"accepted"`
	Value      string `json:"value,omitempty"`
	AcceptedAt string `json:"accepted_at,omitempty"`
	ObservedAt string `json:"observed_at"`
}

func (c *CycleFact) Validate() error {
	a := &acc{}
	a.bad(c.CycleID == "" || !identifierPattern.MatchString(c.CycleID), "cycle: cycle_id must be a valid identifier: %q", c.CycleID).
		bad(c.Delivered == nil, "cycle: delivered must be present (activity vs absence is explicit)").
		bad(c.Accepted == nil, "cycle: accepted must be present (accepted vs nonaccepted success is explicit)").
		bad(c.ObservedAt == "", "cycle: observed_at must be present")
	if a.err != nil {
		return a.err
	}
	if err := validateOptionalRFC3339("cycle.accepted_at", c.AcceptedAt); err != nil {
		return err
	}
	if err := validateOptionalRFC3339("cycle.observed_at", c.ObservedAt); err != nil {
		return err
	}
	if c.RepairOf != "" && !identifierPattern.MatchString(c.RepairOf) {
		return fmt.Errorf("cycle: malformed repair_of cycle id: %q", c.RepairOf)
	}
	if c.Accepted != nil && *c.Accepted {
		// An accepted outcome is an explicitly delivered, value-bearing outcome
		// bound to this cycle: delivered=true, a value, and an acceptance time
		// are all required (a bare evidence reference is never an oracle).
		if c.Delivered != nil && !*c.Delivered {
			return errors.New("cycle: accepted=true requires delivered=true (cannot accept an undelivered outcome)")
		}
		if c.Value == "" {
			return errors.New("cycle: accepted=true requires a delivered value")
		}
		if c.AcceptedAt == "" {
			return errors.New("cycle: accepted=true requires accepted_at")
		}
	}
	return nil
}

// HumanGateFact is Auto-Orch's explicit current human authority boundary. It is
// a concrete gate only when Blocking is explicitly true; informational warnings
// are explicit non-blocking facts and never produce Needs Lee.
type HumanGateFact struct {
	DecisionID  string `json:"decision_id"`
	Scope       string `json:"scope"`
	Title       string `json:"title"`
	Reason      string `json:"reason"`
	Blocking    *bool  `json:"blocking"`
	RaisedAt    string `json:"raised_at"`
	EvidenceRef string `json:"evidence_ref,omitempty"`
}

func (g *HumanGateFact) Validate() error {
	a := &acc{}
	a.bad(strings.TrimSpace(g.DecisionID) == "", "human_gate: decision_id must be non-empty").
		bad(g.Blocking == nil, "human_gate: blocking must be present (informational is explicit false, never an omitted default)").
		bad(strings.TrimSpace(g.Title) == "", "human_gate: title must be non-empty").
		bad(strings.TrimSpace(g.Reason) == "", "human_gate: reason must be non-empty")
	if a.err != nil {
		return a.err
	}
	scope := strings.TrimSpace(g.Scope)
	if scope != FleetScope {
		if err := validateEmployeeID(scope); err != nil {
			return fmt.Errorf("human_gate scope must be an employee_id or %q: %w", FleetScope, err)
		}
	}
	if err := validateOptionalRFC3339("human_gate.raised_at", g.RaisedAt); err != nil {
		return err
	}
	if g.EvidenceRef != "" {
		return validateEvidenceURI(g.EvidenceRef)
	}
	return nil
}

// BlockerFact is Auto-Orch's explicit concrete blocker and repair status.
type BlockerFact struct {
	Active      *bool  `json:"active"`
	Repairing   *bool  `json:"repairing"`
	Description string `json:"description,omitempty"`
}

func (b *BlockerFact) Validate() error {
	a := &acc{}
	a.bad(b.Active == nil, "blocker: active must be present").
		bad(b.Repairing == nil, "blocker: repairing must be present")
	return a.err
}

// LimitationFact is Auto-Orch's explicit current limitation (degraded).
type LimitationFact struct {
	Active      *bool  `json:"active"`
	Description string `json:"description,omitempty"`
}

func (l *LimitationFact) Validate() error {
	if l.Active == nil {
		return errors.New("limitation: active must be present")
	}
	return nil
}
