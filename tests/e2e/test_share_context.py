"""Share image header follows the group's Context card (B-81).

Each edit made on the group page's Context card (suppressor, named suppressor, position, date,
time, distance) must change the header of the PNG the Share card previews and downloads. The
header band is compared as pixels before and after the edit; the exact header text, including a
long suppressor name, is pinned by `tests/integration/test_pages_b66.py` with a text spy.
"""

from __future__ import annotations

import io

import pytest
from PIL import Image
from playwright.sync_api import expect

from tests.e2e.pages import App
from tests.integration.test_group_plot import ring
from tests.support.seed import seed_context, seed_group

pytestmark = pytest.mark.e2e

HEADER_BAND_PX = 230


def header_band(app: App, href: str) -> bytes:
    response = app.context.request.get(f"{app.base}{href}")
    assert response.status == 200, response.status
    assert response.headers["content-type"] == "image/png"
    assert "no-store" in response.headers.get("cache-control", "")
    image = Image.open(io.BytesIO(response.body())).convert("RGB")
    return image.crop((0, 0, image.width, HEADER_BAND_PX)).tobytes()


def make_group(app: App, **context) -> int:
    rifle_id = app.create_rifle(name="Share rifle")
    app.create_load(rifle_id, label="Share load")
    load_id = app.load_id_of(rifle_id)
    with app.seeding() as conn:
        group = seed_group(
            conn,
            load=load_id,
            context=seed_context(rifle_id, date="2026-09-30", **context),
            offsets=ring(0.2, 0.1, n=10),
        )
    return group.id


def share_href(app: App, group_id: int) -> str:
    app.page.locator("#share-open").click()
    expect(app.page.locator("#share-status")).to_have_text("")
    preview = app.page.get_attribute("#share-preview", "src")
    assert app.page.get_attribute("#share-download", "href") == preview
    assert app.page.evaluate("() => document.getElementById('share-preview').naturalWidth") > 0
    return preview


EDITS = {
    "suppressor turned on": ({}, {"suppressor": True}),
    "a named suppressor": ({}, {"suppressor": True, "suppressor_name": "Omega 300"}),
    "suppressor turned off": ({"suppressor": True}, {"suppressor": False}),
    "position": ({}, {"position": "standing_field"}),
    "date": ({}, {"date": "2026-01-02"}),
    "time": ({}, {"time": "06:45"}),
    "distance": ({}, {"distance_yd": 150}),
}


@pytest.mark.parametrize("name", EDITS)
def test_a_context_edit_changes_the_share_image_header(app: App, name: str) -> None:
    initial, edit = EDITS[name]
    group_id = make_group(app, **initial)
    app.open_group(group_id)
    before = header_band(app, share_href(app, group_id))

    app.open_group(group_id)
    app.edit_group_context(**edit)
    after = header_band(app, share_href(app, group_id))

    assert after != before, f"{name} did not reach the share image"


def test_renaming_the_suppressor_changes_the_header_again_and_the_preview_refetches(
    app: App,
) -> None:
    group_id = make_group(app, suppressor=True)
    app.open_group(group_id)
    app.edit_group_context(suppressor=True, suppressor_name="Omega 300")
    first_href = share_href(app, group_id)
    first = header_band(app, first_href)

    app.edit_group_context(suppressor=True, suppressor_name="Thunder Beast Arms Ultra 7 Titanium")
    second_href = share_href(app, group_id)
    second = header_band(app, second_href)

    assert second != first
    assert second_href == first_href, "the address is stable; no-store makes it refetch the image"


def test_a_long_suppressor_name_is_not_cut_off_in_the_header(app: App) -> None:
    """Two names that differ only at their end draw different headers, so nothing is truncated."""
    group_id = make_group(app, position="standing_field", suppressor=True)
    app.open_group(group_id)
    app.edit_group_context(suppressor=True, suppressor_name="Thunder Beast Arms Ultra 7 Titanium")
    titanium = header_band(app, share_href(app, group_id))

    app.edit_group_context(suppressor=True, suppressor_name="Thunder Beast Arms Ultra 7 Carbon")
    carbon = header_band(app, share_href(app, group_id))

    assert carbon != titanium
