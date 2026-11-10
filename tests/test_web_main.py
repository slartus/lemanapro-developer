from __future__ import annotations

import json


import httpx
import pytest
import yaml

from lpdev.db import finish_review
from lpdev.main import create_app


def _initial(page) -> dict:
    raw = page.text.split('<script type="application/json" id="initial-data">', 1)[1].split("</script>", 1)[0]
    return json.loads(raw)


def _client(ctx_obj) -> httpx.AsyncClient:
    app = create_app(ctx_obj)
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123")


async def test_healthz_ok(ctx):
    ctx_obj, _ = ctx
    async with _client(ctx_obj) as client:
        response = await client.get("/healthz")
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["last_poll"] is None
    assert body["config"]
    assert body["data"] == str(ctx_obj.paths.db)


async def test_healthz_reports_config_env(ctx, monkeypatch):
    ctx_obj, _ = ctx
    monkeypatch.setenv("LPDEV_CONFIG", "/tmp/other-profile.yaml")
    async with _client(ctx_obj) as client:
        response = await client.get("/healthz")
    assert response.json()["config"] == "/tmp/other-profile.yaml"


async def test_healthz_degraded_on_error(ctx):
    ctx_obj, _ = ctx
    from lpdev.db import poll_state_set

    poll_state_set(ctx_obj.conn, "poll_last_error", "boom: 1")
    async with _client(ctx_obj) as client:
        response = await client.get("/healthz")
    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


async def test_index_lists_reviews(ctx):
    ctx_obj, review_id = ctx
    async with _client(ctx_obj) as client:
        response = await client.get("/")
    assert response.status_code == 200
    assert "MR 1" in response.text
    assert str(review_id) in response.text


async def test_host_check_blocks_foreign_host(ctx):
    ctx_obj, _ = ctx
    app = create_app(ctx_obj)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://evil.example.com:9999"
    ) as client:
        response = await client.get("/")
    assert response.status_code == 403


async def test_review_detail_page(ctx):
    ctx_obj, review_id = ctx
    finish_review(ctx_obj.conn, review_id, "done", review_md="текст ревью <script>")
    async with _client(ctx_obj) as client:
        response = await client.get(f"/reviews/{review_id}")
    assert response.status_code == 200
    assert "текст ревью" in response.text
    assert "<script>alert" not in response.text


async def test_review_404(ctx):
    ctx_obj, _ = ctx
    async with _client(ctx_obj) as client:
        response = await client.get("/reviews/999")
    assert response.status_code == 404


async def test_retry_requires_header(ctx):
    ctx_obj, review_id = ctx
    finish_review(ctx_obj.conn, review_id, "error", error="x")
    async with _client(ctx_obj) as client:
        forbidden = await client.post(f"/reviews/{review_id}/retry")
        ok = await client.post(
            f"/reviews/{review_id}/retry", headers={"X-Requested-With": "lpdev"}
        )
    assert forbidden.status_code == 403
    assert ok.status_code == 303
    from lpdev.db import get_review

    assert get_review(ctx_obj.conn, review_id)["status"] == "queued"


async def test_index_embeds_severity_counts(ctx):
    ctx_obj, review_id = ctx
    from lpdev.db import finish_review

    finish_review(ctx_obj.conn, review_id, "done", review_md="ok", counts=(1, 2, 0, 3))
    async with _client(ctx_obj) as client:
        response = await client.get("/")
    mr = _initial(response)["mrs"][0]
    assert (mr["crit_count"], mr["major_count"], mr["minor_count"], mr["nit_count"]) == (1, 2, 0, 3)
    assert mr["latest_done_review_id"] == review_id


async def test_force_check_requires_header(ctx, monkeypatch):
    ctx_obj, _ = ctx
    async with _client(ctx_obj) as client:
        response = await client.post("/force-check")
    assert response.status_code == 403


