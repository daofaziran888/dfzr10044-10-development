<template>
  <section class="page" data-module="reclaim_voucher">
    <header class="page-head">
      <div>
        <h2>回收凭证</h2>
        <p class="page-desc">
          按湖表、分区和快照汇总的删除明细。重试回收时按这份明细判断哪些文件应继续回收，
          只有拿到本回收号删除事实或原始确认的文件才计入已释放字节。
        </p>
      </div>
      <form class="filter-bar" @submit.prevent="reload">
        <label class="filter-item">
          <span>所属湖表</span>
          <input v-model="lakeTable" data-testid="filter-lake-table" placeholder="全部湖表" />
        </label>
        <label class="filter-item">
          <span>回收号</span>
          <input v-model="reclaimId" data-testid="filter-reclaim-id" placeholder="全部回收号" />
        </label>
        <button class="btn" data-testid="query" type="submit">查询</button>
        <button class="btn ghost" type="button" @click="reset">重置条件</button>
      </form>
    </header>

    <div class="stat-row">
      <article class="stat-card">
        <span class="stat-label">凭证数</span>
        <strong class="stat-value">{{ view?.summary.voucher_count ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">目标字节</span>
        <strong class="stat-value">{{ view?.summary.target_bytes ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">已释放字节</span>
        <strong class="stat-value">{{ view?.summary.released_bytes ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">待对账字节</span>
        <strong class="stat-value">{{ view?.summary.pending_reconciliation_bytes ?? 0 }}</strong>
      </article>
      <article class="stat-card">
        <span class="stat-label">留存字节</span>
        <strong class="stat-value">{{ view?.summary.retained_bytes ?? 0 }}</strong>
      </article>
    </div>

    <p v-if="errorMessage" class="error-text" data-testid="error">{{ errorMessage }}</p>

    <section class="panel" data-testid="reclaim-tasks">
      <h3>回收任务</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>湖表</th><th>回收号</th><th>凭证数</th><th>目标字节</th>
            <th>已释放</th><th>待对账</th><th>留存</th><th>已释放文件</th>
            <th>待对账文件</th><th>操作</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="task in view?.reclaims ?? []" :key="`${task.lake_table}-${task.reclaim_id}`">
            <td>{{ task.lake_table }}</td>
            <td>{{ task.reclaim_id }}</td>
            <td>{{ task.voucher_count }}</td>
            <td>{{ task.target_bytes }}</td>
            <td>{{ task.released_bytes }}</td>
            <td>{{ task.pending_reconciliation_bytes }}</td>
            <td>{{ task.retained_bytes }}</td>
            <td>{{ task.released_files.join('、') || '—' }}</td>
            <td>{{ task.pending_files.join('、') || '—' }}</td>
            <td class="row-actions">
              <button
                class="link"
                type="button"
                :data-testid="`attempt-${task.reclaim_id}`"
                @click="attempt(task.lake_table, task.reclaim_id)"
              >重试回收</button>
              <button
                class="link"
                type="button"
                :data-testid="`reconcile-${task.reclaim_id}`"
                @click="reconcile(task.lake_table, task.reclaim_id)"
              >对账</button>
            </td>
          </tr>
          <tr v-if="!view?.reclaims.length">
            <td colspan="10" class="empty-state">暂无回收凭证</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section v-if="attemptResult" class="panel" data-testid="attempt-result">
      <h3>回收尝试回执</h3>
      <p><strong>操作记录：</strong><span data-testid="operation-record">{{ attemptResult.operation_record }}</span></p>
      <p>
        执行回执归属字节：<strong data-testid="attributed-bytes">{{ attemptResult.receipt.attributed_bytes }}</strong>；
        已独立确认字节：<strong data-testid="confirmed-bytes">{{ attemptResult.receipt.confirmed_bytes }}</strong>；
        <span v-if="attemptResult.requires_supplementary_evidence" class="warn-text">
          需等待 {{ attemptResult.waiting_for_evidence_from.join('、') }} 补证
        </span>
        <span v-else-if="attemptResult.can_continue" class="ok-text">可继续</span>
      </p>
      <p v-if="attemptResult.receipt.excluded_from_receipt.length" class="warn-text">
        未获得回收归属，不计入执行回执：
        {{ attemptResult.receipt.excluded_from_receipt.map((i) => i.file_id).join('、') }}
      </p>
    </section>

    <section class="panel" data-testid="voucher-groups">
      <h3>删除明细（按湖表 / 分区 / 快照）</h3>
      <div v-for="group in view?.groups ?? []" :key="`${group.lake_table}-${group.partition_key}-${group.snapshot_id}`" class="voucher-group">
        <p class="group-head">
          <span>湖表：{{ group.lake_table }}</span>
          <span>分区：{{ group.partition_key || '—' }}</span>
          <span>快照：{{ group.snapshot_id || '—' }}</span>
          <span>目标：{{ group.target_bytes }} 字节</span>
          <span>已释放：{{ group.released_bytes }}</span>
          <span>待对账：{{ group.pending_reconciliation_bytes }}</span>
          <span>留存：{{ group.retained_bytes }}</span>
        </p>
        <table class="data-table">
          <thead>
            <tr>
              <th>凭证编号</th><th>回收号</th><th>文件标识</th><th>字节数</th>
              <th>删除事实</th><th>审计条目</th><th>对账状态</th><th>说明</th><th>操作</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="item in group.items"
              :key="item.voucher_code"
              :data-testid="`voucher-${item.file_id}`"
              :data-state="item.state"
            >
              <td>{{ item.voucher_code }}</td>
              <td>{{ item.reclaim_id }}</td>
              <td>{{ item.file_id }}</td>
              <td>{{ item.file_bytes }}</td>
              <td>{{ item.deletion_fact || '—' }}</td>
              <td>{{ item.audit_entry || '—' }}</td>
              <td>{{ item.state }}</td>
              <td>{{ item.reason || '—' }}</td>
              <td class="row-actions">
                <button
                  class="link"
                  type="button"
                  :data-testid="`confirm-${item.file_id}`"
                  @click="confirm(item)"
                >录入原始确认</button>
                <button
                  class="link"
                  type="button"
                  :data-testid="`mark-pending-${item.file_id}`"
                  @click="mark(item)"
                >标待对账</button>
              </td>
            </tr>
          </tbody>
        </table>
        <p v-if="group.retained_files.length" class="retained-note">
          留存文件（仍有快照引用，下次回收也不应释放）：
          {{ group.retained_files.map((f) => `${f.file_id}(${f.file_bytes})`).join('、') }}
        </p>
      </div>
      <p v-if="!view?.groups.length" class="empty-state">暂无删除明细</p>
    </section>

    <section class="panel" data-testid="retained-files">
      <h3>仍有快照引用的留存文件</h3>
      <table class="data-table">
        <thead>
          <tr><th>湖表</th><th>分区</th><th>文件标识</th><th>字节数</th><th>引用快照</th><th>说明</th></tr>
        </thead>
        <tbody>
          <tr v-for="file in view?.retained ?? []" :key="`${file.所属湖表}-${file.文件标识}`">
            <td>{{ file.所属湖表 }}</td>
            <td>{{ file.分区标识 || '—' }}</td>
            <td>{{ file.文件标识 }}</td>
            <td>{{ file.文件字节数 }}</td>
            <td>{{ file.引用快照 }}</td>
            <td>{{ file.说明 }}</td>
          </tr>
          <tr v-if="!view?.retained.length">
            <td colspan="6" class="empty-state">没有留存文件</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section class="panel" data-testid="audit-facts">
      <h3>回收确认关联的审计删除事实</h3>
      <table class="data-table">
        <thead>
          <tr>
            <th>审计条目</th><th>湖表</th><th>回收号</th><th>文件标识</th>
            <th>字节数</th><th>删除事实</th><th>核销结论</th><th>操作时间</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="fact in view?.audit_facts ?? []" :key="fact.审计条目">
            <td>{{ fact.审计条目 }}</td>
            <td>{{ fact.所属湖表 }}</td>
            <td>{{ fact.回收号 }}</td>
            <td>{{ fact.文件标识 }}</td>
            <td>{{ fact.文件字节数 }}</td>
            <td>{{ fact.删除事实 || '—' }}</td>
            <td>{{ fact.核销结论 }}</td>
            <td>{{ fact.操作时间 }}</td>
            <td class="row-actions">
              <button class="link" type="button" :data-testid="`audit-detail-${fact.审计条目}`" @click="openAuditDetail(fact)">看审计详情</button>
            </td>
          </tr>
          <tr v-if="!view?.audit_facts.length">
            <td colspan="9" class="empty-state">暂无回收确认</td>
          </tr>
        </tbody>
      </table>
    </section>

    <section v-if="auditDetail" class="panel" data-testid="audit-detail">
      <h3>审计详情 · {{ String(auditDetail.entry.条目编号) }}</h3>
      <dl class="detail-grid">
        <div><dt>操作类型</dt><dd>{{ auditDetail.entry.操作类型 }}</dd></div>
        <div><dt>操作对象</dt><dd>{{ auditDetail.entry.操作对象 }}</dd></div>
        <div><dt>变更前值</dt><dd>{{ auditDetail.entry.变更前值 }}</dd></div>
        <div><dt>变更后值</dt><dd>{{ auditDetail.entry.变更后值 }}</dd></div>
        <template v-if="auditDetail.reclaim_voucher">
          <div><dt>关联回收号</dt><dd>{{ auditDetail.reclaim_voucher.reclaim_id }}</dd></div>
          <div><dt>回收凭证</dt><dd>{{ auditDetail.reclaim_voucher.voucher_code }}</dd></div>
          <div><dt>删除事实</dt><dd>{{ auditDetail.reclaim_voucher.deletion_fact || '—' }}</dd></div>
          <div><dt>文件 / 字节</dt><dd>{{ auditDetail.reclaim_voucher.file_id }} / {{ auditDetail.reclaim_voucher.file_bytes }}</dd></div>
          <div><dt>当前对账状态</dt><dd>{{ auditDetail.reclaim_voucher.state }}</dd></div>
          <div><dt>分区 / 快照</dt><dd>{{ auditDetail.reclaim_voucher.partition_key || '—' }} / {{ auditDetail.reclaim_voucher.snapshot_id || '—' }}</dd></div>
        </template>
      </dl>
    </section>

    <section class="panel" data-testid="attempt-records">
      <h3>回收尝试操作记录</h3>
      <table class="data-table">
        <thead>
          <tr><th>审计条目</th><th>湖表</th><th>回收号</th><th>操作记录</th><th>操作者</th><th>操作时间</th></tr>
        </thead>
        <tbody>
          <tr v-for="record in view?.attempt_records ?? []" :key="record.审计条目">
            <td>{{ record.审计条目 }}</td>
            <td>{{ record.所属湖表 }}</td>
            <td>{{ record.回收号 }}</td>
            <td>{{ record.操作记录 }}</td>
            <td>{{ record.操作者标识 }}</td>
            <td>{{ record.操作时间 }}</td>
          </tr>
          <tr v-if="!view?.attempt_records.length">
            <td colspan="6" class="empty-state">暂无回收尝试记录</td>
          </tr>
        </tbody>
      </table>
    </section>

    <footer class="page-foot">
      <span v-if="reconcileResult">
        对账：目标 {{ reconcileResult.target_bytes }} 字节，已释放 {{ reconcileResult.released_bytes }} 字节，
        待对账 {{ reconcileResult.pending_reconciliation_bytes }} 字节，
        {{ reconcileResult.all_reconciled ? '已全部对平' : '尚未对平' }}
      </span>
      <span>{{ view?.summary.voucher_count ?? 0 }} 份凭证</span>
    </footer>
  </section>
</template>

<script setup lang="ts">
import { onMounted, ref } from 'vue'

import {
  attemptReclaim,
  confirmDeletion,
  loadAuditDetail,
  loadVoucherView,
  markPending,
  reconcileReclaim,
} from '@/api/reclaim-voucher'
import type { AuditDetail, AuditFact, ReclaimAttempt, VoucherItem, VoucherView } from '@/api/reclaim-voucher'

const lakeTable = ref('')
const reclaimId = ref('')
const view = ref<VoucherView | null>(null)
const attemptResult = ref<ReclaimAttempt | null>(null)
const reconcileResult = ref<Awaited<ReturnType<typeof reconcileReclaim>> | null>(null)
const auditDetail = ref<AuditDetail | null>(null)
const errorMessage = ref('')

async function openAuditDetail(fact: AuditFact) {
  errorMessage.value = ''
  try {
    const entries = await fetchAuditEntryId(fact.审计条目)
    auditDetail.value = await loadAuditDetail(entries)
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '审计详情读取失败'
  }
}

async function fetchAuditEntryId(code: string): Promise<number> {
  const response = await fetch(`/api/lake_audit?keyword=${encodeURIComponent(code)}`)
  if (!response.ok) throw new Error('审计条目检索失败')
  const payload = (await response.json()) as { items: { id: number; 条目编号: string }[] }
  const hit = payload.items.find((item) => item.条目编号 === code)
  if (!hit) throw new Error(`审计条目 ${code} 不存在`)
  return Number(hit.id)
}

function reset() {
  lakeTable.value = ''
  reclaimId.value = ''
  void reload()
}

async function reload() {
  errorMessage.value = ''
  try {
    view.value = await loadVoucherView({
      lake_table: lakeTable.value.trim() || undefined,
      reclaim_id: reclaimId.value.trim() || undefined,
    })
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '凭证视图读取失败'
  }
}

async function attempt(table: string, id: string) {
  errorMessage.value = ''
  try {
    attemptResult.value = await attemptReclaim({ lake_table: table, reclaim_id: id })
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '回收尝试失败'
  }
}

async function confirm(item: VoucherItem) {
  errorMessage.value = ''
  try {
    const result = await confirmDeletion({
      lake_table: item.lake_table,
      reclaim_id: item.reclaim_id,
      file_id: item.file_id,
      file_bytes: item.file_bytes,
    })
    if (!result.ok) throw new Error(result.message)
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '核销失败'
  }
}

async function mark(item: VoucherItem) {
  errorMessage.value = ''
  try {
    await markPending({
      lake_table: item.lake_table,
      reclaim_id: item.reclaim_id,
      file_id: item.file_id,
    })
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '标记待对账失败'
  }
}

async function reconcile(table: string, id: string) {
  errorMessage.value = ''
  try {
    reconcileResult.value = await reconcileReclaim({ lake_table: table, reclaim_id: id })
    await reload()
  } catch (error) {
    errorMessage.value = error instanceof Error ? error.message : '对账失败'
  }
}

onMounted(reload)
</script>

<style scoped>
.panel { background: #fff; border: 1px solid var(--border); border-radius: 8px; padding: 12px 14px; margin-bottom: 14px; }
.panel h3 { margin: 0 0 10px; font-size: 15px; }
.voucher-group { margin-bottom: 14px; }
.group-head { display: flex; flex-wrap: wrap; gap: 14px; font-size: 12px; color: var(--muted); margin: 0 0 6px; }
.retained-note { font-size: 12px; color: #b45309; margin: 6px 0 0; }
.detail-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr)); gap: 10px 16px; margin: 0; }
.detail-grid dt { font-size: 12px; color: var(--muted); }
.detail-grid dd { margin: 2px 0 0; font-size: 13px; }
.warn-text { color: #b45309; }
.ok-text { color: #15803d; }
</style>
