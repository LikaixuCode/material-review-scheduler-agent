import json
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parent.parent


def run_cli(db: Path, example: str, schedule: str = "free", *extra: str):
    process = subprocess.run(
        [sys.executable, "-m", "src.main", "run", "--mode", "mock", "--input", str(ROOT / "examples" / f"{example}.input.json"),
         "--schedule", str(ROOT / "data/schedules" / f"{schedule}.json"), "--db", str(db), *extra],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    return process.returncode, json.loads(process.stdout)


def confirm_cli(db: Path, time: str):
    process = subprocess.run(
        [sys.executable, "-m", "src.main", "confirm", "--request-id", "case-m01", "--material-id", "M01",
         "--time", time, "--db", str(db)], cwd=ROOT, capture_output=True, text=True, check=False,
    )
    return process.returncode, json.loads(process.stdout)


def test_normal_and_duplicate_survive_separate_processes(tmp_path):
    db = tmp_path / "state.sqlite3"
    code, first = run_cli(db, "normal")
    assert code == 0 and first["status"] == "completed"
    assert first["items"][0]["review_status"] == "passed"
    assert first["items"][0]["draft_save_status"] == "saved"
    assert first["items"][0]["confirmation_status"] == "not_required"
    code, repeated = run_cli(db, "normal")
    assert code == 0
    assert repeated["items"][0]["draft_id"] == first["items"][0]["draft_id"]
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1


@pytest.mark.parametrize(("example", "expected"), [("missing", "needs_info"), ("risk", "blocked")])
def test_missing_and_risk_do_not_save(tmp_path, example, expected):
    db = tmp_path / "state.sqlite3"
    code, result = run_cli(db, example)
    assert code == 0 and result["status"] == "needs_attention"
    assert result["items"][0]["review_status"] == expected
    assert result["items"][0]["risk_reasons"]
    assert result["items"][0]["draft_save_status"] == "not_attempted"
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0


def test_conflict_proposes_alternative_and_requires_explicit_confirmation(tmp_path):
    db = tmp_path / "state.sqlite3"
    code, result = run_cli(db, "conflict", "busy")
    item = result["items"][0]
    assert code == 0 and result["status"] == "needs_confirmation"
    assert item["requested_at"].endswith("10:30:00+08:00")
    assert item["suggested_at"].endswith("11:15:00+08:00")
    assert item["duration_minutes"] == 45
    assert item["confirmation_status"] == "pending"
    assert [event["arguments"].get("mode") for event in result["trace"] if event["tool"] == "query_slots"] == ["requested", "alternatives"]
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT reserves_slot FROM drafts").fetchone()[0] == 0
    code, result = confirm_cli(db, "2026-11-05T11:15:00+08:00")
    assert code == 0 and result["draft"]["confirmation_status"] == "confirmed"
    assert result["draft"]["requested_at"].endswith("10:30:00+08:00")
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT reserves_slot FROM drafts").fetchone()[0] == 1


def test_confirmation_rechecks_changed_occupancy(tmp_path):
    db = tmp_path / "state.sqlite3"
    run_cli(db, "conflict", "busy")
    with sqlite3.connect(db) as connection:
        start = connection.execute("SELECT start_epoch FROM drafts").fetchone()[0]
        connection.execute("INSERT INTO bookings VALUES (?, ?, ?, ?)", ("short_video", start, start + 2700, "新增占用"))
    code, result = confirm_cli(db, "2026-11-05T11:15:00+08:00")
    assert code == 1 and result["code"] == "slot_conflict"
    assert result["draft"]["confirmation_status"] == "pending"
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT reserves_slot FROM drafts").fetchone()[0] == 0


def test_wrong_confirmation_does_not_modify_draft(tmp_path):
    db = tmp_path / "state.sqlite3"
    run_cli(db, "conflict", "busy")
    code, result = confirm_cli(db, "2026-11-05T12:00:00+08:00")
    assert code == 1 and result["code"] == "confirmation_mismatch"
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT confirmation_status FROM drafts").fetchone()[0] == "pending"


def test_save_failure_is_not_reported_as_success(tmp_path):
    db = tmp_path / "state.sqlite3"
    code, result = run_cli(db, "save-failure", "free", "--simulate-save-failure")
    assert code == 1 and result["status"] == "incomplete"
    assert result["items"][0]["draft_save_status"] == "failed"
    assert result["trace"][-1]["result"]["code"] == "save_failed"
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 0
    code, retried = run_cli(db, "save-failure")
    assert code == 0 and retried["items"][0]["draft_save_status"] == "saved"


def test_duplicate_does_not_attempt_a_second_write(tmp_path):
    db = tmp_path / "state.sqlite3"
    _, first = run_cli(db, "normal")
    code, second = run_cli(db, "normal", "free", "--simulate-save-failure")
    assert code == 0 and second["status"] == "completed"
    assert second["items"][0]["draft_save_status"] == "saved"
    assert second["items"][0]["draft_id"] == first["items"][0]["draft_id"]
    assert second["trace"][-1]["result"]["duplicate"] is True


def test_reusing_request_id_with_changed_parameters_fails(tmp_path):
    db = tmp_path / "state.sqlite3"
    run_cli(db, "normal")
    changed = json.loads((ROOT / "examples/normal.input.json").read_text())
    changed["items"][0]["requested_at"] = "2026-11-05T12:00:00+08:00"
    input_path = tmp_path / "changed.json"
    input_path.write_text(json.dumps(changed))
    process = subprocess.run(
        [sys.executable, "-m", "src.main", "run", "--mode", "mock", "--input", str(input_path), "--db", str(db)],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    assert process.returncode == 2
    assert json.loads(process.stdout)["error_type"] == "RequestConflict"
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM drafts").fetchone()[0] == 1


def test_batch_reports_each_material_independently(tmp_path):
    request = json.loads((ROOT / "examples/normal.input.json").read_text())
    request["request_id"] = "batch-001"
    request["items"] += [dict(request["items"][0], material_id=value) for value in ("M02", "M03")]
    path = tmp_path / "batch.json"
    path.write_text(json.dumps(request))
    process = subprocess.run(
        [sys.executable, "-m", "src.main", "run", "--mode", "mock", "--input", str(path), "--db", str(tmp_path / "batch.sqlite3")],
        cwd=ROOT, capture_output=True, text=True, check=False,
    )
    result = json.loads(process.stdout)
    assert process.returncode == 0 and result["status"] == "needs_attention"
    assert [row["review_status"] for row in result["items"]] == ["passed", "needs_info", "blocked"]
    assert [row["draft_save_status"] for row in result["items"]] == ["saved", "not_attempted", "not_attempted"]


def test_missing_model_credential_never_falls_back_to_mock(tmp_path):
    environment = os.environ.copy()
    environment.pop("ANTHROPIC_AUTH_TOKEN", None)
    process = subprocess.run(
        [sys.executable, "-m", "src.main", "run", "--mode", "glm", "--input", str(ROOT / "examples/normal.input.json"), "--db", str(tmp_path / "state.sqlite3")],
        cwd=ROOT, env=environment, capture_output=True, text=True, check=False,
    )
    result = json.loads(process.stdout)
    assert process.returncode == 1 and result["status"] == "incomplete"
    assert result["terminal"]["reason"] == "missing_credential"
    assert result["trace"] == [] and result["items"][0]["draft_save_status"] == "not_attempted"
