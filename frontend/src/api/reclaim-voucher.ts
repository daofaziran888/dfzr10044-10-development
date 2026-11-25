/** 回收凭证：按湖表 / 分区 / 快照汇总的删除明细，供重试与对账直接使用。 */
import { fetchJson, request } from './client'

export type VoucherItem = {
  id: number
  voucher_code: string
  reclaim_id: string
  lake_table: string
  partition_key: string
  snapshot_id: string
  file_id: string
  file_bytes: number
  state: string
  deletion_fact: string
  audit_entry: string
  attributed: boolean
  attributed_bytes: number
  confirmed: boolean
  confirmed_bytes: number
  independent_confirmation: boolean
  reason: string
}

export type DeletionFact = {
  file_id: string
  file_bytes: number
  删除事实: string
  审计条目: string
  对账状态: string
}

export type VoucherGroup = {
  lake_table: string
  partition_key: string
  snapshot_id: string
  target_bytes: number
  released_bytes: number
  pending_reconciliation_bytes: number
  retained_bytes: number
  items: VoucherItem[]
  deletion_facts: DeletionFact[]
  retained_files: { file_id: string; file_bytes: number }[]
}

export type RetainedFile = {
  文件标识: string
  所属湖表: string
  分区标识: string
  文件字节数: number
  引用快照: string
  对账状态: string
  说明: string
}

export type VoucherView = {
  ok: boolean
  lake_table: string
  reclaim_id: string
  summary: {
    voucher_count: number
    target_bytes: number
    released_bytes: number
    pending_reconciliation_bytes: number
    retained_bytes: number
  }
  groups: VoucherGroup[]
  reclaims: {
    lake_table: string
    reclaim_id: string
    voucher_count: number
    target_bytes: number
    released_bytes: number
    pending_reconciliation_bytes: number
    retained_bytes: number
    released_files: string[]
    pending_files: string[]
    retained_files: string[]
  }[]
  retained: RetainedFile[]
  audit_facts: AuditFact[]
  attempt_records: {
    审计条目: string
    所属湖表: string
    回收号: string
    操作记录: string
    操作者标识: string
    操作时间: string
  }[]
}

export type AuditFact = {
  审计条目: string
  所属湖表: string
  操作类型: string
  文件标识: string
  文件字节数: number
  删除事实: string
  回收号: string
  凭证编号: string
  分区标识: string
  快照标识: string
  核销结论: string
  操作者标识: string
  操作时间: string
}

export type ReclaimAttempt = {
  ok: boolean
  lake_table: string
  reclaim_id: string
  interrupted: boolean
  newly_deleted: { file_id: string; bytes: number }[]
  skipped: { file_id: string; bytes: number; state: string; reason: string }[]
  items: VoucherItem[]
  receipt: {
    attributed_bytes: number
    confirmed_bytes: number
    excluded_from_receipt: { file_id: string; file_bytes: number; reason: string }[]
  }
  released_file_ids: string[]
  independent_confirmations: { file_id: string; file_bytes: number; confirmed: boolean }[]
  independent_confirmed_bytes: number
  pending_reconciliation: VoucherItem[]
  pending_reconciliation_bytes: number
  retained_files: VoucherItem[]
  retained_bytes: number
  waiting_for_evidence_from: string[]
  requires_supplementary_evidence: boolean
  can_continue: boolean
  completion: string
  operation_record: string
  message?: string
}

export type AuditDetail = {
  entry: Record<string, unknown>
  reclaim_voucher: {
    lake_table: string
    reclaim_id: string
    file_id: string
    file_bytes: number
    deletion_fact: string
    voucher_code: string
    state: string
    partition_key: string
    snapshot_id: string
    deletion_facts: AuditFact[]
  } | null
}

export async function loadAuditDetail(entryId: number): Promise<AuditDetail> {
  return fetchJson<AuditDetail>(`/api/reclaim-voucher/audit-detail/${entryId}`)
}

export async function loadVoucherView(params: {
  lake_table?: string
  reclaim_id?: string
}): Promise<VoucherView> {
  const query = new URLSearchParams()
  if (params.lake_table) query.set('lake_table', params.lake_table)
  if (params.reclaim_id) query.set('reclaim_id', params.reclaim_id)
  return fetchJson<VoucherView>(`/api/reclaim-voucher/view?${query.toString()}`)
}

export async function attemptReclaim(payload: {
  lake_table: string
  reclaim_id: string
  interrupt_after?: number | null
}): Promise<ReclaimAttempt> {
  const response = await request('/api/reclaim-voucher/attempt', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new Error('回收尝试未执行')
  return (await response.json()) as ReclaimAttempt
}

export async function confirmDeletion(payload: {
  lake_table: string
  reclaim_id: string
  file_id: string
  file_bytes?: number
  operator_id?: string
}): Promise<{ ok: boolean; released: boolean; message: string }> {
  const response = await request('/api/reclaim-voucher/confirm', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new Error('审计回执核销未执行')
  return await response.json()
}

export async function reconcileReclaim(payload: {
  lake_table: string
  reclaim_id: string
}): Promise<{
  ok: boolean
  target_bytes: number
  released_bytes: number
  pending_reconciliation_bytes: number
  retained_bytes: number
  all_reconciled: boolean
  pending_reconciliation: VoucherItem[]
  deletion_facts: DeletionFact[]
  message?: string
}> {
  const response = await request('/api/reclaim-voucher/reconcile', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new Error('回收对账未执行')
  return await response.json()
}

export async function markPending(payload: {
  lake_table: string
  reclaim_id: string
  file_id: string
  reason?: string
}): Promise<{ ok: boolean; state: string; message: string }> {
  const response = await request('/api/reclaim-voucher/mark-pending', {
    method: 'POST',
    body: JSON.stringify(payload),
  })
  if (!response.ok) throw new Error('待对账标记未执行')
  return await response.json()
}
