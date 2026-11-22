"""SQLite persistence layer for Kiki V1 (5 core tables in WAL mode)."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

GENERAL_TOPIC_ID = "topic-general"
GENERAL_TOPIC_TITLE = "日常闲聊"

SCHEMA_SQL = """
-- 1. 话题表 (Topics)
CREATE TABLE IF NOT EXISTS topics (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    summary TEXT,
    status TEXT CHECK(status IN ('active', 'archived', 'deleted')) DEFAULT 'active',
    unread INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP
);

-- 2. 消息记录表 (Messages)
CREATE TABLE IF NOT EXISTS messages (
    id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL,
    role TEXT CHECK(role IN ('user', 'assistant', 'system')) NOT NULL,
    content TEXT NOT NULL,
    metadata JSON,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(topic_id) REFERENCES topics(id) ON DELETE CASCADE
);

-- 3. 后台重型任务运行表 (SessionRuns)
CREATE TABLE IF NOT EXISTS session_runs (
    id TEXT PRIMARY KEY,
    topic_id TEXT NOT NULL,
    trigger_message_id TEXT NOT NULL,
    agent_type TEXT NOT NULL,
    status TEXT CHECK(status IN ('queued', 'running', 'waiting_input', 'completed', 'failed', 'cancelled')) DEFAULT 'queued',
    current_step TEXT,
    error_message TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(topic_id) REFERENCES topics(id) ON DELETE CASCADE,
    FOREIGN KEY(trigger_message_id) REFERENCES messages(id) ON DELETE CASCADE
);

-- 4. 任务步骤日志表 (StepLogs)
CREATE TABLE IF NOT EXISTS step_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    step_number INTEGER NOT NULL,
    action_type TEXT NOT NULL,
    detail TEXT NOT NULL,
    status TEXT CHECK(status IN ('success', 'failed', 'in_progress')) NOT NULL,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(run_id) REFERENCES session_runs(id) ON DELETE CASCADE
);

