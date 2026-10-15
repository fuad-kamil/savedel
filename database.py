"""Database manager for saving and retrieving Telegram Business messages."""

import sqlite3
import datetime
import os
from typing import Optional, Dict, Any, List

DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "anti_delete.db")


def get_db_connection() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    """Initialize database tables for business connections and messages."""
    with get_db_connection() as conn:
        cursor = conn.cursor()

        # Business connection tracking
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS business_connections (
                connection_id TEXT PRIMARY KEY,
                user_id INTEGER NOT NULL,
                user_chat_id INTEGER NOT NULL,
                is_enabled INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT NOT NULL
            )
            """
        )

        # Message cache
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                business_connection_id TEXT NOT NULL,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                sender_id INTEGER,
                sender_name TEXT,
                sender_username TEXT,
                text TEXT,
                media_type TEXT,
                media_file_id TEXT,
                caption TEXT,
                created_at TEXT NOT NULL,
                is_deleted INTEGER NOT NULL DEFAULT 0,
                deleted_at TEXT,
                local_path TEXT,
                is_timed INTEGER NOT NULL DEFAULT 0,
                cloud_url TEXT,
                cloudinary_public_id TEXT,
                UNIQUE(chat_id, message_id)
            )
            """
        )
        try:
            cursor.execute("ALTER TABLE messages ADD COLUMN local_path TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE messages ADD COLUMN is_timed INTEGER NOT NULL DEFAULT 0")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE messages ADD COLUMN cloud_url TEXT")
        except sqlite3.OperationalError:
            pass
        try:
            cursor.execute("ALTER TABLE messages ADD COLUMN cloudinary_public_id TEXT")
        except sqlite3.OperationalError:
            pass

        # Message edits tracking
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS message_edits (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id INTEGER NOT NULL,
                message_id INTEGER NOT NULL,
                old_text TEXT,
                new_text TEXT,
                edited_at TEXT NOT NULL
            )
            """
        )

        # User management table
        cursor.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                user_id INTEGER PRIMARY KEY,
                full_name TEXT NOT NULL,
                username TEXT,
                is_blocked INTEGER NOT NULL DEFAULT 0,
                is_admin INTEGER NOT NULL DEFAULT 0,
                first_seen TEXT NOT NULL,
                last_seen TEXT NOT NULL,
                total_messages INTEGER NOT NULL DEFAULT 1
            )
            """
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_users_blocked ON users(is_blocked)"
        )

        # Populate users from existing message cache if any
        try:
            cursor.execute(
                """
                INSERT OR IGNORE INTO users (user_id, full_name, username, first_seen, last_seen, total_messages)
                SELECT sender_id, COALESCE(sender_name, 'Unknown'), sender_username,
                       MIN(created_at), MAX(created_at), COUNT(*)
                FROM messages
                WHERE sender_id IS NOT NULL
                GROUP BY sender_id
                """
            )
        except Exception:
            pass

        # Indexes for fast lookup on message deletion and edits
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_chat_msg ON messages(chat_id, message_id)"
        )
        cursor.execute(
            "CREATE INDEX IF NOT EXISTS idx_conn ON messages(business_connection_id)"
        )
        conn.commit()



def save_or_update_connection(
    connection_id: str,
    user_id: int,
    user_chat_id: int,
    is_enabled: bool,
) -> None:
    """Save or update business connection status."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO business_connections (connection_id, user_id, user_chat_id, is_enabled, updated_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(connection_id) DO UPDATE SET
                user_id = excluded.user_id,
                user_chat_id = excluded.user_chat_id,
                is_enabled = excluded.is_enabled,
                updated_at = excluded.updated_at
            """,
            (connection_id, user_id, user_chat_id, 1 if is_enabled else 0, now),
        )
        conn.commit()


def get_connection(connection_id: str) -> Optional[Dict[str, Any]]:
    """Retrieve connection details by connection_id."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM business_connections WHERE connection_id = ?",
            (connection_id,),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def get_active_connection_by_user(user_id: int) -> Optional[Dict[str, Any]]:
    """Retrieve active connection for a given user ID or user_chat_id."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM business_connections 
            WHERE (user_id = ? OR user_chat_id = ?) AND is_enabled = 1 
            ORDER BY updated_at DESC LIMIT 1
            """,
            (user_id, user_id),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def save_message(
    business_connection_id: str,
    chat_id: int,
    message_id: int,
    sender_id: Optional[int],
    sender_name: str,
    sender_username: Optional[str],
    text: Optional[str],
    media_type: Optional[str],
    media_file_id: Optional[str],
    caption: Optional[str],
    created_at: str,
    local_path: Optional[str] = None,
    is_timed: bool = False,
    cloud_url: Optional[str] = None,
    cloudinary_public_id: Optional[str] = None,
) -> None:
    """Save a business chat message to the local cache."""
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO messages (
                business_connection_id, chat_id, message_id,
                sender_id, sender_name, sender_username,
                text, media_type, media_file_id, caption, created_at, is_deleted, local_path, is_timed, cloud_url, cloudinary_public_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?, ?)
            """,
            (
                business_connection_id,
                chat_id,
                message_id,
                sender_id,
                sender_name,
                sender_username,
                text,
                media_type,
                media_file_id,
                caption,
                created_at,
                local_path,
                1 if is_timed else 0,
                cloud_url,
                cloudinary_public_id,
            ),
        )
        conn.commit()


