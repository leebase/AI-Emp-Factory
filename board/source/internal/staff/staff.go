// Package staff builds the read-only staff view: one canonical record per
// employee, assembled only from facts a source file actually recorded.
//
// Three rules separate it from the roster classification it reads:
//
//  1. Absence of a live signal is absence of a signal. It is never rendered as
//     offline, broken, idle or on the bench. Activity is simply unknown.
//  2. A mission registration is a standing remit, not a current assignment, and
//     an Auto-Orch mission status is not everything an interactive employee
//     might be doing.
//  3. A failed cycle is a recorded result, never delivered work.
//
// Every fact carries the file it was read from and the date that file recorded,
// so a reader can tell a stated fact from an inference. Narrative text is
// carried through verbatim and bounded; it is never parsed into a managerial
// conclusion.
package staff

import (
	"sort"
	"strconv"
	"strings"
	"time"

	"agent-board/internal/roster"
)

const (
	maxTextLength    = 400
	maxRunsShown     = 5
	maxFindings      = 3
	maxRelationships = 12
)

// Fact is one recorded value with its provenance. Known=false means no source
// recorded it; Text then says so and states nothing else.
type Fact struct {
	Known      bool     `json:"known"`
	Text       string   `json:"text"`
	RecordedAt string   `json:"recorded_at,omitempty"`
	AgeText    string   `json:"age_text,omitempty"`
	SourceRefs []string `json:"source_refs,omitempty"`
}

// Run is one recorded cycle. Runs are always children of an employee: a run is
// never promoted into an employee identity of its own.
type Run struct {
	RunRef       string   `json:"run_ref"`
	Title        string   `json:"title,omitempty"`
	Outcome      string   `json:"outcome,omitempty"`
	Delivery     string   `json:"delivery"`
	DeliveryText string   `json:"delivery_text,omitempty"`
	EndedAt      string   `json:"ended_at,omitempty"`
	RunStatus    string   `json:"run_status,omitempty"`
	Findings     []string `json:"findings,omitempty"`
	FindingCount int      `json:"finding_count"`
	SourceRef    string   `json:"source_ref,omitempty"`
}

// Attention separates a recorded requirement for a human decision from a
// warning about the record itself. A warning is never counted as a decision.
type Attention struct {
	Kind       string   `json:"kind"`
	Text       string   `json:"text"`
	RecordedAt string   `json:"recorded_at,omitempty"`
	Actor      string   `json:"actor,omitempty"`
	SourceRefs []string `json:"source_refs,omitempty"`
}

const (
	KindDecision = "decision"
	KindBlocker  = "blocker"
	KindPaused   = "paused"
	KindWarning  = "warning"
)

// Employee is the one canonical card. Runs, findings and warnings hang off it;
// none of them is ever rendered as a sibling employee.
type Employee struct {
	EmployeeID    string `json:"employee_id"`
	Name          string `json:"name"`
	Activity      Fact   `json:"activity"`
	ActivityState string `json:"activity_state"`
	// LatestTask is the work the producer recorded for the latest cycle. It is
	// historical unless a run is actually recorded as in progress, and it says
	// so, so an old cycle is never read as today's assignment.
	LatestTask     Fact           `json:"latest_task"`
	Assignment     *Fact          `json:"assignment,omitempty"`
	StandingRemit  Fact           `json:"standing_remit"`
	LastResult     Fact           `json:"last_result"`
	Schedule       Fact           `json:"schedule"`
	Attention      []Attention    `json:"attention,omitempty"`
	DecisionCount  int            `json:"decision_count"`
	AttentionCount int            `json:"attention_count"`
	Runs           []Run          `json:"runs,omitempty"`
	RunCount       int            `json:"run_count"`
	Relationships  []Relationship `json:"relationships,omitempty"`
	SourceRefs     []string       `json:"source_refs,omitempty"`
	// Board is the optional native Board slice of this employee. It is nil
	// when no Board source is configured; when it is present it separately
	// states whether the source covered this employee.
	Board *BoardFacts `json:"board,omitempty"`
}

