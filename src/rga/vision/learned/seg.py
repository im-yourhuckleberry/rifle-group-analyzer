"""seg-v1: a learned bullet-hole detector with the same contract as `rga.vision.detect.detect_holes`.

A small U-Net (ONNX, run by OpenCV's `cv2.dnn` on the CPU) predicts a hole-centre heatmap and a
hole mask on the working image rescaled to 16 px per bullet diameter. Heat peaks give sub-pixel
centres; mask regions give outlines and decide single / split / ragged:

- one peak in a region -> single; two or more well-separated peaks in a region sized for that
  many holes -> split; a region too large for its peaks -> ragged (true outline, one centre);
- a peak with no mask region -> single with a one-diameter circle outline;
- a region with no peak -> ragged if >= 1.4 single-hole areas, otherwise dropped.

Offline only (`requires_network = False`); imports numpy, cv2 and rga.domain.types. The model file
and its SHA-256 (models/SHA256SUMS) are checked when the detector is constructed, so a missing or
corrupt model fails at start-up rather than mid-request. `detect` raises only ValueError.
"""

import hashlib
import json
import math
import threading
from pathlib import Path

import cv2
import numpy as np

from rga.domain.types import Blob, BlobKind, WorkingFrame
from rga.vision.learned import _common as c

NAME = "seg-v1"
SEMVER = "1.2.0"
MODEL_DIR = Path(__file__).resolve().parent / "models"
DEFAULT_MODEL = MODEL_DIR / "seg-v1.onnx"
TILE = 512
OVERLAP = 32
NMS_D = 0.5
SPLIT_SEP_D = 0.6
SPLIT_AREA_BAND = (0.6, 1.4)
RAGGED_MIN_A1 = 1.4
DROP_BELOW_A1 = 0.5
DEFAULTS = {"tau_c": 0.3, "tau_m": 0.5, "temperature": 1.0, "ragged_excess": 0.6}


def decode(
    heat: np.ndarray,
    mask: np.ndarray,
    d_px: float,
    tau_c: float,
    tau_m: float,
    ragged_excess: float = DEFAULTS["ragged_excess"],
) -> list[tuple[BlobKind, np.ndarray, list[tuple[float, float]], float]]:
    """Turn heat and mask maps (normalised px) into (kind, contour, centres, confidence) tuples.

    A region is ragged when its area is at least RAGGED_MIN_A1 single-hole areas and exceeds
    (peaks + ragged_excess) single-hole areas, i.e. it hides more holes than it shows centres.
    """
    a1 = math.pi * (d_px / 2) ** 2
    peaks, scores = _peaks(heat, d_px, tau_c)
    regions = (mask > tau_m).astype(np.uint8)
    n, labels, stats, centroids = cv2.connectedComponentsWithStats(regions, connectivity=8)
    h, w = heat.shape
    owner = [_owner(labels, x, y, NMS_D * d_px) for x, y in peaks]
    out = []
    for lab in range(1, n):
        mine = [i for i, o in enumerate(owner) if o == lab]
        area = float(stats[lab, cv2.CC_STAT_AREA])
        contour = _outline(labels == lab)
        if not mine:
            if area >= RAGGED_MIN_A1 * a1:
                conf = 0.5 * float(mask[labels == lab].mean())
                out.append((BlobKind.RAGGED, contour, [tuple(map(float, centroids[lab]))], conf))
            continue
        mine.sort(key=lambda i: -scores[i])
        best = mine[0]
        k = len(mine)
        if area >= RAGGED_MIN_A1 * a1 and area >= (k + ragged_excess) * a1:
            out.append((BlobKind.RAGGED, contour, [_xy(peaks[best])], float(scores[best])))
        elif (
            k >= 2
            and _separated(peaks[mine], SPLIT_SEP_D * d_px)
            and (SPLIT_AREA_BAND[0] * k * a1 <= area <= SPLIT_AREA_BAND[1] * k * a1)
        ):
            out.append(
                (
                    BlobKind.SPLIT,
                    contour,
                    [_xy(peaks[i]) for i in mine],
                    float(np.mean(scores[mine])),
                )
            )
        else:
            out.append((BlobKind.SINGLE, contour, [_xy(peaks[best])], float(scores[best])))
    for i, o in enumerate(owner):
        if o == 0:
            x, y = peaks[i]
            out.append(
                (
                    BlobKind.SINGLE,
                    c.circle_contour(x, y, d_px / 2),
                    [_xy(peaks[i])],
                    float(scores[i]),
                )
            )
    return out