def update_message_cloud_info(
    chat_id: int,
    message_id: int,
    cloud_url: str,
    cloudinary_public_id: str,
) -> None:
    """Update cloud backup information for an existing message without replacing other fields."""
    with get_db_connection() as conn:
        conn.execute(
            """
            UPDATE messages
            SET cloud_url = ?, cloudinary_public_id = ?
            WHERE chat_id = ? AND message_id = ?
            """,
            (cloud_url, cloudinary_public_id, chat_id, message_id),
        )
        conn.commit()


def get_message(chat_id: int, message_id: int) -> Optional[Dict[str, Any]]:
    """Retrieve a cached message by chat_id and message_id."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )
        row = cursor.fetchone()
        return dict(row) if row else None


def mark_messages_deleted(
    chat_id: int,
    message_ids: List[int],
) -> List[Dict[str, Any]]:
    """Mark messages as deleted and return their cached data."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    recovered_messages = []

    with get_db_connection() as conn:
        cursor = conn.cursor()
        for msg_id in message_ids:
            cursor.execute(
                "SELECT * FROM messages WHERE chat_id = ? AND message_id = ?",
                (chat_id, msg_id),
            )
            row = cursor.fetchone()
            if row:
                recovered_messages.append(dict(row))
                cursor.execute(
                    "UPDATE messages SET is_deleted = 1, deleted_at = ? WHERE chat_id = ? AND message_id = ?",
                    (now, chat_id, msg_id),
                )
        conn.commit()

    return recovered_messages


def record_message_edit(
    chat_id: int,
    message_id: int,
    new_text: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Record an edited message and return the previous cached version."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "SELECT * FROM messages WHERE chat_id = ? AND message_id = ?",
            (chat_id, message_id),
        )
        old_row = cursor.fetchone()
        old_data = dict(old_row) if old_row else None

        if old_data:
            cursor.execute(
                """
                INSERT INTO message_edits (chat_id, message_id, old_text, new_text, edited_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (chat_id, message_id, old_data.get("text") or old_data.get("caption"), new_text, now),
            )
            # Update current text in messages table
            cursor.execute(
                "UPDATE messages SET text = ? WHERE chat_id = ? AND message_id = ?",
                (new_text, chat_id, message_id),
            )
            conn.commit()

        return old_data


def get_stats() -> Dict[str, int]:
    """Get database statistics."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM messages")
        total_messages = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM messages WHERE is_deleted = 1")
        total_deleted = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM message_edits")
        total_edits = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM business_connections WHERE is_enabled = 1")
        active_connections = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM messages WHERE is_timed = 1")
        total_timed = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM users")
        total_users = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM users WHERE is_blocked = 1")
        total_blocked = cursor.fetchone()[0]

        return {
            "total_messages": total_messages,
            "total_deleted": total_deleted,
            "total_edits": total_edits,
            "total_timed": total_timed,
            "active_connections": active_connections,
            "total_users": total_users,
            "total_blocked": total_blocked,
        }


