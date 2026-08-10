#!/usr/bin/env python3
"""Immutable, validated operation context for Etsy dashboard job admission."""

from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
import re
import time
import uuid
from typing import Mapping


_LISTING_URL_RE = re.compile(r"/(?:listing-editor/edit/|listing/)(\d+)")
_ROW_RE = re.compile(r"^[1-9][0-9]*$")


class OperationContextError(ValueError):
    """Raised when a request identity is stale, incomplete, or contradictory."""


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
