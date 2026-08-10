from __future__ import annotations

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import cloud_asset_cli
from cloud_asset_store_config import CloudAssetConfig


class CloudAssetCliTests(unittest.TestCase):
    def test_parser_exposes_all_phase_one_commands_and_safe_defaults(self) -> None:
        parser = cloud_asset_cli.build_parser()
        for command in ("upload", "verify", "restore", "status", "inventory", "maintain"):
            args = parser.parse_args([command] + (["--path", "master_products/product-01"] if command in {"upload", "verify", "restore", "status"} else []))
            self.assertEqual(args.command, command)
        maintain = parser.parse_args(["maintain"])
        self.assertFalse(maintain.apply)
        self.assertFalse(maintain.policy_enabled)

    def test_maintain_is_dry_run_without_explicit_apply(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = CloudAssetConfig(repo_root=root)
            fake_store = Mock()
            fake_store.repo_root = root
            fake_store.maintain.return_value = [
                {
                    "ok": True,
                    "product": "master_products/product-01",
                    "state": "OFFLOAD_SCHEDULED",
                    "would_offload": True,
                    "applied": False,
                }
            ]
            args = cloud_asset_cli.build_parser().parse_args(
                [
                    "--repo-root",
                    str(root),
                    "maintain",
                    "--policy-enabled",
                    "--allow",
                    "master_products/product-01",
                ]
            )
            output = io.StringIO()
            with patch.object(cloud_asset_cli, "build_store", return_value=(fake_store, config)):
                with contextlib.redirect_stdout(output):
                    exit_code = cloud_asset_cli.main(vars_to_argv(args))
            self.assertEqual(exit_code, 0)
            fake_store.maintain.assert_called_once_with(
                product_roots=None,
                apply=False,
                offload_enabled=True,
                allowlist=("master_products/product-01",),
                older_than_days=7,
            )
            self.assertIn('"dry_run": true', output.getvalue())

    def test_apply_requires_the_cli_flag_even_when_policy_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = CloudAssetConfig(
                repo_root=root,
                offload_enabled=True,
                offload_allowlist=("master_products/product-01",),
            )
            fake_store = Mock()
            fake_store.repo_root = root
            fake_store.maintain.return_value = []
            args = cloud_asset_cli.build_parser().parse_args(
                ["--repo-root", str(root), "maintain", "--apply"]
            )
            with patch.object(cloud_asset_cli, "build_store", return_value=(fake_store, config)):
                cloud_asset_cli.run(args)
            fake_store.maintain.assert_called_once_with(
                product_roots=None,
                apply=True,
                offload_enabled=True,
                allowlist=("master_products/product-01",),
                older_than_days=7,
            )

    def test_build_store_passes_configured_offload_age_to_store(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            config = CloudAssetConfig(repo_root=root, offload_age_days=30)
            args = cloud_asset_cli.build_parser().parse_args(
                ["--repo-root", str(root), "inventory"]
            )
            with patch.object(cloud_asset_cli, "load_config", return_value=config), patch.object(
                cloud_asset_cli, "CloudAssetStore"
            ) as store_class:
                cloud_asset_cli.build_store(args)

            self.assertEqual(store_class.call_args.kwargs["offload_age_days"], 30)


def vars_to_argv(args: SimpleNamespace) -> list[str]:
    """Round-trip only the fields needed by the parser test without a shell."""

    values = ["--repo-root", str(args.repo_root), "maintain", "--policy-enabled", "--allow", args.allowlist[0]]
    return values


if __name__ == "__main__":
    unittest.main()
