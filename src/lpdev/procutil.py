from __future__ import annotations

import asyncio
import os
import re
import signal

KILL_GRACE_SEC = 5.0
READ_CHUNK_BYTES = 65536
_GLPAT_RE = re.compile(r"glpat-[A-Za-z0-9_\-]+")
_BEARER_RE = re.compile(r"Bearer\s+\S+", re.IGNORECASE)
_OAUTH_RE = re.compile(r"OAuth\s+\S+", re.IGNORECASE)
_YANDEX_TOKEN_RE = re.compile(r"y0_[A-Za-z0-9_\-]+")


def mask_secrets(text: str) -> str:
    text = _GLPAT_RE.sub("glpat-***", text)
    text = _YANDEX_TOKEN_RE.sub("y0_***", text)
    text = _BEARER_RE.sub("Bearer ***", text)
    return _OAUTH_RE.sub("OAuth ***", text)


class OutputLimitExceeded(Exception):
    pass


def sigkill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        pass


async def _discard(stream: asyncio.StreamReader | None) -> None:
    if stream is None:
        return
    while await stream.read(READ_CHUNK_BYTES):
        pass


async def _reap(proc: asyncio.subprocess.Process, timeout: float | None) -> None:
    drains = [asyncio.create_task(_discard(proc.stdout)), asyncio.create_task(_discard(proc.stderr))]
    try:
        await asyncio.wait_for(proc.wait(), timeout=timeout)
    finally:
        for task in drains:
            task.cancel()
        await asyncio.gather(*drains, return_exceptions=True)


async def kill_group(proc: asyncio.subprocess.Process) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    try:
        await _reap(proc, KILL_GRACE_SEC)
    except asyncio.TimeoutError:
        sigkill_group(proc)
        await _reap(proc, None)


async def kill_on_exit(proc: asyncio.subprocess.Process) -> None:
    sigkill_group(proc)
    try:
        await _reap(proc, None)
    except BaseException:
        sigkill_group(proc)
        raise


async def communicate_capped(
    proc: asyncio.subprocess.Process, limit_bytes: int
) -> tuple[bytes, bytes]:
    async def drain(stream: asyncio.StreamReader) -> bytes:
        buf = bytearray()
        while chunk := await stream.read(READ_CHUNK_BYTES):
            buf += chunk
            if len(buf) > limit_bytes:
                raise OutputLimitExceeded(f"output exceeded {limit_bytes} bytes")
        return bytes(buf)

    tasks = [
        asyncio.create_task(drain(proc.stdout)),
        asyncio.create_task(drain(proc.stderr)),
    ]
    try:
        out, err = await asyncio.gather(*tasks)
        await proc.wait()
    except BaseException:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise
    return out, err
