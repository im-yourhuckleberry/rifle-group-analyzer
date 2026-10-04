"""Suite-wide pytest configuration, owned by the scaffold task.

Guarantees for every test process:
- RGA_* variables exported in the developer's shell are removed (and reported in the header),
  so a stray RGA_HOST or RGA_PORT cannot change what a test exercises.
- RGA_DATA_DIR points at a throwaway directory, so no test can touch <root>/data. Tests of
  config defaults must `monkeypatch.delenv("RGA_DATA_DIR")` explicitly.
- PLAYWRIGHT_BROWSERS_PATH defaults to the project-local browser cache used by bootstrap.sh.
- Tests under tests/e2e carry the `e2e` marker.
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent

os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", str(PROJECT_ROOT / ".playwright-browsers"))

_STRIPPED_VARS: list[str] = []
_SAFETY_DATA_DIR: Path | None = None


def pytest_configure(config: pytest.Config) -> None:
    """Remove ambient RGA_* variables and point RGA_DATA_DIR at a temporary directory."""
    global _SAFETY_DATA_DIR
    for name in sorted(k for k in os.environ if k.startswith("RGA_")):
        del os.environ[name]
        _STRIPPED_VARS.append(name)
    _SAFETY_DATA_DIR = Path(tempfile.mkdtemp(prefix="rga-test-data-"))
    os.environ["RGA_DATA_DIR"] = str(_SAFETY_DATA_DIR)


def pytest_unconfigure(config: pytest.Config) -> None:
    """Delete the throwaway data directory."""
    if _SAFETY_DATA_DIR is not None:
        shutil.rmtree(_SAFETY_DATA_DIR, ignore_errors=True)


def pytest_report_header(config: pytest.Config) -> list[str]:
    """Report the environment overrides applied by this conftest."""
    lines = [f"rga: RGA_DATA_DIR={_SAFETY_DATA_DIR} (temporary)"]
    if _STRIPPED_VARS:
        lines.append(f"rga: ignored ambient env vars: {', '.join(_STRIPPED_VARS)}")
    return lines


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Apply the `e2e` marker to every test under tests/e2e."""
    e2e_dir = PROJECT_ROOT / "tests" / "e2e"
    for item in items:
        if e2e_dir in item.path.resolve().parents:
            item.add_marker(pytest.mark.e2e)


@pytest.fixture(scope="session")
def project_root() -> Path:
    """Absolute path of the repository root."""
    return PROJECT_ROOT