async def test_force_check_reloads_config_and_requests_refresh(ctx, monkeypatch, tmp_path):
    ctx_obj, _ = ctx
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """
gitlab:
  url: https://gitlab.example.com
  pat_command: "printf tok"
projects:
  - gitlab_project_id: 10
    slug: g/p
    agent: agent-a
    re_review_on_push: true
"""
    )
    monkeypatch.setenv("LPDEV_CONFIG", str(config_path))

    class FakePoller:
        def __init__(self):
            self.reloaded = None
            self.refreshes: list[bool] = []

        def reload(self, config, project_ids):
            self.reloaded = (config, project_ids)

        def request_refresh(self, force=False):
            self.refreshes.append(force)
            return True

    app = create_app(ctx_obj)
    fake = FakePoller()
    app.state.poller = fake
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123"
    ) as client:
        response = await client.post(
            "/force-check", headers={"X-Requested-With": "lpdev"}
        )
    assert response.status_code == 303
    assert fake.refreshes == [True]
    assert fake.reloaded is not None
    assert 10 in fake.reloaded[1]
    assert ctx_obj.config.gitlab.url == "https://gitlab.example.com"
    assert 10 in ctx_obj.project_ids


async def test_force_check_bad_config_400(ctx, monkeypatch, tmp_path):
    ctx_obj, _ = ctx
    bad = tmp_path / "bad.yaml"
    bad.write_text("gitlab: [unclosed\n  :::")
    monkeypatch.setenv("LPDEV_CONFIG", str(bad))
    async with _client(ctx_obj) as client:
        response = await client.post(
            "/force-check", headers={"X-Requested-With": "lpdev"}
        )
    assert response.status_code == 400


async def test_settings_page_renders(ctx, tmp_path, monkeypatch):
    ctx_obj, _ = ctx
    monkeypatch.setenv("LPDEV_CONFIG", str(tmp_path / "cfg.yaml"))
    (tmp_path / "cfg.yaml").write_text(
        "gitlab:\n"
        "  url: https://gitlab.example.com\n"
        "projects:\n"
        "  - gitlab_project_id: 10\n"
        "    slug: g/p\n"
        "    agent: agent-a\n",
        encoding="utf-8",
    )
    async with _client(ctx_obj) as client:
        resp = await client.get("/settings")
    assert resp.status_code == 200
    assert "g/p" in resp.text
    assert 'id="push_debounce_sec" value="120" min="0"' in resp.text


async def test_settings_save_requires_header(ctx, tmp_path, monkeypatch):
    ctx_obj, _ = ctx
    monkeypatch.setenv("LPDEV_CONFIG", str(tmp_path / "cfg.yaml"))
    async with _client(ctx_obj) as client:
        resp = await client.post("/settings", json={})
    assert resp.status_code == 403


async def _write_config(tmp_path):
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "gitlab:\n"
        "  url: https://gitlab.example.com\n"
        "  poll_interval_sec: 60\n"
        "projects:\n"
        "  - gitlab_project_id: 10\n"
        "    slug: g/p\n"
        "    agent: agent-a\n",
        encoding="utf-8",
    )
    return cfg


async def test_settings_save_updates_config_and_projects(ctx, tmp_path, monkeypatch):
    ctx_obj, _ = ctx
    cfg = await _write_config(tmp_path)
    monkeypatch.setenv("LPDEV_CONFIG", str(cfg))
    payload = {
        "review": {"timeout_sec": 900, "max_parallel": 2, "clone_depth": 50,
                   "workspace_ttl_days": 3},
        "projects": [
            {"gitlab_project_id": 10, "slug": "g/p", "agent": "agent-a",
             "re_review_on_push": True},
            {"gitlab_project_id": 11, "slug": "g/q", "agent": "agent-a",
             "re_review_on_push": False},
        ],
    }
    async with _client(ctx_obj) as client:
        resp = await client.post(
            "/settings", json=payload, headers={"X-Requested-With": "lpdev"}
        )
    assert resp.status_code == 303
    import yaml as _yaml

    saved = _yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert saved["gitlab"]["poll_interval_sec"] == 60
    assert len(saved["projects"]) == 2
    assert saved["projects"][0]["re_review_on_push"] is True
    assert ctx_obj.config.gitlab.poll_interval_sec == 60
    assert 11 in ctx_obj.project_ids
    assert (tmp_path / "cfg.yaml.bak").is_file()


