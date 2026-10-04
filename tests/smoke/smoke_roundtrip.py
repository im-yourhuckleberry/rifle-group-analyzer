#!/usr/bin/env python3
"""Stdlib-only API driver for `scripts/smoke.sh` (T3 local smoke; loopback only).

Usage (run with the project venv):
    smoke_roundtrip.py healthz   --base-url URL --pid N [--timeout 15]
    smoke_roundtrip.py denials   --base-url URL
    smoke_roundtrip.py roundtrip --base-url URL --work-dir DIR --summary-out FILE
    smoke_roundtrip.py settings  --base-url URL
    smoke_roundtrip.py residue   --root DIR [--pgid N]

Every check prints one line, `[<id>] PASS|FAIL|SKIPPED <text>`, and the exit status is 1 if any
check failed. The driver refuses any base URL that is not a loopback address. `roundtrip` renders
the `clean_circle1` fixture through `tests.synth.generate`, drives the real API from rifle to
finalized group, and writes the fixture and sha256 details to `--summary-out` for the on-disk
checks. A step that cannot run because an earlier step failed is reported as FAIL, never skipped.
"""

from __future__ import annotations

import argparse
import datetime
import hashlib
import http.client
import json
import math
import os
import socket
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "src" / "rga" / "db" / "migrations"
SCENARIO = "clean_circle1"
LOOPBACK_HOSTS = ("127.0.0.1", "localhost", "::1")
UPLOAD_CAP_BYTES = 40 * 1024 * 1024
OVERSIZE_BYTES = 41 * 1024 * 1024
ES_TOLERANCE_IN = 0.05
AIM_TOLERANCE_IN = 0.10
MOA_IN_PER_100YD = 1.0471975512
REQUEST_TIMEOUT = 60.0


class Reporter:
    """Prints check lines and counts failures."""

    def __init__(self) -> None:
        self.failures = 0

    def emit(self, check_id: str, status: str, text: str) -> None:
        """Print `[id] STATUS text`; a FAIL is counted."""
        print(f"[{check_id}] {status:<7} {text}", flush=True)
        if status == "FAIL":
            self.failures += 1

    def passed(self, check_id: str, text: str) -> None:
        """Report a passing check."""
        self.emit(check_id, "PASS", text)

    def failed(self, check_id: str, text: str) -> None:
        """Report a failing check."""
        self.emit(check_id, "FAIL", text)

    def skipped(self, check_id: str, reason: str, text: str) -> None:
        """Report a check that did not apply, with its reason."""
        self.emit(check_id, "SKIPPED", f"({reason}) {text}")


@dataclass(frozen=True)
class Response:
    """One HTTP response: status, lower-cased headers and raw body."""

    status: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> dict | list | None:
        """The body as JSON, or None if it is not JSON."""
        try:
            return json.loads(self.body)
        except ValueError:
            return None

    @property
    def code(self) -> str:
        """The `code` of a `{detail, code}` error body, or an empty string."""
        data = self.json()
        return str(data.get("code", "")) if isinstance(data, dict) else ""

    @property
    def route_missing(self) -> bool:
        """True for the framework's own 404/405 (no such route), not an API-level 404."""
        return self.status in (404, 405) and self.code in ("http_404", "http_405", "")

    def describe(self) -> str:
        """Short text for a failure message."""
        return f"{self.status} {self.body[:160].decode('utf-8', errors='replace')}"


class Api:
    """Minimal loopback-only HTTP client built on `http.client` (no proxies, Host overridable)."""

    def __init__(self, base_url: str) -> None:
        parts = urlsplit(base_url)
        if parts.hostname not in LOOPBACK_HOSTS or parts.port is None:
            raise SystemExit(f"smoke_roundtrip: refusing non-loopback base URL {base_url!r}")
        self.host = parts.hostname
        self.port = parts.port

    def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        body: bytes | None = None,
        timeout: float = REQUEST_TIMEOUT,
    ) -> Response:
        """Send one request and read the whole response."""
        connection = http.client.HTTPConnection(self.host, self.port, timeout=timeout)
        try:
            connection.request(method, path, body=body, headers=dict(headers or {}))
            response = connection.getresponse()
            return Response(
                status=response.status,
                headers={name.lower(): value for name, value in response.getheaders()},
                body=response.read(),
            )
        finally:
            connection.close()

    def json(
        self,
        method: str,
        path: str,
        payload: object | None = None,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Response:
        """Send `payload` as a JSON body (or no body when None)."""
        merged = dict(headers or {})
        data = None
        if payload is not None:
            merged["Content-Type"] = "application/json"
            data = json.dumps(payload).encode("utf-8")
        return self.request(method, path, headers=merged, body=data)


def multipart(filename: str, content_type: str, data: bytes) -> tuple[bytes, dict[str, str]]:
    """Build a multipart/form-data body with a single `file` field."""
    boundary = "smoke" + uuid.uuid4().hex
    head = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"{filename}\"\r\n"
        f"Content-Type: {content_type}\r\n\r\n"
    ).encode()
    body = head + data + f"\r\n--{boundary}--\r\n".encode()
    return body, {"Content-Type": f"multipart/form-data; boundary={boundary}"}


