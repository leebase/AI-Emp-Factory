ALTER TABLE auto_orch_cycle_reports ADD COLUMN employee_id TEXT NOT NULL DEFAULT '';
ALTER TABLE board_review_tasks ADD COLUMN employee_id TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_auto_orch_cycle_reports_employee
ON auto_orch_cycle_reports(employee_id);

CREATE INDEX IF NOT EXISTS idx_board_review_tasks_employee
ON board_review_tasks(employee_id);
