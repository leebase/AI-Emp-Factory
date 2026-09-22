PRAGMA foreign_keys = ON;

ALTER TABLE api_tokens RENAME TO api_tokens_fk_old;

CREATE TABLE api_tokens (
  id TEXT PRIMARY KEY,
  agent_id TEXT NOT NULL,
  token_hash TEXT NOT NULL UNIQUE,
  created_at TEXT NOT NULL,
  revoked_at TEXT NULL
);

INSERT INTO api_tokens(id, agent_id, token_hash, created_at, revoked_at)
SELECT id, agent_id, token_hash, created_at, revoked_at
FROM api_tokens_fk_old;

DROP TABLE api_tokens_fk_old;

CREATE INDEX IF NOT EXISTS idx_api_tokens_agent
ON api_tokens(agent_id, revoked_at);
