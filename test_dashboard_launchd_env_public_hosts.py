from __future__ import annotations

import os
from pathlib import Path
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent
WRAPPER = ROOT / "dashboard_launchd_env.command"
TOGETHER_ENV_NAMES = (
    "TOGETHER_API_KEY",
    "TOGETHER_API_BASE_URL",
    "TOGETHER_SEO_MODEL",
    "TOGETHER_MODEL",
    "TOGETHER_REASONING_EFFORT",
)
GENERIC_ERROR = "dashboard launchd environment: invalid allowed-hosts configuration\n"
TARGET_SCRIPT = r"""
if [[ "${ETSY_DASHBOARD_ALLOWED_HOSTS+x}" == "x" ]]; then hosts_set=true; else hosts_set=false; fi
if [[ "${ETSY_DASHBOARD_ALLOWED_HOSTS-}" == "$1" ]]; then hosts_match=true; else hosts_match=false; fi
printf 'hosts_set=%s\nhosts_match=%s\n' "$hosts_set" "$hosts_match"
"""


class DashboardLaunchdEnvPublicHostsTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(prefix="etsy-dashboard-hosts-")
        self.addCleanup(self.temp_dir.cleanup)
        self.root = Path(self.temp_dir.name)
        self.hosts_file = self.root / "public-hosts"

        self.base_env = os.environ.copy()
        self.base_env["ETSY_DASHBOARD_PUBLIC_HOSTS_FILE"] = str(self.hosts_file)
        self.base_env.pop("ETSY_DASHBOARD_ALLOWED_HOSTS", None)
        for name in TOGETHER_ENV_NAMES:
            # Explicit empty values prevent the wrapper from invoking launchctl.
            self.base_env[name] = ""

    def run_wrapper(self, expected_hosts: str = "", env_overrides: dict[str, str | None] | None = None):
        env = self.base_env.copy()
        for name, value in (env_overrides or {}).items():
            if value is None:
                env.pop(name, None)
            else:
                env[name] = value
        return subprocess.run(
            ["/bin/bash", str(WRAPPER), "/bin/bash", "-c", TARGET_SCRIPT, "target", expected_hosts],
            env=env,
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def assert_hosts_output(self, result: subprocess.CompletedProcess[str], *, is_set: bool, matches: bool = True):
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            result.stdout,
            f"hosts_set={'true' if is_set else 'false'}\nhosts_match={'true' if matches else 'false'}\n",
        )
        self.assertEqual(result.stderr, "")

    def assert_rejected_without_target(self, result: subprocess.CompletedProcess[str]):
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "", "Invalid configuration must stop before the target runs")
        self.assertEqual(result.stderr, GENERIC_ERROR)

    def test_missing_file_leaves_hosts_unset_for_loopback_default(self):
        self.assertFalse(self.hosts_file.exists())

        result = self.run_wrapper()

        self.assert_hosts_output(result, is_set=False)

    def test_valid_regular_file_is_exported_as_hosts_csv(self):
        value = "shop.example,localhost,127.0.0.1"
        self.hosts_file.write_text(value, encoding="ascii")

        result = self.run_wrapper(expected_hosts=value)

        self.assert_hosts_output(result, is_set=True)

    def test_process_environment_wins_over_valid_file(self):
        self.hosts_file.write_text("file.example", encoding="ascii")
        process_value = "process.example,192.168.1.2"

        result = self.run_wrapper(
            expected_hosts=process_value,
            env_overrides={"ETSY_DASHBOARD_ALLOWED_HOSTS": process_value},
        )

        self.assert_hosts_output(result, is_set=True)

    def test_explicit_empty_process_environment_wins_and_skips_invalid_file(self):
        self.hosts_file.write_text("https://invalid.example", encoding="ascii")

        result = self.run_wrapper(
            expected_hosts="",
            env_overrides={"ETSY_DASHBOARD_ALLOWED_HOSTS": ""},
        )

        self.assert_hosts_output(result, is_set=True)

    def test_invalid_files_are_rejected_without_disclosing_content_or_running_target(self):
        sentinel = self.root / "injection-ran"
        invalid_values = (
            "good.example,https://bad.example",
            f"good.example,$(touch {sentinel})",
            'good.example,"quoted.example"',
            "good.example\nbad.example",
            "good.example\x00bad.example",
            "good.example,,bad.example",
            "bad-.example",
            f"{'a' * 64}.example",
            ".".join(("a" * 63,) * 4),
            "999.1.2.3",
            "good.example:8090",
            "a" * 8193,
        )

        for value in invalid_values:
            with self.subTest(value_label=value[:24]):
                self.hosts_file.write_text(value, encoding="utf-8")
                result = self.run_wrapper()
                self.assert_rejected_without_target(result)
                self.assertFalse(sentinel.exists(), "The hosts file must never be evaluated")

    def test_invalid_process_environment_is_rejected_even_when_file_is_valid(self):
        self.hosts_file.write_text("good.example", encoding="ascii")

        result = self.run_wrapper(
            env_overrides={"ETSY_DASHBOARD_ALLOWED_HOSTS": "https://bad.example"},
        )

        self.assert_rejected_without_target(result)

    def test_symlink_and_nonfile_paths_are_rejected(self):
        valid_target = self.root / "valid-target"
        valid_target.write_text("good.example", encoding="ascii")
        symlink = self.root / "hosts-symlink"
        symlink.symlink_to(valid_target)
        nonfile = self.root / "hosts-directory"
        nonfile.mkdir()

        for path in (symlink, nonfile):
            with self.subTest(path_kind="symlink" if path.is_symlink() else "directory"):
                result = self.run_wrapper(
                    env_overrides={"ETSY_DASHBOARD_PUBLIC_HOSTS_FILE": str(path)},
                )
                self.assert_rejected_without_target(result)


if __name__ == "__main__":
    unittest.main()
