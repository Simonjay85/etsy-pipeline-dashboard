#!/usr/bin/env python3
"""Run the narrow Flake8 gate while preserving an explicit legacy baseline.

The project contains older broad exception handlers that predate the
E722/B902 pre-commit gate.  This wrapper keeps those known violations in a
reviewable JSON baseline and fails only when a violation with a new source
fingerprint is introduced.
"""

from __future__ import annotations

import argparse
import ast
from collections import Counter
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_BASELINE = ROOT / ".flake8-baseline.json"
VIOLATION_RE = re.compile(
    r"^(?P<path>.*?):(?P<line>\d+):(?P<column>\d+): "
    r"(?P<code>E722|B902) (?P<message>.*)$"
)


def _relative_path(raw_path: str, aliases: Mapping[str, str] | None = None) -> str:
    if aliases and raw_path in aliases:
        return aliases[raw_path]
    path = Path(raw_path)
    if not path.is_absolute():
        path = ROOT / path
    try:
        return path.resolve().relative_to(ROOT).as_posix()
    except ValueError:
        return path.as_posix()


def _scope_for_line(source_text: str, line_number: int) -> str:
    """Return a stable enclosing class/function path for a source line."""

    scopes: list[tuple[int, int, str]] = []
    try:
        tree = ast.parse(source_text)
    except (SyntaxError, ValueError):
        tree = None
    if tree is not None:
        for node in ast.walk(tree):
            if not isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            scopes.append((node.lineno, getattr(node, "end_lineno", node.lineno), node.name))
    containing = [item for item in scopes if item[0] <= line_number <= item[1]]
    if containing:
        containing.sort(key=lambda item: (item[0], item[1]))
        return ".".join(item[2] for item in containing)
    lines = source_text.splitlines()
    for index in range(min(line_number - 1, len(lines) - 1), -1, -1):
        match = re.match(r"^\s*(?:async\s+def|def|class)\s+([A-Za-z_]\w*)", lines[index])
        if match:
            return match.group(1)
    return "<module>"


def _source_line(
    relative_path: str,
    line_number: int,
    source_map: Mapping[str, str] | None = None,
) -> tuple[str, str]:
    if source_map and relative_path in source_map:
        source_text = source_map[relative_path]
    else:
        path = ROOT / relative_path
        try:
            source_text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return "", "<module>"
    lines = source_text.splitlines()
    if line_number < 1 or line_number > len(lines):
        return "", _scope_for_line(source_text, line_number)
    return re.sub(r"\s+", " ", lines[line_number - 1].strip()), _scope_for_line(
        source_text, line_number
    )


def _fingerprint(path: str, code: str, source: str, scope: str) -> str:
    payload = "\0".join((path, code, scope, source))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _parse_flake8_output(
    output: str,
    source_map: Mapping[str, str] | None = None,
    aliases: Mapping[str, str] | None = None,
) -> tuple[list[dict[str, Any]], list[str]]:
    violations: list[dict[str, Any]] = []
    unparsable: list[str] = []
    for raw_line in output.splitlines():
        if not raw_line.strip():
            continue
        match = VIOLATION_RE.match(raw_line)
        if match is None:
            unparsable.append(raw_line)
            continue
        path = _relative_path(match.group("path"), aliases)
        line_number = int(match.group("line"))
        code = match.group("code")
        source, scope = _source_line(path, line_number, source_map)
        violations.append(
            {
                "path": path,
                "line": line_number,
                "column": int(match.group("column")),
                "code": code,
                "message": match.group("message"),
                "source": source,
                "scope": scope,
                "fingerprint": _fingerprint(path, code, source, scope),
                "raw": raw_line,
            }
        )
    return violations, unparsable


def _load_baseline(path: Path) -> Counter[str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SystemExit(f"Unable to read Flake8 baseline {path}: {error}") from error
    if not isinstance(payload, dict) or payload.get("version") != 1:
        raise SystemExit(f"Unsupported Flake8 baseline format: {path}")
    entries = payload.get("violations", [])
    if not isinstance(entries, list):
        raise SystemExit(f"Invalid violations list in Flake8 baseline: {path}")
    fingerprints: Counter[str] = Counter()
    for entry in entries:
        if not isinstance(entry, dict) or not entry.get("fingerprint"):
            continue
        fingerprint = str(entry["fingerprint"])
        count = entry.get("count", 1)
        if not isinstance(count, int) or count < 1:
            raise SystemExit(f"Invalid count for Flake8 baseline entry: {path}")
        fingerprints[fingerprint] += count
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


def _staged_python_paths() -> list[str]:
    output = subprocess.check_output(["git", "ls-files", "--cached", "-z"], cwd=ROOT)
    return sorted(
        os.fsdecode(raw)
        for raw in output.split(b"\0")
        if raw and os.fsdecode(raw).endswith(".py")
    )


def _run_staged_flake8(
    filenames: list[str],
) -> tuple[int, str, str, dict[str, str], dict[str, str]]:
    """Run Flake8 against exact index bytes, preserving original path names."""

    with tempfile.TemporaryDirectory(prefix="flake8-index-") as temporary:
        temp_root = Path(temporary)
        temp_names: list[str] = []
        source_map: dict[str, str] = {}
        aliases: dict[str, str] = {}
        for filename in filenames:
            blob = subprocess.check_output(["git", "show", f":{filename}"], cwd=ROOT)
            original = Path(filename)
            temp_path = temp_root / original
            temp_path.parent.mkdir(parents=True, exist_ok=True)
            temp_path.write_bytes(blob)
            temp_name = str(temp_path)
            temp_names.append(temp_name)
            aliases[temp_name] = filename
            source_map[filename] = blob.decode("utf-8", errors="replace")
        return (*_run_flake8(temp_names), source_map, aliases)


def _write_baseline(path: Path, violations: list[dict[str, Any]]) -> None:
    unique: dict[str, dict[str, Any]] = {}
    counts: Counter[str] = Counter()
    for violation in violations:
        fingerprint = str(violation["fingerprint"])
        counts[fingerprint] += 1
        unique.setdefault(
            fingerprint,
            {
                "path": violation["path"],
                "code": violation["code"],
                "scope": violation.get("scope", "<module>"),
                "source": violation["source"],
                "fingerprint": fingerprint,
            },
        )
    for fingerprint, entry in unique.items():
        entry["count"] = counts[fingerprint]
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
        help=(
            "Replace the baseline with findings for supplied files; with no files, "
            "read the exact Git-index Python snapshot."
        ),
    )
    parser.add_argument("filenames", nargs="*", help="Python files to check")
    args = parser.parse_args(argv)

    filenames = [name for name in args.filenames if name.endswith(".py")]
    source_map: dict[str, str] | None = None
    aliases: dict[str, str] | None = None
    if not filenames:
        if not args.write_baseline:
            return 0
        filenames = _staged_python_paths()
        return_code, output, error_output, source_map, aliases = _run_staged_flake8(filenames)
    else:
        return_code, output, error_output = _run_flake8(filenames)

    violations, unparsable = _parse_flake8_output(
        output, source_map=source_map, aliases=aliases
    )
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
    remaining = Counter(known)
    new_violations: list[dict[str, Any]] = []
    for violation in violations:
        fingerprint = str(violation["fingerprint"])
        if remaining[fingerprint] > 0:
            remaining[fingerprint] -= 1
        else:
            new_violations.append(violation)
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
