import json
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from src.agent import run_mock
from src.business import BusinessTools, evaluate_material
from src.models import ExistingBooking, Material, RuleSet, ScheduleRequest
from src.storage import DraftStore


ROOT = Path(__file__).resolve().parent.parent


def cli(*arguments):
    process = subprocess.run(
        [sys.executable, "-m", "src.main", *map(str, arguments)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    return process.returncode, json.loads(process.stdout)


def test_confirm_uses_updated_schedule_file(tmp_path):
    database = tmp_path / "state.sqlite3"
    schedule = tmp_path / "schedule.json"
    bookings = json.loads((ROOT / "data/schedules/busy.json").read_text())
    schedule.write_text(json.dumps(bookings))
    code, result = cli("run", "--mode", "mock", "--input", ROOT / "examples/conflict.input.json", "--schedule", schedule, "--db", database)
    assert code == 0 and result["status"] == "needs_confirmation"
    item = result["items"][0]
    bookings.append({"channel": "short_video", "start_at": item["suggested_at"], "duration_minutes": item["duration_minutes"], "label": "新增占用"})
    schedule.write_text(json.dumps(bookings))

    code, confirmed = cli("confirm", "--request-id", result["request_id"], "--material-id", item["material_id"], "--time", item["suggested_at"], "--schedule", schedule, "--db", database)
    assert code == 1 and confirmed["code"] == "slot_conflict"
    assert confirmed["draft"]["confirmation_status"] == "pending"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 2


def test_repeated_confirmation_rechecks_new_schedule(tmp_path):
    database = tmp_path / "state.sqlite3"
    schedule = tmp_path / "schedule.json"
    bookings = json.loads((ROOT / "data/schedules/busy.json").read_text())
    schedule.write_text(json.dumps(bookings))
    _, result = cli("run", "--mode", "mock", "--input", ROOT / "examples/conflict.input.json", "--schedule", schedule, "--db", database)
    item = result["items"][0]
    confirm = ("confirm", "--request-id", result["request_id"], "--material-id", item["material_id"], "--time", item["suggested_at"], "--schedule", schedule, "--db", database)
    code, first = cli(*confirm)
    assert code == 0 and first["draft"]["confirmation_status"] == "confirmed"
    bookings.append({"channel": "short_video", "start_at": item["suggested_at"], "duration_minutes": item["duration_minutes"], "label": "新增占用"})
    schedule.write_text(json.dumps(bookings))
    code, repeated = cli(*confirm)
    assert code == 1 and repeated["code"] == "slot_conflict"
    assert repeated["draft"]["confirmation_status"] == "confirmed"


def test_repeat_reports_conflict_without_creating_another_draft(tmp_path):
    database = tmp_path / "state.sqlite3"
    args = ("run", "--mode", "mock", "--input", ROOT / "examples/normal.input.json", "--db", database)
    _, original = cli(*args)
    with sqlite3.connect(database) as connection:
        channel, start, end = connection.execute("SELECT channel, start_epoch, end_epoch FROM drafts").fetchone()
        connection.execute("INSERT INTO bookings VALUES (?, ?, ?, ?)", (channel, start, end, "新增占用"))
    code, repeated = cli(*args)
    item = repeated["items"][0]
    assert code == 1 and repeated["status"] == "incomplete"
    assert item["draft_id"] == original["items"][0]["draft_id"]
    assert item["draft_save_status"] == "saved" and item["schedule_status"] == "conflict"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1


def test_repeated_request_uses_replaced_schedule_snapshot(tmp_path):
    database = tmp_path / "state.sqlite3"
    schedule = tmp_path / "schedule.json"
    schedule.write_text("[]")
    args = ("run", "--mode", "mock", "--input", ROOT / "examples/normal.input.json", "--schedule", schedule, "--db", database)
    _, original = cli(*args)
    item = original["items"][0]
    schedule.write_text(json.dumps([{"channel": "short_video", "start_at": item["requested_at"], "duration_minutes": item["duration_minutes"], "label": "新增占用"}]))
    code, repeated = cli(*args)
    assert code == 1 and repeated["items"][0]["schedule_status"] == "conflict"
    assert repeated["items"][0]["draft_id"] == item["draft_id"]


def test_invalid_new_schedule_does_not_erase_previous_bookings(tmp_path):
    database = tmp_path / "state.sqlite3"
    schedule = tmp_path / "schedule.json"
    bookings = json.loads((ROOT / "data/schedules/busy.json").read_text())
    schedule.write_text(json.dumps(bookings))
    args = ("run", "--mode", "mock", "--input", ROOT / "examples/conflict.input.json", "--schedule", schedule, "--db", database)
    code, _ = cli(*args)
    assert code == 0
    schedule.write_text(json.dumps(bookings + bookings))
    code, result = cli(*args)
    assert code == 2 and result["error_type"] == "IntegrityError"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 1


def test_conflicting_request_id_does_not_replace_existing_schedule(tmp_path):
    database = tmp_path / "state.sqlite3"
    args = ("run", "--mode", "mock", "--input", ROOT / "examples/normal.input.json", "--db", database)
    code, _ = cli(*args, "--schedule", ROOT / "data/schedules/free.json")
    assert code == 0
    request = json.loads((ROOT / "examples/normal.input.json").read_text())
    request["items"][0]["channel"] = "different_channel"
    modified = tmp_path / "modified.json"
    modified.write_text(json.dumps(request))
    code, result = cli("run", "--mode", "mock", "--input", modified, "--db", database, "--schedule", ROOT / "data/schedules/busy.json")
    assert code == 2 and result["error_type"] == "RequestConflict"
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 0


def test_invalid_confirmation_time_does_not_replace_existing_schedule(tmp_path):
    database = tmp_path / "state.sqlite3"
    code, _ = cli("run", "--mode", "mock", "--input", ROOT / "examples/normal.input.json", "--db", database)
    assert code == 0
    code, _ = cli("confirm", "--request-id", "case-m01", "--material-id", "M01", "--time", "invalid", "--schedule", ROOT / "data/schedules/busy.json", "--db", database)
    assert code == 2
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT COUNT(*) FROM bookings").fetchone()[0] == 0


def test_batch_proposals_do_not_overlap_each_other(tmp_path):
    start = "2027-04-15T09:20:00+08:00"
    request = ScheduleRequest.model_validate({"request_id": "batch", "items": [
        {"material_id": material_id, "channel": "channel", "requested_at": start, "duration_minutes": 40}
        for material_id in ("A", "B")
    ]})
    materials = [Material(id=material_id, title="标题", description="说明", copyright="owned") for material_id in ("A", "B")]
    rules = RuleSet(required_fields=["title", "description", "copyright"], prohibited_terms=[])
    store = DraftStore(tmp_path / "batch.sqlite3", [ExistingBooking(channel="channel", start_at=start, duration_minutes=40, label="occupied")])
    store.register_request(request)
    result = run_mock(BusinessTools(request, materials, rules, store))
    first, second = result["items"]
    assert result["status"] == "needs_confirmation"
    assert first["suggested_at"] != second["suggested_at"]
    assert store.is_available("channel", datetime.fromisoformat(first["suggested_at"]), 40)
    assert store.confirm("batch", "A", datetime.fromisoformat(first["suggested_at"]))["ok"]
    assert store.confirm("batch", "B", datetime.fromisoformat(second["suggested_at"]))["ok"]
    store.close()


def test_missing_information_does_not_hide_known_risk():
    material = Material(id="risk-and-missing", title="夸大宣传", description=None, copyright="unauthorized")
    rules = RuleSet(required_fields=["title", "description", "copyright"], prohibited_terms=["夸大宣传"])
    result = evaluate_material(material, rules)
    assert result["status"] == "blocked"
    assert any("缺少" in reason for reason in result["reasons"])
    assert any("版权" in reason for reason in result["reasons"])
    assert any("夸大宣传" in reason for reason in result["reasons"])


def test_mock_limit_returns_incomplete_and_marks_interrupted_trace(tmp_path):
    code, result = cli("run", "--mode", "mock", "--max-turns", "1", "--input", ROOT / "examples/normal.input.json", "--db", tmp_path / "state.sqlite3")
    assert code == 1 and result["status"] == "incomplete"
    assert result["terminal"]["reason"] == "max_steps"
    assert len(result["trace"]) == 1 and result["trace"][-1]["next_action"] == "interrupted"


def test_mock_limit_allows_completion_at_the_exact_boundary(tmp_path):
    args = ("run", "--mode", "mock", "--input", ROOT / "examples/normal.input.json")
    code, completed = cli(*args, "--max-turns", "4", "--db", tmp_path / "complete.sqlite3")
    assert code == 0 and completed["status"] == "completed" and len(completed["trace"]) == 4
    code, stopped = cli(*args, "--max-turns", "3", "--db", tmp_path / "stopped.sqlite3")
    assert code == 1 and stopped["status"] == "incomplete" and len(stopped["trace"]) == 3


def test_interrupted_trace_does_not_claim_model_finished(tmp_path):
    request = ScheduleRequest.model_validate_json((ROOT / "examples/normal.input.json").read_text())
    material = Material(id="M01", title="标题", description="说明", copyright="owned")
    rules = RuleSet(required_fields=["title", "description", "copyright"], prohibited_terms=[])
    store = DraftStore(tmp_path / "state.sqlite3", [])
    store.register_request(request)
    service = BusinessTools(request, [material], rules, store)
    service.call("read_material", {"material_id": "M01", "reason": "读取素材"})
    result = service.output("glm", {"status": "incomplete", "reason": "max_turns"})
    assert result["trace"][-1]["next_action"] == "interrupted"
    assert result["trace"][-1]["termination_reason"] == "max_turns"
    store.close()


def test_mixed_batch_with_unfinished_valid_item_is_incomplete(tmp_path):
    request = ScheduleRequest.model_validate({"request_id": "partial", "items": [
        {"material_id": "A", "channel": "channel", "requested_at": "2027-04-15T09:20:00+08:00"},
        {"material_id": "B", "channel": "channel", "requested_at": "2027-04-15T11:20:00+08:00"},
    ]})
    materials = [Material(id="A", title="标题", description="说明", copyright="owned"), Material(id="B", title="标题", description=None, copyright="owned")]
    rules = RuleSet(required_fields=["title", "description", "copyright"], prohibited_terms=[])
    store = DraftStore(tmp_path / "state.sqlite3", [])
    store.register_request(request)
    service = BusinessTools(request, materials, rules, store)
    for material_id in ("A", "B"):
        service.call("read_material", {"material_id": material_id, "reason": "读取"})
        service.call("check_rules", {"material_id": material_id, "reason": "检查"})
    result = service.output("glm", {"status": "completed"})
    assert result["status"] == "incomplete"
    assert result["items"][0]["review_status"] == "passed"
    assert result["items"][0]["draft_save_status"] == "not_attempted"
    store.close()