// View is the whole staff read. ObservedAt is when the sources were read, not a
// claim that anything in the estate is currently true.
type View struct {
	ObservedAt        time.Time  `json:"observed_at"`
	Employees         []Employee `json:"employees"`
	SourceNotes       []string   `json:"source_notes,omitempty"`
	RelationshipNotes []string   `json:"relationship_notes,omitempty"`
	// BoardObservedAt is when the native Board source was read, if one is
	// configured. It is distinct from ObservedAt (the owner-file read), and it
	// is the read time, never a claim that a producer event is current.
	BoardObservedAt string   `json:"board_observed_at,omitempty"`
	BoardNotes      []string `json:"board_notes,omitempty"`
}

// Build assembles the view from one fleet read. It copies only named fields:
// mission documents, configuration bodies and state files are deliberately not
// reachable from here, so no raw source body can reach a browser.
func Build(observedAt time.Time, fleet []roster.Detail, sourceNotes []string, book *Book, board *BoardRead) View {
	view := View{ObservedAt: observedAt.UTC(), Employees: make([]Employee, 0, len(fleet)), SourceNotes: sourceNotes}
	present := map[string]string{}
	for _, detail := range fleet {
		present[detail.Row.EmployeeID] = detail.Row.Name
	}
	for _, detail := range fleet {
		employee := buildEmployee(observedAt, detail)
		employee.Board = board.forEmployee(detail.Row.EmployeeID)
		view.Employees = append(view.Employees, employee)
	}
	if book != nil {
		notes := book.Attach(view.Employees, present)
		view.RelationshipNotes = notes
	}
	if board != nil {
		view.BoardObservedAt = board.SnapshotAt
		if board.Note != "" {
			view.BoardNotes = append(view.BoardNotes, board.Note)
		}
		if board.Coverage != "" {
			view.BoardNotes = append(view.BoardNotes, board.Coverage)
		}
	}
	// Order by how much the record actually says, not by the alphabet. A card
	// with a blocker outranks one with a warning, which outranks one carrying a
	// recorded task, which outranks one with nothing; ties stay alphabetical.
	// This is ordering only: no card's content changes with its position, and
	// a quiet employee is still an employee, just further down.
	sort.SliceStable(view.Employees, func(i, j int) bool {
		left, right := rank(view.Employees[i]), rank(view.Employees[j])
		if left != right {
			return left > right
		}
		return strings.ToLower(view.Employees[i].Name) < strings.ToLower(view.Employees[j].Name)
	})
	return view
}

func rank(employee Employee) int {
	best := 0
	if employee.Board != nil {
		if len(employee.Board.Asks) > 0 {
			best = max(best, 4)
		}
		if employee.Board.Assigned != nil && employee.Board.Execution == "executing" {
			best = max(best, 3)
		} else if employee.Board.Assigned != nil {
			best = max(best, 1)
		}
	}
	for _, item := range employee.Attention {
		switch item.Kind {
		case KindDecision:
			best = max(best, 4)
		case KindBlocker:
			best = max(best, 3)
		case KindPaused, KindWarning:
			best = max(best, 2)
		}
	}
	if best == 0 && employee.LatestTask.Known {
		best = 1
	}
	return best
}

func buildEmployee(now time.Time, detail roster.Detail) Employee {
	row := detail.Row
	var latest roster.Cycle
	if len(detail.RecentCycles) > 0 {
		latest = detail.RecentCycles[0]
	}
	employee := Employee{EmployeeID: row.EmployeeID, Name: row.Name, SourceRefs: bounded(row.Sources, 20)}
	employee.ActivityState, employee.Activity = activityOf(row, latest)
	employee.LatestTask = latestTaskOf(now, row, latest)
	employee.Assignment = assignmentOf(row, latest)
	employee.StandingRemit = remitOf(row)
	employee.LastResult = lastResultOf(now, row, latest)
	employee.Schedule = scheduleOf(row)
	employee.Attention = attentionOf(row, latest)
	for _, item := range employee.Attention {
		if item.Kind == KindDecision {
			employee.DecisionCount++
		}
	}
	employee.AttentionCount = len(employee.Attention)
	employee.Runs, employee.RunCount = runsOf(detail)
	return employee
}

