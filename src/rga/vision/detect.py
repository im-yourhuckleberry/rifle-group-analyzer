"""Bullet-hole detection on the rectified working image (design.md section 6).

`detect_holes` finds dark-core holes and light-core holes (backing visible against a dark region),
rejects printed structure and dirt marks, and classifies each blob as single, split (a merged pair
resolved into two centers) or ragged (an unresolvable cluster left to `rga.vision.ragged`).
All coordinates are working-image pixels (x right, y down).
"""

import dataclasses
import math
from dataclasses import dataclass

import cv2
import numpy as np

from rga.domain.types import Blob, BlobKind, WorkingFrame

_COARSE_DIAMETER_PX = 10
_ZERO_ALLOWED = frozenset({"halo_inner_r"})


@dataclass(frozen=True)
class DetectParams:
    """Tunable detection parameters (shipped values are the spike-tuned defaults).

    Lengths are in bullet diameters, areas in single-bullet areas (A1 = pi * (d / 2)^2).
    `dark_rel` marks pixels darker than that fraction of the local paper brightness as hole
    material; `gray_dark_rel` with `max_saturation` also admits grey, unsaturated pixels (grey-rimmed
    holes) while ignoring coloured print. `close_d`, `fill_hole_a1` and `open_d` close rim gaps,
    fill enclosed light cores and drop thin print. The `min_*`/`max_*` fields and `small_area_a1`,
    `halo_*`, `min_core_fraction` and `max_crowding` decide whether a dark blob is a hole; the
    `bright_*`, `min_enclosure`, `light_*` and `min_container_solidity` fields drive the light-core
    cue; `elongation_split`, `*_split_area_a1` and `ragged_area_a1` choose single, split or ragged.
    """

    bg_kernel_d: float = 3.0
    dark_rel: float = 0.5
    gray_dark_rel: float = 0.55
    max_saturation: float = 0.3
    close_d: float = 0.12
    fill_hole_a1: float = 0.6
    open_d: float = 0.35
    min_area_a1: float = 0.55
    max_area_a1: float = 8.0
    max_cluster_area_a1: float = 24.0
    cluster_min_radius_r: float = 0.5
    cluster_max_radius_r: float = 1.2
    cluster_min_sep_d: float = 0.7
    cluster_min_coverage: float = 0.6
    pair_min_sep_d: float = 0.5
    pair_min_radius_r: float = 0.65
    min_solidity: float = 0.8
    max_rect_fill: float = 0.88
    min_roundness: float = 0.6
    min_core_fraction: float = 0.24
    ring_hole_min_a1: float = 0.6
    small_area_a1: float = 0.65
    max_crowding: float = 0.3
    min_halo: float = 0.02
    halo_inner_r: float = 0.25
    halo_outer_r: float = 0.5
    halo_required_a1: float = 2.5
    bright_kernel_d: float = 1.8
    bright_contrast: float = 0.3
    bright_open_d: float = 0.15
    min_enclosure: float = 0.85
    min_container_solidity: float = 0.8
    enclosure_ratio: float = 0.5
    light_min_a1: float = 0.5
    light_max_a1: float = 3.0
    elongation_split: float = 1.35
    min_split_area_a1: float = 1.15
    max_split_area_a1: float = 2.4
    ragged_area_a1: float = 1.4
    tan_hue_lo: int = 8
    tan_hue_hi: int = 28
    tan_sat_lo: float = 0.15
    tan_sat_hi: float = 0.62
    tan_min_value: int = 70
    tan_min_a1: float = 0.1
    tan_max_a1: float = 3.0
    tan_min_roundness: float = 0.55
    tan_dedupe_d: float = 0.6

    def __post_init__(self) -> None:
        for name, value in dataclasses.asdict(self).items():
            if not value > 0 and name not in _ZERO_ALLOWED:
                raise ValueError(f"{name} must be positive")
        if not 0 < self.dark_rel <= self.gray_dark_rel < 1:
            raise ValueError("dark_rel must satisfy 0 < dark_rel <= gray_dark_rel < 1")
        if self.min_area_a1 >= self.max_area_a1:
            raise ValueError("min_area_a1 must be below max_area_a1")
        if self.max_cluster_area_a1 < self.max_area_a1:
            raise ValueError("max_cluster_area_a1 must not be below max_area_a1")
        if not 0 < self.cluster_min_radius_r < self.cluster_max_radius_r:
            raise ValueError("cluster radius bounds must satisfy 0 < min < max")
        if self.light_min_a1 >= self.light_max_a1:
            raise ValueError("light_min_a1 must be below light_max_a1")
        if not 0 <= self.halo_inner_r < self.halo_outer_r:
            raise ValueError("halo ring radii must satisfy 0 <= inner < outer")
        if not 0 < self.min_solidity <= 1:
            raise ValueError("min_solidity must be in (0, 1]")
        if self.elongation_split < 1:
            raise ValueError("elongation_split must be >= 1")


