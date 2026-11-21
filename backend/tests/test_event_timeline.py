"""跨日事件时间检索与回溯重放的业务行为测试。

覆盖题述场景：
- 同一小时的两个来源，一个带时区一个只有本地时钟；
- 跨日搜索既要给出确定落在窗口内的原文，也要列出可能落在前一天/后一天的候选；
- 时区未确定且跨日的结果不能伪装成精确匹配，且保留日志时间原文；
- 回溯重放按事件时间重放范围读取实际正文、分页处理、中断可恢复，
  结果回写事故排查清单，修订明细能返回原日期与时区依据；
- 补登记旧来源时区后重新检索，命中原文增加、待确认数量下降，重放记录仍可关联；
- 兼容历史正文的原始时间值（空格分隔、无时区、分钟精度）。
"""
from __future__ import annotations

import pytest

from app.store import store
from app.services.event_timeline import event_timeline, extract_event_time


@pytest.fixture
def two_sources(pipeline):
    """同一小时的两个来源：tokyo 正文自带时区，legacy 只有本地时钟。"""
    pipeline.register_source("src-tokyo", "http", "tokyo")
    pipeline.validate_source("src-tokyo")
    pipeline.register_source("src-legacy", "http", "legacy-dc")
    pipeline.validate_source("src-legacy")
    return "src-tokyo", "src-legacy"


def _ingest(pipeline, source_id: str, bodies: list[str]) -> str:
    logs = [{"body": body} for body in bodies]
    result = pipeline.start_ingest_batch(source_id, "2026-11-crossday", logs)
    assert result["ok"] is True, result["message"]
    return result["batch"]["批次编号"]


# 跨日两小时窗口（UTC）：2026-11-20T15:00:00Z ~ 2026-11-20T17:00:00Z
WINDOW_START = "2026-11-20T15:00:00Z"
WINDOW_END = "2026-11-20T17:00:00Z"


class TestEventTimeExtraction:
    """事件时间抽取：兼容带偏移、Z 结尾与历史本地时钟格式。"""

    def test_extract_with_offset(self):
        parsed = extract_event_time("2026-11-20T15:30:00+09:00|ERROR|boom")
        assert parsed is not None
        assert parsed["has_offset"] is True
        assert parsed["offset_minutes"] == 9 * 60
        assert parsed["raw"] == "2026-11-20T15:30:00+09:00"

    def test_extract_z_suffix(self):
        parsed = extract_event_time("2026-11-20T15:30:00Z INFO ok")
        assert parsed is not None and parsed["offset_minutes"] == 0

    def test_extract_legacy_local_clock(self):
        # 历史正文：空格分隔、秒级、无时区
        parsed = extract_event_time("2026-11-20 15:30:00|WARN|legacy")
        assert parsed is not None
        assert parsed["has_offset"] is False
        assert parsed["raw"] == "2026-11-20 15:30:00"

    def test_extract_legacy_minute_precision(self):
        parsed = extract_event_time("20261120 no-event-here")
        assert parsed is None


