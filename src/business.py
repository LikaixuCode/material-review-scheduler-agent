import sqlite3
from datetime import datetime, timedelta

from .models import Material, RuleSet, ScheduleItem, ScheduleRequest
from .storage import DraftStore


def public_draft(draft: dict | None) -> dict | None:
    if not draft:
        return None
    result = {key: draft[key] for key in (
        "draft_id", "request_id", "material_id", "channel", "requested_at", "proposed_at", "confirmation_status"
    )}
    result["duration_minutes"] = round((draft["end_epoch"] - draft["start_epoch"]) / 60)
    return result


def evaluate_material(material: Material | None, rules: RuleSet) -> dict:
    if material is None:
        return {"status": "needs_info", "reasons": ["找不到素材"], "missing_information": ["找不到素材"]}
    missing = [name for name in rules.required_fields if not (getattr(material, name) or "").strip()]
    missing_information = [f"缺少字段: {', '.join(missing)}"] if missing else []
    risks = []
    if material.status != "pending":
        risks.append("素材不是待处理状态")
    if material.copyright == "unauthorized":
        risks.append("素材未获得版权授权")
    text = f"{material.title or ''} {material.description or ''}"
    risks.extend(f"命中审核规则: {term}" for term in rules.prohibited_terms if term in text)
    return {
        "status": "blocked" if risks else "needs_info" if missing else "passed",
        "reasons": missing_information + risks,
        "missing_information": missing_information,
    }


