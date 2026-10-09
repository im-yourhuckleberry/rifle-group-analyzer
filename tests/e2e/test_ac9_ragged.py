"""AC-9 (T4.10): a ragged hole holding several shots gets a best guess the shooter can see and move.

`ragged_10x7` (seven clean holes and one ragged hole holding three, 10 expected) goes through the
real UI. The editor draws three dashed "est" markers inside the ragged outline, and dragging one
of them widens the group; the finished group still draws them as estimated on its page.
"""

from __future__ import annotations

import cv2
import numpy as np
import pytest

from tests.e2e.pages import App
from tests.synth.generate import plane_to_image

pytestmark = pytest.mark.e2e

DRAG_SCREEN_PX = 90.0


def spread_px(app: App) -> float:
    """Extreme spread of the drawn shots, in working px (the editor shows no size line)."""
    points = [(s["cx"], s["cy"]) for s in app.review_shots()]
    return max(float(np.hypot(a[0] - b[0], a[1] - b[1])) for a in points for b in points)


def ragged_outline_working_px(app: App, target, photo_id: int) -> np.ndarray:
    """The truth outline of the ragged blob in the review editor's working pixels."""
    plane = np.array(target.truth["ragged_outlines_in"][0], dtype=float)
    image_px = plane_to_image(target.truth, plane)
    working = app.truth_to_working([(float(x), float(y)) for x, y in image_px], photo_id)
    return np.array(working, dtype=np.float32).reshape(-1, 1, 2)


def test_ragged_hole_gets_three_visible_best_guesses_the_shooter_can_move(app: App) -> None:
    rifle_id = app.create_rifle(cartridge="223 Remington", bullet_diameter_in=0.224)
    app.create_load(rifle_id)
    ctx = app.group_context(rifle_id)
    ref, target = app.photo_group_to_review(ctx, "ragged_10x7")
    expected = len(target.truth["groups"][0]["hole_indices"])
    assert expected == 10
    assert app.review_counts() == (10, 10), app.review_counter()

    shots = app.review_shots()
    estimated = [i for i, s in enumerate(shots) if s["origin"] == "estimated"]
    assert len(estimated) == 3, [s["origin"] for s in shots]
    outline = ragged_outline_working_px(app, target, ref.photo_id)
    for index in estimated:
        inside = cv2.pointPolygonTest(
            outline, (float(shots[index]["cx"]), float(shots[index]["cy"])), False
        )
        assert inside >= 0, (index, shots[index])
    assert app.page.locator("#editor-svg text.shot-label", has_text="est").count() == 3
    assert app.review_has_estimated_shots()
    assert all(s["origin"] != "estimated" for i, s in enumerate(shots) if i not in estimated)

    before = spread_px(app)
    centroid = np.mean([[s["cx"], s["cy"]] for s in shots], axis=0)
    mover = max(
        estimated,
        key=lambda i: np.hypot(shots[i]["cx"] - centroid[0], shots[i]["cy"] - centroid[1]),
    )
    sx, sy = app.working_to_client(shots[mover]["cx"], shots[mover]["cy"])
    cx, cy = app.working_to_client(float(centroid[0]), float(centroid[1]))
    norm = float(np.hypot(sx - cx, sy - cy)) or 1.0
    app.review_drag_shot(
        mover, DRAG_SCREEN_PX * (sx - cx) / norm, DRAG_SCREEN_PX * (sy - cy) / norm
    )
    after = spread_px(app)
    assert after > before + 5, (before, after)
    moved = app.review_shots()[mover]
    assert (moved["cx"], moved["cy"]) != (shots[mover]["cx"], shots[mover]["cy"])
    assert app.review_counts() == (10, 10)
    assert app.review_has_estimated_shots()

    app.review_mark_aim(truth_aim_px=target.truth["groups"][0]["aim_px"], photo_id=ref.photo_id)
    app.review_save()
    assert app.review_finalize_enabled(), app.review_finalize_reason()
    app.review_finalize()
    stats = app.read_stats()
    assert stats.n == 10
    assert stats.includes_estimated
