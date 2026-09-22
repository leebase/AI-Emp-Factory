package store

import (
	"context"
	"crypto/rand"
	"crypto/sha256"
	"database/sql"
	"embed"
	"encoding/hex"
	"encoding/json"
	"errors"
	"fmt"
	"io/fs"
	"os"
	"path/filepath"
	"sort"
	"strings"
	"time"

	"agent-board/internal/domain"

	_ "modernc.org/sqlite"
)

//go:embed migrations/*.sql
var migrationFiles embed.FS

type Store struct {
	db *sql.DB
}

type RegisterMachineInput struct {
	ID           string   `json:"id"`
	Hostname     string   `json:"hostname"`
	OS           string   `json:"os"`
	Arch         string   `json:"arch"`
	Capabilities []string `json:"capabilities"`
}

type RegisterAgentInput struct {
	ID           string   `json:"id"`
	MachineID    string   `json:"machine_id"`
	Kind         string   `json:"kind"`
	Role         string   `json:"role"`
	Capabilities []string `json:"capabilities"`
}

type CreateTaskInput struct {
	Title                string   `json:"title"`
	Mission              string   `json:"mission"`
	Priority             string   `json:"priority"`
	AssignedAgentID      string   `json:"assigned_agent_id"`
	RequiredCapabilities []string `json:"required_capabilities"`
	MachineConstraints   []string `json:"machine_constraints"`
	Instructions         string   `json:"instructions"`
	ParentTaskID         string   `json:"parent_task_id"`
	ProjectID            string   `json:"project_id"`
	ActorType            string   `json:"actor_type"`
	ActorID              string   `json:"actor_id"`
}

type PollInput struct {
	AgentID      string   `json:"agent_id"`
	MachineID    string   `json:"machine_id"`
	Capabilities []string `json:"capabilities"`
	LeaseSeconds int      `json:"lease_seconds"`
	// AssignedOnly is server-derived for least-privilege participants: it is
	// never decoded from request JSON. It restricts claiming to tasks already
	// assigned to this agent, so a participant cannot self-select unassigned
	// work.
	AssignedOnly bool `json:"-"`
}

type CreateMessageInput struct {
	TaskID        string `json:"task_id"`
	FromActorType string `json:"from_actor_type"`
	FromActorID   string `json:"from_actor_id"`
	ToActorType   string `json:"to_actor_type"`
	ToActorID     string `json:"to_actor_id"`
	Kind          string `json:"kind"`
	Body          string `json:"body"`
	RequiresAck   bool   `json:"requires_ack"`
}

type AddArtifactInput struct {
	ID        string `json:"id"`
	TaskID    string `json:"task_id"`
	AgentID   string `json:"agent_id"`
	Kind      string `json:"kind"`
	PathOrURL string `json:"path_or_url"`
	Summary   string `json:"summary"`
	Hash      string `json:"hash"`
}
type UpsertAutoOrchCycleReportInput struct {
	MissionName    string
	EmployeeID     string
	CycleID        string
	IdempotencyKey string
	PayloadJSON    string
	ActorID        string
}

type UpsertBoardReviewTaskInput struct {
	MissionName        string            `json:"mission_name"`
	EmployeeID         string            `json:"employee_id"`
	RunID              string            `json:"run_id"`
	ReviewGate         string            `json:"review_gate"`
	IdempotencyKey     string            `json:"idempotency_key"`
	SourcePDDVersion   string            `json:"source_pdd_version"`
	Title              string            `json:"title"`
	Priority           string            `json:"priority"`
	ReviewTargetType   string            `json:"review_target_type"`
	ReviewTargetID     string            `json:"review_target_id"`
	ReviewInstructions string            `json:"review_instructions"`
	ArtifactLinks      map[string]string `json:"artifact_links"`
	GateResults        map[string]any    `json:"gate_results"`
	ActorType          string            `json:"actor_type"`
	ActorID            string            `json:"actor_id"`
}

type ApplyReviewDispositionInput struct {
	MissionName string `json:"mission_name"`
	RunID       string `json:"run_id"`
	ReviewGate  string `json:"review_gate"`
	Action      string `json:"action"`
	Reason      string `json:"reason"`
	ActorType   string `json:"actor_type"`
	ActorID     string `json:"actor_id"`
	ActorRole   string `json:"actor_role"`
}

type AuditRecordInput struct {
	Method       string
	Path         string
	Status       int
	ActorType    string
	ActorID      string
	Roles        []string
	AuthProvider string
	CreatedAt    time.Time
}

var ErrReviewDispositionConflict = errors.New("review task already has a different disposition")

type TaskTransitionInput struct {
	TaskID                    string
	Status                    string
	ActorType                 string
	ActorID                   string
	EventType                 string
	Reason                    string
	LeaseID                   string
	LeaseStatus               string
	ReleaseLease              bool
	ReviewTargetType          string
	ReviewTargetID            string
	ReviewMessageBody         string
	AcknowledgeReviewMessages bool
	Payload                   map[string]string
	// RequireActiveLease scopes the transition to a participant's exact
	// server-derived lease. When set, the task update and lease release both
	// require the same lease id/task/agent/machine, active status, and an
	// expiry after the transaction's write time. Legacy callers leave this
	// unset and retain their existing transition behavior.
	RequireActiveLease bool
	LeaseAgentID       string
	LeaseMachineID     string
}

type CreateAPITokenInput struct {
	AgentID string
	Token   string
}

var ErrNoTask = errors.New("no eligible task")
var ErrUnauthorized = errors.New("unauthorized")

// ErrParticipantLeaseNotActive is returned when a participant's exact lease
// binding is missing, expired, or no longer active at the transaction write.
var ErrParticipantLeaseNotActive = errors.New("participant lease is not active")

func Open(ctx context.Context, path string) (*Store, error) {
	db, err := sql.Open("sqlite", path+"?_pragma=foreign_keys(1)&_pragma=busy_timeout(5000)")
	if err != nil {
		return nil, err
	}
	db.SetMaxOpenConns(1)
	if err := db.PingContext(ctx); err != nil {
		db.Close()
		return nil, err
	}
	return &Store{db: db}, nil
}

func (s *Store) Close() error {
	return s.db.Close()
}

func (s *Store) RecordAudit(ctx context.Context, in AuditRecordInput) error {
	method := strings.TrimSpace(in.Method)
	path := strings.TrimSpace(in.Path)
	actorType := strings.TrimSpace(in.ActorType)
	actorID := strings.TrimSpace(in.ActorID)
	provider := strings.TrimSpace(in.AuthProvider)
	if method == "" || path == "" || actorType == "" || actorID == "" || provider == "" {
		return errors.New("audit method, path, identity, and provider are required")
	}
	if in.Status == 0 {
		in.Status = 200
	}
	roles, err := json.Marshal(in.Roles)
	if err != nil {
		return err
	}
	created := in.CreatedAt
	if created.IsZero() {
		created = time.Now().UTC()
	}
	_, err = s.db.ExecContext(ctx, `
INSERT INTO audit_events(id, method, path, status, actor_type, actor_id, roles_json, auth_provider, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)`, newID(), method, path, in.Status, actorType, actorID, string(roles), provider, created.Format(time.RFC3339Nano))
	return err
}

func (s *Store) ListAuditRecords(ctx context.Context, limit int) ([]domain.AuditRecord, error) {
	if limit <= 0 || limit > 500 {
		limit = 100
	}
	rows, err := s.db.QueryContext(ctx, `
SELECT id, method, path, status, actor_type, actor_id, roles_json, auth_provider, created_at
FROM audit_events ORDER BY created_at DESC, id DESC LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var records []domain.AuditRecord
	for rows.Next() {
		var record domain.AuditRecord
		var rolesJSON string
		var created timeText
		if err := rows.Scan(&record.ID, &record.Method, &record.Path, &record.Status, &record.ActorType, &record.ActorID, &rolesJSON, &record.AuthProvider, &created); err != nil {
			return nil, err
		}
		if err := json.Unmarshal([]byte(rolesJSON), &record.Roles); err != nil {
			return nil, err
		}
		record.CreatedAt = time.Time(created)
		records = append(records, record)
	}
	return records, rows.Err()
}

func (s *Store) Migrate(ctx context.Context) error {
	names, err := fs.Glob(migrationFiles, "migrations/*.sql")
	if err != nil {
		return err
	}
	sort.Strings(names)
	for _, name := range names {
		version := strings.TrimPrefix(name, "migrations/")
		applied, err := s.migrationApplied(ctx, version)
		if err != nil {
			return err
		}
		if applied {
			continue
		}
		sqlText, err := migrationFiles.ReadFile(name)
		if err != nil {
			return err
		}
		tx, err := s.db.BeginTx(ctx, nil)
		if err != nil {
			return err
		}
		if _, err := tx.ExecContext(ctx, string(sqlText)); err != nil {
			tx.Rollback()
			return fmt.Errorf("apply %s: %w", version, err)
		}
		if _, err := tx.ExecContext(ctx, `INSERT INTO schema_migrations(version) VALUES (?)`, version); err != nil {
			tx.Rollback()
			return err
		}
		if err := tx.Commit(); err != nil {
			return err
		}
	}
	return nil
}

func (s *Store) Backup(ctx context.Context, outPath string) error {
	outPath = strings.TrimSpace(outPath)
	if outPath == "" {
		return errors.New("backup output path is required")
	}
	if err := os.MkdirAll(filepath.Dir(outPath), 0o755); err != nil {
		return err
	}
	if _, err := os.Stat(outPath); err == nil {
		return fmt.Errorf("backup output already exists: %s", outPath)
	} else if !errors.Is(err, os.ErrNotExist) {
		return err
	}
	_, err := s.db.ExecContext(ctx, `VACUUM INTO ?`, outPath)
	return err
}

func (s *Store) migrationApplied(ctx context.Context, version string) (bool, error) {
	if _, err := s.db.ExecContext(ctx, `CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now')))`); err != nil {
		return false, err
	}
	var found int
	err := s.db.QueryRowContext(ctx, `SELECT COUNT(*) FROM schema_migrations WHERE version = ?`, version).Scan(&found)
	return found > 0, err
}

func (s *Store) RegisterMachine(ctx context.Context, in RegisterMachineInput) (domain.Machine, error) {
	now := nowText()
	capsJSON, err := jsonText(in.Capabilities)
	if err != nil {
		return domain.Machine{}, err
	}
	_, err = s.db.ExecContext(ctx, `
INSERT INTO machines(id, hostname, os, arch, status, last_seen_at, capabilities_json, created_at, updated_at)
VALUES (?, ?, ?, ?, 'online', ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
hostname = excluded.hostname,
os = excluded.os,
arch = excluded.arch,
status = 'online',
last_seen_at = excluded.last_seen_at,
capabilities_json = excluded.capabilities_json,
updated_at = excluded.updated_at`,
		in.ID, in.Hostname, in.OS, in.Arch, now, capsJSON, now, now)
	if err != nil {
		return domain.Machine{}, err
	}
	return s.GetMachine(ctx, in.ID)
}

