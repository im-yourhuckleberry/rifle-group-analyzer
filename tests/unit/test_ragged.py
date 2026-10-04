import math

import cv2
import numpy as np
import pytest

from rga.domain.types import Blob, BlobKind, ShotOrigin
from rga.vision.ragged import resolve_shots
from tests.synth.generate import render_named

D_PX = 45.0
R_PX = D_PX / 2.0
SINGLE_AREA = math.pi * R_PX**2


def _contour_of(mask: np.ndarray) -> tuple[tuple[int, int], ...]:
    contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
    outline = max(contours, key=cv2.contourArea)
    return tuple((int(p[0][0]), int(p[0][1])) for p in outline)


def _ragged_from_mask(mask: np.ndarray, confidence: float = 0.7) -> Blob:
    centroid = tuple(float(v) for v in np.argwhere(mask > 0).mean(axis=0)[::-1])
    return Blob(
        BlobKind.RAGGED, _contour_of(mask), float((mask > 0).sum()), (centroid,), confidence
    )


def circles_mask(centers, radius=R_PX, shape=(400, 400)) -> np.ndarray:
    mask = np.zeros(shape, dtype=np.uint8)
    for x, y in centers:
        cv2.circle(mask, (round(x), round(y)), round(radius), 255, -1)
    return mask


def ragged_union(center=(200.0, 200.0), angle=0.0, members=3) -> Blob:
    """Union of `members` hole circles each within 0.5 d (= r) of the first."""
    cx, cy = center
    pts = [(cx, cy)]
    for i in range(1, members):
        a = angle + (i - 1)
        pts.append((cx + R_PX * math.cos(a), cy + R_PX * math.sin(a)))
    return _ragged_from_mask(circles_mask(pts))


def single(x, y, area=SINGLE_AREA, confidence=0.9) -> Blob:
    return Blob(BlobKind.SINGLE, ((int(x), int(y)),), area, ((float(x), float(y)),), confidence)


def split(centers, area=None, confidence=0.8) -> Blob:
    area = area if area is not None else SINGLE_AREA * 1.8
    return Blob(BlobKind.SPLIT, ((0, 0),), area, tuple(centers), confidence)


def inside(blob: Blob, x: float, y: float) -> bool:
    contour = np.asarray(blob.contour_px, dtype=np.int32).reshape(-1, 1, 2)
    return cv2.pointPolygonTest(contour, (float(x), float(y)), False) >= 0


def clearance(blob: Blob, x: float, y: float) -> float:
    contour = np.asarray(blob.contour_px, dtype=np.int32).reshape(-1, 1, 2)
    return cv2.pointPolygonTest(contour, (float(x), float(y)), True)


def by_origin(result, origin):
    return [s for s in result.shots_px if s[2] == origin]


def min_spacing(shots) -> float:
    pts = [(s[0], s[1]) for s in shots]
    return min(math.dist(a, b) for i, a in enumerate(pts) for b in pts[i + 1 :])


SEVEN_SINGLES = [single(60 + 90 * i, 60 + 10 * (i % 3)) for i in range(7)]


