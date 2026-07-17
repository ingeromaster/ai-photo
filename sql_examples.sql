-- Useful SQL for managing quotas (run in DBeaver / pgAdmin / psql)
-- Connect: host 127.0.0.1 (or SSH tunnel), db ai_photo, user ai_photo

-- All users and remaining quota
SELECT
  telegram_id,
  username,
  first_name,
  used_count,
  max_generations,
  (max_generations - used_count) AS left_count,
  updated_at
FROM users
ORDER BY updated_at DESC;

-- One user
SELECT * FROM users WHERE telegram_id = 8986126110;

-- Set absolute limit to 50
UPDATE users
SET max_generations = 50, updated_at = NOW()
WHERE telegram_id = 8986126110;

-- Add +10 to current limit
UPDATE users
SET max_generations = max_generations + 10, updated_at = NOW()
WHERE telegram_id = 8986126110;

-- Reset used counter
UPDATE users
SET used_count = 0, updated_at = NOW()
WHERE telegram_id = 8986126110;

-- Give unlimited-like quota
UPDATE users
SET max_generations = 100000, updated_at = NOW()
WHERE telegram_id = 8986126110;
