"""测试回收凭证：中断重试、跨表归属、留存文件、待对账与补证核销。"""
from __future__ import annotations

import pytest

from app.lake_store import lake_store
from app.services.reclaim_voucher import (
    STATE_DELETED_WAIT_RECEIPT,
    STATE_PENDING,
    STATE_RECONCILING,
    STATE_RELEASED,
    STATE_RETAINED,
    ReclaimVoucherService,
)


@pytest.fixture
def svc() -> ReclaimVoucherService:
    return ReclaimVoucherService()


def _state_of(result: dict, file_id: str) -> str:
    for item in result["items"]:
        if item["file_id"] == file_id:
            return item["state"]
    raise AssertionError(f"{file_id} 不在结果中")


def _make_file(table: str, partition: str, file_bytes: int, file_id: str) -> str:
    """注册真实文件，并用固定 file_id 覆盖注册表键，便于按业务名断言。"""
    lake_store.register_file(table, partition, rows=1, bytes_=file_bytes)
    files = lake_store.list_files(table, partition)
    pf = files[-1]
    table_files = lake_store._files[table]
    del table_files[pf.file_id]
    pf.file_id = file_id
    table_files[file_id] = pf
    return file_id


class TestInterruptedReclaimRetry:
    def test_retry_only_deletes_remaining_file_and_reconciles_140_bytes(self, svc):
        table = "test_reclaim_retry"
        reclaim_id = "RCL-RETRY-001"
        _make_file(table, "20260801", 100, "F")
        _make_file(table, "20260801", 40, "G")
        created = svc.create_reclaim_task(
            table, reclaim_id,
            [{"file_id": "F", "file_bytes": 100},
             {"file_id": "G", "file_bytes": 40}],
            partition_key="20260801", snapshot_id="snap-retry-001",
        )
        assert created["voucher_count"] == 2
        assert created["target_bytes"] == 140

        # 第一轮：删掉 F 后回收中断，G 尚未删除。
        first = svc.attempt_reclaim(table, reclaim_id, interrupt_after=1)
        assert first["interrupted"] is True
        assert [item["file_id"] for item in first["newly_deleted"]] == ["F"]
        assert _state_of(first, "F") == STATE_DELETED_WAIT_RECEIPT
        assert _state_of(first, "G") == STATE_PENDING
        assert lake_store.get_file(table, "F") is None
        assert lake_store.get_file(table, "G") is not None

        # 中断后恢复：下一轮只应删除 G，绝不重复删除已登记删除事实的 F。
        second = svc.attempt_reclaim(table, reclaim_id)
        assert [item["file_id"] for item in second["newly_deleted"]] == ["G"]
        assert _state_of(second, "F") == STATE_DELETED_WAIT_RECEIPT
        assert _state_of(second, "G") == STATE_DELETED_WAIT_RECEIPT
        assert lake_store.get_file(table, "G") is None

        # 审计回执迟到：F 的原始确认此时才到。
        confirmed_f = svc.record_audit_confirmation(
            table, reclaim_id, "F", 100, operator_id="audit_01")
        assert confirmed_f["released"] is True
        assert confirmed_f["file_bytes"] == 100
        confirmed_g = svc.record_audit_confirmation(
            table, reclaim_id, "G", 40, operator_id="audit_01")
        assert confirmed_g["released"] is True

        recon = svc.reconcile(table, reclaim_id)
        assert recon["target_bytes"] == 140
        assert recon["released_bytes"] == 140
        assert recon["all_reconciled"] is True
        facts = {item["file_id"]: item for item in recon["deletion_facts"]}
        assert set(facts) == {"F", "G"}
        assert facts["F"]["删除事实"]
        assert facts["G"]["删除事实"]
        assert facts["F"]["审计条目"] == confirmed_f["audit_entry"]
        assert facts["G"]["审计条目"] == confirmed_g["audit_entry"]

    def test_late_receipt_arriving_before_retry_is_absorbed(self, svc):
        table = "test_reclaim_late_receipt"
        reclaim_id = "RCL-RETRY-002"
        _make_file(table, "20260802", 100, "F2")
        svc.create_reclaim_task(
            table, reclaim_id, [{"file_id": "F2", "file_bytes": 100}],
            partition_key="20260802", snapshot_id="snap-late")
        first = svc.attempt_reclaim(table, reclaim_id, interrupt_after=1)
        assert _state_of(first, "F2") == STATE_DELETED_WAIT_RECEIPT

        confirmed = svc.record_audit_confirmation(
            table, reclaim_id, "F2", 100, operator_id="audit_02")
        assert confirmed["released"] is True

        retry = svc.attempt_reclaim(table, reclaim_id)
        assert retry["newly_deleted"] == []
        assert _state_of(retry, "F2") == STATE_RELEASED
        assert retry["receipt"]["confirmed_bytes"] == 100


