package staff

import (
	"context"
	"database/sql"
	"fmt"
	"sort"
	"strings"
	"time"

	"agent-board/internal/roster"

	_ "modernc.org/sqlite"
)

// This file is the optional native Board read side of the staff view.
//
// It is deliberately separate from the board's own mutable store. An operator
// points the staff view at one native Board SQLite database; it is opened
// read-only, in one consistent read transaction, with bounded queries, and it
// is never migrated, written, backed up or repaired. When it is not configured,
// or when it cannot be read, the view says so: an unavailable source is never
// rendered as an employee with no work and never as an all-clear.
//
// Only narrow columns are read: task identity/status/assignment/review target,
// lease binding and expiry, event kind/actor/time and a requires-ack message's
// routing. Instructions, tokens, credentials, raw payloads and message bodies
// are never selected, so no private or arbitrary body can reach a browser.
//
// A bounded read that hits its row limit is not a complete read. It marks the
// affected card partial instead of concluding "no work" or an exact ask count.

const (
	boardQueryTimeout  = 3 * time.Second
	boardMaxRows       = 200
	boardTimelineLimit = 12
	defaultOperatorID  = "lee"
)

// BoardRead is one read of the configured native Board source. It carries the
// per-employee facts and the coverage statement; it is never a claim about an
// employee the source does not cover.
type BoardRead struct {
	Configured bool
	Available  bool
	OperatorID string
	SnapshotAt string
	Note       string
	Coverage   string
	Bound      int
	Total      int
	Connected  int
	byEmployee map[string]*BoardFacts
}

// BoardFacts is the Board slice of one employee card. Absence of a fact is not
// rendered as zero: an unbound or unavailable employee says so in words.
type BoardFacts struct {
	State         string       `json:"state"`
	Note          string       `json:"note,omitempty"`
	AgentID       string       `json:"agent_id,omitempty"`
	MachineID     string       `json:"machine_id,omitempty"`
	BoardRef      string       `json:"board_ref,omitempty"`
	Assigned      *BoardTask   `json:"assigned,omitempty"`
	LastResult    *BoardTask   `json:"last_result_task,omitempty"`
	ActiveCount   int          `json:"active_count"`
	Execution     string       `json:"execution,omitempty"`
	ExecutionText string       `json:"execution_text,omitempty"`
	Milestone     *BoardEvent  `json:"milestone,omitempty"`
	Accepted      *BoardEvent  `json:"accepted,omitempty"`
	ResultText    string       `json:"result_text,omitempty"`
	Asks          []BoardAsk   `json:"asks,omitempty"`
	Timeline      []BoardEvent `json:"timeline,omitempty"`
	TimelineCount int          `json:"timeline_count"`
	// Partial is true when a bounded query limit or an invalid/future source
	// time means this card cannot make a complete statement.
	Partial     bool   `json:"partial,omitempty"`
	PartialText string `json:"partial_text,omitempty"`
}

// BoardTask is a narrow Board task fact. The Board task id is the source, and
// the Board's own created/updated times are preserved unchanged.
type BoardTask struct {
	TaskID        string `json:"task_id"`
	DisplayNumber int64  `json:"display_number"`
	Title         string `json:"title"`
	Status        string `json:"status"`
	CreatedAt     string `json:"created_at,omitempty"`
	UpdatedAt     string `json:"updated_at,omitempty"`
}

// BoardEvent is one native recorded event: the raw native kind, a plain reading
// of it, the attributed actor and the producer's own event time.
type BoardEvent struct {
	Kind       string `json:"kind"`
	Meaning    string `json:"meaning"`
	Actor      string `json:"actor"`
	RecordedAt string `json:"recorded_at"`
	TaskRef    string `json:"task_ref,omitempty"`

	eventID string
	// actorType and actorID are the source's own separate fields. Actor above
	// is only a rendering of them; acceptance is decided from these, so an
	// actor id that merely looks like "human:lee" can never certify anything.
	actorType string
	actorID   string
}

// BoardAsk is a supported direct ask of the configured human operator. Only an
// unexpired pending requires-ack message and an explicit matching human review
// target become one.
type BoardAsk struct {
	Kind       string `json:"kind"`
	Text       string `json:"text"`
	RecordedAt string `json:"recorded_at"`
	ExpiresAt  string `json:"expires_at,omitempty"`
	TaskRef    string `json:"task_ref,omitempty"`
}

type boardBinding struct {
	employeeID string
	agentID    string
	machineID  string
	boardRef   string
}

type boardTaskRow struct {
	id               string
	displayNumber    int64
	title            string
	status           string
	assignedAgentID  string
	reviewTargetType string
	reviewTargetID   string
	createdAt        string
	updatedAt        string
}

type boardLeaseRow struct {
	id        string
	taskID    string
	agentID   string
	machineID string
	status    string
	claimedAt string
	expiresAt string
}

type boardEventRow struct {
	id        string
	taskID    string
	actorType string
	actorID   string
	eventType string
	createdAt string
}

type boardMessageRow struct {
	id             string
	taskID         string
	fromActorType  string
	fromActorID    string
	kind           string
	requiresAck    bool
	status         string
	createdAt      string
	acknowledgedAt string
	expiresAt      string
}