def get_recent_deletions(limit: int = 6, filter_type: str = "all") -> List[Dict[str, Any]]:
    """Retrieve recently deleted messages with optional filter ('all', 'media', 'timed', 'text')."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        if filter_type == "media":
            cursor.execute(
                """
                SELECT * FROM messages
                WHERE is_deleted = 1 AND media_type IS NOT NULL AND media_type != ''
                ORDER BY deleted_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        elif filter_type == "timed":
            cursor.execute(
                """
                SELECT * FROM messages
                WHERE is_timed = 1
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
        elif filter_type == "text":
            cursor.execute(
                """
                SELECT * FROM messages
                WHERE is_deleted = 1 AND (media_type IS NULL OR media_type = '')
                ORDER BY deleted_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        else:
            cursor.execute(
                """
                SELECT * FROM messages
                WHERE is_deleted = 1
                ORDER BY deleted_at DESC
                LIMIT ?
                """,
                (limit,),
            )
        return [dict(r) for r in cursor.fetchall()]


def get_recent_messages(limit: int = 6, filter_type: str = "all") -> List[Dict[str, Any]]:
    """Retrieve most recently cached messages across all chats with optional filter ('all', 'media', 'timed', 'text')."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        if filter_type == "media":
            cursor.execute(
                """
                SELECT * FROM messages
                WHERE media_type IS NOT NULL AND media_type != ''
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
        elif filter_type == "timed":
            cursor.execute(
                """
                SELECT * FROM messages
                WHERE is_timed = 1
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
        elif filter_type == "text":
            cursor.execute(
                """
                SELECT * FROM messages
                WHERE media_type IS NULL OR media_type = ''
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
        else:
            cursor.execute(
                """
                SELECT * FROM messages
                ORDER BY id DESC
                LIMIT ?
                """,
                (limit,),
            )
        return [dict(r) for r in cursor.fetchall()]


def get_message_by_db_id(db_id: int) -> Optional[Dict[str, Any]]:
    """Retrieve a cached message by its primary key ID."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM messages WHERE id = ?", (db_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def get_all_linked_media_records(db_id: int) -> List[Dict[str, Any]]:
    """Get all linked media records sharing the same db_id, chat_id + message_id, or cloud_url."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM messages WHERE id = ?", (db_id,))
        row = cursor.fetchone()
        if not row:
            return []
        item = dict(row)
        cid = item.get("chat_id")
        mid = item.get("message_id")
        curl = item.get("cloud_url")

        cursor.execute(
            """
            SELECT id, media_type, local_path, cloud_url, cloudinary_public_id, chat_id, message_id
            FROM messages
            WHERE id = ?
               OR (chat_id = ? AND message_id = ?)
               OR (? IS NOT NULL AND cloud_url = ?)
            """,
            (db_id, cid, mid, curl, curl),
        )
        return [dict(r) for r in cursor.fetchall()]


def purge_media_record(db_id: int) -> Optional[Dict[str, Any]]:
    """Clear media fields (local_path, media_file_id, cloud_url, cloudinary_public_id) from message record and all duplicate linked rows."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM messages WHERE id = ?", (db_id,))
        row = cursor.fetchone()
        if not row:
            return None
        item = dict(row)
        cid = item.get("chat_id")
        mid = item.get("message_id")
        curl = item.get("cloud_url")

        cursor.execute(
            """
            UPDATE messages
            SET local_path = NULL,
                cloud_url = NULL,
                cloudinary_public_id = NULL,
                media_file_id = NULL
            WHERE id = ?
               OR (chat_id = ? AND message_id = ?)
               OR (? IS NOT NULL AND cloud_url = ?)
            """,
            (db_id, cid, mid, curl, curl),
        )
        conn.commit()
        return item


def delete_message_record(db_id: int) -> Optional[Dict[str, Any]]:
    """Completely remove a message record from the database."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM messages WHERE id = ?", (db_id,))
        row = cursor.fetchone()
        if not row:
            return None
        item = dict(row)
        cursor.execute("DELETE FROM messages WHERE id = ?", (db_id,))
        conn.commit()
        return item


# ── User Management ───────────────────────────────────────────────────────────
def record_user(
    user_id: int,
    full_name: str,
    username: Optional[str] = None,
    is_admin: bool = False,
) -> None:
    """Record or update a user activity in the database."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with get_db_connection() as conn:
        conn.execute(
            """
            INSERT INTO users (user_id, full_name, username, is_admin, first_seen, last_seen, total_messages)
            VALUES (?, ?, ?, ?, ?, ?, 1)
            ON CONFLICT(user_id) DO UPDATE SET
                full_name = excluded.full_name,
                username = COALESCE(excluded.username, users.username),
                last_seen = excluded.last_seen,
                total_messages = users.total_messages + 1
            """,
            (user_id, full_name or "Unknown", username, 1 if is_admin else 0, now, now),
        )
        conn.commit()


def get_user(user_id: int) -> Optional[Dict[str, Any]]:
    """Get single user profile by user_id."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT * FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return dict(row) if row else None


def is_user_blocked(user_id: int) -> bool:
    """Check if a user is blocked."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT is_blocked FROM users WHERE user_id = ?", (user_id,))
        row = cursor.fetchone()
        return bool(row and row[0] == 1)


def block_user(user_id: int) -> bool:
    """Block a user from using the bot or being tracked."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            INSERT INTO users (user_id, full_name, username, is_blocked, first_seen, last_seen, total_messages)
            VALUES (?, 'Blocked User', NULL, 1, ?, ?, 0)
            ON CONFLICT(user_id) DO UPDATE SET is_blocked = 1
            """,
            (user_id, now, now),
        )
        conn.commit()
        return cursor.rowcount > 0


def unblock_user(user_id: int) -> bool:
    """Unblock a user."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE users SET is_blocked = 0 WHERE user_id = ?",
            (user_id,),
        )
        conn.commit()
        return cursor.rowcount > 0


def get_users_paginated(page: int = 1, per_page: int = 6) -> tuple[List[Dict[str, Any]], int, int]:
    """Retrieve users with pagination. Returns (users_list, total_count, total_pages)."""
    offset = max(0, (page - 1) * per_page)
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("SELECT COUNT(*) FROM users")
        total_count = cursor.fetchone()[0]
        total_pages = max(1, (total_count + per_page - 1) // per_page)

        cursor.execute(
            """
            SELECT * FROM users
            ORDER BY last_seen DESC
            LIMIT ? OFFSET ?
            """,
            (per_page, offset),
        )
        rows = [dict(r) for r in cursor.fetchall()]
        return rows, total_count, total_pages


def get_blocked_users(limit: int = 20) -> List[Dict[str, Any]]:
    """Retrieve list of currently blocked users."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute(
            """
            SELECT * FROM users
            WHERE is_blocked = 1
            ORDER BY last_seen DESC
            LIMIT ?
            """,
            (limit,),
        )
        return [dict(r) for r in cursor.fetchall()]


