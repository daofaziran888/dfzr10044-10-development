"""回收凭证领域服务。

回收任务在中断后最怕"凭感觉重试"：文件可能已经被上一轮删掉，但审计回执还没到，
或者文件是别的回收操作删的。本服务把一次回收拆成**按文件**的凭证明细：

- 删除事实（deletion fact）：回收执行器把文件从存储删掉时，当场登记，带回收号。
- 审计回执（audit confirmation）：审计侧异步送达的确认，可能迟到，也按回收号归档。
- 对账状态：待回收 / 已删除待回执 / 待对账 / 已释放 / 留存。

只有拿到了"回收归属"（本回收号的删除事实或原始确认）的文件，才能计入执行回执和
已释放字节；仅仅在存储里看不到文件，只能先标成"待对账"。

凭证视图按湖表、分区、快照汇总删除明细，并列出仍被快照引用的留存文件，
供保留窗口重试时判断哪些文件应继续回收、哪些已经释放。
"""
from __future__ import annotations

import time
from typing import Any, Iterable

from app.lake_store import lake_store
from app.services.base import BaseModuleService
from app.store import store


# 对账状态
STATE_PENDING = "待回收"
STATE_DELETED_WAIT_RECEIPT = "已删除待回执"
STATE_RECONCILING = "待对账"
STATE_RELEASED = "已释放"
STATE_RETAINED = "留存"

ALL_STATES = [
    STATE_PENDING,
    STATE_DELETED_WAIT_RECEIPT,
    STATE_RECONCILING,
    STATE_RELEASED,
    STATE_RETAINED,
]

# 仍会保护文件、使其不可回收的快照状态
PROTECTING_SNAPSHOT_STATUSES = ("available", "generating")


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime())