type boardData struct {
	tasks    []boardTaskRow
	leases   []boardLeaseRow
	events   []boardEventRow
	messages []boardMessageRow
	agents   map[string]string

	tasksOverflow    bool
	leasesOverflow   bool
	eventsOverflow   bool
	messagesOverflow bool
	// unattributedAsks counts valid current configured-operator asks that no bound
	// employee can carry, so coverage stays honestly partial.
	unattributedAsks int
	// unattributedUncertain counts unattributed configured-operator reviews whose own
	// task time is missing, invalid or in the future. They are unknown, and
	// are never folded into the current-ask count.
	unattributedUncertain int
	// unattributedOverflow is true when more unattributed configured-operator reviews
	// exist than this bounded read returned, so the count is a lower bound.
	unattributedOverflow bool
}

// ReadBoard reads the operator-configured native Board database for the exact
// registry-declared bindings in this fleet. It never infers a binding from a
// name, a task title or a mission substring, and it refuses to attribute facts
// when two employees declare the same binding.
func ReadBoard(ctx context.Context, path string, fleet []roster.Detail) *BoardRead {
	return ReadBoardForOperator(ctx, path, fleet, defaultOperatorID)
}

// ReadBoardForOperator reads the same bounded projection as ReadBoard, but
// selects direct human asks for operatorID. The identity is supplied only by
// server configuration; HTTP request data is never accepted here.
func ReadBoardForOperator(ctx context.Context, path string, fleet []roster.Detail, operatorID string) *BoardRead {
	operatorID = strings.TrimSpace(operatorID)
	if operatorID == "" {
		operatorID = defaultOperatorID
	}
	snapshotAt := time.Now().UTC()
	read := &BoardRead{
		Total:      len(fleet),
		OperatorID: operatorID,
		SnapshotAt: snapshotAt.Format(time.RFC3339Nano),
		byEmployee: map[string]*BoardFacts{},
	}
	path = strings.TrimSpace(path)
	if path == "" {
		read.Note = "No native Board database is configured, so no Board fact is shown. An unconfigured source is not evidence that an employee has no Board work."
		return read
	}
	read.Configured = true

	bindings, declared := bindingsFromFleet(fleet, read)
	read.Bound = declared
	if declared == 0 {
		read.Note = "No employee in this read declares a registry Board binding, so no Board fact is attributed. This is not evidence that no Board work exists."
		return read
	}
	if len(bindings) == 0 {
		read.Note = "Every registry Board binding in this read is ambiguous or duplicated, so no Board fact is attributed to any employee. The mapping is not merged."
		return read
	}
	if !plainBoardPath(path) {
		read.Note = "The configured native Board source path is not a plain filesystem path, so it is refused rather than treated as a database URI."
		for _, binding := range bindings {
			read.byEmployee[binding.employeeID] = unavailableFacts(binding, read.Note)
		}
		return read
	}

	data, failure := readBoardSource(ctx, path, bindings, operatorID, snapshotAt)
	if failure != "" {
		read.Note = "The configured native Board source is unavailable (" + failure + "). Board work is unavailable here: not zero and not an all-clear."
		for _, binding := range bindings {
			read.byEmployee[binding.employeeID] = unavailableFacts(binding, read.Note)
		}
		return read
	}
	read.Available = true
	assembleBoard(read, bindings, data, operatorID, snapshotAt)
	return read
}

func (r *BoardRead) forEmployee(employeeID string) *BoardFacts {
	if r == nil {
		return nil
	}
	return r.byEmployee[employeeID]
}

func unavailableFacts(binding boardBinding, note string) *BoardFacts {
	return &BoardFacts{State: "unavailable", Note: note, AgentID: binding.agentID, MachineID: binding.machineID, BoardRef: binding.boardRef}
}

// plainBoardPath refuses anything that could be read as DSN syntax rather than
// a literal filesystem path, so no query parameter can bypass mode=ro.
func plainBoardPath(path string) bool {
	if strings.TrimSpace(path) == "" {
		return false
	}
	for _, r := range path {
		switch r {
		case '?', '#', 0, '\n', '\r':
			return false
		}
	}
	return true
}

// bindingsFromFleet keeps only exact registry-declared agent bindings. A
// duplicate agent id or board ref is ambiguous: neither employee is given the
// other's facts, and both are told the mapping is ambiguous.
func bindingsFromFleet(fleet []roster.Detail, read *BoardRead) ([]boardBinding, int) {
	agentCount := map[string]int{}
	refCount := map[string]int{}
	all := make([]boardBinding, 0, len(fleet))
	for _, detail := range fleet {
		agentID := strings.TrimSpace(detail.Row.BoardAgentID)
		if agentID == "" {
			read.byEmployee[detail.Row.EmployeeID] = &BoardFacts{
				State: "unbound",
				Note:  "No Board agent is declared for this employee in the registry, so this employee's Board work is unknown, not zero.",
			}
			continue
		}
		binding := boardBinding{
			employeeID: detail.Row.EmployeeID,
			agentID:    agentID,
			machineID:  strings.TrimSpace(detail.Row.BoardMachineID),
			boardRef:   strings.TrimSpace(detail.Row.BoardRef),
		}
		all = append(all, binding)
		agentCount[agentID]++
		if binding.boardRef != "" {
			refCount[binding.boardRef]++
		}
	}
	clean := make([]boardBinding, 0, len(all))
	for _, binding := range all {
		if agentCount[binding.agentID] > 1 || (binding.boardRef != "" && refCount[binding.boardRef] > 1) {
			read.byEmployee[binding.employeeID] = &BoardFacts{
				State:     "ambiguous",
				Note:      "More than one employee declares the same Board binding, so no Board fact is attributed to either. The mapping is ambiguous and is not merged.",
				AgentID:   binding.agentID,
				MachineID: binding.machineID,
				BoardRef:  binding.boardRef,
			}
			continue
		}
		clean = append(clean, binding)
	}
	return clean, len(all)
}

