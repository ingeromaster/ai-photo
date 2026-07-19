"""PostgreSQL helpers for Telegram user quotas and reference packs."""

from __future__ import annotations

import os
from contextlib import contextmanager
from typing import Any

import psycopg
from dotenv import load_dotenv

load_dotenv()

DATABASE_URL = os.getenv("DATABASE_URL", "").strip()
DEFAULT_MAX_GENERATIONS = int(os.getenv("DEFAULT_MAX_GENERATIONS", "10"))
MAX_REFERENCE_PACKS = int(os.getenv("MAX_REFERENCE_PACKS", "5"))


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
    return _user_row_to_dict(row)


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
    return _user_row_to_dict(row) if row else None


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
    return _user_row_to_dict(row) if row else None


def list_telegram_ids() -> list[int]:
    with get_conn() as conn:
        rows = conn.execute(
            "SELECT telegram_id FROM users ORDER BY telegram_id"
        ).fetchall()
    return [int(row[0]) for row in rows]


def count_packs(telegram_id: int) -> int:
    with get_conn() as conn:
        row = conn.execute(
            "SELECT COUNT(*) FROM reference_packs WHERE telegram_id = %s",
            (telegram_id,),
        ).fetchone()
    return int(row[0]) if row else 0


def next_pack_title(telegram_id: int) -> str:
    with get_conn() as conn:
        row = conn.execute(
            """
            SELECT COALESCE(MAX(
              NULLIF(regexp_replace(title, '\\D', '', 'g'), '')::INTEGER
            ), 0) + 1
            FROM reference_packs
            WHERE telegram_id = %s
              AND title ~ '^Набор [0-9]+$'
            """,
            (telegram_id,),
        ).fetchone()
    n = int(row[0]) if row and row[0] is not None else 1
    return f"Набор {n}"


def list_packs(telegram_id: int) -> list[dict[str, Any]]:
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT p.id, p.title, p.created_at, COUNT(i.id)::INTEGER AS image_count
            FROM reference_packs p
            LEFT JOIN reference_images i ON i.pack_id = p.id
            WHERE p.telegram_id = %s
            GROUP BY p.id
            ORDER BY p.created_at DESC, p.id DESC
            """,
            (telegram_id,),
        ).fetchall()
    return [
        {
            "id": int(row[0]),
            "title": row[1],
            "created_at": row[2],
            "image_count": int(row[3]),
        }
        for row in rows
    ]


def create_pack(
    telegram_id: int,
    title: str,
    images: list[dict[str, str]],
) -> dict[str, Any]:
    """
    Create a pack with images.
    Each image dict: telegram_file_id, public_url, optional local_path.
    """
    if not images:
        raise ValueError("images must not be empty")
    with get_conn() as conn:
        pack_row = conn.execute(
            """
            INSERT INTO reference_packs (telegram_id, title)
            VALUES (%s, %s)
            RETURNING id, title, created_at
            """,
            (telegram_id, title),
        ).fetchone()
        pack_id = int(pack_row[0])
        for idx, image in enumerate(images):
            conn.execute(
                """
                INSERT INTO reference_images
                  (pack_id, sort_order, telegram_file_id, public_url, local_path)
                VALUES (%s, %s, %s, %s, %s)
                """,
                (
                    pack_id,
                    idx,
                    image["telegram_file_id"],
                    image["public_url"],
                    image.get("local_path"),
                ),
            )
    return {
        "id": pack_id,
        "title": pack_row[1],
        "created_at": pack_row[2],
        "image_count": len(images),
        "images": list(images),
    }


def get_pack(pack_id: int, telegram_id: int) -> dict[str, Any] | None:
    with get_conn() as conn:
        pack_row = conn.execute(
            """
            SELECT id, title, created_at
            FROM reference_packs
            WHERE id = %s AND telegram_id = %s
            """,
            (pack_id, telegram_id),
        ).fetchone()
        if not pack_row:
            return None
        image_rows = conn.execute(
            """
            SELECT telegram_file_id, public_url, local_path, sort_order
            FROM reference_images
            WHERE pack_id = %s
            ORDER BY sort_order, id
            """,
            (pack_id,),
        ).fetchall()
    images = [
        {
            "telegram_file_id": row[0],
            "public_url": row[1],
            "local_path": row[2],
            "sort_order": int(row[3]),
        }
        for row in image_rows
    ]
    return {
        "id": int(pack_row[0]),
        "title": pack_row[1],
        "created_at": pack_row[2],
        "image_count": len(images),
        "images": images,
    }


def get_images_for_packs(telegram_id: int, pack_ids: list[int]) -> list[dict[str, Any]]:
    if not pack_ids:
        return []
    with get_conn() as conn:
        rows = conn.execute(
            """
            SELECT i.telegram_file_id, i.public_url, i.local_path, p.id, p.title, i.sort_order
            FROM reference_images i
            JOIN reference_packs p ON p.id = i.pack_id
            WHERE p.telegram_id = %s
              AND p.id = ANY(%s)
            ORDER BY array_position(%s, p.id), i.sort_order, i.id
            """,
            (telegram_id, pack_ids, pack_ids),
        ).fetchall()
    return [
        {
            "telegram_file_id": row[0],
            "public_url": row[1],
            "local_path": row[2],
            "pack_id": int(row[3]),
            "pack_title": row[4],
            "sort_order": int(row[5]),
        }
        for row in rows
    ]


def delete_pack(pack_id: int, telegram_id: int) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            """
            DELETE FROM reference_packs
            WHERE id = %s AND telegram_id = %s
            RETURNING id
            """,
            (pack_id, telegram_id),
        ).fetchone()
    return bool(row)


def rename_pack(pack_id: int, telegram_id: int, title: str) -> bool:
    with get_conn() as conn:
        row = conn.execute(
            """
            UPDATE reference_packs
            SET title = %s
            WHERE id = %s AND telegram_id = %s
            RETURNING id
            """,
            (title, pack_id, telegram_id),
        ).fetchone()
    return bool(row)


def _user_row_to_dict(row) -> dict[str, Any]:
    return {
        "telegram_id": row[0],
        "username": row[1],
        "first_name": row[2],
        "used_count": row[3],
        "max_generations": row[4],
        "left_count": row[5],
    }
