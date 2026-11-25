"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import reclaim_voucher, resource

# 回收凭证的固定路径必须先于通用 /api/{module} 动态路由注册，
# 否则 /api/reclaim-voucher/view 会被当成模块条目查询。
ROUTERS = [reclaim_voucher, resource]
