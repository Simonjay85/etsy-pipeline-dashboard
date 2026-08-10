#!/usr/bin/env python3
"""Run the narrow Flake8 gate while preserving an explicit legacy baseline.

The project contains older broad exception handlers that predate the
E722/B902 pre-commit gate.  This wrapper keeps those known violations in a
reviewable JSON baseline and fails only when a violation with a new source
fingerprint is introduced.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / ".flake8-baseline.json"
VIOLATION_RE = re.compile(
    r"^(?P<path>.*?):(?P<line>\d+):(?P<column>\d+): "
    r"(?P<code>E722|B902) (?P<message>.*)$"
)


def _relative_path(raw_path: str) -> str:
    path = Path(raw_path)
    if not path.is_absolute():
        path = ROOT / path
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _source_line(relative_path: str, line_number: int) -> str:
    path = ROOT / relative_path
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    if line_number < 1 or line_number > len(lines):
        return ""
    return re.sub(r"\s+", " ", lines[line_number - 1].strip())


def _fingerprint(path: str, code: str, source: str) -> str:
    payload = "\0".join((path, code, source))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_flake8_output(output: str) -> tuple[list[dict[str, Any]], list[str]]:
    violations: list[dict[str, Any]] = []
    unparsable: list[str] = []
    for raw_line in output.splitlines():
        if not raw_line.strip():
            continue
        match = VIOLATION_RE.match(raw_line)
        if match is None:
            unparsable.append(raw_line)
            continue
        path = _relative_path(match.group("path"))
        line_number = int(match.group("line"))
        code = match.group("code")
        source = _source_line(path, line_number)
        violations.append(
            {
                "path": path,
                "line": line_number,
                "column": int(match.group("column")),
                "code": code,
                "message": match.group("message"),
                "source": source,
                "fingerprint": _fingerprint(path, code, source),
                "raw": raw_line,
            }
        )
    return violations, unparsable


def _load_baseline(path: Path) -> set[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"Unable to read Flake8 baseline {path}: {error}") from error
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise SystemExit(f"Unsupported Flake8 baseline format: {path}")
    entries = payload.get("violations", [])
    if not isinstance(entries, list):
        raise SystemExit(f"Invalid violations list in Flake8 baseline: {path}")
    fingerprints = {
        str(entry["fingerprint"])
        for entry in entries
        if isinstance(entry, dict) and entry.get("fingerprint")
    }
    return fingerprints


def _run_flake8(filenames: list[str]) -> tuple[int, str, str]:
    command = [
        sys.executable,
        "-m",
        "flake8",
        "--select",
        "E722,B902",
        "--format=%(path)s:%(row)d:%(col)d: %(code)s %(text)s",
        *filenames,
    ]
    try:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except OSError as error:
        raise SystemExit(f"Unable to run Flake8: {error}") from error
    return completed.returncode, completed.stdout, completed.stderr


def _write_baseline(path: Path, violations: list[dict[str, Any]]) -> None:
    unique: dict[str, dict[str, Any]] = {}
    for violation in violations:
        fingerprint = str(violation["fingerprint"])
        unique.setdefault(
            fingerprint,
            {
                "path": violation["path"],
                "code": violation["code"],
                "source": violation["source"],
                "fingerprint": fingerprint,
            },
        )
    payload = {
        "version": 1,
        "description": (
            "Reviewed legacy E722/B902 findings. New source fingerprints still fail."
        ),
        "violations": sorted(
            unique.values(), key=lambda item: (item["path"], item["code"], item["source"])
        ),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument(
        "--write-baseline",
        action="store_true",
        help="Replace the baseline with the findings for the supplied files.",
    )
    parser.add_argument("filenames", nargs="*", help="Python files to check")
    args = parser.parse_args(argv)

    filenames = [name for name in args.filenames if name.endswith(".py")]
    if not filenames:
        return 0

    return_code, output, error_output = _run_flake8(filenames)
    violations, unparsable = _parse_flake8_output(output)
    stderr_lines = [line for line in error_output.splitlines() if line.strip()]
    unexpected_stderr = [
        line
        for line in stderr_lines
        if not (line.startswith("<unknown>:") and "SyntaxWarning:" in line)
    ]

    if args.write_baseline:
        _write_baseline(args.baseline, violations)
        return 0

    baseline_path = args.baseline if args.baseline.is_absolute() else ROOT / args.baseline
    known = _load_baseline(baseline_path)
    new_violations = [
        violation for violation in violations if violation["fingerprint"] not in known
    ]
    if new_violations or unparsable or unexpected_stderr or (return_code and not violations):
        for violation in new_violations:
            print(violation["raw"])
        for line in unparsable:
            print(line)
        for line in unexpected_stderr:
            print(line, file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