func readBoardSource(ctx context.Context, path string, bindings []boardBinding, operatorID string, now time.Time) (boardData, string) {
	var data boardData
	ctx, cancel := context.WithTimeout(ctx, boardQueryTimeout)
	defer cancel()

	dsn := "file:" + path + "?mode=ro&_pragma=query_only(1)&_pragma=busy_timeout(2000)"
	db, err := sql.Open("sqlite", dsn)
	if err != nil {
		return data, "the source could not be opened read-only"
	}
	defer db.Close()
	db.SetMaxOpenConns(1)
	if err := db.PingContext(ctx); err != nil {
		return data, "the source file is missing or not readable read-only"
	}
	if _, err := db.ExecContext(ctx, "PRAGMA query_only = 1"); err != nil {
		return data, "the source refused read-only enforcement"
	}
	var tables int
	if err := db.QueryRowContext(ctx, `SELECT count(*) FROM sqlite_master WHERE type = 'table' AND name IN ('tasks', 'leases', 'task_events', 'messages', 'agents')`).Scan(&tables); err != nil || tables < 5 {
		return data, "the source schema is incompatible with this Board database"
	}

	tx, err := db.BeginTx(ctx, &sql.TxOptions{ReadOnly: true})
	if err != nil {
		return data, "a consistent read could not be started"
	}
	defer tx.Rollback()

	agentIDs := []string{}
	for _, binding := range bindings {
		agentIDs = appendUnique(agentIDs, binding.agentID)
	}
	data.agents = map[string]string{}
	if err := queryAgents(ctx, tx, agentIDs, &data); err != nil {
		return data, "the source could not be read"
	}
	if err := queryTasks(ctx, tx, agentIDs, &data); err != nil {
		return data, "the source could not be read"
	}
	taskIDs := taskIDList(data.tasks)
	if err := queryLeases(ctx, tx, taskIDs, &data); err != nil {
		return data, "the source could not be read"
	}
	if err := queryEvents(ctx, tx, taskIDs, &data); err != nil {
		return data, "the source could not be read"
	}
	if err := queryMessages(ctx, tx, agentIDs, operatorID, &data); err != nil {
		return data, "the source could not be read"
	}
	if err := collectUnattributedAsks(ctx, tx, agentIDs, operatorID, now, &data); err != nil {
		return data, "the source could not be read"
	}
	return data, ""
}

func queryAgents(ctx context.Context, tx *sql.Tx, agentIDs []string, data *boardData) error {
	query := "SELECT id, machine_id FROM agents WHERE id IN (" + placeholders(len(agentIDs)) + ") ORDER BY id LIMIT ?"
	rows, err := tx.QueryContext(ctx, query, append(stringArgs(agentIDs), boardMaxRows+1)...)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var id, machineID string
		if err := rows.Scan(&id, &machineID); err != nil {
			return err
		}
		data.agents[id] = machineID
	}
	return rows.Err()
}

func queryTasks(ctx context.Context, tx *sql.Tx, agentIDs []string, data *boardData) error {
	query := `SELECT id, display_number, title, status, COALESCE(assigned_agent_id, ''), review_target_type, review_target_id, created_at, updated_at
FROM tasks WHERE assigned_agent_id IN (` + placeholders(len(agentIDs)) + `)
ORDER BY julianday(updated_at) DESC, id DESC LIMIT ?`
	rows, err := tx.QueryContext(ctx, query, append(stringArgs(agentIDs), boardMaxRows+1)...)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var task boardTaskRow
		if err := rows.Scan(&task.id, &task.displayNumber, &task.title, &task.status, &task.assignedAgentID, &task.reviewTargetType, &task.reviewTargetID, &task.createdAt, &task.updatedAt); err != nil {
			return err
		}
		data.tasks = append(data.tasks, task)
	}
	if err := rows.Err(); err != nil {
		return err
	}
	if len(data.tasks) > boardMaxRows {
		data.tasks = data.tasks[:boardMaxRows]
		data.tasksOverflow = true
	}
	return nil
}

