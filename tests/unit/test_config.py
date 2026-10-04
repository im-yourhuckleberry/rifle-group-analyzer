"""T1: configuration parsing, loopback enforcement and the `python -m rga` exit code."""

from __future__ import annotations

import re
import subprocess
import sys
from pathlib import Path

import pytest

from rga import __main__ as entrypoint
from rga import config
from rga.config import ConfigError, Settings, load_settings

SRC_CONFIG = Path(config.__file__)


def test_defaults_when_nothing_is_set(project_root: Path) -> None:
    settings = load_settings({})
    assert settings.data_dir == project_root / "data"
    assert (settings.host, settings.port, settings.log_level) == ("127.0.0.1", 8765, "info")
    assert settings.db_path == project_root / "data" / "rga.sqlite3"
    assert settings.photos_dir == project_root / "data" / "photos"
    assert settings.working_dir == project_root / "data" / "working"


def test_defaults_from_the_real_environment(
    monkeypatch: pytest.MonkeyPatch, project_root: Path
) -> None:
    for name in ("RGA_DATA_DIR", "RGA_HOST", "RGA_PORT", "RGA_LOG_LEVEL"):
        monkeypatch.delenv(name, raising=False)
    assert load_settings().data_dir == project_root / "data"


def test_reads_the_process_environment_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("RGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RGA_PORT", "9001")
    settings = load_settings()
    assert (settings.data_dir, settings.port) == (tmp_path.resolve(), 9001)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_hosts_are_accepted(host: str, tmp_path: Path) -> None:
    assert load_settings({"RGA_HOST": host, "RGA_DATA_DIR": str(tmp_path)}).host == host


@pytest.mark.parametrize(
    "host",
    ["0.0.0.0", "::", "192.168.1.20", "10.0.0.5", "example.com", "127.0.0.2", "[::1]", "*", "0"],
)
def test_non_loopback_hosts_are_rejected_naming_the_variable(host: str) -> None:
    with pytest.raises(ConfigError, match="RGA_HOST"):
        load_settings({"RGA_HOST": host})


@pytest.mark.parametrize("port", ["abc", "0", "65536", "-1", "80.5"])
def test_invalid_ports_are_rejected(port: str) -> None:
    with pytest.raises(ConfigError, match="RGA_PORT"):
        load_settings({"RGA_PORT": port})


@pytest.mark.parametrize("port", ["1", "8765", "65535"])
def test_valid_ports_are_accepted(port: str) -> None:
    assert load_settings({"RGA_PORT": port}).port == int(port)


def test_log_level_is_case_insensitive_and_validated() -> None:
    assert load_settings({"RGA_LOG_LEVEL": "DEBUG"}).log_level == "debug"
    with pytest.raises(ConfigError, match="RGA_LOG_LEVEL"):
        load_settings({"RGA_LOG_LEVEL": "verbose"})


@pytest.mark.parametrize("name", ["RGA_DATA_DIR", "RGA_HOST", "RGA_PORT", "RGA_LOG_LEVEL"])
def test_empty_values_fail_loudly_instead_of_defaulting(name: str) -> None:
    with pytest.raises(ConfigError, match=name):
        load_settings({name: "  "})


def test_relative_and_home_data_dirs_become_absolute(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    assert load_settings({"RGA_DATA_DIR": "store"}).data_dir == tmp_path.resolve() / "store"
    monkeypatch.setenv("HOME", str(tmp_path))
    assert load_settings({"RGA_DATA_DIR": "~/d"}).data_dir == tmp_path.resolve() / "d"


def test_settings_constructor_enforces_the_same_rules(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="RGA_HOST"):
        Settings(data_dir=tmp_path, host="0.0.0.0")
    with pytest.raises(ConfigError, match="RGA_PORT"):
        Settings(data_dir=tmp_path, port=0)
    with pytest.raises(ConfigError, match="RGA_LOG_LEVEL"):
        Settings(data_dir=tmp_path, log_level="loud")


def test_unrelated_rga_variables_are_ignored() -> None:
    assert load_settings({"RGA_UNKNOWN": "x", "RGA_DEBUG": "1"}).host == "127.0.0.1"


def test_config_module_mentions_exactly_the_four_documented_variables() -> None:
    names = set(re.findall(r"RGA_[A-Z_]+", SRC_CONFIG.read_text(encoding="utf-8")))
    assert names == {"RGA_DATA_DIR", "RGA_HOST", "RGA_PORT", "RGA_LOG_LEVEL"}


def test_main_returns_2_without_starting_the_server_on_bad_host(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import uvicorn

    def must_not_run(*args: object, **kwargs: object) -> None:
        raise AssertionError("server must not start")

    monkeypatch.setattr(uvicorn, "run", must_not_run)
    monkeypatch.setenv("RGA_HOST", "0.0.0.0")
    assert entrypoint.main() == 2
    assert "RGA_HOST" in capsys.readouterr().err


def test_python_dash_m_rga_exits_2_on_non_loopback_host(project_root: Path, tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "rga"],
        cwd=project_root,
        env={"RGA_HOST": "0.0.0.0", "RGA_DATA_DIR": str(tmp_path), "PATH": "/usr/bin:/bin"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 2
    assert "RGA_HOST" in result.stderr
    assert not (tmp_path / "rga.sqlite3").exists()


def test_main_passes_validated_settings_to_uvicorn(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import uvicorn

    captured: dict[str, object] = {}
    monkeypatch.setattr(uvicorn, "run", lambda app, **kwargs: captured.update(kwargs, app=app))
    monkeypatch.setenv("RGA_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("RGA_PORT", "9123")
    monkeypatch.setenv("RGA_LOG_LEVEL", "warning")
    assert entrypoint.main() == 0
    assert (captured["host"], captured["port"], captured["log_level"]) == (
        "127.0.0.1",
        9123,
        "warning",
    )
    assert (tmp_path / "rga.sqlite3").exists()
