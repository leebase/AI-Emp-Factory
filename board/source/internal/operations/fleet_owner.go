package operations

import (
	"sort"
	"time"
)

// buildOwnerView assembles one owner's bounded view for one employee. Recent is
// a hard-capped, newest-first record window; Latest and LastKnownSuccess are
// kept separate so a newer outage never erases the last known success.
func buildOwnerView(owner SourceOwner, records []StoredObservation, now time.Time, historyLimit int) FleetOwner {
	sort.SliceStable(records, func(i, j int) bool {
		if !records[i].ObservedAt.Equal(records[j].ObservedAt) {
			return records[i].ObservedAt.After(records[j].ObservedAt)
		}
		return records[i].ObservationID < records[j].ObservationID
	})
	view := FleetOwner{Owner: owner, Status: OwnerStatusMissing, ComputedFreshness: FreshnessUnknown}
	if len(records) == 0 {
		return view
	}
	recent := recentWindow(records, historyLimit)
	view.Recent = make([]FleetObservation, 0, len(recent))
	for _, record := range recent {
		view.Recent = append(view.Recent, toFleetObservation(record, now))
	}
	latest := records[0]
	latestView := toFleetObservation(latest, now)
	view.Latest = &latestView
	view.ComputedFreshness = ComputedFreshness(latest.Envelope.Observation, now)
	view.Status = ownerStatus(latest, view.ComputedFreshness)
	// Last-known success is searched over every record the store returned,
	// which always includes the most recent success even when it is older than
	// the bounded Recent window after many outages.
	for i := range records {
		if records[i].Envelope.Observation.CollectionResult == CollectionResultSuccess {
			lastSuccess := toFleetObservation(records[i], now)
			view.LastKnownSuccess = &lastSuccess
			break
		}
	}
	view.Truncated = len(recent) < len(records) || windowTruncated(records)
	view.TiedRecordsTruncated = cutoffTieTruncated(records, len(recent)) || tieTruncated(records)
	// Conflicts are evaluated only over the returned window so a historical
	// contradiction outside the window cannot flag current state.
	view.SameTimeConflict, view.ConflictObservationIDs = sameTimeConflicts(view.Recent)
	return view
}

// windowTruncated reports whether the store query dropped older history for
// this owner beyond the requested window.
func windowTruncated(records []StoredObservation) bool {
	for _, record := range records {
		if record.WindowTruncated {
			return true
		}
	}
	return false
}

// tieTruncated reports whether the store flagged that the hard cap cut a
// same-instant tie group.
func tieTruncated(records []StoredObservation) bool {
	for _, record := range records {
		if record.TieTruncated {
			return true
		}
	}
	return false
}

// cutoffTieTruncated reports whether the first record dropped from the window
// shares an observed_at with the last retained record. It catches direct
// callers that pass the full record slice rather than a store-bounded window.
func cutoffTieTruncated(records []StoredObservation, kept int) bool {
	if kept <= 0 || kept >= len(records) {
		return false
	}
	return records[kept].ObservedAt.Equal(records[kept-1].ObservedAt)
}

func toFleetObservation(record StoredObservation, now time.Time) FleetObservation {
	return FleetObservation{
		ObservationID:     record.ObservationID,
		ProducerPrincipal: record.ProducerPrincipal,
		ReceivedAt:        record.ReceivedAt,
		MaxAgeSeconds:     int64(MaxAgeForObservation(record.Envelope.Observation) / time.Second),
		ComputedFreshness: ComputedFreshness(record.Envelope.Observation, now),
		Observation:       record.Envelope.Observation,
	}
}

func ownerStatus(latest StoredObservation, freshness Freshness) string {
	switch latest.Envelope.Observation.CollectionResult {
	case CollectionResultFailed, CollectionResultUnavailable:
		return OwnerStatusUnavailable
	case CollectionResultUnknown:
		return OwnerStatusUnknown
	}
	switch freshness {
	case FreshnessFresh:
		return OwnerStatusFresh
	case FreshnessStale:
		return OwnerStatusStale
	default:
		return OwnerStatusUnknown
	}
}