async def test_settings_save_invalid_rejected(ctx, tmp_path, monkeypatch):
    ctx_obj, _ = ctx
    cfg = await _write_config(tmp_path)
    monkeypatch.setenv("LPDEV_CONFIG", str(cfg))
    payload = {
        "review": {},
        "radar": {"approval_threshold": 21},
        "projects": [{"gitlab_project_id": 10, "slug": "g/p", "agent": "a",
                      "re_review_on_push": False}],
    }
    async with _client(ctx_obj) as client:
        resp = await client.post(
            "/settings", json=payload, headers={"X-Requested-With": "lpdev"}
        )
    assert resp.status_code == 400
    saved = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert saved["gitlab"]["poll_interval_sec"] == 60


class _ProjectResolver:
    def __init__(self, by_path):
        self._by_path = by_path

    async def get_project_by_path(self, path):
        if path not in self._by_path:
            raise LookupError(path)
        return self._by_path[path]


async def test_settings_save_resolves_slug_from_url(ctx, tmp_path, monkeypatch):
    ctx_obj, _ = ctx
    cfg = await _write_config(tmp_path)
    monkeypatch.setenv("LPDEV_CONFIG", str(cfg))
    ctx_obj.api = _ProjectResolver({
        "g/q": {"id": 99, "path_with_namespace": "g/q"},
    })
    payload = {
        "gitlab": {"poll_interval_sec": 60},
        "review": {},
        "projects": [
            {"gitlab_project_id": 10, "slug": "g/p", "agent": "agent-a",
             "re_review_on_push": False},
            {"gitlab_project_id": None, "slug": "https://gitlab.example.com/g/q",
             "agent": "agent-a", "re_review_on_push": False},
        ],
    }
    async with _client(ctx_obj) as client:
        resp = await client.post(
            "/settings", json=payload, headers={"X-Requested-With": "lpdev"}
        )
    assert resp.status_code == 303
    saved = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert saved["projects"][1]["gitlab_project_id"] == 99
    assert saved["projects"][1]["slug"] == "g/q"


async def test_settings_save_resolve_failure_400(ctx, tmp_path, monkeypatch):
    ctx_obj, _ = ctx
    cfg = await _write_config(tmp_path)
    monkeypatch.setenv("LPDEV_CONFIG", str(cfg))
    ctx_obj.api = _ProjectResolver({})
    payload = {
        "gitlab": {"poll_interval_sec": 60},
        "review": {},
        "projects": [
            {"gitlab_project_id": None, "slug": "g/unknown", "agent": "agent-a",
             "re_review_on_push": False},
        ],
    }
    async with _client(ctx_obj) as client:
        resp = await client.post(
            "/settings", json=payload, headers={"X-Requested-With": "lpdev"}
        )
    assert resp.status_code == 400
    saved = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert len(saved["projects"]) == 1


def test_normalize_slug_variants():
    from lpdev.web.routes import _normalize_slug

    url = "https://gitlab.example.com"
    assert _normalize_slug("https://gitlab.example.com/g/p/", url) == "g/p"
    assert _normalize_slug("http://gitlab.example.com/g/p.git", url) == "g/p"
    assert _normalize_slug("gitlab.example.com/g/p", url) == "g/p"
    assert _normalize_slug("https://gitlab.example.com/sub/gitlab-x/p", url) == "sub/gitlab-x/p"
    assert _normalize_slug("https://other.host/g/p", url) == "g/p"
    assert _normalize_slug("g/p", url) == "g/p"
    sub = "https://gitlab.example.com/gitlab"
    assert _normalize_slug("https://gitlab.example.com/gitlab/g/p", sub) == "g/p"