func queryLeases(ctx context.Context, tx *sql.Tx, taskIDs []string, data *boardData) error {
	if len(taskIDs) == 0 {
		return nil
	}
	query := `SELECT id, task_id, agent_id, machine_id, status, claimed_at, expires_at
FROM leases WHERE task_id IN (` + placeholders(len(taskIDs)) + `)
ORDER BY julianday(claimed_at) DESC, id DESC LIMIT ?`
	rows, err := tx.QueryContext(ctx, query, append(stringArgs(taskIDs), boardMaxRows+1)...)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var lease boardLeaseRow
		if err := rows.Scan(&lease.id, &lease.taskID, &lease.agentID, &lease.machineID, &lease.status, &lease.claimedAt, &lease.expiresAt); err != nil {
			return err
		}
		data.leases = append(data.leases, lease)
	}
	if err := rows.Err(); err != nil {
		return err
	}
	if len(data.leases) > boardMaxRows {
		data.leases = data.leases[:boardMaxRows]
		data.leasesOverflow = true
	}
	return nil
}

func queryEvents(ctx context.Context, tx *sql.Tx, taskIDs []string, data *boardData) error {
	if len(taskIDs) == 0 {
		return nil
	}
	query := `SELECT id, task_id, actor_type, actor_id, event_type, created_at
FROM task_events WHERE task_id IN (` + placeholders(len(taskIDs)) + `)
ORDER BY julianday(created_at) DESC, id DESC LIMIT ?`
	rows, err := tx.QueryContext(ctx, query, append(stringArgs(taskIDs), boardMaxRows+1)...)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var event boardEventRow
		if err := rows.Scan(&event.id, &event.taskID, &event.actorType, &event.actorID, &event.eventType, &event.createdAt); err != nil {
			return err
		}
		data.events = append(data.events, event)
	}
	if err := rows.Err(); err != nil {
		return err
	}
	if len(data.events) > boardMaxRows {
		data.events = data.events[:boardMaxRows]
		data.eventsOverflow = true
	}
	return nil
}

func queryMessages(ctx context.Context, tx *sql.Tx, agentIDs []string, operatorID string, data *boardData) error {
	query := `SELECT id, COALESCE(task_id, ''), from_actor_type, from_actor_id, kind, requires_ack, status, created_at, COALESCE(acknowledged_at, ''), COALESCE(expires_at, '')
FROM messages
WHERE to_actor_type = 'human' AND to_actor_id = ? AND requires_ack = 1 AND status = 'pending' AND acknowledged_at IS NULL
  AND from_actor_type = 'agent' AND from_actor_id IN (` + placeholders(len(agentIDs)) + `)
ORDER BY julianday(created_at) DESC, id DESC LIMIT ?`
	args := []any{operatorID}
	args = append(args, stringArgs(agentIDs)...)
	args = append(args, boardMaxRows+1)
	rows, err := tx.QueryContext(ctx, query, args...)
	if err != nil {
		return err
	}
	defer rows.Close()
	for rows.Next() {
		var message boardMessageRow
		if err := rows.Scan(&message.id, &message.taskID, &message.fromActorType, &message.fromActorID, &message.kind, &message.requiresAck, &message.status, &message.createdAt, &message.acknowledgedAt, &message.expiresAt); err != nil {
			return err
		}
		data.messages = append(data.messages, message)
	}
	if err := rows.Err(); err != nil {
		return err
	}
	if len(data.messages) > boardMaxRows {
		data.messages = data.messages[:boardMaxRows]
		data.messagesOverflow = true
	}
	return nil
}

// collectUnattributedAsks counts only valid, current asks for the configured
// human operator that no bound employee can carry. Expired, invalid, future,
// acknowledged, terminal or unknown-task messages are not counted.
func collectUnattributedAsks(ctx context.Context, tx *sql.Tx, agentIDs []string, operatorID string, now time.Time, data *boardData) error {
	messageQuery := `SELECT id, COALESCE(task_id, ''), from_actor_type, from_actor_id, kind, requires_ack, status, created_at, COALESCE(acknowledged_at, ''), COALESCE(expires_at, '')
FROM messages
WHERE to_actor_type = 'human' AND to_actor_id = ? AND requires_ack = 1 AND status = 'pending' AND acknowledged_at IS NULL
  AND (from_actor_type != 'agent' OR from_actor_id IS NULL OR from_actor_id NOT IN (` + placeholders(len(agentIDs)) + `))
  AND task_id IS NOT NULL
  AND EXISTS (SELECT 1 FROM tasks t WHERE t.id = messages.task_id AND t.status NOT IN ('done', 'cancelled', 'canceled', 'failed'))
ORDER BY julianday(created_at) DESC, id DESC LIMIT ?`
	messageArgs := []any{operatorID}
	messageArgs = append(messageArgs, stringArgs(agentIDs)...)
	messageArgs = append(messageArgs, boardMaxRows+1)
	rows, err := tx.QueryContext(ctx, messageQuery, messageArgs...)
	if err != nil {
		return err
	}
	count := 0
	for rows.Next() {
		var message boardMessageRow
		if err := rows.Scan(&message.id, &message.taskID, &message.fromActorType, &message.fromActorID, &message.kind, &message.requiresAck, &message.status, &message.createdAt, &message.acknowledgedAt, &message.expiresAt); err != nil {
			rows.Close()
			return err
		}
		if strings.EqualFold(strings.TrimSpace(message.kind), "review_request") {
			continue
		}
		if !validAskTimes(message, now) {
			continue
		}
		count++
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return err
	}
	rows.Close()

	var reviews int
	// A review is a current operator ask only when the task's recorded time is
	// present, readable and not in the future, exactly as an attributed review
	// ask is judged. Anything else is unknown, not current and not zero.
	rowQuery := `SELECT COALESCE(updated_at, '') FROM tasks
WHERE status = 'review' AND review_target_type = 'human' AND review_target_id = ?
  AND (assigned_agent_id IS NULL OR assigned_agent_id NOT IN (` + placeholders(len(agentIDs)) + `))
ORDER BY julianday(updated_at) DESC, id DESC LIMIT ?`
	reviewArgs := []any{operatorID}
	reviewArgs = append(reviewArgs, stringArgs(agentIDs)...)
	reviewArgs = append(reviewArgs, boardMaxRows+1)
	reviewRows, err := tx.QueryContext(ctx, rowQuery, reviewArgs...)
	if err != nil {
		return err
	}
	uncertain := 0
	seen := 0
	for reviewRows.Next() {
		var updatedAt string
		if err := reviewRows.Scan(&updatedAt); err != nil {
			reviewRows.Close()
			return err
		}
		seen++
		if seen > boardMaxRows {
			continue
		}
		at, ok := parseBoardTime(updatedAt)
		if !ok || at.After(now) {
			uncertain++
			continue
		}
		reviews++
	}
	if err := reviewRows.Err(); err != nil {
		reviewRows.Close()
		return err
	}
	reviewRows.Close()
	if seen > boardMaxRows {
		data.unattributedOverflow = true
	}
	data.unattributedAsks = count + reviews
	data.unattributedUncertain = uncertain
	return nil
}

