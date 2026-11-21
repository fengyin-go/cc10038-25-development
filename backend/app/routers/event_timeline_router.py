"""事件时间检索与重放路由：跨日时间段检索、来源时区登记与回溯重放。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from pydantic import BaseModel, Field

from app.services.event_timeline import event_timeline
from app.store import store

router = APIRouter(prefix="/api/event-timeline", tags=["事件时间检索与重放"])


class SourceTimezonePayload(BaseModel):
    source_id: str = Field(..., min_length=1)
    timezone: str = Field(..., min_length=1)
    basis: str = ""


class EventSearchPayload(BaseModel):
    start_at: str = Field(..., min_length=1)
    end_at: str = Field(..., min_length=1)
    source_ids: list[str] | None = None
    anchor_timezone: str = "UTC"
    page: int = Field(default=1, ge=1)
    size: int = Field(default=20, ge=1, le=200)


class ReplayStartPayload(BaseModel):
    search_id: int
    new_parser_rule: str = Field(..., min_length=1)
    page_size: int = Field(default=10, ge=1)
    # 本次最多处理的页数，达到后保留断点，用于模拟重放中断；为空则一次跑完
    max_pages: int | None = Field(default=None, ge=1)


class ReplayRecoverPayload(BaseModel):
    max_pages: int | None = Field(default=None, ge=1)


@router.post("/source-timezone")
def register_source_timezone(payload: SourceTimezonePayload) -> dict[str, Any]:
    """登记或补登记来源的时间配置（时区），旧来源补登记时版本递增。"""
    return event_timeline.register_source_timezone(
        payload.source_id, payload.timezone, payload.basis
    )


@router.get("/source-timezone")
def list_source_timezone() -> dict[str, Any]:
    return {"items": list(store.rows("source_timezone"))}


@router.post("/search")
def search_event_window(payload: EventSearchPayload) -> dict[str, Any]:
    """跨日时间段检索：按正文事件时间生成重放范围，单列时区未确定候选。"""
    return event_timeline.search_event_window(
        start_at=payload.start_at,
        end_at=payload.end_at,
        source_ids=payload.source_ids,
        anchor_timezone=payload.anchor_timezone,
        page=payload.page,
        size=payload.size,
    )


@router.get("/search")
def list_event_searches() -> dict[str, Any]:
    return {"items": event_timeline.list_searches()}


@router.post("/replay")
def start_replay(payload: ReplayStartPayload) -> dict[str, Any]:
    """按事件时间重放范围读取实际正文、分页处理并回写事故排查清单。"""
    return event_timeline.start_replay(
        payload.search_id,
        payload.new_parser_rule,
        page_size=payload.page_size,
        max_pages=payload.max_pages,
    )


@router.post("/replay/{replay_id}/recover")
def recover_replay(replay_id: int, payload: ReplayRecoverPayload) -> dict[str, Any]:
    """重放中断恢复：先核对检索时的时间解释，再从断点续跑。"""
    return event_timeline.recover_replay(replay_id, max_pages=payload.max_pages)


@router.get("/replay")
def list_replays(search_id: int | None = None) -> dict[str, Any]:
    return {"items": event_timeline.list_replays(search_id)}


@router.get("/replay/{replay_id}/revisions")
def list_revisions(replay_id: int) -> dict[str, Any]:
    """修订明细：可返回原日期、时区依据和日志时间原文供重放核对。"""
    return event_timeline.list_revisions(replay_id)


@router.get("/checklist/{checklist_id}")
def get_checklist(checklist_id: int) -> dict[str, Any]:
    return event_timeline.get_checklist(checklist_id)
