package operations

import (
	"fmt"
	"sort"
	"time"
)

// Cycle/value derivation. Three clocks are distinct and never substituted for
// one another:
//
//   - source revision time (the observation's observed_at) decides supersession:
//     for one stable cycle_id the newest revision is current truth and older
//     revisions are inert, whatever order they arrive in;
//   - cycle start time (the cycle fact's own observed_at) locates the cycle and
//     is never a supersession key;
//   - acceptance time (accepted_at) orders delivered values and breaks the
//     non-delivery streak.
//
// Retries never multiply the streak, truncation never claims a complete streak
// or zero, and no nested fact timestamp may exceed its source observation time
// or the evaluation instant (a fabricated future delivery is never accepted).

// normalizedCycle is one deduplicated raw cycle fact.
type normalizedCycle struct {
	cycleID    string
	repairOf   string
	delivered  bool
	accepted   bool
	value      string
	acceptedAt time.Time
	observedAt time.Time
	src        SourceRef
	conflict   bool
}

// cycleEvent returns the ordering instant of a cycle: its acceptance time when
// accepted (the acceptance event completes delivery), otherwise its observed
// start. Ordering by acceptance time means a newer accepted result outranks
// failures older than its acceptedAt, and the consulted run never drags an
// older acceptance below newer failures.
func cycleEvent(nc normalizedCycle) time.Time {
	if nc.accepted && !nc.conflict {
		return nc.acceptedAt
	}
	return nc.observedAt
}

// sameRevisionDisagrees reports whether two facts for one cycle_id published at
// the same source revision time by distinct snapshots describe that cycle
// differently. It compares the complete relevant content, not just the outcome
// flags: an accepted_at-only, repair-lineage-only or cycle-start-only
// difference is still a disagreement, because at equal revision neither
// snapshot supersedes the other and a differing start is a contradiction about
// which cycle c1 is, not a retry. Only a byte-for-byte equal restatement is an
// exact retry. A cycle already conflicted at this point stays conflicted.
func sameRevisionDisagrees(prev, next normalizedCycle) bool {
	if prev.conflict {
		return true
	}
	return prev.delivered != next.delivered || prev.accepted != next.accepted ||
		prev.value != next.value || prev.repairOf != next.repairOf ||
		!prev.acceptedAt.Equal(next.acceptedAt) || !prev.observedAt.Equal(next.observedAt)
}

