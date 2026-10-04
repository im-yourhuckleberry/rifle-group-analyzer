"""T2/T3-local: the live-server support starts the real process and leaves nothing behind.

Covers the happy path (healthz, loopback-only listener, Host/Origin/media-type refusals, security
headers, data dir under the temp root), the setter side of the config-surface contract, and the
teardown guarantees (process, process group, ps environment scan, port, temp dir) including
failure paths, a test that leaks a server, and SIGTERM/SIGHUP/SIGINT sent to a nested pytest.
"""

# ruff: noqa: F811  (the session fixture is imported by name and used as a test parameter)
from __future__ import annotations

import ast
import contextlib
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from rga import config
from rga.web.security import SECURITY_HEADERS
from tests.integration.test_config_surface import _config_runtime_reads, _rga_constants
from tests.support import live_server as live_server_module
from tests.support.live_server import (  # noqa: F401  (fixtures are imported by name)
    LiveServer,
    LiveServerError,
    OrphanedServerWarning,
    ProcessInfo,
    active_servers,
    live_server,
    no_leaked_servers,
    orphaned_servers,
    process_group_alive,
    processes_for_root,
    run_live_server,
)

pytestmark = pytest.mark.usefixtures("no_leaked_servers")

LSOF = "lsof"
# Bounded waits sized for a heavily loaded machine; they only matter when something is broken.
WAIT = 30.0
NESTED_TIMEOUT = 180.0
RESET_SIGNALS = (
    "import signal\n"
    "signal.signal(signal.SIGINT, signal.default_int_handler)\n"
    "signal.signal(signal.SIGTERM, signal.SIG_DFL)\n"
    "signal.signal(signal.SIGHUP, signal.SIG_DFL)\n"
)
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = PROJECT_ROOT / "src" / "rga" / "db" / "migrations"


