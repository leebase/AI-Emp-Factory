package services

import (
	"context"
	"errors"
	"fmt"
	"strings"
	"sync"
	"time"

	"agent-board/internal/domain"
	"agent-board/internal/employee"
	"agent-board/internal/roster"
	"agent-board/internal/store"
)

type Board struct {
	store            *store.Store
	employeeRegistry *employee.Registry
	rosterMonitor    *roster.Monitor
	// Sprint 3A quick shared state artifact. The cache is created once per
	// board and is the only publisher of the artifact.
	activeStatePath string
	activeStateOnce sync.Once
	activeState     *ActiveStateCache
}

type TaskActionInput struct {
	TaskID           string `json:"task_id"`
	ActorType        string `json:"actor_type"`
	ActorID          string `json:"actor_id"`
	LeaseID          string `json:"lease_id"`
	Reason           string `json:"reason"`
	AssignType       string `json:"assign_type"`
	AssignTo         string `json:"assign_to"`
	ReviewTargetType string `json:"review_target_type"`
	ReviewTargetID   string `json:"review_target_id"`
	// These fields are populated only by the participant HTTP boundary. They
	// cause the store to enforce the exact lease binding in the transition
	// transaction; ordinary human/legacy actions leave them unset.
	RequireActiveLease bool   `json:"-"`
	LeaseAgentID       string `json:"-"`
	LeaseMachineID     string `json:"-"`
}

func NewBoard(st *store.Store) *Board {
	return &Board{store: st}
}

func NewBoardWithRegistry(st *store.Store, registry *employee.Registry) *Board {
	return &Board{store: st, employeeRegistry: registry}
}

func NewBoardWithRoster(st *store.Store, registry *employee.Registry, monitor *roster.Monitor) *Board {
	return &Board{store: st, employeeRegistry: registry, rosterMonitor: monitor}
}

func (b *Board) Migrate(ctx context.Context) error {
	return b.store.Migrate(ctx)
}

func (b *Board) RegisterMachine(ctx context.Context, in store.RegisterMachineInput) (domain.Machine, error) {
	return b.store.RegisterMachine(ctx, in)
}

func (b *Board) RegisterAgent(ctx context.Context, in store.RegisterAgentInput) (domain.Agent, error) {
	return b.store.RegisterAgent(ctx, in)
}

func (b *Board) CreateAPIToken(ctx context.Context, in store.CreateAPITokenInput) (domain.APIToken, error) {
	return b.store.CreateAPIToken(ctx, in)
}

func (b *Board) RevokeAPIToken(ctx context.Context, rawToken string) (domain.APIToken, error) {
	return b.store.RevokeAPIToken(ctx, rawToken)
}

func (b *Board) ResolveAPIToken(ctx context.Context, rawToken string) (domain.AuthIdentity, error) {
	return b.store.ResolveAPIToken(ctx, rawToken)
}

// ErrParticipantBinding reports a missing, unknown, inactive, mismatched, or
// stale Factory employee/board binding. Callers must fail closed on any error.
var ErrParticipantBinding = errors.New("participant binding verification failed")