def detect_holes(
    working: np.ndarray,
    frame: WorkingFrame,
    bullet_diameter_in: float,
    roi_px: tuple[int, int, int, int] | None = None,
    params: DetectParams | None = None,
) -> list[Blob]:
    """Detect bullet holes in `working` and return them as blobs sorted by (y, x).

    `roi_px` is (x0, y0, x1, y1) in working px; only blobs centered inside it are returned and an
    empty or fully outside ROI gives []. Raises ValueError for a non-image array, a non-positive
    bullet diameter or ppi.
    """
    p = params or DetectParams()
    if working.ndim not in (2, 3) or working.size == 0:
        raise ValueError("working must be a non-empty 2-D or 3-D image")
    if not (bullet_diameter_in > 0 and frame.ppi > 0):
        raise ValueError("bullet_diameter_in and frame.ppi must be positive")
    height, width = working.shape[:2]
    x0, y0, x1, y1 = _clamp_roi(roi_px, width, height)
    if x1 <= x0 or y1 <= y0:
        return []

    d = bullet_diameter_in * frame.ppi
    margin = int(math.ceil(max(p.bg_kernel_d, p.bright_kernel_d) * d)) + 2
    cx0, cy0 = max(0, x0 - margin), max(0, y0 - margin)
    cx1, cy1 = min(width, x1 + margin), min(height, y1 + margin)
    crop = working[cy0:cy1, cx0:cx1]
    gray = crop if crop.ndim == 2 else crop.max(axis=2)

    paper = _paper_level(gray, d, p)
    rel = gray.astype(np.float32) / paper
    dark = _dark_mask(rel, _saturation(crop), d, p)
    outlines = _dark_outlines(dark)
    candidates = _dark_candidates(outlines, dark, rel, d, p)
    candidates += _light_candidates(gray, paper, rel, outlines, d, p)

    blobs: list[Blob] = []
    for cand in candidates:
        blob = _classify(cand, d, p)
        if blob is None:
            continue
        cx, cy = blob.centers_px[0]
        if not (x0 <= cx + cx0 < x1 and y0 <= cy + cy0 < y1):
            continue
        blobs.append(_shifted(blob, cx0, cy0))
    if crop.ndim == 3:
        blobs += [
            _shifted(b, cx0, cy0)
            for b in _tan_core_blobs(crop, blobs, (cx0, cy0), d, p)
            if x0 <= b.centers_px[0][0] + cx0 < x1 and y0 <= b.centers_px[0][1] + cy0 < y1
        ]
    blobs.sort(key=lambda b: (b.centers_px[0][1], b.centers_px[0][0]))
    return blobs


def _clamp_roi(roi, width, height):
    if roi is None:
        return 0, 0, width, height
    x0, y0, x1, y1 = (int(v) for v in roi)
    return max(0, x0), max(0, y0), min(width, x1), min(height, y1)


def _disc(diameter: float) -> np.ndarray:
    k = max(3, int(diameter) | 1)
    return cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k))