func assembleBoard(read *BoardRead, bindings []boardBinding, data boardData, operatorID string, now time.Time) {
	tasksByAgent := map[string][]boardTaskRow{}
	statusByTask := map[string]string{}
	for _, task := range data.tasks {
		statusByTask[task.id] = task.status
		if task.assignedAgentID == "" {
			continue
		}
		tasksByAgent[task.assignedAgentID] = append(tasksByAgent[task.assignedAgentID], task)
	}
	leasesByTask := map[string][]boardLeaseRow{}
	for _, lease := range data.leases {
		leasesByTask[lease.taskID] = append(leasesByTask[lease.taskID], lease)
	}
	eventsByTask := map[string][]boardEventRow{}
	for _, event := range data.events {
		if event.taskID != "" {
			eventsByTask[event.taskID] = append(eventsByTask[event.taskID], event)
		}
	}

	for _, binding := range bindings {
		facts := &BoardFacts{AgentID: binding.agentID, MachineID: binding.machineID, BoardRef: binding.boardRef}
		read.byEmployee[binding.employeeID] = facts
		if binding.machineID == "" || binding.boardRef == "" {
			facts.State = "partial"
			facts.Note = "The registry Board binding is incomplete (agent id, machine id and board reference are all required), so no Board fact is attributed. This is unknown, not zero."
			continue
		}
		machine, present := data.agents[binding.agentID]
		if !present {
			facts.State = "unconnected"
			facts.Note = "The registry declares Board agent " + binding.agentID + ", which is not present in the configured source. This employee's Board connection is unknown here, not zero."
			continue
		}
		if strings.TrimSpace(machine) != binding.machineID {
			facts.State = "mismatch"
			facts.Note = "The registry machine binding does not match the machine recorded for this Board agent, so no Board fact is attributed. This is unknown, not zero."
			continue
		}
		facts.State = "connected"
		read.Connected++

		agentTasks := tasksByAgent[binding.agentID]
		taskTimesUncertain := false
		for _, task := range agentTasks {
			at, ok := parseBoardTime(task.updatedAt)
			if !ok || at.After(now) {
				taskTimesUncertain = true
				break
			}
		}
		active, terminal := splitTasks(agentTasks)
		sortTasksRecent(active)
		facts.ActiveCount = len(active)
		if len(active) > 0 {
			assigned := boardTaskOf(active[0])
			facts.Assigned = &assigned
			facts.Execution, facts.ExecutionText = executionOf(active[0], leasesByTask[active[0].id], binding, now, data.leasesOverflow)
			if facts.Execution == "partial" {
				facts.Partial = true
				facts.PartialText = appendPartial(facts.PartialText, facts.ExecutionText)
			}
		} else if data.tasksOverflow {
			facts.Partial = true
			facts.PartialText = "The source holds more assigned tasks than this bounded read can attribute, so no-work and exact counts cannot be concluded."
			facts.Execution = "partial"
			facts.ExecutionText = "A bounded read could not confirm whether any active Board work is assigned; this is not a no-work conclusion."
		} else {
			facts.Execution = "none"
			facts.ExecutionText = "No active Board assignment for this employee in the configured source."
		}
		if data.tasksOverflow {
			facts.Partial = true
			facts.PartialText = "The source holds more assigned tasks than this bounded read can attribute, so no-work and exact counts cannot be concluded."
		}

		allEvents, invalidEvents := meaningfulEvents(agentTasks, eventsByTask, now)
		sortBoardEventsDesc(allEvents)
		// A lease heartbeat or renewal is not progress. The milestone is the
		// latest actual work event, never a lease liveness signal.
		for index := range allEvents {
			if isLeaseActivity(allEvents[index].Kind) {
				continue
			}
			milestone := allEvents[index]
			facts.Milestone = &milestone
			break
		}
		if terminal != nil {
			task := boardTaskOf(*terminal)
			facts.LastResult = &task
			terminalEvents, _ := meaningfulEvents([]boardTaskRow{*terminal}, eventsByTask, now)
			sortBoardEventsDesc(terminalEvents)
			for index := range terminalEvents {
				if isAcceptanceEvent(terminalEvents[index]) {
					accepted := terminalEvents[index]
					facts.Accepted = &accepted
					break
				}
			}
			// A bounded event read may have dropped an older approval or
			// completion behind newer rows. Absence of acceptance inside a
			// truncated window is unknown, never a stated "no acceptance".
			acceptanceTruncated := facts.Accepted == nil && data.eventsOverflow
			facts.ResultText = resultTextOf(facts.Accepted, &task, acceptanceTruncated)
			if acceptanceTruncated {
				facts.Partial = true
				facts.PartialText = appendPartial(facts.PartialText, "Acceptance evidence for the latest result may lie beyond this bounded event read, so it is unknown rather than absent.")
			}
		}

		asks, uncertainReview := asksFor(binding, data, statusByTask, operatorID, now)
		facts.Asks = asks
		if uncertainReview > 0 {
			facts.Partial = true
			facts.PartialText = appendPartial(facts.PartialText, fmt.Sprintf("%d review ask(s) carried a missing, invalid or future task time and were not treated as current asks.", uncertainReview))
		}
		facts.TimelineCount = len(allEvents)
		if data.eventsOverflow {
			facts.Partial = true
			facts.PartialText = appendPartial(facts.PartialText, "The event history is longer than this bounded read, so the timeline count is a partial lower bound.")
		}
		if invalidEvents > 0 {
			facts.Partial = true
			facts.PartialText = appendPartial(facts.PartialText, fmt.Sprintf("%d recorded event(s) carried an invalid or future time and were not used to certify progress.", invalidEvents))
		}
		if taskTimesUncertain {
			facts.Partial = true
			facts.PartialText = appendPartial(facts.PartialText, "One or more task timestamps were invalid or in the future, so task ordering and the latest terminal result are uncertain and are not treated as authoritative.")
		}
		if data.messagesOverflow {
			facts.Partial = true
			facts.PartialText = appendPartial(facts.PartialText, "More requires-ack messages exist than this bounded read returned, so the ask list is partial.")
		}
		for index := 0; index < len(allEvents) && index < boardTimelineLimit; index++ {
			facts.Timeline = append(facts.Timeline, allEvents[index])
		}
	}

	operatorRef := actorRef("human", operatorID)
	read.Coverage = fmt.Sprintf("Staff Needs you selects direct Board asks for %s. Board source coverage: %d of %d employees declare a registry Board binding, and %d of those are present in this source with a matching machine. Board work for every other employee is unknown, not zero.", operatorRef, read.Bound, read.Total, read.Connected)
	if data.tasksOverflow || data.leasesOverflow || data.eventsOverflow || data.messagesOverflow {
		read.Coverage = "This bounded read hit its row limit, so Board conclusions are partial. " + read.Coverage
	}
	if data.unattributedAsks > 0 {
		read.Note = fmt.Sprintf("%d current Board ask(s) addressed to %s in this source are not attributed to any employee bound to it, so they are not shown on a card.", data.unattributedAsks, operatorRef)
	}
	if data.unattributedUncertain > 0 {
		read.Note = appendPartial(read.Note, fmt.Sprintf("%d unattributed %s review ask(s) carried a missing, invalid or future task time, so they are unknown rather than current.", data.unattributedUncertain, operatorRef))
	}
	if data.unattributedOverflow {
		read.Note = appendPartial(read.Note, "More unattributed "+operatorRef+" reviews exist than this bounded read returned, so any unattributed ask count here is a lower bound.")
	}
}

