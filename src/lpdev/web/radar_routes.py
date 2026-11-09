from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from lpdev.db import radar_snapshot_get

router = APIRouter()


@router.get("/api/radar")
async def api_radar(request: Request) -> JSONResponse:
    ctx = request.app.state.ctx
    radar = ctx.config.radar
    snapshot = radar_snapshot_get(ctx.conn)
    dashboard = json.loads(snapshot["dashboard_json"]) if snapshot and snapshot["dashboard_json"] else None
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