class TestCrossDaySearch:
    """跨日时间段检索：精确命中与待确认候选分离。"""

    def test_zoned_source_confirmed_hit(self, pipeline, two_sources):
        tokyo, _ = two_sources
        # +09:00 的 20:30 对应 UTC 11:30，不命中；00:30(+09) 对应 UTC 15:30，命中
        _ingest(pipeline, tokyo, [
            "2026-11-21T00:30:00+09:00|ERROR|confirmed-cross-midnight",
            "2026-11-20T20:30:00+09:00|INFO|outside-window",
        ])
        result = event_timeline.search_event_window(
            start_at=WINDOW_START, end_at=WINDOW_END, source_ids=[tokyo])
        assert result["ok"] is True
        assert result["confirmed_count"] == 1
        assert result["pending_count"] == 0
        hit = result["confirmed"][0]
        assert hit["hit_type"] == "精确命中"
        assert hit["event_instant_utc"].startswith("2026-11-20T15:30:00")
        assert hit["tz_basis"] == "正文UTC偏移"
        # 日志时间原文保留，供重放核对
        assert hit["event_time_raw"] == "2026-11-21T00:30:00+09:00"

    def test_uncertain_local_clock_not_faked_as_precise(self, pipeline, two_sources):
        _, legacy = two_sources
        # 本地时钟 15:30：若按 UTC 解释则在窗口内，但时区未确定，只能作为候选
        _ingest(pipeline, legacy, [
            "2026-11-20 15:30:00|ERROR|local-clock-only",
        ])
        result = event_timeline.search_event_window(
            start_at=WINDOW_START, end_at=WINDOW_END, source_ids=[legacy])
        assert result["confirmed_count"] == 0
        assert result["pending_count"] == 1
        candidate = result["pending_candidates"][0]
        assert candidate["hit_type"] == "待确认(时区未确定)"
        assert candidate["event_instant_utc"] is None
        assert candidate["event_time_raw"] == "2026-11-20 15:30:00"
        # 候选明细保留每种可能对应的偏移依据，供值班员核实覆盖范围
        assert all(item["offset_basis"] for item in candidate["candidate_detail"])
        # 时区未确定时绝不给出精确瞬时
        assert candidate["event_instant_utc"] is None

    def test_uncertain_candidate_may_land_on_window_day(self, pipeline, two_sources):
        _, legacy = two_sources
        # 前一天傍晚的本地时钟：按负偏移解释会进入窗口当天的 UTC，必须列为候选
        _ingest(pipeline, legacy, [
            "2026-11-19 16:00:00|WARN|previous-day-old-log",
        ])
        result = event_timeline.search_event_window(
            start_at=WINDOW_START, end_at=WINDOW_END, source_ids=[legacy])
        assert result["confirmed_count"] == 0
        assert result["pending_count"] == 1
        candidate = result["pending_candidates"][0]
        assert candidate["hit_type"] == "待确认(时区未确定)"
        assert "2026-11-20" in candidate["candidate_dates"]
        # 保留日志时间原文，供值班员核对它其实是前一天的旧日志
        assert candidate["local_date"] == "2026-11-19"
        assert candidate["event_time_raw"] == "2026-11-19 16:00:00"

    def test_uncertain_early_morning_spans_two_utc_days(self, pipeline, two_sources):
        _, legacy = two_sources
        # 本地凌晨 02:00：按正偏移解释落在前一天 UTC，按负偏移落在当天，跨日覆盖两天
        _ingest(pipeline, legacy, [
            "2026-11-21 02:00:00|INFO|after-midnight",
        ])
        result = event_timeline.search_event_window(
            start_at="2026-11-20T15:00:00Z",
            end_at="2026-11-21T17:00:00Z",
            source_ids=[legacy])
        assert result["pending_count"] == 1
        candidate = result["pending_candidates"][0]
        dates = candidate["candidate_dates"]
        assert "2026-11-20" in dates and "2026-11-21" in dates

    def test_search_persists_and_links_checklist(self, pipeline, two_sources):
        tokyo, _ = two_sources
        _ingest(pipeline, tokyo, ["2026-11-21T00:30:00+09:00|ERROR|x"])
        result = event_timeline.search_event_window(
            start_at=WINDOW_START, end_at=WINDOW_END, source_ids=[tokyo])
        search_id = result["search_id"]
        assert store.find("event_search", search_id) is not None
        checklist = event_timeline.get_checklist(result["checklist_id"])
        assert checklist["ok"] is True
        assert checklist["checklist"]["精确命中数"] == 1
        item = checklist["items"][0]
        assert item["search_id"] == search_id
        assert item["hit_type"] == "精确命中"
        assert item["status"] == "待重放"

    def test_replay_scope_only_contains_confirmed(self, pipeline, two_sources):
        tokyo, legacy = two_sources
        _ingest(pipeline, tokyo, ["2026-11-21T00:30:00+09:00|ERROR|a"])
        _ingest(pipeline, legacy, ["2026-11-20 15:30:00|ERROR|b"])
        result = event_timeline.search_event_window(
            start_at=WINDOW_START, end_at=WINDOW_END, source_ids=[tokyo, legacy])
        assert result["confirmed_count"] == 1
        assert result["pending_count"] == 1
        scope = result["replay_scope"]
        assert len(scope["raw_record_ids"]) == 1
        assert scope["start_utc"].startswith("2026-11-20T15:00")


