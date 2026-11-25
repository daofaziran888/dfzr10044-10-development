"""湖表审计领域服务：记录结构演进、快照回退、过期清理、分层迁移的变更前后值。"""
from __future__ import annotations

import time
from typing import Any

from app.services.base import BaseModuleService
from app.store import store
from app.lake_store import lake_store


class LakeAuditService(BaseModuleService):
    MODULE = "lake_audit"
    ENTITY = "审计条目"
    OPERATION_TERM = "迁移"
    LIST_FIELDS = ["条目编号", "所属湖表", "操作类型", "操作对象", "变更前值", "变更后值", "操作者标识", "操作时间"]
    REQUIRED_FIELDS = ["条目编号", "所属湖表", "操作类型"]
    STATUS_ORDER = ["已记录", "待核查", "已核查", "已封存"]
    ACTION_RULES = {"提交核查": "待核查", "完成核查": "已核查", "封存条目": "已封存"}
    NEGATIVE_ACTIONS = []

    def record_audit_entry(self, lake_table: str, operation_type: str,
                           target: str, value_before: Any,
                           value_after: Any, operator_id: str) -> dict[str, Any]:
        """记录一条审计条目：变更前后的完整快照。"""
        return {
            "lake_table": lake_table,
            "operation_type": operation_type,
            "target": target,
            "value_before": str(value_before),
            "value_after": str(value_after),
            "operator_id": operator_id,
            "recorded_at": time.time(),
        }

    def verify_audit_chain(self, lake_table: str, operation_type: str) -> list[dict[str, Any]]:
        """验证审计链完整性：列出指定湖表某种操作类型的全部审计条目。"""
        rows = store.rows(self.MODULE)
        chain = [
            r for r in rows
            if str(r.get("所属湖表", "")) == lake_table
               and str(r.get("操作类型", "")) == operation_type
        ]
        chain.sort(key=lambda r: str(r.get("操作时间", "")))
        return chain

    def check_audit_gap(self, lake_table: str,
                        expected_before: str, actual_after: str) -> dict[str, Any]:
        """检查审计缺口：预期变更前值与实际变更后值是否闭合。"""
        gap = expected_before != actual_after
        return {
            "lake_table": lake_table,
            "has_gap": gap,
            "expected_before": expected_before,
            "actual_after": actual_after,
            "severity": "HIGH" if gap else "OK",
        }

    def seal_audit_chain(self, lake_table: str) -> dict[str, Any]:
        """封存审计链：将待核查条目封存为不可变档案。"""
        sealed = 0
        for row in store.rows(self.MODULE):
            if row.get("status") == "待核查" and str(row.get("所属湖表", "")) == lake_table:
                row["status"] = "已封存"
                sealed += 1
        return {
            "lake_table": lake_table,
            "sealed_count": sealed,
            "seal_time": time.time(),
        }

    def audit_entry_detail(self, entry_id: int) -> dict[str, Any] | None:
        """审计详情：回收确认条目回溯到对应回收凭证与删除事实。"""
        from app.services.reclaim_voucher import ReclaimVoucherService

        entry = store.find(self.MODULE, entry_id)
        if entry is None:
            return None
        detail: dict[str, Any] = {"entry": entry, "reclaim_voucher": None}
        if entry.get("_kind") != "reclaim_confirmation":
            return detail

        lake_table = str(entry.get("所属湖表", ""))
        reclaim_id = str(entry.get("_reclaim_id", ""))
        file_id = str(entry.get("_file_id", ""))
        vouchers = ReclaimVoucherService()
        view = vouchers.voucher_view(lake_table, reclaim_id)
        voucher = next(
            (item for group in view["groups"] for item in group["items"]
             if item["file_id"] == file_id),
            None,
        )
        detail["reclaim_voucher"] = {
            "lake_table": lake_table,
            "reclaim_id": reclaim_id,
            "file_id": file_id,
            "file_bytes": entry.get("_file_bytes"),
            "deletion_fact": entry.get("_deletion_fact"),
            "voucher_code": entry.get("_voucher_id"),
            "state": voucher["state"] if voucher else "未知",
            "partition_key": entry.get("_partition_key"),
            "snapshot_id": entry.get("_snapshot_id"),
            "deletion_facts": view["audit_facts"],
        }
        return detail