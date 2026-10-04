"""AC-7 journey: the per-load dope table from groups shot at 100, 300 and 500 yd.

Three photographed groups, each with an elevation dialed, are reviewed and finalized through the
UI. The table's centered elevation is the dialed elevation minus the group's center height in mil
(checked against the rendered truth). Zeroing groups, short zeroing groups, drafts and groups with
no recorded elevation never appear, and the trued muzzle velocity is shown as information only.
"""

from __future__ import annotations

import math

import pytest

from rga.domain.types import Offset
from tests.e2e.pages import App
from tests.support import seed

pytestmark = pytest.mark.e2e

CENTER_TOLERANCE_MIL = 0.03
ROUNDING_MIL = 0.011
TRUED_MV_FPS = 2765
LOAD_LABEL = "Dope load"
DISTANCES_YD = (100, 300, 500)
DIALED_MIL = {100: 0.5, 300: 1.9, 500: 4.2}
EXCLUDED_DIALED_MIL = 9.9
EXCLUDED_ZEROING_GROUPS = (
    ("zero_step1", 3),
    ("zero_check", 2),
    ("zero_step2", 5),
    ("zero_confirm", 9),
)


def _mil(inches: float, distance_yd: int) -> float:
    return inches * 1000.0 / (distance_yd * 36.0)


def _ring(count: int) -> list[Offset]:
    return [Offset(0.3 * math.cos(i * 2.1), 0.3 * math.sin(i * 2.1)) for i in range(count)]


def test_dope_page_without_a_load_shows_the_picker_and_it_leads_to_the_table(app: App) -> None:
    rifle_id = app.create_rifle(name="Picker rifle")
    load_id = app.create_load(rifle_id, label="Picker load")
    app.dope_open()
    assert app.page.locator("#dope-table").count() == 0
    assert app.main.get_by_role("heading", name="Choose a load").is_visible()
    link = app.main.get_by_role("link", name="Picker rifle: Picker load")
    assert link.get_attribute("href") == f"/dope?load_id={load_id}"
    link.click()
    app.wait_path(rf"/dope\?load_id={load_id}$")
    assert app.dope_rows() == []
    assert "No finalized groups" in app.main.locator("#dope-table").inner_text()


def test_dope_rows_show_centered_elevation_at_three_distances(app: App) -> None:
    rifle_id = app.create_rifle(name="Dope rifle")
    load_id = app.create_load(rifle_id, label=LOAD_LABEL, trued_mv_fps=TRUED_MV_FPS)

    truths = {}
    contexts = {}
    for seed_no, distance in enumerate(DISTANCES_YD):
        contexts[distance] = app.group_context(rifle_id, distance_yd=distance)
        ref, target = app.photo_group_to_review(
            contexts[distance], "clean_circle1", seed=seed_no, elevation_mil=DIALED_MIL[distance]
        )
        truths[distance] = target.group_truth()
        app.review_mark_aim(truth_aim_px=target.truth["groups"][0]["aim_px"], photo_id=ref.photo_id)
        app.review_save()
        app.review_finalize()

    with app.seeding() as conn:
        ctx = contexts[100]
        for purpose, count in EXCLUDED_ZEROING_GROUPS:
            seed.seed_group(
                conn,
                context=ctx,
                load=load_id,
                offsets=_ring(count),
                purpose=purpose,
                elevation_dialed_mil=EXCLUDED_DIALED_MIL,
            )
        seed.seed_group(
            conn,
            context=ctx,
            load=load_id,
            offsets=_ring(10),
            elevation_dialed_mil=EXCLUDED_DIALED_MIL,
            finalize=False,
        )
        seed.seed_group(conn, context=ctx, load=load_id, offsets=_ring(10))

    app.dope_open(load_id)
    rows = app.dope_rows()
    assert [int(r[0]) for r in rows] == list(DISTANCES_YD)
    for row, distance in zip(rows, DISTANCES_YD, strict=True):
        _, _, up_in, shots = truths[distance]
        dialed, center, centered = float(row[2]), float(row[3]), float(row[4])
        truth_center = _mil(up_in, distance)
        assert dialed == DIALED_MIL[distance]
        assert int(row[5]) == shots
        assert center == pytest.approx(truth_center, abs=CENTER_TOLERANCE_MIL)
        assert centered == pytest.approx(dialed - truth_center, abs=CENTER_TOLERANCE_MIL)
        assert centered == pytest.approx(dialed - center, abs=ROUNDING_MIL)
    table_text = app.main.locator("#dope-table").inner_text()
    assert f"{EXCLUDED_DIALED_MIL:.2f}" not in table_text

    info = app.main.get_by_text("Trued muzzle velocity")
    assert str(TRUED_MV_FPS) in info.inner_text()
    assert "information only" in info.inner_text()
    assert str(TRUED_MV_FPS) not in table_text


@pytest.mark.parametrize("purpose", ["zero_step2", "zero_confirm"])
def test_ten_shot_zeroing_group_is_in_the_dope_table(app: App, purpose: str) -> None:
    rifle_id = app.create_rifle()
    load_id = app.create_load(rifle_id, label=LOAD_LABEL)
    ctx = app.group_context(rifle_id, distance_yd=100)
    with app.seeding() as conn:
        seed.seed_group(
            conn,
            context=ctx,
            load=load_id,
            offsets=_ring(10),
            purpose=purpose,
            elevation_dialed_mil=2.2,
        )
        seed.seed_group(
            conn,
            context=ctx,
            load=load_id,
            offsets=_ring(9),
            purpose=purpose,
            elevation_dialed_mil=EXCLUDED_DIALED_MIL,
        )
    app.dope_open(load_id)
    rows = app.dope_rows()
    assert len(rows) == 1
    assert (int(rows[0][0]), float(rows[0][2]), int(rows[0][5])) == (100, 2.2, 10)
    assert f"{EXCLUDED_DIALED_MIL:.2f}" not in app.main.locator("#dope-table").inner_text()
