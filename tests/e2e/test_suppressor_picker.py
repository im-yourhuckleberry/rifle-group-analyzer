"""T4.0n (B-65): the named-suppressor dropdown on the add-group form and on the group page's
context card, driven in real Chromium.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.e2e.pages import App

pytestmark = pytest.mark.e2e

CHECK = "#g-suppressor"
CHOICE = "[data-suppressor-choice]"
SELECT = "#g-suppressor-name"
NEW_INPUT = "#g-suppressor-new"
SAVE_NEW = "[data-suppressor-save]"


def stored(app: App, group_id: int) -> tuple[int, int | None]:
    with app.db() as conn:
        row = conn.execute(
            "SELECT suppressor, suppressor_id FROM groups WHERE id = ?", (group_id,)
        ).fetchone()
    return row["suppressor"], row["suppressor_id"]


def suppressor_names(app: App) -> list[str]:
    with app.db() as conn:
        return [r["name"] for r in conn.execute("SELECT name FROM suppressors ORDER BY id")]


def group_count(app: App) -> int:
    with app.db() as conn:
        return conn.execute("SELECT COUNT(*) FROM groups").fetchone()[0]


def form_with_a_load(app: App) -> int:
    rifle_id = app.create_rifle()
    app.create_load(rifle_id)
    app.open_rifle(rifle_id)
    return rifle_id


def selected_text(app: App, select: str = SELECT) -> str:
    return app.page.locator(select).evaluate("s => s.selectedOptions[0].textContent")


def test_the_dropdown_shows_only_while_the_checkbox_is_checked(app: App) -> None:
    form_with_a_load(app)
    page = app.page

    assert not page.locator(CHOICE).first.is_visible()
    page.locator(CHECK).check()
    assert page.locator(CHOICE).first.is_visible()
    options = page.locator(f"{SELECT} option").all_inner_texts()
    assert options[0] == "Not named" and options[-1].startswith("Add new")
    page.locator(CHECK).uncheck()
    assert not page.locator(CHOICE).first.is_visible()


def test_add_new_saves_remembers_selects_and_names_the_group(app: App) -> None:
    rifle_id = form_with_a_load(app)
    page = app.page

    page.locator(CHECK).check()
    assert not page.locator(NEW_INPUT).is_visible()
    page.locator(SELECT).select_option(label="Add new…")
    assert page.locator(NEW_INPUT).is_visible()
    page.locator(NEW_INPUT).fill("Thunder Beast Ultra 7")
    page.locator(SAVE_NEW).click()

    page.locator(f"{SELECT} option", has_text="Thunder Beast Ultra 7").wait_for(state="attached")
    assert selected_text(app) == "Thunder Beast Ultra 7"
    assert not page.locator(NEW_INPUT).is_visible()
    assert suppressor_names(app) == ["Thunder Beast Ultra 7"]

    group = app.add_group_from_form(lot="LOT-S", shots=3)
    suppressor, suppressor_id = stored(app, group)
    assert suppressor == 1 and suppressor_id is not None
    assert (
        "suppressed: Thunder Beast Ultra 7"
        in app.main.locator(f"tr[data-group-id='{group}']").inner_text()
    )
    assert app.page.url.endswith(f"/rifles/{rifle_id}")


def test_a_saved_suppressor_is_offered_again_and_a_retyped_name_is_not_duplicated(
    app: App,
) -> None:
    form_with_a_load(app)
    app.api("POST", "/api/suppressors", {"name": "Omega"}, status=201)
    app.page.reload()
    page = app.page

    page.locator(CHECK).check()
    assert "Omega" in page.locator(f"{SELECT} option").all_inner_texts()
    page.locator(SELECT).select_option(label="Add new…")
    page.locator(NEW_INPUT).fill("  omega ")
    page.locator(NEW_INPUT).press("Enter")
    expect(page.locator(f"{SELECT} option:checked")).to_have_text("Omega")

    assert suppressor_names(app) == ["Omega"]
    assert page.locator(f"{SELECT} option", has_text="Omega").count() == 1


def test_unchecking_on_the_group_page_clears_the_named_suppressor(app: App) -> None:
    rifle_id = app.create_rifle()
    app.create_load(rifle_id)
    ctx = app.group_context(rifle_id, suppressor_name="Omega")
    group = app.create_group(ctx).group_id
    assert stored(app, group)[0] == 1 and stored(app, group)[1] is not None
    app.open_group(group)
    page = app.page
    assert page.locator("#c-suppressor").is_checked()
    assert "Omega" in app.group_header()

    app.edit_group_context(suppressor=False)

    assert stored(app, group) == (0, None)
    assert "Omega" not in app.group_header()


def test_saving_with_add_new_chosen_but_not_saved_is_refused_and_changes_nothing(
    app: App,
) -> None:
    form_with_a_load(app)
    page = app.page
    page.locator(CHECK).check()
    page.locator(SELECT).select_option(label="Add new…")
    page.locator(NEW_INPUT).fill("Never saved")
    page.locator("#g-lot").fill("LOT-X")

    page.locator("#add-group").click()
    page.locator("#flash").wait_for(state="visible")

    assert "Save the new suppressor first" in app.flash()
    assert group_count(app) == 0
    assert suppressor_names(app) == []


def test_saving_a_blank_new_name_shows_an_error_and_saves_nothing(app: App) -> None:
    form_with_a_load(app)
    page = app.page
    page.locator(CHECK).check()
    page.locator(SELECT).select_option(label="Add new…")

    page.locator(SAVE_NEW).click()
    page.locator("#flash").wait_for(state="visible")

    assert "Type the suppressor" in app.flash()
    assert suppressor_names(app) == []


def test_a_script_looking_name_is_shown_as_text_only(app: App) -> None:
    rifle_id = app.create_rifle()
    app.create_load(rifle_id)
    nasty = "<img src=x onerror=alert(1)>"
    app.api("POST", "/api/suppressors", {"name": nasty}, status=201)
    app.open_rifle(rifle_id)

    app.page.locator(CHECK).check()
    assert nasty in app.page.locator(f"{SELECT} option").all_inner_texts()
    assert app.page.locator(f"{SELECT} img").count() == 0
