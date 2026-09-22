PRAGMA foreign_keys = ON;

ALTER TABLE tasks ADD COLUMN project_id TEXT NULL;

CREATE INDEX IF NOT EXISTS idx_tasks_project ON tasks(project_id);
