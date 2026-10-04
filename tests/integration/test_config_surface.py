"""Config-surface contract: the env vars `rga.config` reads equal the vars every setter sets.

Reads are measured two ways (runtime access recording and an AST scan of the source). Setters:
`scripts/run.sh` (run against a stub interpreter that dumps its environment), `tests/support/
live_server.py` (its env builder, an AST scan, and the real child process's environment from
`ps -E`), `scripts/smoke.sh` (a static scan of each launch command and a run against a stub
run.sh that records what it receives) and the Makefile (all four or none). Every comparison is
set equality, not a subset: a forgotten RGA_PORT would silently fall back to the default.
Also asserts run.sh never replaces an explicit value and never lets a non-loopback host listen.
"""

from __future__ import annotations

import ast
import os
import re
import shutil
import socket
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest

from rga import config
from tests.support.live_server import rga_env, run_live_server

RGA_NAME = re.compile(r"RGA_[A-Z0-9_]+")
STUB_PYTHON = """#!/bin/sh
echo "ARGV:$*"
env | grep '^RGA_' || true
"""


class _RecordingEnv(Mapping[str, str]):
    """Empty environment that records every key looked up."""

    def __init__(self) -> None:
        self.accessed: set[str] = set()

    def __getitem__(self, key: str) -> str:
        self.accessed.add(key)
        raise KeyError(key)

    def __iter__(self) -> Iterator[str]:
        return iter(())

    def __len__(self) -> int:
        return 0


def _rga_constants(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and RGA_NAME.fullmatch(node.value)
    }


def _config_runtime_reads() -> set[str]:
    env = _RecordingEnv()
    config.load_settings(env)
    return env.accessed


def _stub_tree(tmp_path: Path, project_root: Path, with_python: bool = True) -> Path:
    """Copy run.sh into a scratch tree whose .venv/bin/python is a stub that dumps RGA_* env."""
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    shutil.copy2(project_root / "scripts" / "run.sh", tree / "scripts" / "run.sh")
    if with_python:
        stub = tree / ".venv" / "bin" / "python"
        stub.parent.mkdir(parents=True)
        stub.write_text(STUB_PYTHON, encoding="utf-8")
        stub.chmod(0o755)
    return tree


