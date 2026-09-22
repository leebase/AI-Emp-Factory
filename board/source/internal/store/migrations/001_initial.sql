PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS schema_migrations (
  version TEXT PRIMARY KEY,
  applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ', 'now'))
);

CREATE TABLE IF NOT EXISTS machines (
  id TEXT PRIMARY KEY,
  hostname TEXT NOT NULL DEFAULT '',
  os TEXT NOT NULL DEFAULT '',
  arch TEXT NOT NULL DEFAULT '',
  is_always_on INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  capabilities_json TEXT NOT NULL DEFAULT '[]',
  workspace_roots_json TEXT NOT NULL DEFAULT '[]',
  tags_json TEXT NOT NULL DEFAULT '[]',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS agents (
  id TEXT PRIMARY KEY,
  machine_id TEXT NOT NULL REFERENCES machines(id),
  kind TEXT NOT NULL DEFAULT 'shell',
  role TEXT NOT NULL DEFAULT 'producer',
  status TEXT NOT NULL,
  last_seen_at TEXT NOT NULL,
  capabilities_json TEXT NOT NULL DEFAULT '[]',
  authority_json TEXT NOT NULL DEFAULT '{}',
  max_concurrent_tasks INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
  id TEXT PRIMARY KEY,
  display_number INTEGER NOT NULL UNIQUE,
  title TEXT NOT NULL,
  mission TEXT NOT NULL DEFAULT '',
  status TEXT NOT NULL,
  priority TEXT NOT NULL DEFAULT 'P2',
  priority_rank INTEGER NOT NULL DEFAULT 2,
  created_by_actor_type TEXT NOT NULL DEFAULT 'human',
  created_by_actor_id TEXT NOT NULL DEFAULT '',
  assigned_agent_id TEXT NULL REFERENCES agents(id),
  required_capabilities_json TEXT NOT NULL DEFAULT '[]',
  machine_constraints_json TEXT NOT NULL DEFAULT '[]',
  instructions TEXT NOT NULL DEFAULT '',
  acceptance_criteria_json TEXT NOT NULL DEFAULT '[]',
  parent_task_id TEXT NULL REFERENCES tasks(id),
  root_task_id TEXT NULL REFERENCES tasks(id),
  blocked_by_json TEXT NOT NULL DEFAULT '[]',
  not_before_at TEXT NULL,
  due_at TEXT NULL,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS task_required_capabilities (
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  capability TEXT NOT NULL,
  PRIMARY KEY (task_id, capability)
);

CREATE TABLE IF NOT EXISTS task_machine_constraints (
  task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
  constraint_value TEXT NOT NULL,
  PRIMARY KEY (task_id, constraint_value)
);

CREATE TABLE IF NOT EXISTS leases (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  agent_id TEXT NOT NULL REFERENCES agents(id),
  machine_id TEXT NOT NULL REFERENCES machines(id),
  status TEXT NOT NULL,
  claimed_at TEXT NOT NULL,
  expires_at TEXT NOT NULL,
  heartbeat_at TEXT NOT NULL,
  released_at TEXT NULL,
  metadata_json TEXT NOT NULL DEFAULT '{}'
);

CREATE UNIQUE INDEX IF NOT EXISTS one_active_lease_per_task
ON leases(task_id)
WHERE status = 'active';

CREATE TABLE IF NOT EXISTS task_events (
  id TEXT PRIMARY KEY,
  task_id TEXT NULL REFERENCES tasks(id),
  actor_type TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  event_type TEXT NOT NULL,
  payload_json TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_status_priority
ON tasks(status, priority_rank, created_at);

CREATE INDEX IF NOT EXISTS idx_leases_status_expires
ON leases(status, expires_at);
