package operations

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"strings"
	"time"
)

// Identity and semantic validation for the quick shared state artifact.
//
// The self-digest proves only that the bytes are internally consistent. It is
// not producer authentication and it confers no authority: every bound the
// artifact claims is recomputed here from its original source timestamps and
// the authoritative owner limits before the artifact is trusted.

// ComputeSnapshotID derives the content digest with snapshot_id blanked, so the
// published id can be recomputed and compared by any reader or loader.
func ComputeSnapshotID(state ActiveState) (string, error) {
	state.SnapshotID = ""
	encoded, err := json.Marshal(state)
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(encoded)
	return snapshotIDPrefix + hex.EncodeToString(sum[:]), nil
}

// CanonicalJSON is the exact published byte form of the artifact.
func (s ActiveState) CanonicalJSON() ([]byte, error) { return json.Marshal(s) }

// Validate checks the artifact envelope and its self-describing digest. It is
// used before trusting any artifact loaded from disk: content that does not
// re-derive its own snapshot id is rejected rather than served.
func (s ActiveState) Validate() error {
	if s.SchemaVersion != ActiveStateSchemaVersion {
		return fmt.Errorf("unsupported active state schema_version: %q", s.SchemaVersion)
	}
	if s.ManagerialSchemaVersion != ManagerialSchemaVersion {
		return fmt.Errorf("unsupported managerial_schema_version: %q", s.ManagerialSchemaVersion)
	}
	if strings.TrimSpace(s.RuleVersion) == "" {
		return fmt.Errorf("missing rule_version")
	}
	if !strings.HasPrefix(s.SnapshotID, snapshotIDPrefix) {
		return fmt.Errorf("missing or malformed snapshot_id: %q", s.SnapshotID)
	}
	if s.GeneratedAt.IsZero() || s.ValidUntil.IsZero() {
		return fmt.Errorf("missing generated_at or valid_until")
	}
	if s.ValidUntil.Before(s.GeneratedAt) {
		return fmt.Errorf("valid_until must not be before generated_at")
	}
	if len(s.Cards) != s.TotalEmployees && !s.EmployeesTruncated {
		return fmt.Errorf("card count %d does not match total_employees %d", len(s.Cards), s.TotalEmployees)
	}
	for i, card := range s.Cards {
		if strings.TrimSpace(card.EmployeeID) == "" {
			return fmt.Errorf("cards[%d] has no employee_id", i)
		}
		if !isValidPlacement(card.Placement) {
			return fmt.Errorf("cards[%d] has unsupported placement %q", i, card.Placement)
		}
		if !isValidCondition(card.Condition) {
			return fmt.Errorf("cards[%d] has unsupported condition %q", i, card.Condition)
		}
		for owner, freshness := range card.SourceFreshness {
			if !isValidSourceOwner(owner) {
				return fmt.Errorf("cards[%d] has unsupported source owner %q", i, owner)
			}
			if !isValidFreshness(freshness) {
				return fmt.Errorf("cards[%d] owner %q has unsupported freshness %q", i, owner, freshness)
			}
		}
	}
	if err := s.validateSources(); err != nil {
		return err
	}
	// A valid digest proves the bytes are internally consistent. It proves
	// nothing about authority, so the validity bound is recomputed from the
	// source timestamps and the owner limits rather than believed.
	if expected := RecomputeValidUntil(s); !s.ValidUntil.Equal(expected) {
		return fmt.Errorf("valid_until %s exceeds the bound its sources support (%s)",
			s.ValidUntil.UTC().Format(time.RFC3339Nano), expected.UTC().Format(time.RFC3339Nano))
	}
	expected, err := ComputeSnapshotID(s)
	if err != nil {
		return err
	}
	if expected != s.SnapshotID {
		return fmt.Errorf("snapshot_id %q does not match content digest %q", s.SnapshotID, expected)
	}
	return nil
}

// validateSources checks each source's vocabulary and its expiry consistency:
// a fresh source must carry exactly the expiry its owner rule allows, and a
// non-fresh source must not carry one at all.
func (s ActiveState) validateSources() error {
	for i, src := range s.Sources {
		if strings.TrimSpace(src.EmployeeID) == "" {
			return fmt.Errorf("sources[%d] has no employee_id", i)
		}
		if !isValidSourceOwner(src.Owner) {
			return fmt.Errorf("sources[%d] has unsupported owner %q", i, src.Owner)
		}
		if !isValidFreshness(src.ComputedFreshness) {
			return fmt.Errorf("sources[%d] has unsupported computed_freshness %q", i, src.ComputedFreshness)
		}
		switch src.Status {
		case OwnerStatusFresh, OwnerStatusStale, OwnerStatusUnknown, OwnerStatusUnavailable, OwnerStatusMissing:
		default:
			return fmt.Errorf("sources[%d] has unsupported status %q", i, src.Status)
		}
		expected := src.recomputedExpiry()
		switch {
		case expected == nil && src.ExpiresAt != nil:
			return fmt.Errorf("sources[%d] claims an expiry without a fresh observation", i)
		case expected != nil && src.ExpiresAt == nil:
			return fmt.Errorf("sources[%d] omits the expiry its fresh observation requires", i)
		case expected != nil && !src.ExpiresAt.Equal(*expected):
			return fmt.Errorf("sources[%d] expires_at %s does not match its owner rule (%s)", i,
				src.ExpiresAt.UTC().Format(time.RFC3339Nano), expected.UTC().Format(time.RFC3339Nano))
		}
	}
	return nil
}
