package operations

import "time"

// Sprint 5 additive card context. The accepted managerial projection answers
// "where is this employee and how is it doing"; the employee card additionally
// has to answer "what is it working on right now", "when is its next
// checkpoint" and "is it ready on the bench". Those facts already exist in the
// owner dimensions, so this adapter only selects and types them. It derives
// nothing: no placement, no condition, no value, no burn rule is read or
// changed here, and no opaque string is parsed into a status.
//
// Three invariants hold for every field below:
//
//  1. A fact is carried only from an owner whose freshness was recomputed as
//     fresh at evaluation time and which has no same-time conflict. A stale,
//     missing, unknown or contradictory owner contributes nothing, so the card
//     shows an explicit unknown rather than an old value dressed as current.
//  2. Every carried fact keeps the source reference it came from, so the UI can
//     show original attribution and link to the raw observation.
//  3. Absence is absence. A nil pointer or an empty struct means "not known",
//     never "false", "none" or "zero".
const CardContextRuleVersion = "1.0"

// Explicit work kinds. They name the owner fact that established current work,
// never a parsed free-text status.
const (
	CardWorkGovernedRun       = "governed_run"
	CardWorkMissionAssignment = "mission_assignment"
)

// CardWork is the explicit current-work fact: an active governed run, or an
// active mission assignment. Ref may be empty when the owner asserted the
// activity without a reference; that stays an explicit unknown reference.
type CardWork struct {
	Kind    string      `json:"kind"`
	Ref     string      `json:"ref,omitempty"`
	Status  string      `json:"status,omitempty"`
	Sources []SourceRef `json:"sources,omitempty"`
}

// CardCheckpoint is the schedule owner's own next-fire fact. NextFire is
// populated only from an RFC3339 timestamp the owner itself published; an
// unparseable or absent value leaves it nil rather than guessing a time.
type CardCheckpoint struct {
	Enabled  *bool       `json:"enabled"`
	NextFire *time.Time  `json:"next_fire"`
	CronExpr string      `json:"cron_expr,omitempty"`
	Sources  []SourceRef `json:"sources,omitempty"`
}

// CardReadiness is Factory's own commissioning/readiness fact, used to say
// whether a benched employee is actually ready to be given work.
type CardReadiness struct {
	LifecycleState string      `json:"lifecycle_state,omitempty"`
	Commissioned   *bool       `json:"commissioned"`
	Ready          *bool       `json:"ready"`
	Sources        []SourceRef `json:"sources,omitempty"`
}

// CardCadence is Auto-Orch's explicit cadence-policy fact. It explains why a
// bench employee has no next run without inventing one.
type CardCadence struct {
	Enabled *bool       `json:"enabled"`
	Sources []SourceRef `json:"sources,omitempty"`
}

// CardContext is the whole additive context block for one card. It is omitted
// entirely when no fresh unconflicted owner supplied any of its facts.
type CardContext struct {
	RuleVersion string          `json:"rule_version"`
	Work        *CardWork       `json:"work,omitempty"`
	Checkpoint  *CardCheckpoint `json:"checkpoint,omitempty"`
	Readiness   *CardReadiness  `json:"readiness,omitempty"`
	Cadence     *CardCadence    `json:"cadence,omitempty"`
}

// DeriveCardContext selects the card context for one already-derived managerial
// employee. It reuses the employee's evaluation-time freshness map, so it can
// never be fresher than the projection it accompanies.
func DeriveCardContext(e ManagerialEmployee) *CardContext {
	usable := usableOwners(e)
	ctx := CardContext{RuleVersion: CardContextRuleVersion}
	ctx.Work = deriveCardWork(usable)
	ctx.Checkpoint = deriveCardCheckpoint(usable[OwnerSchedule])
	ctx.Readiness = deriveCardReadiness(usable[OwnerFactory])
	ctx.Cadence = deriveCardCadence(usable[OwnerAutoOrch])
	if ctx.Work == nil && ctx.Checkpoint == nil && ctx.Readiness == nil && ctx.Cadence == nil {
		return nil
	}
	return &ctx
}

