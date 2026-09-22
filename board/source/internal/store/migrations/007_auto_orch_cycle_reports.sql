CREATE TABLE IF NOT EXISTS auto_orch_cycle_reports (
  mission_name TEXT NOT NULL,
  cycle_id TEXT NOT NULL,
  idempotency_key TEXT NOT NULL DEFAULT '',
  payload_json TEXT NOT NULL,
  actor_id TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (mission_name, cycle_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_auto_orch_cycle_reports_idempotency
ON auto_orch_cycle_reports(idempotency_key)
WHERE idempotency_key <> '';
