"""PostgreSQL helpers for Telegram user quotas."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any

import psycopg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DEFAULT_MAX_GENERATIONS = int(os.getenv("DEFAULT_MAX_GENERATIONS", "10"))


def require_database_url() -> str:
    if not DATABASE_URL:
        raise RuntimeError("DATABASE_URL is missing in .env")
    return DATABASE_URL


@contextmanager
def get_conn():
    conn = psycopg.connect(require_database_url())
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def ensure_user(
    telegram_id: int,
    *,
    username: str | None = None,
    first_name: str | None = None,
) -> dict[str, Any]:
    """Create user with default quota if missing; refresh username/name."""
    with get_conn() as conn:
        row = conn.execute(
            """
            INSERT INTO users (telegram_id, username, first_name, used_count, max_generations)
            VALUES (%s, %s, %s, 0, %s)
            ON CONFLICT (telegram_id) DO UPDATE SET
              username = COALESCE(EXCLUDED.username, users.username),
              first_name = COALESCE(EXCLUDED.first_name, users.first_name),
              updated_at = NOW()
            RETURNING telegram_id, username, first_name, used_count, max_generations,
                      (max_generations - used_count) AS left_count
            """,
            (telegram_id, username, first_name, DEFAULT_MAX_GENERATIONS),
        ).fetchone()
    return _row_to_dict(row)


def get_user(telegram_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT telegram_id, username, first_name, used_count, max_generations,
                   (max_generations - used_count) AS left_count
            FROM users
            WHERE telegram_id = %s
            """,
            (telegram_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def remaining_quota(telegram_id: int) -> int:
    user = get_user(telegram_id)
    if not user:
        return DEFAULT_MAX_GENERATIONS
    return max(0, int(user["left_count"]))


def try_consume_generation(telegram_id: int) -> dict[str, Any] | None:
    """Atomically consume one generation. Returns updated user or None if no quota."""
    with get_conn() as conn:
        row = conn.execute(
            """
            UPDATE users
            SET used_count = used_count + 1,
                updated_at = NOW()
            WHERE telegram_id = %s
              AND used_count < max_generations
            RETURNING telegram_id, username, first_name, used_count, max_generations,
                      (max_generations - used_count) AS left_count
            """,
            (telegram_id,),
        ).fetchone()
    return _row_to_dict(row) if row else None


def list_telegram_ids() -> list[int]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT telegram_id FROM users ORDER BY telegram_id"
        ).fetchall()
    return [int(row[0]) for row in rows]


def _row_to_dict(row) -> dict[str, Any]:
    return {
        "telegram_id": row[0],
        "username": row[1],
        "first_name": row[2],
        "used_count": row[3],
        "max_generations": row[4],
        "left_count": row[5],
    }