def _lsof_listeners(port: int) -> list[str]:
    """Names (`host:port`) of TCP LISTEN sockets on `port`, via lsof field output."""
    result = subprocess.run(  # noqa: S603  (fixed argv)
        [LSOF, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fn"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    return [line[1:] for line in result.stdout.splitlines() if line.startswith("n")]


def _require_lsof(capsys: pytest.CaptureFixture[str]) -> None:
    if shutil.which(LSOF) is None:
        with capsys.disabled():
            print("\nLSOF: NOT AVAILABLE")
        pytest.skip("LSOF: NOT AVAILABLE")


def _process_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _connection_refused(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    except ConnectionRefusedError:
        return True
    return False


def _wait_group_gone(pgid: int, timeout: float = WAIT) -> None:
    """Poll until process group `pgid` has no members left, not even unreaped zombies.

    macOS reports EPERM (not ESRCH) from `killpg(pgid, 0)` while the group's only members are
    zombies that launchd has not reaped yet; that means "still there", so keep polling.
    """
    deadline = time.monotonic() + timeout
    while True:
        try:
            os.killpg(pgid, 0)
        except ProcessLookupError:
            return
        except PermissionError:
            pass
        if time.monotonic() > deadline:
            members = [
                f"{p.pid} {p.state} {p.command[:60]}"
                for p in live_server_module.list_processes()
                if p.pgid == pgid
            ]
            pytest.fail(f"process group {pgid} still has members after {timeout} s: {members}")
        time.sleep(0.05)


def _wait_gone(pid: int, timeout: float = WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while _process_exists(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


def _assert_fully_torn_down(pid: int, port: int, root: Path) -> None:
    """Verify by recorded PID and process group, by the ps environment scan, port and temp dir."""
    assert _wait_gone(pid), "server process is still running"
    assert processes_for_root(root, pgid=pid) == [], "processes survive for this server"
    assert _connection_refused(port), "port still accepts connections"
    assert not root.exists(), "temp directory still exists"


# --- the session fixture: a real, healthy, loopback-only server --------------------------------


def test_fixture_yields_the_base_url(live_server: LiveServer) -> None:
    assert isinstance(live_server, str)
    assert live_server == f"http://127.0.0.1:{live_server.port}"
    assert live_server.url == str(live_server)


def test_healthz_is_200_with_the_current_schema_version(live_server: LiveServer) -> None:
    response = live_server.request("GET", "/healthz")
    newest_migration = max(int(path.name.split("_", 1)[0]) for path in MIGRATIONS.glob("*.sql"))
    assert response.status == 200
    assert response.json() == {"status": "ok", "schema_version": newest_migration}


def test_listener_is_bound_to_loopback_only(
    live_server: LiveServer, capsys: pytest.CaptureFixture[str]
) -> None:
    _require_lsof(capsys)
    assert _lsof_listeners(live_server.port) == [f"127.0.0.1:{live_server.port}"]


def test_server_uses_the_temp_data_dir_not_the_projects(live_server: LiveServer) -> None:
    assert live_server.data_dir.parent == live_server.root
    assert (live_server.data_dir / "rga.sqlite3").is_file()
    assert stat.S_IMODE(live_server.data_dir.stat().st_mode) == 0o700
    assert live_server.data_dir != config.DEFAULT_DATA_DIR


@pytest.mark.parametrize("path", ["/healthz", "/no-such-page"])
def test_security_headers_are_present_on_success_and_error_responses(
    live_server: LiveServer, path: str
) -> None:
    response = live_server.request("GET", path)
    for name, value in SECURITY_HEADERS.items():
        assert response.headers.get(name.lower()) == value, name


def test_foreign_host_header_is_refused_with_400(live_server: LiveServer) -> None:
    response = live_server.request("GET", "/healthz", headers={"Host": "evil.example"})
    assert response.status == 400
    for name in SECURITY_HEADERS:
        assert name.lower() in response.headers


def test_cross_origin_write_is_refused_with_403(live_server: LiveServer) -> None:
    response = live_server.request(
        "POST",
        "/api/rifles",
        headers={"Origin": "http://evil.example", "Content-Type": "application/json"},
        body=b"{}",
    )
    assert response.status == 403
    assert response.json()["code"] == "bad_origin"


def test_text_plain_write_is_refused_with_415(live_server: LiveServer) -> None:
    response = live_server.request(
        "POST", "/api/rifles", headers={"Content-Type": "text/plain"}, body=b"x"
    )
    assert response.status == 415
    assert response.json()["code"] == "unsupported_media_type"


def test_same_origin_json_write_passes_the_guards(live_server: LiveServer) -> None:
    host = f"127.0.0.1:{live_server.port}"
    response = live_server.request(
        "POST",
        "/api/rifles",
        headers={"Origin": f"http://{host}", "Content-Type": "application/json"},
        body=b"{}",
    )
    assert response.status not in (400, 403, 415)


# --- config-surface contract, setter side --------------------------------------------------------


def test_child_environment_sets_only_variables_config_reads(live_server: LiveServer) -> None:
    rga_vars = {name for name in live_server.env if name.startswith("RGA_")}
    assert rga_vars == _config_runtime_reads()
    assert live_server.env["RGA_PORT"] == str(live_server.port)
    assert live_server.env["RGA_HOST"] == "127.0.0.1"
    assert live_server.env["RGA_DATA_DIR"] == str(live_server.data_dir)
    assert set(live_server.env) - rga_vars == {"PATH"}


def test_live_server_source_names_only_variables_config_reads() -> None:
    names = _rga_constants(Path(live_server_module.__file__))
    assert names <= _config_runtime_reads()
    tree = ast.parse(Path(live_server_module.__file__).read_text(encoding="utf-8"))
    assert not any(
        isinstance(node, ast.Constant) and isinstance(node.value, str) and node.value == "0.0.0.0"
        for node in ast.walk(tree)
    )


# --- teardown guarantees -----------------------------------------------------------------------


def test_context_manager_leaves_no_process_port_or_directory(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with run_live_server(terminate_timeout=WAIT) as server:
        pid, port, root = server.pid, server.port, server.root
        assert server.request("GET", "/healthz").status == 200
    assert not server.killed_after_timeout, "SIGTERM should stop the server gracefully"
    _assert_fully_torn_down(pid, port, root)
    _require_lsof(capsys)
    assert _lsof_listeners(port) == []


def test_teardown_runs_when_the_test_body_fails() -> None:
    captured: dict[str, object] = {}
    with pytest.raises(RuntimeError, match="boom"), run_live_server() as server:
        captured.update(pid=server.pid, port=server.port, root=server.root)
        raise RuntimeError("boom")
    _assert_fully_torn_down(captured["pid"], captured["port"], captured["root"])  # type: ignore[arg-type]


def test_stop_is_idempotent() -> None:
    with run_live_server() as server:
        server.stop()
        server.stop()
    assert not server.root.exists()


def test_teardown_reports_a_port_that_stays_open_but_still_cleans_up(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(live_server_module, "PORT_CLOSE_TIMEOUT", 0.2)
    server = live_server_module.start_live_server()
    pid, root = server.pid, server.root
    monkeypatch.setattr(live_server_module, "_port_accepts_connections", lambda host, port: True)
    with pytest.raises(LiveServerError, match="still accepts connections"):
        server.stop()
    assert not _process_exists(pid)
    assert not root.exists()


def test_teardown_reports_and_kills_processes_left_in_the_server_group(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wrapper = tmp_path / "run_with_straggler.sh"
    wrapper.write_text(
        f'#!/bin/sh\nsleep 60 >/dev/null 2>&1 &\nexec "{live_server_module.RUN_SCRIPT}"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    monkeypatch.setattr(live_server_module, "RUN_SCRIPT", wrapper)
    server = live_server_module.start_live_server()
    pid, port, root = server.pid, server.port, server.root
    with pytest.raises(LiveServerError, match="left in the server's process group"):
        server.stop()
    _wait_group_gone(pid)
    _assert_fully_torn_down(pid, port, root)


def _ps_state(pid: int) -> str:
    result = subprocess.run(  # noqa: S603  (fixed argv)
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    return result.stdout.strip()


def _unreaped_zombie_group() -> subprocess.Popen[bytes]:
    """A child that leads its own process group, has exited, and has not been waited for."""
    child = subprocess.Popen(  # noqa: S603  (fixed argv)
        [sys.executable, "-c", "pass"], start_new_session=True, stdin=subprocess.DEVNULL
    )
    deadline = time.monotonic() + WAIT
    while not _ps_state(child.pid).startswith("Z"):
        assert time.monotonic() < deadline, "the child never became a zombie"
        time.sleep(0.02)
    return child


def test_a_group_of_unreaped_zombies_is_reported_as_eperm_and_treated_as_dead() -> None:
    child = _unreaped_zombie_group()
    try:
        with pytest.raises(PermissionError):
            os.killpg(child.pid, 0)
        assert process_group_alive(child.pid) is False
    finally:
        child.wait(timeout=WAIT)
    with pytest.raises(ProcessLookupError):
        os.killpg(child.pid, 0)
    assert process_group_alive(child.pid) is False


def test_a_group_with_a_running_member_is_alive() -> None:
    sleeper = subprocess.Popen(  # noqa: S603  (fixed argv)
        ["/bin/sleep", "60"], start_new_session=True, stdin=subprocess.DEVNULL
    )
    try:
        assert process_group_alive(sleeper.pid) is True
    finally:
        sleeper.kill()
        sleeper.wait(timeout=WAIT)
    assert process_group_alive(sleeper.pid) is False


def test_waiting_for_a_group_to_vanish_polls_through_eperm() -> None:
    child = _unreaped_zombie_group()
    reaper = threading.Timer(0.5, child.wait)
    started = time.monotonic()
    reaper.start()
    try:
        _wait_group_gone(child.pid)
    finally:
        reaper.join(timeout=WAIT)
        child.wait(timeout=WAIT)
    assert time.monotonic() - started >= 0.4, "the group was reported gone while a zombie remained"


def test_teardown_ignores_zombies_but_reports_running_stragglers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def listing(state: str):
        return lambda root, pgid=None: [
            ProcessInfo(pid=99999999, ppid=1, pgid=pgid or 0, command="(straggler)", state=state)
        ]

    zombie_server = live_server_module.start_live_server()
    monkeypatch.setattr(live_server_module, "processes_for_root", listing("Z"))
    zombie_server.stop()

    running_server = live_server_module.start_live_server()
    monkeypatch.setattr(live_server_module, "processes_for_root", listing("S"))
    with pytest.raises(LiveServerError, match="carrying the server's RGA_DATA_DIR"):
        running_server.stop()


def test_a_server_that_ignores_sigterm_is_killed_and_still_verified() -> None:
    with run_live_server(terminate_timeout=1.0) as server:
        pid, port, root = server.pid, server.port, server.root
        os.kill(pid, signal.SIGSTOP)
        server.stop()
        assert server.killed_after_timeout
    _assert_fully_torn_down(pid, port, root)


def test_startup_failure_reports_the_server_log_and_cleans_up() -> None:
    with pytest.raises(LiveServerError) as caught, run_live_server(host="0.0.0.0"):
        pytest.fail("a non-loopback host must never start")
    error = caught.value
    assert error.exit_code == 2
    assert "RGA_HOST" in error.log and "loopback only" in error.log
    assert "Uvicorn running" not in error.log
    assert error.root is not None and not error.root.exists()


def test_startup_timeout_kills_the_process_and_cleans_up() -> None:
    with pytest.raises(LiveServerError, match="did not become healthy") as caught:
        live_server_module.start_live_server(startup_timeout=0.01)
    assert caught.value.exit_code is None
    assert caught.value.root is not None and not caught.value.root.exists()
    assert caught.value.port is not None and _connection_refused(caught.value.port)


def test_a_busy_port_is_retried_on_another_port(monkeypatch: pytest.MonkeyPatch) -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as blocker:
        blocker.bind(("127.0.0.1", 0))
        blocker.listen(1)
        busy_port = blocker.getsockname()[1]
        ports = iter([busy_port])
        monkeypatch.setattr(
            live_server_module,
            "pick_port",
            lambda: next(ports, None) or live_server_module.free_port(),
        )
        with run_live_server() as server:
            assert server.port != busy_port
            assert server.request("GET", "/healthz").status == 200
            root = server.root
    assert not root.exists()


def test_two_servers_are_isolated() -> None:
    with run_live_server() as first, run_live_server() as second:
        assert first.port != second.port
        assert first.data_dir != second.data_dir
        assert first.pid != second.pid
        assert first.request("GET", "/healthz").status == 200
        assert second.request("GET", "/healthz").status == 200


# --- residue verification, leaks, orphans, and signals sent to the test process ------------------


def test_process_scan_sees_a_running_server_whatever_the_interpreter_is_called() -> None:
    with run_live_server() as server:
        found = processes_for_root(server.root, pgid=server.pgid)
        assert [p.pid for p in found] == [server.pid]
        assert "-m rga" in found[0].command
        by_pattern = subprocess.run(  # noqa: S603  (fixed argv)
            ["pgrep", "-f", "--", "-m rga"], capture_output=True, text=True, check=False
        )
        assert str(server.pid) in by_pattern.stdout.split()
    assert processes_for_root(server.root, pgid=server.pgid) == []


def test_a_server_that_escaped_the_process_group_is_found_by_its_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pid_file = tmp_path / "straggler.pid"
    wrapper = tmp_path / "run_with_daemon.sh"
    wrapper.write_text(
        "#!/bin/sh\n"
        f"'{sys.executable}' -c 'import os, time; os.setsid(); time.sleep(60)' >/dev/null 2>&1 &\n"
        f"echo $! > {pid_file}\n"
        f'exec "{live_server_module.RUN_SCRIPT}"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    monkeypatch.setattr(live_server_module, "RUN_SCRIPT", wrapper)
    server = live_server_module.start_live_server()
    pid, port, root = server.pid, server.port, server.root
    deadline = time.monotonic() + WAIT
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    straggler = int(pid_file.read_text(encoding="utf-8"))
    try:
        with pytest.raises(LiveServerError, match="carrying the server's RGA_DATA_DIR"):
            server.stop()
        assert _wait_gone(straggler), "the straggler outside the group was not killed"
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(straggler, signal.SIGKILL)
    _assert_fully_torn_down(pid, port, root)


def test_leak_detector_flags_a_server_that_was_never_stopped() -> None:
    before = {s.pid for s in active_servers()}
    server = live_server_module.start_live_server()
    try:
        leaked = [s for s in active_servers() if s.pid not in before]
        assert [s.pid for s in leaked] == [server.pid]
        assert processes_for_root(server.root, pgid=server.pgid)
    finally:
        server.stop()
    assert [s.pid for s in active_servers() if s.pid not in before] == []


def _nested_pytest(tmp_path: Path, test_source: str) -> tuple[subprocess.Popen[bytes], Path, Path]:
    """Run `test_source` in its own pytest process (own session); return (process, info, output).

    Signal dispositions are reset first: a launcher that backgrounds or nohup's the outer pytest
    leaves SIGINT/SIGHUP ignored, which would otherwise be inherited and make the nested
    process unkillable by the signals under test.
    """
    info = tmp_path / "info.json"
    output = tmp_path / "nested-output.txt"
    test_file = tmp_path / "test_nested.py"
    test_file.write_text(test_source.replace("__INFO__", repr(str(info))), encoding="utf-8")
    with output.open("wb") as sink:
        process = subprocess.Popen(  # noqa: S603  (fixed argv)
            [
                sys.executable,
                "-c",
                RESET_SIGNALS + "import sys, pytest\nsys.exit(pytest.main(sys.argv[1:]))",
                str(test_file),
                "-q",
                "-p",
                "no:cacheprovider",
                "-c",
                str(PROJECT_ROOT / "pyproject.toml"),
                "--rootdir",
                str(PROJECT_ROOT),
            ],
            cwd=PROJECT_ROOT,
            env={"PATH": os.environ["PATH"]},
            stdin=subprocess.DEVNULL,
            stdout=sink,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    return process, info, output


def _wait_for_info(process: subprocess.Popen[bytes], info: Path, output: Path) -> dict[str, object]:
    deadline = time.monotonic() + NESTED_TIMEOUT
    while time.monotonic() < deadline:
        if info.exists() and info.read_text(encoding="utf-8").endswith("}"):
            return json.loads(info.read_text(encoding="utf-8"))
        if process.poll() is not None:
            break
        time.sleep(0.1)
    pytest.fail(f"nested pytest never reported a server:\n{output.read_text(errors='replace')}")


def _force_cleanup(process: subprocess.Popen[bytes], info: Path) -> None:
    """Last resort so a failing test cannot itself leave residue."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(process.pid, signal.SIGKILL)
    process.wait(timeout=WAIT)
    if info.exists():
        recorded = json.loads(info.read_text(encoding="utf-8"))
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(recorded["pid"], signal.SIGKILL)
        shutil.rmtree(recorded["root"], ignore_errors=True)


LEAKING_TEST = """
import json, pathlib
import pytest
from tests.support.live_server import no_leaked_servers, start_live_server

pytestmark = pytest.mark.usefixtures("no_leaked_servers")


def test_fails_before_stopping_its_server():
    server = start_live_server()
    pathlib.Path(__INFO__).write_text(
        json.dumps({"pid": server.pid, "root": str(server.root), "port": server.port})
    )
    raise RuntimeError("the test body fails before server.stop()")
"""

HOLDING_TEST = """
import json, pathlib, time
from tests.support.live_server import run_live_server


def test_holds_a_server_until_interrupted():
    with run_live_server() as server:
        pathlib.Path(__INFO__).write_text(
            json.dumps({"pid": server.pid, "root": str(server.root), "port": server.port})
        )
        time.sleep(120)
"""


def test_a_test_that_fails_before_stopping_its_server_is_cleaned_up_and_reported(
    tmp_path: Path,
) -> None:
    """The orphan case: a test body raising between start_live_server() and stop()."""
    process, info, output = _nested_pytest(tmp_path, LEAKING_TEST)
    try:
        assert process.wait(timeout=NESTED_TIMEOUT) == 1
        recorded = json.loads(info.read_text(encoding="utf-8"))
        text = output.read_text(errors="replace")
        assert "left live server(s) running" in text, text
        assert "the test body fails before server.stop()" in text, text
        _assert_fully_torn_down(recorded["pid"], recorded["port"], Path(recorded["root"]))
    finally:
        _force_cleanup(process, info)


@pytest.mark.parametrize(
    ("sent", "expected_returncode"),
    [
        (signal.SIGTERM, -signal.SIGTERM),
        (signal.SIGHUP, -signal.SIGHUP),
        (signal.SIGINT, 2),
    ],
    ids=["SIGTERM", "SIGHUP", "SIGINT"],
)
def test_signals_to_the_test_process_do_not_orphan_the_server(
    sent: signal.Signals, expected_returncode: int, tmp_path: Path
) -> None:
    process, info, output = _nested_pytest(tmp_path, HOLDING_TEST)
    try:
        recorded = _wait_for_info(process, info, output)
        assert processes_for_root(Path(str(recorded["root"])), pgid=int(str(recorded["pid"])))
        os.kill(process.pid, sent)
        returncode = process.wait(timeout=NESTED_TIMEOUT)
        text = output.read_text(errors="replace")
        assert returncode == expected_returncode, text
        _assert_fully_torn_down(
            int(str(recorded["pid"])), int(str(recorded["port"])), Path(str(recorded["root"]))
        )
    finally:
        _force_cleanup(process, info)


def _run_script(source: str) -> subprocess.CompletedProcess[str]:
    """Run `source` in a fresh interpreter (default signal dispositions) that can import the support."""
    return subprocess.run(  # noqa: S603  (fixed argv)
        [sys.executable, "-c", RESET_SIGNALS + source],
        cwd=PROJECT_ROOT,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(PROJECT_ROOT)},
        capture_output=True,
        text=True,
        timeout=NESTED_TIMEOUT,
        check=False,
    )


def _recorded(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    return json.loads(result.stdout.strip().splitlines()[0])


def _kill_recorded(recorded: dict[str, object]) -> None:
    """Last resort so a failing test cannot itself leave a server or temp dir behind."""
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(int(str(recorded["pid"])), signal.SIGKILL)
    shutil.rmtree(str(recorded["root"]), ignore_errors=True)


def test_a_server_never_stopped_is_stopped_at_interpreter_exit() -> None:
    result = _run_script(
        "import json\n"
        "from tests.support.live_server import start_live_server\n"
        "server = start_live_server()\n"
        "print(json.dumps({'pid': server.pid, 'port': server.port, 'root': str(server.root)}), flush=True)\n"
    )
    recorded = _recorded(result)
    try:
        assert result.returncode == 0, result.stderr
        assert "stopping leaked server" in result.stderr
        _assert_fully_torn_down(
            int(str(recorded["pid"])), int(str(recorded["port"])), Path(str(recorded["root"]))
        )
    finally:
        _kill_recorded(recorded)


def test_signal_handlers_exist_only_while_a_server_runs_and_are_restored() -> None:
    result = _run_script(
        "import json, signal\n"
        "from tests.support.live_server import run_live_server\n"
        "names = {'TERM': signal.SIGTERM, 'HUP': signal.SIGHUP}\n"
        "before = {k: repr(signal.getsignal(v)) for k, v in names.items()}\n"
        "with run_live_server() as server:\n"
        "    during = {k: signal.getsignal(v).__name__ for k, v in names.items()}\n"
        "after = {k: repr(signal.getsignal(v)) for k, v in names.items()}\n"
        "print(json.dumps({'before': before, 'during': during, 'after': after}))\n"
    )
    assert result.returncode == 0, result.stderr
    seen = _recorded(result)
    assert seen["during"] == {"TERM": "_on_terminate_signal", "HUP": "_on_terminate_signal"}
    assert seen["before"] == seen["after"]


def test_a_previous_sigterm_handler_still_runs_after_the_servers_are_killed() -> None:
    result = _run_script(
        "import json, os, signal, sys\n"
        "from tests.support.live_server import run_live_server\n"
        "def custom(signum, frame):\n"
        "    print('custom handler ran', flush=True)\n"
        "    sys.exit(7)\n"
        "signal.signal(signal.SIGTERM, custom)\n"
        "with run_live_server() as server:\n"
        "    print(json.dumps({'pid': server.pid, 'port': server.port, 'root': str(server.root)}), flush=True)\n"
        "    os.kill(os.getpid(), signal.SIGTERM)\n"
        "    import time; time.sleep(30)\n"
    )
    recorded = _recorded(result)
    try:
        assert result.returncode == 7, (result.stdout, result.stderr)
        assert "custom handler ran" in result.stdout
        _assert_fully_torn_down(
            int(str(recorded["pid"])), int(str(recorded["port"])), Path(str(recorded["root"]))
        )
    finally:
        _kill_recorded(recorded)


def test_a_signal_ignored_at_startup_stays_ignored_and_the_server_is_still_stopped() -> None:
    """The nohup/background case: no handler is installed for a signal that was ignored on entry."""
    script = (
        "import json, os, signal, time\n"
        "from tests.support.live_server import run_live_server\n"
        "with run_live_server() as server:\n"
        "    ignored = signal.getsignal(signal.SIGHUP) == signal.SIG_IGN\n"
        "    os.kill(os.getpid(), signal.SIGHUP)\n"
        "    time.sleep(0.2)\n"
        "    print(json.dumps({'pid': server.pid, 'port': server.port, 'root': str(server.root),"
        " 'ignored': ignored}), flush=True)\n"
    )
    result = subprocess.run(  # noqa: S603  (fixed argv)
        ["/bin/sh", "-c", f"trap '' HUP; exec '{sys.executable}' -c \"$0\"", script],
        cwd=PROJECT_ROOT,
        env={"PATH": os.environ["PATH"], "PYTHONPATH": str(PROJECT_ROOT)},
        capture_output=True,
        text=True,
        timeout=NESTED_TIMEOUT,
        check=False,
    )
    recorded = _recorded(result)
    try:
        assert result.returncode == 0, (result.stdout, result.stderr)
        assert recorded["ignored"] is True
        _assert_fully_torn_down(
            int(str(recorded["pid"])), int(str(recorded["port"])), Path(str(recorded["root"]))
        )
    finally:
        _kill_recorded(recorded)


def test_starting_a_server_warns_about_an_orphan_from_an_earlier_run(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The warning must come out of `run_live_server()` itself, where it is wired."""
    marker_dir = tmp_path / "rga-live-orphan-demo" / "data"
    launcher = subprocess.run(  # noqa: S603  (fixed argv)
        [
            "/bin/sh",
            "-c",
            f"'{sys.executable}' -c 'import time; time.sleep(60)' >/dev/null 2>&1 & echo $!",
        ],
        env={"PATH": os.environ["PATH"], "RGA_DATA_DIR": str(marker_dir)},
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    orphan_pid = int(launcher.stdout.strip())
    try:
        deadline = time.monotonic() + WAIT
        while orphan_pid not in {p.pid for p in orphaned_servers()}:
            assert time.monotonic() < deadline, "the orphan was never listed"
            time.sleep(0.1)
        monkeypatch.setattr(live_server_module, "_orphan_check_done", False)
        with (
            pytest.warns(OrphanedServerWarning, match=str(orphan_pid)),
            run_live_server() as server,
        ):
            assert server.request("GET", "/healthz").status == 200
    finally:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.kill(orphan_pid, signal.SIGKILL)
    assert _wait_gone(orphan_pid)


def test_starting_a_server_warns_after_the_previous_test_process_was_sigkilled(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The declared SIGKILL case end to end: a real server orphaned by a killed nested pytest."""
    process, info, output = _nested_pytest(tmp_path, HOLDING_TEST)
    try:
        recorded = _wait_for_info(process, info, output)
        orphan_pid = int(str(recorded["pid"]))
        os.kill(process.pid, signal.SIGKILL)
        assert process.wait(timeout=WAIT) == -signal.SIGKILL
        deadline = time.monotonic() + WAIT
        while orphan_pid not in {p.pid for p in orphaned_servers()}:
            assert time.monotonic() < deadline, "the server never showed up as an orphan"
            time.sleep(0.1)
        monkeypatch.setattr(live_server_module, "_orphan_check_done", False)
        with (
            pytest.warns(OrphanedServerWarning, match=str(orphan_pid)),
            run_live_server() as server,
        ):
            assert server.request("GET", "/healthz").status == 200
    finally:
        _force_cleanup(process, info)
    root = Path(str(recorded["root"]))
    assert _wait_gone(orphan_pid)
    assert processes_for_root(root, pgid=orphan_pid) == []
    assert _connection_refused(int(str(recorded["port"])))
    assert not root.exists()


def test_every_public_name_in_the_support_module_has_a_docstring() -> None:
    tree = ast.parse(Path(live_server_module.__file__).read_text(encoding="utf-8"))
    missing: list[str] = []

    def visit(node: ast.AST, prefix: str = "") -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                public = not child.name.startswith("_")
                if public and ast.get_docstring(child) is None and child.name != "__init__":
                    missing.append(f"{prefix}{child.name}")
                visit(child, f"{prefix}{child.name}.")

    visit(tree)
    assert ast.get_docstring(tree) is not None
    assert missing == []
