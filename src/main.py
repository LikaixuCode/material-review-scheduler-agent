import argparse
import asyncio
import json
import sqlite3
import sys
from datetime import datetime
from pathlib import Path

from pydantic import ValidationError

from .agent import run_glm, run_mock
from .business import BusinessTools, evaluate_material, public_draft
from .models import ExistingBooking, Material, RuleSet, ScheduleRequest
from .storage import DraftStore, RequestConflict


ROOT = Path(__file__).resolve().parent.parent


def parser() -> argparse.ArgumentParser:
    command = argparse.ArgumentParser(description="素材审核与发布排期 Agent")
    subcommands = command.add_subparsers(dest="command", required=True)
    run = subcommands.add_parser("run", help="审核素材并生成排期草稿")
    run.add_argument("--input", type=Path, required=True)
    run.add_argument("--mode", choices=["glm", "mock"], default="glm")
    run.add_argument("--max-turns", type=int, default=12)
    run.add_argument("--simulate-save-failure", action="store_true", help="演示存储失败")
    confirm = subcommands.add_parser("confirm", help="明确确认草稿建议时间")
    confirm.add_argument("--request-id", required=True)
    confirm.add_argument("--material-id", required=True)
    confirm.add_argument("--time", required=True, help="要确认的带时区 ISO 时间")
    for subcommand in (run, confirm):
        subcommand.add_argument("--db", type=Path, default=ROOT / ".runtime/drafts.sqlite3")
        subcommand.add_argument("--schedule", type=Path, help="本次已有排期快照；未提供时沿用数据库中的已有排期")
    return command


def main() -> int:
    args = parser().parse_args()
    try:
        materials = [Material.model_validate(value) for value in json.loads((ROOT / "data/materials.json").read_text())]
        rules = RuleSet.model_validate_json((ROOT / "data/rules.json").read_text())
        bookings = [ExistingBooking.model_validate(value) for value in json.loads(args.schedule.read_text())] if args.schedule else None
        if args.command == "run":
            if args.max_turns < 1:
                raise ValueError("max-turns 必须大于零")
            request = ScheduleRequest.model_validate_json(args.input.read_text())
        else:
            if not args.db.exists():
                raise ValueError("数据库不存在，请先运行排期请求")
            time = datetime.fromisoformat(args.time)
            if time.tzinfo is None or time.utcoffset() is None:
                raise ValueError("确认时间必须包含时区")
        store = DraftStore(args.db)
        try:
            if args.command == "run":
                store.register_request(request)
            if bookings is not None:
                store.replace_bookings(bookings)
            if args.command == "run":
                service = BusinessTools(request, materials, rules, store, args.simulate_save_failure)
                result = run_mock(service, args.max_turns) if args.mode == "mock" else asyncio.run(run_glm(service, args.max_turns))
                exit_code = 1 if result["status"] == "incomplete" else 0
            else:
                review = evaluate_material(next((material for material in materials if material.id == args.material_id), None), rules)
                if review["status"] != "passed":
                    result = {"ok": False, "code": "review_blocked", "review": review}
                else:
                    result = store.confirm(args.request_id, args.material_id, time)
                    if result.get("draft"):
                        result["draft"] = public_draft(result["draft"])
                exit_code = 0 if result["ok"] else 1
        finally:
            store.close()
    except (OSError, ValueError, ValidationError, sqlite3.Error, RequestConflict) as exc:
        result = {"status": "error", "error_type": type(exc).__name__, "message": str(exc)}
        exit_code = 2
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    sys.exit(main())
