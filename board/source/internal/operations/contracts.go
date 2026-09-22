// Package operations is the transport-independent employee-operation contract
// layer for Agent Board Mission Control (Sprint 1): standard-library-only
// semantic validation with closed vocabularies, per-owner dimension ownership,
// opaque-fact safety, positive-claim evidence rules, and strict control-free
// decoding. See the type and Validate docs below for the enforced invariants.
package operations

import (
	"errors"
	"fmt"
	"regexp"
	"strings"
)

const ContractSchemaVersion = "ai-employee-operations/1.0"
const FleetScope = "fleet"

var identifierPattern = regexp.MustCompile(`^[A-Za-z][A-Za-z0-9]*(?:[-_.][A-Za-z0-9]+)*$`)

// oneOf reports whether v is a member of the given closed set.
func oneOf[T comparable](v T, set ...T) bool {
	for _, s := range set {
		if v == s {
			return true
		}
	}
	return false
}

// acc collects the first validation failure so a Validate body reads as a terse
type acc struct{ err error }

func (a *acc) bad(cond bool, format string, args ...any) *acc {
	if a.err == nil && cond {
		a.err = fmt.Errorf(format, args...)
	}
	return a
}

// then runs a sub-validator only when no earlier failure exists, so nil guards
func (a *acc) then(fn func() error) *acc {
	if a.err == nil {
		a.err = fn()
	}
	return a
}

// SourceOwner is the closed set of native truth owners. There is deliberately no
type SourceOwner string

const (
	OwnerFactory   SourceOwner = "factory"    // employee identity / commissioning
	OwnerAutoOrch  SourceOwner = "auto_orch"  // mission admission/pause/policy/cycle
	OwnerAgentOrch SourceOwner = "agent_orch" // governed run/step/evidence
	OwnerSchedule  SourceOwner = "schedule"   // cron/systemd observation
	OwnerProcess   SourceOwner = "process"    // host process observation
	OwnerBoard     SourceOwner = "board"      // native Board task/review/lease state
	OwnerRouter    SourceOwner = "router"     // router/subscription capacity report
)

func isValidSourceOwner(o SourceOwner) bool {
	return oneOf(o, OwnerFactory, OwnerAutoOrch, OwnerAgentOrch, OwnerSchedule, OwnerProcess, OwnerBoard, OwnerRouter)
}

type Placement string

const (
	PlacementRunning         Placement = "running"
	PlacementScheduled       Placement = "scheduled"
	PlacementOnTheBench      Placement = "on_the_bench"
	PlacementPaused          Placement = "paused"
	PlacementNotCommissioned Placement = "not_commissioned"
	PlacementUnknown         Placement = "unknown"
)

func isValidPlacement(p Placement) bool {
	return oneOf(p, PlacementRunning, PlacementScheduled, PlacementOnTheBench, PlacementPaused, PlacementNotCommissioned, PlacementUnknown)
}
func isPositivePlacement(p Placement) bool { return isValidPlacement(p) && p != PlacementUnknown }

type Condition string

const (
	ConditionHealthy  Condition = "healthy"
	ConditionNeedsLee Condition = "needs_lee"
	ConditionBurning  Condition = "burning"
	ConditionBlocked  Condition = "blocked"
	ConditionDegraded Condition = "degraded"
	ConditionUnknown  Condition = "unknown"
)

func isValidCondition(c Condition) bool {
	return oneOf(c, ConditionHealthy, ConditionNeedsLee, ConditionBurning, ConditionBlocked, ConditionDegraded, ConditionUnknown)
}
func isPositiveCondition(c Condition) bool { return isValidCondition(c) && c != ConditionUnknown }

type Freshness string

const (
	FreshnessFresh   Freshness = "fresh"
	FreshnessStale   Freshness = "stale"
	FreshnessUnknown Freshness = "unknown"
)

func isValidFreshness(f Freshness) bool {
	return oneOf(f, FreshnessFresh, FreshnessStale, FreshnessUnknown)
}

type CollectionResult string

const (
	CollectionResultSuccess     CollectionResult = "success"
	CollectionResultPartial     CollectionResult = "partial"
	CollectionResultFailed      CollectionResult = "failed"
	CollectionResultUnavailable CollectionResult = "unavailable"
	CollectionResultUnknown     CollectionResult = "unknown"
)

func isValidCollectionResult(r CollectionResult) bool {
	return oneOf(r, CollectionResultSuccess, CollectionResultPartial, CollectionResultFailed, CollectionResultUnavailable, CollectionResultUnknown)
}

type ChallengeCategory string

const (
	ChallengeBurnLoop           ChallengeCategory = "burn_loop"
	ChallengeLongRepairLineage  ChallengeCategory = "long_repair_lineage"
	ChallengeActivityWithoutVal ChallengeCategory = "activity_without_value"
	ChallengeStaleTruth         ChallengeCategory = "stale_truth"
	ChallengeConflictingSources ChallengeCategory = "conflicting_sources"
	ChallengeUnjustifiedGate    ChallengeCategory = "unjustified_gate"
	ChallengeRetainedPause      ChallengeCategory = "retained_pause"
)

func isValidChallengeCategory(c ChallengeCategory) bool {
	return oneOf(c, ChallengeBurnLoop, ChallengeLongRepairLineage, ChallengeActivityWithoutVal, ChallengeStaleTruth, ChallengeConflictingSources, ChallengeUnjustifiedGate, ChallengeRetainedPause)
}

type ChallengeSeverity string

const (
	ChallengeSeverityInfo     ChallengeSeverity = "info"
	ChallengeSeverityWarn     ChallengeSeverity = "warn"
	ChallengeSeverityCritical ChallengeSeverity = "critical"
)

func isValidChallengeSeverity(s ChallengeSeverity) bool {
	return oneOf(s, ChallengeSeverityInfo, ChallengeSeverityWarn, ChallengeSeverityCritical)
}

type VerificationStatus string

const (
	VerificationVerified   VerificationStatus = "verified"
	VerificationUnverified VerificationStatus = "unverified"
	VerificationUnknown    VerificationStatus = "unknown"
)

func isValidVerificationStatus(v VerificationStatus) bool {
	return oneOf(v, VerificationVerified, VerificationUnverified, VerificationUnknown)
}

func validateEmployeeID(id string) error {
	id = strings.TrimSpace(id)
	if id == "" {
		return errors.New("missing required field: employee_id")
	}
	switch strings.ToLower(id) {
	case "unknown", "pending", "placeholder", "tbd", "todo", "none":
		return fmt.Errorf("employee_id cannot be placeholder: %s", id)
	}
	if !identifierPattern.MatchString(id) {
		return fmt.Errorf("malformed employee_id: %s", id)
	}
	return nil
}

// validateList applies fn to each index, wrapping failures with field[i].
func validateList(field string, n int, fn func(i int) error) error {
	for i := 0; i < n; i++ {
		if err := fn(i); err != nil {
			return fmt.Errorf("%s[%d]: %w", field, i, err)
		}
	}
	return nil
}
