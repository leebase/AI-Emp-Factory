PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS messages (
  id TEXT PRIMARY KEY,
  task_id TEXT NULL REFERENCES tasks(id),
  from_actor_type TEXT NOT NULL,
  from_actor_id TEXT NOT NULL,
  to_actor_type TEXT NOT NULL,
  to_actor_id TEXT NOT NULL,
  kind TEXT NOT NULL,
  body TEXT NOT NULL,
  requires_ack INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  acknowledged_at TEXT NULL,
  expires_at TEXT NULL
);

CREATE TABLE IF NOT EXISTS artifacts (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL REFERENCES tasks(id),
  agent_id TEXT NULL REFERENCES agents(id),
  kind TEXT NOT NULL,
  path_or_url TEXT NOT NULL,
  summary TEXT NOT NULL DEFAULT '',
  hash TEXT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_parent
ON tasks(parent_task_id, display_number);

CREATE INDEX IF NOT EXISTS idx_tasks_root
ON tasks(root_task_id, display_number);

CREATE INDEX IF NOT EXISTS idx_messages_inbox
ON messages(to_actor_type, to_actor_id, status, created_at);

CREATE INDEX IF NOT EXISTS idx_messages_task
ON messages(task_id, created_at);

CREATE INDEX IF NOT EXISTS idx_artifacts_task
ON artifacts(task_id, created_at);

CREATE INDEX IF NOT EXISTS idx_task_events_task
ON task_events(task_id, created_at);