// VerifyParticipantBinding is the authoritative check that a configured
// nonhuman principal is bound to a live Factory employee installation. Stored
// references are not authority on their own: the stable employee_id must
// resolve in the trusted registry, the registry's board_ref must match, the
// lifecycle must be active, and the pinned record digest must still match the
// current DeploymentRecord. This is what LocalAuthProvider calls before it
// issues a participant identity.
func (b *Board) VerifyParticipantBinding(principalID, employeeID, boardRef, recordDigest, agentID, machineID string) error {
	principalID = strings.TrimSpace(principalID)
	employeeID = strings.TrimSpace(employeeID)
	boardRef = strings.TrimSpace(boardRef)
	agentID = strings.TrimSpace(agentID)
	machineID = strings.TrimSpace(machineID)
	recordDigest = strings.TrimSpace(recordDigest)
	if b.employeeRegistry == nil {
		return fmt.Errorf("%w: employee registry is not configured", ErrParticipantBinding)
	}
	if principalID == "" || employeeID == "" || boardRef == "" || agentID == "" || machineID == "" || recordDigest == "" {
		return fmt.Errorf("%w: incomplete binding", ErrParticipantBinding)
	}
	record, ok := b.employeeRegistry.Lookup(employeeID)
	if !ok {
		return fmt.Errorf("%w: unknown employee_id %q", ErrParticipantBinding, employeeID)
	}
	if !record.Active() {
		return fmt.Errorf("%w: employee %q lifecycle %q is not active", ErrParticipantBinding, employeeID, record.LifecycleState)
	}
	if record.BoardRef != boardRef {
		return fmt.Errorf("%w: board_ref mismatch for employee %q", ErrParticipantBinding, employeeID)
	}
	if record.RecordDigest != recordDigest {
		return fmt.Errorf("%w: stale deployment record for employee %q", ErrParticipantBinding, employeeID)
	}
	// The record must explicitly pin the exact participation principal, Board
	// agent, and host. Missing fields are not authority: a legacy record without
	// them can never authorize a participant.
	if record.ParticipantPrincipal == "" || record.AgentID == "" || record.MachineID == "" {
		return fmt.Errorf("%w: employee %q lacks explicit participant binding fields", ErrParticipantBinding, employeeID)
	}
	if record.ParticipantPrincipal != principalID {
		return fmt.Errorf("%w: principal mismatch for employee %q", ErrParticipantBinding, employeeID)
	}
	if record.AgentID != agentID {
		return fmt.Errorf("%w: agent_id mismatch for employee %q", ErrParticipantBinding, employeeID)
	}
	if record.MachineID != machineID {
		return fmt.Errorf("%w: machine_id mismatch for employee %q", ErrParticipantBinding, employeeID)
	}
	return nil
}

func (b *Board) RecordAudit(ctx context.Context, in store.AuditRecordInput) error {
	return b.store.RecordAudit(ctx, in)
}

func (b *Board) ListAuditRecords(ctx context.Context, limit int) ([]domain.AuditRecord, error) {
	return b.store.ListAuditRecords(ctx, limit)
}

func (b *Board) Backup(ctx context.Context, outPath string) error {
	return b.store.Backup(ctx, outPath)
}

func (b *Board) CreateTask(ctx context.Context, in store.CreateTaskInput) (domain.Task, error) {
	return b.store.CreateTask(ctx, in)
}

func (b *Board) GetTask(ctx context.Context, id string) (domain.Task, error) {
	return b.store.GetTask(ctx, id)
}

func (b *Board) ListTasks(ctx context.Context, status string, projectID string) ([]domain.Task, error) {
	return b.store.ListTasks(ctx, status, projectID)
}

func (b *Board) GetTaskTree(ctx context.Context, id string) (domain.TaskTree, error) {
	return b.store.GetTaskTree(ctx, id)
}

func (b *Board) SubmitTaskForReview(ctx context.Context, in TaskActionInput) (domain.Task, error) {
	targetType, targetID := reviewTarget(in)
	task, err := b.store.GetTask(ctx, in.TaskID)
	if err != nil {
		return domain.Task{}, err
	}
	if err := allowTransition(task.Status, domain.TaskStatusReview); err != nil {
		return domain.Task{}, err
	}
	if in.ActorType == "" {
		in.ActorType = "agent"
	}
	return b.store.ApplyTaskTransition(ctx, store.TaskTransitionInput{
		TaskID:             in.TaskID,
		Status:             domain.TaskStatusReview,
		ActorType:          in.ActorType,
		ActorID:            in.ActorID,
		EventType:          "submitted_for_review",
		Reason:             in.Reason,
		LeaseID:            in.LeaseID,
		LeaseStatus:        domain.LeaseStatusCompleted,
		ReleaseLease:       true,
		ReviewTargetType:   targetType,
		ReviewTargetID:     targetID,
		ReviewMessageBody:  reviewMessageBody(task),
		RequireActiveLease: in.RequireActiveLease,
		LeaseAgentID:       in.LeaseAgentID,
		LeaseMachineID:     in.LeaseMachineID,
	})
}

func (b *Board) CompleteTask(ctx context.Context, in TaskActionInput) (domain.Task, error) {
	return b.transition(ctx, in, domain.TaskStatusDone, "completed", domain.LeaseStatusCompleted)
}

func (b *Board) ApproveTask(ctx context.Context, in TaskActionInput, completeOnApproval bool) (domain.Task, error) {
	if completeOnApproval {
		return b.reviewOutcome(ctx, in, domain.TaskStatusDone, "review_approved", "approved")
	}
	return b.reviewOutcome(ctx, in, domain.TaskStatusReview, "review_approved", "approved")
}