class BusinessTools:
    def __init__(self, request: ScheduleRequest, materials: list[Material], rules: RuleSet, store: DraftStore, fail_save: bool = False):
        self.request = request
        self.items = {item.material_id: item for item in request.items}
        self.materials = {material.id: material for material in materials}
        self.rules = rules
        self.store = store
        self.fail_save = fail_save
        self.trace: list[dict] = []
        self.read_ids: set[str] = set()
        self.reviews: dict[str, dict] = {}
        self.queried_ids: set[str] = set()
        self.save_results: dict[str, dict] = {}
        self.proposals: dict[str, str] = {}

    def call(self, name: str, args: dict) -> dict:
        material_id = args["material_id"]
        item = self.items.get(material_id)
        if item is None:
            result = {"ok": False, "code": "unknown_material", "message": "素材不属于本次请求"}
        elif name == "read_material":
            self.read_ids.add(material_id)
            material = self.materials.get(material_id)
            result = {"ok": bool(material), "material": material.model_dump(mode="json") if material else None}
            review = evaluate_material(material, self.rules)
            if review["status"] != "passed":
                self.reviews[material_id] = review
                if review["missing_information"]:
                    result["missing_information"] = review["missing_information"]
        elif name == "check_rules":
            review = evaluate_material(self.materials.get(material_id), self.rules)
            self.reviews[material_id] = review
            result = {"ok": True, **review}
        elif name == "query_slots":
            result = self.query_slots(item, args["mode"])
        elif name == "save_draft":
            result = self.save_draft(item, args["proposed_at"])
        else:
            raise ValueError(f"未知业务工具: {name}")
        if self.trace:
            self.trace[-1]["next_action"] = name
        self.trace.append({
            "step": len(self.trace) + 1,
            "tool": name,
            "arguments": {key: value for key, value in args.items() if key != "reason"},
            "reason": args.get("reason", ""),
            "result": result,
            "next_action": "finish",
        })
        return result

    def query_slots(self, item: ScheduleItem, mode: str) -> dict:
        current_draft = self.store.get_draft(self.request.request_id, item.material_id)
        exclude_draft = current_draft["draft_id"] if current_draft else ""
        if mode == "requested":
            self.queried_ids.add(item.material_id)
            return {"ok": True, "requested_at": item.requested_at.isoformat(), "duration_minutes": item.duration_minutes, "available": self.store.is_available(item.channel, item.requested_at, item.duration_minutes, exclude_draft, self.request.request_id)}
        if mode != "alternatives":
            return {"ok": False, "code": "invalid_mode", "message": "查询模式无效"}
        candidates = []
        for offset in range(1, 9):
            candidate = item.requested_at + timedelta(minutes=item.duration_minutes * offset)
            if self.store.is_available(item.channel, candidate, item.duration_minutes, exclude_draft, self.request.request_id):
                candidates.append(candidate.isoformat())
            if len(candidates) == 3:
                break
        return {"ok": True, "candidates": candidates}

    def save_draft(self, item: ScheduleItem, proposed_at: str) -> dict:
        self.proposals[item.material_id] = proposed_at
        existing = self.store.existing_result(self.request.request_id, item.material_id)
        if existing:
            existing["draft"] = public_draft(existing["draft"])
            self.save_results[item.material_id] = existing
            return existing
        try:
            time = datetime.fromisoformat(proposed_at)
        except ValueError:
            result = {"ok": False, "code": "invalid_time", "message": "建议时段必须是带时区的 ISO 时间"}
        else:
            if time.tzinfo is None or time.utcoffset() is None:
                result = {"ok": False, "code": "invalid_time", "message": "建议时段必须带时区"}
            elif item.material_id not in self.read_ids or item.material_id not in self.reviews or item.material_id not in self.queried_ids:
                result = {"ok": False, "code": "prerequisite_missing", "message": "保存前必须读取素材、检查规则并查询原定时段"}
            elif evaluate_material(self.materials.get(item.material_id), self.rules)["status"] != "passed":
                result = {"ok": False, "code": "review_blocked", "message": "素材资料不足或审核未通过"}
            elif self.fail_save:
                result = {"ok": False, "code": "save_failed", "message": "模拟草稿存储失败"}
            else:
                try:
                    result = self.store.save_draft(self.request.request_id, item, time)
                except sqlite3.Error:
                    result = {"ok": False, "code": "save_failed", "message": "草稿存储失败；请先查询现有草稿再重试"}
                if result.get("draft"):
                    result["draft"] = public_draft(result["draft"])
        self.save_results[item.material_id] = result
        return result

    def output(self, mode: str, terminal: dict) -> dict:
        items = []
        for item in self.request.items:
            review = self.reviews.get(item.material_id, {"status": "not_checked", "reasons": []})
            stored = self.store.get_draft(self.request.request_id, item.material_id)
            saved = public_draft(stored)
            attempt = self.save_results.get(item.material_id)
            conflict = bool(stored and not self.store.is_available(
                stored["channel"], datetime.fromisoformat(stored["proposed_at"]), item.duration_minutes,
                stored["draft_id"], self.request.request_id,
            ))
            items.append({
                "material_id": item.material_id,
                "review_status": review["status"],
                "risk_reasons": review["reasons"],
                "missing_information": review.get("missing_information", []),
                "requested_at": item.requested_at.isoformat(),
                "duration_minutes": item.duration_minutes,
                "suggested_at": saved["proposed_at"] if saved else self.proposals.get(item.material_id),
                "confirmation_status": saved["confirmation_status"] if saved else "not_applicable",
                "draft_save_status": "failed" if attempt and not attempt["ok"] and attempt.get("code") != "draft_conflict" else "saved" if saved else "not_attempted",
                "draft_id": saved["draft_id"] if saved else None,
                "schedule_status": "conflict" if conflict else "pending_confirmation" if saved and saved["confirmation_status"] == "pending" else "available" if saved else "not_applicable",
                "error_code": attempt.get("code") if attempt and not attempt["ok"] else None,
            })
        if terminal.get("status") != "completed" or any(
            row["draft_save_status"] == "failed" or row["schedule_status"] == "conflict"
            or row["review_status"] == "not_checked"
            or (row["review_status"] == "passed" and row["draft_save_status"] != "saved")
            for row in items
        ):
            status = "incomplete"
        elif any(row["review_status"] in {"needs_info", "blocked"} for row in items):
            status = "needs_attention"
        elif any(row["draft_save_status"] != "saved" for row in items):
            status = "incomplete"
        elif any(row["confirmation_status"] == "pending" for row in items):
            status = "needs_confirmation"
        else:
            status = "completed"
        trace = [dict(step) for step in self.trace]
        if trace and terminal.get("status") != "completed":
            trace[-1]["next_action"] = "interrupted"
            trace[-1]["termination_reason"] = terminal.get("reason", "unknown")
        return {"request_id": self.request.request_id, "mode": mode, "status": status, "items": items, "terminal": terminal, "trace": trace}
