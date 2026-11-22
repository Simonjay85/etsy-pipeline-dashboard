from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

from starlette.testclient import TestClient

import dashboard_app


class FakeStdout:
    def __init__(self, lines: list[bytes]):
        self.lines = list(lines)

    async def readline(self) -> bytes:
        return self.lines.pop(0) if self.lines else b""


class FakeProcess:
    def __init__(self, lines: list[bytes]):
        self.stdout = FakeStdout(lines)
        self.returncode = 0

    async def wait(self) -> int:
        return self.returncode


class TerminableProcess:
    def __init__(self):
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True
        self.returncode = -9

    async def wait(self) -> int:
        self.wait_calls += 1
        if self.returncode is None:
            self.returncode = -15
        return self.returncode


def test_action_stream_routes_require_post_and_mutation_token():
    client = TestClient(dashboard_app.app)
    routes = (
        ("/api/scan-listings", "etsy_scan_errors.py"),
        ("/api/clean-duplicates", "etsy_clean_duplicates.py"),
    )

    for route, script_name in routes:
        with patch.object(dashboard_app.asyncio, "create_subprocess_exec", new_callable=AsyncMock) as spawn:
            get_response = client.get(route)
        assert get_response.status_code == 405
        spawn.assert_not_awaited()

        with patch.object(dashboard_app.asyncio, "create_subprocess_exec", new_callable=AsyncMock) as spawn:
            rejected = client.post(route)

        assert rejected.status_code == 403
        assert rejected.json() == {"detail": "missing or invalid mutation token"}
        spawn.assert_not_awaited()

        process = FakeProcess([b'{"type":"info","msg":"mocked"}\n'])
        with patch.object(
            dashboard_app.asyncio,
            "create_subprocess_exec",
            new_callable=AsyncMock,
            return_value=process,
        ) as spawn:
            accepted = client.post(
                route,
                headers={dashboard_app._DASHBOARD_MUTATION_TOKEN_HEADER: dashboard_app._DASHBOARD_MUTATION_TOKEN},
            )

        assert accepted.status_code == 200
        assert "data: {\"type\": \"finished\"}" in accepted.text
        assert spawn.await_count == 1
        assert spawn.await_args.args[1].endswith(script_name)


def test_sse_process_cleanup_terminates_and_waits_for_child():
    process = TerminableProcess()

    asyncio.run(dashboard_app._terminate_sse_process(process))

    assert process.terminated is True
    assert process.killed is False
    assert process.wait_calls == 1
    assert process.returncode == -15


def test_sse_process_cleanup_kills_child_after_termination_timeout():
    process = TerminableProcess()

    async def timeout_without_waiting(awaitable, timeout):
        assert timeout == 2
        awaitable.close()
        raise asyncio.TimeoutError

    with patch.object(dashboard_app.asyncio, "wait_for", new=timeout_without_waiting):
        asyncio.run(dashboard_app._terminate_sse_process(process))

    assert process.terminated is True
    assert process.killed is True
    assert process.wait_calls == 1
    assert process.returncode == -9