def _coarse(gray: np.ndarray, d: float) -> tuple[np.ndarray, int]:
    scale = max(1, int(d / _COARSE_DIAMETER_PX))
    small = cv2.resize(
        gray,
        (max(1, gray.shape[1] // scale), max(1, gray.shape[0] // scale)),
        interpolation=cv2.INTER_AREA,
    )
    return small, scale


def _upsample(level: np.ndarray, like: np.ndarray) -> np.ndarray:
    return cv2.resize(level, (like.shape[1], like.shape[0]), interpolation=cv2.INTER_LINEAR)


def _paper_level(gray: np.ndarray, d: float, p: DetectParams) -> np.ndarray:
    """Local paper brightness: closing with a kernel several bullet diameters wide, smoothed."""
    small, scale = _coarse(gray, d)
    kernel = _disc(p.bg_kernel_d * d / scale)
    bg = cv2.morphologyEx(small, cv2.MORPH_CLOSE, kernel)
    bg = cv2.GaussianBlur(bg, (0, 0), kernel.shape[0] / 3)
    return np.maximum(_upsample(bg, gray).astype(np.float32), 1.0)


def _saturation(img: np.ndarray) -> np.ndarray:
    if img.ndim == 2:
        return np.zeros(img.shape, np.float32)
    top = img.max(axis=2).astype(np.float32)
    return (top - img.min(axis=2)) / (top + 1.0)


def _dark_mask(rel: np.ndarray, saturation: np.ndarray, d: float, p: DetectParams) -> np.ndarray:
    """Dark pixels with small enclosed gaps filled (ring-shaped holes), thin print removed."""
    core = rel < p.dark_rel
    grey = (rel < p.gray_dark_rel) & (saturation < p.max_saturation)
    mask = (core | grey).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, _disc(p.close_d * d))
    _fill_small_holes(mask, p.fill_hole_a1 * math.pi * (d / 2) ** 2)
    return cv2.morphologyEx(mask, cv2.MORPH_OPEN, _disc(p.open_d * d))


def _fill_small_holes(mask: np.ndarray, max_area: float) -> None:
    contours, hierarchy = cv2.findContours(mask, cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    if hierarchy is None:
        return
    small = [
        c
        for c, h in zip(contours, hierarchy[0], strict=True)
        if h[3] >= 0 and cv2.contourArea(c) <= max_area
    ]
    cv2.drawContours(mask, small, -1, 1, -1)


def _tan_core_blobs(crop, found, offset, d, p) -> list[Blob]:
    """Holes whose backing shows as a small tan core with no dark rim to speak of.

    A tan component (hue of cardboard or paper backing, moderate saturation) that is round enough,
    hole-core sized (a cardboard background is one huge component and never qualifies) and not
    already covered by a dark or light detection.
    """
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    hue, sat, val = hsv[..., 0], hsv[..., 1] / 255.0, hsv[..., 2]
    mask = (
        (hue >= p.tan_hue_lo)
        & (hue <= p.tan_hue_hi)
        & (sat >= p.tan_sat_lo)
        & (sat <= p.tan_sat_hi)
        & (val >= p.tan_min_value)
    ).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, _disc(p.bright_open_d * d))
    a1 = math.pi * (d / 2) ** 2
    taken = [(x - offset[0], y - offset[1]) for b in found for x, y in b.centers_px]
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    out: list[Blob] = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if not p.tan_min_a1 * a1 <= area <= p.tan_max_a1 * a1:
            continue
        (_, _), radius = cv2.minEnclosingCircle(contour)
        if area / (math.pi * radius**2) < p.tan_min_roundness:
            continue
        m = cv2.moments(contour)
        cx, cy = m["m10"] / m["m00"], m["m01"] / m["m00"]
        if any(math.hypot(cx - tx, cy - ty) < p.tan_dedupe_d * d for tx, ty in taken):
            continue
        pts = tuple((int(x), int(y)) for x, y in contour.reshape(-1, 2))
        out.append(Blob(BlobKind.SINGLE, pts, area, ((cx, cy),), 0.4))
    return out


def _bright_mask(gray: np.ndarray, paper: np.ndarray, d: float, p: DetectParams) -> np.ndarray:
    """Cores far brighter than the local minimum within a bullet-sized neighbourhood.

    Each bright component is trimmed to the pixels above half its own peak top-hat contrast, so a
    lighter halo around a core is not mistaken for part of it.
    """
    small, scale = _coarse(gray, d)
    opened = cv2.morphologyEx(small, cv2.MORPH_OPEN, _disc(p.bright_kernel_d * d / scale))
    top_hat = gray.astype(np.float32) - _upsample(opened, gray).astype(np.float32)
    mask = (top_hat > p.bright_contrast * paper).astype(np.uint8)
    mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, _disc(p.bright_open_d * d))
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    trimmed = np.zeros_like(mask)
    for i in range(1, count):
        x, y, w, h = (
            stats[i, k]
            for k in (cv2.CC_STAT_LEFT, cv2.CC_STAT_TOP, cv2.CC_STAT_WIDTH, cv2.CC_STAT_HEIGHT)
        )
        region = labels[y : y + h, x : x + w] == i
        values = top_hat[y : y + h, x : x + w]
        peak = np.percentile(values[region], 90)
        trimmed[y : y + h, x : x + w][region & (values > 0.5 * peak)] = 1
    return trimmed