class ReclaimVoucherService(BaseModuleService):
    MODULE = "reclaim_voucher"
    ENTITY = "回收凭证"
    OPERATION_TERM = "核销"
    LIST_FIELDS = [
        "凭证编号", "回收号", "所属湖表", "分区标识", "快照标识",
        "文件标识", "文件字节数", "删除事实", "对账状态", "审计条目",
    ]
    REQUIRED_FIELDS = ["凭证编号", "回收号", "所属湖表", "文件标识", "文件字节数"]
    STATUS_ORDER = ALL_STATES
    ACTION_RULES: dict[str, str] = {}
    NEGATIVE_ACTIONS: list[str] = []

    # ------------------------------------------------------------------
    # 基础工具
    # ------------------------------------------------------------------

    def _rows(self) -> list[dict[str, Any]]:
        return store.rows(self.MODULE)

    @staticmethod
    def _bytes(row: dict[str, Any]) -> int:
        try:
            return int(row.get("文件字节数") or 0)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _file_id(row: dict[str, Any]) -> str:
        return str(row.get("文件标识") or "")

    @staticmethod
    def _reclaim_id(row: dict[str, Any]) -> str:
        return str(row.get("回收号") or "")

    @staticmethod
    def _state(row: dict[str, Any]) -> str:
        return str(row.get("对账状态") or STATE_PENDING)

    def _set_state(self, row: dict[str, Any], state: str, reason: str = "") -> None:
        row["对账状态"] = state
        row["status"] = state
        row["pending"] = state in (
            STATE_PENDING, STATE_DELETED_WAIT_RECEIPT, STATE_RECONCILING)
        row["abnormal"] = state == STATE_RECONCILING
        if reason:
            row["对账说明"] = reason

    def _task_rows(self, lake_table: str, reclaim_id: str) -> list[dict[str, Any]]:
        return [
            row for row in self._rows()
            if str(row.get("所属湖表", "")) == lake_table
            and self._reclaim_id(row) == reclaim_id
        ]

    def _find_row(self, lake_table: str, reclaim_id: str,
                  file_id: str) -> dict[str, Any] | None:
        for row in self._rows():
            if (str(row.get("所属湖表", "")) == lake_table
                    and self._reclaim_id(row) == reclaim_id
                    and self._file_id(row) == file_id):
                return row
        return None

    def _next_voucher_code(self) -> str:
        used = 0
        for row in self._rows():
            code = str(row.get("凭证编号") or "")
            if not code.startswith("RV-"):
                continue
            try:
                used = max(used, int(code.rsplit("-", 1)[-1]))
            except ValueError:
                continue
        return f"RV-{used + 1:04d}"

    # ------------------------------------------------------------------
    # 快照引用与留存文件
    # ------------------------------------------------------------------

    def _snapshot_references(self, lake_table: str, file_id: str) -> list[str]:
        """返回仍引用该文件的快照编号。

        留存判定看整条快照链：只要还有可用/生成中的快照引用该文件就受保护，
        不能因为凭证只记了某个已经过期的快照就误判为可回收。
        """
        hits: list[str] = []
        for snap in lake_store.list_snapshots(lake_table):
            if snap.status not in PROTECTING_SNAPSHOT_STATUSES:
                continue
            if file_id in snap.file_ids:
                hits.append(snap.snapshot_id)
        return hits

    def list_retained_files(self, lake_table: str | None = None) -> list[dict[str, Any]]:
        """列出仍有快照引用的留存文件；下次回收据此跳过，不当作已释放。"""
        tables = [lake_table] if lake_table else self._known_lake_tables()
        result: list[dict[str, Any]] = []
        for table in tables:
            protected: dict[str, list[str]] = {}
            for snap in lake_store.list_snapshots(table):
                if snap.status not in PROTECTING_SNAPSHOT_STATUSES:
                    continue
                for fid in snap.file_ids:
                    protected.setdefault(fid, []).append(snap.snapshot_id)
            for fid, snap_ids in protected.items():
                pf = lake_store.get_file(table, fid)
                result.append({
                    "文件标识": fid,
                    "所属湖表": table,
                    "分区标识": pf.partition_key if pf else "",
                    "文件字节数": pf.file_bytes if pf else 0,
                    "引用快照": ",".join(sorted(set(snap_ids))),
                    "对账状态": STATE_RETAINED,
                    "说明": "仍有快照引用，不能回收",
                })
        return result

    def _known_lake_tables(self) -> list[str]:
        tables = {str(row.get("所属湖表", "")) for row in self._rows()}
        tables.discard("")
        tables.update(lake_store.lake_tables())
        return sorted(tables)

    # ------------------------------------------------------------------
    # 审计回执与删除事实
    # ------------------------------------------------------------------

    def _confirmation_for(self, lake_table: str, reclaim_id: str,
                          file_id: str) -> dict[str, Any] | None:
        """查找与本回收号、本湖表、本文件严格匹配的原始确认。"""
        for entry in store.rows("lake_audit"):
            if entry.get("_kind") != "reclaim_confirmation":
                continue
            if (str(entry.get("所属湖表", "")) == lake_table
                    and str(entry.get("_reclaim_id", "")) == reclaim_id
                    and str(entry.get("_file_id", "")) == file_id):
                return entry
        return None

    @staticmethod
    def _storage_present(lake_table: str, file_id: str) -> bool:
        return lake_store.get_file(lake_table, file_id) is not None

    def _release(self, row: dict[str, Any], audit_entry: dict[str, Any]) -> None:
        self._set_state(row, STATE_RELEASED)
        row["审计条目"] = str(audit_entry.get("条目编号", ""))
        fact = str(audit_entry.get("_deletion_fact") or "").strip()
        if not str(row.get("删除事实") or "").strip():
            row["删除事实"] = fact or str(audit_entry.get("操作时间", ""))

    def _apply_confirmation(self, row: dict[str, Any],
                            audit_entry: dict[str, Any] | None) -> bool:
        """在尝试回收时先吸收已经到达的审计回执。"""
        if audit_entry is None:
            return False
        state = self._state(row)
        if state == STATE_RETAINED:
            return False
        if state == STATE_PENDING:
            # 文件还在存储里，回执却声称已删除：只能先挂起对账，不能核销。
            if self._storage_present(
                    str(row.get("所属湖表", "")), self._file_id(row)):
                self._set_state(
                    row, STATE_RECONCILING,
                    "审计回执与存储不一致：文件仍在，不能核销")
                return False
        if state != STATE_RELEASED:
            self._release(row, audit_entry)
        return True

    def _record_attempt(self, lake_table: str, reclaim_id: str,
                        record: str, operator_id: str) -> dict[str, Any]:
        """把一次回收尝试的操作记录写进审计链，供值班员回看。"""
        entries = store.rows("lake_audit")
        entry_id = max((int(item.get("id", 0)) for item in entries), default=0) + 1
        entry: dict[str, Any] = {
            "id": entry_id,
            "status": "已记录",
            "pending": False,
            "abnormal": False,
            "条目编号": f"AUD-RUN-{entry_id:03d}",
            "所属湖表": lake_table,
            "操作类型": "回收尝试",
            "操作对象": reclaim_id,
            "变更前值": "回收中断/待重试",
            "变更后值": record,
            "操作者标识": operator_id,
            "操作时间": _now_iso(),
            "_kind": "reclaim_attempt",
            "_reclaim_id": reclaim_id,
        }
        entries.append(entry)
        return entry

    def attempt_records(self, lake_table: str | None = None,
                        reclaim_id: str | None = None) -> list[dict[str, Any]]:
        """历史回收尝试的操作记录（审计链 -> 操作记录）。"""
        records: list[dict[str, Any]] = []
        for entry in store.rows("lake_audit"):
            if entry.get("_kind") != "reclaim_attempt":
                continue
            if lake_table and str(entry.get("所属湖表", "")) != lake_table:
                continue
            if reclaim_id and str(entry.get("_reclaim_id", "")) != reclaim_id:
                continue
            records.append({
                "审计条目": entry.get("条目编号"),
                "所属湖表": entry.get("所属湖表"),
                "回收号": entry.get("_reclaim_id"),
                "操作记录": entry.get("变更后值"),
                "操作者标识": entry.get("操作者标识"),
                "操作时间": entry.get("操作时间"),
            })
        records.sort(key=lambda item: str(item.get("操作时间", "")))
        return records

    def audit_facts(self, lake_table: str | None = None,
                    reclaim_id: str | None = None) -> list[dict[str, Any]]:
        """审计详情页的删除事实清单：与回收凭证一一对应。"""
        facts: list[dict[str, Any]] = []
        for entry in store.rows("lake_audit"):
            if entry.get("_kind") != "reclaim_confirmation":
                continue
            if lake_table and str(entry.get("所属湖表", "")) != lake_table:
                continue
            if reclaim_id and str(entry.get("_reclaim_id", "")) != reclaim_id:
                continue
            facts.append({
                "审计条目": entry.get("条目编号"),
                "所属湖表": entry.get("所属湖表"),
                "操作类型": entry.get("操作类型"),
                "文件标识": entry.get("_file_id"),
                "文件字节数": entry.get("_file_bytes"),
                "删除事实": entry.get("_deletion_fact"),
                "回收号": entry.get("_reclaim_id"),
                "凭证编号": entry.get("_voucher_id"),
                "分区标识": entry.get("_partition_key"),
                "快照标识": entry.get("_snapshot_id"),
                "核销结论": entry.get("变更后值"),
                "操作者标识": entry.get("操作者标识"),
                "操作时间": entry.get("操作时间"),
            })
        facts.sort(key=lambda item: str(item.get("操作时间", "")))
        return facts

    # ------------------------------------------------------------------
    # 建立回收任务
    # ------------------------------------------------------------------

    def _normalise_targets(self, targets: Iterable[Any]) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        for target in targets:
            if isinstance(target, dict):
                fid = str(target.get("file_id") or target.get("文件标识") or "")
                raw_bytes = target.get("file_bytes", target.get("文件字节数", 0))
                partition = str(target.get("partition_key") or target.get("分区标识") or "")
                snapshot = str(target.get("snapshot_id") or target.get("快照标识") or "")
            else:
                fid = str(target[0])
                raw_bytes = target[1] if len(target) > 1 else 0
                partition = str(target[2]) if len(target) > 2 else ""
                snapshot = str(target[3]) if len(target) > 3 else ""
            if not fid:
                continue
            try:
                file_bytes = int(raw_bytes or 0)
            except (TypeError, ValueError):
                file_bytes = 0
            result.append({
                "file_id": fid,
                "file_bytes": file_bytes,
                "partition_key": partition,
                "snapshot_id": snapshot,
            })
        return result

    def create_reclaim_task(
        self,
        lake_table: str,
        reclaim_id: str,
        targets: Iterable[Any],
        partition_key: str = "",
        snapshot_id: str = "",
        operator_id: str = "",
    ) -> dict[str, Any]:
        """为一次回收登记逐文件凭证；同名文件只在本湖表内归属。"""
        rows = self._rows()
        created: list[dict[str, Any]] = []
        for target in self._normalise_targets(targets):
            fid = target["file_id"]
            existing = self._find_row(lake_table, reclaim_id, fid)
            if existing is not None:
                created.append(existing)
                continue
            partition = target["partition_key"] or partition_key
            snapshot = target["snapshot_id"] or snapshot_id
            pf = lake_store.get_file(lake_table, fid)
            if not partition and pf is not None:
                partition = pf.partition_key
            file_bytes = target["file_bytes"]
            if not file_bytes and pf is not None:
                file_bytes = pf.file_bytes
            row: dict[str, Any] = {
                "id": max((int(item.get("id", 0)) for item in rows), default=0) + 1,
                "status": STATE_PENDING,
                "pending": True,
                "abnormal": False,
                "凭证编号": self._next_voucher_code(),
                "回收号": reclaim_id,
                "所属湖表": lake_table,
                "分区标识": partition,
                "快照标识": snapshot,
                "文件标识": fid,
                "文件字节数": file_bytes,
                "删除事实": "",
                "对账状态": STATE_PENDING,
                "审计条目": "",
                "操作者标识": operator_id,
                "建立时间": _now_iso(),
            }
            if self._snapshot_references(lake_table, fid):
                self._set_state(row, STATE_RETAINED, "仍有快照引用，不能回收")
                row["待回收字节数"] = file_bytes
            rows.append(row)
            created.append(row)
        total_bytes = sum(self._bytes(row) for row in created)
        return {
            "ok": True,
            "lake_table": lake_table,
            "reclaim_id": reclaim_id,
            "voucher_count": len(created),
            "target_bytes": total_bytes,
            "items": [self._row_summary(row) for row in created],
        }

    # ------------------------------------------------------------------
    # 执行一次回收尝试
    # ------------------------------------------------------------------

    def _row_summary(self, row: dict[str, Any]) -> dict[str, Any]:
        audit_entry = self._confirmation_for(
            str(row.get("所属湖表", "")), self._reclaim_id(row),
            self._file_id(row))
        state = self._state(row)
        fact = str(row.get("删除事实") or "").strip()
        attributed = bool(fact) or audit_entry is not None
        if state == STATE_RECONCILING:
            attributed = False
        confirmed = state == STATE_RELEASED and audit_entry is not None
        return {
            "id": row.get("id"),
            "voucher_code": row.get("凭证编号"),
            "reclaim_id": self._reclaim_id(row),
            "lake_table": row.get("所属湖表"),
            "partition_key": row.get("分区标识"),
            "snapshot_id": row.get("快照标识"),
            "file_id": self._file_id(row),
            "file_bytes": self._bytes(row),
            "state": state,
            "deletion_fact": fact,
            "audit_entry": row.get("审计条目", ""),
            "attributed": attributed,
            "attributed_bytes": self._bytes(row) if attributed else 0,
            "confirmed": confirmed,
            "confirmed_bytes": self._bytes(row) if confirmed else 0,
            "independent_confirmation": confirmed,
            "reason": str(row.get("对账说明") or ""),
        }

    def attempt_reclaim(
        self,
        lake_table: str,
        reclaim_id: str,
        interrupt_after: int | None = None,
        operator_id: str = "reclaim_runner",
    ) -> dict[str, Any]:
        """重试回收：只处理仍应继续回收的文件。

        - 已删除待回执：沿用原删除事实，绝不重复删除；
        - 待对账：等补证，既不再删也不计入回执；
        - 已释放 / 留存：跳过；
        - 待回收：存储里存在才删除并登记删除事实，不存在则标待对账。
        """
        rows = self._task_rows(lake_table, reclaim_id)
        if not rows:
            return {
                "ok": False,
                "message": f"回收号 {reclaim_id} 在湖表 {lake_table} 下没有凭证",
                "lake_table": lake_table,
                "reclaim_id": reclaim_id,
            }

        newly_deleted: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        interrupted = False
        processed_rows: set[int] = set()

        for row in rows:
            fid = self._file_id(row)
            b = self._bytes(row)
            entry = self._confirmation_for(lake_table, reclaim_id, fid)
            self._apply_confirmation(row, entry)
            state = self._state(row)

            if state in (STATE_RELEASED, STATE_RETAINED):
                skipped.append({
                    "file_id": fid, "bytes": b, "state": state,
                    "reason": "已释放，不再重复回收" if state == STATE_RELEASED
                    else "仍有快照引用，跳过",
                })
                continue
            if state == STATE_RECONCILING:
                skipped.append({
                    "file_id": fid, "bytes": b, "state": state,
                    "reason": "待对账，必须等补证后才能继续",
                })
                continue
            if state == STATE_DELETED_WAIT_RECEIPT:
                skipped.append({
                    "file_id": fid, "bytes": b, "state": state,
                    "reason": "删除事实已登记，等待审计回执；不重复删除",
                })
                continue

            # state == STATE_PENDING
            snap_ids = self._snapshot_references(lake_table, fid)
            if snap_ids:
                self._set_state(row, STATE_RETAINED, "仍有快照引用，不能回收")
                skipped.append({
                    "file_id": fid, "bytes": b, "state": STATE_RETAINED,
                    "reason": f"仍有快照引用：{','.join(snap_ids)}",
                })
                continue

            pf = lake_store.get_file(lake_table, fid)
            if pf is None:
                self._set_state(
                    row, STATE_RECONCILING,
                    "存储中已无该文件，且没有本回收号的删除事实/原始确认")
                skipped.append({
                    "file_id": fid, "bytes": b, "state": STATE_RECONCILING,
                    "reason": "存储中已不存在，先标待对账，不得计入执行回执",
                })
                continue

            lake_store.remove_files(lake_table, [fid])
            row["删除事实"] = _now_iso()
            self._set_state(
                row, STATE_DELETED_WAIT_RECEIPT,
                "已删除并登记删除事实，等待审计回执")
            newly_deleted.append({"file_id": fid, "bytes": b})
            processed_rows.add(id(row))

            if entry is not None:
                self._apply_confirmation(row, entry)

            if interrupt_after is not None and len(newly_deleted) >= interrupt_after:
                interrupted = True
                break

        items = [self._row_summary(row) for row in rows]
        pending = [item for item in items if item["state"] == STATE_RECONCILING]
        retained = [item for item in items if item["state"] == STATE_RETAINED]
        released = [item for item in items if item["state"] == STATE_RELEASED]
        confirmed_bytes = sum(item["confirmed_bytes"] for item in items)
        attributed_bytes = sum(item["attributed_bytes"] for item in items)
        pending_bytes = sum(item["file_bytes"] for item in pending)
        retained_bytes = sum(item["file_bytes"] for item in retained)
        independent = [
            {"file_id": item["file_id"], "file_bytes": item["file_bytes"],
             "confirmed": True}
            for item in items
            if item["independent_confirmation"]
        ]
        waiting_for = [item["file_id"] for item in pending]
        requires_evidence = bool(waiting_for)

        operation_record = self._operation_record(
            reclaim_id=reclaim_id,
            newly_deleted=newly_deleted,
            released=released,
            pending=pending,
            interrupted=interrupted,
            confirmed_bytes=confirmed_bytes,
        )
        self._record_attempt(lake_table, reclaim_id, operation_record, operator_id)

        return {
            "ok": True,
            "lake_table": lake_table,
            "reclaim_id": reclaim_id,
            "operator_id": operator_id,
            "interrupted": interrupted,
            "newly_deleted": newly_deleted,
            "skipped": skipped,
            "items": items,
            "receipt": {
                # 执行回执只认有回收归属的字节；待对账项一律排除。
                "attributed_bytes": attributed_bytes,
                "confirmed_bytes": confirmed_bytes,
                "excluded_from_receipt": [
                    {"file_id": item["file_id"], "file_bytes": item["file_bytes"],
                     "reason": item["reason"] or "未获得回收归属"}
                    for item in pending
                ],
            },
            "released_file_ids": [item["file_id"] for item in released],
            "independent_confirmations": independent,
            "independent_confirmed_bytes": sum(
                item["file_bytes"] for item in independent),
            "pending_reconciliation": pending,
            "pending_reconciliation_bytes": pending_bytes,
            "retained_files": retained,
            "retained_bytes": retained_bytes,
            "waiting_for_evidence_from": waiting_for,
            "requires_supplementary_evidence": requires_evidence,
            "can_continue": not requires_evidence and not interrupted,
            "completion": (
                "blocked_by_evidence" if requires_evidence
                else "complete" if not newly_deleted else "deleted_waiting_receipt"
            ),
            "operation_record": operation_record,
        }

    @staticmethod
    def _operation_record(
        *,
        reclaim_id: str,
        newly_deleted: list[dict[str, Any]],
        released: list[dict[str, Any]],
        pending: list[dict[str, Any]],
        interrupted: bool,
        confirmed_bytes: int,
    ) -> str:
        parts: list[str] = [f"回收号 {reclaim_id}"]
        if newly_deleted:
            deleted_bytes = sum(item["bytes"] for item in newly_deleted)
            names = "、".join(item["file_id"] for item in newly_deleted)
            parts.append(f"本轮新删除 {names}（{deleted_bytes} 字节）")
        if interrupted:
            parts.append("回收已中断，重试时不会重复删除已登记删除事实的文件")
        if released:
            detail = "、".join(
                f"{item['file_id']} 已独立确认 {item['file_bytes']} 字节"
                for item in released)
            parts.append(f"{detail}，合计 {confirmed_bytes} 字节")
        if pending:
            names = "、".join(item["file_id"] for item in pending)
            parts.append(f"{names} 待对账，未获得回收归属，不计入任何执行回执")
            if released:
                parts.append(f"本轮仍需等 {names} 补证后才可继续核销")
            else:
                parts.append(f"本轮需等 {names} 补证后才可继续核销")
        if len(parts) == 1:
            parts.append("没有可回收的文件")
        return "；".join(parts)

    # ------------------------------------------------------------------
    # 审计回执核销
    # ------------------------------------------------------------------

    def record_audit_confirmation(
        self,
        lake_table: str,
        reclaim_id: str,
        file_id: str,
        file_bytes: int | None = None,
        operator_id: str = "audit",
        deletion_fact: str | None = None,
    ) -> dict[str, Any]:
        """登记录入审计回执；必须与本湖表、本回收号、本文件严格匹配。"""
        row = self._find_row(lake_table, reclaim_id, file_id)
        if row is None:
            return {
                "ok": False,
                "released": False,
                "message": (
                    f"湖表 {lake_table} 的回收号 {reclaim_id} 下没有文件 {file_id} 的凭证；"
                    "不能用其他湖表或其他回收号的凭据核销"),
            }
        expected = self._bytes(row)
        if file_bytes is not None and int(file_bytes) != expected:
            return {
                "ok": False,
                "released": False,
                "message": f"字节数不一致：凭证为 {expected}，回执为 {file_bytes}",
            }
        if self._state(row) == STATE_RETAINED:
            return {
                "ok": False,
                "released": False,
                "message": "该文件仍有快照引用，属于留存文件，不能核销为已释放",
            }

        existing = self._confirmation_for(lake_table, reclaim_id, file_id)
        if existing is not None:
            self._apply_confirmation(row, existing)
            return {
                "ok": True,
                "released": self._state(row) == STATE_RELEASED,
                "file_id": file_id,
                "file_bytes": expected,
                "audit_entry": existing.get("条目编号"),
                "state": self._state(row),
                "message": "已有原始确认，按既有回执对账",
            }

        fact = str(deletion_fact or row.get("删除事实") or "").strip()
        # 文件还在存储里却收到删除回执：先挂起对账，不能释放。
        storage_present = lake_store.get_file(lake_table, file_id) is not None
        if storage_present and self._state(row) == STATE_PENDING:
            self._set_state(
                row, STATE_RECONCILING,
                "审计回执声称已删除，但文件仍在存储中，先标待对账")
            return {
                "ok": True,
                "released": False,
                "file_id": file_id,
                "file_bytes": expected,
                "state": self._state(row),
                "message": "回执与存储不一致，已标待对账，未计入已释放字节",
            }

        entries = store.rows("lake_audit")
        entry_id = max((int(item.get("id", 0)) for item in entries), default=0) + 1
        entry_code = f"AUD-RCL-{entry_id:03d}"
        before = f"删除事实：{fact}" if fact else "存储已无该文件，归属待对账"
        entry: dict[str, Any] = {
            "id": entry_id,
            "status": "已核查",
            "pending": False,
            "abnormal": False,
            "条目编号": entry_code,
            "所属湖表": lake_table,
            "操作类型": "回收确认",
            "操作对象": file_id,
            "变更前值": before,
            "变更后值": f"已释放 {expected} 字节",
            "操作者标识": operator_id,
            "操作时间": _now_iso(),
            "_kind": "reclaim_confirmation",
            "_reclaim_id": reclaim_id,
            "_voucher_id": row.get("凭证编号"),
            "_file_id": file_id,
            "_file_bytes": expected,
            "_partition_key": row.get("分区标识"),
            "_snapshot_id": row.get("快照标识"),
            "_deletion_fact": fact,
            "_lake_table": lake_table,
        }
        entries.append(entry)
        self._release(row, entry)
        return {
            "ok": True,
            "released": True,
            "file_id": file_id,
            "file_bytes": expected,
            "audit_entry": entry_code,
            "state": self._state(row),
            "voucher_code": row.get("凭证编号"),
            "message": f"已按回收号 {reclaim_id} 的原始确认核销 {expected} 字节",
        }

    # ------------------------------------------------------------------
    # 待对账与对账汇总
    # ------------------------------------------------------------------

    def mark_pending_reconciliation(
        self,
        lake_table: str,
        reclaim_id: str,
        file_id: str,
        reason: str = "存储中已不存在，未找到本回收号的删除事实或原始确认",
        clear_deletion_fact: bool = False,
    ) -> dict[str, Any]:
        row = self._find_row(lake_table, reclaim_id, file_id)
        if row is None:
            return {"ok": False, "message": "凭证不存在，不能跨表标记"}
        if clear_deletion_fact:
            row["删除事实"] = ""
        self._set_state(row, STATE_RECONCILING, reason)
        return {
            "ok": True,
            "file_id": file_id,
            "file_bytes": self._bytes(row),
            "state": STATE_RECONCILING,
            "message": f"{file_id} 已标为待对账：{reason}",
        }

    def detect_missing_files(self, lake_table: str,
                             reclaim_id: str) -> list[dict[str, Any]]:
        """重启后按存储核对：缺失但无本回收号删除事实的，先标待对账。"""
        marked: list[dict[str, Any]] = []
        for row in self._task_rows(lake_table, reclaim_id):
            if self._state(row) != STATE_PENDING:
                continue
            if lake_store.get_file(lake_table, self._file_id(row)) is not None:
                continue
            self._set_state(
                row, STATE_RECONCILING,
                "存储中已无该文件，可能是其他回收操作删除，先标待对账")
            marked.append({
                "file_id": self._file_id(row),
                "file_bytes": self._bytes(row),
                "state": STATE_RECONCILING,
            })
        return marked

    def reconcile(self, lake_table: str, reclaim_id: str) -> dict[str, Any]:
        """对账汇总：已释放字节、待对账项与逐文件删除事实。"""
        rows = self._task_rows(lake_table, reclaim_id)
        if not rows:
            return {
                "ok": False,
                "message": f"回收号 {reclaim_id} 在湖表 {lake_table} 下没有凭证",
            }
        items = [self._row_summary(row) for row in rows]
        released = [item for item in items if item["state"] == STATE_RELEASED]
        pending = [item for item in items if item["state"] == STATE_RECONCILING]
        retained = [item for item in items if item["state"] == STATE_RETAINED]
        target_bytes = sum(item["file_bytes"] for item in items)
        retained_bytes = sum(item["file_bytes"] for item in retained)
        released_bytes = sum(item["file_bytes"] for item in released)
        pending_bytes = sum(item["file_bytes"] for item in pending)
        deletion_facts = [
            {
                "file_id": item["file_id"],
                "file_bytes": item["file_bytes"],
                "删除事实": item["deletion_fact"],
                "审计条目": item["audit_entry"],
                "对账状态": item["state"],
            }
            for item in items
            if item["state"] in (STATE_RELEASED, STATE_DELETED_WAIT_RECEIPT)
        ]
        accountable_bytes = target_bytes - retained_bytes
        all_reconciled = (
            not pending
            and all(item["state"] in (STATE_RELEASED, STATE_RETAINED)
                    for item in items)
            and released_bytes == accountable_bytes
        )
        return {
            "ok": True,
            "lake_table": lake_table,
            "reclaim_id": reclaim_id,
            "target_bytes": target_bytes,
            "accountable_bytes": accountable_bytes,
            "released_count": len(released),
            "released_bytes": released_bytes,
            "confirmed_bytes": released_bytes,
            "pending_reconciliation": pending,
            "pending_reconciliation_bytes": pending_bytes,
            "retained": retained,
            "retained_bytes": retained_bytes,
            "deletion_facts": deletion_facts,
            "all_reconciled": all_reconciled,
            "items": items,
        }

    def reclaim_next_actions(self, lake_table: str,
                             reclaim_id: str) -> dict[str, Any]:
        """给下一次回收的清单：继续、已释放、留存、待对账各是哪些文件。"""
        rows = self._task_rows(lake_table, reclaim_id)
        result = {
            "continue": [],
            "already_released": [],
            "retained": [],
            "pending_reconciliation": [],
            "waiting_for_evidence_from": [],
        }
        for row in rows:
            item = self._row_summary(row)
            state = item["state"]
            if state in (STATE_PENDING, STATE_DELETED_WAIT_RECEIPT):
                result["continue"].append(item)
            elif state == STATE_RELEASED:
                result["already_released"].append(item)
            elif state == STATE_RETAINED:
                result["retained"].append(item)
            elif state == STATE_RECONCILING:
                result["pending_reconciliation"].append(item)
                result["waiting_for_evidence_from"].append(item["file_id"])
        return result

    # ------------------------------------------------------------------
    # 凭证视图
    # ------------------------------------------------------------------

    def voucher_view(
        self,
        lake_table: str | None = None,
        reclaim_id: str | None = None,
    ) -> dict[str, Any]:
        """按湖表、分区、快照汇总删除明细，并附留存文件与审计事实。"""
        rows = [
            row for row in self._rows()
            if (not lake_table or str(row.get("所属湖表", "")) == lake_table)
            and (not reclaim_id or self._reclaim_id(row) == reclaim_id)
        ]
        groups: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
        for row in rows:
            key = (
                str(row.get("所属湖表", "")),
                str(row.get("分区标识", "")),
                str(row.get("快照标识", "")),
            )
            groups.setdefault(key, []).append(row)

        group_payloads: list[dict[str, Any]] = []
        for (table, partition, snapshot), group_rows in sorted(groups.items()):
            items = [self._row_summary(row) for row in group_rows]
            released = [i for i in items if i["state"] == STATE_RELEASED]
            pending = [i for i in items if i["state"] == STATE_RECONCILING]
            retained = [i for i in items if i["state"] == STATE_RETAINED]
            group_payloads.append({
                "lake_table": table,
                "partition_key": partition,
                "snapshot_id": snapshot,
                "target_bytes": sum(i["file_bytes"] for i in items),
                "released_bytes": sum(i["file_bytes"] for i in released),
                "pending_reconciliation_bytes": sum(i["file_bytes"] for i in pending),
                "retained_bytes": sum(i["file_bytes"] for i in retained),
                "items": items,
                "deletion_facts": [
                    {
                        "file_id": i["file_id"],
                        "file_bytes": i["file_bytes"],
                        "删除事实": i["deletion_fact"],
                        "审计条目": i["audit_entry"],
                        "对账状态": i["state"],
                    }
                    for i in items if i["deletion_fact"]
                ],
                "retained_files": [
                    {"file_id": i["file_id"], "file_bytes": i["file_bytes"]}
                    for i in retained
                ],
            })

        reclaims: dict[tuple[str, str], dict[str, Any]] = {}
        for row in rows:
            key = (str(row.get("所属湖表", "")), self._reclaim_id(row))
            bucket = reclaims.setdefault(key, {
                "lake_table": key[0],
                "reclaim_id": key[1],
                "voucher_count": 0,
                "target_bytes": 0,
                "released_bytes": 0,
                "pending_reconciliation_bytes": 0,
                "retained_bytes": 0,
                "released_files": [],
                "pending_files": [],
                "retained_files": [],
            })
            item = self._row_summary(row)
            bucket["voucher_count"] += 1
            bucket["target_bytes"] += item["file_bytes"]
            if item["state"] == STATE_RELEASED:
                bucket["released_bytes"] += item["file_bytes"]
                bucket["released_files"].append(item["file_id"])
            elif item["state"] == STATE_RECONCILING:
                bucket["pending_reconciliation_bytes"] += item["file_bytes"]
                bucket["pending_files"].append(item["file_id"])
            elif item["state"] == STATE_RETAINED:
                bucket["retained_bytes"] += item["file_bytes"]
                bucket["retained_files"].append(item["file_id"])

        summary = {
            "voucher_count": len(rows),
            "target_bytes": sum(i["target_bytes"] for i in group_payloads),
            "released_bytes": sum(i["released_bytes"] for i in group_payloads),
            "pending_reconciliation_bytes": sum(
                i["pending_reconciliation_bytes"] for i in group_payloads),
            "retained_bytes": sum(i["retained_bytes"] for i in group_payloads),
        }
        return {
            "ok": True,
            "lake_table": lake_table or "",
            "reclaim_id": reclaim_id or "",
            "summary": summary,
            "groups": group_payloads,
            "reclaims": sorted(
                reclaims.values(),
                key=lambda item: (item["lake_table"], item["reclaim_id"])),
            "retained": self.list_retained_files(lake_table),
            "audit_facts": self.audit_facts(lake_table, reclaim_id),
            "attempt_records": self.attempt_records(lake_table, reclaim_id),
        }

    # ------------------------------------------------------------------
    # 模块级快照
    # ------------------------------------------------------------------

    def domain_snapshot(self) -> dict[str, Any]:
        snapshot = super().domain_snapshot()
        rows = self._rows()
        snapshot.update({
            "released_bytes": sum(
                self._bytes(row) for row in rows
                if self._state(row) == STATE_RELEASED),
            "pending_reconciliation": sum(
                1 for row in rows if self._state(row) == STATE_RECONCILING),
            "retained_files": len(self.list_retained_files()),
        })
        return snapshot
