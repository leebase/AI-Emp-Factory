package domain

import "time"

const (
	TaskStatusReady    = "ready"
	TaskStatusClaimed  = "claimed"
	TaskStatusDone     = "done"
	TaskStatusBlocked  = "blocked"
	TaskStatusFailed   = "failed"
	TaskStatusReview   = "review"
	TaskStatusWaiting  = "waiting"
	TaskStatusCanceled = "cancelled"

	LeaseStatusActive    = "active"
	LeaseStatusExpired   = "expired"
	LeaseStatusReleased  = "released"
	LeaseStatusCompleted = "completed"
	LeaseStatusCanceled  = "cancelled"

	MessageStatusPending      = "pending"
	MessageStatusAcknowledged = "acknowledged"

	DefaultLeaseSeconds = 900
)

type Machine struct {
	ID           string    `json:"id"`
	Hostname     string    `json:"hostname"`
	OS           string    `json:"os"`
	Arch         string    `json:"arch"`
	Status       string    `json:"status"`
	Capabilities []string  `json:"capabilities"`
	LastSeenAt   time.Time `json:"last_seen_at"`
}

type Agent struct {
	ID           string    `json:"id"`
	MachineID    string    `json:"machine_id"`
	Kind         string    `json:"kind"`
	Role         string    `json:"role"`
	Status       string    `json:"status"`
	Capabilities []string  `json:"capabilities"`
	LastSeenAt   time.Time `json:"last_seen_at"`
}

type APIToken struct {
	ID        string     `json:"id"`
	AgentID   string     `json:"agent_id"`
	CreatedAt time.Time  `json:"created_at"`
	RevokedAt *time.Time `json:"revoked_at,omitempty"`
}

type AuthIdentity struct {
	ActorType    string   `json:"actor_type"`
	ActorID      string   `json:"actor_id"`
	DisplayName  string   `json:"display_name,omitempty"`
	Role         string   `json:"role,omitempty"`
	Roles        []string `json:"roles,omitempty"`
	AuthProvider string   `json:"auth_provider"`
	// Role is the effective authorization role retained for the existing
	// viewer/operator/admin checks. Roles preserves the provider's full role
	// set for audit and future policy decisions. Machine identities leave Role
	// empty so they retain the existing bearer-token authorization behavior.
	//
	// The following fields are set only for an authoritative, verified v2
	// participant identity. They are derived server-side from a Factory
	// DeploymentRecord binding and are never taken from request bodies.
	// Participant marks a least-privilege participant principal whose effective
	// Role is RoleParticipant rather than an empty trusted-machine bypass.
	Participant bool   `json:"participant,omitempty"`
	PrincipalID string `json:"principal_id,omitempty"`
	EmployeeID  string `json:"employee_id,omitempty"`
	BoardRef    string `json:"board_ref,omitempty"`
	AgentID     string `json:"agent_id,omitempty"`
	MachineID   string `json:"machine_id,omitempty"`
	// AgentKind, AgentRole, and AgentCapabilities are the immutable approved
	// grant from the operator-controlled binding. A participant may never
	// self-select these; registration and claim use only these values.
	AgentKind         string   `json:"agent_kind,omitempty"`
	AgentRole         string   `json:"agent_role,omitempty"`
	AgentCapabilities []string `json:"agent_capabilities,omitempty"`

	// IngestOwner is the server-derived, single closed-owner production grant
	// for a scoped observation producer. It is populated only from trusted
	// local machine configuration, never from a request body. A non-empty grant
	// restricts the machine to the operations ingestion write seam and never
	// grants generic operator write privileges. ADR-001 binds each producer
	// credential to exactly one owner.
	IngestOwner string `json:"ingest_owner,omitempty"`

	// Reporter is the server-derived, single mission-scoped Auto-Orch reporter
	// grant. It is intentionally separate from participant and manager
	// authority.
	Reporter           bool   `json:"reporter,omitempty"`
	ReporterMission    string `json:"reporter_mission,omitempty"`
	ReporterEmployeeID string `json:"reporter_employee_id,omitempty"`
}