// collectCycles gathers raw cycle facts from the Auto-Orch owner view's recent
// window plus the retained last-known success. A fact whose observed_at or
// accepted_at exceeds its source observation time or the evaluation instant is
// rejected (fail closed). Retries of the same stable cycle_id are deduplicated
// on (source revision time, snapshot identity, cycle start): the newest
// revision wins outright and replaces whatever older revisions said, including
// clearing a conflict they left behind, so a superseded disagreement cannot
// contaminate a later resolved revision. Snapshot identity is the source
// observation ID, never the revision timestamp: equal observed_at does not make
// two snapshots one. Within one snapshot a later cycle start is the current
// attempt of a retry lineage; across distinct snapshots at one revision neither
// supersedes, so any content difference (start time included) is a
// contradiction and an exact restatement is an idempotent retry. A conflict at
// a revision is sticky for that revision — a later-start fact republished at the
// same revision cannot resolve it, only a genuinely newer revision can. A
// conflicted cycle is unknown: it never picks a positive state and is never
// counted as confirmed non-delivery.
func collectCycles(v *FleetOwner, now time.Time) ([]normalizedCycle, []Contradiction) {
	if v == nil {
		return nil, nil
	}
	records := v.Recent
	if v.LastKnownSuccess != nil {
		inRecent := false
		for _, r := range v.Recent {
			if r.ObservationID == v.LastKnownSuccess.ObservationID {
				inRecent = true
				break
			}
		}
		if !inRecent {
			records = append(records, *v.LastKnownSuccess)
		}
	}
	byID := map[string]normalizedCycle{}
	var ids []string
	var contradictions []Contradiction
	add := func(c CycleFact, fo FleetObservation) {
		observedAt, err := time.Parse(time.RFC3339, c.ObservedAt)
		if err != nil || observedAt.After(fo.Observation.ObservedAt) || observedAt.After(now) {
			return // a cycle whose time exceeds its source/now cannot be claimed
		}
		acceptedAt, err := time.Parse(time.RFC3339, c.AcceptedAt)
		accepted := false
		if c.Accepted != nil {
			accepted = *c.Accepted
		}
		if accepted && (err != nil || acceptedAt.After(fo.Observation.ObservedAt) || acceptedAt.After(now)) {
			accepted = false // never accept a fabricated future delivery; fail closed
		}
		delivered := false
		if c.Delivered != nil {
			delivered = *c.Delivered
		}
		src := SourceRef{Owner: fo.Observation.Owner, ObservationID: fo.ObservationID, ObservedAt: fo.Observation.ObservedAt}
		prev, ok := byID[c.CycleID]
		nc := normalizedCycle{cycleID: c.CycleID, repairOf: c.RepairOf, delivered: delivered, accepted: accepted,
			value: c.Value, acceptedAt: acceptedAt, observedAt: observedAt, src: src}
		if !ok {
			byID[c.CycleID] = nc
			ids = append(ids, c.CycleID)
			return
		}
		switch {
		case src.ObservedAt.After(prev.src.ObservedAt):
			byID[c.CycleID] = nc // newer revision supersedes, clearing any prior conflict
		case src.ObservedAt.Before(prev.src.ObservedAt):
			// Older revision of an already-superseded cycle: inert, in any order.
		case prev.conflict:
			// Sticky: an equal-revision conflict stays unknown whatever the new
			// fact's start time says; only a newer revision may resolve it.
		case src.ObservationID == prev.src.ObservationID && observedAt.After(prev.observedAt):
			byID[c.CycleID] = nc // later attempt of a retry lineage in one snapshot
		case src.ObservationID == prev.src.ObservationID && observedAt.Before(prev.observedAt):
			// Earlier attempt in the same snapshot: already superseded.
		case sameRevisionDisagrees(prev, nc):
			contradictions = append(contradictions, Contradiction{
				DimensionA: "cycle", DimensionB: string(OwnerAutoOrch),
				Description: fmt.Sprintf("same-instant cycle %s observations disagree; failing closed", c.CycleID),
				ObservedAt:  prev.observedAt, Evidence: append([]EvidenceReference(nil), fo.Observation.Evidence...),
			})
			byID[c.CycleID] = normalizedCycle{cycleID: c.CycleID, observedAt: prev.observedAt, src: prev.src, conflict: true}
		}
	}
	for _, rec := range records {
		// Malformed or owner-misattributed raw records are ignored before their
		// cycles can claim an accepted outcome; the raw record itself stays in
		// the embedded sources for explainability.
		if rec.Observation.Validate() != nil {
			continue
		}
		m := rec.Observation.Dimensions.Mission
		if m == nil {
			continue
		}
		for _, c := range m.Cycles {
			add(c, rec)
		}
	}
	out := make([]normalizedCycle, 0, len(ids))
	for _, id := range ids {
		out = append(out, byID[id])
	}
	sort.SliceStable(out, func(i, j int) bool {
		ti, tj := cycleEvent(out[i]), cycleEvent(out[j])
		if !ti.Equal(tj) {
			return ti.After(tj)
		}
		// Deterministic tie: newest acceptance, then newest observed start, then
		// lexical cycle identity. Not a silently-picked positive state: the
		// content conflict path is handled above, ties here are simple order.
		if !out[i].acceptedAt.Equal(out[j].acceptedAt) {
			return out[i].acceptedAt.After(out[j].acceptedAt)
		}
		if !out[i].observedAt.Equal(out[j].observedAt) {
			return out[i].observedAt.After(out[j].observedAt)
		}
		return out[i].cycleID < out[j].cycleID
	})
	return out, contradictions
}

// computeStreak counts consecutive non-delivering cycles from the newest fact.
// A bounded/truncated history cannot claim a complete streak or zero: without a
// confirmed accepted break, truncation makes the count unknown. A conflicted
// cycle is unknown rather than confirmed non-delivery: it neither counts nor
// breaks the streak, it stops the walk and makes the count unknown, so a
// disagreement can never on its own manufacture a burn loop.
func computeStreak(cs []normalizedCycle, truncated bool) (streak int, ambiguous bool, count *int) {
	if len(cs) == 0 {
		return 0, false, nil
	}
	ended, unknown := false, false
	for _, nc := range cs {
		if nc.conflict {
			unknown = true
			break
		}
		if nc.accepted {
			ended = true
			break
		}
		streak++
	}
	if unknown || (!ended && truncated) {
		return streak, true, nil
	}
	return streak, false, &streak
}

// latestAccepted returns the newest explicitly accepted value-bearing cycle.
func latestAccepted(cs []normalizedCycle) *DeliveredValue {
	for _, nc := range cs {
		if nc.accepted && !nc.conflict {
			return &DeliveredValue{Value: nc.value, OutcomeID: nc.cycleID, AcceptedAt: nc.acceptedAt,
				Sources: append([]SourceRef(nil), nc.src)}
		}
	}
	return nil
}

// deliverySources cites the cycles that produced output without accepted value.
func deliverySources(cs []normalizedCycle) []SourceRef {
	var out []SourceRef
	for _, nc := range cs {
		if nc.delivered && !nc.accepted && !nc.conflict {
			out = append(out, nc.src)
		}
	}
	return out
}

func repairLineage(cs []normalizedCycle) []normalizedCycle {
	out := make([]normalizedCycle, 0, len(cs))
	for _, nc := range cs {
		if nc.repairOf != "" && nc.repairOf != nc.cycleID {
			out = append(out, nc)
		}
	}
	return out
}

func repairSources(cs []normalizedCycle) []SourceRef {
	var out []SourceRef
	for _, nc := range cs {
		if nc.repairOf != "" && nc.repairOf != nc.cycleID {
			out = append(out, nc.src)
		}
	}
	return out
}