class TestAc9Case:
    def test_seven_singles_plus_ragged_of_three_fill_to_ten_with_three_estimated(self):
        ragged = ragged_union(center=(200.0, 300.0))
        result = resolve_shots([*SEVEN_SINGLES, ragged], 10, D_PX)

        assert result.status == "ok"
        assert len(result.shots_px) == 10
        assert len(by_origin(result, ShotOrigin.DETECTED)) == 7
        estimated = by_origin(result, ShotOrigin.ESTIMATED)
        assert len(estimated) == 3
        assert all(0.0 < s[3] <= 0.5 for s in estimated)

    @pytest.mark.parametrize("angle", [0.0, 0.5, 1.3, 2.1, 3.0, 4.4, 5.5])
    def test_estimated_shots_lie_inside_the_ragged_outline(self, angle):
        ragged = ragged_union(center=(200.0, 300.0), angle=angle)
        result = resolve_shots([*SEVEN_SINGLES, ragged], 10, D_PX)

        for x, y, _, _ in by_origin(result, ShotOrigin.ESTIMATED):
            assert clearance(ragged, x, y) >= 1.0

    @pytest.mark.parametrize("angle", [0.0, 0.5, 1.3, 2.1, 3.0, 4.4, 5.5])
    def test_estimated_shots_are_spaced_at_least_one_bullet_diameter(self, angle):
        ragged = ragged_union(center=(200.0, 300.0), angle=angle)
        result = resolve_shots([*SEVEN_SINGLES, ragged], 10, D_PX)

        assert min_spacing(by_origin(result, ShotOrigin.ESTIMATED)) >= D_PX

    def test_synthetic_truth_outline_gets_three_estimated_inside_it(self):
        _, truth = render_named("ragged_10x7", 0)
        ppi = truth["ppi"]
        d_px = truth["bullet_diameter_in"] * ppi
        ragged_idx = set(truth["ragged_groups"][0])
        outline_px = np.rint(np.array(truth["ragged_outlines_in"][0]) * ppi).astype(np.int32)
        contour = outline_px.reshape(-1, 1, 2)
        ragged = Blob(
            BlobKind.RAGGED,
            tuple((int(x), int(y)) for x, y in outline_px),
            float(cv2.contourArea(contour)),
            (tuple(outline_px.mean(axis=0)),),
            0.6,
        )
        singles = [
            single(x * ppi, y * ppi)
            for i, (x, y) in enumerate(truth["holes_in"])
            if i not in ragged_idx
        ]
        assert len(singles) == 7

        result = resolve_shots([*singles, ragged], 10, d_px)

        assert result.status == "ok"
        estimated = by_origin(result, ShotOrigin.ESTIMATED)
        assert len(estimated) == 3
        assert len(result.shots_px) == 10
        for x, y, _, _ in estimated:
            assert cv2.pointPolygonTest(contour, (float(x), float(y)), False) >= 0


class TestNoRaggedShortfall:
    def test_shortfall_of_two_is_stacked_in_the_largest_hole(self):
        blobs = [single(50, 50), single(150, 50, area=SINGLE_AREA * 1.3), single(250, 50)]
        result = resolve_shots(blobs, 5, D_PX)

        assert result.status == "ok"
        estimated = by_origin(result, ShotOrigin.ESTIMATED)
        assert len(estimated) == 2
        assert [(s[0], s[1]) for s in estimated] == [(150.0, 50.0), (150.0, 50.0)]
        assert len(result.shots_px) == 5

    def test_largest_hole_for_a_split_blob_is_judged_per_center(self):
        blobs = [
            single(50, 50, area=SINGLE_AREA * 1.1),
            split([(150.0, 50.0), (170.0, 50.0)], area=SINGLE_AREA * 1.9),
        ]
        result = resolve_shots(blobs, 4, D_PX)

        estimated = by_origin(result, ShotOrigin.ESTIMATED)
        assert [(s[0], s[1]) for s in estimated] == [(50.0, 50.0)]

    def test_exact_count_adds_nothing_and_says_nothing(self):
        result = resolve_shots([single(50, 50), single(150, 50)], 2, D_PX)

        assert result.status == "ok"
        assert result.messages == ()
        assert by_origin(result, ShotOrigin.ESTIMATED) == []

    def test_no_blobs_returns_no_shots_and_asks_for_manual_entry(self):
        result = resolve_shots([], 3, D_PX)

        assert result.shots_px == ()
        assert result.status == "ok"
        assert result.messages and "by hand" in result.messages[0]


