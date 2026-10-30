import asyncio
import json
from datetime import datetime
from pathlib import Path

from claude_agent_sdk import ResultMessage

from src.agent import run_glm, run_mock
from src.business import BusinessTools
from src.models import ExistingBooking, Material, RuleSet, ScheduleRequest
from src.storage import DraftStore


ROOT = Path(__file__).resolve().parent.parent


def make_service(tmp_path, example="normal"):
    request = ScheduleRequest.model_validate_json((ROOT / "examples" / f"{example}.input.json").read_text())
    materials = [Material.model_validate(value) for value in json.loads((ROOT / "data/materials.json").read_text())]
    rules = RuleSet.model_validate_json((ROOT / "data/rules.json").read_text())
    store = DraftStore(tmp_path / "state.sqlite3", [])
    store.register_request(request)
    return BusinessTools(request, materials, rules, store)


def test_save_tool_enforces_prerequisites_and_review(tmp_path):
    service = make_service(tmp_path, "risk")
    args = {"material_id": "M03", "proposed_at": "2026-11-05T10:30:00+08:00", "reason": "测试"}
    assert service.call("save_draft", args)["code"] == "prerequisite_missing"
    service.call("read_material", {"material_id": "M03", "reason": "测试"})
    service.call("check_rules", {"material_id": "M03", "reason": "测试"})
    service.call("query_slots", {"material_id": "M03", "mode": "requested", "reason": "测试"})
    assert service.call("save_draft", args)["code"] == "review_blocked"
    assert service.store.get_draft("case-m03", "M03") is None
    service.store.close()


def test_save_rechecks_slot_after_query(tmp_path):
    service = make_service(tmp_path)
    material_id = "M01"
    service.call("read_material", {"material_id": material_id, "reason": "测试"})
    service.call("check_rules", {"material_id": material_id, "reason": "测试"})
    assert service.call("query_slots", {"material_id": material_id, "mode": "requested", "reason": "测试"})["available"]
    start = int(service.items[material_id].requested_at.timestamp())
    with service.store.connection:
        service.store.connection.execute("INSERT INTO bookings VALUES (?, ?, ?, ?)", ("short_video", start, start + 2700, "后来占用"))
    result = service.call("save_draft", {"material_id": material_id, "proposed_at": "2026-11-05T10:30:00+08:00", "reason": "测试"})
    assert result["code"] == "slot_conflict"
    assert service.store.get_draft("case-m01", material_id) is None
    service.store.close()


def test_overlap_uses_the_requested_duration_and_channel(tmp_path):
    booking = ExistingBooking.model_validate({"channel": "short_video", "start_at": "2026-11-05T10:30:00+08:00", "duration_minutes": 45, "label": "已有排期"})
    store = DraftStore(tmp_path / "slots.sqlite3", [booking])
    assert not store.is_available("short_video", datetime.fromisoformat("2026-11-05T11:00:00+08:00"), 20)
    assert store.is_available("short_video", datetime.fromisoformat("2026-11-05T11:15:00+08:00"), 20)
    assert store.is_available("other_channel", datetime.fromisoformat("2026-11-05T11:00:00+08:00"), 20)
    store.close()


def test_missing_material_is_reported_even_if_agent_stops_after_reading(tmp_path):
    service = make_service(tmp_path, "missing")
    result = service.call("read_material", {"material_id": "M02", "reason": "读取资料"})
    assert result["missing_information"]
    output = service.output("glm", {"status": "completed"})
    assert output["status"] == "needs_attention"
    assert output["items"][0]["review_status"] == "needs_info"
    assert output["items"][0]["draft_save_status"] == "not_attempted"
    service.store.close()


def test_sdk_max_turns_returns_incomplete_and_keeps_existing_draft(tmp_path, monkeypatch):
    service = make_service(tmp_path)
    prior = run_mock(service)
    draft_id = prior["items"][0]["draft_id"]

    class FakeClient:
        def __init__(self, options):
            assert options.max_turns == 1
            assert options.tools == [] and options.strict_mcp_config and options.setting_sources == []
            assert set(options.allowed_tools) == {
                "mcp__review__read_material", "mcp__review__check_rules",
                "mcp__review__query_slots", "mcp__review__save_draft",
            }

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def query(self, prompt):
            return None

        async def receive_response(self):
            yield ResultMessage(subtype="error_max_turns", duration_ms=1, duration_api_ms=1, is_error=True, num_turns=1, session_id="test")

    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-only-token")
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", FakeClient)
    result = asyncio.run(run_glm(service, max_turns=1))
    assert result["status"] == "incomplete"
    assert result["terminal"]["reason"] == "error_max_turns"
    assert result["items"][0]["draft_id"] == draft_id
    service.store.close()


def test_model_answer_without_tool_calls_is_incomplete(tmp_path, monkeypatch):
    service = make_service(tmp_path)

    class FakeClient:
        def __init__(self, options):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return None

        async def query(self, prompt):
            return None

        async def receive_response(self):
            yield ResultMessage(subtype="success", duration_ms=1, duration_api_ms=1, is_error=False, num_turns=1, session_id="test", result="已经保存")

    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "test-only-token")
    monkeypatch.setattr("claude_agent_sdk.ClaudeSDKClient", FakeClient)
    result = asyncio.run(run_glm(service))
    assert result["status"] == "incomplete"
    assert result["terminal"]["reason"] == "no_model_tool_calls"
    assert result["items"][0]["draft_save_status"] == "not_attempted"
    service.store.close()
