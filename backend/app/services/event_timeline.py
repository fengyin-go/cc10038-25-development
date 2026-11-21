"""跨日事件时间检索与回溯重放。

原始日志只能按接收时点（采集时间戳）浏览，本服务补上按**原始正文里的事件时间**
定位原文的入口，围绕事故排查的真实流程组织：

1. 来源的时间配置（时区）登记在 ``source_timezone`` 表，检索时读取，同一小时
   的两个来源可以一个带时区、一个只有本地时钟。
2. ``search_event_window`` 做跨日时间段检索，按事件时间生成重放范围：
   - 时间依据充分（正文带 UTC 偏移，或来源已登记时区）的记录判定为「精确命中」；
   - 时区未确定且跨日的记录不伪装成精确匹配，列入待确认候选，并保留日志时间原文；
   - 候选可能落在窗口前一天或后一天（旧日志），候选日期按 UTC 全天覆盖推算。
3. ``start_replay`` 只消费已确认命中的重放范围，读取实际正文、按页处理并回写
   事故排查清单；``recover_replay`` 支持中断恢复，恢复时仍用搜索当时保存的时间
   解释（时区快照）核对范围。
4. 重放产出的修订明细保留原日期、时区依据和日志时间原文，查询与重放记录通过
   search_id / checklist_id / replay_id 保持关联。

只依赖标准库（datetime/re/zoneinfo），与项目其余部分保持一致。
"""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from app.store import store

try:  # 标准库 zoneinfo 在 3.9+ 可用，缺失时退回内置常用时区表
    from zoneinfo import ZoneInfo  # type: ignore
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore


# 常见命名时区兜底，保证没有 tzdata 的环境也能跑通演示
_FALLBACK_ZONES: dict[str, int] = {
    "UTC": 0,
    "GMT": 0,
    "Asia/Shanghai": 8 * 60,
    "Asia/Tokyo": 9 * 60,
    "Asia/Singapore": 8 * 60,
    "Asia/Bangkok": 7 * 60,
    "Europe/London": 0,
    "Europe/Berlin": 60,
    "America/New_York": -5 * 60,
    "America/Los_Angeles": -8 * 60,
}

# 历史正文里出现过的时间形态：
#   2026-11-20T15:30:00+09:00
#   2026-11-20T15:30:00Z
#   2026-11-20 15:30:00          （历史格式，无分隔符 T、无时区）
#   2026-11-20 15:30
_EVENT_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2})?)"
    r"(Z|z|[+-]\d{2}:?\d{2})?"
)
_OFFSET_RE = re.compile(r"^([+-])(\d{2}):?(\d{2})$")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _next_id(module: str) -> int:
    return max((int(r.get("id", 0)) for r in store.rows(module)), default=0) + 1


def _parse_offset(token: str) -> int | None:
    """把 Z/+09:00/-0500 解析成分钟级 UTC 偏移。"""
    if not token:
        return None
    if token in ("Z", "z"):
        return 0
    match = _OFFSET_RE.match(token)
    if not match:
        return None
    sign, hours, minutes = match.groups()
    total = int(hours) * 60 + int(minutes)
    return total if sign == "+" else -total


def resolve_tz(tz_name: str) -> tuple[timezone | None, int | None]:
    """解析命名时区，返回 (tzinfo, 分钟偏移)；固定偏移名称也支持。"""
    if not tz_name:
        return None, None
    fixed = _parse_offset(tz_name)
    if fixed is not None:
        return timezone(timedelta(minutes=fixed)), fixed
    if ZoneInfo is not None:
        try:
            tz = ZoneInfo(tz_name)
            probe = datetime(2026, 1, 1, tzinfo=tz)
            return tz, int(probe.utcoffset().total_seconds()) // 60
        except Exception:
            pass
    if tz_name in _FALLBACK_ZONES:
        minutes = _FALLBACK_ZONES[tz_name]
        return timezone(timedelta(minutes=minutes)), minutes
    return None, None