func (b *Board) SendTaskBack(ctx context.Context, in TaskActionInput) (domain.Task, error) {
	return b.reviewOutcome(ctx, in, domain.TaskStatusReady, "sent_back", "sent_back")
}

func (b *Board) BlockTask(ctx context.Context, in TaskActionInput) (domain.Task, error) {
	return b.transition(ctx, in, domain.TaskStatusBlocked, "blocked", domain.LeaseStatusReleased)
}

func (b *Board) FailTask(ctx context.Context, in TaskActionInput) (domain.Task, error) {
	return b.transition(ctx, in, domain.TaskStatusFailed, "failed", domain.LeaseStatusReleased)
}

func (b *Board) CancelTask(ctx context.Context, in TaskActionInput) (domain.Task, error) {
	return b.transition(ctx, in, domain.TaskStatusCanceled, "cancelled", domain.LeaseStatusCanceled)
}

func (b *Board) reviewOutcome(ctx context.Context, in TaskActionInput, status, eventType, outcome string) (domain.Task, error) {
	task, err := b.store.GetTask(ctx, in.TaskID)
	if err != nil {
		return domain.Task{}, err
	}
	if task.Status != domain.TaskStatusReview {
		return domain.Task{}, fmt.Errorf("task %s is %s, want review", in.TaskID, task.Status)
	}
	if err := allowTransition(task.Status, status); err != nil {
		return domain.Task{}, err
	}
	if in.ActorType == "" {
		in.ActorType = "human"
	}
	return b.store.ApplyTaskTransition(ctx, store.TaskTransitionInput{
		TaskID:                    in.TaskID,
		Status:                    status,
		ActorType:                 in.ActorType,
		ActorID:                   in.ActorID,
		EventType:                 eventType,
		Reason:                    in.Reason,
		LeaseID:                   in.LeaseID,
		LeaseStatus:               domain.LeaseStatusCompleted,
		ReleaseLease:              true,
		AcknowledgeReviewMessages: true,
		Payload:                   map[string]string{"outcome": outcome},
		RequireActiveLease:        in.RequireActiveLease,
		LeaseAgentID:              in.LeaseAgentID,
		LeaseMachineID:            in.LeaseMachineID,
	})
}

func (b *Board) transition(ctx context.Context, in TaskActionInput, status, eventType, leaseStatus string) (domain.Task, error) {
	task, err := b.store.GetTask(ctx, in.TaskID)
	if err != nil {
		return domain.Task{}, err
	}
	if err := allowTransition(task.Status, status); err != nil {
		return domain.Task{}, err
	}
	if in.ActorType == "" {
		in.ActorType = "agent"
	}
	return b.store.ApplyTaskTransition(ctx, store.TaskTransitionInput{
		TaskID:                    in.TaskID,
		Status:                    status,
		ActorType:                 in.ActorType,
		ActorID:                   in.ActorID,
		EventType:                 eventType,
		Reason:                    in.Reason,
		LeaseID:                   in.LeaseID,
		LeaseStatus:               leaseStatus,
		ReleaseLease:              true,
		AcknowledgeReviewMessages: status != domain.TaskStatusReview,
		RequireActiveLease:        in.RequireActiveLease,
		LeaseAgentID:              in.LeaseAgentID,
		LeaseMachineID:            in.LeaseMachineID,
	})
}

func allowTransition(from, to string) error {
	switch from {
	case domain.TaskStatusCanceled:
		return errors.New("cancelled tasks cannot transition")
	case domain.TaskStatusDone:
		return errors.New("done tasks cannot transition")
	case domain.TaskStatusFailed:
		if to == domain.TaskStatusReview || to == domain.TaskStatusDone {
			return fmt.Errorf("failed tasks cannot transition to %s", to)
		}
	}
	return nil
}

func reviewTarget(in TaskActionInput) (string, string) {
	targetType := firstNonEmpty(in.AssignType, in.ReviewTargetType)
	targetID := firstNonEmpty(in.AssignTo, in.ReviewTargetID)
	return strings.TrimSpace(targetType), strings.TrimSpace(targetID)
}

func reviewMessageBody(task domain.Task) string {
	if task.DisplayNumber > 0 {
		return fmt.Sprintf("Review requested for #%d %s", task.DisplayNumber, task.Title)
	}
	return "Review requested for " + task.Title
}