func appendPartial(existing, addition string) string {
	if existing == "" {
		return addition
	}
	return existing + " " + addition
}

func meaningfulEvents(tasks []boardTaskRow, eventsByTask map[string][]boardEventRow, now time.Time) ([]BoardEvent, int) {
	events := []BoardEvent{}
	invalid := 0
	for _, task := range tasks {
		for _, event := range eventsByTask[task.id] {
			at, ok := parseBoardTime(event.createdAt)
			if !ok || at.After(now) {
				invalid++
				continue
			}
			meaning, _ := eventMeaning(event.eventType)
			events = append(events, BoardEvent{
				Kind:       event.eventType,
				Meaning:    meaning,
				Actor:      actorRef(event.actorType, event.actorID),
				RecordedAt: event.createdAt,
				TaskRef:    event.taskID,
				eventID:    event.id,
				actorType:  event.actorType,
				actorID:    event.actorID,
			})
		}
	}
	return events, invalid
}

// isLeaseActivity is a closed list of recorded events that only show a lease
// stayed alive or was renewed. They are not work progress and never become the
// latest meaningful milestone.
func isLeaseActivity(kind string) bool {
	switch strings.ToLower(strings.TrimSpace(kind)) {
	case "heartbeat", "lease_renewed", "lease_renewal", "lease_heartbeat", "renewed":
		return true
	}
	return false
}

