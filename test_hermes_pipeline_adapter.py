from __future__ import annotations

import json

import pytest

from hermes_pipeline_adapter import (
    HermesAdapterError,
    build_hermes_task,
    build_kanban_create_argv,
    dispatch_kanban_task,
    normalize_profile,
)


def _request(**overrides):
    values = {
        "campaign_id": "campaign-1",
        "shop_id": "templystudios",
        "candidate_id": "candidate-1",
        "product_id": "product-1",
        "stage": "research",
        "title": "Research digital planner",
        "brief": "Need evidence-backed competitor observations.",
    }
    values.update(overrides)
    return build_hermes_task(**values)


def test_profile_typo_is_compatible_alias_and_stage_defaults_are_specialized():
    assert normalize_profile("reserach") == "research"
    assert _request(stage="generate-images").profile == "make-image"
    assert _request(stage="create-product").profile == "organizer"


def test_argv_is_json_contract_and_has_no_shell_command():
    request = _request(profile="reserach", stage="research")
    argv = build_kanban_create_argv(request, hermes_bin="/usr/local/bin/hermes")
    assert argv[0] == "/usr/local/bin/hermes"
    assert argv[-1] == "--json"
    assert "--project" in argv and argv[argv.index("--project") + 1] == "etsy"
    assert request.idempotency_key in argv
    body = json.loads(argv[argv.index("--body") + 1])
    assert body["schema"] == "etsy.hermes-kanban-task.v1"
    assert body["publish_allowed"] is False
    assert body["source_policy"]["source_assets_separate"] is True
    assert argv[argv.index("--assignee") + 1] == "research"
    assert all(";" not in item and "&&" not in item for item in argv)


def test_dispatch_accepts_injected_runner_and_parses_task_identity():
    seen = {}

    def runner(argv):
        seen["argv"] = argv
        return '{"id":"task-123","status":"running"}'

    result = dispatch_kanban_task(_request(), runner=runner, hermes_bin="hermes")
    assert result["task_id"] == "task-123"
    assert seen["argv"][1:3] == ["kanban", "create"]


def test_unknown_profile_or_stage_is_rejected():
    with pytest.raises(HermesAdapterError):
        normalize_profile("not-a-profile")
    with pytest.raises(HermesAdapterError):
        _request(stage="not-a-stage")
    with pytest.raises(HermesAdapterError, match="canonical Etsy checkout"):
        _request(workspace="/tmp/unrelated-etsy-copy")
    with pytest.raises(HermesAdapterError, match="Unsupported Etsy pipeline stage"):
        _request(stage="create-verified-draft")
    with pytest.raises(HermesAdapterError, match="read/asset/text-only"):
        _request(side_effect_boundary="etsy_write")
