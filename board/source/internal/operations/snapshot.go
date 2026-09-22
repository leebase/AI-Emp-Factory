package operations

import (
	"bytes"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"strings"
	"time"
)

var forbiddenControlKeys = map[string]bool{
	"action": true, "actions": true, "command": true, "commands": true,
	"control": true, "controls": true, "pause_requested": true,
	"resume_requested": true, "kill": true, "dispatch": true,
	"restart": true, "cancel": true, "execute": true, "mutate": true,
	"approve": true, "direction": true, "send_direction": true,
	"redirect": true, "route": true, "routing": true, "schedule_change": true,
}

type EmployeeOperationSnapshot struct {
	SchemaVersion      string                 `json:"schema_version"`
	SnapshotID         string                 `json:"snapshot_id"`
	GeneratedAt        time.Time              `json:"generated_at"`
	ValidUntil         time.Time              `json:"valid_until"`
	SourceObservations []SourceObservation    `json:"source_observations,omitempty"`
	Employees          []ManagerialProjection `json:"employees"`
	NeedsLee           []NeedsLee             `json:"needs_lee,omitempty"`
	ChallengeFindings  []ChallengeFinding     `json:"challenge_findings,omitempty"`
	SourceErrors       []SourceError          `json:"source_errors,omitempty"`
	Evidence           []EvidenceReference    `json:"evidence,omitempty"`
}

func (s EmployeeOperationSnapshot) Validate() error {
	a := &acc{}
	a.bad(s.SchemaVersion != ContractSchemaVersion, "unsupported or missing schema_version: %q", s.SchemaVersion).
		bad(strings.TrimSpace(s.SnapshotID) == "", "missing snapshot_id").
		bad(s.GeneratedAt.IsZero() || s.ValidUntil.IsZero(), "missing generated_at or valid_until").
		bad(s.ValidUntil.Before(s.GeneratedAt), "valid_until must not be before generated_at").
		then(s.validateCorrespondence).
		then(func() error {
			return validateList("challenge_findings", len(s.ChallengeFindings), func(i int) error { return s.ChallengeFindings[i].Validate() })
		}).
		then(func() error { return validateSourceErrorList("source_errors", s.SourceErrors) }).
		then(func() error { return validateEvidenceList("evidence", s.Evidence) })
	return a.err
}

// uniqueness, two-way observation/projection correspondence, and single-use
func (s EmployeeOperationSnapshot) validateCorrespondence() error {
	seenObs := map[string]bool{}
	obsEmployees := map[string]bool{}
	for i, obs := range s.SourceObservations {
		if err := obs.Validate(); err != nil {
			return fmt.Errorf("source_observations[%d]: %w", i, err)
		}
		key := obs.EmployeeID + "\x00" + string(obs.Owner)
		if seenObs[key] {
			return fmt.Errorf("duplicate source observation for employee %q owner %q", obs.EmployeeID, obs.Owner)
		}
		seenObs[key] = true
		obsEmployees[obs.EmployeeID] = true
	}
	if len(s.Employees) == 0 {
		return errors.New("snapshot must contain at least one employee projection")
	}
	seenEmp := map[string]bool{}
	seenDecision := map[string]bool{}
	for i, emp := range s.Employees {
		if err := emp.Validate(); err != nil {
			return fmt.Errorf("employees[%d]: %w", i, err)
		}
		if seenEmp[emp.EmployeeID] {
			return fmt.Errorf("duplicate employee projection for %q", emp.EmployeeID)
		}
		seenEmp[emp.EmployeeID] = true
		if !obsEmployees[emp.EmployeeID] {
			return fmt.Errorf("employee %q has no corresponding source observation", emp.EmployeeID)
		}
		if emp.NeedsLee != nil {
			if seenDecision[emp.NeedsLee.DecisionID] {
				return fmt.Errorf("duplicate or contradictory needs_lee decision %q", emp.NeedsLee.DecisionID)
			}
			seenDecision[emp.NeedsLee.DecisionID] = true
		}
	}
	for id := range obsEmployees {
		if !seenEmp[id] {
			return fmt.Errorf("source observation for employee %q has no corresponding projection", id)
		}
	}
	for i, n := range s.NeedsLee {
		if err := n.Validate(); err != nil {
			return fmt.Errorf("needs_lee[%d]: %w", i, err)
		}
		if seenDecision[n.DecisionID] {
			return fmt.Errorf("duplicate or contradictory needs_lee decision %q", n.DecisionID)
		}
		seenDecision[n.DecisionID] = true
	}
	return nil
}
func DecodeSnapshot(raw []byte) (EmployeeOperationSnapshot, error) {
	var s EmployeeOperationSnapshot
	if err := decodeStrict(raw, &s); err != nil {
		return EmployeeOperationSnapshot{}, err
	}
	if err := s.Validate(); err != nil {
		return EmployeeOperationSnapshot{}, err
	}
	return s, nil
}
func DecodeSourceObservation(raw []byte) (SourceObservation, error) {
	var o SourceObservation
	if err := decodeStrict(raw, &o); err != nil {
		return SourceObservation{}, err
	}
	if err := o.Validate(); err != nil {
		return SourceObservation{}, err
	}
	return o, nil
}
func decodeStrict(raw []byte, dst any) error {
	// A deep scan catches control/action fields even where they would land in a
	if err := ValidateNoControlActionFields(raw); err != nil {
		return err
	}
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		return fmt.Errorf("strict decode failed: %w", err)
	}
	return expectEOF(dec)
}

// field at any depth. This layer is read-only by contract.
func ValidateNoControlActionFields(raw []byte) error {
	var body any
	dec := json.NewDecoder(bytes.NewReader(raw))
	dec.UseNumber()
	if err := dec.Decode(&body); err != nil {
		return fmt.Errorf("malformed JSON: %w", err)
	}
	if err := expectEOF(dec); err != nil {
		return err
	}
	return checkControlKeys(body)
}

// expectEOF confirms the decoder consumed exactly one JSON document: a second
func expectEOF(dec *json.Decoder) error {
	if err := dec.Decode(new(json.RawMessage)); err != io.EOF {
		return errors.New("unexpected trailing data after JSON document")
	}
	return nil
}
func checkControlKeys(v any) error {
	switch val := v.(type) {
	case map[string]any:
		for k, child := range val {
			if forbiddenControlKeys[strings.ToLower(k)] {
				return fmt.Errorf("forbidden control/action field: %q", k)
			}
			if err := checkControlKeys(child); err != nil {
				return err
			}
		}
	case []any:
		for _, item := range val {
			if err := checkControlKeys(item); err != nil {
				return err
			}
		}
	}
	return nil
}