class _TreeApi:
    def __init__(self, tree):
        self._tree = tree

    async def get_project_by_path(self, path):
        if path == "g/p":
            return {"id": 10, "default_branch": "develop"}
        raise LookupError(path)


async def test_project_agents_returns_only_global_readonly(ctx, monkeypatch):
    ctx_obj, _ = ctx
    monkeypatch.setattr("lpdev.web.routes.list_readonly_agents", lambda: ["glob"])
    ctx_obj.api = _TreeApi({".opencode/agents": [{"type": "blob", "name": "evil.md"}]})
    async with _client(ctx_obj) as client:
        resp = await client.get("/project-agents")
    assert resp.status_code == 200
    assert resp.json()["agents"] == ["glob"]


async def test_settings_save_preserves_unknown_sections(ctx, tmp_path, monkeypatch):
    ctx_obj, _ = ctx
    cfg = tmp_path / "cfg.yaml"
    cfg.write_text(
        "gitlab:\n"
        "  url: https://gitlab.example.com\n"
        "  pat_command: printf tok\n"
        "projects:\n"
        "  - gitlab_project_id: 10\n"
        "    slug: g/p\n"
        "    agent: agent-a\n"
        "opencode:\n"
        "  bin: /custom/opencode\n"
        "paths:\n"
        "  db: /custom/db.sqlite\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("LPDEV_CONFIG", str(cfg))
    payload = {
        "gitlab": {"poll_interval_sec": 30},
        "review": {},
        "projects": [{"gitlab_project_id": 10, "slug": "g/p", "agent": "agent-a",
                      "re_review_on_push": False}],
    }
    async with _client(ctx_obj) as client:
        resp = await client.post(
            "/settings", json=payload, headers={"X-Requested-With": "lpdev"}
        )
    assert resp.status_code == 303
    saved = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert saved["gitlab"]["pat_command"] == "printf tok"
    assert saved["gitlab"]["url"] == "https://gitlab.example.com"
    assert saved["opencode"]["bin"] == "/custom/opencode"
    assert saved["paths"]["db"] == "/custom/db.sqlite"


async def test_settings_bad_body_400(ctx, tmp_path, monkeypatch):
    ctx_obj, _ = ctx
    cfg = await _write_config(tmp_path)
    monkeypatch.setenv("LPDEV_CONFIG", str(cfg))
    async with _client(ctx_obj) as client:
        resp = await client.post(
            "/settings",
            content=b"not json",
            headers={"X-Requested-With": "lpdev", "Content-Type": "application/json"},
        )
    assert resp.status_code == 400
    saved = yaml.safe_load(cfg.read_text(encoding="utf-8"))
    assert saved["gitlab"]["poll_interval_sec"] == 60


async def test_api_mrs_and_index_include_roles(ctx):
    ctx_obj, _ = ctx
    ctx_obj.conn.execute("UPDATE mr SET my_roles = 'mentioned,reviewer'")
    ctx_obj.conn.commit()
    async with _client(ctx_obj) as client:
        api = await client.get("/api/mrs")
        page = await client.get("/")
    assert api.json()[0]["my_roles"] == ["mentioned", "reviewer"]
    assert _initial(page)["mrs"][0]["my_roles"] == ["mentioned", "reviewer"]
    assert api.json()[0]["gitlab_project_id"] == 10


async def test_index_embeds_update_time_and_state(ctx):
    ctx_obj, _ = ctx
    async with _client(ctx_obj) as client:
        page = await client.get("/")
    mr = _initial(page)["mrs"][0]
    assert mr["updated_at"] == "2026-01-01T12:00:00Z"
    assert mr["state"] == "opened"


async def test_api_mrs_exposes_updated_at_and_state(ctx):
    ctx_obj, _ = ctx
    async with _client(ctx_obj) as client:
        api = await client.get("/api/mrs")
    row = api.json()[0]
    assert row["updated_at"] == "2026-01-01T12:00:00Z"
    assert row["state"] == "opened"


async def test_api_mrs_and_index_expose_draft(ctx):
    ctx_obj, _ = ctx
    async with _client(ctx_obj) as client:
        plain = await client.get("/")
        assert (await client.get("/api/mrs")).json()[0]["is_draft"] is False
        ctx_obj.conn.execute("UPDATE mr SET is_draft = 1")
        ctx_obj.conn.commit()
        api = await client.get("/api/mrs")
        page = await client.get("/")
    assert api.json()[0]["is_draft"] is True
    assert _initial(plain)["mrs"][0]["is_draft"] is False
    assert _initial(page)["mrs"][0]["is_draft"] is True


async def test_favicon_svg(ctx):
    ctx_obj, _ = ctx
    async with _client(ctx_obj) as client:
        response = await client.get("/favicon.svg")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")
    assert "<svg" in response.text
    assert response.headers["cache-control"] == "public, max-age=86400"


async def test_favicon_ico_served(ctx):
    ctx_obj, _ = ctx
    async with _client(ctx_obj) as client:
        response = await client.get("/favicon.ico")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/svg+xml")


async def test_base_template_links_icon(ctx):
    ctx_obj, _ = ctx
    async with _client(ctx_obj) as client:
        response = await client.get("/")
    assert '<link rel="icon" type="image/svg+xml" href="/favicon.svg">' in response.text


@pytest.mark.parametrize("path", ["/favicon.svg", "/favicon.ico"])
async def test_favicon_host_check(ctx, path):
    ctx_obj, _ = ctx
    app = create_app(ctx_obj)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://evil.example.com:9999"
    ) as client:
        response = await client.get(path)
    assert response.status_code == 403


