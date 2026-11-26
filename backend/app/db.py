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
    workspace_dir TEXT,
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

-- 6. 工作空间目录挂载表 (WorkspaceMounts)
CREATE TABLE IF NOT EXISTS workspace_mounts (
    id TEXT PRIMARY KEY,
    scope TEXT CHECK(scope IN ('global', 'topic')) NOT NULL,
    topic_id TEXT,
    path TEXT NOT NULL,
    label TEXT,
    is_primary INTEGER DEFAULT 0,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY(topic_id) REFERENCES topics(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_messages_topic_id ON messages(topic_id);
CREATE INDEX IF NOT EXISTS idx_messages_created_at ON messages(created_at);
CREATE INDEX IF NOT EXISTS idx_session_runs_topic_id ON session_runs(topic_id);
CREATE INDEX IF NOT EXISTS idx_session_runs_topic_status ON session_runs(topic_id, status);
CREATE INDEX IF NOT EXISTS idx_session_runs_trigger_msg ON session_runs(trigger_message_id);
CREATE INDEX IF NOT EXISTS idx_step_logs_run_id ON step_logs(run_id);
CREATE INDEX IF NOT EXISTS idx_artifacts_run_id ON artifacts(run_id);
CREATE INDEX IF NOT EXISTS idx_workspace_mounts_scope_topic ON workspace_mounts(scope, topic_id);
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
        self._conn: Optional[sqlite3.Connection] = None
        self._workspace_manager: Optional[Any] = None

    def set_workspace_manager(self, workspace_manager: Optional[Any]) -> None:
        self._workspace_manager = workspace_manager

    def ensure_all_topic_workspaces(self) -> int:
        """Backfill workspace_dir and on-disk directories for any pre-existing topics."""
        if self._workspace_manager is None:
            return 0
        updated = 0
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT id, workspace_dir FROM topics WHERE status != 'deleted'"
            ).fetchall()
            for row in rows:
                tid = row["id"]
                existing_dir = row["workspace_dir"]
                if existing_dir:
                    target = Path(existing_dir).expanduser().resolve()
                    target.mkdir(parents=True, exist_ok=True)
                    (target / "artifacts").mkdir(parents=True, exist_ok=True)
                    (target / "files").mkdir(parents=True, exist_ok=True)
                else:
                    created_dir = self._workspace_manager.ensure_topic_dir(tid)
                    conn.execute(
                        "UPDATE topics SET workspace_dir = ? WHERE id = ?",
                        (str(created_dir), tid),
                    )
                    updated += 1
            if updated:
                conn.commit()
        return updated

    def init_db(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode = WAL")
            conn.executescript(SCHEMA_SQL)
            # Ensure unread & workspace_dir columns exist on pre-existing DBs
            cols = {row["name"] for row in conn.execute("PRAGMA table_info(topics)").fetchall()}
            if "unread" not in cols:
                conn.execute("ALTER TABLE topics ADD COLUMN unread INTEGER DEFAULT 0")
            if "workspace_dir" not in cols:
                conn.execute("ALTER TABLE topics ADD COLUMN workspace_dir TEXT")
            conn.commit()

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if self._conn is None:
                self.db_path.parent.mkdir(parents=True, exist_ok=True)
                conn = sqlite3.connect(
                    str(self.db_path), timeout=30.0, check_same_thread=False
                )
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys = ON")
                conn.execute("PRAGMA synchronous = NORMAL")
                conn.execute("PRAGMA busy_timeout = 30000")
                self._conn = conn
            yield self._conn

    def reconcile_orphaned_state(self) -> dict[str, int]:
        """Converge stale in-flight runs, step logs, and streaming placeholders left by a restart."""
        now = utc_now_iso()
        with self._connect() as conn:
            run_cur = conn.execute(
                """
                UPDATE session_runs
                SET status = 'failed',
                    current_step = '服务重启导致后台任务中断',
                    error_message = COALESCE(error_message, '服务重启导致后台任务中断'),
                    updated_at = ?
                WHERE status IN ('queued', 'running')
                """,
                (now,),
            )
            step_cur = conn.execute(
                """
                UPDATE step_logs
                SET status = 'failed'
                WHERE status = 'in_progress'
                  AND run_id NOT IN (
                      SELECT id FROM session_runs WHERE status = 'waiting_input'
                  )
                """
            )
            streaming_rows = conn.execute(
                "SELECT id, content, metadata FROM messages WHERE metadata LIKE '%\"streaming\"%'"
            ).fetchall()
            fixed_messages = 0
            for row in streaming_rows:
                meta = _parse_metadata(row["metadata"])
                if meta.get("streaming") is True:
                    meta["streaming"] = False
                    content = row["content"] if (row["content"] or "").strip() else "（服务重启，流式回复已中断）"
                    conn.execute(
                        "UPDATE messages SET content = ?, metadata = ? WHERE id = ?",
                        (content, json.dumps(meta, ensure_ascii=False), row["id"]),
                    )
                    fixed_messages += 1
            conn.commit()
            return {
                "runs": max(0, run_cur.rowcount),
                "step_logs": max(0, step_cur.rowcount),
                "messages": fixed_messages,
            }

    # ------------------------------------------------------------------
    # Topics
    # ------------------------------------------------------------------
    @staticmethod
    def _topic_row_from_joined(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "title": row["title"],
            "summary": row["summary"],
            "status": row["status"],
            "unread": bool(row["unread"]),
            "workspace_dir": row["workspace_dir"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "message_count": int(row["message_count"] or 0),
            "last_message_at": row["last_message_at"],
            "active_run_count": int(row["active_run_count"] or 0),
        }

    def _hydrate_topics_by_ids(
        self,
        conn: sqlite3.Connection,
        topic_ids: list[str],
    ) -> dict[str, dict[str, Any]]:
        unique_ids = list(dict.fromkeys(tid for tid in topic_ids if tid))
        if not unique_ids:
            return {}
        placeholders = ", ".join("?" for _ in unique_ids)
        rows = conn.execute(
            f"""
            SELECT
                t.*,
                COALESCE(m.cnt, 0) AS message_count,
                m.last_at AS last_message_at,
                COALESCE(r.active_cnt, 0) AS active_run_count
            FROM topics t
            LEFT JOIN (
                SELECT topic_id, COUNT(*) AS cnt, MAX(created_at) AS last_at
                FROM messages
                WHERE topic_id IN ({placeholders})
                GROUP BY topic_id
            ) m ON m.topic_id = t.id
            LEFT JOIN (
                SELECT topic_id, COUNT(*) AS active_cnt
                FROM session_runs
                WHERE topic_id IN ({placeholders})
                  AND status IN ('queued', 'running', 'waiting_input')
                GROUP BY topic_id
            ) r ON r.topic_id = t.id
            WHERE t.id IN ({placeholders})
            """,
            [*unique_ids, *unique_ids, *unique_ids],
        ).fetchall()
        return {row["id"]: self._topic_row_from_joined(row) for row in rows}

    def _hydrate_topic_row(self, conn: sqlite3.Connection, row: sqlite3.Row) -> dict[str, Any]:
        return self._hydrate_topics_by_ids(conn, [row["id"]])[row["id"]]

    def list_topics(self, include_archived: bool = False) -> list[dict[str, Any]]:
        where_clause = "t.status != 'deleted'" if include_archived else "t.status = 'active'"
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT
                    t.*,
                    COALESCE(m.cnt, 0) AS message_count,
                    m.last_at AS last_message_at,
                    COALESCE(r.active_cnt, 0) AS active_run_count
                FROM topics t
                LEFT JOIN (
                    SELECT topic_id, COUNT(*) AS cnt, MAX(created_at) AS last_at
                    FROM messages
                    GROUP BY topic_id
                ) m ON m.topic_id = t.id
                LEFT JOIN (
                    SELECT topic_id, COUNT(*) AS active_cnt
                    FROM session_runs
                    WHERE status IN ('queued', 'running', 'waiting_input')
                    GROUP BY topic_id
                ) r ON r.topic_id = t.id
                WHERE {where_clause}
                ORDER BY t.updated_at DESC
                """
            ).fetchall()
            return [self._topic_row_from_joined(r) for r in rows]

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
        workspace_dir: Optional[str] = None,
    ) -> dict[str, Any]:
        tid = topic_id or _new_id("topic")
        resolved_ws_dir = workspace_dir
        if resolved_ws_dir is None and self._workspace_manager is not None:
            resolved_ws_dir = str(self._workspace_manager.ensure_topic_dir(tid))
        now = utc_now_iso()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO topics (id, title, summary, status, unread, workspace_dir, created_at, updated_at)
                VALUES (?, ?, ?, 'active', 0, ?, ?, ?)
                """,
                (tid, title.strip(), summary, resolved_ws_dir, now, now),
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
        workspace_dir: Optional[str] = None,
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
        if workspace_dir is not None:
            fields.append("workspace_dir = ?")
            values.append(workspace_dir)

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
        agent_type: str = "internal_agent",
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

    def get_latest_topic_run(self, topic_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT * FROM session_runs
                WHERE topic_id = ?
                ORDER BY created_at DESC, rowid DESC
                LIMIT 1
                """,
                (topic_id,),
            ).fetchone()
            if not row:
                return None
            return self._hydrate_run_row(conn, row)

    def list_active_session_runs(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM session_runs
                WHERE status IN ('queued', 'running', 'waiting_input')
                ORDER BY updated_at DESC
                """
            ).fetchall()
            return [self._hydrate_run_row(conn, r) for r in rows]

    def list_session_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM session_runs
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            return [self._hydrate_run_row(conn, r) for r in rows]

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

    def get_artifact(self, run_id: str, artifact_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE id = ? AND run_id = ?",
                (artifact_id, run_id),
            ).fetchone()
            return dict(row) if row else None

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------
    def _hydrate_messages_batch(
        self,
        conn: sqlite3.Connection,
        rows: list[sqlite3.Row],
    ) -> list[dict[str, Any]]:
        if not rows:
            return []

        parsed_metas = [_parse_metadata(r["metadata"]) for r in rows]
        topic_map = self._hydrate_topics_by_ids(conn, [r["topic_id"] for r in rows])

        explicit_run_ids = list(
            dict.fromkeys(
                str(m["session_run_id"])
                for m in parsed_metas
                if isinstance(m.get("session_run_id"), str) and m.get("session_run_id")
            )
        )
        msg_ids = [r["id"] for r in rows]

        runs_by_id: dict[str, dict[str, Any]] = {}
        runs_by_trigger: dict[str, dict[str, Any]] = {}

        if explicit_run_ids or msg_ids:
            clauses: list[str] = []
            params: list[Any] = []
            if explicit_run_ids:
                ph = ", ".join("?" for _ in explicit_run_ids)
                clauses.append(f"id IN ({ph})")
                params.extend(explicit_run_ids)
            if msg_ids:
                ph = ", ".join("?" for _ in msg_ids)
                clauses.append(f"trigger_message_id IN ({ph})")
                params.extend(msg_ids)

            run_rows = conn.execute(
                f"SELECT * FROM session_runs WHERE {' OR '.join(clauses)} ORDER BY created_at DESC",
                params,
            ).fetchall()

            if run_rows:
                found_run_ids = [r["id"] for r in run_rows]
                rph = ", ".join("?" for _ in found_run_ids)
                steps_by_run: dict[str, list[dict[str, Any]]] = {}
                for srow in conn.execute(
                    f"SELECT * FROM step_logs WHERE run_id IN ({rph}) ORDER BY step_number ASC, id ASC",
                    found_run_ids,
                ).fetchall():
                    steps_by_run.setdefault(srow["run_id"], []).append(dict(srow))

                arts_by_run: dict[str, list[dict[str, Any]]] = {}
                for arow in conn.execute(
                    f"SELECT * FROM artifacts WHERE run_id IN ({rph}) ORDER BY created_at ASC",
                    found_run_ids,
                ).fetchall():
                    arts_by_run.setdefault(arow["run_id"], []).append(dict(arow))

                for rrow in run_rows:
                    rid = rrow["id"]
                    run_dict = {
                        "id": rid,
                        "topic_id": rrow["topic_id"],
                        "trigger_message_id": rrow["trigger_message_id"],
                        "agent_type": rrow["agent_type"],
                        "status": rrow["status"],
                        "current_step": rrow["current_step"],
                        "error_message": rrow["error_message"],
                        "created_at": rrow["created_at"],
                        "updated_at": rrow["updated_at"],
                        "step_logs": steps_by_run.get(rid, []),
                        "artifacts": arts_by_run.get(rid, []),
                        "raw_logs": [],
                        "hitl_request": None,
                    }
                    runs_by_id[rid] = run_dict
                    # ORDER BY created_at DESC ensures first seen is the latest run per trigger message
                    runs_by_trigger.setdefault(rrow["trigger_message_id"], run_dict)

        result: list[dict[str, Any]] = []
        for row, meta in zip(rows, parsed_metas):
            tid = row["topic_id"]
            explicit_rid = meta.get("session_run_id")
            run_dict = (
                runs_by_id.get(explicit_rid)
                if isinstance(explicit_rid, str) and explicit_rid
                else runs_by_trigger.get(row["id"])
            )
            result.append(
                {
                    "id": row["id"],
                    "topic_id": tid,
                    "role": row["role"],
                    "content": row["content"],
                    "metadata": meta,
                    "created_at": row["created_at"],
                    "streaming": bool(meta.get("streaming", False)),
                    "topic": topic_map.get(tid),
                    "session_run": run_dict,
                }
            )
        return result

    def _hydrate_message_row(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
        *,
        topic_cache: Optional[dict[str, Optional[dict[str, Any]]]] = None,
    ) -> dict[str, Any]:
        metadata = _parse_metadata(row["metadata"])
        tid = row["topic_id"]
        if topic_cache is not None and tid in topic_cache:
            topic = topic_cache[tid]
        else:
            topic_row = conn.execute(
                "SELECT * FROM topics WHERE id = ?",
                (tid,),
            ).fetchone()
            topic = self._hydrate_topic_row(conn, topic_row) if topic_row else None
            if topic_cache is not None:
                topic_cache[tid] = topic

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
            "topic_id": tid,
            "role": row["role"],
            "content": row["content"],
            "metadata": metadata,
            "created_at": row["created_at"],
            "streaming": bool(metadata.get("streaming", False)),
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

    def list_messages_page(
        self,
        topic_id: Optional[str] = None,
        *,
        before_id: Optional[str] = None,
        limit: int = 40,
    ) -> dict[str, Any]:
        """Return a chronological page of the latest messages (or older than before_id)."""
        fetch_limit = max(1, int(limit)) + 1
        with self._connect() as conn:
            before_rowid: Optional[int] = None
            if before_id:
                cursor_row = conn.execute(
                    "SELECT rowid AS _rowid FROM messages WHERE id = ?",
                    (before_id,),
                ).fetchone()
                if not cursor_row:
                    return {"messages": [], "has_more": False, "next_cursor": None}
                before_rowid = int(cursor_row["_rowid"])

            if topic_id and before_rowid is not None:
                rows = conn.execute(
                    """
                    SELECT * FROM messages
                    WHERE topic_id = ? AND rowid < ?
                    ORDER BY rowid DESC
                    LIMIT ?
                    """,
                    (topic_id, before_rowid, fetch_limit),
                ).fetchall()
            elif topic_id:
                rows = conn.execute(
                    """
                    SELECT * FROM messages
                    WHERE topic_id = ?
                    ORDER BY rowid DESC
                    LIMIT ?
                    """,
                    (topic_id, fetch_limit),
                ).fetchall()
            elif before_rowid is not None:
                rows = conn.execute(
                    """
                    SELECT * FROM messages
                    WHERE rowid < ?
                    ORDER BY rowid DESC
                    LIMIT ?
                    """,
                    (before_rowid, fetch_limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM messages
                    ORDER BY rowid DESC
                    LIMIT ?
                    """,
                    (fetch_limit,),
                ).fetchall()

            has_more = len(rows) > limit
            page_rows = list(reversed(rows[:limit]))
            messages = self._hydrate_messages_batch(conn, page_rows)
            next_cursor = page_rows[0]["id"] if (has_more and page_rows) else None
            return {
                "messages": messages,
                "has_more": has_more,
                "next_cursor": next_cursor,
            }

    def list_messages(
        self,
        topic_id: Optional[str] = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        return self.list_messages_page(topic_id=topic_id, limit=limit)["messages"]

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

    def get_recent_messages_with_topic(
        self,
        limit: int = 10,
        topic_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        with self._connect() as conn:
            if topic_id:
                rows = conn.execute(
                    """
                    SELECT * FROM messages
                    WHERE topic_id = ?
                    ORDER BY rowid DESC
                    LIMIT ?
                    """,
                    (topic_id, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT * FROM messages
                    ORDER BY rowid DESC
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            result = self._hydrate_messages_batch(conn, list(reversed(rows)))
            for hydrated in result:
                topic = hydrated.get("topic")
                hydrated["topic_title"] = (
                    topic["title"]
                    if isinstance(topic, dict) and topic.get("title")
                    else GENERAL_TOPIC_TITLE
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
            # Also pair messages linked by the same session_run_id (Heavy-Path turns)
            linked_run_ids = {
                parsed_meta[mid]["session_run_id"]
                for mid in expanded
                if isinstance(parsed_meta.get(mid, {}).get("session_run_id"), str)
            }
            if linked_run_ids:
                for r in rows:
                    srid = parsed_meta.get(r["id"], {}).get("session_run_id")
                    if isinstance(srid, str) and srid in linked_run_ids:
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
        extra_metadata: Optional[dict[str, Any]] = None,
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
                if extra_metadata:
                    meta.update(extra_metadata)

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

    def get_messages_by_ids(self, message_ids: list[str]) -> list[dict[str, Any]]:
        if not message_ids:
            return []
        unique_ids = list(dict.fromkeys(message_ids))
        placeholders = ", ".join("?" for _ in unique_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM messages WHERE id IN ({placeholders}) ORDER BY rowid ASC",
                unique_ids,
            ).fetchall()
            return [self._hydrate_message_row(conn, r) for r in rows]

    def list_general_messages_for_dream(
        self,
        *,
        limit: int = 24,
        include_checked: bool = False,
    ) -> list[dict[str, Any]]:
        """Return chronological 日常闲聊 messages eligible for background dream consolidation."""
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT * FROM messages
                WHERE topic_id = ?
                ORDER BY rowid DESC
                LIMIT ?
                """,
                (GENERAL_TOPIC_ID, max(limit * 3, limit)),
            ).fetchall()
            candidates: list[dict[str, Any]] = []
            for row in rows:
                meta = _parse_metadata(row["metadata"])
                if meta.get("streaming"):
                    continue
                if not include_checked and meta.get("dream_checked_at"):
                    continue
                candidates.append(self._hydrate_message_row(conn, row))
                if len(candidates) >= limit:
                    break
            candidates.reverse()
            return candidates

    def mark_messages_dream_checked(self, message_ids: list[str], checked_at: str) -> None:
        if not message_ids:
            return
        unique_ids = list(dict.fromkeys(message_ids))
        placeholders = ", ".join("?" for _ in unique_ids)
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT id, metadata FROM messages WHERE id IN ({placeholders})",
                unique_ids,
            ).fetchall()
            for row in rows:
                meta = _parse_metadata(row["metadata"])
                meta["dream_checked_at"] = checked_at
                conn.execute(
                    "UPDATE messages SET metadata = ? WHERE id = ?",
                    (json.dumps(meta, ensure_ascii=False), row["id"]),
                )
            conn.commit()

    # ------------------------------------------------------------------
    # Workspace Mounts (Main Stream / Topic Custom Directories)
    # ------------------------------------------------------------------
    @staticmethod
    def _mount_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "id": row["id"],
            "scope": row["scope"],
            "topic_id": row["topic_id"],
            "path": row["path"],
            "label": row["label"],
            "is_primary": bool(row["is_primary"]),
            "created_at": row["created_at"],
        }

    def list_workspace_mounts(
        self,
        *,
        scope: Optional[str] = None,
        topic_id: Optional[str] = None,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if scope is not None:
            clauses.append("scope = ?")
            params.append(scope)
        if topic_id is not None:
            clauses.append("topic_id = ?")
            params.append(topic_id)
        where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        with self._connect() as conn:
            rows = conn.execute(
                f"""
                SELECT * FROM workspace_mounts
                {where_sql}
                ORDER BY is_primary DESC, created_at ASC, rowid ASC
                """,
                params,
            ).fetchall()
            return [self._mount_row_to_dict(r) for r in rows]

    def get_workspace_mount(self, mount_id: str) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM workspace_mounts WHERE id = ?",
                (mount_id,),
            ).fetchone()
            return self._mount_row_to_dict(row) if row else None

    def add_workspace_mount(
        self,
        scope: str,
        path: str,
        *,
        topic_id: Optional[str] = None,
        label: Optional[str] = None,
        is_primary: bool = False,
        mount_id: Optional[str] = None,
    ) -> dict[str, Any]:
        if scope not in ("global", "topic"):
            raise ValueError(f"Invalid mount scope: {scope}")
        if scope == "global":
            topic_id = None
        elif not topic_id:
            raise ValueError("topic_id is required when scope='topic'")

        cleaned_label = (label or "").strip() or None
        now = utc_now_iso()

        with self._connect() as conn:
            if scope == "global":
                existing = conn.execute(
                    "SELECT * FROM workspace_mounts WHERE scope = 'global' AND path = ?",
                    (path,),
                ).fetchone()
            else:
                existing = conn.execute(
                    "SELECT * FROM workspace_mounts WHERE scope = 'topic' AND topic_id = ? AND path = ?",
                    (topic_id, path),
                ).fetchone()

            if is_primary:
                if scope == "global":
                    conn.execute(
                        "UPDATE workspace_mounts SET is_primary = 0 WHERE scope = 'global'"
                    )
                else:
                    conn.execute(
                        "UPDATE workspace_mounts SET is_primary = 0 WHERE scope = 'topic' AND topic_id = ?",
                        (topic_id,),
                    )

            if existing:
                mid = existing["id"]
                new_label = cleaned_label if cleaned_label is not None else existing["label"]
                new_primary = 1 if (is_primary or bool(existing["is_primary"])) else 0
                conn.execute(
                    "UPDATE workspace_mounts SET label = ?, is_primary = ? WHERE id = ?",
                    (new_label, new_primary, mid),
                )
            else:
                mid = mount_id or _new_id("mnt")
                conn.execute(
                    """
                    INSERT INTO workspace_mounts (id, scope, topic_id, path, label, is_primary, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (mid, scope, topic_id, path, cleaned_label, 1 if is_primary else 0, now),
                )
            conn.commit()
            row = conn.execute("SELECT * FROM workspace_mounts WHERE id = ?", (mid,)).fetchone()
            return self._mount_row_to_dict(row)

    def update_workspace_mount(
        self,
        mount_id: str,
        *,
        label: Optional[str] = None,
        is_primary: Optional[bool] = None,
    ) -> Optional[dict[str, Any]]:
        with self._connect() as conn:
            existing = conn.execute(
                "SELECT * FROM workspace_mounts WHERE id = ?",
                (mount_id,),
            ).fetchone()
            if not existing:
                return None

            if is_primary is True:
                if existing["scope"] == "global":
                    conn.execute(
                        "UPDATE workspace_mounts SET is_primary = 0 WHERE scope = 'global'"
                    )
                else:
                    conn.execute(
                        "UPDATE workspace_mounts SET is_primary = 0 WHERE scope = 'topic' AND topic_id = ?",
                        (existing["topic_id"],),
                    )

            fields: list[str] = []
            values: list[Any] = []
            if label is not None:
                fields.append("label = ?")
                values.append(label.strip() or None)
            if is_primary is not None:
                fields.append("is_primary = ?")
                values.append(1 if is_primary else 0)

            if fields:
                values.append(mount_id)
                conn.execute(
                    f"UPDATE workspace_mounts SET {', '.join(fields)} WHERE id = ?",
                    values,
                )
                conn.commit()

            row = conn.execute(
                "SELECT * FROM workspace_mounts WHERE id = ?",
                (mount_id,),
            ).fetchone()
            return self._mount_row_to_dict(row) if row else None

    def delete_workspace_mount(self, mount_id: str) -> bool:
        with self._connect() as conn:
            cur = conn.execute("DELETE FROM workspace_mounts WHERE id = ?", (mount_id,))
            conn.commit()
            return cur.rowcount > 0
