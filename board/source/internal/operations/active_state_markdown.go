package operations

import (
	"fmt"
	"strings"
	"time"
)

// Markdown renders the compact managerial view of this exact artifact value.
// It performs no collection and reads no other state, so the Markdown and the
// JSON published together always describe the same snapshot id and the same
// employee facts. Unknowns stay unknown: a nil metric renders "unknown", never
// zero, and a missing value renders "none recorded", never a success.
func (s ActiveState) Markdown() string {
	var b strings.Builder
	fmt.Fprintf(&b, "# Mission Control active state\n\n")
	fmt.Fprintf(&b, "- snapshot_id: `%s`\n", s.SnapshotID)
	fmt.Fprintf(&b, "- schema: `%s` managerial `%s` rules `%s`\n", s.SchemaVersion, s.ManagerialSchemaVersion, s.RuleVersion)
	fmt.Fprintf(&b, "- generated_at: %s\n", stamp(s.GeneratedAt))
	fmt.Fprintf(&b, "- valid_until: %s\n", stamp(s.ValidUntil))
	fmt.Fprintf(&b, "- employees: %d", s.TotalEmployees)
	if s.EmployeesTruncated {
		b.WriteString(" (truncated)")
	}
	b.WriteString("\n\n")
	s.writeNeedsLee(&b)
	s.writeCards(&b)
	s.writeFindings(&b)
	s.writeSources(&b)
	return b.String()
}

func (s ActiveState) writeNeedsLee(b *strings.Builder) {
	b.WriteString("## Needs Lee\n\n")
	if len(s.NeedsLee) == 0 {
		b.WriteString("None recorded.\n\n")
		return
	}
	for _, n := range s.NeedsLee {
		fmt.Fprintf(b, "- **%s** `%s` required=%t scope=%s owner=%s raised=%s — %s\n",
			n.EmployeeID, n.Gate.DecisionID, n.Gate.Required, n.Gate.Scope, n.Gate.Owner,
			stamp(n.Gate.RaisedAt), oneLine(n.Gate.Title))
		if reason := oneLine(n.Gate.Reason); reason != "" {
			fmt.Fprintf(b, "  - reason: %s\n", reason)
		}
		for _, ev := range n.Gate.Evidence {
			fmt.Fprintf(b, "  - evidence: `%s` (%s)\n", ev.URI, ev.VerificationStatus)
		}
	}
	b.WriteString("\n")
}

func (s ActiveState) writeCards(b *strings.Builder) {
	b.WriteString("## Employees\n\n")
	if len(s.Cards) == 0 {
		b.WriteString("No employee observations in this snapshot.\n\n")
		return
	}
	for _, c := range s.Cards {
		fmt.Fprintf(b, "### %s\n\n", c.EmployeeID)
		fmt.Fprintf(b, "- placement: **%s** (`%s` v%s) — %s\n", c.Placement, c.PlacementReason.RuleID, c.PlacementReason.RuleVersion, oneLine(c.PlacementReason.Message))
		fmt.Fprintf(b, "- condition: **%s** (`%s` v%s) — %s\n", c.Condition, c.ConditionReason.RuleID, c.ConditionReason.RuleVersion, oneLine(c.ConditionReason.Message))
		if c.LatestValue == nil {
			b.WriteString("- latest accepted value: none recorded\n")
		} else {
			fmt.Fprintf(b, "- latest accepted value: %s (outcome `%s`, accepted %s)\n",
				oneLine(c.LatestValue.Value), c.LatestValue.OutcomeID, stamp(c.LatestValue.AcceptedAt))
		}
		fmt.Fprintf(b, "- non-delivering cycles: %s", countText(c.NonDeliveryCount))
		if c.DeliveryTruncated {
			b.WriteString(" (window truncated)")
		}
		b.WriteString("\n")
		if c.NeedsLee == nil {
			b.WriteString("- needs Lee: no gate recorded\n")
		} else {
			fmt.Fprintf(b, "- needs Lee: required=%t `%s` %s\n", c.NeedsLee.Required, c.NeedsLee.DecisionID, oneLine(c.NeedsLee.Title))
		}
		for _, cd := range c.Contradictions {
			fmt.Fprintf(b, "- contradiction: %s vs %s — %s\n", cd.DimensionA, cd.DimensionB, oneLine(cd.Description))
		}
		fmt.Fprintf(b, "- source freshness: %s\n\n", freshnessLine(c.SourceFreshness))
	}
}

func (s ActiveState) writeFindings(b *strings.Builder) {
	b.WriteString("## Challenge findings\n\n")
	if len(s.Findings) == 0 {
		b.WriteString("None recorded.\n\n")
		return
	}
	for _, f := range s.Findings {
		fmt.Fprintf(b, "- %s [%s] `%s` v%s %s — %s\n", f.EmployeeID, f.Finding.Severity, f.Finding.RuleID,
			f.Finding.RuleVersion, f.Finding.Category, oneLine(f.Finding.Message))
		for _, src := range f.Finding.Sources {
			fmt.Fprintf(b, "  - source: %s `%s` observed %s\n", src.Owner, src.ObservationID, stamp(src.ObservedAt))
		}
	}
	b.WriteString("\n")
}

func (s ActiveState) writeSources(b *strings.Builder) {
	b.WriteString("## Sources\n\n")
	for _, src := range s.Sources {
		line := fmt.Sprintf("- %s/%s: %s (%s)", src.EmployeeID, src.Owner, src.Status, src.ComputedFreshness)
		if src.ObservedAt != nil {
			line += fmt.Sprintf(" observed %s `%s`", stamp(*src.ObservedAt), src.ObservationID)
		}
		if src.SameTimeConflict {
			line += fmt.Sprintf(" CONFLICT %v", src.ConflictObservationIDs)
		}
		b.WriteString(line + "\n")
	}
	b.WriteString("\n### Source errors\n\n")
	if len(s.Errors) == 0 {
		b.WriteString("None recorded.\n\n")
	} else {
		for _, e := range s.Errors {
			fmt.Fprintf(b, "- %s/%s: `%s` %s\n", e.EmployeeID, e.Owner, e.Code, oneLine(e.Message))
		}
		b.WriteString("\n")
	}
	fmt.Fprintf(b, "### Evidence references (%d)\n\n", len(s.Evidence))
	for _, ev := range s.Evidence {
		fmt.Fprintf(b, "- %s `%s` (%s) from observation `%s`\n", ev.EmployeeID, ev.Reference.URI,
			ev.Reference.VerificationStatus, ev.ObservationID)
	}
}

func countText(v *int) string {
	if v == nil {
		return "unknown"
	}
	return fmt.Sprintf("%d", *v)
}

func stamp(t time.Time) string {
	if t.IsZero() {
		return "unknown"
	}
	return t.UTC().Format(time.RFC3339)
}

func freshnessLine(m map[SourceOwner]Freshness) string {
	if len(m) == 0 {
		return "unknown"
	}
	parts := make([]string, 0, len(m))
	for _, owner := range allSourceOwners {
		if f, ok := m[owner]; ok {
			parts = append(parts, fmt.Sprintf("%s=%s", owner, f))
		}
	}
	return strings.Join(parts, " ")
}

// oneLine keeps source-controlled text on a single Markdown line. Content is
// rendered as data: newlines and backticks cannot break out of the structure.
func oneLine(s string) string {
	replaced := strings.NewReplacer("\r", " ", "\n", " ", "`", "'").Replace(s)
	return strings.TrimSpace(replaced)
}
