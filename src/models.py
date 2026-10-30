from typing import Literal

from pydantic import AwareDatetime, BaseModel, Field, model_validator


class ScheduleItem(BaseModel):
    material_id: str = Field(min_length=1, max_length=80)
    channel: str = Field(min_length=1, max_length=80)
    requested_at: AwareDatetime
    duration_minutes: int = Field(default=60, ge=1, le=1440)
    hard_time: bool = True


class ScheduleRequest(BaseModel):
    request_id: str = Field(min_length=1, max_length=120)
    items: list[ScheduleItem] = Field(min_length=1, max_length=10)

    @model_validator(mode="after")
    def unique_materials(self):
        if len({item.material_id for item in self.items}) != len(self.items):
            raise ValueError("一次请求内的素材 ID 不得重复")
        return self


class Material(BaseModel):
    id: str
    title: str | None = None
    description: str | None = None
    copyright: Literal["owned", "authorized", "unauthorized"] | None = None
    status: Literal["pending", "processed"] = "pending"


class RuleSet(BaseModel):
    required_fields: list[Literal["title", "description", "copyright"]]
    prohibited_terms: list[str]


class ExistingBooking(BaseModel):
    channel: str
    start_at: AwareDatetime
    duration_minutes: int = Field(default=60, ge=1, le=1440)
    label: str