func (s *Store) RegisterAgent(ctx context.Context, in RegisterAgentInput) (domain.Agent, error) {
	if in.Kind == "" {
		in.Kind = "shell"
	}
	if in.Role == "" {
		in.Role = "producer"
	}
	now := nowText()
	capsJSON, err := jsonText(in.Capabilities)
	if err != nil {
		return domain.Agent{}, err
	}
	_, err = s.db.ExecContext(ctx, `
INSERT INTO agents(id, machine_id, kind, role, status, last_seen_at, capabilities_json, created_at, updated_at)
VALUES (?, ?, ?, ?, 'idle', ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
machine_id = excluded.machine_id,
kind = excluded.kind,
role = excluded.role,
status = 'idle',
last_seen_at = excluded.last_seen_at,
capabilities_json = excluded.capabilities_json,
updated_at = excluded.updated_at`,
		in.ID, in.MachineID, in.Kind, in.Role, now, capsJSON, now, now)
	if err != nil {
		return domain.Agent{}, err
	}
	return s.GetAgent(ctx, in.ID)
}

func (s *Store) CreateAPIToken(ctx context.Context, in CreateAPITokenInput) (domain.APIToken, error) {
	if strings.TrimSpace(in.AgentID) == "" {
		return domain.APIToken{}, errors.New("agent_id is required")
	}
	if strings.TrimSpace(in.Token) == "" {
		return domain.APIToken{}, errors.New("token is required")
	}
	id := newID()
	now := nowText()
	_, err := s.db.ExecContext(ctx, `
INSERT INTO api_tokens(id, agent_id, token_hash, created_at)
VALUES (?, ?, ?, ?)`, id, in.AgentID, tokenHash(in.Token), now)
	if err != nil {
		return domain.APIToken{}, err
	}
	return s.GetAPIToken(ctx, id)
}

func (s *Store) GetAPIToken(ctx context.Context, id string) (domain.APIToken, error) {
	var token domain.APIToken
	var revoked sql.NullString
	err := s.db.QueryRowContext(ctx, `
SELECT id, agent_id, created_at, revoked_at
FROM api_tokens
WHERE id = ?`, id).Scan(&token.ID, &token.AgentID, (*timeText)(&token.CreatedAt), &revoked)
	if err != nil {
		return domain.APIToken{}, err
	}
	token.RevokedAt, err = parseNullTime(revoked)
	if err != nil {
		return domain.APIToken{}, err
	}
	return token, nil
}

func (s *Store) RevokeAPIToken(ctx context.Context, rawToken string) (domain.APIToken, error) {
	if strings.TrimSpace(rawToken) == "" {
		return domain.APIToken{}, errors.New("token is required")
	}
	now := nowText()
	result, err := s.db.ExecContext(ctx, `
UPDATE api_tokens
SET revoked_at = ?
WHERE token_hash = ? AND revoked_at IS NULL`, now, tokenHash(rawToken))
	if err != nil {
		return domain.APIToken{}, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return domain.APIToken{}, err
	}
	if affected != 1 {
		return domain.APIToken{}, sql.ErrNoRows
	}
	return s.apiTokenByHash(ctx, tokenHash(rawToken))
}

func (s *Store) ResolveAPIToken(ctx context.Context, rawToken string) (domain.AuthIdentity, error) {
	if strings.TrimSpace(rawToken) == "" {
		return domain.AuthIdentity{}, ErrUnauthorized
	}
	var agentID string
	err := s.db.QueryRowContext(ctx, `
SELECT agent_id
FROM api_tokens
WHERE token_hash = ? AND revoked_at IS NULL`, tokenHash(rawToken)).Scan(&agentID)
	if errors.Is(err, sql.ErrNoRows) {
		return domain.AuthIdentity{}, ErrUnauthorized
	}
	if err != nil {
		return domain.AuthIdentity{}, err
	}
	return domain.AuthIdentity{ActorType: "agent", ActorID: agentID}, nil
}

func (s *Store) apiTokenByHash(ctx context.Context, hash string) (domain.APIToken, error) {
	var token domain.APIToken
	var revoked sql.NullString
	err := s.db.QueryRowContext(ctx, `
SELECT id, agent_id, created_at, revoked_at
FROM api_tokens
WHERE token_hash = ?`, hash).Scan(&token.ID, &token.AgentID, (*timeText)(&token.CreatedAt), &revoked)
	if err != nil {
		return domain.APIToken{}, err
	}
	token.RevokedAt, err = parseNullTime(revoked)
	if err != nil {
		return domain.APIToken{}, err
	}
	return token, nil
}

func (s *Store) CreateTask(ctx context.Context, in CreateTaskInput) (domain.Task, error) {
	if strings.TrimSpace(in.Title) == "" {
		return domain.Task{}, errors.New("title is required")
	}
	if in.Priority == "" {
		in.Priority = "P2"
	}
	if in.ActorType == "" {
		in.ActorType = "human"
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return domain.Task{}, err
	}
	id, err := createTaskTx(ctx, tx, in, domain.TaskStatusReady, "created", "", "")
	if err != nil {
		tx.Rollback()
		return domain.Task{}, err
	}
	if err := tx.Commit(); err != nil {
		return domain.Task{}, err
	}
	return s.GetTask(ctx, id)
}

