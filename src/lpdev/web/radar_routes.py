from __future__ import annotations

import json

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, PlainTextResponse, Response

from lpdev.db import radar_snapshot_get, ui_state_get, ui_state_set
from lpdev.radar_scheduler import loads_or_none

router = APIRouter()

UI_STATE_KEYS = frozenset({"radar"})
MAX_UI_STATE_BYTES = 256 * 1024


def radar_payload(ctx) -> dict:
    radar = ctx.config.radar
    snapshot = radar_snapshot_get(ctx.conn)
    return {
        "enabled": radar.enabled,
        "version": snapshot["version"] if snapshot else 0,
        "fetchedAt": snapshot["fetched_at"] if snapshot else None,
        "failedAt": snapshot["failed_at"] if snapshot else None,
        "error": snapshot["error"] if snapshot else None,
        "dashboard": loads_or_none(snapshot["dashboard_json"]) if snapshot else None,
        "hiddenSections": radar.hidden_sections,
    }


def ui_state_payload(ctx, key: str) -> dict:
    value = loads_or_none(ui_state_get(ctx.conn, key))
    return value if isinstance(value, dict) else {}


@router.get("/api/radar")
async def api_radar(request: Request) -> JSONResponse:
    return JSONResponse(radar_payload(request.app.state.ctx), headers={"Cache-Control": "no-store"})


@router.get("/api/ui-state/{key}")
async def api_ui_state(request: Request, key: str) -> Response:
    if key not in UI_STATE_KEYS:
        return PlainTextResponse("not found", status_code=404)
    return JSONResponse(ui_state_payload(request.app.state.ctx, key), headers={"Cache-Control": "no-store"})


@router.put("/api/ui-state/{key}")
async def api_ui_state_put(request: Request, key: str) -> Response:
    if request.headers.get("x-requested-with") != "lpdev":
        return PlainTextResponse("forbidden", status_code=403)
    if key not in UI_STATE_KEYS:
        return PlainTextResponse("not found", status_code=404)
    body = await request.body()
    if len(body) > MAX_UI_STATE_BYTES:
        return PlainTextResponse("too large", status_code=413)
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, ValueError):
        return PlainTextResponse("invalid json", status_code=400)
    if not isinstance(value, dict):
        return PlainTextResponse("object expected", status_code=400)
    ui_state_set(request.app.state.ctx.conn, key, json.dumps(value, ensure_ascii=False))
    return Response(status_code=204)