type Task struct {
	ID                   string    `json:"id"`
	DisplayNumber        int64     `json:"display_number"`
	Title                string    `json:"title"`
	Mission              string    `json:"mission,omitempty"`
	Status               string    `json:"status"`
	Priority             string    `json:"priority"`
	PriorityRank         int       `json:"priority_rank"`
	AssignedAgentID      string    `json:"assigned_agent_id,omitempty"`
	ReviewTargetType     string    `json:"review_target_type,omitempty"`
	ReviewTargetID       string    `json:"review_target_id,omitempty"`
	RequiredCapabilities []string  `json:"required_capabilities"`
	MachineConstraints   []string  `json:"machine_constraints"`
	Instructions         string    `json:"instructions,omitempty"`
	ParentTaskID         string    `json:"parent_task_id,omitempty"`
	RootTaskID           string    `json:"root_task_id,omitempty"`
	ProjectID            string    `json:"project_id,omitempty"`
	CreatedAt            time.Time `json:"created_at"`
	UpdatedAt            time.Time `json:"updated_at"`
}

type Lease struct {
	ID          string    `json:"id"`
	TaskID      string    `json:"task_id"`
	AgentID     string    `json:"agent_id"`
	MachineID   string    `json:"machine_id"`
	Status      string    `json:"status"`
	ClaimedAt   time.Time `json:"claimed_at"`
	ExpiresAt   time.Time `json:"expires_at"`
	HeartbeatAt time.Time `json:"heartbeat_at"`
}

type StatusSummary struct {
	ReadyTasks     int `json:"ready_tasks"`
	ClaimedTasks   int `json:"claimed_tasks"`
	StaleLeases    int `json:"stale_leases"`
	WaitingTasks   int `json:"waiting_tasks"`
	BlockedTasks   int `json:"blocked_tasks"`
	ReviewTasks    int `json:"review_tasks"`
	FailedTasks    int `json:"failed_tasks"`
	DoneTasks      int `json:"done_tasks"`
	OnlineMachines int `json:"online_machines"`
	OnlineAgents   int `json:"online_agents"`
}

type ClaimedTask struct {
	Task  Task  `json:"task"`
	Lease Lease `json:"lease"`
}

type TaskTree struct {
	Task     Task       `json:"task"`
	Children []TaskTree `json:"children"`
}

type Message struct {
	ID             string     `json:"id"`
	TaskID         string     `json:"task_id,omitempty"`
	FromActorType  string     `json:"from_actor_type"`
	FromActorID    string     `json:"from_actor_id"`
	ToActorType    string     `json:"to_actor_type"`
	ToActorID      string     `json:"to_actor_id"`
	Kind           string     `json:"kind"`
	Body           string     `json:"body"`
	RequiresAck    bool       `json:"requires_ack"`
	Status         string     `json:"status"`
	CreatedAt      time.Time  `json:"created_at"`
	AcknowledgedAt *time.Time `json:"acknowledged_at,omitempty"`
	ExpiresAt      *time.Time `json:"expires_at,omitempty"`
}

type Artifact struct {
	ID        string    `json:"id"`
	TaskID    string    `json:"task_id"`
	AgentID   string    `json:"agent_id,omitempty"`
	Kind      string    `json:"kind"`
	PathOrURL string    `json:"path_or_url"`
	Summary   string    `json:"summary,omitempty"`
	Hash      string    `json:"hash,omitempty"`
	CreatedAt time.Time `json:"created_at"`
}

type AutoOrchCycleReport struct {
	MissionName    string    `json:"mission_name"`
	EmployeeID     string    `json:"employee_id,omitempty"`
	CycleID        string    `json:"cycle_id"`
	IdempotencyKey string    `json:"idempotency_key,omitempty"`
	ActorID        string    `json:"actor_id,omitempty"`
	PayloadJSON    string    `json:"payload_json"`
	CreatedAt      time.Time `json:"created_at"`
	UpdatedAt      time.Time `json:"updated_at"`
}

