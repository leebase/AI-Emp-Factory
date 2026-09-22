package operations

import (
	"strings"
	"time"
)

// activeRunStatuses is the closed allowlist of known-active governed run
// statuses. A run is claimable as Running only when the status is an explicit
// member of this set; queued, empty, pending, typos, and unknowns are never
// live. This is a placement-claim allowlist only — it deliberately does not
// change MaxAgeForObservation's conservative unknown=>short-window freshness
// policy (which still treats unrecognized statuses as active).
var activeRunStatuses = map[string]bool{
	"running": true, "in_progress": true, "in-progress": true, "active": true,
	"executing": true, "started": true, "working": true, "live": true,
}

func isActiveRunStatus(status string) bool {
	return activeRunStatuses[strings.ToLower(strings.TrimSpace(status))]
}

// derivePlacement evaluates the placement ladder independent of condition.
// Explicit pause and below-commissioning are respected; every positive branch
// requires fresh, non-conflicting observation evidence. Missing, stale, or
// conflicting required inputs yield UNKNOWN, never bench or scheduled.
func derivePlacement(fresh map[SourceOwner]*FleetObservation, views map[SourceOwner]*FleetOwner, now time.Time) (Placement, Reason) {
	// Product plan: verified current employee-bound live run or process is
	// placement-first, ahead of not-commissioned, pause, scheduled, and bench.
	// Observable activity is never hidden by a pause or commissioning label; the
	// retained pause/below-commissioning facts stay in the raw sources.
	var runSrcs []*FleetObservation
	if fo := fresh[OwnerAgentOrch]; fo != nil && !conflicted(views, OwnerAgentOrch) {
		if d := fo.Observation.Dimensions.GovernedRun; d != nil && isActiveRunStatus(d.RunStatus) {
			runSrcs = append(runSrcs, fo)
		}
	}
	if fo := fresh[OwnerProcess]; fo != nil && !conflicted(views, OwnerProcess) {
		if d := fo.Observation.Dimensions.Process; d != nil && d.Live != nil && *d.Live {
			runSrcs = append(runSrcs, fo)
		}
	}
	if len(runSrcs) > 0 {
		return PlacementRunning, reason(RulePlacementRunning, "verified current employee-bound live run or process", srcRefs(runSrcs...))
	}
	if fo := fresh[OwnerFactory]; fo != nil && !conflicted(views, OwnerFactory) {
		if d := fo.Observation.Dimensions.Commissioning; d != nil && d.Commissioned != nil && !*d.Commissioned {
			return PlacementNotCommissioned, reason(RulePlacementNotCommissioned, "Factory reports the commissioning gate is not crossed", srcRefs(fo))
		}
	}
	if fo := fresh[OwnerAutoOrch]; fo != nil && !conflicted(views, OwnerAutoOrch) {
		if d := fo.Observation.Dimensions.Mission; d != nil && d.Paused != nil && *d.Paused {
			return PlacementPaused, reason(RulePlacementPaused, "Auto-Orch explicitly paused the mission", srcRefs(fo))
		}
	}
	auto, sched := fresh[OwnerAutoOrch], fresh[OwnerSchedule]
	if auto != nil && sched != nil && !conflicted(views, OwnerAutoOrch) && !conflicted(views, OwnerSchedule) {
		am := auto.Observation.Dimensions.Mission
		sd := sched.Observation.Dimensions.Schedule
		if am != nil && sd != nil && (am.Paused == nil || !*am.Paused) &&
			am.Cadence != nil && am.Cadence.Enabled != nil && *am.Cadence.Enabled &&
			sd.Enabled != nil && *sd.Enabled && fireInFuture(sd.NextFire, now) {
			return PlacementScheduled, reason(RulePlacementScheduled, "credible enabled assignment/cadence with a future next execution", srcRefs(auto, sched))
		}
	}
	factory, proc, agent := fresh[OwnerFactory], fresh[OwnerProcess], fresh[OwnerAgentOrch]
	if factory != nil && auto != nil && sched != nil && proc != nil && agent != nil &&
		!conflicted(views, OwnerFactory) && !conflicted(views, OwnerAutoOrch) &&
		!conflicted(views, OwnerSchedule) && !conflicted(views, OwnerProcess) && !conflicted(views, OwnerAgentOrch) {
		cd := factory.Observation.Dimensions.Commissioning
		ready := cd != nil && cd.Commissioned != nil && *cd.Commissioned && cd.Ready != nil && *cd.Ready
		am := auto.Observation.Dimensions.Mission
		noAssignment := am != nil && am.Assignment != nil && am.Assignment.Active != nil && !*am.Assignment.Active
		sd := sched.Observation.Dimensions.Schedule
		noSchedule := sd != nil && sd.Enabled != nil && !*sd.Enabled
		procDown := proc.Observation.Dimensions.Process != nil && proc.Observation.Dimensions.Process.Live != nil && !*proc.Observation.Dimensions.Process.Live
		if ready && noAssignment && noSchedule && procDown && agentRunTerminalOrAbsent(agent) {
			return PlacementOnTheBench, reason(RulePlacementBench,
				"commissioned and readiness-clear with explicit no active assignment or schedule and no live run/process",
				srcRefs(factory, auto, sched, proc, agent))
		}
	}
	msg := "required placement evidence missing or stale"
	if missing := missingOwners(fresh, OwnerFactory, OwnerAutoOrch, OwnerSchedule, OwnerProcess, OwnerAgentOrch); len(missing) > 0 {
		msg = "required placement evidence missing/stale: " + strings.Join(missing, ", ")
	}
	return PlacementUnknown, reason(RulePlacementUnknown, msg, nil)
}