func asksFor(binding boardBinding, data boardData, statusByTask map[string]string, operatorID string, now time.Time) ([]BoardAsk, int) {
	asks := make([]BoardAsk, 0, 2)
	uncertainReview := 0
	operatorRef := actorRef("human", operatorID)
	for _, message := range data.messages {
		if strings.TrimSpace(message.fromActorID) != binding.agentID {
			continue
		}
		if strings.ToLower(strings.TrimSpace(message.fromActorType)) != "agent" {
			continue
		}
		if message.status != "pending" || !message.requiresAck || strings.TrimSpace(message.acknowledgedAt) != "" {
			continue
		}
		// A review notification is the same ask as the explicit task target.
		if strings.EqualFold(strings.TrimSpace(message.kind), "review_request") {
			continue
		}
		if !validAskTimes(message, now) {
			continue
		}
		// A terminal or unknown task association is not a live ask.
		if message.taskID == "" {
			continue
		}
		status, known := statusByTask[message.taskID]
		if !known || isTerminalStatus(status) {
			continue
		}
		text := "A Board message from " + binding.agentID + " requires acknowledgement from " + operatorRef + "."
		switch strings.ToLower(strings.TrimSpace(message.kind)) {
		case "handoff":
			text = "A Board handoff from " + binding.agentID + " requires acknowledgement from " + operatorRef + "."
		case "request":
			text = "A Board request from " + binding.agentID + " requires acknowledgement from " + operatorRef + "."
		}
		asks = append(asks, BoardAsk{Kind: "acknowledgement", Text: text, RecordedAt: message.createdAt, ExpiresAt: message.expiresAt, TaskRef: message.taskID})
	}
	for _, task := range data.tasks {
		if strings.TrimSpace(task.assignedAgentID) != binding.agentID {
			continue
		}
		if task.status != "review" || task.reviewTargetType != "human" || task.reviewTargetID != operatorID {
			continue
		}
		// A review ask is only current when the task's own recorded time is
		// present, readable and not in the future. Otherwise it stays an
		// uncertain partial, never a current operator ask or a false zero.
		updatedAt, ok := parseBoardTime(task.updatedAt)
		if !ok || updatedAt.After(now) {
			uncertainReview++
			continue
		}
		asks = append(asks, BoardAsk{
			Kind:       "review",
			Text:       "A review addressed to " + operatorRef + " is waiting on \"" + clip(task.title) + "\".",
			RecordedAt: task.updatedAt,
			TaskRef:    task.id,
		})
	}
	return asks, uncertainReview
}

func validAskTimes(message boardMessageRow, now time.Time) bool {
	created, ok := parseBoardTime(message.createdAt)
	if !ok || created.After(now) {
		return false
	}
	if strings.TrimSpace(message.expiresAt) == "" {
		return true
	}
	expires, ok := parseBoardTime(message.expiresAt)
	return ok && expires.After(now)
}

func executionOf(task boardTaskRow, leases []boardLeaseRow, binding boardBinding, now time.Time, leasesOverflow bool) (string, string) {
	if task.status == "claimed" {
		for _, lease := range leases {
			if lease.status != "active" {
				continue
			}
			if strings.TrimSpace(lease.agentID) != binding.agentID {
				continue
			}
			if strings.TrimSpace(lease.machineID) != binding.machineID {
				continue
			}
			claimedAt, claimedOK := parseBoardTime(lease.claimedAt)
			expires, expiresOK := parseBoardTime(lease.expiresAt)
			if !claimedOK || !expiresOK || claimedAt.After(now) || !expires.After(now) {
				continue
			}
			return "executing", "Executing: an active lease held by " + lease.agentID + " on " + lease.machineID + " expires " + lease.expiresAt + "."
		}
		// A bounded read that hit its row limit may have dropped a valid lease
		// behind newer rows. Without a visible valid lease the claim cannot be
		// called stale or running: it is uncertain.
		if leasesOverflow {
			return "partial", "The source returned more leases than this bounded read could attribute, so a valid unexpired lease may exist beyond it. This claim is uncertain, not confirmed stale and not confirmed running."
		}
		return "claimed_stale", "Claimed without a valid unexpired lease for this exact agent/machine binding, so the claim is stale or unconfirmed, not running. A heartbeat alone is not progress."
	}
	switch task.status {
	case "review":
		return "in_review", "Recorded as submitted and in review."
	case "ready", "waiting", "blocked":
		return "assigned_not_started", "Assigned and recorded as " + task.status + ", not yet claimed."
	default:
		return "assigned", "Assigned and recorded as " + task.status + "."
	}
}

func resultTextOf(accepted *BoardEvent, terminal *BoardTask, acceptanceTruncated bool) string {
	if terminal == nil {
		return ""
	}
	title := " \"" + clip(terminal.Title) + "\""
	if accepted != nil {
		return "Accepted by " + accepted.Actor + ": " + accepted.Meaning + " at " + accepted.RecordedAt + " -" + title + "."
	}
	if acceptanceTruncated {
		return "Recorded " + terminal.Status + " at " + terminal.UpdatedAt + " -" + title + "; the event history for it is longer than this bounded read, so acceptance evidence is unknown here, not absent."
	}
	return "Recorded " + terminal.Status + " at " + terminal.UpdatedAt + " -" + title + "; this source records no human or manager acceptance evidence for it."
}