// usableOwners keeps only owners that are fresh at evaluation time, provably
// free of a same-time conflict, not carrying an error receipt, and actually
// about this employee. Everything else is treated as absent.
//
// TiedRecordsTruncated disqualifies an owner in its own right. It means the
// finite row cap cut a same-instant tie group, so records sharing the cutoff
// observed_at were dropped before the conflict check could ever see them. A
// false SameTimeConflict is then only a statement about the records that
// survived the cap, not about the owner's actual equal-time evidence: an
// equally-timed contradiction may sit in the part that was never read. Absence
// of a detected conflict is not proof that no conflict exists, so an owner
// whose tie group is incomplete cannot certify an unconflicted fact. It fails
// closed here and the card shows an explicit unknown instead.
//
// This is a context-selection rule only. Placement, condition, latest value and
// the burn rules read the truncation flags through their own paths and are not
// changed by this.
func usableOwners(e ManagerialEmployee) map[SourceOwner]*FleetObservation {
	usable := map[SourceOwner]*FleetObservation{}
	for i := range e.Sources.Owners {
		owner := &e.Sources.Owners[i]
		if owner.Latest == nil || owner.SameTimeConflict || owner.TiedRecordsTruncated {
			continue
		}
		if e.SourceFreshness[owner.Owner] != FreshnessFresh {
			continue
		}
		obs := owner.Latest.Observation
		if obs.Owner != owner.Owner || obs.EmployeeID != e.EmployeeID || obs.Error != nil {
			continue
		}
		if obs.Validate() != nil {
			continue
		}
		usable[owner.Owner] = owner.Latest
	}
	return usable
}

// deriveCardWork prefers a governed run whose status is an explicit member of
// the accepted active-run allowlist, which is the same closed vocabulary the
// placement rule already uses to claim Running. "Not terminal" is not a claim
// of activity: queued, pending, paused, empty and unrecognised statuses are
// unknown, not work. Failing an active run it uses an explicitly active mission
// assignment; an inactive assignment is not work either.
func deriveCardWork(usable map[SourceOwner]*FleetObservation) *CardWork {
	if run := usable[OwnerAgentOrch]; run != nil {
		if d := run.Observation.Dimensions.GovernedRun; d != nil && isActiveRunStatus(d.RunStatus) {
			return &CardWork{Kind: CardWorkGovernedRun, Ref: d.RunID, Status: d.RunStatus, Sources: srcRefs(run)}
		}
	}
	mission := usable[OwnerAutoOrch]
	if mission == nil {
		return nil
	}
	d := mission.Observation.Dimensions.Mission
	if d == nil || d.Assignment == nil || d.Assignment.Active == nil || !*d.Assignment.Active {
		return nil
	}
	return &CardWork{Kind: CardWorkMissionAssignment, Ref: d.Assignment.Ref, Sources: srcRefs(mission)}
}

func deriveCardCheckpoint(schedule *FleetObservation) *CardCheckpoint {
	if schedule == nil {
		return nil
	}
	d := schedule.Observation.Dimensions.Schedule
	if d == nil {
		return nil
	}
	checkpoint := CardCheckpoint{Enabled: d.Enabled, CronExpr: d.CronExpr, Sources: srcRefs(schedule)}
	if d.NextFire != "" {
		// Only the owner's own RFC3339 instant is accepted. Anything else stays
		// an unknown next fire instead of a fabricated one.
		if parsed, err := time.Parse(time.RFC3339, d.NextFire); err == nil {
			utc := parsed.UTC()
			checkpoint.NextFire = &utc
		}
	}
	if checkpoint.Enabled == nil && checkpoint.NextFire == nil && checkpoint.CronExpr == "" {
		return nil
	}
	return &checkpoint
}

func deriveCardReadiness(factory *FleetObservation) *CardReadiness {
	if factory == nil {
		return nil
	}
	d := factory.Observation.Dimensions.Commissioning
	if d == nil {
		return nil
	}
	return &CardReadiness{
		LifecycleState: d.LifecycleState,
		Commissioned:   d.Commissioned,
		Ready:          d.Ready,
		Sources:        srcRefs(factory),
	}
}

func deriveCardCadence(mission *FleetObservation) *CardCadence {
	if mission == nil {
		return nil
	}
	d := mission.Observation.Dimensions.Mission
	if d == nil || d.Cadence == nil || d.Cadence.Enabled == nil {
		return nil
	}
	return &CardCadence{Enabled: d.Cadence.Enabled, Sources: srcRefs(mission)}
}