class TestCrossTableAttribution:
    def test_other_table_cannot_use_this_tables_voucher(self, svc):
        owner = "test_reclaim_owner"
        other = "test_reclaim_other"
        reclaim_id = "RCL-XTAB-001"
        _make_file(owner, "20260803", 100, "SAME")
        _make_file(other, "20260803", 999, "SAME")
        svc.create_reclaim_task(
            owner, reclaim_id, [{"file_id": "SAME", "file_bytes": 100}],
            partition_key="20260803", snapshot_id="snap-xtab")

        # 另一张湖表用同名文件去核销：没有本表本回收号的凭证，必须拒绝。
        wrong = svc.record_audit_confirmation(
            other, reclaim_id, "SAME", 100)
        assert wrong["ok"] is False
        assert wrong["released"] is False

        # 另一张湖表自己的回收号也不能核销本表的凭据。
        wrong_id = svc.record_audit_confirmation(
            owner, "RCL-OTHER-9", "SAME", 100)
        assert wrong_id["ok"] is False

        # 只有本表本回收号才能核销。
        lake_store.remove_files(owner, ["SAME"])
        right = svc.record_audit_confirmation(
            owner, reclaim_id, "SAME", 100)
        assert right["ok"] is True
        assert right["released"] is True

        assert svc.reconcile(other, reclaim_id)["ok"] is False
        # 另一张湖表的同名文件仍在存储里，未被误删。
        assert lake_store.get_file(other, "SAME") is not None


class TestRetainedFiles:
    def test_snapshot_referenced_file_is_retained_and_skipped(self, svc):
        table = "test_reclaim_retained"
        reclaim_id = "RCL-RET-001"
        _make_file(table, "20260804", 100, "KEEP")
        _make_file(table, "20260804", 40, "DROP")
        snap = lake_store.create_snapshot(table, ["KEEP"], added_rows=1)
        lake_store.mark_snapshot_available(table, snap.snapshot_id)

        svc.create_reclaim_task(
            table, reclaim_id,
            [{"file_id": "KEEP", "file_bytes": 100},
             {"file_id": "DROP", "file_bytes": 40}],
            partition_key="20260804", snapshot_id=snap.snapshot_id,
        )
        view = svc.voucher_view(table, reclaim_id)
        retained_ids = {item["文件标识"] for item in view["retained"]}
        assert "KEEP" in retained_ids
        group = view["groups"][0]
        assert group["retained_bytes"] == 100
        assert any(item["file_id"] == "KEEP" and item["state"] == STATE_RETAINED
                   for item in group["items"])

        attempt = svc.attempt_reclaim(table, reclaim_id)
        assert [item["file_id"] for item in attempt["newly_deleted"]] == ["DROP"]
        assert _state_of(attempt, "KEEP") == STATE_RETAINED
        assert lake_store.get_file(table, "KEEP") is not None
        assert attempt["retained_bytes"] == 100

        # 下一次回收的清单仍把留存文件列出来，不当成已释放。
        nxt = svc.reclaim_next_actions(table, reclaim_id)
        assert [item["file_id"] for item in nxt["retained"]] == ["KEEP"]
        assert "KEEP" not in [item["file_id"] for item in nxt["already_released"]]

    def test_audit_confirmation_cannot_release_retained_file(self, svc):
        table = "test_reclaim_retained_confirm"
        reclaim_id = "RCL-RET-002"
        _make_file(table, "20260805", 100, "KEEP2")
        snap = lake_store.create_snapshot(table, ["KEEP2"])
        lake_store.mark_snapshot_available(table, snap.snapshot_id)
        svc.create_reclaim_task(
            table, reclaim_id, [{"file_id": "KEEP2", "file_bytes": 100}],
            partition_key="20260805", snapshot_id=snap.snapshot_id)
        denied = svc.record_audit_confirmation(table, reclaim_id, "KEEP2", 100)
        assert denied["ok"] is False
        assert denied["released"] is False

    def test_live_snapshot_other_than_voucher_snapshot_still_protects(self, svc):
        table = "test_reclaim_retained_other_snap"
        reclaim_id = "RCL-RET-003"
        _make_file(table, "20260805", 90, "KEEP3")
        expired = lake_store.create_snapshot(table, ["KEEP3"])
        lake_store.mark_snapshot_available(table, expired.snapshot_id)
        lake_store.mark_snapshot_expired(table, expired.snapshot_id)
        live = lake_store.create_snapshot(table, ["KEEP3"])
        lake_store.mark_snapshot_available(table, live.snapshot_id)

        svc.create_reclaim_task(
            table, reclaim_id, [{"file_id": "KEEP3", "file_bytes": 90}],
            partition_key="20260805", snapshot_id=expired.snapshot_id)
        attempt = svc.attempt_reclaim(table, reclaim_id)
        assert _state_of(attempt, "KEEP3") == STATE_RETAINED
        assert lake_store.get_file(table, "KEEP3") is not None


