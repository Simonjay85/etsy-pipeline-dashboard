#!/usr/bin/env python3
"""Offline regression checks for dashboard child-process credential boundaries."""

from __future__ import annotations

import ast
import os
from pathlib import Path

import pytest

import dashboard_app


_DASHBOARD_SOURCE = Path(dashboard_app.__file__).read_text(encoding="utf-8")
_OPT_IN_FUNCTIONS = {
    "_run_etsy_updater",
    "_run_poster",
    "_run_batch_poster",
    "_run_selected_poster",
    "api_repair_listing",
}


@pytest.fixture(autouse=True)
def mock_external_credentials_and_network(monkeypatch):
    """Keep this module independent from Keychain, HTTP clients, and live Etsy."""

    monkeypatch.setattr(dashboard_app.ai_credentials, "load_config", lambda: None)
    monkeypatch.setattr(dashboard_app.ai_credentials, "save_config", lambda _config: None)
    monkeypatch.setattr(dashboard_app.ai_credentials, "delete_config", lambda: None)
    monkeypatch.setattr(
        dashboard_app,
        "_get_httpx",
        lambda: pytest.fail("child-credential tests must not make network calls"),
    )


def test_child_env_scrubs_together_credentials_by_default(monkeypatch):
    monkeypatch.setenv("TOGETHER_API_KEY", "together-secret")
    monkeypatch.setenv("TOGETHER_API_BASE_URL", "https://provider.example.test/v1")
    monkeypatch.setenv("TOGETHER_SEO_MODEL", "provider/seo-model")
    monkeypatch.setenv("TOGETHER_MODEL", "provider/child-model")
    monkeypatch.setenv("TOGETHER_REASONING_EFFORT", "high")
    monkeypatch.setenv("TOGETHER_UNLISTED_SETTING", "also-secret")
    monkeypatch.setenv("ETSY_DASHBOARD_USE_ENV_TOGETHER", "1")
    monkeypatch.setenv("PYTHONUNBUFFERED", "0")
    monkeypatch.setenv("DASHBOARD_UNRELATED_SETTING", "preserved")

    child_env = dashboard_app._together_child_env()

    assert all(not name.startswith("TOGETHER_") for name in child_env)
    assert "ETSY_DASHBOARD_USE_ENV_TOGETHER" not in child_env
    assert child_env["PATH"] == os.environ["PATH"]
    assert child_env["DASHBOARD_UNRELATED_SETTING"] == "preserved"
    assert child_env["PYTHONUNBUFFERED"] == "1"


def test_child_env_opt_in_passes_provider_credentials(monkeypatch):
    provider_values = {
        "TOGETHER_API_KEY": "together-secret",
        "TOGETHER_API_BASE_URL": "https://provider.example.test/v1",
        "TOGETHER_SEO_MODEL": "provider/seo-model",
        "TOGETHER_MODEL": "provider/child-model",
        "TOGETHER_REASONING_EFFORT": "high",
        "TOGETHER_UNLISTED_SETTING": "also-secret",
        "ETSY_DASHBOARD_USE_ENV_TOGETHER": "1",
    }
    for name, value in provider_values.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setenv("DASHBOARD_UNRELATED_SETTING", "preserved")

    child_env = dashboard_app._together_child_env(include_together=True)

    for name, value in provider_values.items():
        assert child_env[name] == value
    assert child_env["PATH"] == os.environ["PATH"]
    assert child_env["DASHBOARD_UNRELATED_SETTING"] == "preserved"
    assert child_env["PYTHONUNBUFFERED"] == "1"


def _subprocess_calls(tree: ast.AST):
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        owner = node.func.value
        if (
            node.func.attr == "create_subprocess_exec"
            and isinstance(owner, ast.Name)
            and owner.id == "asyncio"
        ) or (
            node.func.attr == "Popen"
            and isinstance(owner, ast.Name)
            and owner.id == "subprocess"
        ):
            yield node


def test_subprocess_guard_recognizes_asyncio_and_sync_launches():
    tree = ast.parse("asyncio.create_subprocess_exec('worker')\nsubprocess.Popen(['worker'])")
    calls = list(_subprocess_calls(tree))
    assert len(calls) == 2
    assert all(not call.keywords for call in calls)


def test_every_dashboard_subprocess_launch_has_an_explicit_env_keyword():
    calls = list(_subprocess_calls(ast.parse(_DASHBOARD_SOURCE)))

    assert calls, "expected dashboard subprocess launch sites"
    missing_env = [call.lineno for call in calls if not any(keyword.arg == "env" for keyword in call.keywords)]
    assert not missing_env, f"subprocess launch(es) missing explicit env=: lines {missing_env}"


class _TogetherEnvCallVisitor(ast.NodeVisitor):
    def __init__(self):
        self.function_stack: list[str] = []
        self.calls: list[tuple[str, ast.Call]] = []

    def visit_FunctionDef(self, node: ast.FunctionDef):
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef):
        self.function_stack.append(node.name)
        self.generic_visit(node)
        self.function_stack.pop()

    def visit_Call(self, node: ast.Call):
        if (
            isinstance(node.func, ast.Name)
            and node.func.id == "_together_child_env"
        ):
            function_name = self.function_stack[-1] if self.function_stack else "<module>"
            self.calls.append((function_name, node))
        self.generic_visit(node)


def test_only_approved_dashboard_workers_opt_into_together_credentials():
    visitor = _TogetherEnvCallVisitor()
    visitor.visit(ast.parse(_DASHBOARD_SOURCE))

    assert visitor.calls, "expected dashboard _together_child_env call sites"
    opted_in_functions = set()
    for function_name, call in visitor.calls:
        include_keyword = next(
            (keyword for keyword in call.keywords if keyword.arg == "include_together"),
            None,
        )
        is_literal_true = (
            include_keyword is not None
            and isinstance(include_keyword.value, ast.Constant)
            and include_keyword.value.value is True
        )
        if is_literal_true:
            opted_in_functions.add(function_name)
            assert function_name in _OPT_IN_FUNCTIONS, (
                f"{function_name} unexpectedly passes Together credentials "
                f"at line {call.lineno}"
            )
        else:
            assert function_name not in _OPT_IN_FUNCTIONS, (
                f"{function_name} must explicitly opt into Together credentials "
                f"at line {call.lineno}"
            )

    assert opted_in_functions == _OPT_IN_FUNCTIONS
