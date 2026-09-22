package staff

import (
	"encoding/json"
	"fmt"
	"os"
	"sort"
	"strings"
)

// Relationship is one recorded edge between two employees.
//
// An edge exists only because a named source file recorded it. Nothing here
// matches on names, infers a reporting line from two employees sharing a
// discipline, or upgrades a recorded configuration into live execution: Means
// states what the edge does NOT establish, and travels with it into the UI.
type Relationship struct {
	Direction         string `json:"direction"`
	Kind              string `json:"kind"`
	CounterpartID     string `json:"counterpart_id"`
	CounterpartName   string `json:"counterpart_name,omitempty"`
	CounterpartInView bool   `json:"counterpart_in_view"`
	Label             string `json:"label"`
	Means             string `json:"means"`
	SourceRef         string `json:"source_ref"`
	RecordedOn        string `json:"recorded_on,omitempty"`
	VerifiedBy        string `json:"verified_by,omitempty"`
}

type edge struct {
	FromEmployeeID string `json:"from_employee_id"`
	ToEmployeeID   string `json:"to_employee_id"`
	Kind           string `json:"kind"`
	Label          string `json:"label"`
	Means          string `json:"means"`
	SourceRef      string `json:"source_ref"`
	RecordedOn     string `json:"recorded_on"`
	VerifiedBy     string `json:"verified_by"`
}

// Book is the operator-curated set of recorded relationships. It is a separate
// explicit file rather than something derived at read time, because an edge is
// a claim about the organisation and must be attributable to a human who
// checked a source, not to a string comparison.
type Book struct {
	edges []edge
	notes []string
}

// LoadBook reads the relationship file. An absent path is not an error: it
// means no relationships are configured, and the view says exactly that.
func LoadBook(path string) (*Book, error) {
	path = strings.TrimSpace(path)
	if path == "" {
		return &Book{notes: []string{"No relationship file is configured, so no relationships are shown. This is not a statement that none exist."}}, nil
	}
	raw, err := os.ReadFile(path)
	if err != nil {
		return &Book{notes: []string{"The configured relationship file could not be read, so no relationships are shown."}}, fmt.Errorf("staff relationships: %w", err)
	}
	var body struct {
		Relationships []edge `json:"relationships"`
	}
	decoder := json.NewDecoder(strings.NewReader(string(raw)))
	decoder.DisallowUnknownFields()
	if err := decoder.Decode(&body); err != nil {
		return &Book{notes: []string{"The configured relationship file could not be parsed, so no relationships are shown."}}, fmt.Errorf("staff relationships: %w", err)
	}
	book := &Book{}
	for index, item := range body.Relationships {
		if strings.TrimSpace(item.FromEmployeeID) == "" || strings.TrimSpace(item.ToEmployeeID) == "" || strings.TrimSpace(item.SourceRef) == "" {
			// An edge without both endpoints and a source is not evidence of
			// anything, so it is dropped and disclosed rather than guessed at.
			book.notes = append(book.notes, fmt.Sprintf("Relationship entry %d was ignored: it lacks both employee ids or a source reference.", index+1))
			continue
		}
		book.edges = append(book.edges, item)
	}
	return book, nil
}

// Attach hangs each recorded edge on both of its endpoints that are present in
// this read. An endpoint that is not in the read is still shown, marked as not
// present, so a missing counterpart never silently deletes a recorded edge and
// never invents a substitute for it.
func (b *Book) Attach(employees []Employee, present map[string]string) []string {
	notes := append([]string{}, b.notes...)
	byID := map[string]*Employee{}
	for index := range employees {
		byID[employees[index].EmployeeID] = &employees[index]
	}
	for _, item := range b.edges {
		from, hasFrom := byID[item.FromEmployeeID]
		to, hasTo := byID[item.ToEmployeeID]
		if !hasFrom && !hasTo {
			notes = append(notes, fmt.Sprintf("A recorded relationship between %q and %q is configured, but neither employee is in this read.", item.FromEmployeeID, item.ToEmployeeID))
			continue
		}
		if hasFrom {
			addRelationship(from, relationshipFor(item, "to", item.ToEmployeeID, present))
		}
		if hasTo {
			addRelationship(to, relationshipFor(item, "from", item.FromEmployeeID, present))
		}
	}
	for index := range employees {
		sort.SliceStable(employees[index].Relationships, func(i, j int) bool {
			return employees[index].Relationships[i].CounterpartID < employees[index].Relationships[j].CounterpartID
		})
	}
	return notes
}

func relationshipFor(item edge, direction, counterpartID string, present map[string]string) Relationship {
	name, inView := present[counterpartID]
	return Relationship{
		Direction:         direction,
		Kind:              clip(item.Kind),
		CounterpartID:     clip(counterpartID),
		CounterpartName:   clip(name),
		CounterpartInView: inView,
		Label:             clip(item.Label),
		Means:             clip(item.Means),
		SourceRef:         clip(item.SourceRef),
		RecordedOn:        clip(item.RecordedOn),
		VerifiedBy:        clip(item.VerifiedBy),
	}
}

func addRelationship(employee *Employee, relationship Relationship) {
	if len(employee.Relationships) >= maxRelationships {
		return
	}
	employee.Relationships = append(employee.Relationships, relationship)
}
