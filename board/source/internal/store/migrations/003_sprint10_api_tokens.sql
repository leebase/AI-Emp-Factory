PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS api_tokens (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  revoked_at TEXT NULL
);

CREATE INDEX IF NOT EXISTS idx_api_tokens_agent
ON api_tokens(agent_id, revoked_at);