def _run_stubbed(tree: Path, overrides: Mapping[str, str]) -> tuple[str, dict[str, str]]:
    """Run the copied run.sh with a clean environment; return (argv line, RGA_* env seen)."""
    env = {"PATH": os.environ["PATH"], **overrides}
    result = subprocess.run(  # noqa: S603  (fixed argv)
        ["/bin/bash", str(tree / "scripts" / "run.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.splitlines()
    argv = lines[0].removeprefix("ARGV:")
    seen = dict(line.split("=", 1) for line in lines[1:])
    return argv, seen


def test_config_reads_exactly_the_four_documented_variables() -> None:
    expected = {"RGA_DATA_DIR", "RGA_HOST", "RGA_PORT", "RGA_LOG_LEVEL"}
    assert _config_runtime_reads() == expected
    assert _rga_constants(Path(config.__file__)) == expected


def test_no_module_other_than_config_names_an_rga_variable(project_root: Path) -> None:
    offenders = {
        str(path.relative_to(project_root)): sorted(names)
        for path in (project_root / "src" / "rga").rglob("*.py")
        if path.name != "config.py" and (names := _rga_constants(path))
    }
    assert offenders == {}


def test_run_sh_sets_exactly_the_variables_config_reads(tmp_path: Path, project_root: Path) -> None:
    _argv, seen = _run_stubbed(_stub_tree(tmp_path, project_root), {})
    assert set(seen) == _config_runtime_reads()


def test_run_sh_defaults_match_config_defaults(tmp_path: Path, project_root: Path) -> None:
    tree = _stub_tree(tmp_path, project_root)
    _argv, seen = _run_stubbed(tree, {})
    assert seen["RGA_HOST"] == config.DEFAULT_HOST
    assert seen["RGA_PORT"] == str(config.DEFAULT_PORT)
    assert seen["RGA_LOG_LEVEL"] == config.DEFAULT_LOG_LEVEL
    assert seen["RGA_DATA_DIR"] == str(tree / "data")
    assert project_root / "data" == config.DEFAULT_DATA_DIR


def test_run_sh_execs_the_module_with_no_extra_arguments(
    tmp_path: Path, project_root: Path
) -> None:
    argv, _seen = _run_stubbed(_stub_tree(tmp_path, project_root), {})
    assert argv == "-m rga"


def test_run_sh_keeps_explicit_values(tmp_path: Path, project_root: Path) -> None:
    explicit = {
        "RGA_DATA_DIR": "/tmp/rga explicit dir",
        "RGA_HOST": "::1",
        "RGA_PORT": "9001",
        "RGA_LOG_LEVEL": "debug",
    }
    _argv, seen = _run_stubbed(_stub_tree(tmp_path, project_root), explicit)
    assert seen == explicit


@pytest.mark.parametrize("name", ["RGA_DATA_DIR", "RGA_HOST", "RGA_PORT", "RGA_LOG_LEVEL"])
def test_run_sh_does_not_replace_an_empty_value(
    name: str, tmp_path: Path, project_root: Path
) -> None:
    _argv, seen = _run_stubbed(_stub_tree(tmp_path, project_root), {name: ""})
    assert seen[name] == ""


def test_run_sh_fails_loudly_without_a_venv(tmp_path: Path, project_root: Path) -> None:
    tree = _stub_tree(tmp_path, project_root, with_python=False)
    result = subprocess.run(  # noqa: S603  (fixed argv)
        ["/bin/bash", str(tree / "scripts" / "run.sh")],
        env={"PATH": os.environ["PATH"]},
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    assert result.returncode == 1
    assert "bootstrap.sh" in result.stderr


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"RGA_HOST": "0.0.0.0"}, "RGA_HOST"),
        ({"RGA_HOST": "::"}, "RGA_HOST"),
        ({"RGA_HOST": "192.168.1.5"}, "RGA_HOST"),
        ({"RGA_HOST": "example.com"}, "RGA_HOST"),
        ({"RGA_HOST": ""}, "RGA_HOST"),
        ({"RGA_PORT": "70000"}, "RGA_PORT"),
        ({"RGA_PORT": "not-a-port"}, "RGA_PORT"),
        ({"RGA_LOG_LEVEL": "shout"}, "RGA_LOG_LEVEL"),
        ({"RGA_DATA_DIR": ""}, "RGA_DATA_DIR"),
    ],
)
def test_real_run_sh_rejects_bad_config_without_listening(
    overrides: dict[str, str], message: str, tmp_path: Path, project_root: Path
) -> None:
    data_dir = tmp_path / "data"
    port = _free_port()
    env = {
        "PATH": os.environ["PATH"],
        "RGA_DATA_DIR": str(data_dir),
        "RGA_PORT": str(port),
        **overrides,
    }
    result = subprocess.run(  # noqa: S603  (fixed argv)
        [str(project_root / "scripts" / "run.sh")],
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 2, (result.stdout, result.stderr)
    assert message in result.stderr
    assert "Uvicorn running" not in result.stdout + result.stderr
    assert not data_dir.exists()
    with (
        pytest.raises(ConnectionRefusedError),
        socket.create_connection(("127.0.0.1", port), timeout=2),
    ):
        pass


# --- the other setters: live_server.py, smoke.sh, the Makefile --------------------------------


def _all_rga_tokens(text: str) -> set[str]:
    """Every `RGA_*` name mentioned anywhere in `text` (docs and messages included)."""
    return set(RGA_NAME.findall(text))


def _logical_lines(text: str) -> list[str]:
    """Shell lines with backslash continuations joined."""
    return text.replace("\\\n", " ").splitlines()


def _smoke_launch_environments(text: str) -> list[set[str]]:
    """RGA_* names assigned in front of each `$ROOT/scripts/run.sh` launch in smoke.sh."""
    launch = "$ROOT/scripts/run.sh"
    environments = []
    for line in _logical_lines(text):
        if launch in line and not line.lstrip().startswith("#"):
            before = line.split(launch, 1)[0]
            environments.append(set(re.findall(r"(?<![\w$])(RGA_[A-Z0-9_]+)=", before)))
    return environments


def _makefile_assigned(text: str) -> set[str]:
    """RGA_* names the Makefile assigns, exports, or sets on a recipe command line."""
    assigned = set(re.findall(r"(?m)^[ \t]*(?:export[ \t]+)?(RGA_[A-Z0-9_]+)[ \t]*[:?+]?=", text))
    assigned |= set(re.findall(r"(?m)^[ \t]*export[ \t]+(RGA_[A-Z0-9_]+)\b", text))
    assigned |= set(re.findall(r"(?<![\w$])(RGA_[A-Z0-9_]+)=", text))
    return assigned


def test_live_server_env_builder_sets_exactly_the_variables_config_reads(
    project_root: Path, tmp_path: Path
) -> None:
    env = rga_env(data_dir=tmp_path, host="127.0.0.1", port=12345, log_level="info")
    assert set(env) == _config_runtime_reads()
    support = project_root / "tests" / "support" / "live_server.py"
    assert _rga_constants(support) == _config_runtime_reads()


def test_the_started_server_process_really_has_exactly_those_variables() -> None:
    with run_live_server() as server:
        result = subprocess.run(  # noqa: S603  (fixed argv)
            ["ps", "-wwE", "-o", "command=", "-p", str(server.pid)],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        in_process = set(re.findall(r"(?:^|\s)(RGA_[A-Z0-9_]+)=", result.stdout))
        assert in_process == _config_runtime_reads(), result.stdout


def test_smoke_sh_launches_every_server_with_exactly_the_variables_config_reads(
    project_root: Path,
) -> None:
    text = (project_root / "scripts" / "smoke.sh").read_text(encoding="utf-8")
    environments = _smoke_launch_environments(text)
    assert len(environments) >= 2, "expected the negative start and the real start"
    assert all(names == _config_runtime_reads() for names in environments), environments


def test_smoke_sh_really_passes_those_variables_to_run_sh(
    tmp_path: Path, project_root: Path
) -> None:
    """Run smoke.sh against a stub run.sh that records the RGA_* variables of each launch."""
    tree = tmp_path / "tree"
    (tree / "scripts").mkdir(parents=True)
    (tree / "tests" / "smoke").mkdir(parents=True)
    shutil.copy2(project_root / "scripts" / "smoke.sh", tree / "scripts" / "smoke.sh")
    shutil.copy2(project_root / "tests" / "smoke" / "smoke_roundtrip.py", tree / "tests" / "smoke")
    (tree / ".venv").symlink_to(project_root / ".venv")
    log = tree / "launches.log"
    stub = tree / "scripts" / "run.sh"
    stub.write_text(
        f"#!/bin/sh\n{{ echo LAUNCH; env | grep '^RGA_' | sort; }} >> '{log}'\nexit 2\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    subprocess.run(  # noqa: S603  (fixed argv)
        [str(tree / "scripts" / "smoke.sh")],
        env={"PATH": os.environ["PATH"], "TMPDIR": str(tmp_path)},
        capture_output=True,
        text=True,
        timeout=180,
        check=False,
    )
    launches = [
        {line.split("=", 1)[0] for line in block.splitlines() if line}
        for block in log.read_text(encoding="utf-8").split("LAUNCH\n")[1:]
    ]
    assert len(launches) == 2, launches
    assert all(names == _config_runtime_reads() for names in launches), launches


def test_the_makefile_sets_all_of_the_variables_or_none(project_root: Path) -> None:
    text = (project_root / "Makefile").read_text(encoding="utf-8")
    assigned = _makefile_assigned(text)
    assert assigned in (set(), _config_runtime_reads()), assigned
    assert _all_rga_tokens(text) <= _config_runtime_reads()


def test_no_script_or_test_support_file_names_an_unknown_rga_variable(project_root: Path) -> None:
    files = [
        *(project_root / "scripts").glob("*.sh"),
        project_root / "Makefile",
        *(project_root / "tests" / "support").glob("*.py"),
        *(project_root / "tests" / "smoke").glob("*.py"),
    ]
    unknown = {
        str(path.relative_to(project_root)): sorted(
            _all_rga_tokens(path.read_text(encoding="utf-8")) - _config_runtime_reads()
        )
        for path in files
        if _all_rga_tokens(path.read_text(encoding="utf-8")) - _config_runtime_reads()
    }
    assert unknown == {}
