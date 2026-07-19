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

-- Saved reference photo packs (reusable across generations)
CREATE TABLE IF NOT EXISTS reference_packs (
  id            BIGSERIAL PRIMARY KEY,
  telegram_id   BIGINT NOT NULL REFERENCES users (telegram_id) ON DELETE CASCADE,
  title         TEXT NOT NULL,
  created_at    TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reference_packs_user
  ON reference_packs (telegram_id, created_at DESC);

CREATE TABLE IF NOT EXISTS reference_images (
  id                 BIGSERIAL PRIMARY KEY,
  pack_id            BIGINT NOT NULL REFERENCES reference_packs (id) ON DELETE CASCADE,
  sort_order         INTEGER NOT NULL DEFAULT 0,
  telegram_file_id   TEXT NOT NULL,
  public_url         TEXT NOT NULL,
  local_path         TEXT,
  created_at         TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_reference_images_pack
  ON reference_images (pack_id, sort_order);

-- App role (adjust if your DB user differs)
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE reference_packs TO ai_photo;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE reference_images TO ai_photo;
GRANT USAGE, SELECT ON SEQUENCE reference_packs_id_seq TO ai_photo;
GRANT USAGE, SELECT ON SEQUENCE reference_images_id_seq TO ai_photo;
