"""AC-1 journey with perspective: `skew10_grid` (10 degree tilt) confirmed with the 1 inch grid.

The grid proposal is rectified through its homography, the 10 shots are reviewed, and the extreme
spread on the group page matches the rendered truth within 0.05 in; the mean point of impact is
checked as well, because a wrong rectification shifts it before it changes the spread.
"""

from __future__ import annotations

import math

import pytest

from tests.e2e.pages import App

pytestmark = pytest.mark.e2e

ES_TOLERANCE_IN = 0.05
MEAN_TOLERANCE_IN = 0.05
DISTANCE_YD = 100


def test_skewed_grid_photo_gives_the_true_extreme_spread(app: App) -> None:
    rifle_id = app.create_rifle(name="Grid rifle")
    app.create_load(rifle_id, label="Grid load")
    ctx = app.group_context(rifle_id, distance_yd=DISTANCE_YD)
    target = app.render_target("skew10_grid")
    truth = target.truth
    indices = truth["groups"][0]["hole_indices"]

    ref = app.add_group(ctx, shots=len(indices), photo=target)
    app.confirm_reference("grid", candidate="truth", truth=truth)
    app.wait_path(r"/groups/\d+/review.*")
    app.review_wait_ready()
    assert app.review_counts() == (len(indices), len(indices)), app.review_counter()

    app.review_mark_aim(truth_aim_px=truth["groups"][0]["aim_px"], photo_id=ref.photo_id)
    app.review_save()
    app.review_finalize()

    stats = app.read_stats()
    holes = [truth["holes_in"][i] for i in indices]
    aim = truth["groups"][0]["aim_in"]
    truth_es = max(math.dist(a, b) for a in holes for b in holes)
    assert stats.n == len(indices)
    assert stats.es_in == pytest.approx(truth_es, abs=ES_TOLERANCE_IN)
    assert stats.mean_dx_in == pytest.approx(
        sum(h[0] for h in holes) / len(holes) - aim[0], abs=MEAN_TOLERANCE_IN
    )
    assert stats.mean_dy_in == pytest.approx(
        -(sum(h[1] for h in holes) / len(holes) - aim[1]), abs=MEAN_TOLERANCE_IN
    )
