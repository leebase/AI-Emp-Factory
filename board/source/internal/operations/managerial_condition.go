package operations

import (
	"fmt"
	"time"
)

// deriveCondition evaluates the condition ladder. Precedence: burning >
// needs_lee > blocked > degraded > healthy > unknown. Needs-Lee is derived
// independently of Condition so a real gate can coexist with burning without
// hiding either decision. Every positive conclusion cites rule id/version and
// the source observations; missing, stale, conflicting, ambiguous, or truncated
// required inputs fail closed to UNKNOWN.
func deriveCondition(employeeID string, fresh map[SourceOwner]*FleetObservation, views map[SourceOwner]*FleetOwner, now time.Time,
	contradictions *[]Contradiction, findings *[]ManagerialFinding) (Condition, Reason, *int, bool, *NeedsLee, *DeliveredValue) {
	autoView := views[OwnerAutoOrch]
	autoFresh := fresh[OwnerAutoOrch]
	factoryFresh := fresh[OwnerFactory]

	cs, cycleConflicts := collectCycles(autoView, now)
	*contradictions = append(*contradictions, cycleConflicts...)
	truncated := autoView != nil && (autoView.Truncated || autoView.TiedRecordsTruncated)
	streak, ambiguous, count := computeStreak(cs, truncated)
	burning := streak >= 3

	var burnSrcs []SourceRef
	for _, nc := range cs {
		if !nc.accepted && !nc.conflict {
			burnSrcs = append(burnSrcs, nc.src)
		}
	}

	var blocked, degraded bool
	var blockerSrc, limitationSrc []SourceRef
	if autoFresh != nil && !conflicted(views, OwnerAutoOrch) {
		if m := autoFresh.Observation.Dimensions.Mission; m != nil {
			if b := m.Blocker; b != nil && b.Active != nil && *b.Active && b.Repairing != nil && !*b.Repairing {
				blocked = true
				blockerSrc = srcRefs(autoFresh)
			}
			if l := m.Limitation; l != nil && l.Active != nil && *l.Active {
				degraded = true
				limitationSrc = srcRefs(autoFresh)
			}
		}
	}

	// Needs-Lee: only a current explicit native-owner gate with concrete
	// evidence is a genuine gate; informational warnings are excluded. A gate
	// timestamp may not exceed its source observation time or the evaluation
	// instant (a fabricated future gate is never current).
	var needsLee *NeedsLee
	var gateSrc []SourceRef
	if autoFresh != nil && !conflicted(views, OwnerAutoOrch) {
		if m := autoFresh.Observation.Dimensions.Mission; m != nil && m.HumanGate != nil && m.HumanGate.Blocking != nil && *m.HumanGate.Blocking {
			gateSrc = srcRefs(autoFresh)
			needsLee = buildNeedsLee(m.HumanGate, autoFresh, now)
		}
	}

	latest := latestAccepted(cs)

	activity := 0
	for _, nc := range cs {
		if nc.delivered && !nc.accepted && !nc.conflict {
			activity++
		}
	}
	if activity > 0 {
		*findings = append(*findings, ManagerialFinding{RuleID: RuleChallengeActivity, RuleVersion: ManagerialRuleVersion,
			Category: ChallengeActivityWithoutVal, Severity: ChallengeSeverityWarn,
			Message: fmt.Sprintf("%d cycle(s) produced output without accepted value", activity),
			Sources: deliverySources(cs)})
	}
	repairs := repairLineage(cs)
	if len(repairs) >= 2 {
		*findings = append(*findings, ManagerialFinding{RuleID: RuleChallengeRepair, RuleVersion: ManagerialRuleVersion,
			Category: ChallengeLongRepairLineage, Severity: ChallengeSeverityWarn,
			Message: fmt.Sprintf("repeated repair lineage across %d cycle(s)", len(repairs)),
			Sources: repairSources(cs)})
	}
	if burning {
		*findings = append(*findings, ManagerialFinding{RuleID: RuleChallengeBurn, RuleVersion: ManagerialRuleVersion,
			Category: ChallengeBurnLoop, Severity: ChallengeSeverityCritical,
			Message: "three or more consecutive non-delivering cycles",
			Sources: append([]SourceRef(nil), burnSrcs...)})
	}
	// The retained last-known success is re-validated and re-aged from its raw
	// record at this evaluation instant. A cached ComputedFreshness is never
	// trusted: it was computed when the read model was built and a forged one
	// must not revive stale raw data. A record that fails validation or does not
	// correspond to this native owner and affected employee is not usable truth
	// either, and is challenged rather than believed.
	for _, owner := range allSourceOwners {
		v := views[owner]
		if v == nil || v.LastKnownSuccess == nil {
			continue
		}
		lks := v.LastKnownSuccess
		corresponds := lks.Observation.Owner == owner && lks.Observation.EmployeeID == employeeID &&
			lks.Observation.Validate() == nil
		if corresponds && ComputedFreshness(lks.Observation, now) == FreshnessFresh {
			continue
		}
		msg := "last-known success for " + string(owner) + " is stale and not current truth"
		if !corresponds {
			msg = "last-known success for " + string(owner) + " does not correspond to this owner/employee or failed validation"
		}
		*findings = append(*findings, ManagerialFinding{RuleID: RuleChallengeStale, RuleVersion: ManagerialRuleVersion,
			Category: ChallengeStaleTruth, Severity: ChallengeSeverityWarn,
			Message: msg, Sources: srcRefs(lks)})
	}
	for _, owner := range allSourceOwners {
		if v := views[owner]; v != nil && v.SameTimeConflict {
			if v.Latest != nil {
				*contradictions = append(*contradictions, Contradiction{
					DimensionA: "same_instant", DimensionB: string(owner),
					Description: "same-instant observations for " + string(owner) + " disagree",
					ObservedAt:  v.Latest.Observation.ObservedAt,
					Evidence:    v.Latest.Observation.Evidence,
				})
			}
			*findings = append(*findings, ManagerialFinding{RuleID: RuleChallengeConflict, RuleVersion: ManagerialRuleVersion,
				Category: ChallengeConflictingSources, Severity: ChallengeSeverityWarn,
				Message: "same-instant " + string(owner) + " observations disagree; current truth is unknown",
				Sources: oneOfSources(views[owner])})
		}
	}
	if autoFresh != nil && !conflicted(views, OwnerAutoOrch) {
		if m := autoFresh.Observation.Dimensions.Mission; m != nil && m.HumanGate != nil &&
			(m.HumanGate.Blocking == nil || !*m.HumanGate.Blocking) {
			*findings = append(*findings, ManagerialFinding{RuleID: RuleChallengeGate, RuleVersion: ManagerialRuleVersion,
				Category: ChallengeUnjustifiedGate, Severity: ChallengeSeverityInfo,
				Message: "human gate is informational, not a blocking authority boundary",
				Sources: srcRefs(autoFresh)})
		}
	}
	if burning {
		if len(repairs) >= 2 {
			*findings = append(*findings, ManagerialFinding{RuleID: RuleChallengeNextMove, RuleVersion: ManagerialRuleVersion,
				Category: ChallengeBurnLoop, Severity: ChallengeSeverityInfo,
				Message: "next smallest move: close the oldest open repair cycle before launching new work",
				Sources: repairSources(cs)})
		} else {
			*findings = append(*findings, ManagerialFinding{RuleID: RuleChallengeNextMove, RuleVersion: ManagerialRuleVersion,
				Category: ChallengeBurnLoop, Severity: ChallengeSeverityInfo,
				Message: "next smallest move: resolve the oldest non-delivering cycle before launching new work",
				Sources: append([]SourceRef(nil), burnSrcs...)})
		}
	}

	condition := ConditionUnknown
	cReason := reason(RuleConditionUnknown, "required condition evidence missing, stale, or conflicting", nil)
	// Current valid non-conflicted Auto-Orch facts alone establish Burning,
	// Needs-Lee, Blocked, and Degraded; a missing/stale Factory owner must never
	// suppress a known burn loop or human boundary. Factory commissioning and
	// readiness are required only for Healthy.
	autoOK := autoFresh != nil && !conflicted(views, OwnerAutoOrch)
	factoryOK := factoryFresh != nil && !conflicted(views, OwnerFactory)
	ready := false
	if factoryOK {
		if cd := factoryFresh.Observation.Dimensions.Commissioning; cd != nil {
			ready = cd.Commissioned != nil && *cd.Commissioned && cd.Ready != nil && *cd.Ready
		}
	}
	if autoOK {
		switch {
		case burning:
			condition = ConditionBurning
			cReason = reason(RuleConditionBurning, "three or more consecutive non-delivering cycles", burnSrcs)
		case needsLee != nil:
			condition = ConditionNeedsLee
			cReason = reason(RuleConditionNeedsLee, "current explicit human authority boundary blocks the next useful action", gateSrc)
		case blocked:
			condition = ConditionBlocked
			cReason = reason(RuleConditionBlocked, "unresolved concrete blocker not actively repairing", blockerSrc)
		case degraded:
			condition = ConditionDegraded
			cReason = reason(RuleConditionDegraded, "current limitation reduces confidence or capability", limitationSrc)
		case factoryOK && ready && !ambiguous:
			condition = ConditionHealthy
			cReason = reason(RuleConditionHealthy, "current readiness with no unresolved blocker or limitation",
				srcRefs(factoryFresh, autoFresh))
		case factoryOK && ambiguous:
			cReason = reason(RuleConditionUnknown, "delivery history is truncated; a complete streak or zero cannot be claimed", nil)
		}
	}
	return condition, cReason, count, ambiguous, needsLee, latest
}