class TestStatuses:
    def test_more_distinct_holes_than_expected_is_too_many_with_no_fill(self):
        blobs = [single(50 + 60 * i, 50) for i in range(4)] + [ragged_union()]
        result = resolve_shots(blobs, 3, D_PX)

        assert result.status == "too_many"
        assert len(result.shots_px) == 4
        assert by_origin(result, ShotOrigin.ESTIMATED) == []
        assert "delete detection errors or raise the shot count" in result.messages[0]

    def test_shortfall_smaller_than_ragged_count_is_unassigned_ragged(self):
        small = _ragged_from_mask(circles_mask([(60, 60), (60 + R_PX, 60)]))
        medium = _ragged_from_mask(circles_mask([(200, 60), (200 + R_PX, 60), (200, 60 + R_PX)]))
        big = _ragged_from_mask(
            circles_mask([(60, 250), (60 + R_PX, 250), (60, 250 + R_PX), (60 + R_PX, 250 + R_PX)])
        )
        result = resolve_shots([single(300, 300), small, medium, big], 3, D_PX)

        assert result.status == "unassigned_ragged"
        assert len(result.shots_px) == 3
        estimated = by_origin(result, ShotOrigin.ESTIMATED)
        assert len(estimated) == 2
        assert sum(inside(big, x, y) for x, y, _, _ in estimated) == 1
        assert sum(inside(medium, x, y) for x, y, _, _ in estimated) == 1
        assert not any(inside(small, x, y) for x, y, _, _ in estimated)
        assert any("1 ragged hole(s) have no shot" in m for m in result.messages)

    def test_ragged_blob_with_zero_shortfall_is_unassigned_and_adds_no_shots(self):
        result = resolve_shots([single(50, 50), ragged_union()], 1, D_PX)

        assert result.status == "unassigned_ragged"
        assert [s[2] for s in result.shots_px] == [ShotOrigin.DETECTED]

    def test_each_ragged_blob_gets_a_shot_when_shortfall_covers_them(self):
        a = ragged_union(center=(100.0, 100.0))
        b = ragged_union(center=(300.0, 300.0))
        result = resolve_shots([a, b], 2, D_PX)

        assert result.status == "ok"
        assert sum(inside(a, x, y) for x, y, _, _ in result.shots_px) == 1
        assert sum(inside(b, x, y) for x, y, _, _ in result.shots_px) == 1


class TestAllocation:
    def _two_ragged(self):
        small = _ragged_from_mask(circles_mask([(100, 100), (100 + R_PX, 100)], shape=(500, 500)))
        large = _ragged_from_mask(
            circles_mask(
                [(300, 300), (300 + R_PX, 300), (300, 300 + R_PX), (300 + R_PX, 300 + R_PX)],
                radius=R_PX * 1.4,
                shape=(500, 500),
            )
        )
        return small, large

    def test_shortfall_is_shared_in_proportion_to_excess_area(self):
        small, large = self._two_ragged()
        excess_small = small.area_px - SINGLE_AREA
        excess_large = large.area_px - SINGLE_AREA
        shortfall = 6
        expected_large = round(shortfall * excess_large / (excess_small + excess_large))

        result = resolve_shots([small, large], shortfall, D_PX)

        in_large = sum(inside(large, x, y) for x, y, _, _ in result.shots_px)
        in_small = sum(inside(small, x, y) for x, y, _, _ in result.shots_px)
        assert in_small + in_large == shortfall
        assert in_large == expected_large
        assert in_small >= 1

    def test_every_ragged_blob_keeps_at_least_one_shot_when_one_dominates(self):
        small, large = self._two_ragged()

        result = resolve_shots([small, large], 3, D_PX)

        assert sum(inside(small, x, y) for x, y, _, _ in result.shots_px) >= 1
        assert len(result.shots_px) == 3