-- 5. 任务产出物表 (Artifacts)
CREATE TABLE IF NOT EXISTS artifacts (
    id TEXT PRIMARY KEY,
    run_id TEXT NOT NULL,
    file_path TEXT NOT NULL,
    file_type TEXT NOT NULL,
    description TEXT,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(run_id) REFERENCES session_runs(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_topic_id ON messages(topic_id);
CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at);
CREATE INDEX IF NOT EXISTS idx_session_runs_topic_id ON session_runs(topic_id);
CREATE INDEX IF NOT EXISTS idx_session_runs_trigger_msg ON session_runs(trigger_message_id);
CREATE INDEX IF NOT EXISTS idx_step_logs_run_id ON step_logs(run_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_run_id ON artifacts(run_id);
"""


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:12]}"


def _parse_metadata(raw: Any) -> dict[str, Any]:
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


class Database:
    """Thread-safe SQLite wrapper using WAL mode and foreign key enforcement."""

    def __init__(self, db_path: Path | str) -> None:
        self.db_path = Path(db_path)
        self._lock = threading.RLock()

    def init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.executescript(SCHEMA_SQL)
            # Ensure unread column exists on pre-existing DBs
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(topics)").fetchall()}
            if "unread" not in cols:
                conn.execute("ALTER TABLE topics ADD COLUMN unread INTEGER DEFAULT 0")
            conn.commit()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = sqlite3.connect(str(self.db_path), timeout=30.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA foreign_keys = ON")
            try:
                yield conn
            finally:
                conn.close()

    # ------------------------------------------------------------------
    # Topics
    # ------------------------------------------------------------------
    def _hydrate_topic_row(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        topic_id = row["id"]
        msg_stats = conn.execute(
            """
            SELECT COUNT(*) AS cnt, MAX(created_at) AS last_at
            FROM messages
            WHERE topic_id = ?
            """,
            (topic_id,),
        ).fetchone()
        run_stats = conn.execute(
            """
            SELECT COUNT(*) AS active_cnt
            FROM session_runs
            WHERE topic_id = ? AND status IN ('queued', 'running', 'waiting_input')
            """,
            (topic_id,),
        ).fetchone()
        return {
            "id": topic_id,
            "title": row["title"],
            "summary": row["summary"],
            "status": row["status"],
            "unread": bool(row["unread"]),
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "message_count": int(msg_stats["cnt"] or 0),
            "last_message_at": msg_stats["last_at"],
            "active_run_count": int(run_stats["active_cnt"] or 0),
        }

    def list_topics(self, include_archived: bool = False) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if include_archived:
                rows = conn.execute(
                    "SELECT * FROM topics WHERE status != 'deleted' ORDER BY updated_at DESC"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM topics WHERE status = 'active' ORDER BY updated_at DESC"
                ).fetchall()
            return [self._hydrate_topic_row(conn, r) for r in rows]

    def get_topic(self, topic_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()
            if not row:
                return None
            return self._hydrate_topic_row(conn, row)

    def create_topic(
        self,
        title: str,
        summary: Optional[str] = None,
        topic_id: Optional[str] = None,
    ) -> dict[str, Any]:
        tid = topic_id or _new_id("topic")
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO topics (id, title, summary, status, unread, created_at, updated_at)
                VALUES (?, ?, ?, 'active', 0, ?, ?)
                """,
                (tid, title.strip(), summary, now, now),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM topics WHERE id = ?", (tid,)).fetchone()
            return self._hydrate_topic_row(conn, row)

    def ensure_general_topic(self) -> dict[str, Any]:
        existing = self.get_topic(GENERAL_TOPIC_ID)
        if existing:
            if existing["status"] != "active":
                return self.update_topic(GENERAL_TOPIC_ID, status="active") or existing
            return existing
        return self.create_topic(
            title=GENERAL_TOPIC_TITLE,
            summary="前台轻量闲聊与随口提问",
            topic_id=GENERAL_TOPIC_ID,
        )

    def update_topic(
        self,
        topic_id: str,
        *,
        title: Optional[str] = None,
        summary: Optional[str] = None,
        status: Optional[str] = None,
        unread: Optional[bool] = None,
    ) -> Optional[dict[str, Any]]:
        fields: list[str] = []
        values: list[Any] = []
        if title is not None:
            fields.append("title = ?")
            values.append(title.strip())
        if summary is not None:
            fields.append("summary = ?")
            values.append(summary)
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if unread is not None:
            fields.append("unread = ?")
            values.append(1 if unread else 0)

        if not fields:
            return self.get_topic(topic_id)

        fields.append("updated_at = ?")
        values.append(utc_now_iso())
        values.append(topic_id)

        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE topics SET {', '.join(fields)} WHERE id = ?",
                values,
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM topics WHERE id = ?", (topic_id,)).fetchone()
            return self._hydrate_topic_row(conn, row)

    def touch_topic(self, topic_id: str) -> None:
        with self._connect() as conn:
            conn.execute(
                "UPDATE topics SET updated_at = ? WHERE id = ?",
                (utc_now_iso(), topic_id),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # SessionRuns, StepLogs, Artifacts
    # ------------------------------------------------------------------
    def _hydrate_run_row(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        run_id = row["id"]
        step_rows = conn.execute(
            "SELECT * FROM step_logs WHERE run_id = ? ORDER BY step_number ASC, id ASC",
            (run_id,),
        ).fetchall()
        art_rows = conn.execute(
            "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at ASC",
            (run_id,),
        ).fetchall()
        return {
            "id": run_id,
            "topic_id": row["topic_id"],
            "trigger_message_id": row["trigger_message_id"],
            "agent_type": row["agent_type"],
            "status": row["status"],
            "current_step": row["current_step"],
            "error_message": row["error_message"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "step_logs": [dict(s) for s in step_rows],
            "artifacts": [dict(a) for a in art_rows],
            "raw_logs": [],
            "hitl_request": None,
        }

    def create_session_run(
        self,
        topic_id: str,
        trigger_message_id: str,
        agent_type: str = "gemini_agent",
        status: str = "queued",
        current_step: Optional[str] = None,
        run_id: Optional[str] = None,
    ) -> dict[str, Any]:
        rid = run_id or _new_id("run")
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO session_runs
                    (id, topic_id, trigger_message_id, agent_type, status, current_step, error_message, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)
                """,
                (rid, topic_id, trigger_message_id, agent_type, status, current_step, now, now),
            )
            conn.execute("UPDATE topics SET updated_at = ? WHERE id = ?", (now, topic_id))
            conn.commit()
            row = conn.execute("SELECT * FROM session_runs WHERE id = ?", (rid,)).fetchone()
            return self._hydrate_run_row(conn, row)

    def update_session_run(
        self,
        run_id: str,
        *,
        status: Optional[str] = None,
        current_step: Optional[str] = None,
        error_message: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        fields: list[str] = []
        values: list[Any] = []
        if status is not None:
            fields.append("status = ?")
            values.append(status)
        if current_step is not None:
            fields.append("current_step = ?")
            values.append(current_step)
        if error_message is not None:
            fields.append("error_message = ?")
            values.append(error_message)

        if not fields:
            return self.get_session_run(run_id)

        fields.append("updated_at = ?")
        values.append(utc_now_iso())
        values.append(run_id)

        with self._connect() as conn:
            cur = conn.execute(
                f"UPDATE session_runs SET {', '.join(fields)} WHERE id = ?",
                values,
            )
            conn.commit()
            if cur.rowcount == 0:
                return None
            row = conn.execute("SELECT * FROM session_runs WHERE id = ?", (run_id,)).fetchone()
            return self._hydrate_run_row(conn, row)

    def get_session_run(self, run_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM session_runs WHERE id = ?", (run_id,)).fetchone()
            if not row:
                return None
            return self._hydrate_run_row(conn, row)

    def add_step_log(
        self,
        run_id: str,
        step_number: int,
        action_type: str,
        detail: str,
        status: str = "in_progress",
    ) -> dict[str, Any]:
        now = utc_now_iso()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO step_logs (run_id, step_number, action_type, detail, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (run_id, step_number, action_type, detail, status, now),
            )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM step_logs WHERE id = ?",
                (cur.lastrowid,),
            ).fetchone()
            return dict(row)

    def update_step_log(
        self,
        step_log_id: int,
        status: str,
        detail: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            if detail is not None:
                conn.execute(
                    "UPDATE step_logs SET status = ?, detail = ? WHERE id = ?",
                    (status, detail, step_log_id),
                )
            else:
                conn.execute(
                    "UPDATE step_logs SET status = ? WHERE id = ?",
                    (status, step_log_id),
                )
            conn.commit()
            row = conn.execute(
                "SELECT * FROM step_logs WHERE id = ?",
                (step_log_id,),
            ).fetchone()
            return dict(row) if row else None

    def add_artifact(
        self,
        run_id: str,
        file_path: str,
        file_type: str,
        description: Optional[str] = None,
        artifact_id: Optional[str] = None,
    ) -> dict[str, Any]:
        aid = artifact_id or _new_id("art")
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO artifacts (id, run_id, file_path, file_type, description, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (aid, run_id, file_path, file_type, description, now),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM artifacts WHERE id = ?", (aid,)).fetchone()
            return dict(row)

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------
    def _hydrate_message_row(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        metadata = _parse_metadata(row["metadata"])
        topic_row = conn.execute(
            "SELECT * FROM topics WHERE id = ?",
            (row["topic_id"],),
        ).fetchone()
        topic = self._hydrate_topic_row(conn, topic_row) if topic_row else None

        run_dict: Optional[dict[str, Any]] = None
        run_id = metadata.get("session_run_id")
        if run_id:
            run_row = conn.execute("SELECT * FROM session_runs WHERE id = ?", (run_id,)).fetchone()
            if run_row:
                run_dict = self._hydrate_run_row(conn, run_row)
        else:
            run_row = conn.execute(
                "SELECT * FROM session_runs WHERE trigger_message_id = ? ORDER BY created_at DESC LIMIT 1",
                (row["id"],),
            ).fetchone()
            if run_row:
                run_dict = self._hydrate_run_row(conn, run_row)

        return {
            "id": row["id"],
            "topic_id": row["topic_id"],
            "role": row["role"],
            "content": row["content"],
            "metadata": metadata,
            "created_at": row["created_at"],
            "topic": topic,
            "session_run": run_dict,
        }

    def create_message(
        self,
        topic_id: str,
        role: str,
        content: str,
        metadata: Optional[dict[str, Any]] = None,
        message_id: Optional[str] = None,
    ) -> dict[str, Any]:
        mid = message_id or _new_id("msg")
        now = utc_now_iso()
        meta_json = json.dumps(metadata or {}, ensure_ascii=False)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO messages (id, topic_id, role, content, metadata, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (mid, topic_id, role, content, meta_json, now),
            )
            conn.execute("UPDATE topics SET updated_at = ? WHERE id = ?", (now, topic_id))
            conn.commit()
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (mid,)).fetchone()
            return self._hydrate_message_row(conn, row)

    def update_message(
        self,
        message_id: str,
        *,
        content: Optional[str] = None,
        metadata: Optional[dict[str, Any]] = None,
    ) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            existing = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
            if not existing:
                return None

            new_content = content if content is not None else existing["content"]
            if metadata is not None:
                merged = _parse_metadata(existing["metadata"])
                merged.update(metadata)
                new_meta = json.dumps(merged, ensure_ascii=False)
            else:
                new_meta = existing["metadata"]

            conn.execute(
                "UPDATE messages SET content = ?, metadata = ? WHERE id = ?",
                (new_content, new_meta, message_id),
            )
            conn.execute(
                "UPDATE topics SET updated_at = ? WHERE id = ?",
                (utc_now_iso(), existing["topic_id"]),
            )
            conn.commit()
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
            return self._hydrate_message_row(conn, row)

    def get_message(self, message_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM messages WHERE id = ?", (message_id,)).fetchone()
            if not row:
                return None
            return self._hydrate_message_row(conn, row)

    def list_messages(
        self,
        topic_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if topic_id:
                rows = conn.execute(
                    """
                    SELECT * FROM messages
                    WHERE topic_id = ?
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (topic_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM messages
                    ORDER BY created_at ASC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            return [self._hydrate_message_row(conn, r) for r in rows]

    def get_topic_history(self, topic_id: str, limit: int = 20) -> list[dict[str, str]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT role, content FROM messages
                WHERE topic_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT ?
                """,
                (topic_id, limit),
            ).fetchall()
            return [{"role": r["role"], "content": r["content"]} for r in reversed(rows)]

    def get_recent_messages_with_topic(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT m.*, t.title AS topic_title
                FROM messages m
                LEFT JOIN topics t ON m.topic_id = t.id
                ORDER BY m.rowid DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            result: list[dict[str, Any]] = []
            for row in reversed(rows):
                result.append(
                    {
                        "id": row["id"],
                        "topic_id": row["topic_id"],
                        "topic_title": row["topic_title"] or GENERAL_TOPIC_TITLE,
                        "role": row["role"],
                        "content": row["content"],
                        "created_at": row["created_at"],
                    }
                )
            return result

    def get_latest_message(self) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM messages ORDER BY rowid DESC LIMIT 1").fetchone()
            if not row:
                return None
            return self._hydrate_message_row(conn, row)

    def list_topic_messages_page_before(
        self,
        topic_id: str,
        *,
        before_rowid: Optional[int] = None,
        limit: int = 6,
    ) -> tuple[list[dict[str, Any]], Optional[int]]:
        """Return (chronological_messages, min_rowid) for a topic page older than before_rowid."""
        with self._connect() as conn:
            if before_rowid is None:
                rows = conn.execute(
                    """
                    SELECT rowid AS _rowid, * FROM messages
                    WHERE topic_id = ?
                    ORDER BY rowid DESC
                    LIMIT ?
                    """,
                    (topic_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT rowid AS _rowid, * FROM messages
                    WHERE topic_id = ? AND rowid < ?
                    ORDER BY rowid DESC
                    LIMIT ?
                    """,
                    (topic_id, before_rowid, limit),
                ).fetchall()
            if not rows:
                return [], None
            chronological = list(reversed(rows))
            min_rowid = min(int(r["_rowid"]) for r in chronological)
            return [self._hydrate_message_row(conn, r) for r in chronological], min_rowid

    def expand_paired_topic_messages(
        self,
        topic_id: str,
        message_ids: list[str],
    ) -> tuple[list[str], Optional[int]]:
        """Expand selected message IDs within topic_id to include paired user/assistant turns."""
        if not message_ids:
            return [], None
        seed_set = set(message_ids)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT rowid AS _rowid, id, metadata
                FROM messages
                WHERE topic_id = ?
                ORDER BY rowid ASC
                """,
                (topic_id,),
            ).fetchall()
            if not rows:
                return [], None

            parsed_meta: dict[str, dict[str, Any]] = {
                r["id"]: _parse_metadata(r["metadata"]) for r in rows
            }
            expanded = set(seed_set)
            # If an assistant message is selected, pull in its reply_to user message
            for mid in list(expanded):
                reply_to = parsed_meta.get(mid, {}).get("reply_to")
                if isinstance(reply_to, str) and reply_to in parsed_meta:
                    expanded.add(reply_to)
            # If a user message is selected, pull in any assistant replies referencing it
            for r in rows:
                reply_to = parsed_meta.get(r["id"], {}).get("reply_to")
                if isinstance(reply_to, str) and reply_to in expanded:
                    expanded.add(r["id"])

            ordered_ids: list[str] = []
            min_rowid: Optional[int] = None
            for r in rows:
                if r["id"] in expanded:
                    ordered_ids.append(r["id"])
                    rid = int(r["_rowid"])
                    if min_rowid is None or rid < min_rowid:
                        min_rowid = rid
            return ordered_ids, min_rowid

    def reassign_messages_topic(
        self,
        message_ids: list[str],
        target_topic_id: str,
        *,
        backtracked: bool = False,
    ) -> list[dict[str, Any]]:
        """Reassign multiple messages atomically to target_topic_id in chronological order."""
        if not message_ids:
            return []
        now = utc_now_iso()
        unique_ids = list(dict.fromkeys(message_ids))
        placeholders = ", ".join("?" for _ in unique_ids)
        with self._connect() as conn:
            target_row = conn.execute(
                "SELECT * FROM topics WHERE id = ?", (target_topic_id,)
            ).fetchone()
            if not target_row:
                return []

            msg_rows = conn.execute(
                f"SELECT * FROM messages WHERE id IN ({placeholders}) ORDER BY rowid ASC",
                unique_ids,
            ).fetchall()
            if not msg_rows:
                return []

            updated_ids: list[str] = []
            for msg_row in msg_rows:
                meta = _parse_metadata(msg_row["metadata"])
                old_topic_id = msg_row["topic_id"]
                meta["reassigned"] = True
                meta["confidence"] = 1.0
                if backtracked:
                    meta["backtracked"] = True
                    if old_topic_id != target_topic_id:
                        meta["reassigned_from"] = old_topic_id

                conn.execute(
                    "UPDATE messages SET topic_id = ?, metadata = ? WHERE id = ?",
                    (target_topic_id, json.dumps(meta, ensure_ascii=False), msg_row["id"]),
                )
                conn.execute(
                    "UPDATE session_runs SET topic_id = ?, updated_at = ? WHERE trigger_message_id = ?",
                    (target_topic_id, now, msg_row["id"]),
                )
                updated_ids.append(msg_row["id"])

            conn.execute(
                "UPDATE topics SET updated_at = ? WHERE id = ?", (now, target_topic_id)
            )
            conn.commit()
            out_placeholders = ", ".join("?" for _ in updated_ids)
            updated_rows = conn.execute(
                f"SELECT * FROM messages WHERE id IN ({out_placeholders}) ORDER BY rowid ASC",
                updated_ids,
            ).fetchall()
            return [self._hydrate_message_row(conn, r) for r in updated_rows]

    def reassign_message_topic(
        self,
        message_id: str,
        target_topic_id: str,
    ) -> Optional[dict[str, Any]]:
        updated = self.reassign_messages_topic([message_id], target_topic_id)
        return updated[0] if updated else None
