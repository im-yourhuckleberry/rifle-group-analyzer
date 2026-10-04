"""Start and stop the real server process (`scripts/run.sh`) for T2/T3/T4 tests.

Usage:
    from tests.support.live_server import live_server  # noqa: F401  (session fixture, import explicitly)

    def test_x(live_server):
        assert live_server.request("GET", "/healthz").status == 200

    with run_live_server() as server:   # per-test server; torn down on exit, even on failure
        ...

`LiveServer` is a `str` equal to the base URL (`http://127.0.0.1:<port>`) and also exposes
`port`, `pid`, `pgid`, `data_dir`, `env` (the exact environment given to the child), `request()`
and `stop()`. Each server gets a free loopback port, a throwaway RGA_DATA_DIR, and exactly the
four RGA_* variables that `rga.config` reads; it runs in its own session.

Teardown (`stop()`): SIGTERM, escalating to SIGKILL, then verification by recorded PID and
process group, by a `ps -E` scan for any process still carrying this server's RGA_DATA_DIR
(this finds a server whatever the interpreter binary is called; `pgrep -f "python -m rga"` is
blind to Homebrew's `Python`), and by a port probe; then the temp directory is removed.

Interruption of the test process:
- Exceptions and Ctrl-C (SIGINT -> KeyboardInterrupt) run the normal `stop()`.
- SIGTERM and SIGHUP while a server is running: a handler SIGKILLs the servers' process groups,
  removes their temp directories, restores the previous handler and re-raises the signal, so the
  process still dies from it (exit status is not masked). It is installed only from the main
  thread and only while servers run.
- Interpreter exit with a server still running (a test that never stopped it): an atexit hook
  stops it and says so on stderr.
- SIGKILL of the test process cannot be handled. The server is then orphaned (PPID 1); the next
  `start_live_server()` warns with `OrphanedServerWarning` naming the PIDs.
- `no_leaked_servers` is an opt-in fixture that stops and fails a test that leaves a server running.
"""

from __future__ import annotations

import atexit
import contextlib
import http.client
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import threading
import time
import warnings
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
RUN_SCRIPT = PROJECT_ROOT / "scripts" / "run.sh"
LOOPBACK = "127.0.0.1"
ADDRESS_IN_USE = "address already in use"
PORT_CLOSE_TIMEOUT = 3.0


class LiveServerError(RuntimeError):
    """The server failed to start, never became healthy, or left something behind on teardown."""

    def __init__(
        self,
        message: str,
        *,
        exit_code: int | None = None,
        log: str = "",
        root: Path | None = None,
        port: int | None = None,
    ) -> None:
        super().__init__(message)
        self.exit_code = exit_code
        self.log = log
        self.root = root
        self.port = port


@dataclass(frozen=True)
class HttpResponse:
    """Status, lower-cased headers and body of a response."""

    status: int
    headers: Mapping[str, str]
    body: bytes

    @property
    def text(self) -> str:
        """The body decoded as UTF-8 (undecodable bytes become U+FFFD)."""
        return self.body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        """The body parsed as JSON; raises `ValueError` if it is not valid JSON."""
        return json.loads(self.body)


