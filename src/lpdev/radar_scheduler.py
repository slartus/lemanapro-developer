from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import sqlite3
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from lpdev.config import Config, RadarConfig
from lpdev.db import (
    radar_snapshot_get,
    radar_snapshot_save_failure,
    radar_snapshot_save_success,
    utcnow_iso,
)
from lpdev.procutil import OutputLimitExceeded, communicate_capped, kill_group, kill_on_exit, mask_secrets

logger = logging.getLogger(__name__)

RUNNER_PATH = Path(__file__).resolve().parents[2] / "radar" / "src" / "runner.mjs"
MIN_REFRESH_GAP_SEC = 120.0
MAX_RUNNER_OUTPUT_BYTES = 32 * 1024 * 1024
STDERR_TAIL_CHARS = 500

RunnerFn = Callable[[dict[str, Any], RadarConfig], Awaitable[dict[str, Any]]]


class RadarRunnerError(Exception):
    pass


def runner_settings(radar: RadarConfig) -> dict[str, Any]:
    return {
        "projectFilterMode": radar.project_filter_mode,
        "selectedProjectIds": radar.selected_project_ids,
        "ignoredCheckNames": radar.ignored_check_names,
        "approvalThreshold": radar.approval_threshold,
        "hiddenSections": radar.hidden_sections,
    }


async def run_node_runner(payload: dict[str, Any], radar: RadarConfig) -> dict[str, Any]:
    node = shutil.which(radar.node_bin)
    if node is None:
        raise RadarRunnerError(f"node не найден: {radar.node_bin}")
    try:
        proc = await asyncio.create_subprocess_exec(
            node,
            str(RUNNER_PATH),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=str(RUNNER_PATH.parent),
            env={"PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")},
            start_new_session=True,
        )
    except OSError as exc:
        raise RadarRunnerError(f"не удалось запустить node: {exc}") from None
    secret = str(payload.get("token") or "")
    try:
        async with asyncio.timeout(radar.timeout_sec):
            try:
                proc.stdin.write(json.dumps(payload).encode())
                await proc.stdin.drain()
                proc.stdin.close()
            except (BrokenPipeError, ConnectionResetError):
                pass
            out, err = await communicate_capped(proc, MAX_RUNNER_OUTPUT_BYTES)
    except TimeoutError:
        await kill_group(proc)
        raise RadarRunnerError(f"radar не уложился в {radar.timeout_sec} с") from None
    except OutputLimitExceeded:
        await kill_group(proc)
        raise RadarRunnerError("radar вернул слишком большой ответ") from None
    except BaseException:
        await kill_on_exit(proc)
        raise
    if proc.returncode != 0:
        stderr = err.decode("utf-8", "replace")
        if secret:
            stderr = stderr.replace(secret, "***")
        tail = mask_secrets(stderr)[-STDERR_TAIL_CHARS:]
        raise RadarRunnerError(f"radar завершился с кодом {proc.returncode}: {tail}")
    try:
        result = json.loads(out)
    except ValueError:
        raise RadarRunnerError("radar вернул не JSON") from None
    if not isinstance(result, dict):
        raise RadarRunnerError("radar вернул не объект")
    return result


class RadarScheduler:
    def __init__(
        self,
        conn: sqlite3.Connection,
        config_provider: Callable[[], Config],
        api_base: str,
        proxy_secret: str,
        runner: RunnerFn = run_node_runner,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._conn = conn
        self._config_provider = config_provider
        self._api_base = api_base
        self._proxy_secret = proxy_secret
        self._runner = runner
        self._clock = clock or (lambda: asyncio.get_running_loop().time())
        self._refresh_task: asyncio.Task | None = None
        self._last_refresh_done: float | None = None

    def request_refresh(self, force: bool = False) -> bool:
        """Schedule one radar refresh in the background (on-demand mode).

        Returns False when a refresh is already running or the minimal
        gap between refreshes has not elapsed yet (unless force=True).
        """
        if self._refresh_task is not None and not self._refresh_task.done():
            return False
        loop = asyncio.get_running_loop()
        if not force and self._last_refresh_done is not None:
            if self._clock() - self._last_refresh_done < MIN_REFRESH_GAP_SEC:
                return False
        self._refresh_task = loop.create_task(self._refresh_job())
        return True

    async def _refresh_job(self) -> None:
        try:
            await self.refresh_once()
        except Exception:
            logger.exception("radar refresh crashed")
        finally:
            self._last_refresh_done = self._clock()
            self._refresh_task = None

    def _source_of(self, config: Config) -> tuple[str, dict[str, Any]]:
        return config.gitlab.url, runner_settings(config.radar)

    async def refresh_once(self) -> bool:
        config = self._config_provider()
        source = self._source_of(config)
        snapshot = radar_snapshot_get(self._conn)
        payload = {
            "baseUrl": config.gitlab.url,
            "apiBase": self._api_base,
            "token": self._proxy_secret,
            "settings": runner_settings(config.radar),
            "previous": loads_or_none(snapshot["dashboard_json"]) if snapshot else None,
            "threadCache": loads_or_none(snapshot["thread_cache_json"]) if snapshot else None,
        }
        try:
            result = await self._runner(payload, config.radar)
        except RadarRunnerError as exc:
            result = {"ok": False, "error": str(exc)}
        if self._source_of(self._config_provider()) != source:
            logger.info("radar settings changed during refresh, result dropped")
            return False
        dashboard = result.get("dashboard")
        if result.get("ok") and not isinstance(dashboard, dict):
            result = {"ok": False, "error": "radar вернул дашборд неверного формата"}
        if not result.get("ok"):
            error = str(result.get("error") or "неизвестная ошибка radar")
            logger.warning("radar refresh failed: %s", error)
            radar_snapshot_save_failure(self._conn, utcnow_iso(), error)
            return False
        radar_snapshot_save_success(
            self._conn,
            str(dashboard.get("fetchedAt") or utcnow_iso()),
            json.dumps(dashboard, ensure_ascii=False),
            json.dumps(result.get("threadCache"), ensure_ascii=False),
        )
        return bool(result.get("activePipelines"))


def loads_or_none(value: str | None) -> Any:
    if not value:
        return None
    try:
        return json.loads(value)
    except ValueError:
        return None
