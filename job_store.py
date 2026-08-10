"""SQLite-backed Etsy dashboard job store."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"

_TERMINAL_STATUSES = {JOB_STATUS_SUCCEEDED, JOB_STATUS_FAILED, JOB_STATUS_CANCELLED}
_ACTIVE_STATUSES = {JOB_STATUS_QUEUED, JOB_STATUS_RUNNING}


class JobStoreError(RuntimeError):
    pass


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    shop_id: str
    operation: str
    row: int
    folder: str
    listing_id: str
    request_id: str
    dedupe_key: str
    operation_receipt: str
    status: str
    attempt_count: int
    created_at: float
    updated_at: float
    started_at: float | None
    finished_at: float | None
    pid: int | None
    exit_code: int | None
    latest_log_excerpt: str
    retry_parent_job_id: str | None
    error_message: str | None
    fields_json: str

    def to_dict(self) -> dict[str, Any]:
        try:
            fields = json.loads(self.fields_json)
            if not isinstance(fields, list):
                fields = []
        except Exception:
            fields = []
        return {
            "job_id": self.job_id,
            "shop_id": self.shop_id,
            "operation": self.operation,
            "row": self.row,
            "folder": self.folder,
            "listing_id": self.listing_id,
            "request_id": self.request_id,
            "dedupe_key": self.dedupe_key,
            "operation_receipt": self.operation_receipt,
            "status": self.status,
            "attempt_count": self.attempt_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "pid": self.pid,
            "exit_code": self.exit_code,
            "latest_log_excerpt": self.latest_log_excerpt,
            "retry_parent_job_id": self.retry_parent_job_id,
            "error_message": self.error_message,
            "fields": fields,
            "logs": [self.latest_log_excerpt] if self.latest_log_excerpt else [],
            "last_message": self.error_message or self.latest_log_excerpt,
            "key": self.dedupe_key,
            "last_message_at": self.updated_at,
            "attempted_at": self.created_at,
        }


class JobStore:
    """Small durable queue table for update/sync jobs."""

    _SCHEMA_VERSION = 2

    def __init__(self, db_path: str | Path):
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(
            str(self._db_path),
            timeout=30.0,
            check_same_thread=False,
        )
        self._conn.row_factory = sqlite3.Row
        self._init_schema()

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def create_or_get_deduplicated_job(
        self,
        *,
        shop_id: str,
        operation: str,
        row: int,
        folder: str,
        listing_id: str,
        request_id: str,
        dedupe_key: str,
        operation_receipt: dict[str, Any] | str,
        fields: list[str] | None = None,
        parent_job_id: str | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Create a job row if no active one exists.

        Returns (job_record_dict, created).
        """

        if not dedupe_key:
            dedupe_key = f"{shop_id}:{operation}:{folder or row}"
        status = _normalize_status(operation)
        if status != operation:
            operation = status

        job_receipt = (
            operation_receipt
            if isinstance(operation_receipt, str)
            else _stable_json(operation_receipt)
        )
        now = time.time()

        with self._lock:
            with self._conn:
                active = self._select_one(
                    """
                    SELECT *
                    FROM jobs
                    WHERE dedupe_key = ?
                      AND status IN ('queued', 'running')
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (dedupe_key,),
                )
                if active is not None:
                    self._update_job_timestamps(active.job_id, now=now)
                    return active.to_dict(), False

                latest = self._select_one(
                    """
                    SELECT *
                    FROM jobs
                    WHERE dedupe_key = ?
                    ORDER BY created_at DESC
                    LIMIT 1
                    """,
                    (dedupe_key,),
                )
                attempt_count = (latest.attempt_count + 1) if latest else 1
                next_parent = parent_job_id or (latest.job_id if latest else None)
                job_id = f"job-{int(now * 1000)}-{hashlib.md5((dedupe_key + str(attempt_count)).encode('utf-8')).hexdigest()[:8]}"

                fields_json = json.dumps(fields or [])
                self._conn.execute(
                    """
                    INSERT INTO jobs (
                        id, shop_id, operation, row, folder, listing_id, request_id,
                        dedupe_key, operation_receipt, status, attempt_count,
                        created_at, updated_at, started_at, finished_at, pid,
                        exit_code, latest_log_excerpt, retry_parent_job_id,
                        error_message, fields_json
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL, NULL, NULL, ?, ?, ?, ?)
                    """,
                    (
                        job_id,
                        shop_id,
                        operation,
                        int(row),
                        str(folder or ""),
                        str(listing_id or ""),
                        str(request_id or ""),
                        dedupe_key,
                        job_receipt,
                        JOB_STATUS_QUEUED,
                        attempt_count,
                        now,
                        now,
                        "",
                        next_parent,
                        None,
                        fields_json,
                    ),
                )

                record = self._select_one("SELECT * FROM jobs WHERE id = ?", (job_id,))
                if record is None:
                    raise JobStoreError("Failed to persist job")
                return record.to_dict(), True

    def get_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            record = self._select_one("SELECT * FROM jobs WHERE id = ?", (str(job_id),))
            return record.to_dict() if record else None

    def list_jobs(
        self,
        *,
        shop_id: str | None = None,
        status: str | Iterable[str] | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        clauses = []
        params: list[object] = []
        if shop_id is not None:
            clauses.append("shop_id = ?")
            params.append(shop_id)

        if status is not None:
            statuses = [str(item) for item in (status if isinstance(status, Iterable) and not isinstance(status, str) else [status])]
            placeholders = ",".join("?" for _ in statuses)
            clauses.append(f"status IN ({placeholders})")
            params.extend(statuses)

        where_clause = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        query = f"SELECT * FROM jobs {where_clause} ORDER BY created_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(int(limit))
        with self._lock:
            return [record.to_dict() for record in self._select_many(query, tuple(params))]

    def get_active_jobs_for_shop(self, shop_id: str) -> list[dict[str, Any]]:
        return self.list_jobs(shop_id=shop_id, status={JOB_STATUS_QUEUED, JOB_STATUS_RUNNING})

    def mark_running(self, job_id: str, *, pid: int | None = None) -> None:
        self._set_status(job_id, JOB_STATUS_RUNNING, pid=pid, started_at=time.time())

    def mark_succeeded(self, job_id: str, *, exit_code: int = 0, log_excerpt: str = "") -> None:
        if log_excerpt:
            self.append_log_excerpt(job_id, log_excerpt)
        self._set_status(job_id, JOB_STATUS_SUCCEEDED, exit_code=exit_code, finished_at=time.time())

    def mark_failed(self, job_id: str, *, exit_code: int = 1, log_excerpt: str = "") -> None:
        if log_excerpt:
            self.append_log_excerpt(job_id, log_excerpt)
        self._set_status(
            job_id,
            JOB_STATUS_FAILED,
            exit_code=exit_code,
            finished_at=time.time(),
            error_message=log_excerpt or "failed",
        )

    def append_log_excerpt(self, job_id: str, message: str, *, max_chars: int = 4000) -> None:
        safe = str(message or "").strip()
        if not safe:
            return
        with self._lock:
            row = self._conn.execute(
                "SELECT latest_log_excerpt FROM jobs WHERE id = ?",
                (str(job_id),),
            ).fetchone()
            if row is None:
                return
            existing = str(row["latest_log_excerpt"] or "")
            merged = (f"{existing} {safe}" if existing else safe).strip()
            if len(merged) > max_chars:
                merged = merged[-max_chars:]
            self._conn.execute(
                "UPDATE jobs SET latest_log_excerpt = ?, updated_at = ? WHERE id = ?",
                (merged, time.time(), str(job_id)),
            )
            self._conn.commit()

    def cancel_job(self, job_id: str) -> bool:
        return self._set_status(
            job_id,
            JOB_STATUS_CANCELLED,
            error_message="cancelled",
            finished_at=time.time(),
            allow_terminal=True,
            only_if_active=True,
        )

    def cancel_jobs_for_shop(self, shop_id: str) -> int:
        with self._lock:
            row = self._conn.execute(
                """
                UPDATE jobs
                SET status = ?, finished_at = COALESCE(finished_at, ?), updated_at = ?, error_message = COALESCE(NULLIF(error_message, ''), 'cancelled')
                WHERE shop_id = ? AND status IN ('queued', 'running')
                """,
                (JOB_STATUS_CANCELLED, time.time(), time.time(), str(shop_id)),
            )
            self._conn.commit()
            return int(row.rowcount or 0)

    def cancel_all_jobs(self) -> int:
        with self._lock:
            row = self._conn.execute(
                """
                UPDATE jobs
                SET status = ?, finished_at = COALESCE(finished_at, ?), updated_at = ?, error_message = COALESCE(NULLIF(error_message, ''), 'cancelled')
                WHERE status IN ('queued', 'running')
                """,
                (JOB_STATUS_CANCELLED, time.time(), time.time()),
            )
            self._conn.commit()
            return int(row.rowcount or 0)

    def recover_running_jobs(self, reason: str = "server_restart") -> int:
        """Mark any running jobs from previous runtime as failed."""

        with self._lock:
            now = time.time()
            rows = self._conn.execute(
                "SELECT COUNT(*) AS cnt FROM jobs WHERE status = ?",
                (JOB_STATUS_RUNNING,),
            ).fetchone()
            count = int(rows["cnt"]) if rows else 0
            if count <= 0:
                return 0

            message = f"{reason}: recovered_at={_now_iso(now)}"
            self._conn.execute(
                """
                UPDATE jobs
                SET status = ?, finished_at = ?, exit_code = -1, updated_at = ?,
                    latest_log_excerpt = COALESCE(latest_log_excerpt || ' ', '') || ?,
                    error_message = COALESCE(NULLIF(error_message, ''), ?)
                WHERE status = ?
                """,
                (
                    JOB_STATUS_FAILED,
                    now,
                    now,
                    message,
                    message,
                    JOB_STATUS_RUNNING,
                ),
            )
            self._conn.commit()
            return count

    def _set_status(
        self,
        job_id: str,
        status: str,
        *,
        pid: int | None = None,
        exit_code: int | None = None,
        finished_at: float | None = None,
        started_at: float | None = None,
        error_message: str | None = None,
        allow_terminal: bool = False,
        only_if_active: bool = False,
    ) -> bool:
        with self._lock:
            row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (str(job_id),)).fetchone()
            if row is None:
                return False
            current = str(row["status"])
            if only_if_active and current in _TERMINAL_STATUSES:
                return False
            if status in _TERMINAL_STATUSES and (current in _TERMINAL_STATUSES and not allow_terminal):
                return False

            fields: list[str] = ["status = ?", "updated_at = ?"]
            params: list[object] = [status, time.time()]
            if pid is not None:
                fields.append("pid = COALESCE(pid, ?)")
                params.append(pid)
            if started_at is not None:
                fields.append("started_at = COALESCE(started_at, ?)")
                params.append(started_at)
            if finished_at is not None:
                fields.append("finished_at = COALESCE(finished_at, ?)")
                params.append(finished_at)
            if exit_code is not None:
                fields.append("exit_code = ?")
                params.append(exit_code)
            if error_message is not None:
                fields.append("error_message = COALESCE(NULLIF(error_message, ''), ?)")
                params.append(error_message)

            set_clause = ", ".join(fields)
            params.append(str(job_id))
            self._conn.execute(f"UPDATE jobs SET {set_clause} WHERE id = ?", tuple(params))
            self._conn.commit()
            return True

    def _select_one(self, query: str, params: tuple[object, ...]) -> JobRecord | None:
        row = self._conn.execute(query, params).fetchone()
        if row is None:
            return None
        return self._row_to_record(row)

    def _select_many(self, query: str, params: tuple[object, ...]) -> list[JobRecord]:
        return [self._row_to_record(row) for row in self._conn.execute(query, params).fetchall()]

    def _row_to_record(self, row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            job_id=str(row["id"]),
            shop_id=str(row["shop_id"]),
            operation=str(row["operation"]),
            row=int(row["row"]),
            folder=str(row["folder"]),
            listing_id=str(row["listing_id"]),
            request_id=str(row["request_id"]),
            dedupe_key=str(row["dedupe_key"]),
            operation_receipt=str(row["operation_receipt"]),
            status=str(row["status"]),
            attempt_count=int(row["attempt_count"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            started_at=float(row["started_at"]) if row["started_at"] is not None else None,
            finished_at=float(row["finished_at"]) if row["finished_at"] is not None else None,
            pid=int(row["pid"]) if row["pid"] is not None else None,
            exit_code=int(row["exit_code"]) if row["exit_code"] is not None else None,
            latest_log_excerpt=str(row["latest_log_excerpt"] or ""),
            retry_parent_job_id=str(row["retry_parent_job_id"]) if row["retry_parent_job_id"] else None,
            error_message=str(row["error_message"]) if row["error_message"] else None,
            fields_json=str(row["fields_json"] or "[]"),
        )

    def _init_schema(self) -> None:
        with self._lock:
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
            self._conn.execute("CREATE TABLE IF NOT EXISTS _meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    shop_id TEXT NOT NULL,
                    operation TEXT NOT NULL,
                    row INTEGER NOT NULL,
                    folder TEXT NOT NULL,
                    listing_id TEXT NOT NULL,
                    request_id TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL,
                    operation_receipt TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')),
                    attempt_count INTEGER NOT NULL DEFAULT 1,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL,
                    pid INTEGER,
                    exit_code INTEGER,
                    latest_log_excerpt TEXT NOT NULL DEFAULT '',
                    retry_parent_job_id TEXT,
                    error_message TEXT,
                    fields_json TEXT NOT NULL DEFAULT '[]'
                )
                """,
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs (status, shop_id, updated_at)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_dedupe ON jobs (dedupe_key, status, created_at)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_jobs_operation_receipt ON jobs (operation_receipt)")

            # Lightweight schema migration support for older local copies.
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(jobs)")}
            if "retry_parent_job_id" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN retry_parent_job_id TEXT")
            if "operation_receipt" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN operation_receipt TEXT")
                self._conn.execute(
                    "UPDATE jobs SET operation_receipt = '' WHERE operation_receipt IS NULL OR operation_receipt = ''"
                )
            if "dedupe_key" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN dedupe_key TEXT")
                self._conn.execute("UPDATE jobs SET dedupe_key = (shop_id || ':' || operation || ':' || COALESCE(folder, row))")
            if "attempt_count" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN attempt_count INTEGER")
            if "latest_log_excerpt" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN latest_log_excerpt TEXT NOT NULL DEFAULT ''")
            if "fields_json" not in columns:
                self._conn.execute("ALTER TABLE jobs ADD COLUMN fields_json TEXT NOT NULL DEFAULT '[]'")

            self._conn.execute(
                "INSERT OR IGNORE INTO _meta(key, value) VALUES('schema_version', ?)" ,
                (str(self._SCHEMA_VERSION),),
            )
            self._conn.execute(
                "REPLACE INTO _meta(key, value) VALUES('schema_version', ?)",
                (str(self._SCHEMA_VERSION),),
            )
            self._conn.commit()

            # Ensure no empty required fields after migration.
            self._conn.execute("UPDATE jobs SET created_at = COALESCE(created_at, 0) WHERE created_at IS NULL")
            self._conn.commit()

    def _update_job_timestamps(self, job_id: str, *, now: float | None = None) -> None:
        self._conn.execute(
            "UPDATE jobs SET updated_at = ? WHERE id = ?",
            (time.time() if now is None else now, str(job_id)),
        )
        self._conn.commit()


def _normalize_status(value: str) -> str:
    return str(value or "").strip().lower()


def _stable_json(value: Mapping[str, Any] | object) -> str:
    """Return deterministic JSON for a request hash."""

    if isinstance(value, str):
        return value
    try:
        payload = dict(value)
    except Exception:
        payload = {"value": value}
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _now_iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")