func splitTasks(tasks []boardTaskRow) ([]boardTaskRow, *boardTaskRow) {
	active := make([]boardTaskRow, 0, len(tasks))
	var terminal *boardTaskRow
	for index := range tasks {
		if isTerminalStatus(tasks[index].status) {
			if terminal == nil || taskNewer(tasks[index], *terminal) {
				copied := tasks[index]
				terminal = &copied
			}
			continue
		}
		active = append(active, tasks[index])
	}
	return active, terminal
}

func taskNewer(left, right boardTaskRow) bool {
	leftAt, leftOK := parseBoardTime(left.updatedAt)
	rightAt, rightOK := parseBoardTime(right.updatedAt)
	if leftOK && rightOK && !leftAt.Equal(rightAt) {
		return leftAt.After(rightAt)
	}
	if leftOK != rightOK {
		return leftOK
	}
	return left.updatedAt > right.updatedAt
}

func sortTasksRecent(tasks []boardTaskRow) {
	sort.SliceStable(tasks, func(i, j int) bool { return taskNewer(tasks[i], tasks[j]) })
}

func isTerminalStatus(status string) bool {
	switch strings.ToLower(strings.TrimSpace(status)) {
	case "done", "cancelled", "canceled", "failed":
		return true
	}
	return false
}

func boardTaskOf(task boardTaskRow) BoardTask {
	return BoardTask{
		TaskID:        task.id,
		DisplayNumber: task.displayNumber,
		Title:         clip(task.title),
		Status:        task.status,
		CreatedAt:     task.createdAt,
		UpdatedAt:     task.updatedAt,
	}
}

func eventMeaning(eventType string) (string, bool) {
	switch strings.ToLower(strings.TrimSpace(eventType)) {
	case "created":
		return "Task created", false
	case "claimed":
		return "Task claimed", false
	case "artifact_added":
		return "Artifact added", false
	case "message_sent":
		return "Message sent", false
	case "message_acknowledged":
		return "Message acknowledged", false
	case "submitted_for_review":
		return "Worker submitted for review (not acceptance)", false
	case "review_requested":
		return "Review requested", false
	case "review_approved":
		return "Review approved", true
	case "completed":
		return "Completed", true
	case "done":
		return "Recorded done", true
	case "released", "lease_released":
		return "Lease released", false
	case "lease_expired":
		return "Lease expired", false
	default:
		return clip(humanizeKind(eventType)), false
	}
}

func isAcceptanceEvent(event BoardEvent) bool {
	_, acceptance := eventMeaning(event.Kind)
	if !acceptance {
		return false
	}
	// The source's own actor_type decides this, matched exactly. A rendered
	// "human:..." prefix can be produced by a spoofed actor id or by an actor
	// type that merely starts with "human", and never certifies acceptance.
	switch strings.ToLower(strings.TrimSpace(event.actorType)) {
	case "human", "manager":
		return strings.TrimSpace(event.actorID) != ""
	default:
		return false
	}
}

func humanizeKind(value string) string {
	value = strings.TrimSpace(strings.ReplaceAll(value, "_", " "))
	if value == "" {
		return "Recorded event"
	}
	return strings.ToUpper(value[:1]) + value[1:]
}

func actorRef(actorType, actorID string) string {
	actorType = strings.TrimSpace(actorType)
	actorID = strings.TrimSpace(actorID)
	switch {
	case actorType == "":
		return actorID
	case actorID == "":
		return actorType
	default:
		return actorType + ":" + actorID
	}
}

func sortBoardEventsDesc(events []BoardEvent) {
	sort.SliceStable(events, func(i, j int) bool {
		left, leftOK := parseBoardTime(events[i].RecordedAt)
		right, rightOK := parseBoardTime(events[j].RecordedAt)
		if leftOK && rightOK && !left.Equal(right) {
			return left.After(right)
		}
		if leftOK != rightOK {
			return leftOK
		}
		return events[i].eventID > events[j].eventID
	})
}

func parseBoardTime(value string) (time.Time, bool) {
	value = strings.TrimSpace(value)
	if value == "" {
		return time.Time{}, false
	}
	for _, layout := range []string{time.RFC3339Nano, time.RFC3339} {
		if parsed, err := time.Parse(layout, value); err == nil {
			return parsed.UTC(), true
		}
	}
	return time.Time{}, false
}

func taskIDList(tasks []boardTaskRow) []string {
	out := make([]string, 0, len(tasks))
	for _, task := range tasks {
		if task.id != "" {
			out = append(out, task.id)
		}
	}
	return out
}

func placeholders(count int) string {
	if count <= 0 {
		return "NULL"
	}
	return strings.TrimSuffix(strings.Repeat("?,", count), ",")
}

func stringArgs(values []string) []any {
	out := make([]any, 0, len(values))
	for _, value := range values {
		out = append(out, value)
	}
	return out
}

func appendUnique(values []string, value string) []string {
	for _, existing := range values {
		if existing == value {
			return values
		}
	}
	return append(values, value)
}
