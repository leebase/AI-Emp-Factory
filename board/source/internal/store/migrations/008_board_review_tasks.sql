CREATE TABLE IF NOT EXISTS board_review_tasks (
  task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
  mission_name TEXT NOT NULL,
  run_id TEXT NOT NULL,
  review_gate TEXT NOT NULL,
  idempotency_key TEXT NOT NULL DEFAULT '',
  source_pdd_version TEXT NOT NULL,
  artifact_links_json TEXT NOT NULL DEFAULT '{}',
  gate_results_json TEXT NOT NULL DEFAULT '{}',
  review_instructions TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  UNIQUE(mission_name, run_id, review_gate)
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_board_review_tasks_idempotency
ON board_review_tasks(idempotency_key)
WHERE idempotency_key <> '';

CREATE TABLE IF NOT EXISTS board_review_dispositions (
  id TEXT PRIMARY KEY,
  task_id TEXT NOT NULL UNIQUE REFERENCES board_review_tasks(task_id),
  mission_name TEXT NOT NULL,
  run_id TEXT NOT NULL,
  review_gate TEXT NOT NULL,
  action TEXT NOT NULL,
  reason TEXT NOT NULL DEFAULT '',
  actor_type TEXT NOT NULL,
  actor_id TEXT NOT NULL,
  actor_role TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL
);
