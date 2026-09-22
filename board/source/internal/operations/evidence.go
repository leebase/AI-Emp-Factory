package operations

import (
	"errors"
	"fmt"
	"regexp"
	"strings"
	"time"
)

var (
	opaqueTokenPattern = regexp.MustCompile(`^[A-Za-z0-9][A-Za-z0-9._-]{0,254}$`)
	sha256Pattern      = regexp.MustCompile(`^[a-f0-9]{64}$`)
)

type EvidenceReference struct {
	Owner              SourceOwner        `json:"owner"`
	URI                string             `json:"uri"`
	VerificationStatus VerificationStatus `json:"verification_status"`
	Digest             string             `json:"digest,omitempty"`
	ObservedAt         time.Time          `json:"observed_at"`
}

func (e EvidenceReference) Validate() error {
	a := &acc{}
	a.bad(!isValidSourceOwner(e.Owner), "evidence owner is not a native source owner: %q", e.Owner).
		bad(e.ObservedAt.IsZero(), "evidence observed_at must be non-zero").
		then(func() error { return validateEvidenceURI(e.URI) }).
		bad(!isValidVerificationStatus(e.VerificationStatus), "evidence verification_status must be an explicit contract state (omission and empty are not permitted): %q", e.VerificationStatus).
		bad(e.Digest != "" && !sha256Pattern.MatchString(e.Digest), "evidence digest must be a lowercase sha256 hex string: %q", e.Digest)
	return a.err
}
func validateEvidenceURI(uri string) error {
	if uri == "" {
		return errors.New("evidence URI must be non-empty")
	}
	if uri != strings.TrimSpace(uri) {
		return fmt.Errorf("evidence URI has surrounding whitespace: %q", uri)
	}
	if strings.ContainsRune(uri, 0) || strings.ContainsAny(uri, "/\\ \t\r\n") {
		return fmt.Errorf("evidence URI contains illegal characters: %q", uri)
	}
	if strings.Contains(uri, "..") {
		return fmt.Errorf("evidence URI contains traversal: %q", uri)
	}
	idx := strings.IndexByte(uri, ':')
	if idx <= 0 || idx == len(uri)-1 {
		return fmt.Errorf("evidence URI must be an opaque <scheme>:<token> reference: %q", uri)
	}
	scheme, token := uri[:idx], uri[idx+1:]
	switch scheme {
	case "sha256":
		if !sha256Pattern.MatchString(token) {
			return fmt.Errorf("sha256 evidence reference must be 64 hex chars: %q", uri)
		}
	case "artifact", "run", "cycle", "board":
		if !opaqueTokenPattern.MatchString(token) {
			return fmt.Errorf("evidence reference token is not a valid opaque identifier: %q", uri)
		}
	default:
		return fmt.Errorf("evidence URI scheme %q is not an allowed opaque owner/Board reference (arbitrary paths and URLs are rejected)", scheme)
	}
	return nil
}
func validateEvidenceList(field string, refs []EvidenceReference) error {
	return validateList(field, len(refs), func(i int) error { return refs[i].Validate() })
}

type SourceError struct {
	Owner      SourceOwner `json:"owner"`
	Code       string      `json:"code"`
	Message    string      `json:"message"`
	ObservedAt time.Time   `json:"observed_at"`
}

func (se SourceError) Validate() error {
	a := &acc{}
	a.bad(!isValidSourceOwner(se.Owner), "source_error owner is not a native source owner: %q", se.Owner).
		bad(strings.TrimSpace(se.Code) == "", "source_error code must be non-empty").
		bad(strings.TrimSpace(se.Message) == "", "source_error message must be non-empty").
		bad(se.ObservedAt.IsZero(), "source_error observed_at must be non-zero")
	return a.err
}
func validateSourceErrorList(field string, errs []SourceError) error {
	return validateList(field, len(errs), func(i int) error { return errs[i].Validate() })
}