// activityOf answers in a few words and never converts silence into a state.
// Only an observed process or a run the producer recorded as in progress is a
// live signal. Everything else is simply not reported; the caveat that this is
// not offline, broken or on the bench belongs in the evidence text, not on
// every card.
func activityOf(row roster.Row, latest roster.Cycle) (string, Fact) {
	if uptime := strings.TrimSpace(row.Uptime); uptime != "" {
		return "process_observed", Fact{Known: true, Text: "Process running (" + clip(uptime) + ")", SourceRefs: []string{"docker ps"}}
	}
	if strings.EqualFold(strings.TrimSpace(latest.RunStatus), "RUNNING") {
		return "run_in_progress", Fact{Known: true, Text: "Run in progress", SourceRefs: refs(latest.Path)}
	}
	return "unknown", Fact{Known: false, Text: "Activity not reported"}
}

// latestTaskOf names the work the producer itself selected for its latest
// cycle. When no run is in progress it is labelled historical, because an old
// cycle report is a record of what was worked on, not a current assignment.
func latestTaskOf(now time.Time, row roster.Row, latest roster.Cycle) Fact {
	title := strings.TrimSpace(latest.Title)
	if title == "" {
		return Fact{Known: false, Text: "No recorded task"}
	}
	recorded := strings.TrimSpace(latest.CycleEnd)
	if recorded == "" {
		recorded = strings.TrimSpace(row.LastCycleEnd)
	}
	prefix := "Last worked on: "
	if strings.EqualFold(strings.TrimSpace(latest.RunStatus), "RUNNING") {
		prefix = "Working on: "
	}
	return Fact{Known: true, Text: prefix + clip(title), RecordedAt: recorded, AgeText: ageText(now, recorded), SourceRefs: refs(latest.Path)}
}

// assignmentOf is present only when a run is actually recorded as in progress.
// An old loop state is not an assignment, so most employees carry no field here
// at all rather than a second panel saying the same unknown twice.
func assignmentOf(row roster.Row, latest roster.Cycle) *Fact {
	if !strings.EqualFold(strings.TrimSpace(latest.RunStatus), "RUNNING") {
		return nil
	}
	text := "Run " + clip(latest.RunID) + " is recorded as in progress"
	if title := strings.TrimSpace(latest.Title); title != "" {
		text = "In progress: " + clip(title)
	}
	return &Fact{Known: true, Text: text, RecordedAt: strings.TrimSpace(latest.CycleEnd), SourceRefs: refs(latest.Path)}
}

// remitOf quotes the mission charter's own Purpose section. A registration
// identifier explains nothing, so it is never offered in its place: with no
// charter purpose the answer is a short unavailable.
func remitOf(row roster.Row) Fact {
	if purpose := strings.TrimSpace(row.MissionPurpose); purpose != "" {
		return Fact{Known: true, Text: clip(purpose), SourceRefs: refs(row.MissionPath)}
	}
	return Fact{Known: false, Text: "No mission purpose recorded", SourceRefs: refs(row.MissionPath, row.RegistryPath)}
}

// lastResultOf states the producer's own outcome and its own delivery
// disposition, with the cycle's own end date preserved.
func lastResultOf(now time.Time, row roster.Row, latest roster.Cycle) Fact {
	outcome := strings.TrimSpace(latest.Outcome)
	if outcome == "" {
		outcome = strings.TrimSpace(row.LastOutcome)
	}
	sources := refs(latest.Path, row.StatePath)
	if outcome == "" {
		return Fact{Known: false, Text: "No result recorded", SourceRefs: sources}
	}
	_, deliveryText := deliveryOf(latest, outcome)
	recorded := strings.TrimSpace(latest.CycleEnd)
	if recorded == "" {
		recorded = strings.TrimSpace(row.LastCycleEnd)
	}
	return Fact{Known: true, Text: clip(outcome) + " - " + deliveryText, RecordedAt: recorded, AgeText: ageText(now, recorded), SourceRefs: sources}
}

