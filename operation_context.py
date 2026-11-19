#!/usr/bin/env python3
"""Immutable, validated operation context for Etsy dashboard job admission."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re
import time
import uuid
from typing import Mapping

from pipeline_contracts import CONTRACT_VERSION, PipelineStage, build_idempotency_key


_LISTING_URL_RE = re.compile(r"/(?:listing-editor/edit/|listing/)(\d+)")
_ROW_RE = re.compile(r"^[1-9][0-9]*$")


class OperationContextError(ValueError):
    """Raised when a request identity is stale, incomplete, or contradictory."""


@dataclass(frozen=True)
class PipelineOperationContext:
    """Immutable identity for one candidate-stage pipeline operation.

    Unlike :class:`OperationContext`, this context does not require a
    workbook row or Etsy listing id.  Research and asset creation therefore
    remain addressable after a restart, before a catalog row exists.
    """

    campaign_id: str
    shop_id: str
    candidate_id: str
    product_id: str
    stage: PipelineStage
    input_version: str
    source_checksum: str
    contract_version: str = CONTRACT_VERSION
    side_effect_boundary: str = "none"
    created_at: float = 0.0

    def __post_init__(self) -> None:
        if not self.campaign_id or not self.shop_id or not self.candidate_id:
            raise OperationContextError("Pipeline context thiếu campaign/shop/candidate")
        if not self.input_version:
            raise OperationContextError("Pipeline context thiếu input_version")
        if self.side_effect_boundary not in {"none", "etsy", "etsy_write", "external_write"}:
            raise OperationContextError("side_effect_boundary không hợp lệ")

    @property
    def idempotency_key(self) -> str:
        return build_idempotency_key(
            self.campaign_id,
            self.shop_id,
            self.product_id or self.candidate_id,
            self.stage,
            input_version=self.input_version,
            source_fingerprint_value=self.source_checksum,
            contract_version=self.contract_version,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "campaign_id": self.campaign_id,
            "shop_id": self.shop_id,
            "candidate_id": self.candidate_id,
            "product_id": self.product_id,
            "stage": self.stage.value,
            "input_version": self.input_version,
            "source_checksum": self.source_checksum,
            "contract_version": self.contract_version,
            "side_effect_boundary": self.side_effect_boundary,
            "idempotency_key": self.idempotency_key,
            "created_at": self.created_at,
        }


def build_pipeline_operation_context(
    *,
    campaign_id: str,
    shop_id: str,
    stage: str | PipelineStage,
    candidate_id: str | None = None,
    product_id: str | None = None,
    input_version: str = "1",
    source_checksum: str = "",
    contract_version: str = CONTRACT_VERSION,
    side_effect_boundary: str = "none",
) -> PipelineOperationContext:
    """Validate and freeze a pipeline identity at admission."""

    try:
        resolved_stage = stage if isinstance(stage, PipelineStage) else PipelineStage(str(stage).strip().lower())
    except ValueError as exc:
        raise OperationContextError(f"Pipeline stage không hợp lệ: {stage}") from exc
    campaign = str(campaign_id or "").strip()
    shop = str(shop_id or "").strip()
    candidate = str(candidate_id or "").strip() or f"candidate-{uuid.uuid4()}"
    product = str(product_id or "").strip()
    version = str(input_version or "").strip()
    checksum = str(source_checksum or "").strip()
    if not campaign or not shop:
        raise OperationContextError("Pipeline context thiếu campaign/shop")
    if not version:
        raise OperationContextError("Pipeline context thiếu input_version")
    return PipelineOperationContext(
        campaign_id=campaign,
        shop_id=shop,
        candidate_id=candidate,
        product_id=product,
        stage=resolved_stage,
        input_version=version,
        source_checksum=checksum,
        contract_version=str(contract_version or CONTRACT_VERSION),
        side_effect_boundary=str(side_effect_boundary or "none"),
        created_at=time.time(),
    )


_OPERATION_ALIASES = {
    "etsy-sync-from": "etsy_sync_from",
    "sync-from-etsy": "etsy_sync_from",
    "etsy_sync_from": "etsy_sync_from",
    "etsy-sync-from": "etsy_sync_from",
    "etsy_push_update": "etsy_push_update",
    "push-to-etsy": "etsy_push_update",
    "etsy-push-update": "etsy_push_update",
    "etsy_push": "etsy_push_update",
}


@dataclass(frozen=True)
class OperationContext:
    """Immutable request context resolved once at admission."""

    shop_id: str
    operation: str
    row: int
    folder: str
    listing_id: str
    request_id: str
    created_at: float

    @property
    def key(self) -> str:
        """Stable dedupe scope key for the operation."""

        scope = self.folder if self.folder else str(self.row)
        return f"{self.shop_id}:{self.operation}:{scope}"

    @property
    def context_key(self) -> str:
        return self.key

    def receipt(self, variant: str | None = None) -> str:
        seed = (
            f"{self.operation}|{self.shop_id}|{self.row}|{self.folder}|"
            f"{self.listing_id}|{self.request_id}|{variant or ''}"
        )
        return sha256(seed.encode("utf-8")).hexdigest()

    @classmethod
    def from_request(
        cls,
        *,
        row: int,
        payload: Mapping[str, object] | None,
        active_shop_id: str,
        current_folder: object,
        current_etsy_url: object,
        operation: str,
    ) -> "OperationContext":
        """Validate and freeze an operation context from request + current row."""

        normalized_operation = _normalize_operation(operation)
        if not normalized_operation:
            raise OperationContextError("operation không hợp lệ")

        safe_row = str(row or "").strip()
        if not _ROW_RE.fullmatch(safe_row):
            raise OperationContextError("Row không hợp lệ")
        resolved_row = int(safe_row)

        shop_id = str(active_shop_id or "").strip()
        if not shop_id:
            raise OperationContextError("Không tìm thấy shop đang hoạt động")

        folder = str(current_folder or "").strip()
        if not folder:
            raise OperationContextError("Dữ liệu sản phẩm thiếu folder")

        listing_id = extract_listing_id(str(current_etsy_url or ""))
        if not listing_id:
            raise OperationContextError("Sản phẩm chưa có Etsy listing ID hợp lệ")

        request_shop = str((payload or {}).get("shop") or "").strip()
        request_folder = str((payload or {}).get("folder") or "").strip()
        request_listing_id = str((payload or {}).get("listing_id") or "").strip()
        has_identity = any((request_shop, request_folder, request_listing_id))

        if has_identity:
            if not request_shop or not request_folder or not request_listing_id:
                raise OperationContextError("Payload định danh phải có đủ shop, folder, listing_id")
            if not request_listing_id.isdigit():
                raise OperationContextError("listing_id trong payload phải là số")

            if request_shop != shop_id:
                raise OperationContextError(
                    f"Shop không khớp: đang hoạt động={shop_id}, yêu cầu={request_shop}",
                )
            if request_folder != folder:
                raise OperationContextError(
                    f"Row {resolved_row} bị thay đổi: folder hiện tại '{folder}' không khớp '{request_folder}'",
                )
            if request_listing_id != listing_id:
                raise OperationContextError(
                    f"Row {resolved_row} bị thay đổi: listing hiện tại {listing_id} không khớp {request_listing_id}",
                )

        request_id = str((payload or {}).get("request_id") or "").strip()
        if not request_id:
            request_id = str(uuid.uuid4())

        return cls(
            shop_id=shop_id,
            operation=normalized_operation,
            row=resolved_row,
            folder=folder,
            listing_id=listing_id,
            request_id=request_id,
            created_at=time.time(),
        )


def _normalize_operation(value: str) -> str:
    raw = str(value or "").strip().lower()
    return _OPERATION_ALIASES.get(raw, raw)


def extract_listing_id(value: str) -> str:
    """Extract listing id from Etsy public or manager URL."""

    match = _LISTING_URL_RE.search(str(value or "").strip())
    return match.group(1) if match else ""


def build_operation_context(
    *,
    row: int,
    product: Mapping[str, object],
    payload: Mapping[str, object] | None,
    active_shop_id: str,
    operation: str,
) -> OperationContext:
    """Compatibility wrapper for previous local call-sites."""

    return OperationContext.from_request(
        operation=operation,
        row=row,
        payload=payload,
        active_shop_id=active_shop_id,
        current_folder=product.get("folder"),
        current_etsy_url=product.get("etsy_url"),
    )
