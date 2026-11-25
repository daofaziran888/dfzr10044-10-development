"""回收凭证接口：凭证视图、回收重试、审计回执核销与对账动作。"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.schemas import ActionResult
from app.services import service_for
from app.services.reclaim_voucher import ReclaimVoucherService

router = APIRouter(prefix="/api/reclaim-voucher", tags=["回收凭证"])


def _service() -> ReclaimVoucherService:
    service = service_for("reclaim_voucher")
    if not isinstance(service, ReclaimVoucherService):
        raise HTTPException(status_code=500, detail="回收凭证服务未注册")
    return service


class TargetPayload(BaseModel):
    file_id: str
    file_bytes: int = 0
    partition_key: str = ""
    snapshot_id: str = ""


class ReclaimTaskPayload(BaseModel):
    lake_table: str
    reclaim_id: str
    targets: list[TargetPayload] = Field(default_factory=list)
    partition_key: str = ""
    snapshot_id: str = ""
    operator_id: str = ""


class AttemptPayload(BaseModel):
    lake_table: str
    reclaim_id: str
    interrupt_after: int | None = None
    operator_id: str = "reclaim_runner"


class ConfirmationPayload(BaseModel):
    lake_table: str
    reclaim_id: str
    file_id: str
    file_bytes: int | None = None
    operator_id: str = "audit"
    deletion_fact: str | None = None


class ReconcilePayload(BaseModel):
    lake_table: str
    reclaim_id: str


class MarkPendingPayload(BaseModel):
    lake_table: str
    reclaim_id: str
    file_id: str
    reason: str = "存储中已不存在，未找到本回收号的删除事实或原始确认"
    clear_deletion_fact: bool = False


@router.get("/view")
def voucher_view(lake_table: str | None = None,
                 reclaim_id: str | None = None) -> dict[str, Any]:
    return _service().voucher_view(lake_table, reclaim_id)


@router.get("/retained")
def retained_files(lake_table: str | None = None) -> dict[str, Any]:
    files = _service().list_retained_files(lake_table)
    return {"items": files, "total": len(files)}


@router.get("/audit-facts")
def audit_facts(lake_table: str | None = None,
                reclaim_id: str | None = None) -> dict[str, Any]:
    facts = _service().audit_facts(lake_table, reclaim_id)
    return {"items": facts, "total": len(facts)}


@router.get("/attempt-records")
def attempt_records(lake_table: str | None = None,
                    reclaim_id: str | None = None) -> dict[str, Any]:
    records = _service().attempt_records(lake_table, reclaim_id)
    return {"items": records, "total": len(records)}


@router.get("/audit-detail/{entry_id}")
def audit_detail(entry_id: int) -> dict[str, Any]:
    """审计详情页：回收确认条目回溯到删除事实与凭证。"""
    from app.services.lake_audit import LakeAuditService

    detail = LakeAuditService().audit_entry_detail(entry_id)
    if detail is None:
        raise HTTPException(status_code=404, detail=f"审计条目 {entry_id} 不存在")
    return detail


@router.get("/next-actions")
def next_actions(lake_table: str, reclaim_id: str) -> dict[str, Any]:
    return _service().reclaim_next_actions(lake_table, reclaim_id)


@router.post("/tasks", response_model=ActionResult)
def create_task(payload: ReclaimTaskPayload) -> ActionResult:
    result = _service().create_reclaim_task(
        lake_table=payload.lake_table,
        reclaim_id=payload.reclaim_id,
        targets=[target.model_dump() for target in payload.targets],
        partition_key=payload.partition_key,
        snapshot_id=payload.snapshot_id,
        operator_id=payload.operator_id,
    )
    return ActionResult(
        ok=bool(result.get("ok")),
        message=f"已登记 {result.get('voucher_count', 0)} 份回收凭证",
    )


@router.post("/attempt")
def attempt(payload: AttemptPayload) -> dict[str, Any]:
    return _service().attempt_reclaim(
        lake_table=payload.lake_table,
        reclaim_id=payload.reclaim_id,
        interrupt_after=payload.interrupt_after,
        operator_id=payload.operator_id,
    )


@router.post("/confirm")
def confirm(payload: ConfirmationPayload) -> dict[str, Any]:
    return _service().record_audit_confirmation(
        lake_table=payload.lake_table,
        reclaim_id=payload.reclaim_id,
        file_id=payload.file_id,
        file_bytes=payload.file_bytes,
        operator_id=payload.operator_id,
        deletion_fact=payload.deletion_fact,
    )


@router.post("/reconcile")
def reconcile(payload: ReconcilePayload) -> dict[str, Any]:
    return _service().reconcile(payload.lake_table, payload.reclaim_id)


@router.post("/mark-pending")
def mark_pending(payload: MarkPendingPayload) -> dict[str, Any]:
    return _service().mark_pending_reconciliation(
        lake_table=payload.lake_table,
        reclaim_id=payload.reclaim_id,
        file_id=payload.file_id,
        reason=payload.reason,
        clear_deletion_fact=payload.clear_deletion_fact,
    )