class TestReplay:
    """回溯重放：按事件时间范围读取正文、分页、中断恢复、清单回写与关联。"""

    def _prepare(self, pipeline, two_sources):
        tokyo, legacy = two_sources
        _ingest(pipeline, tokyo, [
            "2026-11-21T00:30:00+09:00|ERROR|one",
            "2026-11-21T01:30:00+09:00|ERROR|two",
            "2026-11-21T01:45:00+09:00|WARN|three",
        ])
        _ingest(pipeline, legacy, ["2026-11-20 15:30:00|ERROR|uncertain"])
        return event_timeline.search_event_window(
            start_at=WINDOW_START, end_at=WINDOW_END, source_ids=[tokyo, legacy])

    def test_replay_reads_actual_body_and_writes_checklist(self, pipeline, two_sources):
        search = self._prepare(pipeline, two_sources)
        started = event_timeline.start_replay(search["search_id"], "rule-v2", page_size=10)
        assert started["ok"] is True
        replay = started["replay"]
        assert replay["status"] == "已完成"
        # 只重放精确命中，待确认候选不参与
        assert int(replay["产出记录数"]) == 3
        revisions = event_timeline.list_revisions(replay["id"])
        assert revisions["total"] == 3
        first = revisions["items"][0]
        assert first["原始正文"].endswith("one")
        assert first["原日期"] == "2026-11-21"  # 正文里的本地原日期
        assert first["时区依据"] == "正文UTC偏移"
        assert first["日志时间原文"].startswith("2026-11-21T00:30:00")
        assert first["search_id"] == search["search_id"]
        # 清单回写：精确命中项已重放，候选仍待确认
        checklist = event_timeline.get_checklist(search["checklist_id"])
        assert checklist["checklist"]["已重放数"] == 3
        statuses = {item["raw_record_id"]: item["status"] for item in checklist["items"]}
        assert all(v == "已重放" for rid, v in statuses.items()
                   if rid in search["replay_scope"]["raw_record_ids"])
        uncertain = [i for i in checklist["items"] if i["hit_type"] != "精确命中"]
        assert uncertain and uncertain[0]["status"] == "待确认"
        assert checklist["checklist"]["status"] == "待确认覆盖范围"

    def test_replay_interrupt_and_recover_keeps_time_interpretation(
        self, pipeline, two_sources
    ):
        search = self._prepare(pipeline, two_sources)
        # 每页 1 条、最多处理 1 页 -> 处理 1 条后中断，保留断点
        started = event_timeline.start_replay(
            search["search_id"], "rule-v2", page_size=1, max_pages=1)
        replay = started["replay"]
        assert replay["interrupted"] is True
        assert replay["status"] == "部分失败"
        assert replay["next_cursor"] == 1
        assert len(replay["processed_ids"]) == 1

        # 恢复前先登记来源时区（检索快照里本没有该来源），恢复时仍按检索时解释续跑
        event_timeline.register_source_timezone("src-tokyo", "UTC", "演练变更")
        recovered = event_timeline.recover_replay(replay["id"])
        assert recovered["ok"] is True
        # 检索保存的时区快照被带到恢复核对环节，时间解释可追溯
        search_row = store.find("event_search", search["search_id"])
        check = recovered["replay"]["last_recovery_check"]
        assert check["snapshot"] == search_row["tz_snapshot"]
        assert check["snapshot"] == replay["tz_snapshot"]
        # 断点续跑：剩余 2 条处理完，总计 3 条
        finished = recovered["replay"]
        assert finished["status"] == "已完成"
        assert len(finished["processed_ids"]) == 3
        assert finished["next_cursor"] == 3
        # 修订明细仍带检索当时的时区依据，没有被后续时区变更污染
        revisions = event_timeline.list_revisions(replay["id"])["items"]
        assert all(item["时区依据"] == "正文UTC偏移" for item in revisions)
        assert len({item["raw_record_id"] for item in revisions}) == 3

    def test_recover_reports_drift_when_timezone_changes_after_search(
        self, pipeline, two_sources
    ):
        _, legacy = two_sources
        # 检索前先确定来源时区：本地 15:30/16:30（UTC）都在窗口内
        event_timeline.register_source_timezone(legacy, "UTC", "机房时钟=UTC")
        _ingest(pipeline, legacy, [
            "2026-11-20 15:30:00|ERROR|a",
            "2026-11-20 16:30:00|ERROR|b",
        ])
        search = event_timeline.search_event_window(
            start_at=WINDOW_START, end_at=WINDOW_END, source_ids=[legacy])
        assert search["confirmed_count"] == 2
        started = event_timeline.start_replay(
            search["search_id"], "rule-v2", page_size=1, max_pages=1)
        # 检索后来源时区被改成东九区
        event_timeline.register_source_timezone(legacy, "Asia/Tokyo", "事后补正")
        recovered = event_timeline.recover_replay(started["replay"]["id"])
        drift = recovered["time_drift"]
        assert len(drift) == 1
        assert drift[0]["source_id"] == legacy
        assert drift[0]["search_timezone"] == "UTC"
        assert drift[0]["current_timezone"] == "Asia/Tokyo"
        assert "检索快照" in drift[0]["note"]
        # 仍按检索时的 UTC 解释完成重放，原日期/瞬时不被事后变更改写
        revisions = event_timeline.list_revisions(started["replay"]["id"])["items"]
        assert len(revisions) == 2
        assert all(item["时区依据"] == "来源时区配置" for item in revisions)
        assert all(item["event_instant_utc"].startswith("2026-11-20T1") for item in revisions)

    def test_replay_without_confirmed_hits_rejected(self, pipeline, two_sources):
        _, legacy = two_sources
        _ingest(pipeline, legacy, ["2026-11-20 15:30:00|ERROR|only-uncertain"])
        search = event_timeline.search_event_window(
            start_at=WINDOW_START, end_at=WINDOW_END, source_ids=[legacy])
        result = event_timeline.start_replay(search["search_id"], "rule-v2")
        assert result["ok"] is False
        assert "精确命中" in result["message"]

    def test_replay_linked_back_to_search(self, pipeline, two_sources):
        search = self._prepare(pipeline, two_sources)
        started = event_timeline.start_replay(search["search_id"], "rule-v2")
        replay_id = started["replay"]["id"]
        listed = event_timeline.list_replays(search["search_id"])
        assert len(listed) == 1 and listed[0]["id"] == replay_id
        revisions = event_timeline.list_revisions(replay_id)
        assert all(item["replay_id"] == replay_id for item in revisions["items"])
        assert all(item["checklist_id"] == search["checklist_id"]
                   for item in revisions["items"])


