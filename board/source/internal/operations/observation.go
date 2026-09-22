package operations

import (
	"errors"
	"fmt"
	"time"
)

type SourceObservation struct {
	SchemaVersion    string              `json:"schema_version"`
	EmployeeID       string              `json:"employee_id"`
	Owner            SourceOwner         `json:"owner"`
	ObservedAt       time.Time           `json:"observed_at"`
	Freshness        Freshness           `json:"freshness"`
	CollectionResult CollectionResult    `json:"collection_result"`
	Dimensions       SourceDimensions    `json:"dimensions,omitempty"`
	Error            *SourceError        `json:"error,omitempty"`
	Evidence         []EvidenceReference `json:"evidence,omitempty"`
}

func (o SourceObservation) Validate() error {
	a := &acc{}
	a.bad(o.SchemaVersion != ContractSchemaVersion, "unsupported or missing schema_version: %q", o.SchemaVersion).
		then(func() error { return validateEmployeeID(o.EmployeeID) }).
		bad(!isValidSourceOwner(o.Owner), "observation owner is not a native source owner (disagreement must be separate observations): %q", o.Owner).
		bad(o.ObservedAt.IsZero(), "missing or zero observed_at").
		bad(!isValidFreshness(o.Freshness), "unsupported freshness: %q", o.Freshness).
		bad(!isValidCollectionResult(o.CollectionResult), "unsupported collection_result: %q", o.CollectionResult).
		then(o.validateDimensionOwnership).
		then(o.validateResultConsistency).
		then(o.validateError).
		then(o.Dimensions.validate).
		then(func() error { return validateEvidenceList("evidence", o.Evidence) })
	return a.err
}
func (o SourceObservation) validateError() error {
	if o.Error == nil {
		return nil
	}
	if err := o.Error.Validate(); err != nil {
		return err
	}
	if o.Error.Owner != o.Owner {
		return fmt.Errorf("source error attribution mismatch: error owner %q != observation owner %q", o.Error.Owner, o.Owner)
	}
	return nil
}

// validateDimensionOwnership enforces that only the native owner may emit its
func (o SourceObservation) validateDimensionOwnership() error {
	for _, od := range o.Dimensions.present() {
		if od.owner != o.Owner {
			return fmt.Errorf("owner %q may not emit the %s dimension (only %q may)", o.Owner, od.name, od.owner)
		}
	}
	return nil
}

// coherent so failure never masquerades as healthy data and a non-success
func (o SourceObservation) validateResultConsistency() error {
	dims := len(o.Dimensions.present())
	switch o.CollectionResult {
	case CollectionResultSuccess:
		if o.Error != nil {
			return errors.New("collection_result success must not carry an error")
		}
		if o.Freshness == FreshnessUnknown {
			return errors.New("collection_result success must not have unknown freshness")
		}
		if dims == 0 && len(o.Evidence) == 0 { // success must actually assert something
			return errors.New("collection_result success must carry owner-authorized dimensions or evidence")
		}
	case CollectionResultPartial:
		if o.Error == nil {
			return errors.New("collection_result partial must carry an error describing the partial failure")
		}
	case CollectionResultFailed, CollectionResultUnavailable, CollectionResultUnknown:
		if o.Freshness != FreshnessUnknown {
			return fmt.Errorf("collection_result %q must have unknown freshness (no fresh data exists)", o.CollectionResult)
		}
		if dims != 0 {
			return fmt.Errorf("collection_result %q must not carry positive dimension facts", o.CollectionResult)
		}
		if o.CollectionResult != CollectionResultUnknown && o.Error == nil {
			return fmt.Errorf("collection_result %q must carry an error", o.CollectionResult)
		}
	}
	return nil
}
