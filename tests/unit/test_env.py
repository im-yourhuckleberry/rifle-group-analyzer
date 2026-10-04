"""Environment sanity: interpreter, locked dependencies, OpenCV API surface, and the socket guard."""

from __future__ import annotations

import importlib
import importlib.metadata
import io
import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest
from pytest_socket import SocketBlockedError, SocketConnectBlockedError

REQUIRED_MODULES = ("cv2", "numpy", "fastapi", "uvicorn", "PIL", "pillow_heif", "jinja2")
LOCK_PIN = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==([^\s\\]+)", re.MULTILINE)


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def test_python_is_3_14() -> None:
    assert sys.version_info[:2] == (3, 14), sys.version


@pytest.mark.parametrize("module", REQUIRED_MODULES)
def test_runtime_dependency_imports(module: str) -> None:
    importlib.import_module(module)


def test_cv2_version_is_reported(capsys: pytest.CaptureFixture[str]) -> None:
    import cv2

    version = cv2.__version__
    with capsys.disabled():
        print(f"\ncv2 version: {version}")
    assert re.match(r"^[45]\.\d+", version), version


def test_opencv_functions_used_by_the_design_work() -> None:
    import cv2
    import numpy as np

    canvas = np.zeros((200, 200), np.uint8)
    cv2.line(canvas, (10, 100), (190, 100), 255, 1)
    cv2.line(canvas, (100, 10), (100, 190), 255, 1)
    assert cv2.HoughLinesP(canvas, 1, np.pi / 180, 50, minLineLength=60, maxLineGap=3) is not None

    disc = np.zeros((120, 120), np.uint8)
    cv2.ellipse(disc, (60, 60), (30, 20), 25, 0, 360, 255, -1)
    contours, _ = cv2.findContours(disc, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    (cx, cy), (width, height), _angle = cv2.fitEllipse(contours[0])
    assert abs(cx - 60) < 1 and abs(cy - 60) < 1 and abs(max(width, height) - 60) < 2
    assert cv2.pointPolygonTest(contours[0], (60.0, 60.0), False) > 0
    assert cv2.distanceTransform(disc, cv2.DIST_L2, 5).max() > 15

    points = np.vstack([np.random.default_rng(0).normal(c, 1.0, (20, 2)) for c in (0, 20)])
    criteria = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 50, 1e-3)
    _compactness, _labels, centers = cv2.kmeans(
        points.astype(np.float32), 2, None, criteria, 3, cv2.KMEANS_PP_CENTERS
    )
    assert sorted(round(float(c)) for c in centers[:, 0]) == [0, 20]

    src = np.array([[0, 0], [100, 0], [100, 100], [0, 100], [50, 50]], np.float32)
    dst = src * 2.0 + 5.0
    homography, _mask = cv2.findHomography(src, dst, cv2.RANSAC, 1.0)
    warped = cv2.warpPerspective(canvas, homography, (400, 400))
    assert warped.shape == (400, 400)
    gray = cv2.adaptiveThreshold(
        canvas, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
    )
    assert gray.shape == canvas.shape
    assert cv2.connectedComponentsWithStats(disc)[0] == 2


def test_pillow_heif_round_trips_an_image() -> None:
    import pillow_heif
    from PIL import Image

    pillow_heif.register_heif_opener()
    source = Image.new("RGB", (32, 32), (200, 30, 30))
    buffer = io.BytesIO()
    source.save(buffer, format="HEIF")
    buffer.seek(0)
    with Image.open(buffer) as decoded:
        assert decoded.size == (32, 32)


def test_installed_packages_match_the_lock(project_root: Path) -> None:
    lock_text = (project_root / "requirements-dev.lock").read_text(encoding="utf-8")
    locked = {_normalize(n): v for n, v in LOCK_PIN.findall(lock_text)}
    installed = {
        _normalize(dist.metadata["Name"]): dist.version
        for dist in importlib.metadata.distributions()
    }
    assert locked, "no pins parsed from requirements-dev.lock"
    assert installed == locked


def test_rga_is_importable_from_the_venv_without_pythonpath(tmp_path: Path) -> None:
    env = {k: v for k, v in os.environ.items() if k != "PYTHONPATH"}
    result = subprocess.run(  # noqa: S603  (fixed argv, sys.executable)
        [sys.executable, "-c", "import rga; print(rga.__version__)"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip()


@pytest.mark.filterwarnings("ignore:A test tried to use socket:UserWarning")
def test_socket_guard_blocks_outbound_connections() -> None:
    """With --allow-hosts active pytest-socket raises SocketConnectBlockedError (a sibling of
    SocketBlockedError, which it raises when socket creation itself is disabled)."""
    with pytest.raises((SocketBlockedError, SocketConnectBlockedError)):
        socket.create_connection(("1.1.1.1", 53), timeout=2)


def test_socket_guard_allows_loopback() -> None:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        with socket.create_connection(("127.0.0.1", port), timeout=2) as client:
            peer, _ = server.accept()
            peer.close()
            assert client.getpeername()[0] == "127.0.0.1"
