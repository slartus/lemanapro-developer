import os
import signal
import socket
import subprocess

import pytest

from lpdev.qa_main import (
    kill_previous_instance,
    pid_alive,
    pid_is_ours,
    port_in_use,
    qa_pid_path,
    read_pid,
    stop_pid,
)


def test_read_pid(tmp_path):
    pid_path = tmp_path / "lpdev-qa.pid"
    assert read_pid(pid_path) is None
    pid_path.write_text("123\n", encoding="ascii")
    assert read_pid(pid_path) == 123
    pid_path.write_text("not-a-pid", encoding="ascii")
    assert read_pid(pid_path) is None


def test_pid_alive():
    assert pid_alive(os.getpid()) is True
    dead_pid = _spawn_and_wait_killed_process_pid()
    assert pid_alive(dead_pid) is False


def _spawn_and_wait_killed_process_pid() -> int:
    proc = subprocess.Popen(["/bin/sleep", "30"])
    proc.kill()
    proc.wait()
    return proc.pid


def test_pid_is_ours(monkeypatch):
    monkeypatch.setattr(
        "lpdev.qa_main.subprocess.run",
        lambda *a, **k: type("Proc", (), {"stdout": b"/some/path/lpdev-qa\n"})(),
    )
    assert pid_is_ours(123) is True
    monkeypatch.setattr(
        "lpdev.qa_main.subprocess.run",
        lambda *a, **k: type("Proc", (), {"stdout": b"/Applications/Safari.app/Contents/MacOS/Safari\n"})(),
    )
    assert pid_is_ours(123) is False
    monkeypatch.setattr(
        "lpdev.qa_main.subprocess.run",
        lambda *a, **k: type("Proc", (), {"stdout": b""})(),
    )
    assert pid_is_ours(123) is True

    def slow(*a, **k):
        raise OSError("no ps")

    monkeypatch.setattr("lpdev.qa_main.subprocess.run", slow)
    assert pid_is_ours(123) is True


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
    pid_path = tmp_path / "lpdev-qa.pid"
    pid_path.write_text(f"{proc.pid}\n", encoding="ascii")
    stopped = []
    monkeypatch.setattr("lpdev.qa_main.stop_pid", lambda pid, timeout_sec=10.0: stopped.append(pid))
    monkeypatch.setattr("lpdev.qa_main.pid_is_ours", lambda pid: True)
    assert kill_previous_instance(pid_path) is True
    assert stopped == [proc.pid]
    assert not pid_path.exists()
    proc.kill()
    proc.wait()


def test_kill_previous_instance_skips_foreign_process(tmp_path, monkeypatch):
    pid_path = tmp_path / "lpdev-qa.pid"
    pid_path.write_text(f"{os.getpid()}\n", encoding="ascii")
    stopped = []
    monkeypatch.setattr("lpdev.qa_main.stop_pid", lambda pid, timeout_sec=10.0: stopped.append(pid))
    assert kill_previous_instance(pid_path) is False
    assert stopped == []
    assert not pid_path.exists()


def test_kill_previous_instance_skips_non_lpdev_process(tmp_path, monkeypatch):
    pid_path = tmp_path / "lpdev-qa.pid"
    pid_path.write_text("424242\n", encoding="ascii")
    monkeypatch.setattr("lpdev.qa_main.pid_alive", lambda pid: True)
    monkeypatch.setattr("lpdev.qa_main.pid_is_ours", lambda pid: False)
    stopped = []
    monkeypatch.setattr("lpdev.qa_main.stop_pid", lambda pid, timeout_sec=10.0: stopped.append(pid))
    assert kill_previous_instance(pid_path) is False
    assert stopped == []
    assert not pid_path.exists()


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


def test_kill_previous_instance_ignores_own_pid(tmp_path, monkeypatch):
    pid_path = tmp_path / "lpdev-qa.pid"
    pid_path.write_text(f"{os.getpid()}\n", encoding="ascii")

    def fail_stop(pid, timeout_sec=10.0):
        pytest.fail("must not stop own pid")

    monkeypatch.setattr("lpdev.qa_main.stop_pid", fail_stop)
    monkeypatch.setattr("lpdev.qa_main.pid_is_ours", lambda pid: True)
    assert kill_previous_instance(pid_path) is False
    assert not pid_path.exists()
