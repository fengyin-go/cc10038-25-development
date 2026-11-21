"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import event_timeline_router, pipeline_router, resource

# 固定前缀的专用路由放在通用 /api/{module} 动态路由之前注册，
# 避免 event-timeline、pipeline 这类多段路径被动态路由抢先匹配。
ROUTERS = [event_timeline_router, pipeline_router, resource]
