PRAGMA foreign_keys = ON;

ALTER TABLE tasks ADD COLUMN review_target_type TEXT NOT NULL DEFAULT '';
ALTER TABLE tasks ADD COLUMN review_target_id TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_tasks_review_target
ON tasks(status, review_target_type, review_target_id, priority_rank, display_number);
