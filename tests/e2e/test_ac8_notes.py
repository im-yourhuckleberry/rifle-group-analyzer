"""AC-8 journey: group notes, and the read-only earlier notes carried over from a former session, are shown with their
group and are searchable, and a `<script>` in any user text renders as text on every page that
shows it.
"""

from __future__ import annotations

import math

import pytest

from rga.domain.types import Offset
from tests.e2e.pages import App
from tests.support import seed

pytestmark = pytest.mark.e2e

PAYLOAD = "<script>window.__x=1</script>"
FORMER_NOTE = "Windy morning at the north range"
GROUP_NOTE = "Pulled the third shot low"
PERCENT_NOTE = "Humidity 50% at the bench"
PLAIN_NOTE = "Humidity 5000 reading is a typo"


def _ring(count: int) -> list[Offset]:
    return [
        Offset(0.5 + 0.3 * math.cos(i * 2.1), 0.2 + 0.3 * math.sin(i * 2.1)) for i in range(count)
    ]


def assert_inert(app: App, path: str) -> None:
    """Open `path`; the payload must not have run or been inserted as a script element."""
    app.goto(path)
    assert app.page.evaluate("() => typeof window.__x") == "undefined", path
    injected = app.page.evaluate(
        "() => [...document.scripts].filter((s) => s.textContent.includes('__x')).length"
    )
    assert injected == 0, path


@pytest.fixture
def world(app: App) -> dict[str, int]:
    rifle_id = app.create_rifle(name="Notes rifle")
    load_id = app.create_load(rifle_id, label="Notes load")
    ctx = app.group_context(rifle_id, distance_yd=100, legacy_notes=FORMER_NOTE)
    with app.seeding() as conn:
        group_id = seed.seed_group(
            conn, context=ctx, load=load_id, offsets=_ring(10), elevation_dialed_mil=0.5
        ).id
    return {"rifle": rifle_id, "load": load_id, "group": group_id}


def test_group_notes_and_earlier_notes_are_shown_and_searchable(
    app: App, world: dict[str, int]
) -> None:
    app.open_group(world["group"])
    assert app.earlier_notes() == FORMER_NOTE
    app.group_notes(GROUP_NOTE)

    app.open_group(world["group"])
    assert app.main.locator("textarea[name=notes]").input_value() == GROUP_NOTE
    assert app.earlier_notes() == FORMER_NOTE
    assert FORMER_NOTE not in app.main.locator("textarea[name=notes]").input_value()
    rows = app.group_rows(world["rifle"])
    assert [r["notes"] for r in rows] == [GROUP_NOTE]

    app.analysis_open()
    former_hits = app.analysis_search("north range")
    assert len(former_hits) == 1 and "group" in former_hits[0] and FORMER_NOTE in former_hits[0]
    group_hits = app.analysis_search("THIRD shot")
    assert len(group_hits) == 1 and "group" in group_hits[0] and GROUP_NOTE in group_hits[0]
    hit_link = app.page.locator("#search-results li a").first
    assert hit_link.get_attribute("href") == f"/groups/{world['group']}"
    assert app.analysis_search("no such words anywhere") == []
    assert app.page.locator("#search-empty").is_visible()


def test_the_earlier_notes_cannot_be_edited_and_a_group_without_them_shows_none(
    app: App, world: dict[str, int]
) -> None:
    ctx = app.group_context(world["rifle"], distance_yd=100)
    with app.seeding() as conn:
        plain = seed.seed_group(conn, context=ctx, load=world["load"], offsets=_ring(10)).id

    app.open_group(plain)
    assert app.earlier_notes() is None
    assert app.main.get_by_text("Earlier notes (carried over)").count() == 0
    app.open_group(world["group"])
    assert app.main.locator("#earlier-notes").locator("textarea, input").count() == 0
    assert app.main.locator("textarea[name=notes]").count() == 1


def test_a_group_matching_in_both_notes_is_one_hit(app: App, world: dict[str, int]) -> None:
    app.open_group(world["group"])
    app.group_notes("The north range again")

    app.analysis_open()
    hits = app.analysis_search("north range")

    assert len(hits) == 1
    assert "The north range again" in hits[0]


def test_percent_in_a_search_is_matched_literally(app: App, world: dict[str, int]) -> None:
    ctx = app.group_context(world["rifle"], distance_yd=100, legacy_notes=PERCENT_NOTE)
    with app.seeding() as conn:
        legacy = seed.seed_group(conn, context=ctx, load=world["load"], offsets=_ring(10)).id
        other = seed.seed_group(
            conn,
            context=app.group_context(world["rifle"], distance_yd=100),
            load=world["load"],
            offsets=_ring(10),
            notes=PLAIN_NOTE,
        ).id
    app.analysis_open()
    hits = app.analysis_search("50%")
    assert len(hits) == 1 and f"#{legacy}" in hits[0]
    assert len(app.analysis_search("%")) == 1
    assert app.analysis_search("5%") == [] and app.analysis_search("50% at") != []
    assert any(f"#{other}" in hit for hit in app.analysis_search("Humidity"))
    assert len(app.analysis_search("Humidity")) == 2


def test_script_in_user_text_renders_as_text_on_every_page(app: App) -> None:
    rifle_id = app.create_rifle(name=f"Rifle {PAYLOAD}")
    load_id = app.create_load(rifle_id, label=f"Load {PAYLOAD}", notes=f"Load notes {PAYLOAD}")
    ctx = app.group_context(rifle_id, distance_yd=100)
    draft = app.add_group(ctx, shots=3, notes=f"Draft {PAYLOAD}")
    with app.seeding() as conn:
        group_id = seed.seed_group(
            conn,
            context=ctx,
            load=load_id,
            offsets=_ring(10),
            elevation_dialed_mil=0.5,
            notes=f"Group {PAYLOAD}",
        ).id
        former_id = seed.seed_group(
            conn,
            context=app.group_context(rifle_id, distance_yd=100, legacy_notes=f"Former {PAYLOAD}"),
            load=load_id,
            offsets=_ring(10),
        ).id

    assert_inert(app, f"/rifles/{rifle_id}")
    notes = [r["notes"] for r in app.group_rows()]
    assert f"Draft {PAYLOAD}" in notes and f"Group {PAYLOAD}" in notes
    assert draft.group_id not in (group_id, former_id)

    assert_inert(app, f"/groups/{group_id}")
    assert app.main.locator("textarea[name=notes]").input_value() == f"Group {PAYLOAD}"
    assert_inert(app, f"/groups/{former_id}")
    assert app.earlier_notes() == f"Former {PAYLOAD}"

    assert_inert(app, "/analysis")
    app.analysis_open()
    hits = app.analysis_search("window.__x")
    assert len(hits) == 3 and all(PAYLOAD in hit for hit in hits)
    assert app.page.evaluate("() => typeof window.__x") == "undefined"
    assert app.page.locator("#search-results script").count() == 0

    assert_inert(app, f"/compare?rifle_id={rifle_id}")
    assert app.main.get_by_text(f"Load notes {PAYLOAD}", exact=True).is_visible()
    assert app.main.get_by_text(f"Load {PAYLOAD}").first.is_visible()

    assert_inert(app, f"/dope?load_id={load_id}")
    assert app.main.get_by_text(f"Load {PAYLOAD}").first.is_visible()
    assert len(app.dope_rows()) == 1
    assert_inert(app, "/dope")
