package operations

import (
	"crypto/sha256"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"strings"
	"time"
)

const (
	// IngestEnvelopeSchemaVersion is the closed schema for the immutable
	// ingestion envelope. It is versioned separately from the Sprint 1
	// observation contract it carries.
	IngestEnvelopeSchemaVersion = "ai-employee-observation-envelope/1.0"
	// MaxEnvelopeBytes bounds a single ingestion request body.
	MaxEnvelopeBytes = 256 << 10
)

// ObservationEnvelope is the versioned, immutable ingestion wrapper. It carries
// only the producer-asserted observation identity and the Sprint 1
// SourceObservation. Authentic producer identity, owner grant, receipt time and
// content digest are derived server-side and are deliberately absent from the
// decoded shape, so a forged producer or digest claim is rejected as an unknown
// field by the strict decoder.
type ObservationEnvelope struct {
	SchemaVersion string            `json:"schema_version"`
	ObservationID string            `json:"observation_id"`
	Observation   SourceObservation `json:"observation"`
}

// DecodeObservationEnvelope strictly decodes and validates one ingestion
// envelope. It reuses the Sprint 1 control/unknown-field safety, rejects extra
// JSON documents, requires a valid owner/employee/dimension shape, and rejects
// an observed_at in the future relative to the receipt instant.
func DecodeObservationEnvelope(raw []byte, now time.Time) (ObservationEnvelope, error) {
	if len(raw) > MaxEnvelopeBytes {
		return ObservationEnvelope{}, fmt.Errorf("observation envelope exceeds %d bytes", MaxEnvelopeBytes)
	}
	var env ObservationEnvelope
	if err := decodeStrict(raw, &env); err != nil {
		return ObservationEnvelope{}, err
	}
	if err := env.validate(now); err != nil {
		return ObservationEnvelope{}, err
	}
	return env, nil
}

// Validate re-runs the full envelope validation at the public service seam, so
// an embedder that constructs an ObservationEnvelope directly cannot bypass
// the HTTP strict-decoding path. It is the same validation the strict decoder
// applies.
func (e ObservationEnvelope) Validate(now time.Time) error { return e.validate(now) }

func (e ObservationEnvelope) validate(now time.Time) error {
	a := &acc{}
	a.bad(e.SchemaVersion != IngestEnvelopeSchemaVersion, "unsupported or missing envelope schema_version: %q", e.SchemaVersion).
		then(func() error { return validateObservationID(e.ObservationID) }).
		then(e.Observation.Validate)
	if a.err != nil {
		return a.err
	}
	if now.IsZero() {
		now = time.Now().UTC()
	}
	if e.Observation.ObservedAt.After(now) {
		return fmt.Errorf("observed_at %s is in the future relative to receipt %s",
			e.Observation.ObservedAt.Format(time.RFC3339Nano), now.Format(time.RFC3339Nano))
	}
	return nil
}

func validateObservationID(id string) error {
	id = strings.TrimSpace(id)
	if id == "" {
		return errors.New("observation_id must be non-empty")
	}
	if len(id) > 128 {
		return errors.New("observation_id exceeds 128 bytes")
	}
	if !identifierPattern.MatchString(id) {
		return fmt.Errorf("malformed observation_id: %s", id)
	}
	return nil
}

// CanonicalJSON returns the deterministic re-encoding of the validated
// envelope. The server hashes this representation, never the raw request body,
// so whitespace or key-order changes do not alter record identity.
func (e ObservationEnvelope) CanonicalJSON() ([]byte, error) {
	return json.Marshal(e)
}

// SHA256 returns the lowercase hex SHA-256 of the canonical envelope JSON.
func (e ObservationEnvelope) SHA256() (string, error) {
	canonical, err := e.CanonicalJSON()
	if err != nil {
		return "", err
	}
	sum := sha256.Sum256(canonical)
	return hex.EncodeToString(sum[:]), nil
}

// IsValidSourceOwner reports whether value is a member of the closed native
// owner vocabulary. It lets the authentication layer validate a configured
// owner grant without duplicating the vocabulary.
func IsValidSourceOwner(value string) bool { return isValidSourceOwner(SourceOwner(value)) }

// IsBoardOwner reports whether value names the Board-native owner, which is
// reserved for the Board itself and may never be granted to an external
// producer.
func IsBoardOwner(value string) bool { return SourceOwner(value) == OwnerBoard }

// IngestOwnerGranted reports whether owner is exactly the single owner granted
// to a producer credential. The grant is one owner per credential (ADR-001);
// it is exact and case-sensitive, and an empty grant grants nothing.
func IngestOwnerGranted(granted string, owner SourceOwner) bool {
	return granted != "" && granted == string(owner)
}

// ValidateEmployeeID exposes the Sprint 1 employee-id validation to the
// authentication and HTTP layers.
func ValidateEmployeeID(id string) error { return validateEmployeeID(id) }