// deliveryOf reads the producer's own delivery disposition. Completion is not
// acceptance: only the recorded value_assessment status of "delivered", with a
// passing run, is delivery. A successful cycle with no such assessment is
// completed with delivery unverified, and a failure is never a delivery.
func deliveryOf(latest roster.Cycle, outcome string) (string, string) {
	status := strings.ToLower(strings.TrimSpace(latest.ValueStatus))
	passed := strings.ToLower(strings.TrimSpace(latest.RunPassed))
	switch {
	case status == "delivered" && passed == "true":
		return "delivered", "delivered (producer recorded delivery)"
	case status == "delivered":
		return "delivery_unverified", "recorded as delivered, but the run recorded no pass; delivery unverified"
	case strings.EqualFold(outcome, "failed") || passed == "false":
		return "not_delivered", "not delivered"
	case strings.EqualFold(outcome, "success"):
		return "delivery_unverified", "completed; delivery unverified"
	default:
		return "unknown", "delivery not recorded"
	}
}

// scheduleOf never infers future work from a weak match. A next run is stated
// only when a crontab line names this mission as an argument to the Auto-Orch
// CLI; anything else is reported as an unconfirmed possibility. An explicit
// pause, a halted loop or an unapproved launch means not runnable, full stop.
func scheduleOf(row roster.Row) Fact {
	if row.Paused {
		text := "Paused - no scheduled work"
		if actor := strings.TrimSpace(row.PauseActor); actor != "" {
			text += " (paused by " + clip(actor) + ")"
		}
		return Fact{Known: true, Text: text, RecordedAt: strings.TrimSpace(row.PausedAt), SourceRefs: refs(row.StatePath)}
	}
	if row.LaunchApproved != nil && !*row.LaunchApproved {
		return Fact{Known: true, Text: "Launch not approved - not runnable", SourceRefs: refs(row.ConfigPath)}
	}
	if haltedState(row.LoopState) {
		return Fact{Known: true, Text: "Loop " + clip(row.LoopState) + " - not runnable", SourceRefs: refs(row.StatePath)}
	}
	matched := strings.TrimSpace(row.CronMatchedOn)
	if !row.CronExactBinding {
		if matched == "" {
			return Fact{Known: false, Text: "No schedule recorded"}
		}
		return Fact{Known: false, Text: "Possible schedule match on \"" + clip(matched) + "\"; not a confirmed binding", SourceRefs: []string{"crontab -l"}}
	}
	if !row.ScheduleRunnable || strings.TrimSpace(row.NextFire) == "" {
		return Fact{Known: false, Text: "No runnable schedule recorded"}
	}
	return Fact{Known: true, Text: "Next run " + clip(row.NextFire), SourceRefs: []string{"crontab -l"}}
}

// haltedState is a closed list of recorded states that actually mean stopped.
// Idle is normal work rhythm and is deliberately absent: treating it as a
// blocker would turn the whole roster into warnings.
func haltedState(state string) bool {
	switch strings.ToLower(strings.TrimSpace(state)) {
	case "admission_blocked", "blocked", "halted", "stopped", "value_exhausted":
		return true
	}
	return false
}