func firstNonEmpty(values ...string) string {
	for _, value := range values {
		if strings.TrimSpace(value) != "" {
			return value
		}
	}
	return ""
}

func (b *Board) CreateMessage(ctx context.Context, in store.CreateMessageInput) (domain.Message, error) {
	return b.store.CreateMessage(ctx, in)
}

func (b *Board) AcknowledgeMessage(ctx context.Context, id, actorType, actorID string) (domain.Message, error) {
	return b.store.AcknowledgeMessage(ctx, id, actorType, actorID)
}

func (b *Board) GetMessage(ctx context.Context, id string) (domain.Message, error) {
	return b.store.GetMessage(ctx, id)
}

func (b *Board) GetLease(ctx context.Context, id string) (domain.Lease, error) {
	return b.store.GetLease(ctx, id)
}

func (b *Board) ListTaskMessages(ctx context.Context, taskID string) ([]domain.Message, error) {
	return b.store.ListTaskMessages(ctx, taskID)
}

func (b *Board) ListInbox(ctx context.Context, toActorType, toActorID string) ([]domain.Message, error) {
	return b.store.ListInbox(ctx, toActorType, toActorID)
}

func (b *Board) ListDirectInbox(ctx context.Context, toActorType, toActorID string) ([]domain.Message, error) {
	return b.store.ListDirectInbox(ctx, toActorType, toActorID)
}

func (b *Board) AddArtifact(ctx context.Context, in store.AddArtifactInput) (domain.Artifact, error) {
	return b.store.AddArtifact(ctx, in)
}

func (b *Board) GetArtifact(ctx context.Context, id string) (domain.Artifact, error) {
	return b.store.GetArtifact(ctx, id)
}

func (b *Board) ListArtifacts(ctx context.Context, taskID string) ([]domain.Artifact, error) {
	return b.store.ListArtifacts(ctx, taskID)
}

func (b *Board) validateEmployeeID(employeeID string) error {
	employeeID = strings.TrimSpace(employeeID)
	if b.employeeRegistry == nil {
		if employeeID == "" {
			return nil
		}
		return errors.New("employee registry is not configured")
	}
	if employeeID == "" {
		return errors.New("employee_id is required when employee registry is configured")
	}
	if _, ok := b.employeeRegistry.Lookup(employeeID); !ok {
		return fmt.Errorf("unknown employee_id: %s", employeeID)
	}
	return nil
}

func (b *Board) ListEmployees() []employee.Record {
	if b.employeeRegistry == nil {
		return nil
	}
	return b.employeeRegistry.List()
}

func (b *Board) EmployeeRoster(ctx context.Context) roster.Snapshot {
	if b.rosterMonitor == nil {
		return roster.Snapshot{GeneratedAt: time.Now().UTC(), Employees: []roster.Row{}}
	}
	return b.rosterMonitor.Snapshot(ctx)
}

func (b *Board) EmployeeDetail(ctx context.Context, name string) (roster.Detail, error) {
	if b.rosterMonitor == nil {
		return roster.Detail{}, errors.New("employee roster is not configured")
	}
	return b.rosterMonitor.Detail(ctx, name)
}

// EmployeeFleet is the read-only one-pass read used by the staff view: every
// employee with its own records, from a single read of the sources. An absent
// monitor is reported as such rather than as an empty estate.
func (b *Board) EmployeeFleet(ctx context.Context) (time.Time, []roster.Detail, []string, error) {
	if b.rosterMonitor == nil {
		return time.Time{}, nil, nil, errors.New("employee roster is not configured")
	}
	observedAt, fleet, sourceErrors := b.rosterMonitor.Fleet(ctx)
	return observedAt, fleet, sourceErrors, nil
}

func (b *Board) UpsertAutoOrchCycleReport(ctx context.Context, in store.UpsertAutoOrchCycleReportInput) (domain.AutoOrchCycleReport, error) {
	if err := b.validateEmployeeID(in.EmployeeID); err != nil {
		return domain.AutoOrchCycleReport{}, err
	}
	return b.store.UpsertAutoOrchCycleReport(ctx, in)
}

func (b *Board) ListAutoOrchCycleReports(ctx context.Context, missionName string, limit int) ([]domain.AutoOrchCycleReport, error) {
	return b.store.ListAutoOrchCycleReports(ctx, missionName, limit)
}