def extract_event_time(body: str) -> dict[str, Any] | None:
    """从原始正文里抽取事件时间，区分带偏移/仅本地时钟，兼容历史时间值。"""
    if not body:
        return None
    match = _EVENT_RE.search(str(body))
    if not match:
        return None
    date_part, clock_part, offset_token = match.groups()
    try:
        if len(clock_part) == 5:
            naive = datetime.strptime(f"{date_part} {clock_part}", "%Y-%m-%d %H:%M")
        else:
            naive = datetime.strptime(f"{date_part} {clock_part}", "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return None
    offset_minutes = _parse_offset(offset_token) if offset_token else None
    return {
        "raw": match.group(0),
        "naive": naive,
        "offset_token": offset_token,
        "offset_minutes": offset_minutes,
        "has_offset": offset_minutes is not None,
    }


class EventTimelineService:
    """跨日时间段检索、待确认候选管理与按事件时间范围的回溯重放。"""

    MODULE = "event_timeline"

    # ---------- 来源时间配置 ----------
    def register_source_timezone(
        self, source_id: str, tz_name: str, basis: str = ""
    ) -> dict[str, Any]:
        """登记/更新来源的时区配置，旧来源补登记时版本递增。"""
        tz, offset_minutes = resolve_tz(tz_name)
        if tz is None:
            return {"ok": False, "message": f"无法识别的时区配置：{tz_name}"}
        table = store.rows("source_timezone")
        existing = next((row for row in table if row.get("source_id") == source_id), None)
        now = _now_iso()
        if existing:
            existing["timezone"] = tz_name
            existing["offset_minutes"] = offset_minutes
            existing["basis"] = basis or existing.get("basis", "")
            existing["tz_version"] = int(existing.get("tz_version", 1)) + 1
            existing["updated_at"] = now
            return {"ok": True, "message": f"来源 {source_id} 时区已更新", "entry": existing}
        entry = {
            "id": _next_id("source_timezone"),
            "source_id": source_id,
            "timezone": tz_name,
            "offset_minutes": offset_minutes,
            "basis": basis or "人工登记",
            "status": "已确定",
            "pending": False,
            "abnormal": False,
            "tz_version": 1,
            "registered_at": now,
            "updated_at": now,
        }
        table.append(entry)
        return {"ok": True, "message": f"来源 {source_id} 时区已登记", "entry": entry}

    def get_source_timezone(self, source_id: str) -> dict[str, Any] | None:
        for row in store.rows("source_timezone"):
            if row.get("source_id") == source_id:
                return row
        return None

    # ---------- 时间窗口解析 ----------
    def _parse_window_bound(self, value: str, tz: timezone | None) -> datetime:
        """窗口边界优先按带偏移 ISO 解释；纯本地时间按检索锚定时区解释，再转 UTC。"""
        text = value.strip().replace("Z", "+00:00")
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            parsed = datetime.strptime(value.strip(), "%Y-%m-%d %H:%M:%S")
        if parsed.tzinfo is None:
            if tz is None:
                raise ValueError("窗口边界不带时区，且未提供锚定时区")
            parsed = parsed.replace(tzinfo=tz)
        return parsed.astimezone(timezone.utc)

    def _interpret_record(
        self,
        raw: dict[str, Any],
        tz_override: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        """对一条原始记录做事件时间解释，输出确定性、UTC 瞬时和时区依据。

        tz_override 传入检索时保存的来源时区快照（``{source_id: {timezone,...}}``），
        保证重放恢复时沿用检索当时的时间解释，不受事后时区补登记影响。
        """
        body = str(raw.get("原始正文", ""))
        event = extract_event_time(body)
        if event is None:
            return None
        source_id = str(raw.get("source_id") or raw.get("所属数据源") or "")
        if tz_override is not None:
            snap = tz_override.get(source_id) or {}
            tz_name = snap.get("timezone") or ""
            tz_row = None
        else:
            tz_row = self.get_source_timezone(source_id)
            tz_name = tz_row.get("timezone") if tz_row else ""
        tz, _ = (resolve_tz(tz_name) if tz_name else (None, None))

        naive: datetime = event["naive"]
        if event["has_offset"]:
            instant = naive.replace(
                tzinfo=timezone(timedelta(minutes=event["offset_minutes"]))
            ).astimezone(timezone.utc)
            return {
                "event": event,
                "certain": True,
                "instant_utc": instant,
                "tz_basis": "正文UTC偏移",
                "timezone": f"UTC{event['offset_token']}",
                "tz_row": tz_row,
                "local_date": naive.date().isoformat(),
            }
        if tz is not None:
            instant = naive.replace(tzinfo=tz).astimezone(timezone.utc)
            return {
                "event": event,
                "certain": True,
                "instant_utc": instant,
                "tz_basis": "来源时区配置",
                "timezone": tz_name,
                "tz_row": tz_row,
                "local_date": naive.date().isoformat(),
            }
        return {
            "event": event,
            "certain": False,
            "instant_utc": None,
            "tz_basis": "未确定",
            "timezone": "",
            "tz_row": tz_row,
            "local_date": naive.date().isoformat(),
        }

    @staticmethod
    def _uncertain_candidates(naive: datetime) -> list[dict[str, Any]]:
        """时区未确定时按 UTC 全天偏移覆盖，推算可能落入的 UTC 日期及依据偏移。"""
        candidates: dict[str, list[str]] = {}
        # 覆盖全球实际 UTC 偏移范围 -12:00 ~ +14:00，按小时枚举
        for minutes in range(-12 * 60, 14 * 60 + 1, 60):
            instant = naive.replace(tzinfo=timezone(timedelta(minutes=minutes))).astimezone(
                timezone.utc
            )
            day = instant.date().isoformat()
            sign = "+" if minutes >= 0 else "-"
            label = f"UTC{sign}{abs(minutes) // 60:02d}:{abs(minutes) % 60:02d}"
            candidates.setdefault(day, []).append(label)
        return [
            {"candidate_date": day, "offset_basis": labels}
            for day, labels in sorted(candidates.items())
        ]

    # ---------- 跨日时间段检索 ----------
    def search_event_window(
        self,
        *,
        start_at: str,
        end_at: str,
        source_ids: list[str] | None = None,
        anchor_timezone: str = "UTC",
        page: int = 1,
        size: int = 20,
    ) -> dict[str, Any]:
        """按原始正文事件时间做跨日检索，生成精确命中与待确认候选两套结果。"""
        anchor_tz, _ = resolve_tz(anchor_timezone)
        if anchor_tz is None:
            anchor_tz = timezone.utc
        try:
            window_start = self._parse_window_bound(start_at, anchor_tz)
            window_end = self._parse_window_bound(end_at, anchor_tz)
        except ValueError as exc:
            return {"ok": False, "message": f"时间窗口无法解析：{exc}"}
        if window_end <= window_start:
            return {"ok": False, "message": "结束时间必须晚于开始时间"}

        wanted = {str(s) for s in source_ids} if source_ids else None
        raws = [
            row
            for row in store.rows("raw_records")
            if not wanted or str(row.get("source_id") or row.get("所属数据源") or "") in wanted
        ]

        confirmed: list[dict[str, Any]] = []
        uncertain: list[dict[str, Any]] = []
        for raw in raws:
            interpreted = self._interpret_record(raw)
            if interpreted is None:
                continue
            event = interpreted["event"]
            base = {
                "raw_record_id": raw["id"],
                "batch_no": raw.get("批次编号", ""),
                "source_id": str(raw.get("source_id") or raw.get("所属数据源") or ""),
                "event_time_raw": event["raw"],
                "local_date": interpreted["local_date"],
                "原始正文": raw.get("原始正文", ""),
                "tz_basis": interpreted["tz_basis"],
                "timezone": interpreted["timezone"],
                "ingested_at": raw.get("采集时间戳", ""),
            }
            if interpreted["certain"]:
                instant = interpreted["instant_utc"]
                if window_start <= instant < window_end:
                    confirmed.append(
                        {
                            **base,
                            "hit_type": "精确命中",
                            "event_instant_utc": instant.isoformat(),
                            "candidate_dates": [instant.date().isoformat()],
                        }
                    )
                continue

            # 时区未确定：不做精确匹配，只按候选 UTC 日期与窗口覆盖日期比对
            window_days = {
                (window_start + timedelta(days=delta)).date().isoformat()
                for delta in range((window_end.date() - window_start.date()).days + 1)
            }
            candidates = self._uncertain_candidates(event["naive"])
            hit_candidates = [c for c in candidates if c["candidate_date"] in window_days]
            if hit_candidates:
                uncertain.append(
                    {
                        **base,
                        "hit_type": "待确认(时区未确定)",
                        "event_instant_utc": None,
                        "candidate_dates": [c["candidate_date"] for c in hit_candidates],
                        "candidate_detail": hit_candidates,
                    }
                )

        return self._persist_search(
            start_at=start_at,
            end_at=end_at,
            anchor_timezone=anchor_timezone,
            window_start=window_start,
            window_end=window_end,
            source_ids=sorted(wanted) if wanted else [],
            confirmed=confirmed,
            uncertain=uncertain,
            page=page,
            size=size,
        )

    def _persist_search(
        self,
        *,
        start_at: str,
        end_at: str,
        anchor_timezone: str,
        window_start: datetime,
        window_end: datetime,
        source_ids: list[str],
        confirmed: list[dict[str, Any]],
        uncertain: list[dict[str, Any]],
        page: int,
        size: int,
    ) -> dict[str, Any]:
        search_id = _next_id("event_search")
        # 保存检索当时各来源的时区快照，重放恢复时据此核对时间解释
        tz_snapshot = {
            row["source_id"]: {
                "timezone": row.get("timezone"),
                "offset_minutes": row.get("offset_minutes"),
                "tz_version": row.get("tz_version"),
            }
            for row in store.rows("source_timezone")
        }
        search_entry = {
            "id": search_id,
            "window_start_input": start_at,
            "window_end_input": end_at,
            "anchor_timezone": anchor_timezone,
            "window_start_utc": window_start.isoformat(),
            "window_end_utc": window_end.isoformat(),
            "source_ids": ",".join(source_ids),
            "confirmed_count": len(confirmed),
            "pending_count": len(uncertain),
            "status": "已检索",
            "pending": len(uncertain) > 0,
            "abnormal": False,
            "tz_snapshot": tz_snapshot,
            "searched_at": _now_iso(),
        }
        store.rows("event_search").append(search_entry)

        checklist = store.rows("incident_checklist")
        checklist_id = _next_id("incident_checklist")
        checklist_entry = {
            "id": checklist_id,
            "search_id": search_id,
            "事故主题": f"跨日排查 {window_start.date().isoformat()}~{window_end.date().isoformat()}",
            "时间窗口": f"{window_start.isoformat()} ~ {window_end.isoformat()}",
            "精确命中数": len(confirmed),
            "待确认数": len(uncertain),
            "已重放数": 0,
            "状态": "待确认" if uncertain else "可重放",
            "status": "待确认" if uncertain else "可重放",
            "pending": True,
            "abnormal": False,
            "created_at": _now_iso(),
        }
        checklist.append(checklist_entry)

        detail_table = store.rows("incident_checklist_item")
        for item in confirmed + uncertain:
            detail_table.append(
                {
                    "id": _next_id("incident_checklist_item"),
                    "checklist_id": checklist_id,
                    "search_id": search_id,
                    "raw_record_id": item["raw_record_id"],
                    "source_id": item["source_id"],
                    "hit_type": item["hit_type"],
                    "event_time_raw": item["event_time_raw"],
                    "local_date": item["local_date"],
                    "candidate_dates": ",".join(item["candidate_dates"]),
                    "timezone": item["timezone"],
                    "tz_basis": item["tz_basis"],
                    "event_instant_utc": item["event_instant_utc"] or "",
                    "status": "待确认" if item["hit_type"] != "精确命中" else "待重放",
                    "pending": item["hit_type"] != "精确命中",
                    "abnormal": False,
                }
            )

        total = len(confirmed)
        start = (page - 1) * size
        return {
            "ok": True,
            "message": f"跨日检索完成：精确命中 {len(confirmed)} 条，待确认 {len(uncertain)} 条",
            "search_id": search_id,
            "checklist_id": checklist_id,
            "window": {
                "start_utc": window_start.isoformat(),
                "end_utc": window_end.isoformat(),
                "anchor_timezone": anchor_timezone,
            },
            "confirmed_count": len(confirmed),
            "pending_count": len(uncertain),
            "replay_scope": {
                "start_utc": window_start.isoformat(),
                "end_utc": window_end.isoformat(),
                "raw_record_ids": [item["raw_record_id"] for item in confirmed],
            },
            "confirmed": confirmed[start:start + size],
            "page": page,
            "size": size,
            "total": total,
            "pending_candidates": uncertain,
            "checklist": checklist_entry,
        }

    # ---------- 回溯重放 ----------
    def start_replay(
        self,
        search_id: int,
        new_parser_rule: str,
        *,
        page_size: int = 10,
        max_pages: int | None = None,
    ) -> dict[str, Any]:
        """按检索生成的事件时间重放范围读取正文并分页重放，可模拟中断。"""
        search = store.find("event_search", search_id)
        if search is None:
            return {"ok": False, "message": f"检索记录 {search_id} 不存在"}
        scope_ids = self._scope_raw_ids(search_id)
        if not scope_ids:
            return {"ok": False, "message": f"检索 {search_id} 没有可重放的精确命中记录"}

        replay_id = _next_id("event_replay")
        replay_entry = {
            "id": replay_id,
            "search_id": search_id,
            "checklist_id": self._checklist_id_of(search_id),
            "新解析规则版本": new_parser_rule,
            "历史批次范围": search.get("window_start_utc", ""),
            "重放状态": "重放中",
            "产出记录数": "0",
            "执行时间": _now_iso(),
            "status": "重放中",
            "pending": True,
            "abnormal": False,
            "scope_start_utc": search["window_start_utc"],
            "scope_end_utc": search["window_end_utc"],
            "scope_raw_ids": list(scope_ids),
            "tz_snapshot": search.get("tz_snapshot", {}),
            "page_size": page_size,
            "next_cursor": 0,
            "processed_ids": [],
            "interrupted": False,
        }
        store.rows("event_replay").append(replay_entry)
        processed = self._process_replay_page(replay_entry, max_pages=max_pages)
        return {
            "ok": True,
            "message": self._replay_message(replay_entry),
            "replay": replay_entry,
            "processed": processed,
        }

    def recover_replay(self, replay_id: int, *, max_pages: int | None = None) -> dict[str, Any]:
        """中断恢复：用检索保存的时区快照重新核对解释后，从断点继续分页重放。"""
        replay = store.find("event_replay", replay_id)
        if replay is None:
            return {"ok": False, "message": f"重放记录 {replay_id} 不存在"}
        if replay.get("status") == "已完成":
            return {"ok": False, "message": f"重放 {replay_id} 已完成，无需恢复"}

        search = store.find("event_search", int(replay["search_id"]))
        drift = self._detect_time_drift(search, replay.get("tz_snapshot", {}))
        replay["last_recovery_check"] = {
            "recovered_at": _now_iso(),
            "drift": drift,
            "snapshot": replay.get("tz_snapshot", {}),
        }
        replay["interrupted"] = False
        processed = self._process_replay_page(replay, max_pages=max_pages)
        return {
            "ok": True,
            "message": self._replay_message(replay),
            "replay": replay,
            "processed": processed,
            "time_drift": drift,
        }

    def _process_replay_page(
        self, replay: dict[str, Any], *, max_pages: int | None
    ) -> list[dict[str, Any]]:
        """实际读取正文并按页处理；max_pages 限制本次处理页数，用于模拟中断。"""
        scope_ids: list[int] = list(replay["scope_raw_ids"])
        cursor = int(replay.get("next_cursor", 0))
        page_size = int(replay.get("page_size", 10))
        pages_done = 0
        processed: list[dict[str, Any]] = []
        revision_table = store.rows("event_replay_revision")

        while cursor < len(scope_ids):
            page_ids = scope_ids[cursor:cursor + page_size]
            # 重放（含中断恢复）一律按检索快照解释事件时间
            snapshot = replay.get("tz_snapshot", {})
            for raw_id in page_ids:
                raw = store.find("raw_records", raw_id)
                if raw is None:
                    continue
                interpreted = self._interpret_record(raw, tz_override=snapshot)
                event = interpreted["event"] if interpreted else None
                revision = {
                    "id": _next_id("event_replay_revision"),
                    "replay_id": replay["id"],
                    "search_id": replay["search_id"],
                    "checklist_id": replay["checklist_id"],
                    "raw_record_id": raw_id,
                    "source_id": str(raw.get("source_id") or raw.get("所属数据源") or ""),
                    "batch_no": raw.get("批次编号", ""),
                    "原日期": interpreted["local_date"] if interpreted else "",
                    "时区依据": interpreted["tz_basis"] if interpreted else "未确定",
                    "timezone": interpreted["timezone"] if interpreted else "",
                    "日志时间原文": event["raw"] if event else "",
                    "event_instant_utc": (
                        interpreted["instant_utc"].isoformat()
                        if interpreted and interpreted["certain"]
                        else ""
                    ),
                    "原始正文": raw.get("原始正文", ""),
                    "解析规则版本": replay["新解析规则版本"],
                    "status": "已修订",
                    "pending": False,
                    "abnormal": False,
                    "replayed_at": _now_iso(),
                }
                revision_table.append(revision)
                processed.append(revision)
                replay.setdefault("processed_ids", []).append(raw_id)
                self._mark_checklist_item(replay["checklist_id"], raw_id, replay["id"])
            cursor += len(page_ids)
            replay["next_cursor"] = cursor
            replay["产出记录数"] = str(len(replay["processed_ids"]))
            pages_done += 1
            if max_pages is not None and pages_done >= max_pages and cursor < len(scope_ids):
                replay["interrupted"] = True
                replay["status"] = "部分失败"
                replay["重放状态"] = "中断待恢复"
                replay["pending"] = True
                replay["abnormal"] = True
                return processed

        replay["interrupted"] = False
        replay["status"] = "已完成"
        replay["重放状态"] = "已完成"
        replay["pending"] = False
        replay["abnormal"] = False
        self._sync_checklist(replay["checklist_id"], replay["id"])
        return processed

    # ---------- 查询与关联 ----------
    def list_replays(self, search_id: int | None = None) -> list[dict[str, Any]]:
        rows = store.rows("event_replay")
        if search_id is not None:
            rows = [r for r in rows if int(r.get("search_id", 0)) == search_id]
        return rows

    def list_revisions(self, replay_id: int) -> dict[str, Any]:
        replay = store.find("event_replay", replay_id)
        if replay is None:
            return {"ok": False, "message": f"重放记录 {replay_id} 不存在", "items": []}
        items = [
            row
            for row in store.rows("event_replay_revision")
            if int(row.get("replay_id", 0)) == replay_id
        ]
        return {
            "ok": True,
            "replay": replay,
            "total": len(items),
            "items": items,
        }

    def get_checklist(self, checklist_id: int) -> dict[str, Any]:
        entry = store.find("incident_checklist", checklist_id)
        if entry is None:
            return {"ok": False, "message": f"事故排查清单 {checklist_id} 不存在"}
        items = [
            row
            for row in store.rows("incident_checklist_item")
            if int(row.get("checklist_id", 0)) == checklist_id
        ]
        return {"ok": True, "checklist": entry, "items": items, "total": len(items)}

    def list_searches(self) -> list[dict[str, Any]]:
        return list(store.rows("event_search"))

    # ---------- 内部工具 ----------
    def _scope_raw_ids(self, search_id: int) -> list[int]:
        return [
            int(row["raw_record_id"])
            for row in store.rows("incident_checklist_item")
            if int(row.get("search_id", 0)) == search_id and row.get("hit_type") == "精确命中"
        ]

    def _checklist_id_of(self, search_id: int) -> int:
        for row in store.rows("incident_checklist"):
            if int(row.get("search_id", 0)) == search_id:
                return int(row["id"])
        return 0

    def _mark_checklist_item(self, checklist_id: int, raw_id: int, replay_id: int) -> None:
        for row in store.rows("incident_checklist_item"):
            if (
                int(row.get("checklist_id", 0)) == checklist_id
                and int(row.get("raw_record_id", 0)) == raw_id
            ):
                row["status"] = "已重放"
                row["pending"] = False
                row["replay_id"] = replay_id

    def _sync_checklist(self, checklist_id: int, replay_id: int) -> None:
        entry = store.find("incident_checklist", checklist_id)
        if entry is None:
            return
        items = [
            row
            for row in store.rows("incident_checklist_item")
            if int(row.get("checklist_id", 0)) == checklist_id
        ]
        replayed = sum(1 for row in items if row.get("status") == "已重放")
        pending = sum(1 for row in items if row.get("hit_type") != "精确命中")
        entry["已重放数"] = replayed
        entry["状态"] = "待确认覆盖范围" if pending else "已完成"
        entry["status"] = entry["状态"]
        entry["pending"] = pending > 0
        entry["last_replay_id"] = replay_id

    def _detect_time_drift(
        self, search: dict[str, Any] | None, snapshot: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """恢复时比对检索快照与当前来源时区配置，输出时间解释漂移。"""
        if search is None:
            return []
        drift: list[dict[str, Any]] = []
        for source_id, old in snapshot.items():
            current = self.get_source_timezone(source_id)
            current_tz = current.get("timezone") if current else ""
            if current_tz != old.get("timezone"):
                drift.append(
                    {
                        "source_id": source_id,
                        "search_timezone": old.get("timezone"),
                        "current_timezone": current_tz,
                        "note": "来源时区在检索后发生变更，本次仍按检索快照解释",
                    }
                )
        return drift

    @staticmethod
    def _replay_message(replay: dict[str, Any]) -> str:
        if replay.get("interrupted"):
            return (
                f"重放 {replay['id']} 已处理 {len(replay['processed_ids'])}/"
                f"{len(replay['scope_raw_ids'])} 条后中断，可调用恢复接口续跑"
            )
        return f"重放 {replay['id']} 已完成，产出修订明细 {len(replay['processed_ids'])} 条"


event_timeline = EventTimelineService()