async def test_api_mrs_and_review_expose_verdict(ctx):
    ctx_obj, review_id = ctx
    finish_review(ctx_obj.conn, review_id, "done", review_md="ok", verdict="REQUEST_CHANGES")
    async with _client(ctx_obj) as client:
        mrs = (await client.get("/api/mrs")).json()
        review = (await client.get(f"/api/reviews/{review_id}")).json()
    assert mrs[0]["verdict"] == "REQUEST_CHANGES"
    assert review["verdict"] == "REQUEST_CHANGES"


async def test_verdict_on_index_data_and_review_page(ctx):
    ctx_obj, review_id = ctx
    finish_review(ctx_obj.conn, review_id, "done", review_md="ok", verdict="APPROVE_WITH_NITS")
    async with _client(ctx_obj) as client:
        index = await client.get("/")
        page = await client.get(f"/reviews/{review_id}")
    assert _initial(index)["mrs"][0]["verdict"] == "APPROVE_WITH_NITS"
    assert 'class="verdict APPROVE_WITH_NITS">approve · nits</span>' in page.text


async def test_verdict_tag_absent_and_untrusted_value_ignored(ctx):
    ctx_obj, review_id = ctx
    finish_review(ctx_obj.conn, review_id, "done", review_md="ok", verdict="<b>x</b>")
    async with _client(ctx_obj) as client:
        index = await client.get("/")
        page = await client.get(f"/reviews/{review_id}")
    assert 'class="verdict ' not in index.text
    assert 'class="verdict ' not in page.text
    assert "<b>x</b>" not in index.text


async def test_mention_url_in_api_and_index_data(ctx):
    ctx_obj, _ = ctx
    url = "https://gitlab.example.com/g/p/-/mr/1#note_5"
    ctx_obj.conn.execute("UPDATE mr SET my_roles = 'mentioned', mention_url = ?", (url,))
    ctx_obj.conn.commit()
    async with _client(ctx_obj) as client:
        api = await client.get("/api/mrs")
        page = await client.get("/")
    assert api.json()[0]["mention_url"] == url
    assert _initial(page)["mrs"][0]["mention_url"] == url


