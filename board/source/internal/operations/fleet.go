package operations

import (
	"sort"
	"strings"
	"time"
)

const (
	// OperationsFleetSchemaVersion versions the read model served to the UI.
	OperationsFleetSchemaVersion = "ai-employee-operations-fleet/1.0"
	// DefaultHistoryLimit bounds how many recent distinct instants are kept per
	// owner. The store applies the same bound in SQL so a read never
	// materializes the whole history before trimming.
	DefaultHistoryLimit = 5
	// MaxFleetEmployees bounds how many employees one fleet read materializes.
	// EmployeesTruncated makes the finite bound explicit rather than silently
	// dropping employees beyond it.
	MaxFleetEmployees = 200
	// MaxHistoryLimit is the hard per-owner row cap for a bounded read,
	// independent of how many records share an observation instant.
	MaxHistoryLimit = 50
	// FleetValidFor bounds the generated/valid-until window of a fleet read.
	FleetValidFor = 5 * time.Minute

	// Owner status vocabulary. Absence is never reported as fresh/stopped.
	OwnerStatusFresh       = "fresh"
	OwnerStatusStale       = "stale"
	OwnerStatusUnknown     = "unknown"
	OwnerStatusUnavailable = "unavailable"
	OwnerStatusMissing     = "missing"
)

// allSourceOwners is the deterministic owner order used in every read model, so
// missing owners are explicitly present rather than silently absent.
var allSourceOwners = []SourceOwner{OwnerFactory, OwnerAutoOrch, OwnerAgentOrch, OwnerSchedule, OwnerProcess, OwnerBoard, OwnerRouter}

const (
	maxAgeFactory           = 24 * time.Hour
	maxAgeAutoOrch          = 3 * time.Hour
	maxAgeAgentOrchActive   = 2 * time.Minute
	maxAgeAgentOrchTerminal = 24 * time.Hour
	maxAgeSchedule          = 15 * time.Minute
	maxAgeProcess           = 2 * time.Minute
	maxAgeBoard             = 5 * time.Minute
	maxAgeRouter            = 15 * time.Minute
)

// terminalRunStatuses is the closed set used to pick the Agent-Orch freshness
// limit. Anything not recognized is treated as active (the tighter 2 minute
// limit), so an unknown terminal label cannot claim a longer fresh window.
var terminalRunStatuses = map[string]bool{
	"passed": true, "success": true, "succeeded": true, "completed": true, "complete": true,
	"failed": true, "failure": true, "error": true, "cancelled": true, "canceled": true,
	"timeout": true, "timed_out": true, "terminal": true,
}

func isTerminalRunStatus(status string) bool {
	return terminalRunStatuses[strings.ToLower(strings.TrimSpace(status))]
}

// MaxAgeForObservation returns the owner-specific ADR/Sprint 0 v1 freshness
// limit. Agent-Orch active runs use 2 minutes while terminal runs use 24 hours.
func MaxAgeForObservation(o SourceObservation) time.Duration {
	runStatus := ""
	if o.Dimensions.GovernedRun != nil {
		runStatus = o.Dimensions.GovernedRun.RunStatus
	}
	return MaxAgeForOwner(o.Owner, runStatus)
}

// MaxAgeForOwner is the same authoritative limit expressed over the two original
// fields that select it. A persisted artifact carries those fields, so its
// freshness bound can be recomputed from source truth rather than believed.
func MaxAgeForOwner(owner SourceOwner, runStatus string) time.Duration {
	switch owner {
	case OwnerFactory:
		return maxAgeFactory
	case OwnerAutoOrch:
		return maxAgeAutoOrch
	case OwnerAgentOrch:
		if isTerminalRunStatus(runStatus) {
			return maxAgeAgentOrchTerminal
		}
		return maxAgeAgentOrchActive
	case OwnerSchedule:
		return maxAgeSchedule
	case OwnerProcess:
		return maxAgeProcess
	case OwnerBoard:
		return maxAgeBoard
	case OwnerRouter:
		return maxAgeRouter
	default:
		return maxAgeBoard
	}
}

// ComputedFreshness derives freshness from observed_at and the owner limit. The
// producer-supplied Freshness field is never trusted for current reads, and a
// failed/unavailable/unknown collection is never fresh.
func ComputedFreshness(o SourceObservation, now time.Time) Freshness {
	if o.ObservedAt.IsZero() {
		return FreshnessUnknown
	}
	switch o.CollectionResult {
	case CollectionResultSuccess, CollectionResultPartial:
	default:
		return FreshnessUnknown
	}
	if o.ObservedAt.After(now) {
		return FreshnessUnknown
	}
	if now.Sub(o.ObservedAt) <= MaxAgeForObservation(o) {
		return FreshnessFresh
	}
	return FreshnessStale
}

