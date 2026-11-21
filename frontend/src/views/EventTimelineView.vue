<template>
  <section class="page" data-module="event_timeline">
    <header class="page-head">
      <div>
        <h2>跨日事件时间检索与回溯重放</h2>
        <p class="page-desc">
          按原始正文里的事件时间（而非接收时点）定位原文：带时区的来源给精确命中，
          只有本地时钟的来源列为待确认候选；重放按事件时间范围读取正文并回写事故排查清单。
        </p>
      </div>
    </header>

    <!-- ① 来源时间配置 -->
    <article class="tl-panel">
      <h3>① 来源时间配置（时区）</h3>
      <form class="filter-bar" @submit.prevent="submitTimezone">
        <label class="filter-item">
          <span>来源标识</span>
          <input v-model="tzForm.sourceId" data-testid="tz-source" placeholder="如 src-legacy" required />
        </label>
        <label class="filter-item">
          <span>时区</span>
          <input v-model="tzForm.timezone" data-testid="tz-name" placeholder="如 Asia/Tokyo 或 +09:00" required />
        </label>
        <label class="filter-item">
          <span>登记依据</span>
          <input v-model="tzForm.basis" data-testid="tz-basis" placeholder="如 值班补登记" />
        </label>
        <button class="btn primary" data-testid="tz-submit" type="submit">登记/补登记时区</button>
        <button class="btn ghost" type="button" @click="loadTimezones">刷新</button>
      </form>
      <table class="data-table">
        <thead>
          <tr><th>来源标识</th><th>时区</th><th>UTC偏移(分钟)</th><th>依据</th><th>版本</th><th>更新时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="row in timezones" :key="String(row.id)">
            <td>{{ row.source_id }}</td>
            <td>{{ row.timezone }}</td>
            <td>{{ row.offset_minutes }}</td>
            <td>{{ row.basis }}</td>
            <td>v{{ row.tz_version }}</td>
            <td>{{ row.updated_at }}</td>
          </tr>
          <tr v-if="!timezones.length">
            <td colspan="6" class="empty-state">尚无来源时区配置，未配置的来源其跨日结果只会进入待确认候选</td>
          </tr>
        </tbody>
      </table>
    </article>

    <!-- ② 跨日时间段检索 -->
    <article class="tl-panel">
      <h3>② 跨日时间段检索（按正文事件时间）</h3>
      <form class="filter-bar" @submit.prevent="submitSearch">
        <label class="filter-item">
          <span>开始(UTC)</span>
          <input v-model="searchForm.startAt" data-testid="search-start" placeholder="2026-11-20T15:00:00Z" required />
        </label>
        <label class="filter-item">
          <span>结束(UTC)</span>
          <input v-model="searchForm.endAt" data-testid="search-end" placeholder="2026-11-20T17:00:00Z" required />
        </label>
        <label class="filter-item">
          <span>来源(逗号分隔，留空全部)</span>
          <input v-model="searchForm.sources" data-testid="search-sources" placeholder="src-tokyo,src-legacy" />
        </label>
        <button class="btn primary" data-testid="search-submit" type="submit">检索</button>
      </form>

      <div v-if="lastSearch" class="tl-summary">
        <span class="stat-card">
          <span class="stat-label">精确命中</span>
          <strong class="stat-value">{{ lastSearch.confirmed_count }}</strong>
        </span>
        <span class="stat-card">
          <span class="stat-label">待确认(时区未确定)</span>
          <strong class="stat-value">{{ lastSearch.pending_count }}</strong>
        </span>
        <span class="stat-card">
          <span class="stat-label">检索ID / 清单ID</span>
          <strong class="stat-value">{{ lastSearch.search_id }} / {{ lastSearch.checklist_id }}</strong>
        </span>
      </div>
      <p v-if="notice" class="error-text">{{ notice }}</p>

      <h4>精确命中原文（事件时间重放范围）</h4>
      <table class="data-table">
        <thead>
          <tr><th>原始记录</th><th>来源</th><th>日志时间原文</th><th>原日期</th><th>时区依据</th><th>事件瞬时(UTC)</th><th>原始正文</th></tr>
        </thead>
        <tbody>
          <tr v-for="hit in lastSearch?.confirmed ?? []" :key="hit.raw_record_id" data-testid="confirmed-row">
            <td>{{ hit.raw_record_id }}</td>
            <td>{{ hit.source_id }}</td>
            <td>{{ hit.event_time_raw }}</td>
            <td>{{ hit.local_date }}</td>
            <td>{{ hit.tz_basis }} {{ hit.timezone }}</td>
            <td>{{ hit.event_instant_utc }}</td>
            <td class="tl-body">{{ hit.原始正文 }}</td>
          </tr>
          <tr v-if="lastSearch && !lastSearch.confirmed.length">
            <td colspan="7" class="empty-state">没有时间依据充分、确定落在窗口内的原文</td>
          </tr>
        </tbody>
      </table>

      <h4>待确认候选（不伪装成精确匹配，含前/后一天旧日志）</h4>
      <table class="data-table">
        <thead>
          <tr><th>原始记录</th><th>来源</th><th>日志时间原文</th><th>本地日期</th><th>候选UTC日期</th><th>偏移依据</th><th>原始正文</th></tr>
        </thead>
        <tbody>
          <tr v-for="c in lastSearch?.pending_candidates ?? []" :key="c.raw_record_id" data-testid="candidate-row">
            <td>{{ c.raw_record_id }}</td>
            <td>{{ c.source_id }}</td>
            <td>{{ c.event_time_raw }}</td>
            <td>{{ c.local_date }}</td>
            <td>{{ c.candidate_dates.join(', ') }}</td>
            <td class="tl-body">
              <span v-for="d in c.candidate_detail ?? []" :key="d.candidate_date">
                {{ d.candidate_date }}: {{ d.offset_basis.join(' ') }}
              </span>
            </td>
            <td class="tl-body">{{ c.原始正文 }}</td>
          </tr>
          <tr v-if="lastSearch && !lastSearch.pending_candidates.length">
            <td colspan="7" class="empty-state">没有待确认候选，窗口覆盖范围已全部核实</td>
          </tr>
        </tbody>
      </table>
    </article>

    <!-- ③ 回溯重放 -->
    <article class="tl-panel">
      <h3>③ 按事件时间范围回溯重放</h3>
      <form class="filter-bar" @submit.prevent="submitReplay">
        <label class="filter-item">
          <span>检索ID</span>
          <input v-model.number="replayForm.searchId" data-testid="replay-search-id" type="number" required />
        </label>
        <label class="filter-item">
          <span>新解析规则版本</span>
          <input v-model="replayForm.rule" data-testid="replay-rule" placeholder="rule-v2" required />
        </label>
        <label class="filter-item">
          <span>每页条数</span>
          <input v-model.number="replayForm.pageSize" data-testid="replay-page-size" type="number" min="1" />
        </label>
        <label class="filter-item">
          <span>本次最多页数(留空=跑完)</span>
          <input v-model="replayForm.maxPages" data-testid="replay-max-pages" placeholder="留空一次跑完，填1模拟中断" />
        </label>
        <button class="btn primary" data-testid="replay-submit" type="submit">发起重放</button>
      </form>
      <div v-if="activeReplay" class="tl-summary">
        <span class="stat-card">
          <span class="stat-label">重放状态</span>
          <strong class="stat-value">{{ activeReplay.status }}</strong>
        </span>
        <span class="stat-card">
          <span class="stat-label">断点/总量</span>
          <strong class="stat-value">{{ activeReplay.next_cursor }} / {{ activeReplay.scope_raw_ids.length }}</strong>
        </span>
        <button v-if="activeReplay.interrupted" class="btn primary" data-testid="replay-recover" type="button" @click="submitRecover(activeReplay.id)">
          中断恢复（按检索时区快照续跑）
        </button>
      </div>

      <h4>重放记录（与检索关联）</h4>
      <table class="data-table">
        <thead>
          <tr><th>重放ID</th><th>检索ID</th><th>清单ID</th><th>规则版本</th><th>状态</th><th>产出数</th><th>操作</th></tr>
        </thead>
        <tbody>
          <tr v-for="r in replays" :key="r.id">
            <td>{{ r.id }}</td>
            <td>{{ r.search_id }}</td>
            <td>{{ r.checklist_id }}</td>
            <td>{{ r.新解析规则版本 }}</td>
            <td>{{ r.status }}</td>
            <td>{{ r.产出记录数 }}</td>
            <td class="row-actions">
              <button class="link" type="button" @click="loadRevisions(r.id)">查看修订明细</button>
              <button v-if="r.interrupted" class="link" type="button" @click="submitRecover(r.id)">恢复</button>
            </td>
          </tr>
          <tr v-if="!replays.length">
            <td colspan="7" class="empty-state">暂无重放记录</td>
          </tr>
        </tbody>
      </table>

      <h4 v-if="revisions.length">修订明细（原日期 / 时区依据 / 日志时间原文）</h4>
      <table v-if="revisions.length" class="data-table">
        <thead>
          <tr><th>原始记录</th><th>来源</th><th>原日期</th><th>时区依据</th><th>日志时间原文</th><th>事件瞬时(UTC)</th><th>重放正文</th></tr>
        </thead>
        <tbody>
          <tr v-for="item in revisions" :key="String(item.id)">
            <td>{{ item.raw_record_id }}</td>
            <td>{{ item.source_id }}</td>
            <td>{{ item.原日期 }}</td>
            <td>{{ item.时区依据 }} {{ item.timezone }}</td>
            <td>{{ item.日志时间原文 }}</td>
            <td>{{ item.event_instant_utc || '—' }}</td>
            <td class="tl-body">{{ item.原始正文 }}</td>
          </tr>
        </tbody>
      </table>
    </article>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  getChecklist,
  listReplays,
  listRevisions,
  listSourceTimezones,
  recoverReplay,
  registerSourceTimezone,
  searchEventWindow,
  startReplay,
} from '@/api/event-timeline'
import type { ReplayEntry, SearchResult } from '@/api/event-timeline'

