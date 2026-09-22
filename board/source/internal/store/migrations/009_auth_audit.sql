CREATE TABLE IF NOT EXISTS audit_events (
  id TEXT PRIMARY KEY,
  method TEXT NOT NULL,
  path TEXT NOT NULL,
  status INTEGER NOT NULL,
  actor_type TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  roles_json TEXT NOT NULL DEFAULT '[]',
  auth_provider TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_audit_events_created_at
ON audit_events(created_at DESC);
