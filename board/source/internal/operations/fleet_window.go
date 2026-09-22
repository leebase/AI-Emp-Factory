package operations

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
)

// ObservationContentSHA256 returns the lowercase hex SHA-256 of the canonical
// SourceObservation JSON. It deliberately excludes the envelope observation id
// and envelope schema version, so two records that assert identical facts at
// the same instant are not treated as contradictory merely because they carry
// different observation ids. The immutable envelope digest (ContentSHA256 on
// StoredObservation) remains the idempotency/conflict key and is tracked
// separately.
func (o SourceObservation) ObservationContentSHA256() (string, error) {
	canonical, err := json.Marshal(o)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(canonical)
	return hex.EncodeToString(sum[:]), nil
}

// recentWindow returns at most historyLimit records in deterministic
// newest-first order. records must already be sorted newest-first. The cap is a
// hard row count, not a distinct-instant count, so arbitrarily many records
// sharing one observed_at can never make the returned window unbounded; when
// that cap cuts a same-time tie group the caller records it as truncation.
func recentWindow(records []StoredObservation, historyLimit int) []StoredObservation {
	if historyLimit <= 0 {
		historyLimit = DefaultHistoryLimit
	}
	if len(records) <= historyLimit {
		return records
	}
	return records[:historyLimit]
}

// sameTimeConflicts reports observation ids that share an observed_at with a
// Record in recent that carries different canonical content. It is evaluated
// only over the returned bounded window, so a historical contradiction that is
// no longer in the window never flags the current state, while a contradiction
// that is still in the window remains visible.
func sameTimeConflicts(recent []FleetObservation) (bool, []string) {
	digestsByInstant := map[int64]map[string]bool{}
	for _, item := range recent {
		digest, err := item.Observation.ObservationContentSHA256()
		if err != nil {
			continue
		}
		instant := item.Observation.ObservedAt.UnixNano()
		if digestsByInstant[instant] == nil {
			digestsByInstant[instant] = map[string]bool{}
		}
		digestsByInstant[instant][digest] = true
	}
	var ids []string
	for _, item := range recent {
		if len(digestsByInstant[item.Observation.ObservedAt.UnixNano()]) > 1 {
			ids = append(ids, item.ObservationID)
		}
	}
	if len(ids) == 0 {
		return false, nil
	}
	return true, ids
}