def free_port() -> int:
    """Return a currently unused loopback TCP port."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((LOOPBACK, 0))
        return sock.getsockname()[1]


pick_port = free_port


def rga_env(*, data_dir: Path, host: str, port: int, log_level: str) -> dict[str, str]:
    """The four variables `rga.config` reads, set explicitly."""
    return {
        "RGA_DATA_DIR": str(data_dir),
        "RGA_HOST": host,
        "RGA_PORT": str(port),
        "RGA_LOG_LEVEL": log_level,
    }


def _url_host(host: str) -> str:
    return f"[{host}]" if ":" in host else host


def _connect_host(host: str) -> str:
    """Loopback address used to reach a server configured with `host`."""
    return "::1" if ":" in host else LOOPBACK


def _port_accepts_connections(host: str, port: int) -> bool:
    try:
        with socket.create_connection((_connect_host(host), port), timeout=1):
            return True
    except OSError:
        return False


@dataclass(frozen=True)
class ProcessInfo:
    """One row of the process table: ids, state and the command line (with its environment, ps -E)."""

    pid: int
    ppid: int
    pgid: int
    command: str
    state: str = ""

    @property
    def zombie(self) -> bool:
        """True for a process that has exited but has not been reaped yet (ps state `Z`)."""
        return self.state.startswith("Z")


class OrphanedServerWarning(UserWarning):
    """A live-server process from an earlier test run survived its parent (PPID 1)."""


def list_processes() -> list[ProcessInfo]:
    """Snapshot of every process via macOS `ps -axwwE`; raises LiveServerError if ps fails."""
    result = subprocess.run(  # noqa: S603  (fixed argv)
        ["ps", "-axwwE", "-o", "pid=,ppid=,pgid=,stat=,command="],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if result.returncode != 0:
        raise LiveServerError(f"ps failed (exit {result.returncode}): {result.stderr.strip()}")
    processes: list[ProcessInfo] = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 4)
        if len(parts) >= 4 and all(part.isdigit() for part in parts[:3]):
            command = parts[4] if len(parts) > 4 else ""
            processes.append(ProcessInfo(int(parts[0]), int(parts[1]), int(parts[2]), command, parts[3]))
    return processes


def processes_for_root(root: Path, *, pgid: int | None = None) -> list[ProcessInfo]:
    """Processes carrying RGA_DATA_DIR under `root`, or belonging to process group `pgid`."""
    marker = f"RGA_DATA_DIR={root}"
    return [p for p in list_processes() if marker in p.command or (pgid is not None and p.pgid == pgid)]


def process_group_alive(pgid: int) -> bool:
    """True if any process in group `pgid` is still running.

    macOS answers `killpg(pgid, 0)` with EPERM, not ESRCH, while the group's only members are
    zombies that the OS has not reaped yet. Such a group is already dead, so EPERM is resolved
    with `ps`: alive only if a member is not a zombie.
    """
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return any(p.pgid == pgid and not p.zombie for p in list_processes())
    return True


def orphaned_servers() -> list[ProcessInfo]:
    """Live-server processes (by RGA_DATA_DIR under an `rga-live-*` dir) whose parent is gone."""
    pattern = re.compile(r"RGA_DATA_DIR=\S*rga-live-")
    return [p for p in list_processes() if p.ppid == 1 and pattern.search(p.command)]


_ACTIVE: dict[int, LiveServer] = {}
_PREVIOUS_HANDLERS: dict[int, Any] = {}
_HANDLED_SIGNALS = (signal.SIGTERM, signal.SIGHUP)
_orphan_check_done = False


def active_servers() -> list[LiveServer]:
    """Servers started in this process and not yet stopped."""
    return list(_ACTIVE.values())


def _register(server: LiveServer) -> None:
    _ACTIVE[server.pid] = server
    if _PREVIOUS_HANDLERS or threading.current_thread() is not threading.main_thread():
        return
    for sig in _HANDLED_SIGNALS:
        previous = signal.getsignal(sig)
        if previous == signal.SIG_IGN:
            continue
        _PREVIOUS_HANDLERS[sig] = signal.SIG_DFL if previous is None else previous
        signal.signal(sig, _on_terminate_signal)


def _unregister(server: LiveServer) -> None:
    _ACTIVE.pop(server.pid, None)
    if _ACTIVE or threading.current_thread() is not threading.main_thread():
        return
    for sig, previous in list(_PREVIOUS_HANDLERS.items()):
        signal.signal(sig, previous)
        del _PREVIOUS_HANDLERS[sig]


def _on_terminate_signal(signum: int, frame: Any) -> None:
    """Kill every running server and its temp dir, then let the signal take its normal course."""
    os.write(2, f"live_server: signal {signum}: killing live servers\n".encode())
    for server in list(_ACTIVE.values()):
        server._emergency_stop()
    signal.signal(signum, _PREVIOUS_HANDLERS.pop(signum, signal.SIG_DFL))
    os.kill(os.getpid(), signum)


def _stop_leaked_servers_at_exit() -> None:
    for server in active_servers():
        sys.stderr.write(f"live_server: stopping leaked server pid={server.pid} root={server.root}\n")
        try:
            server.stop()
        except LiveServerError as exc:
            sys.stderr.write(f"live_server: teardown problem: {exc}\n")


atexit.register(_stop_leaked_servers_at_exit)


def _warn_about_orphans() -> None:
    global _orphan_check_done
    if _orphan_check_done:
        return
    _orphan_check_done = True
    try:
        orphans = orphaned_servers()
    except LiveServerError:
        return
    if orphans:
        pids = " ".join(str(p.pid) for p in orphans)
        warnings.warn(
            f"live servers from an earlier run are still running (PPID 1): {pids}; stop them with: kill {pids}",
            OrphanedServerWarning,
            stacklevel=3,
        )


class LiveServer(str):
    """A running server process; behaves as its base URL string."""

    port: int
    host: str
    pid: int
    pgid: int
    root: Path
    data_dir: Path
    log_path: Path
    env: dict[str, str]
    killed_after_timeout: bool

    def __new__(
        cls,
        *,
        process: subprocess.Popen[bytes],
        host: str,
        port: int,
        root: Path,
        env: dict[str, str],
        terminate_timeout: float,
    ) -> LiveServer:
        obj = super().__new__(cls, f"http://{_url_host(host)}:{port}")
        obj.port = port
        obj.host = host
        obj.pid = process.pid
        obj.pgid = process.pid
        obj.root = root
        obj.data_dir = root / "data"
        obj.log_path = root / "server.log"
        obj.env = env
        obj.killed_after_timeout = False
        obj._process = process
        obj._terminate_timeout = terminate_timeout
        obj._stopped = False
        obj._final_log = ""
        _register(obj)
        return obj

    @property
    def url(self) -> str:
        """Base URL without a trailing slash."""
        return str(self)

    def log_text(self) -> str:
        """Combined stdout/stderr of the server process."""
        if self.log_path.exists():
            return self.log_path.read_text(encoding="utf-8", errors="replace")
        return self._final_log

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | str | None = None,
        timeout: float = 30.0,
    ) -> HttpResponse:
        """Send one request over a plain socket (no proxies); `Host` may be overridden via headers."""
        connection = http.client.HTTPConnection(
            _connect_host(self.host), self.port, timeout=timeout
        )
        try:
            connection.request(method, path, body=body, headers=dict(headers or {}))
            response = connection.getresponse()
            return HttpResponse(
                status=response.status,
                headers={name.lower(): value for name, value in response.getheaders()},
                body=response.read(),
            )
        finally:
            connection.close()

    def stop(self) -> None:
        """Terminate the process and verify nothing is left; idempotent. Raises LiveServerError."""
        if self._stopped:
            return
        self._stopped = True
        problems: list[str] = []
        process: subprocess.Popen[bytes] = self._process
        try:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=self._terminate_timeout)
                except subprocess.TimeoutExpired:
                    self.killed_after_timeout = True
                    self._signal_group(signal.SIGKILL)
                    process.wait(timeout=10)
            group_alive = self._group_alive()
            stragglers = [
                p
                for p in processes_for_root(self.root, pgid=self.pgid)
                if p.pid != self.pid and not p.zombie
            ]
            if group_alive or stragglers:
                self._signal_group(signal.SIGKILL)
                for straggler in stragglers:
                    with contextlib.suppress(ProcessLookupError, PermissionError):
                        os.kill(straggler.pid, signal.SIGKILL)
                where = (
                    f"in the server's process group {self.pgid}"
                    if group_alive
                    else "carrying the server's RGA_DATA_DIR"
                )
                listing = ", ".join(f"{p.pid}: {p.command[:80]}" for p in stragglers) or "unlisted"
                problems.append(f"processes were left {where} ({listing})")
            deadline = time.monotonic() + PORT_CLOSE_TIMEOUT
            while _port_accepts_connections(self.host, self.port):
                if time.monotonic() > deadline:
                    problems.append(f"port {self.port} still accepts connections after teardown")
                    break
                time.sleep(0.1)
        except Exception as exc:
            problems.append(f"teardown error: {exc!r}")
        finally:
            self._final_log = self.log_text()
            shutil.rmtree(self.root, ignore_errors=True)
            _unregister(self)
        if self.root.exists():
            problems.append(f"temp directory {self.root} was not removed")
        if problems:
            raise LiveServerError("; ".join(problems), log=self._final_log)

    def _emergency_stop(self) -> None:
        """Signal-handler-safe teardown: SIGKILL the group, reap, remove the temp dir. No checks."""
        self._stopped = True
        self._signal_group(signal.SIGKILL)
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            try:
                reaped, _status = os.waitpid(self.pid, os.WNOHANG)
            except ChildProcessError:
                break
            if reaped:
                break
            time.sleep(0.01)
        shutil.rmtree(self.root, ignore_errors=True)
        _ACTIVE.pop(self.pid, None)

    def _signal_group(self, sig: signal.Signals) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(self.pgid, sig)

    def _group_alive(self) -> bool:
        return process_group_alive(self.pgid)


def _wait_until_healthy(
    host: str, port: int, process: subprocess.Popen[bytes], timeout: float
) -> str:
    """Poll /healthz; return "healthy", "exited" (process died first) or "timeout"."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process.poll() is not None:
            return "exited"
        try:
            connection = http.client.HTTPConnection(_connect_host(host), port, timeout=1)
            try:
                connection.request("GET", "/healthz")
                response = connection.getresponse()
                body = response.read()
            finally:
                connection.close()
            if response.status == 200 and json.loads(body).get("status") == "ok":
                return "healthy"
        except (OSError, http.client.HTTPException, ValueError):
            pass
        time.sleep(0.1)
    return "timeout"