const timezones = ref<Record<string, any>[]>([])
const tzForm = ref({ sourceId: '', timezone: '', basis: '' })
const searchForm = ref({
  startAt: '2026-11-20T15:00:00Z',
  endAt: '2026-11-20T17:00:00Z',
  sources: '',
})
const lastSearch = ref<SearchResult | null>(null)
const replayForm = ref({ searchId: 0, rule: 'rule-v2', pageSize: 10, maxPages: '' })
const replays = ref<ReplayEntry[]>([])
const activeReplay = ref<ReplayEntry | null>(null)
const revisions = ref<Record<string, unknown>[]>([])
const notice = ref('')

async function loadTimezones() {
  const data = await listSourceTimezones()
  timezones.value = data.items
}

async function submitTimezone() {
  notice.value = ''
  try {
    const result = await registerSourceTimezone(
      tzForm.value.sourceId.trim(),
      tzForm.value.timezone.trim(),
      tzForm.value.basis.trim(),
    )
    if (!result.ok) throw new Error(result.message)
    await loadTimezones()
  } catch (error) {
    notice.value = error instanceof Error ? error.message : '时区登记失败'
  }
}

async function submitSearch() {
  notice.value = ''
  try {
    const sourceIds = searchForm.value.sources
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean)
    const result = await searchEventWindow({
      start_at: searchForm.value.startAt.trim(),
      end_at: searchForm.value.endAt.trim(),
      source_ids: sourceIds,
    })
    lastSearch.value = result
    replayForm.value.searchId = result.search_id
    await loadReplays()
  } catch (error) {
    notice.value = error instanceof Error ? error.message : '跨日检索失败'
  }
}

