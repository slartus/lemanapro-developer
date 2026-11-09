from __future__ import annotations

import json

import httpx
import pytest

from lpdev.main import create_app

H = {"X-Requested-With": "lpdev"}


def _client(ctx_obj) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app(ctx_obj)), base_url="http://127.0.0.1:8123")


def _initial(page) -> dict:
    raw = page.text.split('<script type="application/json" id="initial-data">', 1)[1].split("</script>", 1)[0]
    return json.loads(raw)


async def test_ui_state_round_trip_and_page_embeds_it(ctx):
    state = {"snoozed": {"10!1": {"until": "2026-11-10T09:00:00.000Z"}}, "dismissedTokenProblems": {"noExpiry": "noExpiry"}}
    async with _client(ctx[0]) as client:
        empty = await client.get("/api/ui-state/radar")
        saved = await client.put("/api/ui-state/radar", headers=H, json=state)
        loaded = await client.get("/api/ui-state/radar")
        page = await client.get("/")
    assert empty.json() == {}
    assert saved.status_code == 204
    assert loaded.json() == state
    assert _initial(page)["userState"] == state


@pytest.mark.parametrize(
    ("key", "headers", "body", "status"),
    [
        ("radar", {}, b"{}", 403),
        ("other", H, b"{}", 404),
        ("radar", H, b"[1]", 400),
        ("radar", H, b"{broken", 400),
        ("radar", H, json.dumps({"x": "y" * (256 * 1024)}).encode(), 413),
    ],
)
async def test_ui_state_rejects_bad_writes(ctx, key, headers, body, status):
    async with _client(ctx[0]) as client:
        response = await client.put(f"/api/ui-state/{key}", headers={**headers, "Content-Type": "application/json"}, content=body)
        stored = await client.get("/api/ui-state/radar")
    assert response.status_code == status
    assert stored.json() == {}


async def test_ui_state_put_is_same_origin_only(ctx):
    async with _client(ctx[0]) as client:
        response = await client.put("/api/ui-state/radar", headers={**H, "Origin": "https://evil.example.com"}, json={})
    assert response.status_code == 403


async def test_unknown_ui_state_key_is_not_found(ctx):
    async with _client(ctx[0]) as client:
        response = await client.get("/api/ui-state/other")
    assert response.status_code == 404


async def test_board_page_embeds_initial_data_safely(ctx):
    ctx_obj, _ = ctx
    ctx_obj.conn.execute("UPDATE mr SET title = '</script><script>alert(1)</script>'")
    ctx_obj.conn.commit()
    async with _client(ctx_obj) as client:
        page = await client.get("/")
    assert "</script><script>alert(1)" not in page.text
    data = _initial(page)
    assert data["mrs"][0]["title"] == "</script><script>alert(1)</script>"
    assert data["radar"]["version"] == 0
    assert data["gitlabUrl"] == "https://gitlab.example.com"


@pytest.mark.parametrize("path", ["/static/board.js", "/static/board-model.js", "/static/radar/view.js", "/static/radar/snooze.js", "/static/radar/links.js"])
async def test_board_modules_are_served_as_javascript(ctx, path):
    async with _client(ctx[0]) as client:
        response = await client.get(path)
    assert response.status_code == 200
    assert "javascript" in response.headers["content-type"]