class TestPendingReconciliation:
    def test_missing_file_without_attribution_goes_pending(self, svc):
        table = "test_reclaim_pending"
        reclaim_id = "RCL-PEND-001"
        _make_file(table, "20260806", 100, "F3")
        _make_file(table, "20260806", 40, "G3")
        svc.create_reclaim_task(
            table, reclaim_id,
            [{"file_id": "F3", "file_bytes": 100},
             {"file_id": "G3", "file_bytes": 40}],
            partition_key="20260806", snapshot_id="snap-pend")

        # 模拟重启后从存储发现 F3 已消失，但可能是别的回收操作删的。
        lake_store.remove_files(table, ["F3"])
        marked = svc.detect_missing_files(table, reclaim_id)
        assert [item["file_id"] for item in marked] == ["F3"]
        assert marked[0]["state"] == STATE_RECONCILING

        # F3 待对账时尝试回收：G3 可独立确认 40 字节，F3 未获归属不计入回执。
        attempt = svc.attempt_reclaim(table, reclaim_id)
        assert [item["file_id"] for item in attempt["newly_deleted"]] == ["G3"]
        assert _state_of(attempt, "F3") == STATE_RECONCILING
        assert attempt["requires_supplementary_evidence"] is True
        assert attempt["can_continue"] is False
        assert attempt["waiting_for_evidence_from"] == ["F3"]
        # 只有本轮确实删除的 G3 有删除事实；F3 未获回收归属，不进回执。
        assert attempt["receipt"]["attributed_bytes"] == 40
        assert [item["file_id"] for item in attempt["receipt"]["excluded_from_receipt"]] == ["F3"]

        confirmed_g = svc.record_audit_confirmation(
            table, reclaim_id, "G3", 40)
        assert confirmed_g["released"] is True

        # 再尝试时 F3 仍未补证：G3 已独立确认 40 字节，但整体仍要等 F3。
        waiting = svc.attempt_reclaim(table, reclaim_id)
        assert waiting["independent_confirmed_bytes"] == 40
        assert [item["file_id"] for item in waiting["independent_confirmations"]] == ["G3"]
        assert _state_of(waiting, "F3") == STATE_RECONCILING
        assert waiting["receipt"]["confirmed_bytes"] == 40
        # 执行回执只含已获归属的 G3 的 40 字节，F3 仍未计入。
        assert waiting["receipt"]["attributed_bytes"] == 40
        assert [item["file_id"] for item in waiting["receipt"]["excluded_from_receipt"]] == ["F3"]
        assert "40" in waiting["operation_record"]
        assert "F3" in waiting["operation_record"]
        assert "补证" in waiting["operation_record"]

        # 每次尝试都写进审计链，值班员可在操作记录里回看。
        from app.store import store
        attempts = [
            row for row in store.rows("lake_audit")
            if row.get("_kind") == "reclaim_attempt"
            and row.get("_reclaim_id") == reclaim_id
        ]
        assert attempts
        assert any("F3" in str(row.get("变更后值")) for row in attempts)        # 未补证前对账不能算完成，F3 也不进已释放字节。
        before = svc.reconcile(table, reclaim_id)
        assert before["released_bytes"] == 40
        assert before["pending_reconciliation_bytes"] == 100
        assert before["all_reconciled"] is False

        # 找到与本回收号关联的原始确认后，F3 才转已释放并纳入 140 字节。
        confirmed_f = svc.record_audit_confirmation(
            table, reclaim_id, "F3", 100,
            deletion_fact="2026-11-22T05:00:00")
        assert confirmed_f["released"] is True
        after = svc.reconcile(table, reclaim_id)
        assert after["released_bytes"] == 140
        assert after["all_reconciled"] is True
        assert after["pending_reconciliation"] == []

    def test_pending_item_never_enters_receipt_before_evidence(self, svc):
        table = "test_reclaim_pending_receipt"
        reclaim_id = "RCL-PEND-002"
        _make_file(table, "20260807", 100, "F4")
        svc.create_reclaim_task(
            table, reclaim_id, [{"file_id": "F4", "file_bytes": 100}],
            partition_key="20260807", snapshot_id="snap-pend2")
        lake_store.remove_files(table, ["F4"])

        attempt = svc.attempt_reclaim(table, reclaim_id)
        assert attempt["ok"] is True
        assert attempt["newly_deleted"] == []
        assert attempt["receipt"]["attributed_bytes"] == 0
        assert attempt["receipt"]["confirmed_bytes"] == 0
        assert attempt["released_file_ids"] == []
        assert _state_of(attempt, "F4") == STATE_RECONCILING

        # 别的湖表的确认不能补这张表的证。
        other = svc.record_audit_confirmation(
            "test_reclaim_pending_receipt_other", reclaim_id, "F4", 100)
        assert other["ok"] is False
        assert svc.reconcile(table, reclaim_id)["released_bytes"] == 0

    def test_manual_mark_pending_is_table_scoped(self, svc):
        table = "test_reclaim_manual_pending"
        reclaim_id = "RCL-PEND-003"
        _make_file(table, "20260808", 100, "F5")
        svc.create_reclaim_task(
            table, reclaim_id, [{"file_id": "F5", "file_bytes": 100}],
            partition_key="20260808", snapshot_id="snap-pend3")
        missing = svc.mark_pending_reconciliation(
            "another_table", reclaim_id, "F5")
        assert missing["ok"] is False
        marked = svc.mark_pending_reconciliation(table, reclaim_id, "F5")
        assert marked["ok"] is True
        assert marked["state"] == STATE_RECONCILING


