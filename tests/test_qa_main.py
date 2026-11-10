import os
import signal
import socket
import subprocess

import pytest

from lpdev.qa_main import (
    kill_previous_instance,
    pid_alive,
    port_in_use,
    process_start_time,
    qa_pid_path,
    read_pid_entry,
    stop_pid,
    write_pid_entry,
)


def test_read_pid_entry(tmp_path):
    pid_path = tmp_path / "lpdev-qa.pid"
    assert read_pid_entry(pid_path) == (None, "")
    pid_path.write_text("123 Tue Nov 10 12:34:56 2026\n", encoding="ascii")
    assert read_pid_entry(pid_path) == (123, "Tue Nov 10 12:34:56 2026")
    pid_path.write_text("123\n", encoding="ascii")
    assert read_pid_entry(pid_path) == (123, "")
    pid_path.write_text("not-a-pid", encoding="ascii")
    assert read_pid_entry(pid_path) == (None, "")


def test_pid_alive():
    assert pid_alive(os.getpid()) is True
    dead_pid = _spawn_and_wait_killed_process_pid()
    assert pid_alive(dead_pid) is False


def _spawn_and_wait_killed_process_pid() -> int:
    proc = subprocess.Popen(["/bin/sleep", "30"])
    proc.kill()
    proc.wait()
    return proc.pid


def test_process_start_time():
    proc = subprocess.Popen(["/bin/sleep", "30"])
    try:
        start_time = process_start_time(proc.pid)
    finally:
        proc.kill()
        proc.wait()
    assert start_time is not None and start_time
    assert process_start_time(proc.pid) is None


def test_write_pid_entry_creates_file_with_owner_only_mode(tmp_path):
    proc = subprocess.Popen(["/bin/sleep", "30"])
    try:
        pid_path = tmp_path / "lpdev-qa.pid"
        write_pid_entry(pid_path, proc.pid)
        pid, start_time = read_pid_entry(pid_path)
        assert pid == proc.pid
        assert start_time == process_start_time(proc.pid)
        assert (pid_path.stat().st_mode & 0o777) == 0o600
    finally:
        proc.kill()
        proc.wait()


