#!/usr/bin/env python3
"""CLI for the Phase 1 immutable cloud asset store.

Examples (all product paths are relative to the canonical repository by
default)::

    python3 cloud_asset_cli.py upload --path shops/templystudios/product-01
    python3 cloud_asset_cli.py verify --path master_products/product-01
    python3 cloud_asset_cli.py restore --path shops/templystudios/product-01
    python3 cloud_asset_cli.py status --path shops/templystudios/product-01 --check-remote
    python3 cloud_asset_cli.py inventory
    python3 cloud_asset_cli.py maintain --dry-run

Maintenance is dry-run unless ``--apply`` is explicitly supplied.  The policy
must also be enabled in the secret-free config file/environment and the exact
product key must be in the allowlist.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from pathlib import Path
from typing import Any, List, Optional, Sequence

from cloud_asset_store import CloudAssetError, CloudAssetStore, discover_product_roots
from cloud_asset_store_config import CloudAssetConfig, load_config


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--repo-root",
        default=str(Path(__file__).resolve().parent),
        help="canonical Etsy repository root (default: this script's repository)",
    )
    parser.add_argument("--config", type=Path, help="optional JSON cloud asset config")
    parser.add_argument("--remote", help="rclone remote name override")
    parser.add_argument("--parent-id", help="Google Drive parent folder ID override")
    parser.add_argument("--rclone-bin", help="rclone executable override")
    parser.add_argument("--cache-root", type=Path, help="hydration/audit cache root override")

    subparsers = parser.add_subparsers(dest="command", required=True)

    for name in ("upload", "verify", "restore", "status"):
        command = subparsers.add_parser(name, help=f"{name} one product")
        command.add_argument("--path", required=True, help="shops/<shop>/<product> or master_products/<product>")
        if name == "restore":
            command.add_argument("--force", action="store_true", help="replace dirty local content")
        if name == "status":
            command.add_argument("--check-remote", action="store_true", help="perform read-only remote verification")
        if name == "upload":
            command.add_argument("--revision", help="safe immutable revision ID; generated when omitted")

    inventory = subparsers.add_parser("inventory", help="list canonical product roots and local states")
    inventory.add_argument("--check-remote", action="store_true", help="perform read-only remote verification")

    maintain = subparsers.add_parser("maintain", help="dry-run or safely offload eligible products")
    maintain.add_argument(
        "--path",
        action="append",
        help="limit to one or more product paths; defaults to the canonical inventory",
    )
    maintain_mode = maintain.add_mutually_exclusive_group()
    maintain_mode.add_argument("--apply", action="store_true", help="explicitly permit deletion after every safety gate")
    maintain_mode.add_argument("--dry-run", action="store_true", help="show eligible work without deleting (default)")
    maintain.add_argument(
        "--policy-enabled",
        action="store_true",
        help="enable offload policy for this invocation in addition to config",
    )
    maintain.add_argument(
        "--allow",
        action="append",
        dest="allowlist",
        help="exact product key to allow (repeatable); config allowlist is used when omitted",
    )
    maintain.add_argument("--older-than-days", type=int, help="eligibility age; minimum is 7 days")

    return parser


def build_store(args: argparse.Namespace) -> tuple[CloudAssetStore, CloudAssetConfig]:
    root = Path(args.repo_root).expanduser().absolute()
    config = load_config(root, args.config)
    overrides = {}
    if args.remote:
        overrides["remote"] = args.remote
    if args.parent_id:
        overrides["parent_id"] = args.parent_id
    if args.rclone_bin:
        overrides["rclone_bin"] = args.rclone_bin
    if args.cache_root:
        overrides["cache_root"] = args.cache_root
    if overrides:
        config = replace(config, **overrides)
    store = CloudAssetStore(
        repo_root=config.repo_root,
        remote=config.remote,
        parent_id=config.parent_id,
        rclone_bin=config.rclone_bin,
        cache_root=config.cache_root,
        lock_timeout_seconds=config.lock_timeout_seconds,
        success_ttl_seconds=config.success_ttl_seconds,
        failure_ttl_seconds=config.failure_ttl_seconds,
        offload_age_days=config.offload_age_days,
    )
    return store, config


def _output(value: Any) -> None:
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2, default=str))


def _inventory(store: CloudAssetStore, check_remote: bool) -> List[dict]:
    items = []
    for product_path, identity in discover_product_roots(store.repo_root):
        items.append(store.status(product_path, check_remote=check_remote))
    return items


def run(args: argparse.Namespace) -> Any:
    store, config = build_store(args)
    if args.command == "upload":
        return store.upload(args.path, revision=args.revision)
    if args.command == "verify":
        return store.verify(args.path)
    if args.command == "restore":
        return store.restore(args.path, force=args.force)
    if args.command == "status":
        return store.status(args.path, check_remote=args.check_remote)
    if args.command == "inventory":
        return {
            "ok": True,
            "config": config.public_dict(),
            "items": _inventory(store, args.check_remote),
        }
    if args.command == "maintain":
        enabled = bool(config.offload_enabled or args.policy_enabled)
        allowlist = tuple(args.allowlist) if args.allowlist else config.offload_allowlist
        age_days = args.older_than_days if args.older_than_days is not None else config.offload_age_days
        results = store.maintain(
            product_roots=args.path,
            apply=bool(args.apply),
            offload_enabled=enabled,
            allowlist=allowlist,
            older_than_days=age_days,
        )
        return {
            "ok": all(bool(item.get("ok")) for item in results),
            "dry_run": not bool(args.apply),
            "policy_enabled": enabled,
            "allowlist": list(allowlist),
            "older_than_days": age_days,
            "results": results,
        }
    raise CloudAssetError(f"unsupported command: {args.command}")


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        _output(run(args))
    except (CloudAssetError, OSError, ValueError, TypeError) as exc:
        print(f"cloud asset error: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