func (b *Board) UpsertBoardReviewTask(ctx context.Context, in store.UpsertBoardReviewTaskInput) (domain.BoardReviewTask, error) {
	if err := b.validateEmployeeID(in.EmployeeID); err != nil {
		return domain.BoardReviewTask{}, err
	}
	return b.store.UpsertBoardReviewTask(ctx, in)
}

func (b *Board) GetBoardReviewTask(ctx context.Context, missionName, runID, reviewGate string) (domain.BoardReviewTask, error) {
	return b.store.GetBoardReviewTask(ctx, missionName, runID, reviewGate)
}

func (b *Board) ListAssignedBoardReviewTasks(ctx context.Context, targetType, targetID string) ([]domain.BoardReviewTask, error) {
	return b.store.ListAssignedBoardReviewTasks(ctx, targetType, targetID)
}

func (b *Board) ApplyReviewDisposition(ctx context.Context, in store.ApplyReviewDispositionInput) (domain.BoardReviewTask, error) {
	if b.employeeRegistry != nil {
		review, err := b.store.GetBoardReviewTask(ctx, in.MissionName, in.RunID, in.ReviewGate)
		if err != nil {
			return domain.BoardReviewTask{}, err
		}
		if err := b.validateEmployeeID(review.EmployeeID); err != nil {
			return domain.BoardReviewTask{}, err
		}
	}
	return b.store.ApplyReviewDisposition(ctx, in)
}

func (b *Board) PollAndClaim(ctx context.Context, in store.PollInput) (domain.ClaimedTask, error) {
	return b.store.PollAndClaim(ctx, in)
}

func (b *Board) RenewLease(ctx context.Context, leaseID string, seconds int) (domain.Lease, error) {
	return b.store.RenewLease(ctx, leaseID, seconds)
}

// RenewParticipantLease keeps the participant's exact task/agent/machine
// binding inside the renewal UPDATE. Legacy callers continue to use
// RenewLease, whose compatibility behavior is unchanged.
func (b *Board) RenewParticipantLease(ctx context.Context, leaseID, taskID, agentID, machineID string, seconds int) (domain.Lease, error) {
	return b.store.RenewParticipantLease(ctx, leaseID, taskID, agentID, machineID, seconds)
}

// ActiveLease returns the server-derived active, unexpired lease for the exact
// task/agent/machine binding, or an error when no such claim exists.
func (b *Board) ActiveLease(ctx context.Context, taskID, agentID, machineID string) (domain.Lease, error) {
	return b.store.ActiveLeaseForParticipation(ctx, taskID, agentID, machineID)
}

func (b *Board) ExpireStaleLeases(ctx context.Context) (int64, error) {
	return b.store.ExpireStaleLeases(ctx)
}

func (b *Board) ListMachines(ctx context.Context) ([]domain.Machine, error) {
	return b.store.ListMachines(ctx)
}

func (b *Board) ListAgents(ctx context.Context) ([]domain.Agent, error) {
	return b.store.ListAgents(ctx)
}

func (b *Board) ListActiveLeases(ctx context.Context) ([]domain.LeaseObservation, error) {
	return b.store.ListActiveLeases(ctx)
}

func (b *Board) ProjectStates(ctx context.Context, projectID string) ([]domain.ProjectState, error) {
	return b.store.ListProjectStates(ctx, projectID)
}

func (b *Board) ListStaleLeases(ctx context.Context) ([]domain.LeaseObservation, error) {
	return b.store.ListStaleLeases(ctx)
}

func (b *Board) ListEvents(ctx context.Context, taskID string) ([]domain.TaskEvent, error) {
	return b.store.ListEvents(ctx, taskID)
}

func (b *Board) Status(ctx context.Context) (domain.StatusSummary, error) {
	return b.store.Status(ctx)
}

func (b *Board) Snapshot(ctx context.Context) (domain.BoardSnapshot, error) {
	snapshot, err := b.store.Snapshot(ctx)
	if err != nil {
		return domain.BoardSnapshot{}, err
	}
	snapshot.Kind = domain.BoardSnapshotKind
	snapshot.SchemaVersion = domain.BoardSnapshotSchemaVersion
	snapshot.OwnerVersion = domain.BoardOwnerVersion
	return snapshot, nil
}

func (b *Board) DecayPresence(ctx context.Context, window time.Duration) (int64, int64, error) {
	return b.store.DecayPresence(ctx, window)
}
