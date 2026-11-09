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
FAST_REFRESH_SEC = 60
MAX_FAST_REFRESH_SEC = 60 * 60
STARTUP_DELAY_SEC = 3.0
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
        startup_delay_sec: float = STARTUP_DELAY_SEC,
    ) -> None:
        self._conn = conn
        self._config_provider = config_provider
        self._api_base = api_base
        self._proxy_secret = proxy_secret
        self._runner = runner
        self._clock = clock or (lambda: asyncio.get_running_loop().time())
        self._wake = asyncio.Event()
        self._fast_since: float | None = None
        self._startup_delay_sec = startup_delay_sec

    def wake(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        if self._startup_delay_sec:
            await asyncio.sleep(self._startup_delay_sec)
        while True:
            self._wake.clear()
            radar = self._config_provider().radar
            delay: float | None = None
            if radar.enabled:
                try:
                    active = await self.refresh_once()
                except Exception:
                    logger.exception("radar refresh crashed")
                    active = False
                delay = self._next_delay(active, radar)
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=delay)
            except asyncio.TimeoutError:
                pass

    def _next_delay(self, active_pipelines: bool, radar: RadarConfig) -> float:
        interval = radar.interval_minutes * 60
        if not (active_pipelines and radar.fast_refresh_while_ci):
            self._fast_since = None
            return interval
        now = self._clock()
        if self._fast_since is None:
            self._fast_since = now
        if now - self._fast_since < MAX_FAST_REFRESH_SEC:
            return min(FAST_REFRESH_SEC, interval)
        return interval

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