class TestTimezoneBackfill:
    """补登记旧来源时区后重新检索：候选转正、待确认下降、重放记录可比较。"""

    def test_backfill_promotes_candidates_and_compare(self, pipeline, two_sources):
        tokyo, legacy = two_sources
        # 第三个旧来源全程不登记时区，用来验证候选不会被误判为精确匹配
        pipeline.register_source("src-unzoned", "http", "dc3")
        pipeline.validate_source("src-unzoned")
        _ingest(pipeline, tokyo, ["2026-11-21T00:30:00+09:00|ERROR|zoned"])
        _ingest(pipeline, legacy, [
            "2026-11-20 15:30:00|ERROR|legacy-local",       # +09:00 -> UTC 06:30，不命中
            "2026-11-21 00:30:00|ERROR|legacy-midnight",    # +09:00 -> UTC 15:30，命中
            "2026-11-19 16:00:00|WARN|previous-day",        # +09:00 -> 11-19 07:00，不命中
        ])
        _ingest(pipeline, "src-unzoned", ["2026-11-20 15:45:00|ERROR|still-unzoned"])

        first = event_timeline.search_event_window(
            start_at=WINDOW_START, end_at=WINDOW_END,
            source_ids=[tokyo, legacy, "src-unzoned"])
        assert first["confirmed_count"] == 1
        assert first["pending_count"] == 4
        first_replay = event_timeline.start_replay(first["search_id"], "rule-v1")
        assert int(first_replay["replay"]["产出记录数"]) == 1

        # 补登记旧来源时区（版本递增），重新检索
        reg = event_timeline.register_source_timezone(legacy, "Asia/Tokyo", "值班补登记")
        assert reg["entry"]["tz_version"] == 1
        second = event_timeline.search_event_window(
            start_at=WINDOW_START, end_at=WINDOW_END,
            source_ids=[tokyo, legacy, "src-unzoned"])

        # 命中原文从 1 增加到 2，待确认从 4 降到 1：时区确定后候选要么转正要么排除
        assert second["confirmed_count"] == 2
        assert second["pending_count"] == 1
        promoted = next(
            item for item in second["confirmed"]
            if item["event_time_raw"] == "2026-11-21 00:30:00"
        )
        assert promoted["hit_type"] == "精确命中"
        assert promoted["tz_basis"] == "来源时区配置"
        assert promoted["timezone"] == "Asia/Tokyo"
        assert promoted["event_instant_utc"].startswith("2026-11-20T15:30:00")
        # 前一天/当天但不在窗口内的旧日志在时区确定后被明确排除，不再混在候选里
        excluded_raws = {"2026-11-20 15:30:00", "2026-11-19 16:00:00"}
        hit_raws = {item["event_time_raw"] for item in second["confirmed"]}
        assert excluded_raws.isdisjoint(hit_raws)
        # 未登记时区的来源仍保留为候选，且原文时间未丢
        remaining = second["pending_candidates"][0]
        assert remaining["source_id"] == "src-unzoned"
        assert remaining["hit_type"] == "待确认(时区未确定)"
        assert remaining["event_time_raw"] == "2026-11-20 15:45:00"

        # 第二次重放消费新的重放范围，记录与两次检索分别关联
        second_replay = event_timeline.start_replay(second["search_id"], "rule-v2")
        assert int(second_replay["replay"]["产出记录数"]) == 2
        assert second_replay["replay"]["search_id"] == second["search_id"]
        assert first_replay["replay"]["search_id"] == first["search_id"]
        revisions = event_timeline.list_revisions(second_replay["replay"]["id"])["items"]
        legacy_revision = next(r for r in revisions if r["source_id"] == legacy)
        assert legacy_revision["时区依据"] == "来源时区配置"
        assert legacy_revision["原日期"] == "2026-11-21"
        assert legacy_revision["日志时间原文"] == "2026-11-21 00:30:00"

    def test_backfill_unknown_timezone_rejected(self):
        result = event_timeline.register_source_timezone("src-x", "Mars/Olympus")
        assert result["ok"] is False

    def test_backfill_supports_fixed_offset(self, pipeline, two_sources):
        _, legacy = two_sources
        _ingest(pipeline, legacy, ["2026-11-21 00:30:00|ERROR|offset-form"])
        event_timeline.register_source_timezone(legacy, "+09:00")
        result = event_timeline.search_event_window(
            start_at=WINDOW_START, end_at=WINDOW_END, source_ids=[legacy])
        assert result["confirmed_count"] == 1
        assert result["confirmed"][0]["event_instant_utc"].startswith("2026-11-20T15:30:00")
