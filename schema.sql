-- Schema for AI Photosessions user quotas
-- Database: ai_photo

CREATE TABLE IF NOT EXISTS users (
  telegram_id     BIGINT PRIMARY KEY,
  username        TEXT,
  first_name      TEXT,
  used_count      INTEGER NOT NULL DEFAULT 0 CHECK (used_count >= 0),
  max_generations INTEGER NOT NULL DEFAULT 10 CHECK (max_generations >= 0),
  created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
  updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_users_updated_at ON users (updated_at DESC);
