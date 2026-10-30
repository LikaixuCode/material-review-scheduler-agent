import json
import sqlite3
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from .models import ExistingBooking, ScheduleItem, ScheduleRequest


class RequestConflict(ValueError):
    """同一请求 ID 对应了不同的输入。"""


class DraftStore:
    def __init__(self, path: Path, bookings: list[ExistingBooking] | None = None):
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path, timeout=5)
        self.connection.row_factory = sqlite3.Row
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS requests (request_id TEXT PRIMARY KEY, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS bookings (
                channel TEXT NOT NULL, start_epoch INTEGER NOT NULL, end_epoch INTEGER NOT NULL,
                label TEXT NOT NULL, UNIQUE(channel, start_epoch, end_epoch, label)
            );
            CREATE TABLE IF NOT EXISTS drafts (
                draft_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, material_id TEXT NOT NULL,
                channel TEXT NOT NULL, requested_at TEXT NOT NULL, proposed_at TEXT NOT NULL,
                start_epoch INTEGER NOT NULL, end_epoch INTEGER NOT NULL,
                confirmation_status TEXT NOT NULL, reserves_slot INTEGER NOT NULL,
                UNIQUE(request_id, material_id), FOREIGN KEY(request_id) REFERENCES requests(request_id)
            );
            """
        )
        with self.connection:
            self.connection.execute("INSERT OR IGNORE INTO metadata VALUES ('initialized', '1')")
        if bookings is not None:
            self.replace_bookings(bookings)

    def close(self):
        self.connection.close()

    def replace_bookings(self, bookings: list[ExistingBooking]):
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            self.connection.execute("DELETE FROM bookings")
            for booking in bookings:
                start = booking.start_at.timestamp()
                self.connection.execute(
                    "INSERT INTO bookings VALUES (?, ?, ?, ?)",
                    (booking.channel, start, start + booking.duration_minutes * 60, booking.label),
                )

    def register_request(self, request: ScheduleRequest):
        payload = json.dumps(request.model_dump(mode="json"), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            existing = self.connection.execute(
                "SELECT payload FROM requests WHERE request_id = ?", (request.request_id,)
            ).fetchone()
            if existing and existing["payload"] != payload:
                raise RequestConflict("相同 request_id 对应不同请求内容")
            if not existing:
                self.connection.execute("INSERT INTO requests VALUES (?, ?)", (request.request_id, payload))

    def is_available(self, channel: str, start_at: datetime, duration_minutes: int, exclude_draft: str = "", pending_request_id: str | None = None) -> bool:
        start = start_at.timestamp()
        end = (start_at + timedelta(minutes=duration_minutes)).timestamp()
        booking = self.connection.execute(
            "SELECT 1 FROM bookings WHERE channel = ? AND start_epoch < ? AND end_epoch > ? LIMIT 1",
            (channel, end, start),
        ).fetchone()
        draft = self.connection.execute(
            "SELECT 1 FROM drafts WHERE channel = ? AND draft_id != ? "
            "AND (reserves_slot = 1 OR (confirmation_status = 'pending' AND request_id = ?)) "
            "AND start_epoch < ? AND end_epoch > ? LIMIT 1",
            (channel, exclude_draft, pending_request_id, end, start),
        ).fetchone()
        return not booking and not draft

    def get_draft(self, request_id: str, material_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT * FROM drafts WHERE request_id = ? AND material_id = ?", (request_id, material_id)
        ).fetchone()
        return dict(row) if row else None

    def existing_result(self, request_id: str, material_id: str) -> dict | None:
        draft = self.get_draft(request_id, material_id)
        if not draft:
            return None
        duration_minutes = round((draft["end_epoch"] - draft["start_epoch"]) / 60)
        available = self.is_available(
            draft["channel"], datetime.fromisoformat(draft["proposed_at"]), duration_minutes,
            draft["draft_id"], request_id,
        )
        if not available:
            return {"ok": False, "duplicate": True, "code": "draft_conflict", "message": "已有草稿时段与当前排期冲突", "draft": draft}
        return {"ok": True, "duplicate": True, "draft": draft}

    def save_draft(self, request_id: str, item: ScheduleItem, proposed_at: datetime) -> dict:
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            existing = self.existing_result(request_id, item.material_id)
            if existing:
                return existing
            if not self.is_available(item.channel, proposed_at, item.duration_minutes, pending_request_id=request_id):
                return {"ok": False, "code": "slot_conflict", "message": "保存前检查发现时段已被占用"}
            changed = proposed_at != item.requested_at
            draft_id = str(uuid.uuid4())
            start = proposed_at.timestamp()
            self.connection.execute(
                "INSERT INTO drafts VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    draft_id, request_id, item.material_id, item.channel,
                    item.requested_at.isoformat(), proposed_at.isoformat(), start, start + item.duration_minutes * 60,
                    "pending" if changed else "not_required", 0 if changed else 1,
                ),
            )
            return {"ok": True, "duplicate": False, "draft": self.get_draft(request_id, item.material_id)}

    def confirm(self, request_id: str, material_id: str, approved_at: datetime) -> dict:
        with self.connection:
            self.connection.execute("BEGIN IMMEDIATE")
            draft = self.get_draft(request_id, material_id)
            if not draft:
                return {"ok": False, "code": "draft_not_found", "message": "没有找到对应草稿"}
            if datetime.fromisoformat(draft["proposed_at"]) != approved_at:
                return {"ok": False, "code": "confirmation_mismatch", "message": "确认时段与草稿建议不一致"}
            if draft["confirmation_status"] not in {"pending", "confirmed"}:
                return {"ok": False, "code": "confirmation_not_required", "message": "该草稿不需要确认"}
            duration_minutes = round((draft["end_epoch"] - draft["start_epoch"]) / 60)
            if not self.is_available(draft["channel"], approved_at, duration_minutes, draft["draft_id"], request_id):
                return {"ok": False, "code": "slot_conflict", "message": "确认失败：建议时段已被占用", "draft": draft}
            if draft["confirmation_status"] == "confirmed":
                return {"ok": True, "duplicate": True, "draft": draft}
            self.connection.execute(
                "UPDATE drafts SET confirmation_status = 'confirmed', reserves_slot = 1 WHERE draft_id = ?",
                (draft["draft_id"],),
            )
            return {"ok": True, "duplicate": False, "draft": self.get_draft(request_id, material_id)}