class TestOrigins:
    def test_split_blob_centers_are_split_shots_and_singles_are_detected(self):
        blobs = [
            single(50, 50, confidence=0.91),
            split([(150.0, 50.0), (175.0, 52.0)], confidence=0.8),
        ]
        result = resolve_shots(blobs, 3, D_PX)

        assert result.shots_px == (
            (50.0, 50.0, ShotOrigin.DETECTED, 0.91),
            (150.0, 50.0, ShotOrigin.SPLIT, 0.8),
            (175.0, 52.0, ShotOrigin.SPLIT, 0.8),
        )
        assert result.status == "ok"

    def test_split_shots_count_as_distinct_so_ragged_fill_is_only_the_remainder(self):
        ragged = ragged_union()
        blobs = [single(50, 50), split([(150.0, 50.0), (175.0, 52.0)]), ragged]

        result = resolve_shots(blobs, 6, D_PX)

        assert len(by_origin(result, ShotOrigin.SPLIT)) == 2
        assert len(by_origin(result, ShotOrigin.ESTIMATED)) == 3
        assert len(result.shots_px) == 6

    def test_single_blob_without_centers_falls_back_to_the_contour_mean(self):
        blob = Blob(BlobKind.SINGLE, ((10, 10), (30, 10), (30, 30), (10, 30)), 400.0, (), 0.5)

        result = resolve_shots([blob], 1, D_PX)

        assert result.shots_px == ((20.0, 20.0, ShotOrigin.DETECTED, 0.5),)


class TestPlacementGeometry:
    @pytest.mark.parametrize("count", [1, 2, 3, 4])
    def test_concave_outline_still_gets_points_inside(self, count):
        mask = np.zeros((300, 300), dtype=np.uint8)
        cv2.ellipse(mask, (150, 150), (90, 90), 0, 0, 360, 255, -1)
        cv2.circle(mask, (210, 150), 60, 0, -1)
        ragged = _ragged_from_mask(mask)

        result = resolve_shots([ragged], count, D_PX)

        assert len(result.shots_px) == count
        for x, y, origin, _ in result.shots_px:
            assert origin == ShotOrigin.ESTIMATED
            assert clearance(ragged, x, y) >= 1.0

    @pytest.mark.parametrize("gap_side", ["right", "top", "left"])
    def test_single_shot_in_a_c_shaped_outline_is_projected_into_the_outline(self, gap_side):
        """The kmeans seed of one shot is the mean of the outline, which lies in the C's hollow."""
        mask = np.zeros((400, 400), dtype=np.uint8)
        cv2.circle(mask, (200, 200), 120, 255, -1)
        cv2.circle(mask, (200, 200), 85, 0, -1)
        cut = {
            "right": ((250, 160), (330, 240)),
            "top": ((160, 70), (240, 150)),
            "left": ((70, 160), (150, 240)),
        }[gap_side]
        cv2.rectangle(mask, cut[0], cut[1], 0, -1)
        ragged = _ragged_from_mask(mask)
        mean_x, mean_y = np.argwhere(mask > 0).mean(axis=0)[::-1]
        assert not inside(ragged, mean_x, mean_y)

        result = resolve_shots([ragged], 1, D_PX)

        assert len(result.shots_px) == 1
        x, y, origin, _ = result.shots_px[0]
        assert origin == ShotOrigin.ESTIMATED
        assert clearance(ragged, x, y) >= 1.0

    def test_roomy_outline_reaches_full_bullet_diameter_spacing(self):
        mask = np.zeros((400, 400), dtype=np.uint8)
        cv2.circle(mask, (200, 200), 120, 255, -1)
        ragged = _ragged_from_mask(mask)

        result = resolve_shots([ragged], 5, D_PX)

        assert min_spacing(result.shots_px) >= D_PX

    def test_outline_too_small_for_spacing_still_places_every_shot_inside(self):
        mask = circles_mask([(100, 100)], radius=R_PX * 0.9, shape=(200, 200))
        ragged = _ragged_from_mask(mask)

        result = resolve_shots([ragged], 3, D_PX)

        assert len(result.shots_px) == 3
        assert all(clearance(ragged, x, y) >= 1.0 for x, y, _, _ in result.shots_px)

    def test_cramped_outline_keeps_the_widest_spacing_reached_while_spreading(self):
        mask = np.zeros((400, 400), dtype=np.uint8)
        cv2.ellipse(mask, (200, 200), (23, 25), 50.38, 0, 360, 255, -1)
        cv2.circle(mask, (220, 200), 12, 0, -1)
        ragged = _ragged_from_mask(mask)

        result = resolve_shots([ragged], 5, D_PX)

        assert all(inside(ragged, x, y) for x, y, _, _ in result.shots_px)
        assert min_spacing(result.shots_px) >= 19.0

    def test_tiny_outline_with_fewer_pixels_than_shots_does_not_fail(self):
        mask = np.zeros((20, 20), dtype=np.uint8)
        mask[5:7, 5:7] = 255
        ragged = _ragged_from_mask(mask)

        result = resolve_shots([ragged], 6, D_PX)

        assert len(result.shots_px) == 6
        assert all(inside(ragged, x, y) for x, y, _, _ in result.shots_px)

    def test_degenerate_outline_falls_back_to_the_blob_centroid(self):
        blob = Blob(BlobKind.RAGGED, ((10, 10), (20, 20)), 50.0, ((15.0, 15.0),), 0.5)

        result = resolve_shots([blob], 2, D_PX)

        assert [(s[0], s[1]) for s in result.shots_px] == [(15.0, 15.0), (15.0, 15.0)]
        assert all(s[2] == ShotOrigin.ESTIMATED for s in result.shots_px)


