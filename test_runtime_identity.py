#!/usr/bin/env python3
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import runtime_identity as ri


class RuntimeIdentityUnitTests(unittest.TestCase):
    def test_canonical_match(self) -> None:
        with tempfile.TemporaryDirectory(prefix="canonical-match-") as tmpdir:
            canonical = Path(tmpdir).resolve()
            with patch.object(ri, "canonical_root", return_value=canonical):
                self.assertTrue(ri.is_canonical_runtime(canonical))
                self.assertFalse(ri.is_canonical_runtime(canonical / "nested"))

    def test_startup_guard_refuses_non_canonical_without_override(self) -> None:
        with patch.object(ri, "canonical_root", return_value=Path("/canonical")):
            message = ri.startup_guard_message(Path("/not-canonical"))
        self.assertIsNotNone(message)
        self.assertIn("Refusing to start", message)

    def test_startup_guard_allows_non_canonical_when_override_set(self) -> None:
        with patch.object(ri, "canonical_root", return_value=Path("/canonical")):
            with patch.dict(os.environ, {ri.DEVELOPMENT_OVERRIDE_ENV: "1"}):
                message = ri.startup_guard_message(Path("/not-canonical"))
        self.assertIsNone(message)

    def test_canonical_root_override_environment_variable(self) -> None:
        with tempfile.TemporaryDirectory(prefix="etsy-canonical-") as tmpdir:
            with patch.dict(os.environ, {ri.CANONICAL_ROOT_ENV: tmpdir}):
                self.assertEqual(ri.canonical_root(), Path(tmpdir).resolve())

    def test_runtime_health_payload_shape_and_no_secret_fields(self) -> None:
        fixed_root = Path(tempfile.gettempdir())
        service_checks = {
            "vertex_app": True,
            "mlx_ai": False,
            "watcher": True,
            "running": ["__ETSY_SYNC__"],
            "running_tasks": ["task-a"],
        }
        source = {
            "commit": "abc123",
            "dirty": False,
            "file_hashes": {
                "dashboard_app.py": "hash-app",
                "dashboard_static/index.html": "hash-index",
                "dashboard_static/style.css": "hash-style",
                "dashboard_static/app.js": "hash-js",
            },
        }
        backup = {
            "loaded": {"daily": True, "weekly": True},
            "plists": {
                "daily": {"exists": True, "path": "/ignored/path/daily.plist"},
                "weekly": {"exists": True, "path": "/ignored/path/weekly.plist"},
            },
            "configured": True,
            "loaded_ok": True,
            "status_evidence": {
                "last_success": {"timestamp": "2026-08-08T12:00:00", "status": "success"},
                "last_failure": None,
            },
        }

        with patch.object(ri, "source_identity", return_value=source), \
             patch.object(ri, "backup_scheduler_identity", return_value=backup), \
             patch.object(ri, "runtime_environment", return_value={"python_version": "3.13"}), \
             patch.object(ri, "runtime_process_start_time_iso", return_value="2026-08-08T00:00:00+00:00"):
            payload = ri.runtime_health_payload(
                runtime_root=fixed_root,
                listen_host="127.0.0.1",
                listen_port=8090,
                service_readiness=service_checks,
                active_shop_id="templyshops",
                active_shop_name="Temply Studio",
            )

        self.assertEqual(payload["canonical_root"], str(ri.canonical_root()))
        self.assertEqual(payload["current_root"], str(fixed_root))
        self.assertEqual(payload["active_shop"], {"id": "templyshops", "name": "Temply Studio"})
        self.assertEqual(payload["services"], service_checks)
        self.assertEqual(payload["service_readiness"]["checks"], service_checks)
        self.assertFalse(payload["service_readiness"]["core"]["ok"])
        self.assertFalse(payload["service_readiness"]["optional"]["ok"])
        self.assertEqual(payload["service_readiness"]["optional"]["checks"], {
            "vertex_app": True,
            "mlx_ai": False,
            "watcher": True,
        })
        self.assertEqual(payload["source"]["commit"], "abc123")
        self.assertEqual(payload["frontend_assets"]["index_hash"], "hash-index")
        self.assertEqual(payload["process"]["listen"], {"host": "127.0.0.1", "port": 8090})
        self.assertIn("status_evidence", payload["backup_scheduler"])
        status_evidence = payload["backup_scheduler"].get("status_evidence", {})
        self.assertNotIn("evidence_file", status_evidence)
        self.assertNotIn("line", status_evidence.get("last_success") or {})
        self.assertNotIn("line", status_evidence.get("last_failure") or {})
        self.assertNotIn("sensitive-file-name", str(payload))

    def test_runtime_health_payload_core_ready_with_optional_offline(self) -> None:
        fixed_root = Path(tempfile.gettempdir())
        service_checks = {
            "vertex_app": False,
            "mlx_ai": False,
            "watcher": False,
            "running": ["__ETSY_SYNC__"],
            "running_tasks": ["task-a"],
        }
        source = {
            "commit": "abc123",
            "dirty": True,
            "file_hashes": {
                "dashboard_app.py": "hash-app",
                "dashboard_static/index.html": "hash-index",
                "dashboard_static/style.css": "hash-style",
                "dashboard_static/app.js": "hash-js",
            },
        }
        backup = {
            "loaded": {"daily": True, "weekly": True},
            "plists": {
                "daily": {"exists": True, "path": "/ignored/path/daily.plist"},
                "weekly": {"exists": True, "path": "/ignored/path/weekly.plist"},
            },
            "configured": True,
            "loaded_ok": True,
            "status_evidence": {
                "last_success": {"timestamp": "2026-08-08T12:00:00", "status": "success"},
                "last_failure": None,
            },
        }

        with patch.object(ri, "source_identity", return_value=source), \
             patch.object(ri, "backup_scheduler_identity", return_value=backup), \
             patch.object(ri, "runtime_environment", return_value={"python_version": "3.13"}), \
             patch.object(ri, "runtime_process_start_time_iso", return_value="2026-08-08T00:00:00+00:00"):
            payload = ri.runtime_health_payload(
                runtime_root=fixed_root,
                listen_host="127.0.0.1",
                listen_port=8090,
                service_readiness=service_checks,
                core_service_readiness={"ok": True, "checks": {"dashboard_endpoint": True}},
                optional_service_readiness={"ok": False, "checks": {"vertex_app": False, "mlx_ai": False, "watcher": False}},
                active_shop_id="templyshops",
                active_shop_name="Temply Studio",
            )

        self.assertTrue(payload["service_readiness"]["ok"])
        self.assertTrue(payload["service_readiness"]["core"]["ok"])
        self.assertFalse(payload["service_readiness"]["optional"]["ok"])
        self.assertFalse(payload["service_readiness"]["optional"]["checks"]["watcher"])
        self.assertEqual(payload["service_readiness"]["optional"]["checks"]["vertex_app"], False)

    def test_runtime_health_payload_backup_failure_timestamp_logic(self) -> None:
        fixed_root = Path(tempfile.gettempdir())
        source = {
            "commit": "abc123",
            "dirty": False,
            "file_hashes": {
                "dashboard_app.py": "hash-app",
                "dashboard_static/index.html": "hash-index",
                "dashboard_static/style.css": "hash-style",
                "dashboard_static/app.js": "hash-js",
            },
        }
        clean_backup = {
            "loaded": {"daily": True, "weekly": True},
            "plists": {
                "daily": {"exists": True, "path": "/ignored/path/daily.plist"},
                "weekly": {"exists": True, "path": "/ignored/path/weekly.plist"},
            },
            "configured": True,
            "loaded_ok": True,
            "status_evidence": {
                "last_success": None,
                "last_failure": None,
            },
        }
        with patch.object(ri, "source_identity", return_value=source), \
             patch.object(ri, "backup_scheduler_identity", return_value=clean_backup), \
             patch.object(ri, "runtime_environment", return_value={"python_version": "3.13"}), \
             patch.object(ri, "runtime_process_start_time_iso", return_value="2026-08-08T00:00:00+00:00"):
            payload_with_clean_scheduler = ri.runtime_health_payload(
                runtime_root=fixed_root,
                listen_host="127.0.0.1",
                listen_port=8090,
                service_readiness={"vertex_app": False, "mlx_ai": False, "watcher": False},
                active_shop_id="templyshops",
                active_shop_name="Temply Studio",
            )

        self.assertFalse(payload_with_clean_scheduler["health_summary"]["backup_last_failure"])

        failure_newer_backup = {
            "loaded": {"daily": True, "weekly": True},
            "plists": {
                "daily": {"exists": True, "path": "/ignored/path/daily.plist"},
                "weekly": {"exists": True, "path": "/ignored/path/weekly.plist"},
            },
            "configured": True,
            "loaded_ok": True,
            "status_evidence": {
                "last_success": {"timestamp": "2026-08-08T01:00:00", "status": "success"},
                "last_failure": {"timestamp": "2026-08-08T00:50:00", "status": "failure"},
            },
        }

        with patch.object(ri, "source_identity", return_value=source), \
             patch.object(ri, "backup_scheduler_identity", return_value=failure_newer_backup), \
             patch.object(ri, "runtime_environment", return_value={"python_version": "3.13"}), \
             patch.object(ri, "runtime_process_start_time_iso", return_value="2026-08-08T00:00:00+00:00"):
            payload_with_old_failure = ri.runtime_health_payload(
                runtime_root=fixed_root,
                listen_host="127.0.0.1",
                listen_port=8090,
                service_readiness={"vertex_app": False, "mlx_ai": False, "watcher": False},
                active_shop_id="templyshops",
                active_shop_name="Temply Studio",
            )

        self.assertFalse(payload_with_old_failure["health_summary"]["backup_last_failure"])

        failure_latest_backup = {
            "loaded": {"daily": True, "weekly": True},
            "plists": {
                "daily": {"exists": True, "path": "/ignored/path/daily.plist"},
                "weekly": {"exists": True, "path": "/ignored/path/weekly.plist"},
            },
            "configured": True,
            "loaded_ok": True,
            "status_evidence": {
                "last_success": {"timestamp": "2026-08-08T00:30:00", "status": "success"},
                "last_failure": {"timestamp": "2026-08-08T01:05:00", "status": "failure"},
            },
        }

        with patch.object(ri, "source_identity", return_value=source), \
             patch.object(ri, "backup_scheduler_identity", return_value=failure_latest_backup), \
             patch.object(ri, "runtime_environment", return_value={"python_version": "3.13"}), \
             patch.object(ri, "runtime_process_start_time_iso", return_value="2026-08-08T00:00:00+00:00"):
            payload_with_latest_failure = ri.runtime_health_payload(
                runtime_root=fixed_root,
                listen_host="127.0.0.1",
                listen_port=8090,
                service_readiness={"vertex_app": False, "mlx_ai": False, "watcher": False},
                active_shop_id="templyshops",
                active_shop_name="Temply Studio",
            )

        self.assertTrue(payload_with_latest_failure["health_summary"]["backup_last_failure"])

    def test_backup_failure_timestamp_comparison_accepts_mixed_timezone_forms(self) -> None:
        self.assertTrue(
            ri._backup_failure_is_current(
                {"timestamp": "2026-08-08T01:00:00+00:00"},
                {"timestamp": "2026-08-08T03:00:00+01:00"},
            )
        )

    def test_parse_backup_log_exposes_summary_only(self) -> None:
        with tempfile.TemporaryDirectory(prefix="runtime-health-log-") as tmpdir:
            root = Path(tmpdir)
            log_path = root / "backup.log"
            log_path.write_text(
                "\n".join(
                    [
                        "2026-08-08T12:00:00 Uploading /tmp/sensitive-product.zip",
                        "2026-08-08T12:00:01 backup success: uploaded /tmp/another-sensitive-name.zip",
                        "2026-08-08T12:00:02 ERROR: failed path /tmp/backup/error-secret.txt",
                    ]
                ),
                encoding="utf-8",
            )
            parsed = ri._parse_backup_log(log_path, root=root)

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed["last_failure"]["status"], "failure")
        self.assertEqual(parsed["last_success"]["status"], "success")
        self.assertNotIn("line", parsed["last_failure"])
        self.assertNotIn("line", parsed["last_success"])
        self.assertNotIn("sensitive-product.zip", str(parsed))
        self.assertNotIn("error-secret.txt", str(parsed))


if __name__ == "__main__":
    unittest.main()