def test_process_start_time_uses_c_locale(monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["env"] = kwargs.get("env")
        return type("Proc", (), {"stdout": b"Wed Nov 10 09:04:42 2026"})()

    monkeypatch.setattr("lpdev.qa_main.subprocess.run", fake_run)
    assert process_start_time(123) == "Wed Nov 10 09:04:42 2026"
    assert seen["env"]["LC_ALL"] == "C"


def test_write_pid_entry_survives_non_ascii_start_time(tmp_path, monkeypatch):
    proc = subprocess.Popen(["/bin/sleep", "30"])
    try:
        monkeypatch.setattr("lpdev.qa_main.process_start_time", lambda pid: "среда ноября")
        pid_path = tmp_path / "lpdev-qa.pid"
        write_pid_entry(pid_path, proc.pid)
        pid, start_time = read_pid_entry(pid_path)
        assert pid == proc.pid
        assert start_time == "????? ??????"
        assert (pid_path.stat().st_mode & 0o777) == 0o600
    finally:
        proc.kill()
        proc.wait()


def test_stop_pid_kills_process():
    proc = subprocess.Popen(["/bin/sleep", "30"])
    stop_pid(proc.pid, timeout_sec=2.0)
    assert proc.poll() is not None
    proc.wait()


def test_stop_pid_gone_is_noop():
    proc = subprocess.Popen(["/bin/sleep", "30"])
    proc.kill()
    proc.wait()
    stop_pid(proc.pid)


def test_kill_previous_instance_dead_pid(tmp_path):
    proc = subprocess.Popen(["/bin/sleep", "30"])
    proc.kill()
    proc.wait()
    pid_path = tmp_path / "lpdev-qa.pid"
    pid_path.write_text(f"{proc.pid}\n", encoding="ascii")
    assert kill_previous_instance(pid_path) is False
    assert not pid_path.exists()


def test_kill_previous_instance_removes_garbage(tmp_path):
    pid_path = tmp_path / "lpdev-qa.pid"
    pid_path.write_text("garbage", encoding="ascii")
    assert kill_previous_instance(pid_path) is False
    assert not pid_path.exists()


def test_kill_previous_instance_stops_live_process(tmp_path, monkeypatch):
    proc = subprocess.Popen(["/bin/sleep", "30"])
    try:
        start_time = process_start_time(proc.pid)
        pid_path = tmp_path / "lpdev-qa.pid"
        pid_path.write_text(f"{proc.pid} {start_time}\n", encoding="ascii")
        stopped = []
        monkeypatch.setattr("lpdev.qa_main.stop_pid", lambda pid, timeout_sec=10.0: stopped.append(pid))
        assert kill_previous_instance(pid_path) is True
        assert stopped == [proc.pid]
        assert not pid_path.exists()
    finally:
        proc.kill()
        proc.wait()


def test_kill_previous_instance_skips_own_pid(tmp_path, monkeypatch):
    pid_path = tmp_path / "lpdev-qa.pid"
    pid_path.write_text(f"{os.getpid()} {process_start_time(os.getpid())}\n", encoding="ascii")

    def fail_stop(pid, timeout_sec=10.0):
        pytest.fail("must not stop own pid")

    monkeypatch.setattr("lpdev.qa_main.stop_pid", fail_stop)
    assert kill_previous_instance(pid_path) is False
    assert not pid_path.exists()


def test_kill_previous_instance_skips_start_time_mismatch(tmp_path, monkeypatch):
    proc = subprocess.Popen(["/bin/sleep", "30"])
    try:
        pid_path = tmp_path / "lpdev-qa.pid"
        pid_path.write_text(f"{proc.pid} Mon Jan  1 00:00:00 1990\n", encoding="ascii")
        stopped = []
        monkeypatch.setattr("lpdev.qa_main.stop_pid", lambda pid, timeout_sec=10.0: stopped.append(pid))
        assert kill_previous_instance(pid_path) is False
        assert stopped == []
        assert not pid_path.exists()
    finally:
        proc.kill()
        proc.wait()


def test_kill_previous_instance_skips_pidfile_without_start_time(tmp_path, monkeypatch):
    proc = subprocess.Popen(["/bin/sleep", "30"])
    try:
        pid_path = tmp_path / "lpdev-qa.pid"
        pid_path.write_text(f"{proc.pid}\n", encoding="ascii")
        stopped = []
        monkeypatch.setattr("lpdev.qa_main.stop_pid", lambda pid, timeout_sec=10.0: stopped.append(pid))
        assert kill_previous_instance(pid_path) is False
        assert stopped == []
        assert not pid_path.exists()
    finally:
        proc.kill()
        proc.wait()


def test_kill_previous_instance_skips_when_ps_unavailable(tmp_path, monkeypatch):
    proc = subprocess.Popen(["/bin/sleep", "30"])
    try:
        pid_path = tmp_path / "lpdev-qa.pid"
        start_time = process_start_time(proc.pid)
        pid_path.write_text(f"{proc.pid} {start_time}\n", encoding="ascii")

        def fail_ps(*args, **kwargs):
            raise OSError("no ps")

        monkeypatch.setattr("lpdev.qa_main.subprocess.run", fail_ps)
        stopped = []
        monkeypatch.setattr("lpdev.qa_main.stop_pid", lambda pid, timeout_sec=10.0: stopped.append(pid))
        assert kill_previous_instance(pid_path) is False
        assert stopped == []
        assert not pid_path.exists()
    finally:
        proc.kill()
        proc.wait()


def test_port_in_use_false_on_free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        sock.listen(1)
        port = sock.getsockname()[1]
        assert port_in_use(port) is True
        with socket.socket() as other:
            other.bind(("127.0.0.1", 0))
            other.listen(1)
            free_port = other.getsockname()[1]
            other.close()
        assert port_in_use(free_port) is False


def test_qa_pid_path(tmp_path):
    assert qa_pid_path(tmp_path / "lpdev.db") == tmp_path / "lpdev-qa.pid"


def test_signal_constants_available():
    assert signal.SIGTERM is not None and signal.SIGKILL is not None