async def test_foreign_mention_url_not_exposed_or_rendered(ctx):
    ctx_obj, _ = ctx
    ctx_obj.conn.execute(
        "UPDATE mr SET my_roles = 'mentioned', mention_url = 'https://evil.example.org/x'"
    )
    ctx_obj.conn.commit()
    async with _client(ctx_obj) as client:
        api = await client.get("/api/mrs")
        page = await client.get("/")
    assert api.json()[0]["mention_url"] == ""
    assert "evil.example.org" not in page.text


def _seed_updated(conn, project_id, values):
    from lpdev.db import upsert_mr

    ids = {}
    for iid, updated in values.items():
        res = upsert_mr(
            conn, project_db_id=project_id, iid=iid, title=f"MR {iid}", author_username="me",
            source_branch="b", web_url="https://x/y", state="opened", head_sha="a" * 40,
            updated_at="2026-01-01T00:00:00Z",
        )
        conn.execute("UPDATE mr SET updated_at = ? WHERE id = ?", (updated, res.mr_id))
        ids[iid] = res.mr_id
    conn.commit()
    return ids


UPDATED_ORDER_CASES = {
    11: "2026-03-01T10:00:00.000Z",
    12: "2026-03-01T12:00:00+03:00",
    13: "2026-03-01T10:00:00.000Z",
    14: "",
    15: "2026-03-02T00:00:00Z",
}


async def test_list_mrs_and_api_sorted_by_updated_at(ctx):
    from lpdev.db import list_mrs

    ctx_obj, _ = ctx
    ids = _seed_updated(ctx_obj.conn, ctx_obj.project_ids[10], UPDATED_ORDER_CASES)
    expected = [ids[15], ids[13], ids[11], ids[12], ids[14]]
    ordered = [r["id"] for r in list_mrs(ctx_obj.conn) if r["id"] in ids.values()]
    assert ordered == expected
    async with _client(ctx_obj) as client:
        api = [r["id"] for r in (await client.get("/api/mrs")).json() if r["id"] in ids.values()]
    assert api == expected


async def test_board_script_polls_with_adaptive_intervals(ctx):
    ctx_obj, _ = ctx
    async with _client(ctx_obj) as client:
        page = await client.get("/")
        script = await client.get("/static/board.js")
    assert '<script type="module" src="/static/board.js"></script>' in page.text
    assert script.status_code == 200
    assert "javascript" in script.headers["content-type"]
    for marker in ("REFRESH_ACTIVE_MS = 5000", "REFRESH_IDLE_MS = 30000", "REFRESH_HIDDEN_MS = 60000", "visibilitychange", "inFlight"):
        assert marker in script.text


async def test_board_marks_reviews_finished_while_hidden(ctx):
    ctx_obj, _ = ctx
    async with _client(ctx_obj) as client:
        script = await client.get("/static/board.js")
        dot = await client.get("/static/favicon-dot.svg")
    assert "function trackFinishedReviews()" in script.text
    assert "/static/favicon-dot.svg" in script.text
    assert dot.status_code == 200


async def test_force_check_reschedules_followup_refreshes(ctx):
    ctx_obj, _ = ctx
    async with _client(ctx_obj) as client:
        script = (await client.get("/static/board.js")).text
    body = script[script.index("async function forceCheck("):]
    body = body[: body.index("\n}\n")]
    assert "'/force-check'" in body
    assert "setTimeout(refresh, delay)" in body


H = {"X-Requested-With": "lpdev"}


def _done(ctx_obj, review_id, session_id="ses_root1"):
    finish_review(ctx_obj.conn, review_id, "done", review_md="ok", session_id=session_id)


async def test_ask_requires_header(ctx):
    ctx_obj, review_id = ctx
    _done(ctx_obj, review_id)
    async with _client(ctx_obj) as client:
        response = await client.post(f"/reviews/{review_id}/questions", json={"question": "q"})
    assert response.status_code == 403
    assert response.text == "forbidden"