class TestVoucherView:
    def test_groups_by_table_partition_snapshot(self, svc):
        table = "test_reclaim_view"
        _make_file(table, "20260809", 100, "V1")
        _make_file(table, "20260809", 40, "V2")
        lake_store.remove_files(table, ["V2"])
        svc.create_reclaim_task(
            table, "RCL-VIEW-001",
            [{"file_id": "V1", "file_bytes": 100},
             {"file_id": "V2", "file_bytes": 40}],
            partition_key="20260809", snapshot_id="snap-view")
        svc.attempt_reclaim(table, "RCL-VIEW-001", interrupt_after=1)
        svc.record_audit_confirmation(table, "RCL-VIEW-001", "V1", 100)
        # 中断后重试：V2 在存储里已不存在，只能先标待对账。
        retry = svc.attempt_reclaim(table, "RCL-VIEW-001")
        assert _state_of(retry, "V2") == STATE_RECONCILING

        view = svc.voucher_view(table)
        assert view["summary"]["voucher_count"] == 2
        assert view["summary"]["released_bytes"] == 100
        assert view["summary"]["pending_reconciliation_bytes"] == 40
        group = view["groups"][0]
        assert group["lake_table"] == table
        assert group["partition_key"] == "20260809"
        assert group["snapshot_id"] == "snap-view"
        assert group["target_bytes"] == 140
        assert {fact["file_id"] for fact in group["deletion_facts"]} == {"V1"}
        reclaim = next(r for r in view["reclaims"] if r["reclaim_id"] == "RCL-VIEW-001")
        assert reclaim["released_files"] == ["V1"]
        assert reclaim["pending_files"] == ["V2"]

    def test_audit_facts_link_to_deletion_facts(self, svc):
        table = "test_reclaim_audit_facts"
        reclaim_id = "RCL-VIEW-002"
        _make_file(table, "20260810", 100, "V3")
        svc.create_reclaim_task(
            table, reclaim_id, [{"file_id": "V3", "file_bytes": 100}],
            partition_key="20260810", snapshot_id="snap-facts")
        svc.attempt_reclaim(table, reclaim_id)
        confirmation = svc.record_audit_confirmation(
            table, reclaim_id, "V3", 100, operator_id="audit_03")

        facts = svc.audit_facts(table, reclaim_id)
        assert len(facts) == 1
        fact = facts[0]
        assert fact["审计条目"] == confirmation["audit_entry"]
        assert fact["文件标识"] == "V3"
        assert fact["文件字节数"] == 100
        assert fact["删除事实"]
        assert fact["回收号"] == reclaim_id
        assert fact["凭证编号"] == "RV-0001" or fact["凭证编号"].startswith("RV-")

        # 审计详情页的条目本身也能看到回收确认与删除事实。
        from app.store import store
        entry = next(
            row for row in store.rows("lake_audit")
            if row.get("条目编号") == confirmation["audit_entry"])
        assert entry["操作类型"] == "回收确认"
        assert entry["_reclaim_id"] == reclaim_id
        assert entry["_deletion_fact"]

    def test_audit_detail_links_confirmation_to_deletion_fact(self, svc):
        from app.services.lake_audit import LakeAuditService
        from app.store import store

        table = "test_reclaim_audit_detail"
        reclaim_id = "RCL-VIEW-004"
        _make_file(table, "20260812", 100, "V4")
        svc.create_reclaim_task(
            table, reclaim_id, [{"file_id": "V4", "file_bytes": 100}],
            partition_key="20260812", snapshot_id="snap-detail")
        svc.attempt_reclaim(table, reclaim_id)
        confirmation = svc.record_audit_confirmation(
            table, reclaim_id, "V4", 100)

        entry = next(
            row for row in store.rows("lake_audit")
            if row.get("条目编号") == confirmation["audit_entry"])
        detail = LakeAuditService().audit_entry_detail(int(entry["id"]))
        assert detail is not None
        voucher = detail["reclaim_voucher"]
        assert voucher["reclaim_id"] == reclaim_id
        assert voucher["file_id"] == "V4"
        assert voucher["file_bytes"] == 100
        assert voucher["deletion_fact"]
        assert voucher["state"] == STATE_RELEASED
        assert voucher["voucher_code"] == entry["_voucher_id"]

    def test_next_actions_identify_already_released(self, svc):
        table = "test_reclaim_next"
        reclaim_id = "RCL-VIEW-003"
        _make_file(table, "20260811", 100, "N1")
        _make_file(table, "20260811", 40, "N2")
        svc.create_reclaim_task(
            table, reclaim_id,
            [{"file_id": "N1", "file_bytes": 100},
             {"file_id": "N2", "file_bytes": 40}],
            partition_key="20260811", snapshot_id="snap-next")
        svc.attempt_reclaim(table, reclaim_id)
        svc.record_audit_confirmation(table, reclaim_id, "N1", 100)

        nxt = svc.reclaim_next_actions(table, reclaim_id)
        assert [item["file_id"] for item in nxt["already_released"]] == ["N1"]
        assert {item["file_id"] for item in nxt["continue"]} == {"N2"}
