from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from lpdev.db import radar_snapshot_get
from lpdev.radar_scheduler import loads_or_none

router = APIRouter()


@router.get("/api/radar")
async def api_radar(request: Request) -> JSONResponse:
    ctx = request.app.state.ctx
    radar = ctx.config.radar
    snapshot = radar_snapshot_get(ctx.conn)
    dashboard = loads_or_none(snapshot["dashboard_json"]) if snapshot else None
    return JSONResponse(
        {
            "enabled": radar.enabled,
            "version": snapshot["version"] if snapshot else 0,
            "fetchedAt": snapshot["fetched_at"] if snapshot else None,
            "failedAt": snapshot["failed_at"] if snapshot else None,
            "error": snapshot["error"] if snapshot else None,
            "dashboard": dashboard,
            "hiddenSections": radar.hidden_sections,
        },
        headers={"Cache-Control": "no-store"},
    )
