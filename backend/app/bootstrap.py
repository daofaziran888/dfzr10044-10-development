"""启动期数据准备：把示例数据里的存储态（文件、快照）幂等灌入内存湖表存储。"""
from __future__ import annotations

from app.lake_store import lake_store
from app.seed import RECLAIM_DEMO_FILES, RECLAIM_DEMO_SNAPSHOTS


def bootstrap_lake_store() -> None:
    """按 file_id / snapshot_id 幂等登记，重复启动不会产生重复数据。"""
    for item in RECLAIM_DEMO_FILES:
        lake_table = str(item["lake_table"])
        file_id = str(item["file_id"])
        if lake_store.get_file(lake_table, file_id) is not None:
            continue
        lake_store.register_file(
            lake_table,
            str(item["partition_key"]),
            rows=int(item.get("rows", 0)),
            bytes_=int(item.get("file_bytes", 0)),
            file_id=file_id,
        )
    for item in RECLAIM_DEMO_SNAPSHOTS:
        lake_table = str(item["lake_table"])
        snapshot_id = str(item["snapshot_id"])
        if lake_store.get_snapshot(lake_table, snapshot_id) is not None:
            continue
        lake_store.create_snapshot(
            lake_table,
            list(item.get("file_ids", [])),
            snapshot_id=snapshot_id,
        )
        lake_store.mark_snapshot_available(lake_table, snapshot_id)
