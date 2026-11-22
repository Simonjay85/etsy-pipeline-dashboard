"""SQLite-backed Etsy dashboard job store."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from pipeline_contracts import PipelinePolicy, PipelineStage


JOB_STATUS_QUEUED = "queued"
JOB_STATUS_RUNNING = "running"
JOB_STATUS_SUCCEEDED = "succeeded"
JOB_STATUS_FAILED = "failed"
JOB_STATUS_CANCELLED = "cancelled"

# The legacy ``jobs`` table above is intentionally kept stable for Etsy row
# updates.  Product-campaign work uses the additive pipeline tables below so
# a candidate can have a durable UUID before it has a workbook row or Etsy
# listing id.  These values are also the wire contract consumed by the
# Dashboard and the Anna bridge.
PIPELINE_STATUS_QUEUED = "queued"
PIPELINE_STATUS_RUNNING = "running"
PIPELINE_STATUS_WAITING = "waiting"
PIPELINE_STATUS_UNKNOWN = "unknown"
PIPELINE_STATUS_RECONCILING = "reconciling"
PIPELINE_STATUS_VERIFIED = "verified"
PIPELINE_STATUS_FAILED = "failed"
PIPELINE_STATUS_BLOCKED = "blocked"
PIPELINE_STATUS_CANCELLED = "cancelled"
PIPELINE_ALLOWED_SHOPS = frozenset({"templystudios", "daisyflowdigital", "shop3", "BubbleByJulie"})
PIPELINE_ALLOWED_STAGES = frozenset(stage.value for stage in PipelineStage if stage is not PipelineStage.PUBLISH)
PIPELINE_DRAFT_STAGE = PipelineStage.CREATE_VERIFIED_DRAFT.value
PIPELINE_ACTIVE_STATUSES = frozenset({
    PIPELINE_STATUS_QUEUED,
    PIPELINE_STATUS_RUNNING,
    PIPELINE_STATUS_WAITING,
    PIPELINE_STATUS_UNKNOWN,
    PIPELINE_STATUS_RECONCILING,
})
PIPELINE_TERMINAL_STATUSES = frozenset({
    PIPELINE_STATUS_VERIFIED,
    PIPELINE_STATUS_FAILED,
    PIPELINE_STATUS_BLOCKED,
    PIPELINE_STATUS_CANCELLED,
})

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

    _SCHEMA_VERSION = 4

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

    # ------------------------------------------------------------------
    # Durable product-pipeline store
    # ------------------------------------------------------------------
    # Keep these methods separate from the row-oriented ``jobs`` API above.
    # A pipeline candidate exists before a workbook row/listing id and has a
    # different recovery contract at the Etsy side-effect boundary.

    def create_pipeline_job(
        self,
        *,
        campaign_id: str,
        shop_id: str,
        stage: str,
        product_id: str | None = None,
        candidate_id: str | None = None,
        input_version: str = "1",
        source_fingerprint: str = "",
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
        contract_version: str = "1.0",
        side_effect_boundary: str = "none",
        status: str = PIPELINE_STATUS_QUEUED,
        max_inflight_jobs: int | None = None,
    ) -> tuple[dict[str, Any], bool]:
        """Persist one candidate-stage operation with strict idempotency.

        ``candidate_id`` and ``product_id`` are intentionally independent:
        research may create a candidate UUID long before a catalog row exists.
        Reusing an idempotency key always returns the original record, even if
        that attempt later became terminal; callers must change the input
        version/checksum when they mean a new attempt.
        """

        campaign = str(campaign_id or "").strip()
        shop = str(shop_id or "").strip()
        stage_value = str(stage or "").strip().lower()
        if not campaign or not shop or not stage_value:
            raise JobStoreError("pipeline job requires campaign_id, shop_id and stage")
        if shop not in PIPELINE_ALLOWED_SHOPS:
            raise JobStoreError(f"unsupported pipeline shop: {shop}")
        if stage_value not in PIPELINE_ALLOWED_STAGES:
            raise JobStoreError(f"unsupported pipeline stage: {stage_value}")
        allowed_statuses = PIPELINE_ACTIVE_STATUSES | PIPELINE_TERMINAL_STATUSES
        status_value = str(status or PIPELINE_STATUS_QUEUED).strip().lower()
        if status_value not in allowed_statuses:
            raise JobStoreError(f"invalid pipeline status: {status_value}")
        candidate = str(candidate_id or "").strip() or f"candidate-{uuid.uuid4()}"
        product = str(product_id or "").strip()
        input_value = str(input_version or "1").strip() or "1"
        fingerprint = str(source_fingerprint or "").strip()
        if not idempotency_key:
            seed = "|".join((campaign, shop, candidate, product, stage_value, input_value, fingerprint, contract_version))
            idempotency_key = hashlib.sha256(seed.encode("utf-8")).hexdigest()
        idem = str(idempotency_key).strip()
        if not idem:
            raise JobStoreError("pipeline job requires idempotency_key")
        boundary = str(side_effect_boundary or "none").strip().lower()
        if boundary not in {"none", "external_read", "etsy", "etsy_write", "external_write"}:
            raise JobStoreError(f"invalid pipeline side_effect_boundary: {boundary}")
        if stage_value == PIPELINE_DRAFT_STAGE and boundary != "etsy_write":
            raise JobStoreError("create-verified-draft must declare side_effect_boundary=etsy_write")
        if stage_value != PIPELINE_DRAFT_STAGE and boundary in {"etsy", "etsy_write", "external_write"}:
            raise JobStoreError("only create-verified-draft may declare an Etsy write boundary")
        payload_json = _stable_json(payload or {})
        now = time.time()
        job_id = f"pipeline-{uuid.uuid4()}"

        with self._lock:
            with self._conn:
                existing = self._conn.execute(
                    "SELECT * FROM pipeline_jobs WHERE idempotency_key = ? LIMIT 1",
                    (idem,),
                ).fetchone()
                if existing is not None:
                    return self._pipeline_row_to_dict(existing), False
                if max_inflight_jobs is not None:
                    active_count = self._conn.execute(
                        "SELECT COUNT(*) AS count FROM pipeline_jobs WHERE status IN (?,?,?,?,?)",
                        tuple(PIPELINE_ACTIVE_STATUSES),
                    ).fetchone()
                    if int(active_count["count"] or 0) >= max(1, int(max_inflight_jobs)):
                        raise JobStoreError("pipeline inflight limit reached")
                self._conn.execute(
                    """
                    INSERT INTO pipeline_jobs (
                        id, campaign_id, candidate_id, product_id, shop_id, stage,
                        status, contract_version, input_version, source_fingerprint,
                        idempotency_key, payload_json, result_json, receipt_json,
                        error_code, error_message, side_effect_boundary,
                        lease_owner, lease_expires_at, fence, attempt_count,
                        created_at, updated_at, started_at, finished_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, '{}', '{}',
                              NULL, NULL, ?, NULL, NULL, 0, 0, ?, ?, NULL, NULL)
                    """,
                    (
                        job_id,
                        campaign,
                        candidate,
                        product,
                        shop,
                        stage_value,
                        status_value,
                        str(contract_version or "1.0"),
                        input_value,
                        fingerprint,
                        idem,
                        payload_json,
                        boundary,
                        now,
                        now,
                    ),
                )
                row = self._conn.execute("SELECT * FROM pipeline_jobs WHERE id = ?", (job_id,)).fetchone()
                if row is None:  # pragma: no cover - sqlite would raise first
                    raise JobStoreError("failed to persist pipeline job")
                return self._pipeline_row_to_dict(row), True

    def get_pipeline_job(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM pipeline_jobs WHERE id = ?", (str(job_id),)).fetchone()
            return self._pipeline_row_to_dict(row) if row is not None else None

    def list_pipeline_jobs(
        self,
        *,
        campaign_id: str | None = None,
        shop_id: str | None = None,
        status: str | Iterable[str] | None = None,
        stage: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[object] = []
        if campaign_id:
            clauses.append("campaign_id = ?")
            params.append(str(campaign_id))
        if shop_id:
            clauses.append("shop_id = ?")
            params.append(str(shop_id))
        if stage:
            clauses.append("stage = ?")
            params.append(str(stage).strip().lower())
        if status is not None:
            statuses = [str(item).strip().lower() for item in (status if isinstance(status, Iterable) and not isinstance(status, str) else [status])]
            statuses = [item for item in statuses if item]
            if statuses:
                clauses.append("status IN (" + ",".join("?" for _ in statuses) + ")")
                params.extend(statuses)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        safe_limit = max(1, min(int(limit or 100), 500))
        params.append(safe_limit)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM pipeline_jobs{where} ORDER BY created_at DESC LIMIT ?",
                tuple(params),
            ).fetchall()
            return [self._pipeline_row_to_dict(row) for row in rows]

    def claim_pipeline_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        lease_seconds: int = 900,
    ) -> dict[str, Any] | None:
        """Claim a queued job and return its fencing token.

        An expired lease can be reclaimed by another worker.  ``unknown`` is
        deliberately excluded: it represents an Etsy write whose outcome is
        not known and must pass reconciliation before any retry.
        """

        owner = str(worker_id or "").strip()
        if not owner:
            raise JobStoreError("worker_id is required")
        now = time.time()
        expires = now + max(30, int(lease_seconds or 900))
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute("SELECT * FROM pipeline_jobs WHERE id = ?", (str(job_id),)).fetchone()
                if row is None:
                    self._conn.rollback()
                    return None
                current = str(row["status"])
                lease_owner = str(row["lease_owner"] or "")
                lease_expires = float(row["lease_expires_at"] or 0)
                if current in PIPELINE_TERMINAL_STATUSES or current in {PIPELINE_STATUS_UNKNOWN}:
                    self._conn.rollback()
                    return None
                if current == PIPELINE_STATUS_RUNNING and lease_expires > now and lease_owner != owner:
                    self._conn.rollback()
                    return None
                if str(row["stage"]) in {"image_gen", "generate-images"}:
                    image_lock = self._conn.execute(
                        "SELECT id, lease_expires_at FROM pipeline_jobs WHERE status = ? AND stage IN ('image_gen','generate-images') AND lease_expires_at IS NOT NULL AND lease_expires_at > ? AND id <> ? LIMIT 1",
                        (PIPELINE_STATUS_RUNNING, now, str(job_id)),
                    ).fetchone()
                    if image_lock is not None:
                        self._conn.rollback()
                        return None
                if str(row["stage"]) == PIPELINE_DRAFT_STAGE:
                    lock = self._conn.execute(
                        "SELECT job_id, lease_expires_at FROM pipeline_locks WHERE lock_key = ?",
                        (f"etsy-write:{row['shop_id']}",),
                    ).fetchone()
                    if lock is not None and str(lock["job_id"]) != str(job_id) and float(lock["lease_expires_at"] or 0) > now:
                        self._conn.rollback()
                        return None
                    if lock is not None and float(lock["lease_expires_at"] or 0) <= now:
                        self._conn.execute("DELETE FROM pipeline_locks WHERE lock_key = ?", (f"etsy-write:{row['shop_id']}",))
                fence = int(row["fence"] or 0) + 1
                attempts = int(row["attempt_count"] or 0) + 1
                self._conn.execute(
                    """
                    UPDATE pipeline_jobs
                    SET status = ?, lease_owner = ?, lease_expires_at = ?, fence = ?,
                        attempt_count = ?, started_at = COALESCE(started_at, ?), updated_at = ?
                    WHERE id = ?
                    """,
                    (PIPELINE_STATUS_RUNNING, owner, expires, fence, attempts, now, now, str(job_id)),
                )
                if str(row["stage"]) == PIPELINE_DRAFT_STAGE:
                    self._conn.execute(
                        "INSERT OR REPLACE INTO pipeline_locks(lock_key, job_id, owner, fence, lease_expires_at, updated_at) VALUES(?,?,?,?,?,?)",
                        (f"etsy-write:{row['shop_id']}", str(job_id), owner, fence, expires, now),
                    )
                updated = self._conn.execute("SELECT * FROM pipeline_jobs WHERE id = ?", (str(job_id),)).fetchone()
                self._conn.commit()
                return self._pipeline_row_to_dict(updated) if updated is not None else None
            except Exception:
                self._conn.rollback()
                raise

    def heartbeat_pipeline_job(
        self,
        job_id: str,
        *,
        worker_id: str,
        fence: int,
        lease_seconds: int = 900,
    ) -> bool:
        now = time.time()
        expires = now + max(30, int(lease_seconds or 900))
        with self._lock:
            with self._conn:
                result = self._conn.execute(
                    """
                    UPDATE pipeline_jobs
                    SET lease_expires_at = ?, updated_at = ?
                    WHERE id = ? AND status = ? AND lease_owner = ? AND fence = ?
                      AND lease_expires_at IS NOT NULL AND lease_expires_at > ?
                    """,
                    (expires, now, str(job_id), PIPELINE_STATUS_RUNNING, str(worker_id), int(fence), now),
                )
                if result.rowcount:
                    self._conn.execute(
                        "UPDATE pipeline_locks SET lease_expires_at = ?, updated_at = ? WHERE job_id = ? AND owner = ? AND fence = ?",
                        (expires, now, str(job_id), str(worker_id), int(fence)),
                    )
                return bool(result.rowcount)

    def record_pipeline_manifest(
        self,
        job_id: str,
        *,
        worker_id: str,
        fence: int,
        manifest_sha256: str,
        manifest: Mapping[str, Any],
        source_job_id: str | None = None,
    ) -> dict[str, Any]:
        """Persist immutable manifest evidence under the current worker lease.

        The artifact writer validates the files on disk. This second, SQLite
        boundary stores the accepted hash/evidence so a later Etsy receipt
        cannot replace it with caller-supplied 64-character strings. A
        manifest may be recorded once for a job; retries are idempotent only
        when the hash and evidence are byte-for-byte equivalent.
        """

        digest = str(manifest_sha256 or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise JobStoreError("manifest_sha256 must be a 64-character SHA-256")
        if not isinstance(manifest, Mapping):
            raise JobStoreError("manifest evidence must be an object")
        data = dict(manifest)
        identity = {
            "pipeline_job_id": str(data.get("pipeline_job_id") or "").strip(),
            "campaign_id": str(data.get("campaign_id") or "").strip(),
            "candidate_id": str(data.get("candidate_id") or "").strip(),
            "product_id": str(data.get("product_id") or "").strip(),
            "shop_id": str(data.get("shop_id") or "").strip(),
            "contract_version": str(data.get("contract_version") or "").strip(),
        }
        if any(not value for value in identity.values()):
            raise JobStoreError("manifest evidence is missing a required identity")
        if identity["shop_id"] not in PIPELINE_ALLOWED_SHOPS:
            raise JobStoreError(f"unsupported manifest shop: {identity['shop_id']}")
        if str(data.get("schema") or "") != "etsy.pipeline-product.v1":
            raise JobStoreError("manifest schema is invalid")
        if str(data.get("listing_type") or "").strip().lower() != "digital":
            raise JobStoreError("pipeline manifest listing_type must be digital")
        customer_files = data.get("customer_files")
        customer_evidence = data.get("customer_file_evidence")
        if not isinstance(customer_files, list) or not (1 <= len(customer_files) <= 5):
            raise JobStoreError("manifest customer_files must contain 1-5 files")
        if not isinstance(customer_evidence, list) or len(customer_evidence) != len(customer_files):
            raise JobStoreError("manifest customer_file_evidence is incomplete")
        normalized_files: list[str] = []
        normalized_evidence: list[dict[str, Any]] = []
        for raw_name, raw_record in zip(customer_files, customer_evidence):
            name = str(raw_name or "").strip().replace("\\", "/")
            if not name or name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/")):
                raise JobStoreError("manifest customer file path is unsafe")
            if any(part.casefold() == "source_assets" for part in Path(name).parts):
                raise JobStoreError("manifest customer file cannot be source_assets")
            if not isinstance(raw_record, Mapping):
                raise JobStoreError("manifest customer file evidence is invalid")
            record_path = str(raw_record.get("path") or "").strip().replace("\\", "/")
            record_hash = str(raw_record.get("sha256") or "").strip().lower()
            try:
                record_size = int(raw_record.get("size"))
            except (TypeError, ValueError) as exc:
                raise JobStoreError("manifest customer file size is invalid") from exc
            if record_path != name or not re.fullmatch(r"[0-9a-f]{64}", record_hash) or record_size < 1:
                raise JobStoreError("manifest customer file evidence is invalid")
            if name in normalized_files:
                raise JobStoreError("manifest customer files contain a duplicate path")
            normalized_files.append(name)
            normalized_evidence.append({"path": name, "sha256": record_hash, "size": record_size})
        image_files = data.get("image_files", [])
        image_evidence = data.get("image_file_evidence", [])
        if not isinstance(image_files, list) or not isinstance(image_evidence, list) or len(image_files) != len(image_evidence):
            raise JobStoreError("manifest image_file_evidence is incomplete")
        if image_files and len(image_files) != PipelinePolicy().image_count:
            raise JobStoreError("manifest must contain exactly 10 listing images when supplied")
        normalized_image_files: list[str] = []
        normalized_image_evidence: list[dict[str, Any]] = []
        for raw_name, raw_record in zip(image_files, image_evidence):
            name = str(raw_name or "").strip().replace("\\", "/")
            if not name or name.startswith("/") or any(part in {"", ".", ".."} for part in name.split("/")):
                raise JobStoreError("manifest image path is unsafe")
            if Path(name).parts[0].casefold() != "images":
                raise JobStoreError("manifest images must remain under images/")
            if Path(name).suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp"}:
                raise JobStoreError("manifest image extension is not supported")
            if not isinstance(raw_record, Mapping):
                raise JobStoreError("manifest image evidence is invalid")
            record_path = str(raw_record.get("path") or "").strip().replace("\\", "/")
            record_hash = str(raw_record.get("sha256") or "").strip().lower()
            try:
                record_size = int(raw_record.get("size"))
            except (TypeError, ValueError) as exc:
                raise JobStoreError("manifest image size is invalid") from exc
            if record_path != name or not re.fullmatch(r"[0-9a-f]{64}", record_hash) or record_size < 1:
                raise JobStoreError("manifest image evidence is invalid")
            if name.casefold() in {item.casefold() for item in normalized_image_files}:
                raise JobStoreError("manifest images contain a duplicate path")
            normalized_image_files.append(name)
            normalized_image_evidence.append({"path": name, "sha256": record_hash, "size": record_size})
        source_assets = data.get("source_assets", [])
        source_provenance = data.get("source_provenance", [])
        if not isinstance(source_assets, list) or not isinstance(source_provenance, list):
            raise JobStoreError("manifest source evidence must be lists")
        evidence_snapshot: dict[str, Any] = {
            **identity,
            "schema": str(data.get("schema") or "etsy.pipeline-product.v1"),
            "listing_type": "digital",
            "customer_files": normalized_files,
            "customer_file_evidence": normalized_evidence,
            "image_files": normalized_image_files,
            "image_file_evidence": normalized_image_evidence,
            "source_assets": source_assets,
            "source_provenance": source_provenance,
        }
        evidence_json = _stable_json(evidence_snapshot)
        source_id = str(source_job_id or "").strip() or None
        owner = str(worker_id or "").strip()
        if not owner or fence < 1:
            raise JobStoreError("worker_id and fence are required for manifest evidence")
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM pipeline_jobs WHERE id = ?", (str(job_id),)
                ).fetchone()
                if row is None:
                    self._conn.rollback()
                    raise JobStoreError("pipeline job not found")
                if str(row["stage"] or "") not in {
                    PipelineStage.CREATE.value,
                    PipelineStage.CREATE_PRODUCT.value,
                    PIPELINE_DRAFT_STAGE,
                }:
                    self._conn.rollback()
                    raise JobStoreError("manifest evidence is only accepted at product/draft stages")
                if str(row["stage"] or "") == PIPELINE_DRAFT_STAGE and len(normalized_image_files) != PipelinePolicy().image_count:
                    self._conn.rollback()
                    raise JobStoreError("draft manifest must contain exactly 10 accepted listing images")
                if identity["pipeline_job_id"] != str(row["id"]):
                    self._conn.rollback()
                    raise JobStoreError("manifest pipeline_job_id does not match job")
                for key, column in (
                    ("campaign_id", "campaign_id"),
                    ("candidate_id", "candidate_id"),
                    ("product_id", "product_id"),
                    ("shop_id", "shop_id"),
                    ("contract_version", "contract_version"),
                ):
                    if identity[key] != str(row[column] or ""):
                        self._conn.rollback()
                        raise JobStoreError(f"manifest {key} does not match job")
                if not self._pipeline_lease_matches(row, owner, fence, now=now):
                    self._conn.rollback()
                    raise JobStoreError("active lease/fence is required for manifest evidence")
                current_digest = str(row["manifest_sha256"] or "").strip().lower()
                current_evidence = str(row["manifest_evidence_json"] or "{}").strip()
                if current_digest:
                    if current_digest != digest or current_evidence != evidence_json:
                        self._conn.rollback()
                        raise JobStoreError("manifest evidence is already bound to a different artifact")
                    self._conn.commit()
                    return self._pipeline_row_to_dict(row)
                self._conn.execute(
                    "UPDATE pipeline_jobs SET manifest_sha256 = ?, manifest_evidence_json = ?, manifest_source_job_id = ?, updated_at = ? WHERE id = ?",
                    (digest, evidence_json, source_id, now, str(job_id)),
                )
                updated = self._conn.execute(
                    "SELECT * FROM pipeline_jobs WHERE id = ?", (str(job_id),)
                ).fetchone()
                self._conn.commit()
                return self._pipeline_row_to_dict(updated)
            except Exception:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def transition_pipeline_job(
        self,
        job_id: str,
        status: str,
        *,
        worker_id: str | None = None,
        fence: int | None = None,
        result: Mapping[str, Any] | None = None,
        receipt: Mapping[str, Any] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        side_effect_boundary: str | None = None,
    ) -> bool:
        """Apply a fenced state transition and optionally persist evidence."""

        next_status = str(status or "").strip().lower()
        if next_status not in PIPELINE_ACTIVE_STATUSES | PIPELINE_TERMINAL_STATUSES:
            raise JobStoreError(f"invalid pipeline status: {next_status}")
        if next_status == PIPELINE_STATUS_VERIFIED:
            # VERIFIED is a compound operation. Keep this compatibility entry
            # point, but route it through the same atomic command used by the
            # local poster so quota and Anna outbox cannot drift apart.
            return self.complete_verified(
                job_id,
                worker_id=worker_id,
                fence=fence,
                receipt=receipt or {},
                result=result,
            )
        now = time.time()
        with self._lock:
            with self._conn:
                row = self._conn.execute("SELECT * FROM pipeline_jobs WHERE id = ?", (str(job_id),)).fetchone()
                if row is None:
                    return False
                current = str(row["status"])
                if current in PIPELINE_TERMINAL_STATUSES and next_status != current:
                    return False
                # The only system-owned transition is UNKNOWN -> RECONCILING;
                # every worker mutation must prove the current, unexpired
                # lease and fencing token. This prevents a stale poster from
                # writing over a newer attempt after a restart.
                system_reconcile = (
                    next_status == PIPELINE_STATUS_RECONCILING
                    and current == PIPELINE_STATUS_UNKNOWN
                    and worker_id is None
                    and fence is None
                )
                if not system_reconcile and not self._pipeline_lease_matches(row, worker_id, fence, now=now):
                    return False
                fields = ["status = ?", "updated_at = ?"]
                params: list[object] = [next_status, now]
                if result is not None:
                    fields.append("result_json = ?")
                    params.append(_stable_json(result))
                if receipt is not None:
                    fields.append("receipt_json = ?")
                    params.append(_stable_json(receipt))
                if error_code is not None:
                    fields.append("error_code = ?")
                    params.append(str(error_code))
                if error_message is not None:
                    fields.append("error_message = ?")
                    params.append(str(error_message))
                if side_effect_boundary is not None:
                    fields.append("side_effect_boundary = ?")
                    params.append(str(side_effect_boundary))
                if next_status in PIPELINE_TERMINAL_STATUSES:
                    fields.extend(["finished_at = COALESCE(finished_at, ?)", "lease_owner = NULL", "lease_expires_at = NULL"])
                    params.append(now)
                elif next_status == PIPELINE_STATUS_UNKNOWN:
                    # The side effect may already exist; release the worker
                    # lease while retaining the quota reservation for explicit
                    # reconciliation.
                    fields.extend(["lease_owner = NULL", "lease_expires_at = NULL"])
                params.append(str(job_id))
                self._conn.execute(f"UPDATE pipeline_jobs SET {', '.join(fields)} WHERE id = ?", tuple(params))
                if next_status in PIPELINE_TERMINAL_STATUSES or next_status == PIPELINE_STATUS_UNKNOWN:
                    self._conn.execute("DELETE FROM pipeline_locks WHERE job_id = ?", (str(job_id),))
                if next_status in {PIPELINE_STATUS_FAILED, PIPELINE_STATUS_BLOCKED, PIPELINE_STATUS_CANCELLED}:
                    # Failure/cancellation must release a held draft slot in
                    # the same transaction as the state change.
                    self._conn.execute(
                        "UPDATE pipeline_quota SET status = 'released', released_at = ?, updated_at = ? WHERE job_id = ? AND status = 'held'",
                        (now, now, str(job_id)),
                    )
                event_key = f"pipeline.job.{next_status}:{job_id}:{int(row['attempt_count'] or 0)}"
                try:
                    job_payload = json.loads(row["payload_json"] or "{}")
                except (TypeError, ValueError):
                    job_payload = {}
                event_payload = {
                    "job_id": str(job_id),
                    "campaign_id": str(row["campaign_id"]),
                    "shop_id": str(row["shop_id"]),
                    "stage": str(row["stage"]),
                    "status": next_status,
                    "idempotency_key": event_key,
                }
                if isinstance(job_payload, dict) and job_payload.get("anna_task_id"):
                    event_payload["task_id"] = str(job_payload["anna_task_id"])
                if receipt is not None:
                    event_payload["receipt"] = dict(receipt)
                self._conn.execute(
                    "INSERT OR IGNORE INTO pipeline_outbox (event_id, job_id, event_type, idempotency_key, payload_json, created_at, delivered_at, attempts, last_error) VALUES (?, ?, ?, ?, ?, ?, NULL, 0, NULL)",
                    (f"event-{uuid.uuid4()}", str(job_id), f"pipeline.job.{next_status}", event_key, _stable_json(event_payload), now),
                )
                return True

    @staticmethod
    def _pipeline_lease_matches(
        row: sqlite3.Row,
        worker_id: str | None,
        fence: int | None,
        *,
        now: float,
    ) -> bool:
        if not worker_id or fence is None:
            return False
        expires = row["lease_expires_at"]
        return (
            str(row["status"] or "") == PIPELINE_STATUS_RUNNING
            and str(row["lease_owner"] or "") == str(worker_id)
            and int(row["fence"] or 0) == int(fence)
            and expires is not None
            and float(expires) > now
        )

    def _verified_receipt_errors(
        self,
        row: sqlite3.Row,
        receipt: Mapping[str, Any],
        *,
        worker_id: str | None,
        fence: int | None,
    ) -> list[str]:
        evidence = dict(receipt or {})
        errors: list[str] = []
        if str(row["stage"] or "") != PIPELINE_DRAFT_STAGE:
            errors.append("job stage must be create-verified-draft")
        if str(row["side_effect_boundary"] or "") != "etsy_write":
            errors.append("job does not have the local Etsy write boundary")
        if not row["product_id"]:
            errors.append("job must have a product_id before verification")
        for key, column in (
            ("job_id", "id"),
            ("campaign_id", "campaign_id"),
            ("candidate_id", "candidate_id"),
            ("shop_id", "shop_id"),
            ("product_id", "product_id"),
        ):
            if str(evidence.get(key) or "") != str(row[column] or ""):
                errors.append(f"receipt {key} does not match job")
        if worker_id and str(evidence.get("worker_id") or "") != str(worker_id):
            errors.append("receipt worker_id does not match lease owner")
        if fence is None or int(evidence.get("fence") or 0) != int(fence):
            errors.append("receipt fence does not match active lease")
        listing_id = str(evidence.get("listing_id") or evidence.get("etsy_listing_id") or "").strip()
        if not listing_id.isdigit():
            errors.append("receipt listing_id must be numeric")
        listing_status = str(evidence.get("listing_status") or evidence.get("status") or "").strip().lower()
        if listing_status != "draft":
            errors.append("receipt listing_status must be draft")
        readback_url = str(evidence.get("readback_url") or evidence.get("listing_url") or "").strip()
        if not readback_url or not (f"/listing/{listing_id}" in readback_url or f"/listing-editor/edit/{listing_id}" in readback_url):
            errors.append("receipt readback_url must identify the same Etsy listing")
        if evidence.get("readback_ok") is not True and evidence.get("readback_verified") is not True:
            errors.append("receipt readback verification is missing")
        image_hashes = evidence.get("image_hashes")
        if not isinstance(image_hashes, list) or len(image_hashes) != PipelinePolicy().image_count:
            errors.append("receipt must contain hashes for exactly 10 images")
        elif any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", item) for item in image_hashes):
            errors.append("receipt image hashes are invalid")
        file_hashes = evidence.get("customer_file_hashes")
        if not isinstance(file_hashes, list) or not (1 <= len(file_hashes) <= 5):
            errors.append("receipt must contain customer file hashes")
        elif any(not isinstance(item, str) or not re.fullmatch(r"[0-9a-fA-F]{64}", item) for item in file_hashes):
            errors.append("receipt customer file hashes are invalid")
        manifest_hash = str(evidence.get("manifest_sha256") or "")
        if not re.fullmatch(r"[0-9a-fA-F]{64}", manifest_hash):
            errors.append("receipt manifest_sha256 is required")
        expected_manifest_hash = str(row["manifest_sha256"] or "").strip().lower()
        if not expected_manifest_hash:
            errors.append("job has no accepted product manifest evidence")
        elif manifest_hash.lower() != expected_manifest_hash:
            errors.append("receipt manifest_sha256 does not match accepted manifest")
        try:
            accepted_manifest = json.loads(row["manifest_evidence_json"] or "{}")
        except (TypeError, ValueError):
            accepted_manifest = {}
        accepted_records = accepted_manifest.get("customer_file_evidence") if isinstance(accepted_manifest, dict) else None
        accepted_image_records = accepted_manifest.get("image_file_evidence") if isinstance(accepted_manifest, dict) else None
        expected_image_hashes = [
            str(item.get("sha256") or "").lower()
            for item in (accepted_image_records or [])
            if isinstance(item, dict)
        ]
        actual_image_hashes = [
            str(item).lower() for item in (image_hashes or []) if isinstance(item, str)
        ]
        if len(expected_image_hashes) != PipelinePolicy().image_count:
            errors.append("job has no accepted evidence for exactly 10 listing images")
        elif actual_image_hashes != expected_image_hashes:
            errors.append("receipt image hashes do not match accepted manifest evidence")
        expected_file_hashes = sorted(
            str(item.get("sha256") or "").lower()
            for item in (accepted_records or [])
            if isinstance(item, dict)
        )
        actual_file_hashes = sorted(
            str(item).lower() for item in (file_hashes or []) if isinstance(item, str)
        )
        if not expected_file_hashes:
            errors.append("job has no accepted customer-file evidence")
        elif actual_file_hashes != expected_file_hashes:
            errors.append("receipt customer file hashes do not match accepted manifest evidence")
        for key in ("asset_qa_ok", "seo_validated", "customer_files_validated"):
            if evidence.get(key) is not True:
                errors.append(f"receipt {key} must be true")
        return errors

    def _ensure_current_pipeline_quota(
        self,
        row: sqlite3.Row,
        quota: sqlite3.Row,
        *,
        policy: PipelinePolicy,
        now: float,
    ) -> sqlite3.Row:
        """Roll one held reservation into today's quota inside a transaction.

        The caller must already hold ``BEGIN IMMEDIATE``.  Keeping this logic
        in one store helper lets both the pre-save admission command and the
        post-save completion command enforce the same midnight boundary.
        """

        current_day = _pipeline_quota_date(policy)
        if str(quota["quota_date"]) == current_day:
            return quota
        counts = self._conn.execute(
            """
            SELECT COUNT(*) AS total,
                   SUM(CASE WHEN shop_id = ? THEN 1 ELSE 0 END) AS shop_count
            FROM pipeline_quota
            WHERE quota_date = ? AND status IN ('held', 'consumed') AND job_id <> ?
            """,
            (str(row["shop_id"]), current_day, str(row["id"])),
        ).fetchone()
        total = int(counts["total"] or 0)
        shop_count = int(counts["shop_count"] or 0)
        if total >= int(policy.max_drafts_per_day):
            raise JobStoreError(
                "current-day daily quota exhausted; reconcile the Etsy draft before retrying"
            )
        if shop_count >= int(policy.max_drafts_per_shop_per_day):
            raise JobStoreError(
                "current-day shop quota exhausted; reconcile the Etsy draft before retrying"
            )
        self._conn.execute(
            "UPDATE pipeline_quota SET quota_date = ?, updated_at = ? WHERE job_id = ? AND status = 'held'",
            (current_day, now, str(row["id"])),
        )
        updated = self._conn.execute(
            "SELECT * FROM pipeline_quota WHERE job_id = ? AND status = 'held'",
            (str(row["id"]),),
        ).fetchone()
        if updated is None:  # pragma: no cover - row was checked immediately before
            raise JobStoreError("held quota disappeared during rollover")
        return updated

    def prepare_pipeline_write(
        self,
        job_id: str,
        *,
        worker_id: str,
        fence: int,
        policy: PipelinePolicy | None = None,
    ) -> dict[str, Any]:
        """Revalidate the fenced Etsy write immediately before browser Save.

        This command performs no external side effect.  It atomically checks
        the active lease and held slot, rolls a reservation into the current
        policy day when necessary, and returns the reservation that the
        poster may use for the short Save/readback boundary.
        """

        active_policy = policy or PipelinePolicy()
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute(
                    "SELECT * FROM pipeline_jobs WHERE id = ?", (str(job_id),)
                ).fetchone()
                if row is None:
                    self._conn.rollback()
                    raise JobStoreError("pipeline job not found")
                if str(row["stage"]) != PIPELINE_DRAFT_STAGE or str(row["side_effect_boundary"] or "") != "etsy_write":
                    self._conn.rollback()
                    raise JobStoreError("job is not admitted to the Etsy write boundary")
                if not self._pipeline_lease_matches(row, worker_id, fence, now=now):
                    self._conn.rollback()
                    raise JobStoreError("active lease/fence is required before Etsy Save")
                quota = self._conn.execute(
                    "SELECT * FROM pipeline_quota WHERE job_id = ? AND status = 'held'",
                    (str(job_id),),
                ).fetchone()
                if quota is None:
                    self._conn.rollback()
                    raise JobStoreError("Etsy write requires a held quota slot")
                updated_quota = self._ensure_current_pipeline_quota(
                    row, quota, policy=active_policy, now=now
                )
                self._conn.commit()
                return self._pipeline_quota_result(updated_quota, True, "write_ready", active_policy)
            except Exception:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def complete_verified(
        self,
        job_id: str,
        *,
        worker_id: str | None,
        fence: int | None,
        receipt: Mapping[str, Any],
        result: Mapping[str, Any] | None = None,
        policy: PipelinePolicy | None = None,
    ) -> dict[str, Any]:
        """Atomically complete an Etsy draft with quota and Anna outbox.

        The receipt is accepted only from the current poster lease at the
        exact draft-writing stage. The job row, consumed quota reservation and
        outbound event commit together, eliminating crash windows between
        those three records.
        """
        active_policy = policy or PipelinePolicy()
        now = time.time()
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                row = self._conn.execute("SELECT * FROM pipeline_jobs WHERE id = ?", (str(job_id),)).fetchone()
                if row is None:
                    self._conn.rollback()
                    raise JobStoreError("pipeline job not found")
                if str(row["status"]) == PIPELINE_STATUS_VERIFIED:
                    # A replay of the exact completed receipt is idempotent;
                    # a different receipt cannot mutate a terminal job.
                    if str(row["receipt_json"] or "{}") == _stable_json(receipt):
                        event_key = f"pipeline.job.verified:{job_id}:{int(row['attempt_count'] or 0)}"
                        event = self._conn.execute("SELECT * FROM pipeline_outbox WHERE idempotency_key = ?", (event_key,)).fetchone()
                        self._conn.commit()
                        return {"job": self._pipeline_row_to_dict(row), "event": self._pipeline_outbox_row_to_dict(event) if event else None, "replayed": True}
                    self._conn.rollback()
                    raise JobStoreError("verified job already has a different receipt")
                if not self._pipeline_lease_matches(row, worker_id, fence, now=now):
                    self._conn.rollback()
                    raise JobStoreError("active lease/fence is required to complete verified draft")
                errors = self._verified_receipt_errors(row, receipt, worker_id=worker_id, fence=fence)
                if errors:
                    self._conn.rollback()
                    raise JobStoreError("; ".join(errors))
                quota = self._conn.execute(
                    "SELECT * FROM pipeline_quota WHERE job_id = ? AND status = 'held'", (str(job_id),)
                ).fetchone()
                if quota is None:
                    self._conn.rollback()
                    raise JobStoreError("verified draft requires a held quota slot")
                # Recheck the calendar boundary in the same transaction that
                # consumes the slot.  A long browser attempt cannot be
                # accounted against yesterday if it crosses midnight.
                self._ensure_current_pipeline_quota(
                    row, quota, policy=active_policy, now=now
                )
                receipt_json = _stable_json(receipt)
                result_json = _stable_json(result or {})
                self._conn.execute(
                    "UPDATE pipeline_jobs SET status = ?, result_json = ?, receipt_json = ?, updated_at = ?, finished_at = ?, lease_owner = NULL, lease_expires_at = NULL WHERE id = ?",
                    (PIPELINE_STATUS_VERIFIED, result_json, receipt_json, now, now, str(job_id)),
                )
                self._conn.execute("DELETE FROM pipeline_locks WHERE job_id = ?", (str(job_id),))
                self._conn.execute(
                    "UPDATE pipeline_quota SET status = 'consumed', updated_at = ? WHERE job_id = ? AND status = 'held'",
                    (now, str(job_id)),
                )
                event_key = f"pipeline.job.verified:{job_id}:{int(row['attempt_count'] or 0)}"
                try:
                    job_payload = json.loads(row["payload_json"] or "{}")
                except (TypeError, ValueError):
                    job_payload = {}
                payload = {
                    "job_id": str(job_id),
                    "campaign_id": str(row["campaign_id"]),
                    "shop_id": str(row["shop_id"]),
                    "stage": str(row["stage"]),
                    "status": PIPELINE_STATUS_VERIFIED,
                    "receipt": dict(receipt),
                    "idempotency_key": event_key,
                }
                if isinstance(job_payload, dict) and job_payload.get("anna_task_id"):
                    payload["task_id"] = str(job_payload["anna_task_id"])
                self._conn.execute(
                    "INSERT OR IGNORE INTO pipeline_outbox (event_id, job_id, event_type, idempotency_key, payload_json, created_at, delivered_at, attempts, last_error) VALUES (?, ?, ?, ?, ?, ?, NULL, 0, NULL)",
                    (f"event-{uuid.uuid4()}", str(job_id), "pipeline.job.verified", event_key, _stable_json(payload), now),
                )
                event = self._conn.execute("SELECT * FROM pipeline_outbox WHERE idempotency_key = ?", (event_key,)).fetchone()
                self._conn.commit()
                updated = self._conn.execute("SELECT * FROM pipeline_jobs WHERE id = ?", (str(job_id),)).fetchone()
                return {"job": self._pipeline_row_to_dict(updated), "event": self._pipeline_outbox_row_to_dict(event)}
            except Exception:
                if self._conn.in_transaction:
                    self._conn.rollback()
                raise

    def mark_pipeline_unknown(
        self,
        job_id: str,
        *,
        worker_id: str | None = None,
        fence: int | None = None,
        reason: str = "external_side_effect_outcome_unknown",
    ) -> bool:
        return self.transition_pipeline_job(
            job_id,
            PIPELINE_STATUS_UNKNOWN,
            worker_id=worker_id,
            fence=fence,
            error_code="OUTCOME_UNKNOWN",
            error_message=reason,
        )

    def begin_pipeline_reconciliation(self, job_id: str, *, reason: str = "reconciliation_requested") -> bool:
        row = self.get_pipeline_job(job_id)
        if not row or str(row.get("status")) != PIPELINE_STATUS_UNKNOWN:
            return False
        return self.transition_pipeline_job(
            job_id,
            PIPELINE_STATUS_RECONCILING,
            error_code="RECONCILING",
            error_message=reason,
        )

    def recover_pipeline_jobs(self, reason: str = "server_restart") -> dict[str, int]:
        """Recover stale worker leases without replaying an Etsy write."""

        now = time.time()
        queued = unknown = 0
        with self._lock:
            with self._conn:
                rows = self._conn.execute(
                    "SELECT id, side_effect_boundary, lease_expires_at FROM pipeline_jobs WHERE status = ?",
                    (PIPELINE_STATUS_RUNNING,),
                ).fetchall()
                for row in rows:
                    # A healthy worker lease survives a dashboard restart. A
                    # restart only recovers work whose lease has actually
                    # expired; this avoids killing an in-flight Etsy write.
                    if row["lease_expires_at"] is not None and float(row["lease_expires_at"]) > now:
                        continue
                    self._conn.execute("DELETE FROM pipeline_locks WHERE job_id = ?", (row["id"],))
                    if str(row["side_effect_boundary"] or "none") in {"etsy", "etsy_write", "external_write"}:
                        self._conn.execute(
                            """
                            UPDATE pipeline_jobs SET status = ?, error_code = ?,
                                error_message = ?, lease_owner = NULL,
                                lease_expires_at = NULL, updated_at = ? WHERE id = ?
                            """,
                            (PIPELINE_STATUS_UNKNOWN, "OUTCOME_UNKNOWN", f"{reason}: reconcile Etsy result before retry", now, row["id"]),
                        )
                        unknown += 1
                    else:
                        self._conn.execute(
                            """
                            UPDATE pipeline_jobs SET status = ?, error_code = ?,
                                error_message = ?, lease_owner = NULL,
                                lease_expires_at = NULL, updated_at = ? WHERE id = ?
                            """,
                            (PIPELINE_STATUS_QUEUED, "WORKER_RESTARTED", f"{reason}: queued for safe retry", now, row["id"]),
                        )
                        queued += 1
        return {"queued": queued, "unknown": unknown}

    def pipeline_quota_snapshot(
        self,
        *,
        quota_date: str | None = None,
        shop_ids: Iterable[str] | None = None,
        policy: PipelinePolicy | None = None,
    ) -> dict[str, Any]:
        active_policy = policy or PipelinePolicy()
        day = str(quota_date or _pipeline_quota_date(active_policy))
        with self._lock:
            rows = self._conn.execute(
                "SELECT shop_id, status, COUNT(*) AS count FROM pipeline_quota WHERE quota_date = ? AND status IN ('held', 'consumed') GROUP BY shop_id, status",
                (day,),
            ).fetchall()
        by_shop: dict[str, int] = {}
        for row in rows:
            by_shop[str(row["shop_id"])] = by_shop.get(str(row["shop_id"]), 0) + int(row["count"] or 0)
        if shop_ids is not None:
            allowed = {str(item) for item in shop_ids}
            by_shop = {key: value for key, value in by_shop.items() if key in allowed}
        used = sum(by_shop.values())
        return {
            "quota_date": day,
            "used": used,
            "remaining": max(0, int(active_policy.max_drafts_per_day) - used),
            "max_total": int(active_policy.max_drafts_per_day),
            "max_per_shop": int(active_policy.max_drafts_per_shop_per_day),
            "by_shop": {
                key: {
                    "used": value,
                    "remaining": max(0, int(active_policy.max_drafts_per_shop_per_day) - value),
                }
                for key, value in sorted(by_shop.items())
            },
        }

    def reserve_pipeline_quota(
        self,
        job_id: str,
        *,
        shop_id: str,
        quota_date: str | None = None,
        policy: PipelinePolicy | None = None,
        worker_id: str | None = None,
        fence: int | None = None,
    ) -> dict[str, Any]:
        """Atomically hold one daily draft slot for a pipeline job."""

        active_policy = policy or PipelinePolicy()
        day = _pipeline_quota_date(active_policy)
        requested_day = str(quota_date or "").strip()
        if requested_day and requested_day != day:
            raise JobStoreError("quota_date is server-owned and must be the current Asia/Ho_Chi_Minh day")
        shop = str(shop_id or "").strip()
        if not shop:
            raise JobStoreError("shop_id is required for quota reservation")
        if shop not in PIPELINE_ALLOWED_SHOPS:
            raise JobStoreError(f"unsupported pipeline shop: {shop}")
        now = time.time()
        reservation_id = f"quota-{uuid.uuid4()}"
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                job_row = self._conn.execute("SELECT * FROM pipeline_jobs WHERE id = ?", (str(job_id),)).fetchone()
                if job_row is None:
                    self._conn.rollback()
                    return {"reserved": False, "reason": "job_not_found", "job_id": str(job_id), "quota_date": day}
                if str(job_row["shop_id"]) != shop:
                    self._conn.rollback()
                    return {"reserved": False, "reason": "shop_mismatch", "job_id": str(job_id), "quota_date": day}
                if str(job_row["stage"]) != PIPELINE_DRAFT_STAGE:
                    self._conn.rollback()
                    return {"reserved": False, "reason": "wrong_stage", "job_id": str(job_id), "quota_date": day}
                if not self._pipeline_lease_matches(job_row, worker_id, fence, now=now):
                    self._conn.rollback()
                    return {"reserved": False, "reason": "active_lease_required", "job_id": str(job_id), "quota_date": day}
                existing = self._conn.execute("SELECT * FROM pipeline_quota WHERE job_id = ?", (str(job_id),)).fetchone()
                if existing is not None and str(existing["status"]) in {"held", "consumed"}:
                    self._conn.commit()
                    return self._pipeline_quota_result(existing, True, "already_reserved", active_policy)
                counts = self._conn.execute(
                    "SELECT COUNT(*) AS total, SUM(CASE WHEN shop_id = ? THEN 1 ELSE 0 END) AS shop_count FROM pipeline_quota WHERE quota_date = ? AND status IN ('held', 'consumed')",
                    (shop, day),
                ).fetchone()
                total = int(counts["total"] or 0)
                shop_count = int(counts["shop_count"] or 0)
                if total >= int(active_policy.max_drafts_per_day):
                    self._conn.rollback()
                    return {
                        "reserved": False,
                        "reason": "daily_quota_exhausted",
                        "quota_date": day,
                        "used": total,
                        "max_total": int(active_policy.max_drafts_per_day),
                        "max_per_shop": int(active_policy.max_drafts_per_shop_per_day),
                    }
                if shop_count >= int(active_policy.max_drafts_per_shop_per_day):
                    self._conn.rollback()
                    return {
                        "reserved": False,
                        "reason": "shop_quota_exhausted",
                        "quota_date": day,
                        "used": total,
                        "shop_used": shop_count,
                        "max_total": int(active_policy.max_drafts_per_day),
                        "max_per_shop": int(active_policy.max_drafts_per_shop_per_day),
                    }
                self._conn.execute(
                    "INSERT OR REPLACE INTO pipeline_quota (reservation_id, job_id, campaign_id, shop_id, quota_date, status, created_at, updated_at, released_at) SELECT ?, id, campaign_id, ?, ?, 'held', ?, ?, NULL FROM pipeline_jobs WHERE id = ?",
                    (reservation_id, shop, day, now, now, str(job_id)),
                )
                self._conn.commit()
                row = self._conn.execute("SELECT * FROM pipeline_quota WHERE job_id = ?", (str(job_id),)).fetchone()
                return self._pipeline_quota_result(row, True, "reserved", active_policy)
            except Exception:
                self._conn.rollback()
                raise

    def release_pipeline_quota(self, job_id: str) -> bool:
        with self._lock:
            with self._conn:
                result = self._conn.execute(
                    "UPDATE pipeline_quota SET status = 'released', released_at = ?, updated_at = ? WHERE job_id = ? AND status = 'held'",
                    (time.time(), time.time(), str(job_id)),
                )
                return bool(result.rowcount)

    def get_pipeline_quota_reservation(self, job_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM pipeline_quota WHERE job_id = ?", (str(job_id),)).fetchone()
            if row is None:
                return None
            return self._pipeline_quota_result(row, str(row["status"]) in {"held", "consumed"}, "read", PipelinePolicy())

    def consume_pipeline_quota(self, job_id: str) -> bool:
        with self._lock:
            with self._conn:
                result = self._conn.execute(
                    "UPDATE pipeline_quota SET status = 'consumed', updated_at = ? WHERE job_id = ? AND status = 'held'",
                    (time.time(), str(job_id)),
                )
                return bool(result.rowcount)

    def enqueue_pipeline_event(
        self,
        *,
        job_id: str,
        event_type: str,
        payload: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        """Write an Anna/outbound event once; delivery can be retried safely."""

        key = str(idempotency_key or f"{job_id}:{event_type}").strip()
        event_id = f"event-{uuid.uuid4()}"
        now = time.time()
        with self._lock:
            with self._conn:
                job_row = self._conn.execute(
                    "SELECT campaign_id, shop_id, stage, payload_json FROM pipeline_jobs WHERE id = ?",
                    (str(job_id),),
                ).fetchone()
                normalized_payload = dict(payload or {})
                if job_row is not None:
                    normalized_payload.setdefault("campaign_id", str(job_row["campaign_id"]))
                    normalized_payload.setdefault("shop_id", str(job_row["shop_id"]))
                    normalized_payload.setdefault("stage", str(job_row["stage"]))
                    try:
                        job_payload = json.loads(job_row["payload_json"] or "{}")
                    except (TypeError, ValueError):
                        job_payload = {}
                    if isinstance(job_payload, dict) and job_payload.get("anna_task_id"):
                        normalized_payload.setdefault("task_id", str(job_payload["anna_task_id"]))
                normalized_payload.setdefault("job_id", str(job_id))
                normalized_payload.setdefault("idempotency_key", key)
                self._conn.execute(
                    "INSERT OR IGNORE INTO pipeline_outbox (event_id, job_id, event_type, idempotency_key, payload_json, created_at, delivered_at, attempts, last_error) VALUES (?, ?, ?, ?, ?, ?, NULL, 0, NULL)",
                    (event_id, str(job_id), str(event_type), key, _stable_json(normalized_payload), now),
                )
                row = self._conn.execute("SELECT * FROM pipeline_outbox WHERE idempotency_key = ?", (key,)).fetchone()
                if row is None:
                    raise JobStoreError("failed to persist pipeline outbox event")
                return self._pipeline_outbox_row_to_dict(row)

    def get_pipeline_event(self, idempotency_key: str) -> dict[str, Any] | None:
        """Read one durable outbox event by its idempotency key."""

        key = str(idempotency_key or "").strip()
        if not key:
            return None
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM pipeline_outbox WHERE idempotency_key = ?",
                (key,),
            ).fetchone()
            return self._pipeline_outbox_row_to_dict(row) if row is not None else None

    def list_pipeline_outbox(self, *, pending_only: bool = True, limit: int = 100) -> list[dict[str, Any]]:
        where = "WHERE delivered_at IS NULL" if pending_only else ""
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM pipeline_outbox {where} ORDER BY created_at LIMIT ?",
                (max(1, min(int(limit or 100), 500)),),
            ).fetchall()
            return [self._pipeline_outbox_row_to_dict(row) for row in rows]

    def mark_pipeline_event_delivered(self, event_id: str, *, error: str | None = None) -> bool:
        with self._lock:
            with self._conn:
                if error:
                    result = self._conn.execute(
                        "UPDATE pipeline_outbox SET attempts = attempts + 1, last_error = ? WHERE event_id = ?",
                        (str(error), str(event_id)),
                    )
                else:
                    result = self._conn.execute(
                        "UPDATE pipeline_outbox SET attempts = attempts + 1, delivered_at = ?, last_error = NULL WHERE event_id = ?",
                        (time.time(), str(event_id)),
                    )
                return bool(result.rowcount)

    def _pipeline_row_to_dict(self, row: sqlite3.Row | None) -> dict[str, Any]:
        if row is None:
            return {}
        def parse(name: str, default: Any):
            raw = row[name] if name in row.keys() else None
            if raw in (None, ""):
                return default
            try:
                return json.loads(raw) if name.endswith("_json") else raw
            except Exception:
                return default
        return {
            "job_id": str(row["id"]),
            "campaign_id": str(row["campaign_id"]),
            "candidate_id": str(row["candidate_id"]),
            "product_id": str(row["product_id"] or ""),
            "shop_id": str(row["shop_id"]),
            "stage": str(row["stage"]),
            "status": str(row["status"]),
            "contract_version": str(row["contract_version"]),
            "input_version": str(row["input_version"]),
            "source_fingerprint": str(row["source_fingerprint"] or ""),
            "idempotency_key": str(row["idempotency_key"]),
            "payload": parse("payload_json", {}),
            "result": parse("result_json", {}),
            "receipt": parse("receipt_json", {}),
            "manifest_sha256": str(row["manifest_sha256"] or ""),
            "manifest_evidence": parse("manifest_evidence_json", {}),
            "manifest_source_job_id": str(row["manifest_source_job_id"] or ""),
            "error_code": row["error_code"],
            "error_message": row["error_message"],
            "side_effect_boundary": str(row["side_effect_boundary"] or "none"),
            "lease_owner": row["lease_owner"],
            "lease_expires_at": row["lease_expires_at"],
            "fence": int(row["fence"] or 0),
            "attempt_count": int(row["attempt_count"] or 0),
            "created_at": float(row["created_at"]),
            "updated_at": float(row["updated_at"]),
            "started_at": row["started_at"],
            "finished_at": row["finished_at"],
        }

    @staticmethod
    def _pipeline_outbox_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
        try:
            payload = json.loads(row["payload_json"] or "{}")
        except Exception:
            payload = {}
        return {
            "event_id": str(row["event_id"]),
            "job_id": str(row["job_id"]),
            "event_type": str(row["event_type"]),
            "idempotency_key": str(row["idempotency_key"]),
            "payload": payload,
            "created_at": float(row["created_at"]),
            "delivered_at": row["delivered_at"],
            "attempts": int(row["attempts"] or 0),
            "last_error": row["last_error"],
        }

    @staticmethod
    def _pipeline_quota_result(row: sqlite3.Row | None, ok: bool, reason: str, policy: PipelinePolicy) -> dict[str, Any]:
        if row is None:
            return {"reserved": False, "reason": reason}
        return {
            "reserved": bool(ok),
            "reason": reason,
            "reservation_id": str(row["reservation_id"]),
            "job_id": str(row["job_id"]),
            "campaign_id": str(row["campaign_id"]),
            "shop_id": str(row["shop_id"]),
            "quota_date": str(row["quota_date"]),
            "status": str(row["status"]),
            "quota": None,
        }

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

            # Durable candidate pipeline.  Do not fold these columns into the
            # legacy row update table: candidate/product UUIDs deliberately
            # exist before workbook rows and the recovery rules differ at an
            # external Etsy write boundary.
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pipeline_jobs (
                    id TEXT PRIMARY KEY,
                    campaign_id TEXT NOT NULL,
                    candidate_id TEXT NOT NULL,
                    product_id TEXT NOT NULL DEFAULT '',
                    shop_id TEXT NOT NULL,
                    stage TEXT NOT NULL,
                    status TEXT NOT NULL,
                    contract_version TEXT NOT NULL,
                    input_version TEXT NOT NULL,
                    source_fingerprint TEXT NOT NULL DEFAULT '',
                        idempotency_key TEXT NOT NULL UNIQUE,
                        payload_json TEXT NOT NULL DEFAULT '{}',
                        result_json TEXT NOT NULL DEFAULT '{}',
                        receipt_json TEXT NOT NULL DEFAULT '{}',
                        manifest_sha256 TEXT,
                        manifest_evidence_json TEXT NOT NULL DEFAULT '{}',
                        manifest_source_job_id TEXT,
                        error_code TEXT,
                    error_message TEXT,
                    side_effect_boundary TEXT NOT NULL DEFAULT 'none',
                    lease_owner TEXT,
                    lease_expires_at REAL,
                    fence INTEGER NOT NULL DEFAULT 0,
                    attempt_count INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    started_at REAL,
                    finished_at REAL
                )
                """,
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_scope ON pipeline_jobs (campaign_id, shop_id, status, updated_at)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_jobs_candidate ON pipeline_jobs (candidate_id, stage, created_at)")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pipeline_locks (
                    lock_key TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    owner TEXT NOT NULL,
                    fence INTEGER NOT NULL,
                    lease_expires_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                )
                """,
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_locks_job ON pipeline_locks (job_id)")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pipeline_quota (
                    reservation_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL UNIQUE,
                    campaign_id TEXT NOT NULL,
                    shop_id TEXT NOT NULL,
                    quota_date TEXT NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('held', 'consumed', 'released')),
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    released_at REAL
                )
                """,
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_quota_day ON pipeline_quota (quota_date, shop_id, status)")
            self._conn.execute(
                """
                CREATE TABLE IF NOT EXISTS pipeline_outbox (
                    event_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,
                    delivered_at REAL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT
                )
                """,
            )
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_pipeline_outbox_pending ON pipeline_outbox (delivered_at, created_at)")

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

            pipeline_columns = {row[1] for row in self._conn.execute("PRAGMA table_info(pipeline_jobs)")}
            if "manifest_sha256" not in pipeline_columns:
                self._conn.execute("ALTER TABLE pipeline_jobs ADD COLUMN manifest_sha256 TEXT")
            if "manifest_evidence_json" not in pipeline_columns:
                self._conn.execute(
                    "ALTER TABLE pipeline_jobs ADD COLUMN manifest_evidence_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "manifest_source_job_id" not in pipeline_columns:
                self._conn.execute("ALTER TABLE pipeline_jobs ADD COLUMN manifest_source_job_id TEXT")

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


def _pipeline_quota_date(policy: PipelinePolicy) -> str:
    """Return the policy-local calendar day used for draft reservations."""

    try:
        from zoneinfo import ZoneInfo

        zone = ZoneInfo(policy.schedule_timezone)
    except Exception:  # pragma: no cover - the bundled zoneinfo is available on macOS
        zone = timezone.utc
    return datetime.now(zone).date().isoformat()