func createTaskTx(ctx context.Context, tx *sql.Tx, in CreateTaskInput, status, eventType, reviewTargetType, reviewTargetID string) (string, error) {
	now := nowText()
	id := newID()
	requiredJSON, err := jsonText(in.RequiredCapabilities)
	if err != nil {
		return "", err
	}
	machineJSON, err := jsonText(in.MachineConstraints)
	if err != nil {
		return "", err
	}
	var parentTaskID any
	var rootTaskID any
	if strings.TrimSpace(in.ParentTaskID) != "" {
		var parentRoot sql.NullString
		if err := tx.QueryRowContext(ctx, `SELECT root_task_id FROM tasks WHERE id = ?`, in.ParentTaskID).Scan(&parentRoot); err != nil {
			if errors.Is(err, sql.ErrNoRows) {
				return "", fmt.Errorf("parent task %q not found", in.ParentTaskID)
			}
			return "", err
		}
		parentTaskID = in.ParentTaskID
		if parentRoot.Valid && parentRoot.String != "" {
			rootTaskID = parentRoot.String
		} else {
			rootTaskID = in.ParentTaskID
		}
	}
	var displayNumber int64
	if err := tx.QueryRowContext(ctx, `SELECT COALESCE(MAX(display_number), 0) + 1 FROM tasks`).Scan(&displayNumber); err != nil {
		return "", err
	}
	_, err = tx.ExecContext(ctx, `
INSERT INTO tasks(id, display_number, title, mission, status, priority, priority_rank, created_by_actor_type, created_by_actor_id, assigned_agent_id, review_target_type, review_target_id, required_capabilities_json, machine_constraints_json, instructions, parent_task_id, root_task_id, project_id, created_at, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
		id, displayNumber, in.Title, in.Mission, status, in.Priority, priorityRank(in.Priority), in.ActorType, in.ActorID, nullString(in.AssignedAgentID), reviewTargetType, reviewTargetID, requiredJSON, machineJSON, in.Instructions, parentTaskID, rootTaskID, nullString(in.ProjectID), now, now)
	if err != nil {
		return "", err
	}
	for _, cap := range normalize(in.RequiredCapabilities) {
		if _, err := tx.ExecContext(ctx, `INSERT INTO task_required_capabilities(task_id, capability) VALUES (?, ?)`, id, cap); err != nil {
			return "", err
		}
	}
	for _, machine := range normalize(in.MachineConstraints) {
		if _, err := tx.ExecContext(ctx, `INSERT INTO task_machine_constraints(task_id, constraint_value) VALUES (?, ?)`, id, machine); err != nil {
			return "", err
		}
	}
	if err := appendEvent(ctx, tx, id, in.ActorType, in.ActorID, eventType, map[string]string{"title": in.Title, "parent_task_id": in.ParentTaskID}); err != nil {
		return "", err
	}
	if in.ParentTaskID != "" {
		if err := appendEvent(ctx, tx, in.ParentTaskID, in.ActorType, in.ActorID, "spawned", map[string]string{"child_task_id": id, "title": in.Title}); err != nil {
			return "", err
		}
	}
	if status == domain.TaskStatusReview && strings.TrimSpace(reviewTargetType) != "" && strings.TrimSpace(reviewTargetID) != "" {
		body := "Review requested for task " + id
		if _, err := tx.ExecContext(ctx, `
INSERT INTO messages(id, task_id, from_actor_type, from_actor_id, to_actor_type, to_actor_id, kind, body, requires_ack, status, created_at)
VALUES (?, ?, ?, ?, ?, ?, 'review_request', ?, 1, 'pending', ?)`,
			newID(), id, in.ActorType, in.ActorID, reviewTargetType, reviewTargetID, body, now); err != nil {
			return "", err
		}
	}
	return id, nil
}

func (s *Store) PollAndClaim(ctx context.Context, in PollInput) (domain.ClaimedTask, error) {
	if in.LeaseSeconds == 0 {
		in.LeaseSeconds = domain.DefaultLeaseSeconds
	}
	now := nowText()
	expires := time.Now().UTC().Add(time.Duration(in.LeaseSeconds) * time.Second).Format(time.RFC3339Nano)
	tx, err := s.db.BeginTx(ctx, &sql.TxOptions{})
	if err != nil {
		return domain.ClaimedTask{}, err
	}
	if _, err := tx.ExecContext(ctx, `UPDATE machines SET status = 'online', last_seen_at = ?, updated_at = ? WHERE id = ?`, now, now, in.MachineID); err != nil {
		tx.Rollback()
		return domain.ClaimedTask{}, err
	}
	if _, err := tx.ExecContext(ctx, `UPDATE agents SET status = 'polling', last_seen_at = ?, updated_at = ? WHERE id = ?`, now, now, in.AgentID); err != nil {
		tx.Rollback()
		return domain.ClaimedTask{}, err
	}
	taskID, err := findEligibleTask(ctx, tx, in)
	if err != nil {
		tx.Rollback()
		return domain.ClaimedTask{}, err
	}
	if taskID == "" {
		tx.Rollback()
		return domain.ClaimedTask{}, ErrNoTask
	}
	result, err := tx.ExecContext(ctx, `UPDATE tasks SET status = 'claimed', assigned_agent_id = COALESCE(assigned_agent_id, ?), updated_at = ? WHERE id = ? AND status = 'ready'`, in.AgentID, now, taskID)
	if err != nil {
		tx.Rollback()
		return domain.ClaimedTask{}, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		tx.Rollback()
		return domain.ClaimedTask{}, err
	}
	if affected != 1 {
		tx.Rollback()
		return domain.ClaimedTask{}, ErrNoTask
	}
	leaseID := newID()
	if _, err := tx.ExecContext(ctx, `
INSERT INTO leases(id, task_id, agent_id, machine_id, status, claimed_at, expires_at, heartbeat_at)
VALUES (?, ?, ?, ?, 'active', ?, ?, ?)`, leaseID, taskID, in.AgentID, in.MachineID, now, expires, now); err != nil {
		tx.Rollback()
		return domain.ClaimedTask{}, err
	}
	if _, err := tx.ExecContext(ctx, `UPDATE agents SET status = 'busy', updated_at = ? WHERE id = ?`, now, in.AgentID); err != nil {
		tx.Rollback()
		return domain.ClaimedTask{}, err
	}
	if err := appendEvent(ctx, tx, taskID, "agent", in.AgentID, "claimed", map[string]string{"lease_id": leaseID, "machine_id": in.MachineID}); err != nil {
		tx.Rollback()
		return domain.ClaimedTask{}, err
	}
	if err := tx.Commit(); err != nil {
		return domain.ClaimedTask{}, err
	}
	task, err := s.GetTask(ctx, taskID)
	if err != nil {
		return domain.ClaimedTask{}, err
	}
	lease, err := s.GetLease(ctx, leaseID)
	if err != nil {
		return domain.ClaimedTask{}, err
	}
	return domain.ClaimedTask{Task: task, Lease: lease}, nil
}

func (s *Store) RenewLease(ctx context.Context, leaseID string, seconds int) (domain.Lease, error) {
	if seconds == 0 {
		seconds = domain.DefaultLeaseSeconds
	}
	now := nowText()
	expires := time.Now().UTC().Add(time.Duration(seconds) * time.Second).Format(time.RFC3339Nano)
	result, err := s.db.ExecContext(ctx, `UPDATE leases SET heartbeat_at = ?, expires_at = ? WHERE id = ? AND status = 'active'`, now, expires, leaseID)
	if err != nil {
		return domain.Lease{}, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		return domain.Lease{}, err
	}
	if affected != 1 {
		return domain.Lease{}, sql.ErrNoRows
	}
	if _, err := s.db.ExecContext(ctx, `
UPDATE agents
SET status = 'busy', last_seen_at = ?, updated_at = ?
WHERE id = (SELECT agent_id FROM leases WHERE id = ? AND status = 'active')`, now, now, leaseID); err != nil {
		return domain.Lease{}, err
	}
	if _, err := s.db.ExecContext(ctx, `
UPDATE machines
SET status = 'online', last_seen_at = ?, updated_at = ?
WHERE id = (SELECT machine_id FROM leases WHERE id = ? AND status = 'active')`, now, now, leaseID); err != nil {
		return domain.Lease{}, err
	}
	return s.GetLease(ctx, leaseID)
}

// RenewParticipantLease renews only the exact participant lease binding. The
// lease id, task, agent, and machine predicates, including expiry, are part of
// the UPDATE that writes the new heartbeat and expiry. This keeps an expired
// status-active lease from being revived after a handler-side precheck.
func (s *Store) RenewParticipantLease(ctx context.Context, leaseID, taskID, agentID, machineID string, seconds int) (domain.Lease, error) {
	if strings.TrimSpace(leaseID) == "" || strings.TrimSpace(taskID) == "" || strings.TrimSpace(agentID) == "" || strings.TrimSpace(machineID) == "" {
		return domain.Lease{}, ErrParticipantLeaseNotActive
	}
	if seconds == 0 {
		seconds = domain.DefaultLeaseSeconds
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return domain.Lease{}, err
	}
	nowTime := time.Now().UTC()
	now := nowTime.Format(time.RFC3339Nano)
	expires := nowTime.Add(time.Duration(seconds) * time.Second).Format(time.RFC3339Nano)
	result, err := tx.ExecContext(ctx, `
UPDATE leases
SET heartbeat_at = ?, expires_at = ?
WHERE id = ?
  AND task_id = ?
  AND agent_id = ?
  AND machine_id = ?
  AND status = 'active'
  AND expires_at > ?`, now, expires, leaseID, taskID, agentID, machineID, now)
	if err != nil {
		_ = tx.Rollback()
		return domain.Lease{}, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		_ = tx.Rollback()
		return domain.Lease{}, err
	}
	if affected != 1 {
		_ = tx.Rollback()
		return domain.Lease{}, ErrParticipantLeaseNotActive
	}
	if _, err := tx.ExecContext(ctx, `
UPDATE agents
SET status = 'busy', last_seen_at = ?, updated_at = ?
WHERE id = ?`, now, now, agentID); err != nil {
		_ = tx.Rollback()
		return domain.Lease{}, err
	}
	if _, err := tx.ExecContext(ctx, `
UPDATE machines
SET status = 'online', last_seen_at = ?, updated_at = ?
WHERE id = ?`, now, now, machineID); err != nil {
		_ = tx.Rollback()
		return domain.Lease{}, err
	}
	if err := tx.Commit(); err != nil {
		return domain.Lease{}, err
	}
	return s.GetLease(ctx, leaseID)
}

// ActiveLeaseForParticipation returns the single active, unexpired lease whose
// task, agent, and machine all match the verified participant identity. It is
// the server-derived claim a participant transition must hold; no caller input
// can substitute a different lease.
func (s *Store) ActiveLeaseForParticipation(ctx context.Context, taskID, agentID, machineID string) (domain.Lease, error) {
	var l domain.Lease
	err := s.db.QueryRowContext(ctx, `
SELECT id, task_id, agent_id, machine_id, status, claimed_at, expires_at, heartbeat_at
FROM leases
WHERE task_id = ? AND agent_id = ? AND machine_id = ?
  AND status = 'active' AND expires_at > ?
ORDER BY claimed_at DESC
LIMIT 1`, taskID, agentID, machineID, nowText()).
		Scan(&l.ID, &l.TaskID, &l.AgentID, &l.MachineID, &l.Status, (*timeText)(&l.ClaimedAt), (*timeText)(&l.ExpiresAt), (*timeText)(&l.HeartbeatAt))
	return l, err
}

func (s *Store) ExpireStaleLeases(ctx context.Context) (int64, error) {
	now := nowText()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, err
	}
	rows, err := tx.QueryContext(ctx, `SELECT id, task_id FROM leases WHERE status = 'active' AND expires_at <= ?`, now)
	if err != nil {
		tx.Rollback()
		return 0, err
	}
	type staleLease struct {
		id     string
		taskID string
	}
	var stale []staleLease
	for rows.Next() {
		var item staleLease
		if err := rows.Scan(&item.id, &item.taskID); err != nil {
			rows.Close()
			tx.Rollback()
			return 0, err
		}
		stale = append(stale, item)
	}
	if err := rows.Close(); err != nil {
		tx.Rollback()
		return 0, err
	}
	for _, item := range stale {
		if _, err := tx.ExecContext(ctx, `UPDATE leases SET status = 'expired', released_at = ? WHERE id = ?`, now, item.id); err != nil {
			tx.Rollback()
			return 0, err
		}
		if _, err := tx.ExecContext(ctx, `UPDATE tasks SET status = 'ready', assigned_agent_id = NULL, updated_at = ? WHERE id = ?`, now, item.taskID); err != nil {
			tx.Rollback()
			return 0, err
		}
		if err := appendEvent(ctx, tx, item.taskID, "system", "lease-expirer", "lease_expired", map[string]string{"lease_id": item.id}); err != nil {
			tx.Rollback()
			return 0, err
		}
	}
	if err := tx.Commit(); err != nil {
		return 0, err
	}
	return int64(len(stale)), nil
}

// DecayPresence marks entities that have stopped reporting within window as
// offline. It deliberately affects only presence status; task and lease state
// remain governed by the existing stale-lease path.
func (s *Store) DecayPresence(ctx context.Context, window time.Duration) (machines int64, agents int64, err error) {
	if window <= 0 {
		return 0, 0, errors.New("presence window must be positive")
	}
	cutoff := time.Now().UTC().Add(-window).Format(time.RFC3339Nano)
	now := nowText()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return 0, 0, err
	}
	machineResult, err := tx.ExecContext(ctx, `
UPDATE machines
SET status = 'offline', updated_at = ?
WHERE status != 'offline' AND julianday(last_seen_at) < julianday(?)`, now, cutoff)
	if err != nil {
		tx.Rollback()
		return 0, 0, err
	}
	agentResult, err := tx.ExecContext(ctx, `
UPDATE agents
SET status = 'offline', updated_at = ?
WHERE status != 'offline' AND julianday(last_seen_at) < julianday(?)`, now, cutoff)
	if err != nil {
		tx.Rollback()
		return 0, 0, err
	}
	if err := tx.Commit(); err != nil {
		return 0, 0, err
	}
	machines, err = machineResult.RowsAffected()
	if err != nil {
		return 0, 0, err
	}
	agents, err = agentResult.RowsAffected()
	return machines, agents, err
}

func (s *Store) ApplyTaskTransition(ctx context.Context, in TaskTransitionInput) (domain.Task, error) {
	if strings.TrimSpace(in.TaskID) == "" {
		return domain.Task{}, errors.New("task_id is required")
	}
	if strings.TrimSpace(in.Status) == "" {
		return domain.Task{}, errors.New("status is required")
	}
	if strings.TrimSpace(in.ActorType) == "" {
		in.ActorType = "agent"
	}
	if strings.TrimSpace(in.EventType) == "" {
		in.EventType = "status_changed"
	}
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return domain.Task{}, err
	}
	// Compute one transaction timestamp and use it for both the task write and
	// every participant lease predicate. A lease that expires after the
	// handler's read but before this write therefore cannot pass the assertion.
	now := nowText()
	reviewTargetType := strings.TrimSpace(in.ReviewTargetType)
	reviewTargetID := strings.TrimSpace(in.ReviewTargetID)
	clearReviewTarget := in.Status != domain.TaskStatusReview
	updateQuery := `
UPDATE tasks
SET status = ?,
    review_target_type = CASE WHEN ? THEN '' WHEN ? != '' THEN ? ELSE review_target_type END,
    review_target_id = CASE WHEN ? THEN '' WHEN ? != '' THEN ? ELSE review_target_id END,
    updated_at = ?`

	updateArgs := []any{
		in.Status,
		clearReviewTarget, reviewTargetType, reviewTargetType,
		clearReviewTarget, reviewTargetID, reviewTargetID,
		now, in.TaskID}
	updateQuery += "\nWHERE id = ?"
	if in.RequireActiveLease {
		if strings.TrimSpace(in.LeaseID) == "" || strings.TrimSpace(in.LeaseAgentID) == "" || strings.TrimSpace(in.LeaseMachineID) == "" {
			_ = tx.Rollback()
			return domain.Task{}, ErrParticipantLeaseNotActive
		}
		updateQuery += `
  AND status = 'claimed'
  AND EXISTS (
    SELECT 1 FROM leases l
    WHERE l.id = ?
      AND l.task_id = tasks.id
      AND l.agent_id = ?
      AND l.machine_id = ?
      AND l.status = 'active'
      AND l.expires_at > ?
  )`
		updateArgs = append(updateArgs, in.LeaseID, in.LeaseAgentID, in.LeaseMachineID, now)
	}
	result, err := tx.ExecContext(ctx, updateQuery, updateArgs...)
	if err != nil {
		tx.Rollback()
		return domain.Task{}, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		tx.Rollback()
		return domain.Task{}, err
	}
	if affected != 1 {
		tx.Rollback()
		if in.RequireActiveLease {
			return domain.Task{}, ErrParticipantLeaseNotActive
		}
		return domain.Task{}, sql.ErrNoRows
	}
	if in.ReleaseLease {
		leaseStatus := in.LeaseStatus
		if leaseStatus == "" {
			leaseStatus = domain.LeaseStatusReleased
		}
		selectQuery := `SELECT agent_id FROM leases WHERE task_id = ? AND status = 'active'`
		selectArgs := []any{in.TaskID}
		if in.RequireActiveLease {
			selectQuery = `SELECT agent_id FROM leases
WHERE id = ? AND task_id = ? AND agent_id = ? AND machine_id = ?
  AND status = 'active' AND expires_at > ?`
			selectArgs = []any{in.LeaseID, in.TaskID, in.LeaseAgentID, in.LeaseMachineID, now}
		} else if strings.TrimSpace(in.LeaseID) != "" {
			selectQuery += ` AND id = ?`
			selectArgs = append(selectArgs, in.LeaseID)
		}
		rows, err := tx.QueryContext(ctx, selectQuery, selectArgs...)
		if err != nil {
			tx.Rollback()
			return domain.Task{}, err
		}
		agentIDs := map[string]bool{}
		for rows.Next() {
			var agentID string
			if err := rows.Scan(&agentID); err != nil {
				rows.Close()
				tx.Rollback()
				return domain.Task{}, err
			}
			agentIDs[agentID] = true
		}
		if err := rows.Close(); err != nil {
			tx.Rollback()
			return domain.Task{}, err
		}
		if err := rows.Err(); err != nil {
			tx.Rollback()
			return domain.Task{}, err
		}
		if (in.RequireActiveLease || strings.TrimSpace(in.LeaseID) != "") && len(agentIDs) == 0 {
			tx.Rollback()
			if in.RequireActiveLease {
				return domain.Task{}, ErrParticipantLeaseNotActive
			}
			return domain.Task{}, sql.ErrNoRows
		}
		query := `UPDATE leases SET status = ?, released_at = ? WHERE task_id = ? AND status = 'active'`
		args := []any{leaseStatus, now, in.TaskID}
		if in.RequireActiveLease {
			query = `UPDATE leases SET status = ?, released_at = ?
WHERE id = ? AND task_id = ? AND agent_id = ? AND machine_id = ?
  AND status = 'active' AND expires_at > ?`
			args = []any{leaseStatus, now, in.LeaseID, in.TaskID, in.LeaseAgentID, in.LeaseMachineID, now}
		} else if strings.TrimSpace(in.LeaseID) != "" {
			query += ` AND id = ?`
			args = append(args, in.LeaseID)
		}
		result, err := tx.ExecContext(ctx, query, args...)
		if err != nil {
			tx.Rollback()
			return domain.Task{}, err
		}
		if in.RequireActiveLease || strings.TrimSpace(in.LeaseID) != "" {
			affected, err := result.RowsAffected()
			if err != nil {
				tx.Rollback()
				return domain.Task{}, err
			}
			if affected != 1 {
				tx.Rollback()
				if in.RequireActiveLease {
					return domain.Task{}, ErrParticipantLeaseNotActive
				}
				return domain.Task{}, sql.ErrNoRows
			}
		}
		for agentID := range agentIDs {
			if _, err := tx.ExecContext(ctx, `UPDATE agents SET status = 'idle', updated_at = ? WHERE id = ? AND status = 'busy'`, now, agentID); err != nil {
				tx.Rollback()
				return domain.Task{}, err
			}
		}
	}
	if in.Status == domain.TaskStatusReview && reviewTargetType != "" && reviewTargetID != "" {
		if _, err := tx.ExecContext(ctx, `
UPDATE messages
SET status = 'acknowledged', acknowledged_at = ?
WHERE task_id = ?
  AND kind = 'review_request'
  AND status = 'pending'`, now, in.TaskID); err != nil {
			tx.Rollback()
			return domain.Task{}, err
		}
	}
	if in.Status == domain.TaskStatusReview && reviewTargetType != "" && reviewTargetID != "" {
		body := strings.TrimSpace(in.ReviewMessageBody)
		if body == "" {
			body = "Review requested for task " + in.TaskID
		}
		if _, err := tx.ExecContext(ctx, `
INSERT INTO messages(id, task_id, from_actor_type, from_actor_id, to_actor_type, to_actor_id, kind, body, requires_ack, status, created_at)
VALUES (?, ?, ?, ?, ?, ?, 'review_request', ?, 1, 'pending', ?)`,
			newID(), in.TaskID, in.ActorType, in.ActorID, reviewTargetType, reviewTargetID, body, now); err != nil {
			tx.Rollback()
			return domain.Task{}, err
		}
	}
	if in.AcknowledgeReviewMessages || in.Status != domain.TaskStatusReview {
		if _, err := tx.ExecContext(ctx, `
UPDATE messages
SET status = 'acknowledged', acknowledged_at = ?
WHERE task_id = ?
  AND kind = 'review_request'
  AND status = 'pending'`, now, in.TaskID); err != nil {
			tx.Rollback()
			return domain.Task{}, err
		}
	}
	payload := map[string]string{"status": in.Status}
	if in.Reason != "" {
		payload["reason"] = in.Reason
	}
	if in.LeaseID != "" {
		payload["lease_id"] = in.LeaseID
	}
	if reviewTargetType != "" {
		payload["review_target_type"] = reviewTargetType
	}
	if reviewTargetID != "" {
		payload["review_target_id"] = reviewTargetID
	}
	for key, value := range in.Payload {
		if value != "" {
			payload[key] = value
		}
	}
	if err := appendEvent(ctx, tx, in.TaskID, in.ActorType, in.ActorID, in.EventType, payload); err != nil {
		tx.Rollback()
		return domain.Task{}, err
	}
	if err := tx.Commit(); err != nil {
		return domain.Task{}, err
	}
	return s.GetTask(ctx, in.TaskID)
}

func findEligibleTask(ctx context.Context, tx *sql.Tx, in PollInput) (string, error) {
	args := []any{in.AgentID, nowText()}
	var capabilityPredicate string
	caps := normalize(in.Capabilities)
	if len(caps) == 0 {
		capabilityPredicate = `AND NOT EXISTS (SELECT 1 FROM task_required_capabilities trc WHERE trc.task_id = t.id)`
	} else {
		placeholders := make([]string, 0, len(caps))
		for _, cap := range caps {
			placeholders = append(placeholders, "?")
			args = append(args, cap)
		}
		capabilityPredicate = fmt.Sprintf(`
AND NOT EXISTS (
  SELECT 1 FROM task_required_capabilities trc
  WHERE trc.task_id = t.id
    AND trc.capability NOT IN (%s)
)`, strings.Join(placeholders, ","))
	}
	assignedPredicate := `AND (t.assigned_agent_id IS NULL OR t.assigned_agent_id = ?)`
	if in.AssignedOnly {
		assignedPredicate = `AND t.assigned_agent_id = ?`
	}
	args = append(args, in.MachineID)
	query := fmt.Sprintf(`
SELECT t.id
FROM tasks t
WHERE t.status = 'ready'
  %s
  AND (t.not_before_at IS NULL OR t.not_before_at <= ?)
  AND NOT EXISTS (SELECT 1 FROM leases l WHERE l.task_id = t.id AND l.status = 'active')
  %s
  AND NOT EXISTS (
    SELECT 1 FROM task_machine_constraints tmc
    WHERE tmc.task_id = t.id
      AND tmc.constraint_value != ?
  )
ORDER BY t.priority_rank, t.created_at
LIMIT 1`, assignedPredicate, capabilityPredicate)
	var taskID string
	err := tx.QueryRowContext(ctx, query, args...).Scan(&taskID)
	if errors.Is(err, sql.ErrNoRows) {
		return "", nil
	}
	return taskID, err
}

func (s *Store) GetMachine(ctx context.Context, id string) (domain.Machine, error) {
	var m domain.Machine
	var caps string
	err := s.db.QueryRowContext(ctx, `SELECT id, hostname, os, arch, status, capabilities_json, last_seen_at FROM machines WHERE id = ?`, id).
		Scan(&m.ID, &m.Hostname, &m.OS, &m.Arch, &m.Status, &caps, (*timeText)(&m.LastSeenAt))
	if err != nil {
		return domain.Machine{}, err
	}
	m.Capabilities = decodeList(caps)
	return m, nil
}

func (s *Store) GetAgent(ctx context.Context, id string) (domain.Agent, error) {
	var a domain.Agent
	var caps string
	err := s.db.QueryRowContext(ctx, `SELECT id, machine_id, kind, role, status, capabilities_json, last_seen_at FROM agents WHERE id = ?`, id).
		Scan(&a.ID, &a.MachineID, &a.Kind, &a.Role, &a.Status, &caps, (*timeText)(&a.LastSeenAt))
	if err != nil {
		return domain.Agent{}, err
	}
	a.Capabilities = decodeList(caps)
	return a, nil
}

func (s *Store) ListMachines(ctx context.Context) ([]domain.Machine, error) {
	ids, err := s.listIDs(ctx, `SELECT id FROM machines ORDER BY id`)
	if err != nil {
		return nil, err
	}
	machines := make([]domain.Machine, 0, len(ids))
	for _, id := range ids {
		machine, err := s.GetMachine(ctx, id)
		if err != nil {
			return nil, err
		}
		machines = append(machines, machine)
	}
	return machines, nil
}

func (s *Store) ListAgents(ctx context.Context) ([]domain.Agent, error) {
	ids, err := s.listIDs(ctx, `SELECT id FROM agents ORDER BY id`)
	if err != nil {
		return nil, err
	}
	agents := make([]domain.Agent, 0, len(ids))
	for _, id := range ids {
		agent, err := s.GetAgent(ctx, id)
		if err != nil {
			return nil, err
		}
		agents = append(agents, agent)
	}
	return agents, nil
}

func (s *Store) GetTask(ctx context.Context, id string) (domain.Task, error) {
	var t domain.Task
	var required string
	var machine string
	var assigned sql.NullString
	var reviewTargetType string
	var reviewTargetID string
	var parent sql.NullString
	var root sql.NullString
	var projectID sql.NullString
	err := s.db.QueryRowContext(ctx, `
SELECT id, display_number, title, mission, status, priority, priority_rank, assigned_agent_id, review_target_type, review_target_id, required_capabilities_json, machine_constraints_json, instructions, parent_task_id, root_task_id, project_id, created_at, updated_at
FROM tasks WHERE id = ?`, id).
		Scan(&t.ID, &t.DisplayNumber, &t.Title, &t.Mission, &t.Status, &t.Priority, &t.PriorityRank, &assigned, &reviewTargetType, &reviewTargetID, &required, &machine, &t.Instructions, &parent, &root, &projectID, (*timeText)(&t.CreatedAt), (*timeText)(&t.UpdatedAt))
	if err != nil {
		return domain.Task{}, err
	}
	t.AssignedAgentID = assigned.String
	t.ReviewTargetType = reviewTargetType
	t.ReviewTargetID = reviewTargetID
	t.ParentTaskID = parent.String
	t.RootTaskID = root.String
	t.ProjectID = projectID.String
	t.RequiredCapabilities = decodeList(required)
	t.MachineConstraints = decodeList(machine)
	return t, nil
}

func (s *Store) GetLease(ctx context.Context, id string) (domain.Lease, error) {
	var l domain.Lease
	err := s.db.QueryRowContext(ctx, `SELECT id, task_id, agent_id, machine_id, status, claimed_at, expires_at, heartbeat_at FROM leases WHERE id = ?`, id).
		Scan(&l.ID, &l.TaskID, &l.AgentID, &l.MachineID, &l.Status, (*timeText)(&l.ClaimedAt), (*timeText)(&l.ExpiresAt), (*timeText)(&l.HeartbeatAt))
	return l, err
}

func (s *Store) ListTasks(ctx context.Context, status string, projectID string) ([]domain.Task, error) {
	query := `SELECT id FROM tasks`
	args := []any{}
	conditions := []string{}
	if status != "" {
		conditions = append(conditions, `status = ?`)
		args = append(args, status)
	}
	if projectID != "" {
		conditions = append(conditions, `project_id = ?`)
		args = append(args, projectID)
	}
	if len(conditions) > 0 {
		query += ` WHERE ` + strings.Join(conditions, " AND ")
	}
	query += ` ORDER BY priority_rank, display_number`
	ids, err := s.listIDs(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	tasks := make([]domain.Task, 0, len(ids))
	for _, id := range ids {
		task, err := s.GetTask(ctx, id)
		if err != nil {
			return nil, err
		}
		tasks = append(tasks, task)
	}
	return tasks, nil
}

func (s *Store) ListReadyByPriority(ctx context.Context) ([]domain.CountByLabel, error) {
	rows, err := s.db.QueryContext(ctx, `
SELECT priority, COUNT(*)
FROM tasks
WHERE status = 'ready'
GROUP BY priority, priority_rank
ORDER BY priority_rank`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var counts []domain.CountByLabel
	for rows.Next() {
		var item domain.CountByLabel
		if err := rows.Scan(&item.Label, &item.Count); err != nil {
			return nil, err
		}
		counts = append(counts, item)
	}
	return counts, rows.Err()
}

func (s *Store) ListActiveLeases(ctx context.Context) ([]domain.LeaseObservation, error) {
	return s.listLeaseObservations(ctx, `l.status = 'active'`, nil)
}

func (s *Store) ListProjectStates(ctx context.Context, projectID string) ([]domain.ProjectState, error) {
	tasks, err := s.ListTasks(ctx, "", strings.TrimSpace(projectID))
	if err != nil {
		return nil, err
	}
	activeLeases, err := s.ListActiveLeases(ctx)
	if err != nil {
		return nil, err
	}
	activeByTask := make(map[string]domain.LeaseObservation, len(activeLeases))
	for _, observation := range activeLeases {
		activeByTask[observation.Task.ID] = observation
	}

	statesByProject := map[string]*domain.ProjectState{}
	for _, task := range tasks {
		if task.ProjectID == "" {
			continue
		}
		state := statesByProject[task.ProjectID]
		if state == nil {
			state = &domain.ProjectState{
				ProjectID:          task.ProjectID,
				ProjectStatus:      "completed",
				Mode:               "not-started",
				TaskCountsByStatus: map[string]int{},
				LastActivityAt:     task.UpdatedAt,
			}
			statesByProject[task.ProjectID] = state
		}
		state.TaskCountsByStatus[task.Status]++
		if task.UpdatedAt.After(state.LastActivityAt) {
			state.LastActivityAt = task.UpdatedAt
		}
		if !isTerminalTaskStatus(task.Status) {
			state.ProjectStatus = "active"
		}
		if shouldReplaceProjectTask(state.CurrentTask, task) {
			taskCopy := task
			state.CurrentTask = &taskCopy
			state.NextAction = task.Title
		}
		if observation, ok := activeByTask[task.ID]; ok {
			state.Host = observation.Lease.MachineID
			state.Agent = observation.Lease.AgentID
			if state.Mode != "waiting-on-operator" {
				state.Mode = "autonomous"
			}
		} else if state.Agent == "" && task.AssignedAgentID != "" {
			state.Agent = task.AssignedAgentID
		}
		if projectTaskNeedsHumanAttention(task) {
			state.NeedsHumanAttention = true
			if task.Status == domain.TaskStatusBlocked || task.Status == domain.TaskStatusWaiting || task.Status == domain.TaskStatusReview || task.Status == domain.TaskStatusFailed {
				state.BlockedOn = task.Title
			}
		}
		if task.Status == domain.TaskStatusBlocked {
			state.Blocked = true
		}
		if projectTaskNeedsHumanAttention(task) {
			state.Mode = "waiting-on-operator"
		}
	}

	states := make([]domain.ProjectState, 0, len(statesByProject))
	for _, state := range statesByProject {
		if state.ProjectStatus == "completed" {
			state.Mode = "completed"
		}
		states = append(states, *state)
	}
	sort.SliceStable(states, func(i, j int) bool {
		if states[i].ProjectStatus != states[j].ProjectStatus {
			return states[i].ProjectStatus == "active"
		}
		if !states[i].LastActivityAt.Equal(states[j].LastActivityAt) {
			return states[i].LastActivityAt.After(states[j].LastActivityAt)
		}
		return states[i].ProjectID < states[j].ProjectID
	})
	return states, nil
}

func isTerminalTaskStatus(status string) bool {
	return status == domain.TaskStatusDone || status == domain.TaskStatusCanceled
}

func projectTaskNeedsHumanAttention(task domain.Task) bool {
	if task.ReviewTargetType == "human" {
		return true
	}
	switch task.Status {
	case domain.TaskStatusReview, domain.TaskStatusBlocked, domain.TaskStatusFailed, domain.TaskStatusWaiting:
		return true
	default:
		return false
	}
}

func shouldReplaceProjectTask(current *domain.Task, candidate domain.Task) bool {
	if current == nil {
		return true
	}
	currentRank := projectTaskRank(current.Status)
	candidateRank := projectTaskRank(candidate.Status)
	if candidateRank != currentRank {
		return candidateRank < currentRank
	}
	if !candidate.UpdatedAt.Equal(current.UpdatedAt) {
		return candidate.UpdatedAt.After(current.UpdatedAt)
	}
	return candidate.DisplayNumber < current.DisplayNumber
}

func projectTaskRank(status string) int {
	switch status {
	case domain.TaskStatusBlocked, domain.TaskStatusFailed:
		return 0
	case domain.TaskStatusReview, domain.TaskStatusWaiting:
		return 1
	case domain.TaskStatusClaimed:
		return 2
	case domain.TaskStatusReady:
		return 3
	case domain.TaskStatusDone, domain.TaskStatusCanceled:
		return 5
	default:
		return 4
	}
}

func (s *Store) ListStaleLeases(ctx context.Context) ([]domain.LeaseObservation, error) {
	return s.listLeaseObservations(ctx, `l.status = 'active' AND l.expires_at <= ?`, []any{nowText()})
}

func (s *Store) listLeaseObservations(ctx context.Context, where string, args []any) ([]domain.LeaseObservation, error) {
	query := fmt.Sprintf(`
SELECT l.id
FROM leases l
JOIN tasks t ON t.id = l.task_id
WHERE %s
ORDER BY l.expires_at, t.priority_rank, t.display_number`, where)
	rows, err := s.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var leaseIDs []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		leaseIDs = append(leaseIDs, id)
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	now := time.Now().UTC()
	observations := make([]domain.LeaseObservation, 0, len(leaseIDs))
	for _, id := range leaseIDs {
		lease, err := s.GetLease(ctx, id)
		if err != nil {
			return nil, err
		}
		task, err := s.GetTask(ctx, lease.TaskID)
		if err != nil {
			return nil, err
		}
		observations = append(observations, domain.LeaseObservation{
			Lease:              lease,
			Task:               task,
			IsStale:            !lease.ExpiresAt.After(now),
			SecondsUntilExpiry: int64(time.Until(lease.ExpiresAt).Seconds()),
		})
	}
	return observations, nil
}

func (s *Store) ListHumanAttentionTasks(ctx context.Context) ([]domain.Task, error) {
	ids, err := s.listIDs(ctx, `
SELECT id
FROM tasks
WHERE status IN ('blocked', 'review', 'waiting')
ORDER BY priority_rank, display_number`)
	if err != nil {
		return nil, err
	}
	tasks := make([]domain.Task, 0, len(ids))
	for _, id := range ids {
		task, err := s.GetTask(ctx, id)
		if err != nil {
			return nil, err
		}
		tasks = append(tasks, task)
	}
	return tasks, nil
}

func (s *Store) GetTaskTree(ctx context.Context, id string) (domain.TaskTree, error) {
	task, err := s.GetTask(ctx, id)
	if err != nil {
		return domain.TaskTree{}, err
	}
	children, err := s.listChildTaskTrees(ctx, id)
	if err != nil {
		return domain.TaskTree{}, err
	}
	return domain.TaskTree{Task: task, Children: children}, nil
}

func (s *Store) listChildTaskTrees(ctx context.Context, parentID string) ([]domain.TaskTree, error) {
	rows, err := s.db.QueryContext(ctx, `SELECT id FROM tasks WHERE parent_task_id = ? ORDER BY display_number`, parentID)
	if err != nil {
		return nil, err
	}
	var childIDs []string
	for rows.Next() {
		var childID string
		if err := rows.Scan(&childID); err != nil {
			rows.Close()
			return nil, err
		}
		childIDs = append(childIDs, childID)
	}
	if err := rows.Close(); err != nil {
		return nil, err
	}
	if err := rows.Err(); err != nil {
		return nil, err
	}
	children := make([]domain.TaskTree, 0, len(childIDs))
	for _, childID := range childIDs {
		child, err := s.GetTaskTree(ctx, childID)
		if err != nil {
			return nil, err
		}
		children = append(children, child)
	}
	return children, nil
}

func (s *Store) CreateMessage(ctx context.Context, in CreateMessageInput) (domain.Message, error) {
	if strings.TrimSpace(in.FromActorType) == "" {
		in.FromActorType = "agent"
	}
	if strings.TrimSpace(in.ToActorType) == "" {
		return domain.Message{}, errors.New("to_actor_type is required")
	}
	if strings.TrimSpace(in.ToActorID) == "" {
		return domain.Message{}, errors.New("to_actor_id is required")
	}
	if strings.TrimSpace(in.Body) == "" {
		return domain.Message{}, errors.New("body is required")
	}
	if in.Kind == "" {
		in.Kind = "request"
	}
	id := newID()
	now := nowText()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return domain.Message{}, err
	}
	if _, err := tx.ExecContext(ctx, `
INSERT INTO messages(id, task_id, from_actor_type, from_actor_id, to_actor_type, to_actor_id, kind, body, requires_ack, status, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)`,
		id, nullString(in.TaskID), in.FromActorType, in.FromActorID, in.ToActorType, in.ToActorID, in.Kind, in.Body, boolInt(in.RequiresAck), now); err != nil {
		tx.Rollback()
		return domain.Message{}, err
	}
	if in.TaskID != "" {
		if err := appendEvent(ctx, tx, in.TaskID, in.FromActorType, in.FromActorID, "message_sent", map[string]string{"message_id": id, "to_actor_type": in.ToActorType, "to_actor_id": in.ToActorID, "kind": in.Kind}); err != nil {
			tx.Rollback()
			return domain.Message{}, err
		}
	}
	if err := tx.Commit(); err != nil {
		return domain.Message{}, err
	}
	return s.GetMessage(ctx, id)
}

func (s *Store) AcknowledgeMessage(ctx context.Context, id, actorType, actorID string) (domain.Message, error) {
	if strings.TrimSpace(actorType) == "" {
		actorType = "agent"
	}
	now := nowText()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return domain.Message{}, err
	}
	var taskID sql.NullString
	if err := tx.QueryRowContext(ctx, `SELECT task_id FROM messages WHERE id = ?`, id).Scan(&taskID); err != nil {
		tx.Rollback()
		return domain.Message{}, err
	}
	result, err := tx.ExecContext(ctx, `UPDATE messages SET status = 'acknowledged', acknowledged_at = ? WHERE id = ? AND status = 'pending'`, now, id)
	if err != nil {
		tx.Rollback()
		return domain.Message{}, err
	}
	affected, err := result.RowsAffected()
	if err != nil {
		tx.Rollback()
		return domain.Message{}, err
	}
	if affected != 1 {
		tx.Rollback()
		return domain.Message{}, sql.ErrNoRows
	}
	if taskID.Valid && taskID.String != "" {
		if err := appendEvent(ctx, tx, taskID.String, actorType, actorID, "message_acknowledged", map[string]string{"message_id": id}); err != nil {
			tx.Rollback()
			return domain.Message{}, err
		}
	}
	if err := tx.Commit(); err != nil {
		return domain.Message{}, err
	}
	return s.GetMessage(ctx, id)
}

func (s *Store) GetMessage(ctx context.Context, id string) (domain.Message, error) {
	row := s.db.QueryRowContext(ctx, `
SELECT id, task_id, from_actor_type, from_actor_id, to_actor_type, to_actor_id, kind, body, requires_ack, status, created_at, acknowledged_at, expires_at
FROM messages WHERE id = ?`, id)
	return scanMessage(row)
}

func (s *Store) ListInbox(ctx context.Context, toActorType, toActorID string) ([]domain.Message, error) {
	rows, err := s.db.QueryContext(ctx, `
SELECT id, task_id, from_actor_type, from_actor_id, to_actor_type, to_actor_id, kind, body, requires_ack, status, created_at, acknowledged_at, expires_at
FROM messages
WHERE status = 'pending'
  AND (expires_at IS NULL OR expires_at > ?)
  AND ((to_actor_type = ? AND to_actor_id = ?) OR to_actor_type = 'broadcast')
ORDER BY created_at`, nowText(), toActorType, toActorID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanMessages(rows)
}

// ListDirectInbox returns pending messages addressed to the exact actor. It
// excludes broadcast messages so a scoped participant only sees messages
// addressed to it.
func (s *Store) ListDirectInbox(ctx context.Context, toActorType, toActorID string) ([]domain.Message, error) {
	rows, err := s.db.QueryContext(ctx, `
SELECT id, task_id, from_actor_type, from_actor_id, to_actor_type, to_actor_id, kind, body, requires_ack, status, created_at, acknowledged_at, expires_at
FROM messages
WHERE status = 'pending'
  AND (expires_at IS NULL OR expires_at > ?)
  AND to_actor_type = ? AND to_actor_id = ?
ORDER BY created_at`, nowText(), toActorType, toActorID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanMessages(rows)
}

func (s *Store) ListTaskMessages(ctx context.Context, taskID string) ([]domain.Message, error) {
	rows, err := s.db.QueryContext(ctx, `
SELECT id, task_id, from_actor_type, from_actor_id, to_actor_type, to_actor_id, kind, body, requires_ack, status, created_at, acknowledged_at, expires_at
FROM messages
WHERE task_id = ?
ORDER BY created_at`, taskID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	return scanMessages(rows)
}

func (s *Store) AddArtifact(ctx context.Context, in AddArtifactInput) (domain.Artifact, error) {
	if strings.TrimSpace(in.TaskID) == "" {
		return domain.Artifact{}, errors.New("task_id is required")
	}
	if strings.TrimSpace(in.PathOrURL) == "" {
		return domain.Artifact{}, errors.New("path_or_url is required")
	}
	if in.Kind == "" {
		in.Kind = "file"
	}
	id := strings.TrimSpace(in.ID)
	if id == "" {
		id = newID()
	}
	now := nowText()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return domain.Artifact{}, err
	}
	if _, err := tx.ExecContext(ctx, `
INSERT INTO artifacts(id, task_id, agent_id, kind, path_or_url, summary, hash, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)`,
		id, in.TaskID, nullString(in.AgentID), in.Kind, in.PathOrURL, in.Summary, nullString(in.Hash), now); err != nil {
		tx.Rollback()
		return domain.Artifact{}, err
	}
	if err := appendEvent(ctx, tx, in.TaskID, "agent", in.AgentID, "artifact_added", map[string]string{"artifact_id": id, "kind": in.Kind, "path_or_url": in.PathOrURL}); err != nil {
		tx.Rollback()
		return domain.Artifact{}, err
	}
	if err := tx.Commit(); err != nil {
		return domain.Artifact{}, err
	}
	return s.GetArtifact(ctx, id)
}

func (s *Store) GetArtifact(ctx context.Context, id string) (domain.Artifact, error) {
	row := s.db.QueryRowContext(ctx, `
SELECT id, task_id, agent_id, kind, path_or_url, summary, hash, created_at
FROM artifacts WHERE id = ?`, id)
	return scanArtifact(row)
}

func (s *Store) ListArtifacts(ctx context.Context, taskID string) ([]domain.Artifact, error) {
	rows, err := s.db.QueryContext(ctx, `
SELECT id, task_id, agent_id, kind, path_or_url, summary, hash, created_at
FROM artifacts
WHERE task_id = ?
ORDER BY created_at`, taskID)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var artifacts []domain.Artifact
	for rows.Next() {
		artifact, err := scanArtifact(rows)
		if err != nil {
			return nil, err
		}
		artifacts = append(artifacts, artifact)
	}
	return artifacts, rows.Err()
}

func (s *Store) UpsertAutoOrchCycleReport(ctx context.Context, in UpsertAutoOrchCycleReportInput) (domain.AutoOrchCycleReport, error) {
	missionName := strings.TrimSpace(in.MissionName)
	employeeID := strings.TrimSpace(in.EmployeeID)
	cycleID := strings.TrimSpace(in.CycleID)
	if missionName == "" {
		return domain.AutoOrchCycleReport{}, errors.New("mission_name is required")
	}
	if cycleID == "" {
		return domain.AutoOrchCycleReport{}, errors.New("cycle_id is required")
	}
	if !json.Valid([]byte(in.PayloadJSON)) {
		return domain.AutoOrchCycleReport{}, errors.New("payload_json must be valid JSON")
	}
	now := nowText()
	_, err := s.db.ExecContext(ctx, `
INSERT INTO auto_orch_cycle_reports(mission_name, employee_id, cycle_id, idempotency_key, payload_json, actor_id, created_at, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(mission_name, cycle_id) DO UPDATE SET
  employee_id = excluded.employee_id,
  idempotency_key = excluded.idempotency_key,
  payload_json = excluded.payload_json,
  actor_id = excluded.actor_id,
  updated_at = excluded.updated_at`,
		missionName, employeeID, cycleID, strings.TrimSpace(in.IdempotencyKey), in.PayloadJSON, strings.TrimSpace(in.ActorID), now, now)
	if err != nil {
		return domain.AutoOrchCycleReport{}, err
	}
	return s.GetAutoOrchCycleReport(ctx, missionName, cycleID)
}

func (s *Store) UpsertBoardReviewTask(ctx context.Context, in UpsertBoardReviewTaskInput) (domain.BoardReviewTask, error) {
	missionName := strings.TrimSpace(in.MissionName)
	employeeID := strings.TrimSpace(in.EmployeeID)
	runID := strings.TrimSpace(in.RunID)
	reviewGate := strings.TrimSpace(in.ReviewGate)
	sourcePDDVersion := strings.TrimSpace(in.SourcePDDVersion)
	if missionName == "" || runID == "" || reviewGate == "" || sourcePDDVersion == "" {
	}
	if strings.TrimSpace(in.Title) == "" {
		return domain.BoardReviewTask{}, errors.New("title is required")
	}
	if in.Priority == "" {
		in.Priority = "P1"
	}
	if in.ActorType == "" {
		in.ActorType = "agent"
	}
	artifactJSON, err := json.Marshal(in.ArtifactLinks)
	if err != nil {
		return domain.BoardReviewTask{}, err
	}
	gateJSON, err := json.Marshal(in.GateResults)
	if err != nil {
		return domain.BoardReviewTask{}, err
	}
	now := nowText()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return domain.BoardReviewTask{}, err
	}
	var taskID string
	err = tx.QueryRowContext(ctx, `
SELECT task_id FROM board_review_tasks
WHERE mission_name = ? AND run_id = ? AND review_gate = ?`, missionName, runID, reviewGate).Scan(&taskID)
	if errors.Is(err, sql.ErrNoRows) {
		taskID, err = createTaskTx(ctx, tx, CreateTaskInput{
			Title:        in.Title,
			Mission:      missionName,
			Priority:     in.Priority,
			Instructions: in.ReviewInstructions,
			ActorType:    in.ActorType,
			ActorID:      in.ActorID,
		}, domain.TaskStatusReview, "review_requested", in.ReviewTargetType, in.ReviewTargetID)
		if err != nil {
			tx.Rollback()
			return domain.BoardReviewTask{}, err
		}
		_, err = tx.ExecContext(ctx, `
INSERT INTO board_review_tasks(task_id, mission_name, employee_id, run_id, review_gate, idempotency_key, source_pdd_version, artifact_links_json, gate_results_json, review_instructions, created_at, updated_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`,
			taskID, missionName, employeeID, runID, reviewGate, strings.TrimSpace(in.IdempotencyKey), sourcePDDVersion, string(artifactJSON), string(gateJSON), in.ReviewInstructions, now, now)
		if err != nil {
			tx.Rollback()
			return domain.BoardReviewTask{}, err
		}
	} else if err != nil {
		tx.Rollback()
		return domain.BoardReviewTask{}, err
	} else {
		_, err = tx.ExecContext(ctx, `
UPDATE tasks
SET title = ?, priority = ?, priority_rank = ?, instructions = ?, review_target_type = ?, review_target_id = ?, updated_at = ?
WHERE id = ?`,
			in.Title, in.Priority, priorityRank(in.Priority), in.ReviewInstructions, in.ReviewTargetType, in.ReviewTargetID, now, taskID)
		if err != nil {
			tx.Rollback()
			return domain.BoardReviewTask{}, err
		}
		_, err = tx.ExecContext(ctx, `
UPDATE board_review_tasks
SET employee_id = ?, idempotency_key = ?, source_pdd_version = ?, artifact_links_json = ?, gate_results_json = ?, review_instructions = ?, updated_at = ?
WHERE task_id = ?`,
			employeeID, strings.TrimSpace(in.IdempotencyKey), sourcePDDVersion, string(artifactJSON), string(gateJSON), in.ReviewInstructions, now, taskID)
		if err != nil {
			tx.Rollback()
			return domain.BoardReviewTask{}, err
		}
	}
	if err := tx.Commit(); err != nil {
		return domain.BoardReviewTask{}, err
	}
	return s.GetBoardReviewTask(ctx, missionName, runID, reviewGate)
}

func (s *Store) GetBoardReviewTask(ctx context.Context, missionName, runID, reviewGate string) (domain.BoardReviewTask, error) {
	missionName = strings.TrimSpace(missionName)
	runID = strings.TrimSpace(runID)
	reviewGate = strings.TrimSpace(reviewGate)
	var taskID, employeeID, idempotencyKey, sourcePDDVersion, artifactJSON, gateJSON, instructions string
	if err := s.db.QueryRowContext(ctx, `
SELECT task_id, employee_id, idempotency_key, source_pdd_version, artifact_links_json, gate_results_json, review_instructions
FROM board_review_tasks
WHERE mission_name = ? AND run_id = ? AND review_gate = ?`, missionName, runID, reviewGate).
		Scan(&taskID, &employeeID, &idempotencyKey, &sourcePDDVersion, &artifactJSON, &gateJSON, &instructions); err != nil {
		return domain.BoardReviewTask{}, err
	}
	task, err := s.GetTask(ctx, taskID)
	if err != nil {
		return domain.BoardReviewTask{}, err
	}
	result := domain.BoardReviewTask{
		Task:               task,
		EmployeeID:         employeeID,
		MissionName:        missionName,
		RunID:              runID,
		ReviewGate:         reviewGate,
		IdempotencyKey:     idempotencyKey,
		SourcePDDVersion:   sourcePDDVersion,
		ArtifactLinks:      map[string]string{},
		GateResults:        map[string]any{},
		ReviewInstructions: instructions,
	}
	var disposition domain.ReviewDisposition
	var created timeText
	err = s.db.QueryRowContext(ctx, `
SELECT id, task_id, mission_name, run_id, review_gate, action, reason, actor_type, actor_id, actor_role, created_at
FROM board_review_dispositions WHERE task_id = ?`, taskID).
		Scan(&disposition.ID, &disposition.TaskID, &disposition.MissionName, &disposition.RunID, &disposition.ReviewGate, &disposition.Action, &disposition.Reason, &disposition.ActorType, &disposition.ActorID, &disposition.ActorRole, &created)
	if err == nil {
		disposition.CreatedAt = time.Time(created)
		result.Disposition = &disposition
	} else if !errors.Is(err, sql.ErrNoRows) {
		return domain.BoardReviewTask{}, err
	}
	return result, nil
}

func (s *Store) ListAssignedBoardReviewTasks(ctx context.Context, targetType, targetID string) ([]domain.BoardReviewTask, error) {
	rows, err := s.db.QueryContext(ctx, `
SELECT br.mission_name, br.run_id, br.review_gate
FROM board_review_tasks br
JOIN tasks t ON t.id = br.task_id
WHERE t.status = ? AND t.review_target_type = ? AND t.review_target_id = ?
ORDER BY t.priority_rank, t.display_number`, domain.TaskStatusReview, strings.TrimSpace(targetType), strings.TrimSpace(targetID))
	if err != nil {
		return nil, err
	}
	type reviewRef struct {
		missionName string
		runID       string
		reviewGate  string
	}
	var refs []reviewRef
	for rows.Next() {
		var ref reviewRef
		if err := rows.Scan(&ref.missionName, &ref.runID, &ref.reviewGate); err != nil {
			rows.Close()
			return nil, err
		}
		refs = append(refs, ref)
	}
	if err := rows.Err(); err != nil {
		rows.Close()
		return nil, err
	}
	if err := rows.Close(); err != nil {
		return nil, err
	}

	result := make([]domain.BoardReviewTask, 0, len(refs))
	for _, ref := range refs {
		task, err := s.GetBoardReviewTask(ctx, ref.missionName, ref.runID, ref.reviewGate)
		if err != nil {
			return nil, err
		}
		result = append(result, task)
	}
	return result, nil
}

func (s *Store) ApplyReviewDisposition(ctx context.Context, in ApplyReviewDispositionInput) (domain.BoardReviewTask, error) {
	missionName := strings.TrimSpace(in.MissionName)
	runID := strings.TrimSpace(in.RunID)
	reviewGate := strings.TrimSpace(in.ReviewGate)
	action := strings.TrimSpace(in.Action)
	if missionName == "" || runID == "" || reviewGate == "" {
		return domain.BoardReviewTask{}, errors.New("mission_name, run_id, and review_gate are required")
	}
	if action != "approve" && action != "send_back" {
		return domain.BoardReviewTask{}, errors.New("action must be approve or send_back")
	}
	if in.ActorType != "human" || (in.ActorRole != "operator" && in.ActorRole != "admin") || strings.TrimSpace(in.ActorID) == "" {
		return domain.BoardReviewTask{}, errors.New("review disposition requires an authenticated human operator or admin")
	}
	now := nowText()
	tx, err := s.db.BeginTx(ctx, nil)
	if err != nil {
		return domain.BoardReviewTask{}, err
	}
	var taskID string
	if err := tx.QueryRowContext(ctx, `
SELECT task_id FROM board_review_tasks
WHERE mission_name = ? AND run_id = ? AND review_gate = ?`, missionName, runID, reviewGate).Scan(&taskID); err != nil {
		tx.Rollback()
		return domain.BoardReviewTask{}, err
	}
	var existing domain.ReviewDisposition
	var existingCreated timeText
	err = tx.QueryRowContext(ctx, `
SELECT id, task_id, mission_name, run_id, review_gate, action, reason, actor_type, actor_id, actor_role, created_at
FROM board_review_dispositions WHERE task_id = ?`, taskID).
		Scan(&existing.ID, &existing.TaskID, &existing.MissionName, &existing.RunID, &existing.ReviewGate, &existing.Action, &existing.Reason, &existing.ActorType, &existing.ActorID, &existing.ActorRole, &existingCreated)
	if err == nil {
		existing.CreatedAt = time.Time(existingCreated)
		if existing.Action != action || existing.Reason != strings.TrimSpace(in.Reason) || existing.ActorID != in.ActorID {
			tx.Rollback()
			return domain.BoardReviewTask{}, ErrReviewDispositionConflict
		}
		if err := tx.Commit(); err != nil {
			return domain.BoardReviewTask{}, err
		}
		return s.GetBoardReviewTask(ctx, missionName, runID, reviewGate)
	} else if !errors.Is(err, sql.ErrNoRows) {
		tx.Rollback()
		return domain.BoardReviewTask{}, err
	}
	var status string
	if err := tx.QueryRowContext(ctx, `SELECT status FROM tasks WHERE id = ?`, taskID).Scan(&status); err != nil {
		tx.Rollback()
		return domain.BoardReviewTask{}, err
	}
	if status != domain.TaskStatusReview {
		tx.Rollback()
		return domain.BoardReviewTask{}, fmt.Errorf("review task %s is %s, want review", taskID, status)
	}
	newStatus := domain.TaskStatusDone
	eventType := "review_approved"
	if action == "send_back" {
		newStatus = domain.TaskStatusReady
		eventType = "review_sent_back"
	}
	receiptID := newID()
	reason := strings.TrimSpace(in.Reason)
	if _, err := tx.ExecContext(ctx, `
UPDATE tasks SET status = ?, review_target_type = '', review_target_id = '', updated_at = ? WHERE id = ?`, newStatus, now, taskID); err != nil {
		tx.Rollback()
		return domain.BoardReviewTask{}, err
	}
	if _, err := tx.ExecContext(ctx, `
UPDATE messages SET status = 'acknowledged', acknowledged_at = ?
WHERE task_id = ? AND kind = 'review_request' AND status = 'pending'`, now, taskID); err != nil {
		tx.Rollback()
		return domain.BoardReviewTask{}, err
	}
	if err := appendEvent(ctx, tx, taskID, in.ActorType, in.ActorID, eventType, map[string]string{
		"action": action, "reason": reason, "actor_role": in.ActorRole, "receipt_id": receiptID,
	}); err != nil {
		tx.Rollback()
		return domain.BoardReviewTask{}, err
	}
	if _, err := tx.ExecContext(ctx, `
INSERT INTO board_review_dispositions(id, task_id, mission_name, run_id, review_gate, action, reason, actor_type, actor_id, actor_role, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)`, receiptID, taskID, missionName, runID, reviewGate, action, reason, in.ActorType, in.ActorID, in.ActorRole, now); err != nil {
		tx.Rollback()
		return domain.BoardReviewTask{}, err
	}
	if err := tx.Commit(); err != nil {
		return domain.BoardReviewTask{}, err
	}
	return s.GetBoardReviewTask(ctx, missionName, runID, reviewGate)
}

func (s *Store) GetAutoOrchCycleReport(ctx context.Context, missionName, cycleID string) (domain.AutoOrchCycleReport, error) {
	row := s.db.QueryRowContext(ctx, `
SELECT mission_name, employee_id, cycle_id, idempotency_key, payload_json, actor_id, created_at, updated_at
FROM auto_orch_cycle_reports
WHERE mission_name = ? AND cycle_id = ?`, strings.TrimSpace(missionName), strings.TrimSpace(cycleID))
	return scanAutoOrchCycleReport(row)
}

func (s *Store) ListAutoOrchCycleReports(ctx context.Context, missionName string, limit int) ([]domain.AutoOrchCycleReport, error) {
	if limit <= 0 || limit > 100 {
		limit = 50
	}
	query := `
SELECT mission_name, employee_id, cycle_id, idempotency_key, payload_json, actor_id, created_at, updated_at
FROM auto_orch_cycle_reports`
	args := []any{}
	if strings.TrimSpace(missionName) != "" {
		query += ` WHERE mission_name = ?`
		args = append(args, strings.TrimSpace(missionName))
	}
	query += ` ORDER BY updated_at DESC, mission_name, cycle_id DESC LIMIT ?`
	args = append(args, limit)

	rows, err := s.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()

	var reports []domain.AutoOrchCycleReport
	for rows.Next() {
		report, err := scanAutoOrchCycleReport(rows)
		if err != nil {
			return nil, err
		}
		reports = append(reports, report)
	}
	return reports, rows.Err()
}

func (s *Store) ListEvents(ctx context.Context, taskID string) ([]domain.TaskEvent, error) {
	query := `
SELECT id, task_id, actor_type, actor_id, event_type, payload_json, created_at
FROM task_events`
	args := []any{}
	if taskID != "" {
		query += ` WHERE task_id = ?`
		args = append(args, taskID)
	}
	query += ` ORDER BY created_at`
	rows, err := s.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var events []domain.TaskEvent
	for rows.Next() {
		var event domain.TaskEvent
		var task sql.NullString
		if err := rows.Scan(&event.ID, &task, &event.ActorType, &event.ActorID, &event.EventType, &event.PayloadJSON, (*timeText)(&event.CreatedAt)); err != nil {
			return nil, err
		}
		event.TaskID = task.String
		events = append(events, event)
	}
	return events, rows.Err()
}

func (s *Store) ListRecentEvents(ctx context.Context, limit int) ([]domain.TaskEvent, error) {
	if limit <= 0 {
		limit = 20
	}
	rows, err := s.db.QueryContext(ctx, `
SELECT id, task_id, actor_type, actor_id, event_type, payload_json, created_at
FROM task_events
ORDER BY created_at DESC
LIMIT ?`, limit)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var events []domain.TaskEvent
	for rows.Next() {
		var event domain.TaskEvent
		var task sql.NullString
		if err := rows.Scan(&event.ID, &task, &event.ActorType, &event.ActorID, &event.EventType, &event.PayloadJSON, (*timeText)(&event.CreatedAt)); err != nil {
			return nil, err
		}
		event.TaskID = task.String
		events = append(events, event)
	}
	return events, rows.Err()
}

func (s *Store) Status(ctx context.Context) (domain.StatusSummary, error) {
	var summary domain.StatusSummary
	err := s.db.QueryRowContext(ctx, `
SELECT
  COALESCE(SUM(CASE WHEN status = 'ready' THEN 1 ELSE 0 END), 0),
  COALESCE(SUM(CASE WHEN status = 'claimed' THEN 1 ELSE 0 END), 0),
  COALESCE(SUM(CASE WHEN status = 'waiting' THEN 1 ELSE 0 END), 0),
  COALESCE(SUM(CASE WHEN status = 'blocked' THEN 1 ELSE 0 END), 0),
  COALESCE(SUM(CASE WHEN status = 'review' THEN 1 ELSE 0 END), 0),
  COALESCE(SUM(CASE WHEN status = 'failed' THEN 1 ELSE 0 END), 0),
  COALESCE(SUM(CASE WHEN status = 'done' THEN 1 ELSE 0 END), 0)
FROM tasks`).Scan(&summary.ReadyTasks, &summary.ClaimedTasks, &summary.WaitingTasks, &summary.BlockedTasks, &summary.ReviewTasks, &summary.FailedTasks, &summary.DoneTasks)
	if err != nil {
		return domain.StatusSummary{}, err
	}
	if err := s.db.QueryRowContext(ctx, `SELECT COUNT(*) FROM leases WHERE status = 'active' AND expires_at <= ?`, nowText()).Scan(&summary.StaleLeases); err != nil {
		return domain.StatusSummary{}, err
	}
	if err := s.db.QueryRowContext(ctx, `SELECT COUNT(*) FROM machines WHERE status = 'online'`).Scan(&summary.OnlineMachines); err != nil {
		return domain.StatusSummary{}, err
	}
	if err := s.db.QueryRowContext(ctx, `SELECT COUNT(*) FROM agents WHERE status IN ('idle', 'polling', 'busy')`).Scan(&summary.OnlineAgents); err != nil {
		return domain.StatusSummary{}, err
	}
	return summary, nil
}

func (s *Store) Snapshot(ctx context.Context) (domain.BoardSnapshot, error) {
	status, err := s.Status(ctx)
	if err != nil {
		return domain.BoardSnapshot{}, err
	}
	machines, err := s.ListMachines(ctx)
	if err != nil {
		return domain.BoardSnapshot{}, err
	}
	agents, err := s.ListAgents(ctx)
	if err != nil {
		return domain.BoardSnapshot{}, err
	}
	readyByPriority, err := s.ListReadyByPriority(ctx)
	if err != nil {
		return domain.BoardSnapshot{}, err
	}
	activeLeases, err := s.ListActiveLeases(ctx)
	if err != nil {
		return domain.BoardSnapshot{}, err
	}
	staleLeases, err := s.ListStaleLeases(ctx)
	if err != nil {
		return domain.BoardSnapshot{}, err
	}
	humanAttention, err := s.ListHumanAttentionTasks(ctx)
	if err != nil {
		return domain.BoardSnapshot{}, err
	}
	recentEvents, err := s.ListRecentEvents(ctx, 20)
	if err != nil {
		return domain.BoardSnapshot{}, err
	}
	return domain.BoardSnapshot{
		GeneratedAt:     time.Now().UTC(),
		Status:          status,
		Machines:        machines,
		Agents:          agents,
		ReadyByPriority: readyByPriority,
		ActiveLeases:    activeLeases,
		StaleLeases:     staleLeases,
		HumanAttention:  humanAttention,
		RecentEvents:    recentEvents,
	}, nil
}

type scanner interface {
	Scan(dest ...any) error
}

func scanMessage(row scanner) (domain.Message, error) {
	var msg domain.Message
	var task sql.NullString
	var ack sql.NullString
	var expires sql.NullString
	var requiresAck int
	if err := row.Scan(&msg.ID, &task, &msg.FromActorType, &msg.FromActorID, &msg.ToActorType, &msg.ToActorID, &msg.Kind, &msg.Body, &requiresAck, &msg.Status, (*timeText)(&msg.CreatedAt), &ack, &expires); err != nil {
		return domain.Message{}, err
	}
	msg.TaskID = task.String
	msg.RequiresAck = requiresAck != 0
	if ack.Valid && ack.String != "" {
		parsed, err := time.Parse(time.RFC3339Nano, ack.String)
		if err != nil {
			return domain.Message{}, err
		}
		msg.AcknowledgedAt = &parsed
	}
	if expires.Valid && expires.String != "" {
		parsed, err := time.Parse(time.RFC3339Nano, expires.String)
		if err != nil {
			return domain.Message{}, err
		}
		msg.ExpiresAt = &parsed
	}
	return msg, nil
}

func scanMessages(rows *sql.Rows) ([]domain.Message, error) {
	var messages []domain.Message
	for rows.Next() {
		msg, err := scanMessage(rows)
		if err != nil {
			return nil, err
		}
		messages = append(messages, msg)
	}
	return messages, rows.Err()
}

func scanArtifact(row scanner) (domain.Artifact, error) {
	var artifact domain.Artifact
	var agent sql.NullString
	var hash sql.NullString
	if err := row.Scan(&artifact.ID, &artifact.TaskID, &agent, &artifact.Kind, &artifact.PathOrURL, &artifact.Summary, &hash, (*timeText)(&artifact.CreatedAt)); err != nil {
		return domain.Artifact{}, err
	}
	artifact.AgentID = agent.String
	artifact.Hash = hash.String
	return artifact, nil
}

func scanAutoOrchCycleReport(row scanner) (domain.AutoOrchCycleReport, error) {
	var report domain.AutoOrchCycleReport
	if err := row.Scan(&report.MissionName, &report.EmployeeID, &report.CycleID, &report.IdempotencyKey, &report.PayloadJSON, &report.ActorID, (*timeText)(&report.CreatedAt), (*timeText)(&report.UpdatedAt)); err != nil {
		return domain.AutoOrchCycleReport{}, err
	}
	return report, nil
}

func (s *Store) listIDs(ctx context.Context, query string, args ...any) ([]string, error) {
	rows, err := s.db.QueryContext(ctx, query, args...)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	var ids []string
	for rows.Next() {
		var id string
		if err := rows.Scan(&id); err != nil {
			return nil, err
		}
		ids = append(ids, id)
	}
	return ids, rows.Err()
}

func appendEvent(ctx context.Context, tx *sql.Tx, taskID, actorType, actorID, eventType string, payload any) error {
	payloadJSON, err := json.Marshal(payload)
	if err != nil {
		return err
	}
	_, err = tx.ExecContext(ctx, `
INSERT INTO task_events(id, task_id, actor_type, actor_id, event_type, payload_json, created_at)
VALUES (?, ?, ?, ?, ?, ?, ?)`, newID(), taskID, actorType, actorID, eventType, string(payloadJSON), nowText())
	return err
}

func priorityRank(priority string) int {
	switch strings.ToUpper(priority) {
	case "P0":
		return 0
	case "P1":
		return 1
	case "P3":
		return 3
	default:
		return 2
	}
}

func normalize(values []string) []string {
	seen := map[string]bool{}
	var out []string
	for _, value := range values {
		value = strings.ToLower(strings.TrimSpace(value))
		if value == "" || seen[value] {
			continue
		}
		seen[value] = true
		out = append(out, value)
	}
	sort.Strings(out)
	return out
}

func jsonText(values []string) (string, error) {
	data, err := json.Marshal(normalize(values))
	return string(data), err
}

func decodeList(text string) []string {
	var values []string
	if err := json.Unmarshal([]byte(text), &values); err != nil {
		return []string{}
	}
	if values == nil {
		return []string{}
	}
	return values
}

func nullString(value string) any {
	value = strings.TrimSpace(value)
	if value == "" {
		return nil
	}
	return value
}

func boolInt(value bool) int {
	if value {
		return 1
	}
	return 0
}

func nowText() string {
	return time.Now().UTC().Format(time.RFC3339Nano)
}

func tokenHash(rawToken string) string {
	sum := sha256.Sum256([]byte(rawToken))
	return hex.EncodeToString(sum[:])
}

func parseNullTime(value sql.NullString) (*time.Time, error) {
	if !value.Valid || value.String == "" {
		return nil, nil
	}
	parsed, err := time.Parse(time.RFC3339Nano, value.String)
	if err != nil {
		return nil, err
	}
	return &parsed, nil
}

func newID() string {
	var b [16]byte
	if _, err := rand.Read(b[:]); err != nil {
		panic(err)
	}
	b[6] = (b[6] & 0x0f) | 0x40
	b[8] = (b[8] & 0x3f) | 0x80
	encoded := hex.EncodeToString(b[:])
	return encoded[0:8] + "-" + encoded[8:12] + "-" + encoded[12:16] + "-" + encoded[16:20] + "-" + encoded[20:32]
}

func NewID() string {
	return newID()
}

type timeText time.Time

func (t *timeText) Scan(value any) error {
	switch v := value.(type) {
	case string:
		parsed, err := time.Parse(time.RFC3339Nano, v)
		if err != nil {
			return err
		}
		*(*time.Time)(t) = parsed
		return nil
	case []byte:
		return t.Scan(string(v))
	case nil:
		*(*time.Time)(t) = time.Time{}
		return nil
	default:
		return fmt.Errorf("unsupported time value %T", value)
	}
}