// buildNeedsLee converts a fresh blocking native gate into the Needs-Lee
// payload. Gate evidence is the observation's own evidence plus the gate's
// opaque owner reference (marked unverified: the Board never certifies the
// owner's evidence). A gate with no evidence or an out-of-range timestamp is
// not a concrete current boundary and yields no Needs-Lee.
func buildNeedsLee(g *HumanGateFact, fo *FleetObservation, now time.Time) *NeedsLee {
	raisedAt, err := time.Parse(time.RFC3339, g.RaisedAt)
	if err != nil || raisedAt.IsZero() || raisedAt.After(fo.Observation.ObservedAt) || raisedAt.After(now) {
		return nil
	}
	evidence := append([]EvidenceReference(nil), fo.Observation.Evidence...)
	if g.EvidenceRef != "" {
		evidence = append(evidence, EvidenceReference{Owner: OwnerAutoOrch, URI: g.EvidenceRef,
			VerificationStatus: VerificationUnknown, ObservedAt: raisedAt})
	}
	if len(evidence) == 0 {
		return nil
	}
	return &NeedsLee{Required: true, DecisionID: g.DecisionID, Scope: g.Scope, Title: g.Title,
		Reason: g.Reason, RaisedAt: raisedAt, Owner: OwnerAutoOrch, Evidence: evidence}
}

func oneOfSources(v *FleetOwner) []SourceRef {
	if v == nil || v.Latest == nil {
		return nil
	}
	return srcRefs(v.Latest)
}
