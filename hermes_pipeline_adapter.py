"""Hermes Kanban adapter for the Etsy product pipeline.

The adapter is deliberately transport-light: it builds the exact JSON task
contract and an argument vector for ``hermes kanban create --json``.  Calling
``dispatch_kanban_task`` is an explicit operator/worker action; importing this
module never touches the Hermes database, gateway or network.  That keeps the
Etsy Dashboard JobStore authoritative and makes retries safe to reason about.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence


class HermesAdapterError(ValueError):
    """Raised when a Hermes task request is outside the Etsy contract."""


PROFILE_ALIASES: dict[str, str] = {"reserach": "research"}
KNOWN_PROFILES = frozenset({"research", "download", "make-image", "organizer", "tai-hinh"})
STAGE_PROFILES: dict[str, str] = {
    "research": "research",
    "download": "download",
    "acquire-source": "download",
    "create": "organizer",
    "create-product": "organizer",
    "verify": "organizer",
    "verify-assets": "organizer",
    "image_gen": "make-image",
    "generate-images": "make-image",
    "seo": "research",
    "seo-package": "research",
}
WORKSPACE = Path(__file__).resolve().parent


def normalize_profile(profile: str) -> str:
    value = str(profile or "").strip().lower()
    value = PROFILE_ALIASES.get(value, value)
    if value not in KNOWN_PROFILES:
        raise HermesAdapterError(f"Unknown Hermes profile: {profile!r}")
    return value


def _bounded(value: object, name: str, *, max_length: int = 180) -> str:
    text = str(value or "").strip()
    if not text or len(text) > max_length or "\x00" in text:
        raise HermesAdapterError(f"{name} is empty or too long")
    return text


def _canonical_digest(payload: Mapping[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class HermesTaskRequest:
    campaign_id: str
    shop_id: str
    candidate_id: str
    product_id: str
    stage: str
    title: str
    brief: str = ""
    profile: str | None = None
    input_version: str = "1"
    source_fingerprint: str = ""
    contract_version: str = "1.0"
    side_effect_boundary: str = "none"
    workspace: Path = WORKSPACE

    def __post_init__(self) -> None:
        for name in ("campaign_id", "shop_id", "candidate_id", "product_id", "stage", "title", "input_version", "contract_version"):
            _bounded(getattr(self, name), name)
        stage = self.stage.strip().lower()
        if stage not in STAGE_PROFILES:
            raise HermesAdapterError(f"Unsupported Etsy pipeline stage: {self.stage!r}")
        if self.side_effect_boundary not in {"none", "external_read"}:
            raise HermesAdapterError(
                "Hermes is read/asset/text-only; Etsy write stages belong to the local poster"
            )
        if self.brief and len(self.brief) > 12000:
            raise HermesAdapterError("brief is too long")
        if self.source_fingerprint and len(self.source_fingerprint) > 160:
            raise HermesAdapterError("source_fingerprint is too long")
        object.__setattr__(self, "stage", stage)
        object.__setattr__(self, "profile", normalize_profile(self.profile or STAGE_PROFILES[stage]))
        root = Path(self.workspace).expanduser().resolve()
        if root != WORKSPACE:
            # The Etsy contract is bound to one canonical checkout.  Allowing
            # an arbitrary absolute path here would let a Hermes card route
            # work into an unrelated project or stale duplicate tree.
            raise HermesAdapterError(f"workspace must be the canonical Etsy checkout: {WORKSPACE}")
        object.__setattr__(self, "workspace", root)

    @property
    def idempotency_key(self) -> str:
        return _canonical_digest({
            "contract_version": self.contract_version,
            "campaign_id": self.campaign_id,
            "shop_id": self.shop_id,
            "candidate_id": self.candidate_id,
            "product_id": self.product_id,
            "stage": self.stage,
            "input_version": self.input_version,
            "source_fingerprint": self.source_fingerprint,
        })

    def body(self) -> dict[str, Any]:
        return {
            "schema": "etsy.hermes-kanban-task.v1",
            "contract_version": self.contract_version,
            "campaign_id": self.campaign_id,
            "shop_id": self.shop_id,
            "candidate_id": self.candidate_id,
            "product_id": self.product_id,
            "stage": self.stage,
            "input_version": self.input_version,
            "source_fingerprint": self.source_fingerprint,
            "side_effect_boundary": self.side_effect_boundary,
            "workspace": str(self.workspace),
            "source_policy": {"source_assets_separate": True, "customer_files_only": True},
            "requires_dashboard_verification": True,
            "publish_allowed": False,
            "brief": self.brief,
        }


def build_kanban_create_argv(
    request: HermesTaskRequest,
    *,
    hermes_bin: str | Path | None = None,
) -> list[str]:
    """Build a shell-free Hermes Kanban CLI invocation.

    The returned argv can be passed directly to ``subprocess.run``.  It never
    embeds credentials, shell operators or a user-controlled command string.
    """

    binary = str(hermes_bin or shutil.which("hermes") or "hermes")
    title = f"Etsy · {request.shop_id} · {request.stage} · {request.product_id}"
    return [
        binary,
        "kanban",
        "create",
        title,
        "--body",
        json.dumps(request.body(), ensure_ascii=False, sort_keys=True),
        "--assignee",
        request.profile or STAGE_PROFILES[request.stage],
        "--project",
        "etsy",
        "--workspace",
        f"dir:{request.workspace}",
        "--idempotency-key",
        request.idempotency_key,
        "--max-retries",
        "1",
        "--initial-status",
        "running",
        "--json",
    ]


def parse_kanban_result(value: str | bytes | Mapping[str, Any]) -> dict[str, Any]:
    if isinstance(value, Mapping):
        result = dict(value)
    else:
        try:
            result = json.loads(value)
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise HermesAdapterError("Hermes Kanban did not return JSON") from exc
    if not isinstance(result, dict):
        raise HermesAdapterError("Hermes Kanban result must be a JSON object")
    task_id = result.get("id") or result.get("task_id")
    if task_id is None or not str(task_id).strip():
        raise HermesAdapterError("Hermes Kanban result has no task id")
    return result


def dispatch_kanban_task(
    request: HermesTaskRequest,
    *,
    runner: Callable[..., Any] | None = None,
    hermes_bin: str | Path | None = None,
) -> dict[str, Any]:
    """Explicitly submit a task and return the durable Hermes task record.

    Tests and a future Mac worker can inject a runner.  The default uses an
    argv-only subprocess with a bounded timeout; it never invokes a shell.
    """

    argv = build_kanban_create_argv(request, hermes_bin=hermes_bin)
    if runner is not None:
        raw = runner(argv)
    else:
        completed = subprocess.run(
            argv,
            cwd=str(request.workspace),
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if completed.returncode != 0:
            raise HermesAdapterError(f"Hermes Kanban create failed with exit {completed.returncode}")
        raw = completed.stdout
    result = parse_kanban_result(raw)
    return {
        "task_id": str(result.get("id") or result.get("task_id")),
        "hermes": result,
        "request": request.body(),
        "idempotency_key": request.idempotency_key,
    }


def build_hermes_task(**kwargs: Any) -> HermesTaskRequest:
    """Typed convenience constructor used by Dashboard/Mac worker adapters."""

    return HermesTaskRequest(**kwargs)


__all__ = [
    "HermesAdapterError",
    "HermesTaskRequest",
    "KNOWN_PROFILES",
    "PROFILE_ALIASES",
    "STAGE_PROFILES",
    "build_hermes_task",
    "build_kanban_create_argv",
    "dispatch_kanban_task",
    "normalize_profile",
    "parse_kanban_result",
]