def _start_once(
    *, host: str, log_level: str, startup_timeout: float, terminate_timeout: float
) -> LiveServer:
    root = Path(tempfile.mkdtemp(prefix="rga-live-"))
    port = pick_port()
    env = {
        "PATH": os.environ["PATH"],
        **rga_env(data_dir=root / "data", host=host, port=port, log_level=log_level),
    }
    with (root / "server.log").open("wb") as log_file:
        process = subprocess.Popen(  # noqa: S603  (fixed argv: the project's own run script)
            [str(RUN_SCRIPT)],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    server = LiveServer(
        process=process,
        host=host,
        port=port,
        root=root,
        env=env,
        terminate_timeout=terminate_timeout,
    )
    try:
        state = _wait_until_healthy(host, port, process, startup_timeout)
        if state != "healthy":
            log = server.log_text()
            what = (
                f"exited with code {process.poll()} during startup"
                if state == "exited"
                else f"did not become healthy within {startup_timeout} s"
            )
            raise LiveServerError(
                f"server {what} (port {port}):\n{log}",
                exit_code=process.poll(),
                log=log,
                root=root,
                port=port,
            )
    except BaseException as exc:
        try:
            server.stop()
        except LiveServerError as cleanup_error:
            exc.add_note(f"cleanup problem: {cleanup_error}")
        raise
    return server


def start_live_server(
    *,
    host: str = LOOPBACK,
    log_level: str = "info",
    startup_timeout: float = 30.0,
    terminate_timeout: float = 20.0,
    attempts: int = 3,
) -> LiveServer:
    """Start the server, retrying on another port if the chosen one was taken in the meantime."""
    _warn_about_orphans()
    last: LiveServerError | None = None
    for _ in range(attempts):
        try:
            return _start_once(
                host=host,
                log_level=log_level,
                startup_timeout=startup_timeout,
                terminate_timeout=terminate_timeout,
            )
        except LiveServerError as exc:
            if ADDRESS_IN_USE not in exc.log.lower():
                raise
            last = exc
    assert last is not None
    raise last


@contextmanager
def run_live_server(**options: Any) -> Iterator[LiveServer]:
    """Context manager around `start_live_server`; always stops and verifies on exit."""
    server = start_live_server(**options)
    try:
        yield server
    finally:
        server.stop()


@pytest.fixture(scope="session")
def live_server() -> Iterator[LiveServer]:
    """Session-scoped real server on a free loopback port with a throwaway data directory."""
    with run_live_server() as server:
        yield server


@pytest.fixture
def no_leaked_servers() -> Iterator[None]:
    """Stop, then fail, any test that leaves a server it started running."""
    before = {server.pid for server in active_servers()}
    yield
    leaked = [server for server in active_servers() if server.pid not in before]
    if not leaked:
        return
    problems: list[str] = []
    for server in leaked:
        try:
            server.stop()
        except LiveServerError as exc:
            problems.append(str(exc))
    pids = ", ".join(str(server.pid) for server in leaked)
    pytest.fail(
        f"test left live server(s) running (pids {pids}; stopped by the fixture) {problems}",
        pytrace=False,
    )