def run_tiled(net_fn, chw: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Evaluate `net_fn(x, y0, x0) -> (1, 2, h, w)` over <= TILE tiles with OVERLAP, averaging seams.

    The input is reflect-padded to a multiple of 8 (the U-Net's stride); the result is cropped back.
    """
    _, h, w = chw.shape
    ph, pw = (-h) % 8, (-w) % 8
    mode = "reflect" if min(h, w) > 1 else "edge"
    x = np.pad(chw, ((0, 0), (0, ph), (0, pw)), mode=mode) if ph or pw else chw
    H, W = x.shape[1:]
    acc = np.zeros((2, H, W), np.float32)
    cnt = np.zeros((H, W), np.float32)
    for y0 in _starts(H):
        for x0 in _starts(W):
            th, tw = min(TILE, H), min(TILE, W)
            tile = np.ascontiguousarray(x[None, :, y0 : y0 + th, x0 : x0 + tw])
            acc[:, y0 : y0 + th, x0 : x0 + tw] += net_fn(tile, y0, x0)[0]
            cnt[y0 : y0 + th, x0 : x0 + tw] += 1
    acc /= cnt
    return acc[0, :h, :w], acc[1, :h, :w]


class SegHoleDetector:
    """seg-v1 behind the `HoleDetector` protocol (name, version, requires_network, detect)."""

    name = NAME
    requires_network = False

    def __init__(self, model_path: str | Path | None = None) -> None:
        path = Path(model_path) if model_path else DEFAULT_MODEL
        if not path.is_file():
            raise RuntimeError(f"seg-v1 model not found: {path}")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        expected = _expected_sha(path)
        if digest != expected:
            raise RuntimeError(
                f"seg-v1 model {path.name} SHA-256 {digest} != SHA256SUMS {expected}"
            )
        self.version = f"{SEMVER}+{digest[:12]}"
        cfg = dict(DEFAULTS)
        meta = path.with_suffix(".json")
        if meta.is_file():
            cfg.update(json.loads(meta.read_text()).get("operating", {}))
        self.tau_c, self.tau_m = float(cfg["tau_c"]), float(cfg["tau_m"])
        self.temperature, self.ragged_excess = (
            float(cfg["temperature"]),
            float(cfg["ragged_excess"]),
        )
        if not (
            0 < self.tau_c < 1
            and 0 < self.tau_m < 1
            and 0 < self.temperature < 100
            and self.ragged_excess >= 0
        ):
            raise RuntimeError(f"invalid seg-v1 operating values in {meta.name}: {cfg}")
        self._net = cv2.dnn.readNetFromONNX(str(path))
        self._net.setPreferableBackend(cv2.dnn.DNN_BACKEND_OPENCV)
        self._net.setPreferableTarget(cv2.dnn.DNN_TARGET_CPU)
        self._lock = threading.Lock()  # cv2.dnn.Net is not safe to share across threads

    def detect(
        self,
        working: np.ndarray,
        frame: WorkingFrame,
        bullet_diameter_in: float,
        roi_px: tuple[int, int, int, int] | None = None,
    ) -> list[Blob]:
        """Blobs centred in `roi_px` (working px, None = whole image), sorted by (y, x); ValueError only."""
        img = c.validate(working, frame, bullet_diameter_in)
        h, w = img.shape[:2]
        roi = c.clamp_roi(roi_px, w, h)
        if roi[2] <= roi[0] or roi[3] <= roi[1]:
            return []
        prep = c.prepare(img, float(frame.ppi), float(bullet_diameter_in), roi)
        with self._lock:
            heat, mask = run_tiled(self._forward, c.normalise(prep.image))
        found = decode(heat, mask, prep.d_px, self.tau_c, self.tau_m, self.ragged_excess)
        blobs = [
            c.make_blob(k, poly, centers, self._calibrate(conf), prep)
            for k, poly, centers, conf in found
        ]
        return c.finalize(blobs, roi)

    def _forward(self, tile: np.ndarray, _y0: int, _x0: int) -> np.ndarray:
        self._net.setInput(tile)
        return self._net.forward()

    def _calibrate(self, p: float) -> float:
        p = min(1 - 1e-6, max(1e-6, p))
        logit = math.log(p / (1 - p)) / self.temperature
        return 1 / (1 + math.exp(-logit))


_DEFAULT: SegHoleDetector | None = None
_DEFAULT_LOCK = threading.Lock()


def detect_holes(
    working: np.ndarray,
    frame: WorkingFrame,
    bullet_diameter_in: float,
    roi_px: tuple[int, int, int, int] | None = None,
    params: object | None = None,
) -> list[Blob]:
    """Drop-in for `rga.vision.detect.detect_holes` (`params` is accepted and ignored)."""
    global _DEFAULT
    with _DEFAULT_LOCK:
        if _DEFAULT is None:
            _DEFAULT = SegHoleDetector()
    return _DEFAULT.detect(working, frame, bullet_diameter_in, roi_px)


def _expected_sha(path: Path) -> str:
    sums = path.parent / "SHA256SUMS"
    if not sums.is_file():
        raise RuntimeError(f"missing {sums}")
    for line in sums.read_text().splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1] == path.name:
            return parts[0]
    raise RuntimeError(f"{path.name} is not listed in {sums}")


def _peaks(heat: np.ndarray, d_px: float, tau: float) -> tuple[np.ndarray, np.ndarray]:
    """Local maxima above `tau`, greedy NMS at NMS_D diameters, quadratic sub-pixel refinement."""
    h, w = heat.shape
    mx = cv2.dilate(heat, np.ones((3, 3), np.uint8))
    ys, xs = np.nonzero((heat >= mx) & (heat > tau))
    if not len(xs):
        return np.zeros((0, 2)), np.zeros(0)
    vals = heat[ys, xs]
    order = np.lexsort((xs, ys, -vals))  # strongest first; ties broken by position (deterministic)
    keep: list[int] = []
    r2 = (NMS_D * d_px) ** 2
    for i in order:
        if all((xs[i] - xs[j]) ** 2 + (ys[i] - ys[j]) ** 2 >= r2 for j in keep):
            keep.append(int(i))
    pts = []
    for i in keep:
        x, y = int(xs[i]), int(ys[i])
        dx = _vertex(heat[y, x - 1], heat[y, x], heat[y, x + 1]) if 0 < x < w - 1 else 0.0
        dy = _vertex(heat[y - 1, x], heat[y, x], heat[y + 1, x]) if 0 < y < h - 1 else 0.0
        pts.append((x + dx, y + dy))
    return np.asarray(pts, dtype=np.float64), vals[keep].astype(np.float64)


def _owner(labels: np.ndarray, x: float, y: float, radius: float) -> int:
    """Region label under the peak, else the nearest region within `radius` (0 = none).

    A peak that falls a pixel outside its own (weak) mask region must still own it; otherwise the
    hole would be reported twice, once as the peak's circle and once as the peakless region.
    """
    h, w = labels.shape
    xi, yi = min(w - 1, max(0, int(round(x)))), min(h - 1, max(0, int(round(y))))
    if labels[yi, xi]:
        return int(labels[yi, xi])
    r = int(math.ceil(radius))
    y0, y1, x0, x1 = max(0, yi - r), min(h, yi + r + 1), max(0, xi - r), min(w, xi + r + 1)
    win = labels[y0:y1, x0:x1]
    ys, xs = np.nonzero(win)
    if not len(ys):
        return 0
    d2 = (ys + y0 - y) ** 2 + (xs + x0 - x) ** 2
    i = int(np.argmin(d2))
    return int(win[ys[i], xs[i]]) if d2[i] <= radius**2 else 0


def _vertex(left: float, mid: float, right: float) -> float:
    """Offset of the parabola vertex through three samples, using log values (exact for a Gaussian)."""
    lo, mo, ro = (math.log(max(v, 1e-9)) for v in (left, mid, right))
    den = lo - 2 * mo + ro
    if den >= 0:
        return 0.0
    return float(min(0.5, max(-0.5, 0.5 * (lo - ro) / den)))


def _outline(region: np.ndarray) -> np.ndarray:
    contours, _ = cv2.findContours(
        region.astype(np.uint8), cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE
    )
    best = max(contours, key=cv2.contourArea)
    return best.reshape(-1, 2).astype(np.float64)


def _separated(pts: np.ndarray, min_sep: float) -> bool:
    d = np.sqrt(((pts[:, None] - pts[None]) ** 2).sum(-1))
    return bool((d[np.triu_indices(len(pts), 1)] >= min_sep).all())


def _xy(p) -> tuple[float, float]:
    return float(p[0]), float(p[1])


def _starts(n: int) -> list[int]:
    if n <= TILE:
        return [0]
    step = TILE - OVERLAP
    s = list(range(0, n - TILE, step))
    return s + [n - TILE]