class TestDeterminismAndCounts:
    def _blobs(self):
        return [*SEVEN_SINGLES, ragged_union(center=(200.0, 300.0), angle=0.8)]

    def test_two_runs_give_identical_output(self):
        assert resolve_shots(self._blobs(), 10, D_PX) == resolve_shots(self._blobs(), 10, D_PX)

    def test_output_does_not_depend_on_global_random_state(self):
        roomy = _ragged_from_mask(circles_mask([(200, 200)], radius=130.0))
        first = resolve_shots(self._blobs(), 10, D_PX), resolve_shots([roomy], 6, D_PX)
        for seed in (1, 5, 987654, 31337):
            cv2.setRNGSeed(seed)
            np.random.seed(seed)
            again = resolve_shots(self._blobs(), 10, D_PX), resolve_shots([roomy], 6, D_PX)

            assert again == first

    def test_resolved_count_always_equals_expected_unless_too_many(self):
        rng = np.random.default_rng(11)
        for _ in range(25):
            n_single = int(rng.integers(0, 6))
            n_ragged = int(rng.integers(0, 3))
            blobs = [single(40 + 70 * i, 40) for i in range(n_single)]
            for j in range(n_ragged):
                blobs.append(
                    ragged_union(
                        center=(120.0 + 150 * j, 250.0),
                        angle=float(rng.uniform(0, 6)),
                        members=int(rng.integers(2, 4)),
                    )
                )
            expected = int(rng.integers(max(1, n_single), n_single + 6))

            result = resolve_shots(blobs, expected, D_PX)

            assert result.status in {"ok", "unassigned_ragged"}
            assert len(result.shots_px) == expected or (not blobs and not result.shots_px)

    def test_estimated_shots_always_follow_their_blob_in_input_order(self):
        ragged = ragged_union(center=(200.0, 300.0))
        result = resolve_shots([single(50, 50), ragged, single(400, 50)], 5, D_PX)

        assert [s[2] for s in result.shots_px] == [
            ShotOrigin.DETECTED,
            ShotOrigin.ESTIMATED,
            ShotOrigin.ESTIMATED,
            ShotOrigin.ESTIMATED,
            ShotOrigin.DETECTED,
        ]


class TestInputValidation:
    @pytest.mark.parametrize("expected", [0, -1])
    def test_non_positive_expected_count_is_rejected(self, expected):
        with pytest.raises(ValueError, match="shots_expected"):
            resolve_shots([single(1, 1)], expected, D_PX)

    @pytest.mark.parametrize("diameter", [0.0, -3.0, float("nan")])
    def test_non_positive_bullet_diameter_is_rejected(self, diameter):
        with pytest.raises(ValueError, match="bullet_diameter_px"):
            resolve_shots([single(1, 1)], 1, diameter)

    def test_blob_with_neither_centers_nor_contour_is_rejected(self):
        blob = Blob(BlobKind.SINGLE, (), 10.0, (), 0.5)
        with pytest.raises(ValueError, match="neither centers nor a contour"):
            resolve_shots([blob], 1, D_PX)