// StoredObservation is one persisted, immutable source record as returned by
// the store.
type StoredObservation struct {
	Owner             SourceOwner
	EmployeeID        string
	ObservationID     string
	ProducerPrincipal string
	SchemaVersion     string
	ObservedAt        time.Time
	ReceivedAt        time.Time
	ContentSHA256     string
	Envelope          ObservationEnvelope
	// WindowTruncated is store-supplied read metadata: true when this owner has
	// older history than the bounded query window returned. It is not part of
	// the immutable record.
	WindowTruncated bool
	// TieTruncated is store-supplied read metadata: true when the hard row cap
	// cut a same-instant tie group, so records sharing the last included
	// observed_at were dropped.
	TieTruncated bool
}

// FleetObservation is the read projection of one stored record.
type FleetObservation struct {
	ObservationID     string            `json:"observation_id"`
	ProducerPrincipal string            `json:"producer_principal"`
	ReceivedAt        time.Time         `json:"received_at"`
	MaxAgeSeconds     int64             `json:"max_age_seconds"`
	ComputedFreshness Freshness         `json:"computed_freshness"`
	Observation       SourceObservation `json:"observation"`
}

// FleetOwner groups the records for one owner of one employee. Latest and
// LastKnownSuccess are separated so a newer outage never erases the last
// known success, and SameTimeConflict preserves contradictory observations.
type FleetOwner struct {
	Owner                  SourceOwner        `json:"owner"`
	Status                 string             `json:"status"`
	ComputedFreshness      Freshness          `json:"computed_freshness"`
	Latest                 *FleetObservation  `json:"latest,omitempty"`
	LastKnownSuccess       *FleetObservation  `json:"last_known_success,omitempty"`
	Recent                 []FleetObservation `json:"recent,omitempty"`
	SameTimeConflict       bool               `json:"same_time_conflict"`
	ConflictObservationIDs []string           `json:"conflict_observation_ids,omitempty"`
	// Truncated reports that the owner has history older than the returned
	// bounded window, so Recent is not the full record set.
	Truncated bool `json:"truncated,omitempty"`
	// TiedRecordsTruncated reports that the finite row cap cut a same-time tie
	// group, so not every record sharing the cutoff instant is present.
	TiedRecordsTruncated bool `json:"tied_records_truncated,omitempty"`
}

// FleetEmployee is the explicit owner-by-owner view for one employee.
type FleetEmployee struct {
	EmployeeID string       `json:"employee_id"`
	Owners     []FleetOwner `json:"owners"`
}

// OperationsFleet is the bounded, versioned fleet read model.
type OperationsFleet struct {
	SchemaVersion      string          `json:"schema_version"`
	GeneratedAt        time.Time       `json:"generated_at"`
	ValidUntil         time.Time       `json:"valid_until"`
	Employees          []FleetEmployee `json:"employees"`
	TotalEmployees     int             `json:"total_employees"`
	EmployeesTruncated bool            `json:"employees_truncated"`
}

// BuildFleet assembles the read model from stored records. Every known owner is
// present for every employee; owners without records are explicitly missing.
func BuildFleet(records []StoredObservation, now time.Time, historyLimit int) OperationsFleet {
	if now.IsZero() {
		now = time.Now().UTC()
	}
	byEmployee := map[string][]StoredObservation{}
	for _, record := range records {
		byEmployee[record.EmployeeID] = append(byEmployee[record.EmployeeID], record)
	}
	ids := make([]string, 0, len(byEmployee))
	for id := range byEmployee {
		ids = append(ids, id)
	}
	sort.Strings(ids)
	employees := make([]FleetEmployee, 0, len(ids))
	for _, id := range ids {
		employees = append(employees, BuildEmployeeFleet(id, byEmployee[id], now, historyLimit))
	}
	return OperationsFleet{
		SchemaVersion:  OperationsFleetSchemaVersion,
		GeneratedAt:    now,
		ValidUntil:     now.Add(FleetValidFor),
		Employees:      employees,
		TotalEmployees: len(employees),
	}
}

// BuildEmployeeFleet assembles one employee's owner-by-owner view. It always
// returns a view, even with no records, so a missing employee is explicit
// unknown rather than absent.
func BuildEmployeeFleet(employeeID string, records []StoredObservation, now time.Time, historyLimit int) FleetEmployee {
	if now.IsZero() {
		now = time.Now().UTC()
	}
	if historyLimit <= 0 {
		historyLimit = DefaultHistoryLimit
	}
	byOwner := map[SourceOwner][]StoredObservation{}
	for _, record := range records {
		if record.EmployeeID != employeeID {
			continue
		}
		byOwner[record.Owner] = append(byOwner[record.Owner], record)
	}
	owners := make([]FleetOwner, 0, len(allSourceOwners))
	for _, owner := range allSourceOwners {
		owners = append(owners, buildOwnerView(owner, byOwner[owner], now, historyLimit))
	}
	return FleetEmployee{EmployeeID: employeeID, Owners: owners}
}