type BoardReviewTask struct {
	Task               Task               `json:"task"`
	EmployeeID         string             `json:"employee_id,omitempty"`
	MissionName        string             `json:"mission_name"`
	RunID              string             `json:"run_id"`
	ReviewGate         string             `json:"review_gate"`
	IdempotencyKey     string             `json:"idempotency_key,omitempty"`
	SourcePDDVersion   string             `json:"source_pdd_version"`
	ArtifactLinks      map[string]string  `json:"artifact_links"`
	GateResults        map[string]any     `json:"gate_results"`
	ReviewInstructions string             `json:"review_instructions"`
	Disposition        *ReviewDisposition `json:"disposition,omitempty"`
}

type ReviewDisposition struct {
	ID          string    `json:"id"`
	TaskID      string    `json:"task_id"`
	MissionName string    `json:"mission_name"`
	RunID       string    `json:"run_id"`
	ReviewGate  string    `json:"review_gate"`
	Action      string    `json:"action"`
	Reason      string    `json:"reason"`
	ActorType   string    `json:"actor_type"`
	ActorID     string    `json:"actor_id"`
	ActorRole   string    `json:"actor_role,omitempty"`
	CreatedAt   time.Time `json:"created_at"`
}

type TaskEvent struct {
	ID          string    `json:"id"`
	TaskID      string    `json:"task_id,omitempty"`
	ActorType   string    `json:"actor_type"`
	ActorID     string    `json:"actor_id"`
	EventType   string    `json:"event_type"`
	PayloadJSON string    `json:"payload_json"`
	CreatedAt   time.Time `json:"created_at"`
}

type AuditRecord struct {
	ID           string    `json:"id"`
	Method       string    `json:"method"`
	Path         string    `json:"path"`
	Status       int       `json:"status"`
	ActorType    string    `json:"actor_type"`
	ActorID      string    `json:"actor_id"`
	Roles        []string  `json:"roles"`
	AuthProvider string    `json:"auth_provider"`
	CreatedAt    time.Time `json:"created_at"`
}

type CountByLabel struct {
	Label string `json:"label"`
	Count int    `json:"count"`
}

type LeaseObservation struct {
	Lease              Lease `json:"lease"`
	Task               Task  `json:"task"`
	IsStale            bool  `json:"is_stale"`
	SecondsUntilExpiry int64 `json:"seconds_until_expiry"`
}

type ProjectState struct {
	ProjectID           string         `json:"project_id"`
	ProjectStatus       string         `json:"project_status"`
	Mode                string         `json:"mode"`
	Host                string         `json:"host,omitempty"`
	Agent               string         `json:"agent,omitempty"`
	BlockedOn           string         `json:"blocked_on,omitempty"`
	NextAction          string         `json:"next_action,omitempty"`
	NeedsHumanAttention bool           `json:"needs_human_attention"`
	Blocked             bool           `json:"blocked"`
	CurrentTask         *Task          `json:"current_task,omitempty"`
	TaskCountsByStatus  map[string]int `json:"task_counts_by_status"`
	LastActivityAt      time.Time      `json:"last_activity_at"`
}

const (
	// BoardSnapshotKind and BoardSnapshotSchemaVersion are the closed native
	// handshake for the authenticated snapshot projection.
	BoardSnapshotKind          = "agent-board.snapshot"
	BoardSnapshotSchemaVersion = "agent-board.snapshot/v1"
	BoardOwnerVersion          = "agent-board/v1"
)

type BoardSnapshot struct {
	Kind            string             `json:"kind"`
	SchemaVersion   string             `json:"schema_version"`
	OwnerVersion    string             `json:"owner_version"`
	GeneratedAt     time.Time          `json:"generated_at"`
	Status          StatusSummary      `json:"status"`
	Machines        []Machine          `json:"machines"`
	Agents          []Agent            `json:"agents"`
	ReadyByPriority []CountByLabel     `json:"ready_by_priority"`
	ActiveLeases    []LeaseObservation `json:"active_leases"`
	StaleLeases     []LeaseObservation `json:"stale_leases"`
	HumanAttention  []Task             `json:"human_attention"`
	RecentEvents    []TaskEvent        `json:"recent_events"`
}