async def test_ask_validation_errors(ctx):
    ctx_obj, review_id = ctx
    _done(ctx_obj, review_id)
    url = f"/reviews/{review_id}/questions"
    async with _client(ctx_obj) as client:
        empty = await client.post(url, headers=H, json={"question": " \x00\x07 \n"})
        wrong_type = await client.post(url, headers=H, json={"question": 1})
        not_json = await client.post(url, headers=H, content=b"{oops")
        too_long = await client.post(url, headers=H, json={"question": "я" * 4001})
        missing = await client.post("/reviews/999/questions", headers=H, json={"question": "q"})
    assert empty.status_code == 400
    assert wrong_type.status_code == 400
    assert not_json.status_code == 400
    assert too_long.status_code == 413
    assert missing.status_code == 404
    from lpdev.db import list_questions

    assert list_questions(ctx_obj.conn, review_id) == []


async def test_ask_conflicts(ctx):
    ctx_obj, review_id = ctx
    url = f"/reviews/{review_id}/questions"
    async with _client(ctx_obj) as client:
        not_done = await client.post(url, headers=H, json={"question": "q"})
        _done(ctx_obj, review_id)
        first = await client.post(url, headers=H, json={"question": "q"})
        second = await client.post(url, headers=H, json={"question": "q2"})
    assert not_done.status_code == 409
    assert "завершённого" in not_done.text
    assert first.status_code == 201
    assert second.status_code == 409
    assert "обрабатывается" in second.text


async def test_ask_creates_normalized_question_and_wakes_worker(ctx):
    import asyncio
    import unicodedata

    from lpdev.db import list_questions

    ctx_obj, review_id = ctx
    _done(ctx_obj, review_id)
    app = create_app(ctx_obj)
    app.state.question_event = asyncio.Event()
    raw = "  " + unicodedata.normalize("NFD", "й") + "\x00 a\tb\r\nc\x1b  "
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:8123"
    ) as client:
        response = await client.post(
            f"/reviews/{review_id}/questions", headers=H, json={"question": raw}
        )
    assert response.status_code == 201
    questions = list_questions(ctx_obj.conn, review_id)
    assert response.json() == {"id": questions[0]["id"]}
    assert questions[0]["question"] == "й a\tb\nc"
    assert app.state.question_event.is_set()


async def test_questions_fragment_escapes_and_marks_previous(ctx):
    from lpdev.db import claim_next_question, create_question, finish_question, retry_review

    ctx_obj, review_id = ctx
    conn = ctx_obj.conn
    _done(ctx_obj, review_id)
    old = create_question(conn, review_id, "<script>alert(1)</script>")
    claim_next_question(conn)
    finish_question(conn, old, "done", answer_md="ответ <img src=x onerror=alert(2)>\n`<b>`")
    retry_review(conn, review_id)
    _done(ctx_obj, review_id, session_id="ses_root2")
    create_question(conn, review_id, "новый")
    async with _client(ctx_obj) as client:
        fragment = await client.get(f"/reviews/{review_id}/questions")
        page = await client.get(f"/reviews/{review_id}")
        missing = await client.get("/reviews/999/questions")
    assert fragment.status_code == 200
    assert fragment.headers["cache-control"] == "no-store"
    body = fragment.text
    assert "<script>alert(1)" not in body
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in body
    assert "<img" not in body
    assert "&lt;img src=x onerror=alert(2)&gt;" in body
    assert "<code>&lt;b&gt;</code>" in body
    assert "к предыдущей версии ревью" in body
    assert body.index("<details") < body.index(f'data-id="{old}"') < body.index("</details>")
    assert 'data-status="queued"' in body
    assert "ответ готовится" in body
    assert 'id="qa-feed"' in page.text
    assert "&lt;script&gt;alert(1)" in page.text
    assert missing.status_code == 404


async def test_review_page_ask_form_only_for_done_with_session(ctx):
    ctx_obj, review_id = ctx
    async with _client(ctx_obj) as client:
        queued = await client.get(f"/reviews/{review_id}")
        _done(ctx_obj, review_id)
        done = await client.get(f"/reviews/{review_id}")
    assert 'id="ask-form"' not in queued.text
    assert "вопросы доступны для завершённого ревью" in queued.text
    assert 'id="ask-form"' in done.text
    assert 'maxlength="4000"' in done.text