async function loadReplays() {
  const data = await listReplays()
  replays.value = data.items
}

async function submitReplay() {
  notice.value = ''
  try {
    const maxPages = replayForm.value.maxPages.trim()
      ? Number(replayForm.value.maxPages)
      : null
    const result = await startReplay(
      replayForm.value.searchId,
      replayForm.value.rule.trim(),
      { page_size: replayForm.value.pageSize, max_pages: maxPages },
    )
    activeReplay.value = result.replay
    await loadReplays()
    if (lastSearch.value) {
      const checklist = await getChecklist(lastSearch.value.checklist_id)
      lastSearch.value.checklist = checklist.checklist
    }
  } catch (error) {
    notice.value = error instanceof Error ? error.message : '发起重放失败'
  }
}

async function submitRecover(replayId: number) {
  notice.value = ''
  try {
    const result = await recoverReplay(replayId)
    activeReplay.value = result.replay
    if (result.time_drift.length) {
      notice.value = `恢复时发现 ${result.time_drift.length} 项来源时区变更，已按检索时快照解释续跑`
    }
    await loadReplays()
    await loadRevisions(replayId)
  } catch (error) {
    notice.value = error instanceof Error ? error.message : '重放恢复失败'
  }
}

async function loadRevisions(replayId: number) {
  const data = await listRevisions(replayId)
  revisions.value = data.items
}

onMounted(() => {
  void loadTimezones()
  void loadReplays()
})
</script>

<style scoped>
.tl-panel {
  background: var(--card-bg, #fff);
  border: 1px solid var(--border-color, #e3e6eb);
  border-radius: 10px;
  padding: 16px;
  margin-bottom: 20px;
}
.tl-panel h3 {
  margin: 0 0 12px;
}
.tl-panel h4 {
  margin: 16px 0 8px;
}
.tl-summary {
  display: flex;
  gap: 12px;
  align-items: center;
  margin: 12px 0;
  flex-wrap: wrap;
}
.tl-body {
  max-width: 320px;
  word-break: break-all;
  font-size: 12px;
}
</style>