@dataclass
class _Candidate:
    contour: np.ndarray
    mask_patch: np.ndarray
    origin: tuple[int, int]
    area: float
    cue: str
    halo: float
    contrast: float
    core_fraction: float
    roundness: float
    crowding: float
    solidity: float = 1.0


def _dark_outlines(dark: np.ndarray) -> list[tuple[np.ndarray, float, float]]:
    """(contour, area, solidity) of every connected dark region."""
    contours, _ = cv2.findContours(dark, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    out = []
    for contour in contours:
        area = cv2.contourArea(contour)
        hull_area = cv2.contourArea(cv2.convexHull(contour))
        out.append((contour, area, area / hull_area if hull_area > 0 else 0.0))
    return out


def _dark_candidates(outlines, dark, rel, d, p) -> list[_Candidate]:
    a1 = math.pi * (d / 2) ** 2
    out = []
    for contour, area, _ in outlines:
        if not p.min_area_a1 * a1 <= area <= p.max_cluster_area_a1 * a1:
            continue
        cand = _describe(contour, area, "dark", dark, rel, d, p)
        if cand is not None:
            out.append(cand)
    return out


def _light_candidates(gray, paper, rel, outlines, d, p) -> list[_Candidate]:
    a1 = math.pi * (d / 2) ** 2
    bright = _bright_mask(gray, paper, d, p)
    contours, _ = cv2.findContours(bright, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    out = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if not p.light_min_a1 * a1 <= area <= p.light_max_a1 * a1:
            continue
        m = cv2.moments(contour)
        center = (m["m10"] / m["m00"], m["m01"] / m["m00"])
        if not _in_acceptable_container(center, outlines, a1, p):
            continue
        cand = _describe(contour, area, "light", bright, rel, d, p)
        if cand is not None and _enclosure(cand, gray, d, p) >= p.min_enclosure:
            out.append(cand)
    return out


def _in_acceptable_container(center, outlines, a1: float, p: DetectParams) -> bool:
    """A bright blob must not sit inside a hole-sized dark region (handled by the dark cue) nor
    inside a ragged dark region such as a QR code."""
    for contour, area, solidity in outlines:
        if cv2.pointPolygonTest(contour, center, False) < 0:
            continue
        if area <= p.max_area_a1 * a1 or solidity < p.min_container_solidity:
            return False
    return True


def _describe(contour, area, cue, mask, rel, d, p) -> _Candidate | None:
    x, y, w, h = cv2.boundingRect(contour)
    pad = int(math.ceil(p.halo_outer_r * d / 2)) + 3
    x0, y0 = max(0, x - pad), max(0, y - pad)
    x1, y1 = min(mask.shape[1], x + w + pad), min(mask.shape[0], y + h + pad)
    patch = np.zeros((y1 - y0, x1 - x0), np.uint8)
    cv2.drawContours(patch, [contour - np.array([x0, y0])], -1, 1, -1)
    solidity = area / max(cv2.contourArea(cv2.convexHull(contour)), 1.0)
    cluster_sized = area > p.min_split_area_a1 * math.pi * (d / 2) ** 2
    if solidity < p.min_solidity and not cluster_sized:
        return None
    (_, _), (rw, rh), _ = cv2.minAreaRect(contour)
    if area / max(rw * rh, 1.0) > p.max_rect_fill and not cluster_sized:
        return None
    rel_patch = rel[y0:y1, x0:x1]
    dist = cv2.distanceTransform(1 - patch, cv2.DIST_L2, 5)
    r = d / 2
    annulus = (dist >= p.halo_inner_r * r) & (dist <= p.halo_outer_r * r)
    ring = annulus & (mask[y0:y1, x0:x1] == 0)
    crowding = float(np.mean(mask[y0:y1, x0:x1][annulus] == 1)) if annulus.any() else 0.0
    halo = float(abs(1.0 - rel_patch[ring].mean())) if ring.any() else 0.0
    inside = rel_patch[patch == 1]
    contrast = float(abs(1.0 - inside.mean()))
    core_fraction = float(np.mean(inside >= p.dark_rel)) if cue == "dark" else 0.0
    (_, _), radius = cv2.minEnclosingCircle(contour)
    roundness = area / (math.pi * radius**2)
    return _Candidate(
        contour,
        patch,
        (x0, y0),
        area,
        cue,
        halo,
        contrast,
        core_fraction,
        roundness,
        crowding,
        solidity,
    )


def _enclosure(c: _Candidate, gray: np.ndarray, d: float, p: DetectParams) -> float:
    """Fraction of the ring just outside a bright blob that is much darker than the blob."""
    x0, y0 = c.origin
    h, w = c.mask_patch.shape
    dist = cv2.distanceTransform(1 - c.mask_patch, cv2.DIST_L2, 5)
    ring = (dist >= 2.0) & (dist <= max(3.0, 0.4 * d / 2))
    g = gray[y0 : y0 + h, x0 : x0 + w]
    if not ring.any():
        return 0.0
    core_level = float(np.median(g[c.mask_patch == 1]))
    return float(np.mean(g[ring] < p.enclosure_ratio * core_level))


def _moments(patch: np.ndarray) -> tuple[tuple[float, float], float, float]:
    """(centroid x, y in patch px, axis ratio >= 1, major-axis orientation in radians)."""
    m = cv2.moments(patch, binaryImage=True)
    cx, cy = m["m10"] / m["m00"], m["m01"] / m["m00"]
    mu20, mu02, mu11 = m["mu20"] / m["m00"], m["mu02"] / m["m00"], m["mu11"] / m["m00"]
    spread = math.sqrt(max(((mu20 - mu02) / 2) ** 2 + mu11**2, 0.0))
    lam1, lam2 = (mu20 + mu02) / 2 + spread, (mu20 + mu02) / 2 - spread
    return (cx, cy), math.sqrt(lam1 / max(lam2, 1e-9)), 0.5 * math.atan2(2 * mu11, mu20 - mu02)


def _accepted(c: _Candidate, ratio: float, axis_ratio: float, p: DetectParams) -> bool:
    """Whether a candidate looks like a bullet hole rather than print, dirt or a printed dot."""
    peanut = ratio >= p.min_split_area_a1 and axis_ratio >= p.elongation_split
    if c.roundness < p.min_roundness and not peanut:
        return False
    if c.cue == "light":
        return True
    if c.crowding > p.max_crowding:
        return False
    if c.core_fraction >= p.min_core_fraction and ratio >= p.ring_hole_min_a1:
        return True
    if ratio >= p.halo_required_a1:
        return c.halo >= p.min_halo
    return c.halo >= p.min_halo or ratio >= p.small_area_a1


def _classify(c: _Candidate, d: float, p: DetectParams) -> Blob | None:
    ratio = c.area / (math.pi * (d / 2) ** 2)
    (cx, cy), axis_ratio, theta = _moments(c.mask_patch)
    if c.cue == "dark" and (ratio > p.max_split_area_a1 or c.solidity < p.min_solidity):
        cluster = _cluster_blob(c, ratio, d, p)
        if cluster is not None:
            return cluster
        if ratio > p.max_area_a1 or c.solidity < p.min_solidity:
            return None
    if not _accepted(c, ratio, axis_ratio, p):
        return None
    ox, oy = c.origin
    contour = tuple((int(x), int(y)) for x, y in c.contour.reshape(-1, 2))
    confidence = _confidence(c, ratio)
    pair_like = (
        axis_ratio >= p.elongation_split and p.min_split_area_a1 <= ratio <= p.max_split_area_a1
    )
    if pair_like:
        peaks, _ = _distance_peaks(c.mask_patch, d, p.pair_min_radius_r, p.pair_min_sep_d, 0.05)
        if len(peaks) >= 2:
            centers = _pair_centers(c.mask_patch, (cx, cy), theta)
            if centers is not None:
                return Blob(
                    BlobKind.SPLIT,
                    contour,
                    c.area,
                    tuple((x + ox, y + oy) for x, y in centers),
                    confidence,
                )
        elif len(peaks) == 1:
            cx, cy = _disc_center(c.mask_patch, peaks[0])
    kind = BlobKind.RAGGED if ratio >= p.ragged_area_a1 else BlobKind.SINGLE
    return Blob(kind, contour, c.area, ((cx + ox, cy + oy),), confidence)


def _disc_center(patch: np.ndarray, peak: tuple[float, float, float]) -> tuple[float, float]:
    """Centroid of the blob pixels inside the inscribed disc around a distance-transform peak."""
    x, y, radius = peak
    ys, xs = np.nonzero(patch)
    inside = np.hypot(xs - x, ys - y) <= radius
    return (float(xs[inside].mean()), float(ys[inside].mean())) if inside.any() else (x, y)


def _cluster_blob(c: _Candidate, ratio: float, d: float, p: DetectParams) -> Blob | None:
    """Split a chain or clump of touching holes into one centre per hole, or None."""
    if c.crowding > p.max_crowding or c.halo < p.min_halo:
        return None
    centers = _cluster_centers(c.mask_patch, d, p)
    if centers is None:
        return None
    ox, oy = c.origin
    contour = tuple((int(x), int(y)) for x, y in c.contour.reshape(-1, 2))
    shifted = tuple((x + ox, y + oy) for x, y in centers)
    return Blob(BlobKind.SPLIT, contour, c.area, shifted, _confidence(c, ratio / len(centers)))


def _distance_peaks(
    patch: np.ndarray, d: float, min_radius_r: float, min_sep_d: float, sigma_d: float
) -> tuple[list[tuple[float, float, float]], np.ndarray]:
    """Peaks (x, y, radius) of the smoothed distance transform of `patch`, strongest first."""
    r = d / 2
    dist = cv2.GaussianBlur(
        cv2.distanceTransform(patch, cv2.DIST_L2, 5), (0, 0), max(1.0, sigma_d * d)
    )
    is_peak = (dist >= cv2.dilate(dist, _disc(0.5 * d))) & (dist >= min_radius_r * r)
    ys, xs = np.nonzero(is_peak & (patch == 1))
    order = np.argsort(-dist[ys, xs], kind="stable")
    peaks: list[tuple[float, float, float]] = []
    for i in order:
        x, y = float(xs[i]), float(ys[i])
        if all(math.hypot(x - qx, y - qy) >= min_sep_d * d for qx, qy, _ in peaks):
            peaks.append((x, y, float(dist[ys[i], xs[i]])))
    return peaks, dist


def _cluster_centers(patch: np.ndarray, d: float, p: DetectParams):
    """Hole centres of a clump: the distance-transform peaks (each hole is a disc of radius d/2)."""
    peaks, dist = _distance_peaks(patch, d, p.cluster_min_radius_r, p.cluster_min_sep_d, 0.1)
    if dist.max() > p.cluster_max_radius_r * d / 2 or len(peaks) < 2:
        return None
    pys, pxs = np.nonzero(patch)
    covered = np.zeros(len(pxs), dtype=bool)
    for x, y, radius in peaks:
        covered |= np.hypot(pxs - x, pys - y) <= 1.3 * radius
    if covered.mean() < p.cluster_min_coverage:
        return None
    return sorted(((x, y) for x, y, _ in peaks), key=lambda pt: (pt[1], pt[0]))


def _pair_centers(patch, centroid, theta):
    """Centers of two equal overlapping discs from the blob's extent along and across its axis."""
    ys, xs = np.nonzero(patch)
    u = np.array([math.cos(theta), math.sin(theta)])
    v = np.array([-u[1], u[0]])
    offsets = np.column_stack([xs - centroid[0], ys - centroid[1]]).astype(np.float64)
    along, across = offsets @ u, offsets @ v
    half_length = (along.max() - along.min()) / 2
    radius = (across.max() - across.min()) / 2
    spread = half_length - radius
    if spread <= 0:
        return None
    middle = np.array(centroid) + u * (along.max() + along.min()) / 2
    return tuple(tuple(float(t) for t in middle + sign * spread * u) for sign in (-1.0, 1.0))


def _confidence(c: _Candidate, ratio: float) -> float:
    size = 1.0 - min(abs(math.log(max(ratio, 1e-3) / 0.9)), 1.5) / 1.5
    contrast = min(max(c.contrast, 0.0), 1.0)
    return float(min(1.0, max(0.0, 0.5 * contrast + 0.3 * size + 0.2 * min(1.0, c.halo * 10))))


def _shifted(blob: Blob, dx: int, dy: int) -> Blob:
    return Blob(
        blob.kind,
        tuple((x + dx, y + dy) for x, y in blob.contour_px),
        blob.area_px,
        tuple((x + dx, y + dy) for x, y in blob.centers_px),
        blob.confidence,
    )
