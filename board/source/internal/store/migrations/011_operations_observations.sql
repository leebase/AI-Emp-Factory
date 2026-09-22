CREATE TABLE IF NOT EXISTS operations_observations (
  owner TEXT NOT NULL,
  employee_id TEXT NOT NULL,
  observation_id TEXT NOT NULL,
  producer_principal TEXT NOT NULL,
  schema_version TEXT NOT NULL,
  collection_result TEXT NOT NULL,
  observed_at TEXT NOT NULL,
  received_at TEXT NOT NULL,
  content_sha256 TEXT NOT NULL,
  envelope_json TEXT NOT NULL,
  PRIMARY KEY (owner, employee_id, observation_id)
);

-- observed_at is a fixed nine-fraction UTC timestamp, so lexical ordering here
-- matches chronological ordering for the bounded read query.
CREATE INDEX IF NOT EXISTS idx_operations_observations_employee
ON operations_observations(employee_id, owner, observed_at DESC, observation_id ASC);