async def test_api_review_exposes_questions_without_text(ctx):
    from lpdev.db import create_question

    ctx_obj, review_id = ctx
    _done(ctx_obj, review_id)
    question_id = create_question(ctx_obj.conn, review_id, "секретный текст")
    async with _client(ctx_obj) as client:
        response = await client.get(f"/api/reviews/{review_id}")
    assert response.json()["questions"] == [{"id": question_id, "status": "queued"}]
    assert "секретный" not in response.text


async def test_retry_with_instructions(ctx):
    from lpdev.db import get_review

    ctx_obj, review_id = ctx
    _done(ctx_obj, review_id)
    url = f"/reviews/{review_id}/retry"
    async with _client(ctx_obj) as client:
        too_long = await client.post(url, headers=H, json={"instructions": "x" * 2001})
        bad = await client.post(url, headers=H, json={"instructions": 5})
        ok = await client.post(url, headers=H, json={"instructions": "  смотри <b>SQL</b>\x00 "})
        _done(ctx_obj, review_id)
        page = await client.get(f"/reviews/{review_id}")
    assert too_long.status_code == 413
    assert bad.status_code == 400
    assert ok.status_code == 303
    assert get_review(ctx_obj.conn, review_id)["extra_instructions"] == "смотри <b>SQL</b>"
    assert "Доп. инструкции" in page.text
    assert "смотри &lt;b&gt;SQL&lt;/b&gt;" in page.text
    assert "<b>SQL</b>" not in page.text


async def test_retry_blocked_by_active_question(ctx):
    from lpdev.db import create_question, get_review

    ctx_obj, review_id = ctx
    _done(ctx_obj, review_id)
    create_question(ctx_obj.conn, review_id, "q")
    async with _client(ctx_obj) as client:
        response = await client.post(f"/reviews/{review_id}/retry", headers=H)
    assert response.status_code == 409
    assert get_review(ctx_obj.conn, review_id)["status"] == "done"


@pytest.mark.parametrize(("path", "active_href"), [("/", "/"), ("/settings", "/settings")])
async def test_sidebar_marks_current_section(ctx, path, active_href, monkeypatch):
    monkeypatch.setattr("lpdev.web.routes.load_config", lambda *a, **k: ctx[0].config)
    ctx_obj, _ = ctx
    async with _client(ctx_obj) as client:
        response = await client.get(path)
    assert response.status_code == 200
    assert 'id="sidebar"' in response.text
    assert f'<a href="{active_href}" class="active" aria-current="page"' in response.text
    assert response.text.count('aria-current="page"') == 1


async def test_review_page_highlights_merge_requests(ctx):
    ctx_obj, review_id = ctx
    async with _client(ctx_obj) as client:
        response = await client.get(f"/reviews/{review_id}")
    assert '<a href="/" class="active" aria-current="page"' in response.text


class _FakeRadar:
    def __init__(self) -> None:
        self.refreshes: list[bool] = []

    def request_refresh(self, force: bool = False) -> bool:
        self.refreshes.append(force)
        return True


@pytest.mark.parametrize(
    ("update", "force", "expected"),
    [
        ({}, False, 0),
        ({}, True, 1),
        ({"radar": {"approval_threshold": 2}}, False, 1),
        ({"gitlab": {"url": "https://other.example.com"}}, False, 1),
        ({"review": {"timeout_sec": 99}}, False, 0),
    ],
)
async def test_apply_config_requests_radar_refresh_only_when_needed(ctx, update, force, expected):
    from types import SimpleNamespace

    from lpdev.config import Config
    from lpdev.web.routes import _apply_config

    ctx_obj, _ = ctx
    radar = _FakeRadar()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(ctx=ctx_obj, radar=radar)))
    data = ctx_obj.config.model_dump()
    for section, values in update.items():
        data[section] = {**data[section], **values}
    _apply_config(request, Config(**data), force=force)
    assert len(radar.refreshes) == expected
