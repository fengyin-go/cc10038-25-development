/**
 * 事件时间检索与回溯重放 API。
 * 对应后端 /api/event-timeline：跨日时间段检索、来源时区登记、按事件时间范围重放。
 */
import { request } from './client'

export type ConfirmedHit = {
  raw_record_id: number
  batch_no: string
  source_id: string
  event_time_raw: string
  local_date: string
  原始正文: string
  tz_basis: string
  timezone: string
  ingested_at: string
  hit_type: string
  event_instant_utc: string | null
  candidate_dates: string[]
  candidate_detail?: { candidate_date: string; offset_basis: string[] }[]
}

export type SearchResult = {
  ok: boolean
  message: string
  search_id: number
  checklist_id: number
  window: { start_utc: string; end_utc: string; anchor_timezone: string }
  confirmed_count: number
  pending_count: number
  replay_scope: { start_utc: string; end_utc: string; raw_record_ids: number[] }
  confirmed: ConfirmedHit[]
  page: number
  size: number
  total: number
  pending_candidates: ConfirmedHit[]
  checklist: Record<string, unknown>
}

export type ReplayEntry = {
  id: number
  search_id: number
  checklist_id: number
  新解析规则版本: string
  重放状态: string
  产出记录数: string
  status: string
  interrupted: boolean
  next_cursor: number
  scope_raw_ids: number[]
  tz_snapshot: Record<string, unknown>
  last_recovery_check?: Record<string, unknown>
}

async function postJson<T>(path: string, body: unknown): Promise<T> {
  const response = await request(path, {
    method: 'POST',
    body: JSON.stringify(body),
  })
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return (await response.json()) as T
}

async function getJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) throw new Error(`接口返回 ${response.status}`)
  return (await response.json()) as T
}

export function registerSourceTimezone(sourceId: string, timezone: string, basis = '') {
  return postJson<{ ok: boolean; message: string; entry?: Record<string, unknown> }>(
    '/api/event-timeline/source-timezone',
    { source_id: sourceId, timezone, basis },
  )
}

export function listSourceTimezones() {
  return getJson<{ items: Record<string, unknown>[] }>('/api/event-timeline/source-timezone')
}

export function searchEventWindow(payload: {
  start_at: string
  end_at: string
  source_ids: string[]
  anchor_timezone?: string
}) {
  return postJson<SearchResult>('/api/event-timeline/search', {
    anchor_timezone: 'UTC',
    ...payload,
  })
}

export function startReplay(
  searchId: number,
  newParserRule: string,
  opts: { page_size?: number; max_pages?: number | null } = {},
) {
  return postJson<{ ok: boolean; message: string; replay: ReplayEntry; processed: unknown[] }>(
    '/api/event-timeline/replay',
    {
      search_id: searchId,
      new_parser_rule: newParserRule,
      page_size: opts.page_size ?? 10,
      max_pages: opts.max_pages ?? null,
    },
  )
}

export function recoverReplay(replayId: number) {
  return postJson<{
    ok: boolean
    message: string
    replay: ReplayEntry
    processed: unknown[]
    time_drift: unknown[]
  }>(`/api/event-timeline/replay/${replayId}/recover`, {})
}

export function listReplays(searchId?: number) {
  const query = searchId ? `?search_id=${searchId}` : ''
  return getJson<{ items: ReplayEntry[] }>(`/api/event-timeline/replay${query}`)
}

export function listRevisions(replayId: number) {
  return getJson<{
    ok: boolean
    total: number
    replay: ReplayEntry
    items: Record<string, unknown>[]
  }>(`/api/event-timeline/replay/${replayId}/revisions`)
}

export function getChecklist(checklistId: number) {
  return getJson<{
    ok: boolean
    checklist: Record<string, unknown>
    items: Record<string, unknown>[]
    total: number
  }>(`/api/event-timeline/checklist/${checklistId}`)
}