def newest_migration() -> int:
    """Highest migration number shipped with the app, which `/healthz` must report."""
    return max(int(path.name.split("_", 1)[0]) for path in MIGRATIONS.glob("*.sql"))


def process_state(pid: int) -> str:
    """ps state of `pid` ("" if it no longer exists)."""
    result = subprocess.run(  # noqa: S603  (fixed argv)
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    return result.stdout.strip()


# --- check 3: healthz ----------------------------------------------------------------------------


def cmd_healthz(args: argparse.Namespace, report: Reporter) -> None:
    """Poll /healthz until it answers (or the server dies / the time runs out), then check the schema."""
    api = Api(args.base_url)
    deadline = time.monotonic() + args.timeout
    started = time.monotonic()
    last = "no answer yet"
    health: dict | None = None
    while time.monotonic() < deadline:
        state = process_state(args.pid)
        if state == "" or state.startswith("Z"):
            last = f"server process {args.pid} exited"
            break
        try:
            response = api.request("GET", "/healthz", timeout=2)
        except (OSError, http.client.HTTPException) as exc:
            last = f"connect failed: {exc}"
        else:
            data = response.json()
            if response.status == 200 and isinstance(data, dict) and data.get("status") == "ok":
                health = data
                break
            last = f"GET /healthz -> {response.describe()}"
        time.sleep(0.1)
    if health is None:
        report.failed("3.2", f"/healthz did not report ok within {args.timeout:g} s ({last})")
        report.failed("3.3", "schema version not checked (server not healthy)")
        return
    report.passed("3.2", f"/healthz answered 200 ok after {time.monotonic() - started:.1f} s")
    expected = newest_migration()
    version = health.get("schema_version")
    if version == expected:
        report.passed("3.3", f"schema_version == {expected} (newest migration)")
    else:
        report.failed("3.3", f"schema_version is {version!r}, expected {expected}")


# --- check 5: denials ----------------------------------------------------------------------------

SECURITY_HEADER_EXPECTATIONS = {
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "no-referrer",
}


def post_oversize_upload(api: Api, size: int) -> Response | None:
    """Stream a `size`-byte multipart upload, reading the answer while sending.

    The server may answer 413 and close before the whole body is sent; the response is collected
    on a reader thread so a reset during the send does not lose it. None means no answer at all.
    """
    boundary = "smoke" + uuid.uuid4().hex
    head = (
        f"--{boundary}\r\nContent-Disposition: form-data; name=\"file\"; filename=\"big.jpg\"\r\n"
        "Content-Type: image/jpeg\r\n\r\n"
    ).encode()
    tail = f"\r\n--{boundary}--\r\n".encode()
    request_head = (
        f"POST /api/photos HTTP/1.1\r\nHost: {api.host}:{api.port}\r\n"
        f"Content-Type: multipart/form-data; boundary={boundary}\r\n"
        f"Content-Length: {len(head) + size + len(tail)}\r\nConnection: close\r\n\r\n"
    ).encode()
    chunks: list[bytes] = []
    with socket.create_connection((api.host, api.port), timeout=REQUEST_TIMEOUT) as sock:

        def read_answer() -> None:
            try:
                while data := sock.recv(65536):
                    chunks.append(data)
            except OSError:
                pass

        reader = threading.Thread(target=read_answer, daemon=True)
        reader.start()
        try:
            sock.sendall(request_head + head)
            block = b"\0" * (1024 * 1024)
            for _ in range(size // len(block)):
                sock.sendall(block)
            sock.sendall(tail)
        except OSError:
            pass
        reader.join(timeout=30)
    raw = b"".join(chunks)
    if not raw.startswith(b"HTTP/"):
        return None
    header_block, _, body = raw.partition(b"\r\n\r\n")
    lines = header_block.decode("latin-1").split("\r\n")
    headers = {k.lower(): v.strip() for k, _, v in (line.partition(":") for line in lines[1:])}
    return Response(status=int(lines[0].split()[1]), headers=headers, body=body)


def cmd_denials(args: argparse.Namespace, report: Reporter) -> None:
    """Foreign Host, cross-origin write, wrong content type, oversize and non-image uploads."""
    api = Api(args.base_url)

    def expect(check_id: str, label: str, response: Response | None, status: int) -> None:
        if response is None:
            report.failed(check_id, f"{label}: no response (expected {status})")
        elif response.status == status:
            code = f" code={response.code}" if response.code else ""
            report.passed(check_id, f"{label} -> {status}{code}")
        else:
            report.failed(check_id, f"{label}: expected {status}, got {response.describe()}")

    refused_host = api.request("GET", "/healthz", headers={"Host": "evil.example"})
    expect("5.1", "GET /healthz with Host: evil.example", refused_host, 400)
    expect(
        "5.2",
        "POST /api/rifles with Origin: http://evil.example",
        api.json("POST", "/api/rifles", {}, headers={"Origin": "http://evil.example"}),
        403,
    )
    expect(
        "5.3",
        "POST /api/rifles with Content-Type: text/plain",
        api.request("POST", "/api/rifles", headers={"Content-Type": "text/plain"}, body=b"x"),
        415,
    )
    expect(
        "5.4",
        f"upload of {OVERSIZE_BYTES // (1024 * 1024)} MiB (cap {UPLOAD_CAP_BYTES // (1024 * 1024)} MiB)",
        post_oversize_upload(api, OVERSIZE_BYTES),
        413,
    )
    body, headers = multipart("notes.txt", "text/plain", b"this is not a picture\n")
    expect("5.5", "upload of a text file", api.request("POST", "/api/photos", headers=headers, body=body), 415)

    problems: list[str] = []
    samples = {
        "/healthz": api.request("GET", "/healthz"),
        "a 404": api.request("GET", "/no-such-page"),
        "the Host refusal": refused_host,
    }
    for label, response in samples.items():
        for name, value in SECURITY_HEADER_EXPECTATIONS.items():
            if response.headers.get(name) != value:
                problems.append(f"{label}: {name}={response.headers.get(name)!r}")
        policy = response.headers.get("content-security-policy", "")
        if "default-src 'self'" not in policy or "frame-ancestors 'none'" not in policy:
            problems.append(f"{label}: content-security-policy={policy[:60]!r}")
    if problems:
        report.failed("5.6", "security headers missing or wrong: " + "; ".join(problems))
    else:
        report.passed("5.6", "security headers present on /healthz, a 404 and the Host refusal")


# --- check 6: the round trip ---------------------------------------------------------------------


class SoftFailure(Exception):
    """A failed assertion after which the remaining steps can still run."""


class HardFailure(Exception):
    """A failure that leaves nothing for the remaining steps to work with."""


def expect_status(response: Response, status: int, what: str) -> None:
    """Raise HardFailure unless `response` has `status`; says so when the route does not exist."""
    if response.status == status:
        return
    if response.route_missing:
        raise HardFailure(f"{what}: route missing ({response.describe()})")
    raise HardFailure(f"{what}: expected {status}, got {response.describe()}")


def body_of(response: Response) -> dict:
    """The JSON object body of a response, or a HardFailure."""
    data = response.json()
    if not isinstance(data, dict):
        raise HardFailure(f"response body is not a JSON object: {response.describe()}")
    return data


def truth_es(truth: dict) -> float:
    """Largest centre-to-centre distance between the truth holes."""
    holes = truth["holes_in"]
    return max(math.dist(a, b) for a in holes for b in holes)


class RoundTrip:
    """The ordered steps of check 6; `ctx` carries ids and values between them."""

    STEPS: tuple[tuple[str, str], ...] = (
        ("6.1", "render the clean_circle1 fixture"),
        ("6.2", "create a rifle"),
        ("6.3", "create a load"),
        ("6.4", "read the group defaults (none yet)"),
        ("6.5", "create a group"),
        ("6.6", "upload the photo"),
        ("6.7", "attach the photo to the group"),
        ("6.8", "propose the reference"),
        ("6.9", "confirm the reference"),
        ("6.10", "detect holes"),
        ("6.11", "fill shortfall"),
        ("6.12", "set the aim point"),
        ("6.13", "save the review"),
        ("6.14", "finalize the group"),
        ("6.15", "read the stats"),
    )

    def __init__(self, api: Api, work_dir: Path, report: Reporter) -> None:
        self.api = api
        self.work_dir = work_dir
        self.report = report
        self.ctx: dict = {}

    def run(self, summary_out: Path) -> None:
        """Run every step in order; after a hard failure the rest are reported as FAIL."""
        blocked_by: str | None = None
        for check_id, label in self.STEPS:
            if blocked_by is not None:
                self.report.failed(check_id, f"{label}: not run (blocked by step {blocked_by})")
                continue
            step = getattr(self, "step_" + check_id.replace(".", "_"))
            try:
                detail = step()
            except SoftFailure as exc:
                self.report.failed(check_id, f"{label}: {exc}")
            except HardFailure as exc:
                self.report.failed(check_id, f"{label}: {exc}")
                blocked_by = check_id
            except Exception as exc:
                self.report.failed(check_id, f"{label}: unexpected {type(exc).__name__}: {exc}")
                blocked_by = check_id
            else:
                self.report.passed(check_id, f"{label}: {detail}")
        summary_out.write_text(json.dumps(self.ctx.get("summary", {})), encoding="utf-8")

    # --- steps -----------------------------------------------------------------------------

    def step_6_1(self) -> str:
        """Render the fixture with the project's generator and read its truth."""
        out = self.work_dir / "fixture"
        result = subprocess.run(  # noqa: S603  (fixed argv)
            [sys.executable, "-m", "tests.synth.generate", "--scenario", SCENARIO, "--seed", "0", "--out", str(out)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=180,
            check=False,
        )
        png = out / f"{SCENARIO}.png"
        meta = out / f"{SCENARIO}.truth.json"
        if result.returncode != 0 or not png.is_file() or not meta.is_file():
            raise HardFailure(f"generator exited {result.returncode}: {result.stderr.strip()[:200]}")
        truth = json.loads(meta.read_text(encoding="utf-8"))
        self.ctx.update(png=png, truth=truth, expected=len(truth["holes_in"]), truth_es=truth_es(truth))
        return f"{png.name}, {self.ctx['expected']} holes, truth ES {self.ctx['truth_es']:.4f} in"

    def step_6_2(self) -> str:
        """Create the rifle with the fixture's bullet diameter."""
        response = self.api.json(
            "POST",
            "/api/rifles",
            {"name": "Smoke rifle", "cartridge": ".223 Rem", "bullet_diameter_in": self.ctx["truth"]["bullet_diameter_in"]},
        )
        expect_status(response, 201, "POST /api/rifles")
        self.ctx["rifle_id"] = body_of(response)["id"]
        return f"rifle {self.ctx['rifle_id']}"

    def step_6_3(self) -> str:
        """Create a factory load on the rifle."""
        response = self.api.json(
            "POST",
            f"/api/rifles/{self.ctx['rifle_id']}/loads",
            {"kind": "factory", "label": "Smoke 55gr", "maker": "Acme", "product": "Test", "bullet_weight_gr": 55},
        )
        expect_status(response, 201, "POST /api/rifles/{id}/loads")
        self.ctx["load_id"] = body_of(response)["id"]
        return f"load {self.ctx['load_id']}"

    def step_6_4(self) -> str:
        """Read the new rifle's group defaults: with no group anywhere, every field is null."""
        response = self.api.json("GET", f"/api/rifles/{self.ctx['rifle_id']}/group-defaults")
        expect_status(response, 200, "GET /api/rifles/{id}/group-defaults")
        defaults = body_of(response)
        filled = {name: value for name, value in defaults.items() if value is not None}
        if filled:
            raise SoftFailure(f"expected no defaults before the first group, got {filled}")
        return f"no defaults yet ({len(defaults)} fields, all null)"

    def step_6_5(self) -> str:
        """Create the group with its own context: the expected count comes from the truth."""
        response = self.api.json(
            "POST",
            "/api/groups",
            {
                "load_id": self.ctx["load_id"],
                "lot_code": "SMOKE-1",
                "shots_expected": self.ctx["expected"],
                "shot_date": datetime.date.today().isoformat(),
                "distance_yd": 100,
                "position": "bench",
                "suppressor": False,
            },
        )
        expect_status(response, 201, "POST /api/groups")
        group = body_of(response)
        self.ctx["group_id"] = group["id"]
        if (group.get("distance_yd"), group.get("shot_at_source")) != (100, "manual"):
            raise SoftFailure(f"the group did not keep its context: {group}")
        return f"group {self.ctx['group_id']} at 100 yd, {self.ctx['expected']} shots expected"

    def step_6_6(self) -> str:
        """Upload the rendered PNG; the stored sha256 must be the file's own."""
        data = self.ctx["png"].read_bytes()
        body, headers = multipart(self.ctx["png"].name, "image/png", data)
        response = self.api.request("POST", "/api/photos", headers=headers, body=body)
        expect_status(response, 201, "POST /api/photos")
        photo = body_of(response)
        self.ctx["photo"] = photo
        digest = hashlib.sha256(data).hexdigest()
        self.ctx["summary"] = {
            "fixture": str(self.ctx["png"]),
            "sha256": photo["sha256"],
            "ext": photo["ext"],
            "bytes": len(data),
        }
        if photo["sha256"] != digest:
            raise SoftFailure(f"stored sha256 {photo['sha256'][:12]} is not the file's {digest[:12]}")
        return f"photo {photo['id']} ({photo['width_px']}x{photo['height_px']} px, sha256 {digest[:12]})"

    def step_6_7(self) -> str:
        """Attach the uploaded photo to the group."""
        response = self.api.json(
            "PATCH", f"/api/groups/{self.ctx['group_id']}", {"photo_id": self.ctx["photo"]["id"]}
        )
        expect_status(response, 200, "PATCH /api/groups/{id}")
        return f"group {self.ctx['group_id']} uses photo {self.ctx['photo']['id']}"

    def step_6_8(self) -> str:
        """Ask for circle candidates; the proposal never confirms anything by itself."""
        photo_id = self.ctx["photo"]["id"]
        response = self.api.json("POST", f"/api/photos/{photo_id}/reference/propose", {"kind": "circle_1in"})
        expect_status(response, 200, "POST /api/photos/{id}/reference/propose")
        candidates = [c for c in body_of(response)["candidates"] if c.get("circle")]
        if not candidates:
            raise HardFailure("no circle candidate was proposed")
        centre = self.ctx["truth"]["reference"]["center_px"]

        def distance(candidate: dict) -> float:
            circle = candidate["circle"]
            return math.hypot(circle["center_x_px"] - centre[0], circle["center_y_px"] - centre[1])

        first = candidates[0]
        pick = first if distance(first) <= 0.15 * self.ctx["truth"]["ppi"] else min(candidates, key=distance)
        self.ctx["candidate_index"] = pick["index"]
        note = "" if pick is first else f"; candidate 0 was not the reference circle, using {pick['index']}"
        return f"{len(candidates)} candidate(s), reference circle is candidate {pick['index']}{note}"

    def step_6_9(self) -> str:
        """Confirm the reference candidate (this builds the working image)."""
        photo_id = self.ctx["photo"]["id"]
        response = self.api.json(
            "POST",
            f"/api/photos/{photo_id}/reference/confirm",
            {"kind": "circle_1in", "candidate_index": self.ctx["candidate_index"]},
        )
        expect_status(response, 200, "POST /api/photos/{id}/reference/confirm")
        photo = body_of(response)["photo"]
        if not photo["reference_confirmed"] or not photo["working_ppi"]:
            raise HardFailure(f"reference not confirmed: {photo}")
        self.ctx["photo"] = photo
        return f"working image {photo['working_w']}x{photo['working_h']} px at {photo['working_ppi']:.0f} ppi"

    def step_6_10(self) -> str:
        """Detect holes over the whole working image."""
        response = self.api.json("POST", f"/api/groups/{self.ctx['group_id']}/detect", {})
        expect_status(response, 200, "POST /api/groups/{id}/detect")
        result = body_of(response)
        self.ctx["frame"] = result["frame"]
        self.ctx["shots"] = result["shots"]
        if result["status"] != "ok":
            raise HardFailure(f"detect status {result['status']!r}: {result['messages']}")
        if len(result["shots"]) != self.ctx["expected"]:
            raise SoftFailure(f"{len(result['shots'])} shots proposed, expected {self.ctx['expected']}")
        return f"{len(result['shots'])} shots proposed, status ok"

    def step_6_11(self) -> str:
        """Fill any shortfall (none is expected for a five-hole clean target)."""
        response = self.api.json("POST", f"/api/groups/{self.ctx['group_id']}/fill-shortfall", None)
        expect_status(response, 200, "POST /api/groups/{id}/fill-shortfall")
        result = body_of(response)
        self.ctx["shots"] = result["shots"]
        estimated = sum(1 for shot in result["shots"] if shot["origin"] == "estimated")
        if result["status"] != "ok":
            raise HardFailure(f"fill status {result['status']!r}: {result['messages']}")
        if len(result["shots"]) != self.ctx["expected"] or estimated:
            raise SoftFailure(f"{len(result['shots'])} shots, {estimated} estimated; expected {self.ctx['expected']}, 0")
        return f"{len(result['shots'])} shots, {estimated} estimated"

    def predicted_aim_in(self) -> tuple[float, float]:
        """The truth aim point in the pipeline's plane (inches), for a similarity-camera fixture.

        The pipeline plane is the paper plane rotated by the camera rotation and shifted; the shift
        is found by matching the centroid of the detected shots to the centroid of the truth holes.
        """
        truth = self.ctx["truth"]
        matrix = truth["h_plane_to_image"]
        if abs(matrix[2][0]) > 1e-9 or abs(matrix[2][1]) > 1e-9:
            raise SoftFailure("fixture has a perspective camera; the truth aim cannot be mapped")
        theta = math.atan2(matrix[1][0], matrix[0][0])
        holes = truth["holes_in"]
        shots = self.ctx["shots"]
        truth_cx = sum(h[0] for h in holes) / len(holes)
        truth_cy = sum(h[1] for h in holes) / len(holes)
        shot_cx = sum(s["x_in"] for s in shots) / len(shots)
        shot_cy = sum(s["y_in"] for s in shots) / len(shots)
        vx = truth["aim_in"][0] - truth_cx
        vy = truth["aim_in"][1] - truth_cy
        return (
            shot_cx + math.cos(theta) * vx - math.sin(theta) * vy,
            shot_cy + math.sin(theta) * vx + math.cos(theta) * vy,
        )

    def step_6_12(self) -> str:
        """Click where the truth aim point should be and let the app snap to the aim mark."""
        aim = self.predicted_aim_in()
        self.ctx["aim"] = aim
        ppi = self.ctx["frame"]["ppi"]
        response = self.api.json(
            "POST",
            f"/api/photos/{self.ctx['photo']['id']}/snap-aim",
            {"x_px": aim[0] * ppi, "y_px": aim[1] * ppi},
        )
        expect_status(response, 200, "POST /api/photos/{id}/snap-aim")
        snap = body_of(response)
        if not snap["snapped"]:
            raise SoftFailure("no aim mark was found near the truth aim point (aim left at the clicked point)")
        self.ctx["aim"] = (snap["x_in"], snap["y_in"])
        distance = math.dist(self.ctx["aim"], aim)
        if distance > AIM_TOLERANCE_IN:
            raise SoftFailure(f"snapped aim is {distance:.3f} in from the truth aim (limit {AIM_TOLERANCE_IN})")
        return f"snapped to the aim mark, {distance:.3f} in from the truth aim"

    def step_6_13(self) -> str:
        """Save the reviewed shots and aim (the draft is complete but not final yet)."""
        aim = self.ctx.get("aim") or self.predicted_aim_in()
        shots = [
            {key: shot[key] for key in ("x_in", "y_in", "origin", "moved_by_user", "confidence")}
            for shot in self.ctx["shots"]
        ]
        response = self.api.json(
            "PUT",
            f"/api/groups/{self.ctx['group_id']}/review",
            {
                "aim": {"x_in": aim[0], "y_in": aim[1]},
                "shots": shots,
                "rejections": [],
                "shots_expected": self.ctx["expected"],
            },
        )
        expect_status(response, 200, "PUT /api/groups/{id}/review")
        saved = body_of(response)
        if len(saved["shots"]) != len(shots) or saved["aim"] is None:
            raise SoftFailure(f"saved {len(saved['shots'])} shots, aim {saved['aim']}")
        return f"{len(saved['shots'])} shots and the aim saved, status {saved['status']}"

    def step_6_14(self) -> str:
        """Finalize (needs a confirmed reference, an aim point and the expected shot count)."""
        response = self.api.json("POST", f"/api/groups/{self.ctx['group_id']}/finalize", None)
        expect_status(response, 200, "POST /api/groups/{id}/finalize")
        group = body_of(response)
        if group["status"] != "final":
            raise HardFailure(f"status after finalize is {group['status']!r}")
        return f"group {group['id']} is final"

    def step_6_15(self) -> str:
        """The stats must have n == expected and ES within the tolerance of the truth ES."""
        response = self.api.request("GET", f"/api/groups/{self.ctx['group_id']}/stats")
        expect_status(response, 200, "GET /api/groups/{id}/stats")
        stats = body_of(response)
        problems = []
        if stats["n"] != self.ctx["expected"]:
            problems.append(f"n={stats['n']} (expected {self.ctx['expected']})")
        error = abs(stats["es_in"] - self.ctx["truth_es"])
        if error > ES_TOLERANCE_IN:
            problems.append(f"ES {stats['es_in']:.4f} in is {error:.4f} from truth {self.ctx['truth_es']:.4f}")
        if abs(stats["es_moa"] - stats["es_in"] / MOA_IN_PER_100YD) > 0.01:
            problems.append(f"ES {stats['es_moa']:.3f} MOA does not match {stats['es_in']:.4f} in at 100 yd")
        if problems:
            raise SoftFailure("; ".join(problems))
        return (
            f"n={stats['n']}, ES {stats['es_in']:.4f} in / {stats['es_moa']:.2f} MOA "
            f"(truth {self.ctx['truth_es']:.4f} in, error {error:.4f})"
        )


def cmd_roundtrip(args: argparse.Namespace, report: Reporter) -> None:
    """Run check 6 against the live server."""
    work_dir = Path(args.work_dir)
    work_dir.mkdir(parents=True, exist_ok=True)
    RoundTrip(Api(args.base_url), work_dir, report).run(Path(args.summary_out))


# --- check 10: the stored share-card defaults ----------------------------------------------------

SHARE_DEFAULTS_PATH = "/api/settings/share-defaults"
BUILT_IN_SHARE_DEFAULTS = {
    "photo": True,
    "spread": True,
    "thing_hitter": False,
    "poi": False,
    "correction": False,
    "numbers": True,
}
CHANGED_SHARE_DEFAULTS = {name: not value for name, value in BUILT_IN_SHARE_DEFAULTS.items()}
REFUSED_SHARE_DEFAULTS = dict.fromkeys(BUILT_IN_SHARE_DEFAULTS, True)


def cmd_settings(args: argparse.Namespace, report: Reporter) -> None:
    """GET the built-ins, PUT a changed set, GET it back, and refuse a cross-origin PUT."""
    api = Api(args.base_url)
    steps = [
        ("10.1", "GET returns the built-in defaults"),
        ("10.2", "PUT a changed set"),
        ("10.3", "GET reflects the saved set"),
        ("10.4", "a cross-origin PUT is refused and changes nothing"),
    ]
    blocked_by: str | None = None

    def fail_hard(check_id: str, label: str, text: str) -> None:
        nonlocal blocked_by
        report.failed(check_id, f"{label}: {text}")
        blocked_by = check_id

    def read(check_id: str, label: str, expect: dict[str, bool]) -> bool:
        response = api.request("GET", SHARE_DEFAULTS_PATH)
        if response.status != 200:
            what = "route missing" if response.route_missing else "expected 200"
            fail_hard(check_id, label, f"{what} (GET {SHARE_DEFAULTS_PATH} -> {response.describe()})")
            return False
        if response.json() != expect:
            fail_hard(check_id, label, f"expected {expect}, got {response.json()}")
            return False
        return True

    for check_id, label in steps:
        if blocked_by is not None:
            report.failed(check_id, f"{label}: not run (blocked by step {blocked_by})")
            continue
        if check_id == "10.1" and read(check_id, label, BUILT_IN_SHARE_DEFAULTS):
            report.passed(check_id, f"{label}: {BUILT_IN_SHARE_DEFAULTS}")
        elif check_id == "10.2":
            response = api.json("PUT", SHARE_DEFAULTS_PATH, CHANGED_SHARE_DEFAULTS)
            if response.status != 200:
                what = "route missing" if response.route_missing else "expected 200"
                fail_hard(check_id, label, f"{what} (PUT -> {response.describe()})")
            elif response.json() != CHANGED_SHARE_DEFAULTS:
                fail_hard(check_id, label, f"the answer {response.json()} is not the saved set {CHANGED_SHARE_DEFAULTS}")
            else:
                report.passed(check_id, f"{label}: every one of the six layers flipped, answered 200 with the new set")
        elif check_id == "10.3" and read(check_id, label, CHANGED_SHARE_DEFAULTS):
            report.passed(check_id, f"{label}: {CHANGED_SHARE_DEFAULTS}")
        elif check_id == "10.4":
            refused = api.json(
                "PUT", SHARE_DEFAULTS_PATH, REFUSED_SHARE_DEFAULTS, headers={"Origin": "http://evil.example"}
            )
            if refused.status != 403:
                fail_hard(check_id, label, f"expected 403, got {refused.describe()}")
            elif read(check_id, label, CHANGED_SHARE_DEFAULTS):
                report.passed(check_id, f"{label}: PUT with Origin http://evil.example -> 403 code={refused.code}, stored set unchanged")


# --- check 9 helper: residue ---------------------------------------------------------------------


def cmd_residue(args: argparse.Namespace, report: Reporter) -> None:
    """Nothing may still run in the server's process group or carry its temp RGA_DATA_DIR."""
    result = subprocess.run(  # noqa: S603  (fixed argv)
        ["ps", "-axwwE", "-o", "pid=,pgid=,stat=,command="],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    if result.returncode != 0:
        report.failed("9.3", f"ps failed (exit {result.returncode}): {result.stderr.strip()}")
        return
    marker = f"RGA_DATA_DIR={args.root}"
    mine = {os.getpid(), os.getppid()}
    left: list[str] = []
    for line in result.stdout.splitlines():
        parts = line.split(None, 3)
        if len(parts) < 3 or not all(part.isdigit() for part in parts[:2]):
            continue
        pid, pgid, state = int(parts[0]), int(parts[1]), parts[2]
        command = parts[3] if len(parts) > 3 else ""
        if pid in mine:
            continue
        in_group = args.pgid > 0 and pgid == args.pgid and not state.startswith("Z")
        if in_group or marker in command:
            left.append(f"{pid} (pgid {pgid}, {state}) {command[:80]}")
    if left:
        report.failed("9.3", "processes still running after teardown: " + "; ".join(left))
    else:
        group = f"process group {args.pgid}" if args.pgid > 0 else "no recorded process group"
        report.passed("9.3", f"no process in {group} and none carries {marker}")


# --- entry point ---------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    """Command-line parser."""
    parser = argparse.ArgumentParser(description="Smoke-test driver for the local server (loopback only).")
    sub = parser.add_subparsers(dest="command", required=True)
    health = sub.add_parser("healthz", help="check 3: wait for /healthz and check schema_version")
    health.add_argument("--base-url", required=True)
    health.add_argument("--pid", type=int, required=True)
    health.add_argument("--timeout", type=float, default=15.0)
    denials = sub.add_parser("denials", help="check 5: refusals and security headers")
    denials.add_argument("--base-url", required=True)
    trip = sub.add_parser("roundtrip", help="check 6: upload to finalized group to stats")
    trip.add_argument("--base-url", required=True)
    trip.add_argument("--work-dir", required=True)
    trip.add_argument("--summary-out", required=True)
    settings = sub.add_parser("settings", help="check 10: stored share-card defaults")
    settings.add_argument("--base-url", required=True)
    residue = sub.add_parser("residue", help="check 9.3: no process left after teardown")
    residue.add_argument("--root", required=True)
    residue.add_argument("--pgid", type=int, default=0)
    return parser


COMMANDS: dict[str, Callable[[argparse.Namespace, Reporter], None]] = {
    "healthz": cmd_healthz,
    "denials": cmd_denials,
    "roundtrip": cmd_roundtrip,
    "settings": cmd_settings,
    "residue": cmd_residue,
}


def main(argv: list[str] | None = None) -> int:
    """Run one subcommand; a crash is reported as a FAIL line and exit status 1."""
    args = build_parser().parse_args(argv)
    report = Reporter()
    try:
        COMMANDS[args.command](args, report)
    except SystemExit:
        raise
    except Exception as exc:
        report.failed(args.command, f"driver error {type(exc).__name__}: {exc}")
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