// attentionOf raises only what a source explicitly recorded as stopped or
// failed. An intentional pause is its own kind: it is a decision already taken,
// not a decision waiting on Lee. No entry here is a request for a decision,
// because no source in this slice records a structured decision gate.
func attentionOf(row roster.Row, latest roster.Cycle) []Attention {
	items := make([]Attention, 0, 3)
	if row.Paused {
		text := "Paused"
		if reason := strings.TrimSpace(row.PauseReasonText); reason != "" {
			text = clip(reason)
		}
		items = append(items, Attention{Kind: KindPaused, Text: text, Actor: clip(row.PauseActor), RecordedAt: strings.TrimSpace(row.PausedAt), SourceRefs: refs(row.StatePath)})
	}
	if row.PauseConflict {
		items = append(items, Attention{Kind: KindWarning, Text: "Two equally recent pause records disagree; the paused state is contradictory in the record.", SourceRefs: refs(row.StatePath)})
	}
	if haltedState(row.LoopState) {
		items = append(items, Attention{Kind: KindBlocker, Text: "Loop state " + clip(row.LoopState), SourceRefs: refs(row.StatePath)})
	}
	outcome := strings.TrimSpace(latest.Outcome)
	if outcome == "" {
		outcome = strings.TrimSpace(row.LastOutcome)
	}
	if strings.EqualFold(outcome, "failed") {
		text := "Latest cycle failed"
		if reason := strings.TrimSpace(latest.ValueReason); reason != "" {
			text += " (" + clip(reason) + ")"
		}
		items = append(items, Attention{Kind: KindWarning, Text: text, RecordedAt: strings.TrimSpace(latest.CycleEnd), SourceRefs: refs(latest.Path, row.StatePath)})
	}
	return items
}

// runsOf nests the recorded cycles under the employee and bounds both the
// number shown and the text carried out of each one.
func runsOf(detail roster.Detail) ([]Run, int) {
	runs := make([]Run, 0, maxRunsShown)
	for i, cycle := range detail.RecentCycles {
		if i >= maxRunsShown {
			break
		}
		run := Run{
			RunRef:       clip(cycle.CycleID),
			Title:        clip(cycle.Title),
			Outcome:      clip(cycle.Outcome),
			RunStatus:    clip(cycle.RunStatus),
			FindingCount: len(cycle.Errors),
			SourceRef:    clip(cycle.Path),
		}
		run.Delivery, run.DeliveryText = deliveryOf(cycle, cycle.Outcome)
		run.EndedAt = clip(cycle.CycleEnd)
		for j, finding := range cycle.Errors {
			if j >= maxFindings {
				break
			}
			run.Findings = append(run.Findings, clip(finding))
		}
		runs = append(runs, run)
	}
	return runs, len(detail.RecentCycles)
}

// ageText states how old the record is, and says so as the age of the record.
// The source date itself is preserved separately and unmodified; an
// unparseable or absent date yields an explicit unknown, never "just now".
func ageText(now time.Time, recorded string) string {
	recorded = strings.TrimSpace(recorded)
	if recorded == "" {
		return "age unknown: no date recorded"
	}
	parsed, err := time.Parse(time.RFC3339, recorded)
	if err != nil {
		return "age unknown: recorded date is not a readable timestamp"
	}
	delta := now.UTC().Sub(parsed.UTC())
	if delta < 0 {
		return "age unknown: the recorded date is later than this read"
	}
	switch {
	case delta < time.Hour:
		return "recorded " + itoa(int(delta/time.Minute)) + "m before this read"
	case delta < 48*time.Hour:
		return "recorded " + itoa(int(delta/time.Hour)) + "h before this read"
	default:
		return "recorded " + itoa(int(delta/(24*time.Hour))) + "d before this read"
	}
}

func itoa(value int) string { return strconv.Itoa(value) }

func refs(values ...string) []string {
	out := make([]string, 0, len(values))
	for _, value := range values {
		if value = strings.TrimSpace(value); value != "" {
			out = append(out, value)
		}
	}
	if len(out) == 0 {
		return nil
	}
	return out
}

func bounded(values []string, limit int) []string {
	if len(values) <= limit {
		return values
	}
	return values[:limit]
}

// clip bounds any text copied out of a source file. The text itself is carried
// verbatim: it is a quotation, never an interpretation.
func clip(value string) string {
	value = strings.TrimSpace(value)
	if len(value) <= maxTextLength {
		return value
	}
	return strings.TrimSpace(value[:maxTextLength]) + "…"
}
