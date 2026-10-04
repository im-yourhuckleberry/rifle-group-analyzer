"""T4.0c: wall time of `POST /api/groups/{id}/detect` against a live server (target: median <= 3 s).

Each photo gets a fresh group with no aim. Real photos are read in place from data/real_photos
(uploaded to a throwaway server data directory, never copied elsewhere); synthetic photos are
rendered from the catalog into a temporary directory. The reference is confirmed with the first
proposed candidate: only the time is measured here, not accuracy. Exits 1 if a set's median is over
3 s, or if no photo could be timed.

Run: .venv/bin/python tests/smoke/timing_detect.py
"""

from __future__ import annotations

import datetime
import json
import os
import statistics
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from tests.smoke.smoke_roundtrip import multipart  # noqa: E402
from tests.support.live_server import run_live_server  # noqa: E402

TARGET_S = 3.0
REAL_DIR = ROOT / "data" / "real_photos"
REAL_EXTS = {".jpg", ".jpeg", ".png", ".heic"}
CIRCLE_KINDS = {1.0: "circle_1in", 1.5: "circle_1_5in", 3.0: "circle_3in"}
ALL_KINDS = ["grid_1in", "circle_1_5in", "circle_1in", "circle_3in"]


class ApiFailure(Exception):
    """A non-2xx answer, with the method, path, status and body."""


class Client:
    def __init__(self, base: str) -> None:
        self.base = base

    def call(self, method: str, path: str, body: bytes | None = None, headers: dict | None = None):
        request = urllib.request.Request(  # noqa: S310  (loopback test server)
            self.base + path,
            data=body,
            method=method,
            headers={"Origin": self.base, **(headers or {})},
        )
        try:
            with urllib.request.urlopen(request, timeout=120) as response:  # noqa: S310
                return json.loads(response.read() or b"null")
        except urllib.error.HTTPError as error:
            raise ApiFailure(f"{method} {path} -> {error.code}: {error.read()[:300]!r}") from None

    def json(self, method: str, path: str, payload: dict):
        data = json.dumps(payload).encode()
        return self.call(method, path, data, {"Content-Type": "application/json"})


def setup(client: Client) -> int:
    rifle = client.json("POST", "/api/rifles", {"name": "Timing", "cartridge": ".223 Rem"})
    load = client.json(
        "POST",
        f"/api/rifles/{rifle['id']}/loads",
        {"kind": "factory", "label": "T", "maker": "A", "product": "B", "bullet_weight_gr": 55},
    )
    return load["id"]


def time_detect(client: Client, load_id: int, photo: Path, kinds: list[str]) -> float | None:
    """Seconds for one detect call, or None when no reference candidate could be confirmed."""
    content_type = "image/png" if photo.suffix.lower() == ".png" else "image/jpeg"
    body, headers = multipart(photo.name, content_type, photo.read_bytes())
    uploaded = client.call("POST", "/api/photos", body, headers)
    group = client.json(
        "POST",
        "/api/groups",
        {
            "load_id": load_id,
            "lot_code": "T",
            "shots_expected": 5,
            "shot_date": datetime.date.today().isoformat(),
            "distance_yd": 100,
            "position": "bench",
            "suppressor": False,
        },
    )
    client.json("PATCH", f"/api/groups/{group['id']}", {"photo_id": uploaded["id"]})
    if not confirm_any(client, uploaded["id"], kinds):
        return None
    start = time.perf_counter()
    client.json("POST", f"/api/groups/{group['id']}/detect", {})
    return time.perf_counter() - start


def confirm_any(client: Client, photo_id: int, kinds: list[str]) -> bool:
    """Confirm the first proposed candidate the server accepts (up to three per kind)."""
    for kind in kinds:
        proposal = client.json("POST", f"/api/photos/{photo_id}/reference/propose", {"kind": kind})
        for candidate in proposal["candidates"][:3]:
            try:
                client.json(
                    "POST",
                    f"/api/photos/{photo_id}/reference/confirm",
                    {"kind": kind, "candidate_index": candidate["index"]},
                )
            except ApiFailure:
                continue
            return True
    return False


def synth_photos(out: Path) -> list[tuple[Path, list[str]]]:
    names = subprocess.run(  # noqa: S603  (fixed argv)
        [sys.executable, "-m", "tests.synth.generate", "--list"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stdout.split()
    photos = []
    for name in names:
        subprocess.run(  # noqa: S603
            [
                sys.executable,
                "-m",
                "tests.synth.generate",
                "--scenario",
                name,
                "--seed",
                "0",
                "--out",
                str(out),
            ],
            cwd=ROOT,
            capture_output=True,
            check=True,
            timeout=180,
        )
        truth = json.loads((out / f"{name}.truth.json").read_text())
        reference = truth.get("reference") or {}
        kind = reference.get("kind")
        if kind == "grid":
            kinds = ["grid_1in"]
        elif kind == "circle":
            kinds = [CIRCLE_KINDS.get(float(reference.get("diameter_in", 1.0)), "circle_1in")]
        else:
            kinds = ALL_KINDS
        photos.append((out / f"{name}.png", kinds))
    return photos


def report(label: str, times: list[float], skipped: int) -> bool:
    if not times:
        print(f"{label}: no photo timed ({skipped} without a reference candidate)")
        return False
    median, worst = statistics.median(times), max(times)
    verdict = "PASS" if median <= TARGET_S else "FAIL"
    print(
        f"{label}: n={len(times)} median={median:.2f}s max={worst:.2f}s "
        f"(skipped {skipped} without a reference candidate) {verdict}"
    )
    return median <= TARGET_S


def main() -> int:
    print(f"load average before: {' '.join(f'{x:.2f}' for x in os.getloadavg())}")
    real = (
        sorted(p for p in REAL_DIR.glob("*") if p.suffix.lower() in REAL_EXTS)
        if REAL_DIR.is_dir()
        else []
    )
    ok = True
    timed_any = False
    with (
        tempfile.TemporaryDirectory(prefix="rga-timing-") as tmp,
        run_live_server(log_level="warning") as server,
    ):
        client = Client(str(server).rstrip("/"))
        load_id = setup(client)
        for label, photos in (
            ("real photos", [(p, ALL_KINDS) for p in real]),
            ("synthetic catalog", synth_photos(Path(tmp))),
        ):
            times, skipped = [], 0
            for photo, kinds in photos:
                seconds = time_detect(client, load_id, photo, kinds)
                if seconds is None:
                    skipped += 1
                else:
                    times.append(seconds)
            timed_any = timed_any or bool(times)
            passed = report(label, times, skipped)
            if times:
                ok = ok and passed
    print(f"load average after: {' '.join(f'{x:.2f}' for x in os.getloadavg())}")
    return 0 if ok and timed_any else 1


if __name__ == "__main__":
    sys.exit(main())
