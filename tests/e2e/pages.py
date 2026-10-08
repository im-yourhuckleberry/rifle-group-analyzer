"""Page helpers for the Playwright journeys: drive the real UI the way a shooter would.

`App` wraps one Playwright page on a live server. Its methods are grouped by page (rifles, loads,
groups, photos and references, the review editor, the group page, zeroing, analysis, compare,
dope) and every one acts through the UI; `api()` and `db()` exist only for set-up and read-back.
Conventions that keep journeys stable:
- Counts and text are read inside `main` (`App.main`): the header holds the brand and navigation;
  the Offline/Online radios live on /settings, inside `main`.
- Waits are locator waits; `page.wait_for_function` is blocked by the page's CSP.
- A page that navigates after a click is awaited with `page.wait_for_url`.
- Pointer positions for the review editor are derived from the SVG viewBox, so they are exact at
  any zoom (`App.working_to_client`).
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import time as clock
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import cv2
import numpy as np
from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import expect

from rga.config import Settings
from rga.vision.transform import apply_h
from tests.synth.generate import render_named

ORIGIN_HEADER = "Origin"
NAV_PATHS = ("/", "/rifles", "/groups", "/analysis", "/compare", "/dope", "/settings")
REFERENCE_KINDS = {
    "grid": "grid_1in",
    "circle1": "circle_1in",
    "circle1.5": "circle_1_5in",
    "circle3": "circle_3in",
}
_RENDER_CACHE: dict[tuple[str, int], tuple[np.ndarray, dict]] = {}


class ConsoleCollector:
    """Records JavaScript errors of a page and fails the test if any were not allowed.

    Collects uncaught page exceptions and error-level console messages (which include the
    browser's "Failed to load resource ... status of 4xx" lines). `allow` declares messages a
    journey expects, such as the 409 of a refused finalize.
    """

    def __init__(self, page) -> None:
        self.errors: list[str] = []
        self._allowed: list[re.Pattern[str]] = []
        self._tolerated: list[str] = []
        page.on("pageerror", lambda error: self._record(f"pageerror: {error}"))
        page.on("console", self._on_console)

    def _on_console(self, message) -> None:
        if message.type == "error":
            self._record(f"console.error: {message.text}")

    def _record(self, text: str) -> None:
        self.errors.append(text)
        if any(p.search(text) for p in self._allowed):
            self._tolerated.append(text)

    @contextmanager
    def allow(self, *patterns: str) -> Iterator[None]:
        """Allow messages matching any regex in `patterns` while the block runs."""
        added = [re.compile(p) for p in patterns]
        self._allowed.extend(added)
        try:
            yield
        finally:
            for pattern in added:
                self._allowed.remove(pattern)

    def unexpected(self) -> list[str]:
        """The recorded errors that were not matched by an `allow` active when they arrived."""
        remaining = list(self._tolerated)
        out = []
        for error in self.errors:
            if error in remaining:
                remaining.remove(error)
            else:
                out.append(error)
        return out

    def clear(self) -> None:
        """Forget everything recorded so far (for a test that provoked errors on purpose)."""
        self.errors.clear()
        self._tolerated.clear()

    def assert_clean(self) -> None:
        """Raise AssertionError listing every unexpected JavaScript error."""
        assert not self.unexpected(), "JavaScript errors:\n" + "\n".join(self.unexpected())


@dataclass(frozen=True)
class RenderedTarget:
    """A synthetic target photo on disk with its ground truth."""

    path: Path
    truth: dict
    name: str
    seed: int

    def group_truth(self, group: int = 0) -> tuple[float, float, float, int]:
        """(extreme spread in, mean right in, mean up in, shot count) of a rendered group.

        Offsets are from the group's aim point, y up, from the truth hole positions.
        """
        spec = self.truth["groups"][group]
        holes = [self.truth["holes_in"][i] for i in spec["hole_indices"]]
        spread = max(math.dist(a, b) for a in holes for b in holes)
        right = sum(h[0] for h in holes) / len(holes) - spec["aim_in"][0]
        up = -(sum(h[1] for h in holes) / len(holes) - spec["aim_in"][1])
        return spread, right, up, len(holes)

    @property
    def size_px(self) -> tuple[int, int]:
        """(width, height) of the rendered photo in pixels."""
        w, h = self.truth["image_size"]
        return int(w), int(h)


@dataclass(frozen=True)
class GroupRef:
    """Ids returned when a group is added; `photo_id` is set when a photo was uploaded with it."""

    group_id: int
    photo_id: int | None = None


@dataclass(frozen=True)
class GroupContext:
    """Where, when and how a group is shot (a group carries its own context; there are no sessions).

    `date` is the local `YYYY-MM-DD` day, `time` an optional local `HH:MM:SS`. `suppressor_name`
    picks a named suppressor in the page's picker. `legacy_notes` only matters when seeding: it
    fills the read-only "Earlier notes (carried over)". Weather is entered only through the group
    form or the API (`weather=` of `create_group`).
    """

    rifle_id: int
    distance_yd: int = 100
    date: str = ""
    time: str | None = None
    position: str = "bench"
    suppressor: bool = False
    suppressor_name: str | None = None
    legacy_notes: str = ""

    def group_fields(self) -> dict[str, object]:
        """The `create_group` keyword arguments of this context (so it also works in `seed_group`)."""
        return {
            "shot_date": self.date,
            "shot_time": self.time,
            "utc_offset": None,
            "distance_yd": self.distance_yd,
            "position": self.position,
            "suppressor": self.suppressor,
            "suppressor_id": None,
            "temperature_f": None,
            "density_altitude_ft": None,
            "wind_mph": None,
            "wind_clock": None,
            "weather_skipped": self.distance_yd >= 200,
            "legacy_session_notes": self.legacy_notes,
        }


@dataclass(frozen=True)
class Stats:
    """Numbers read from the group page's statistics tiles."""

    n: int
    es_in: float
    es_moa: float
    mean_dx_in: float | None
    mean_dy_in: float | None
    poi_text: str | None
    includes_estimated: bool

    def mean_mil(self, distance_yd: float) -> tuple[float, float] | None:
        """(right, up) mean point of impact in mil at `distance_yd`, or None when it is hidden."""
        if self.mean_dx_in is None or self.mean_dy_in is None:
            return None
        scale = 1000.0 / (distance_yd * 36.0)
        return self.mean_dx_in * scale, self.mean_dy_in * scale


@dataclass
class App:
    """One browser page on a live server, with helpers for every screen of the app."""

    page: object
    context: object
    server: object
    tmp_path: Path
    console: ConsoleCollector
    _rendered: dict[tuple[str, int], RenderedTarget] = field(default_factory=dict)

    # ------------------------------------------------------------------------------ basics

    @property
    def base(self) -> str:
        """The server's base URL, no trailing slash."""
        return str(self.server).rstrip("/")

    @property
    def main(self):
        """Locator for `<main>`; scope text and counts to it (the header holds only the brand and navigation)."""
        return self.page.locator("main")

    @property
    def settings(self) -> Settings:
        """Settings pointing at the live server's data directory (paths only)."""
        return Settings(data_dir=self.server.data_dir, port=self.server.port)

    def goto(self, path: str) -> None:
        """Open `path` on the server and wait for the load event."""
        self.page.goto(f"{self.base}{path}")

    def path(self) -> str:
        """Path and query of the current page."""
        match = re.match(r"https?://[^/]+(.*)", self.page.url)
        return match[1] if match else self.page.url

    def wait_path(self, pattern: str) -> re.Match[str]:
        """Wait until the current path matches the regex `pattern`; returns the match."""
        compiled = re.compile(pattern)
        self.page.wait_for_url(re.compile(rf"^{re.escape(self.base)}{pattern}"))
        match = compiled.search(self.path())
        assert match is not None, (pattern, self.path())
        return match

    def _read_page(self, script: str, arg: object = None, timeout_s: float = 10.0):
        """Evaluate `script` in the page as one atomic read.

        A read that spans a navigation fails with "execution context destroyed"; it is retried on
        the next page. Reading state and acting on it in separate calls races with navigation,
        so a journey that must decide from the page reads it here.
        """
        deadline = clock.monotonic() + timeout_s
        while True:
            try:
                return self.page.evaluate(script, arg)
            except PlaywrightError:
                if clock.monotonic() > deadline:
                    raise
                self.page.wait_for_timeout(50)

    def visible_text(self, selector: str) -> str | None:
        """Text of the element when it exists and is shown, else None (one atomic read)."""
        return self._read_page(
            """(selector) => {
              const e = document.querySelector(selector);
              return e && !e.hidden && e.getClientRects().length > 0 ? e.innerText : null;
            }""",
            selector,
        )

    def flash(self) -> str | None:
        """Text of the page's `#flash` message, or None when it is hidden."""
        return self.visible_text("#flash")

    def nav_links(self) -> list[str]:
        """Labels of the main navigation links."""
        return self.page.locator("header nav a").all_inner_texts()

    # ------------------------------------------------------------------- set-up and read-back

    def api(self, method: str, path: str, body: object | None = None, *, status: int | None = 200):
        """Call the JSON API with the page's cookies and a same-origin `Origin` header.

        For set-up only; journeys should use the UI for what they test. Returns the parsed JSON
        body. When `status` is not None the response must have that status (default 200).
        """
        response = self.context.request.fetch(
            f"{self.base}{path}",
            method=method,
            data=None if body is None else json.dumps(body),
            headers={"Content-Type": "application/json", ORIGIN_HEADER: self.base},
        )
        if status is not None:
            assert response.status == status, (method, path, response.status, response.text())
        try:
            return response.json()
        except ValueError:
            return None

    @contextmanager
    def db(self) -> Iterator[sqlite3.Connection]:
        """A read-only connection to the server's database (read-back and fixtures)."""
        path = self.settings.db_path
        connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    @contextmanager
    def seeding(self) -> Iterator[sqlite3.Connection]:
        """A read-write connection for seeding with `tests.support.seed` (not through the UI)."""
        from rga.db.connection import connect

        connection = connect(self.settings.db_path)
        try:
            yield connection
        finally:
            connection.close()

    # ------------------------------------------------------------------------ synthetic photos

    def render_target(self, scenario: str, seed: int = 0) -> RenderedTarget:
        """Render catalog scenario `scenario` to a PNG in `tmp_path`; renders are cached per run."""
        key = (scenario, seed)
        if key not in self._rendered:
            if key not in _RENDER_CACHE:
                _RENDER_CACHE[key] = render_named(scenario, seed)
            image, truth = _RENDER_CACHE[key]
            path = self.tmp_path / f"{scenario}_{seed}.png"
            if not path.exists():
                assert cv2.imwrite(str(path), image)
            self._rendered[key] = RenderedTarget(path, truth, scenario, seed)
        return self._rendered[key]

    # ------------------------------------------------------------------------------- rifles

    def create_rifle(
        self,
        name: str = "Test rifle",
        cartridge: str = "6.5 Creedmoor",
        bullet_diameter_in: float | None = None,
        scope: str | None = None,
    ) -> int:
        """Add a rifle on /rifles and return its id (the page moves to its detail page)."""
        self.goto("/rifles")
        form = self.main.locator('form[data-form="new-rifle"]')
        form.locator("#name").fill(name)
        form.locator("#cartridge").fill(cartridge)
        form.locator("#cartridge").dispatch_event("change")
        if bullet_diameter_in is not None:
            form.locator("#bullet_diameter_in").select_option(f"{bullet_diameter_in:.3f}")
        if scope:
            form.locator("#scope").fill(scope)
        form.get_by_role("button", name="Add rifle").click()
        return int(self.wait_path(r"/rifles/(\d+)$")[1])

    def create_load(
        self,
        rifle_id: int,
        label: str = "Factory 140 ELD-M",
        kind: str = "factory",
        **fields: str | float,
    ) -> int:
        """Add a load to a rifle (UI) and return its id; `fields` are the form field names."""
        self.goto(f"/rifles/{rifle_id}")
        form = self.main.locator('form[data-form="new-load"]')
        form.locator("#kind").select_option(kind)
        form.locator("#label").fill(label)
        for name, value in fields.items():
            if kind == "handload" and name == "bullet_weight_gr":
                name = "handload_bullet_weight_gr"  # the new-load form has a factory field of the same name
            form.locator(f"[name={name}]").fill(str(value))
        form.get_by_role("button", name="Add load").click()
        card = self.main.locator("article[data-load-id]").filter(has_text=label).last
        card.wait_for()
        return int(card.get_attribute("data-load-id"))

    def set_zero_load(self, rifle_id: int, load_label: str) -> None:
        """Choose the rifle's zero load on its detail page."""
        self.goto(f"/rifles/{rifle_id}")
        form = self.main.locator('form[data-form="zero-load"]')
        form.locator("#zero-load-select").select_option(label=load_label)
        form.get_by_role("button", name="Set zero load").click()
        self.main.locator(".badge", has_text="zero load").first.wait_for()

    # ------------------------------------------------------------------------ group context

    def group_context(
        self,
        rifle_id: int,
        distance_yd: int = 100,
        on: date | str | None = None,
        position: str = "bench",
        suppressor: bool = False,
        time: str | None = None,
        suppressor_name: str | None = None,
        legacy_notes: str = "",
    ) -> GroupContext:
        """Describe the context of the next group (nothing is created; `on` defaults to today)."""
        return GroupContext(
            rifle_id=rifle_id,
            distance_yd=distance_yd,
            date=str(on) if on is not None else date.today().isoformat(),
            time=time,
            position=position,
            suppressor=suppressor or suppressor_name is not None,
            suppressor_name=suppressor_name,
            legacy_notes=legacy_notes,
        )

    def upload_photo_api(self, photo: RenderedTarget | Path) -> dict:
        """Upload a photo through `POST /api/photos` (set-up back door); returns the PhotoOut."""
        path = photo.path if isinstance(photo, RenderedTarget) else Path(photo)
        response = self.context.request.post(
            f"{self.base}/api/photos",
            multipart={
                "file": {
                    "name": path.name,
                    "mimeType": "image/png" if path.suffix.lower() == ".png" else "image/jpeg",
                    "buffer": path.read_bytes(),
                }
            },
            headers={ORIGIN_HEADER: self.base},
        )
        assert response.status == 201, (response.status, response.text())
        return response.json()

    def load_id_of(self, rifle_id: int, label: str | None = None) -> int:
        """The id of the rifle's load named `label`, or of its first load."""
        with self.db() as conn:
            if label is None:
                row = conn.execute(
                    "SELECT id FROM loads WHERE rifle_id = ? ORDER BY id LIMIT 1", (rifle_id,)
                ).fetchone()
            else:
                row = conn.execute(
                    "SELECT id FROM loads WHERE rifle_id = ? AND label = ?", (rifle_id, label)
                ).fetchone()
        assert row is not None, (rifle_id, label)
        return int(row["id"])

    def create_group(
        self,
        ctx: GroupContext,
        lot: str = "LOT-1",
        shots: int = 5,
        load_label: str | None = None,
        elevation_mil: float | None = None,
        notes: str = "",
        photo: RenderedTarget | Path | None = None,
        weather: dict[str, object] | None = None,
        status: int | None = 201,
    ) -> GroupRef:
        """Create a group through `POST /api/groups` (set-up back door); returns its ids.

        The group carries `ctx`; `weather` holds `temperature_f`, `density_altitude_ft`,
        `wind_mph`, `wind_clock` or `weather_skipped`. With a photo, it is uploaded first and
        linked. Use `add_group` to drive the page instead.
        """
        photo_id = None if photo is None else int(self.upload_photo_api(photo)["id"])
        body: dict[str, object] = {
            "load_id": self.load_id_of(ctx.rifle_id, load_label),
            "lot_code": lot,
            "shots_expected": shots,
            "shot_date": ctx.date,
            "distance_yd": ctx.distance_yd,
            "position": ctx.position,
            "suppressor": ctx.suppressor,
            "notes": notes,
            "elevation_dialed_mil": elevation_mil,
            "photo_id": photo_id,
            **(weather or {}),
        }
        if ctx.time is not None:
            body["shot_time"] = ctx.time
        if ctx.suppressor_name is not None:
            body["suppressor_id"] = self._suppressor_id(ctx.suppressor_name)
        created = self.api("POST", "/api/groups", body, status=status)
        return GroupRef(int(created["id"]), photo_id)

    def _suppressor_id(self, name: str) -> int:
        with self.db() as conn:
            row = conn.execute("SELECT id FROM suppressors WHERE name = ?", (name,)).fetchone()
        if row is not None:
            return int(row["id"])
        return int(self.api("POST", "/api/suppressors", {"name": name}, status=201)["id"])

    # ------------------------------------------------------------------------ groups on the rifle page

    def open_rifle(self, rifle_id: int) -> None:
        """Open the rifle page and wait for its add-group card."""
        self.goto(f"/rifles/{rifle_id}")
        self.page.locator("#new-group-card").wait_for()

    def set_group_distance(self, distance_yd: int) -> None:
        """Type the distance into the add-group form (the weather gate follows it live)."""
        self.page.locator("#g-distance").fill(str(distance_yd))
        self.page.locator("#g-distance").dispatch_event("input")

    def weather_required_visible(self) -> bool:
        """True when the form says weather is required (distance of 200 yd or more)."""
        return self.page.locator("#weather-required").is_visible()

    def add_group_enabled(self) -> bool:
        """True when the form's Add group button can be pressed (the weather gate lets it)."""
        return self.page.locator("#add-group").is_enabled()

    def group_form_skip_weather(self) -> None:
        """Press Skip weather on the add-group form."""
        panel = self.page.locator("#weather-panel")
        if panel.get_attribute("open") is None:
            panel.locator("summary").click()
        self.page.locator("#skip-weather").click()
        self.page.locator("#weather-skipped-badge").wait_for()

    def group_form_weather(self, temperature_f: float, **optional: float) -> None:
        """Enter weather on the add-group form (`density_altitude_ft`, `wind_mph`, `wind_clock`)."""
        panel = self.page.locator("#weather-panel")
        if panel.get_attribute("open") is None:
            panel.locator("summary").click()
        self.page.locator("#w-temp").fill(str(temperature_f))
        ids = {"density_altitude_ft": "w-da", "wind_mph": "w-wind", "wind_clock": "w-clock"}
        for name, value in optional.items():
            self.page.locator(f"#{ids[name]}").fill(str(value))

    def group_form_photo(self, photo: RenderedTarget | Path) -> str:
        """Choose a photo on the add-group form; returns the date source the form settles on.

        The photo uploads at once and fills the date and time: "from photo", or
        "entered (the photo has no date)".
        """
        path = photo.path if isinstance(photo, RenderedTarget) else Path(photo)
        self.page.locator("#g-photo").set_input_files(str(path))
        source = self.page.locator("#g-date-source")
        expect(source).to_have_text(re.compile(r"^(from photo|entered \(the photo has no date\))$"))
        return source.inner_text().strip()

    def add_group(
        self,
        ctx: GroupContext,
        lot: str = "LOT-1",
        shots: int = 5,
        load_label: str | None = None,
        elevation_mil: float | None = None,
        notes: str = "",
        photo: RenderedTarget | Path | None = None,
        weather: dict[str, float] | None = None,
        keep_photo_date: bool = False,
    ) -> GroupRef:
        """Add a group on the rifle page; with a photo the app moves on to the reference page.

        The group carries `ctx`. A photo is chosen first (it fills the date and time); `ctx`'s date
        and time then replace them unless `keep_photo_date`. From 200 yd the weather gate needs
        `weather` (a temperature and optional extras) or, by default, is skipped. Returns the new
        group's id and, with a photo, its photo id (read from the URL the page moves to: the
        reference page, or the review page when a reference was reused).
        """
        self.open_rifle(ctx.rifle_id)
        before = self.main.locator("#groups-card tr[data-group-id]").count()
        if photo is not None:
            self.group_form_photo(photo)
        if not (photo is not None and keep_photo_date):
            self.page.locator("#g-date").fill(ctx.date)
            if ctx.time is not None:
                self.page.locator("#g-time").fill(ctx.time[:5])
        self.set_group_distance(ctx.distance_yd)
        self.page.locator("#g-position").select_option(ctx.position)
        if ctx.suppressor:
            self.page.locator("#g-suppressor").check()
            if ctx.suppressor_name is not None:
                self._pick_suppressor("g", ctx.suppressor_name)
        if ctx.distance_yd >= 200:
            if weather:
                self.group_form_weather(**weather)  # type: ignore[arg-type]
            else:
                self.group_form_skip_weather()
        if load_label is not None:
            self.page.locator("#g-load").select_option(label=load_label)
        self.page.locator("#g-lot").fill(lot)
        self.page.locator("#g-shots").fill(str(shots))
        if elevation_mil is not None:
            self.page.locator("#g-elevation").fill(str(elevation_mil))
        if notes:
            self.page.locator("#g-notes").fill(notes)
        self.page.locator("#add-group").click()
        if photo is not None:
            match = self.wait_path(
                r"/(?:photos/(\d+)/reference\?group_id=(\d+)|groups/(\d+)/review.*)"
            )
            if match[2]:
                return GroupRef(int(match[2]), int(match[1]))
            return GroupRef(int(match[3]), self._photo_of_group(int(match[3])))
        self.main.locator("#groups-card tr[data-group-id]").nth(before).wait_for()
        newest = max(row["id"] for row in self.group_rows())
        return GroupRef(newest)

    def add_group_from_form(self, lot: str = "LOT-1", shots: int = 5) -> int:
        """Fill the lot and shots of the add-group form as it stands and press Add group.

        For journeys that set other fields of the form by hand first. Returns the new group's id
        (the page reloads without a photo).
        """
        before = {r["id"] for r in self.group_rows()}
        self.page.locator("#g-lot").fill(lot)
        self.page.locator("#g-shots").fill(str(shots))
        self.page.locator("#add-group").click()
        self.page.locator("#groups-card tr[data-group-id]").first.wait_for()
        fresh = {r["id"] for r in self.group_rows()} - before
        assert len(fresh) == 1, fresh
        return fresh.pop()

    def _pick_suppressor(self, prefix: str, name: str) -> None:
        """Choose a suppressor by name in the picker with `prefix` (g, c or ag), adding it if new."""
        select = self.page.locator(f"#{prefix}-suppressor-name")
        names = select.locator("option").all_inner_texts()
        if name in names:
            select.select_option(label=name)
            return
        select.select_option("__new__")
        self.page.locator(f"#{prefix}-suppressor-new").fill(name)
        self.page.locator(f"#{prefix}-suppressor-new").press("Enter")
        expect(select.locator("option", has_text=name)).to_have_count(1)
        select.select_option(label=name)

    def _photo_of_group(self, group_id: int) -> int | None:
        with self.db() as conn:
            row = conn.execute("SELECT photo_id FROM groups WHERE id = ?", (group_id,)).fetchone()
        return None if row is None else row["photo_id"]

    def group_rows(self, rifle_id: int | None = None) -> list[dict]:
        """Rows of the rifle page's groups list, day by day (newest day first).

        Each row has id, day, time ("none" or HH:MM), distance_yd, position text, load/lot text,
        shots text ("n of m"), status, notes and the `has_photo` flag. Opens the rifle page first
        when `rifle_id` is given.
        """
        if rifle_id is not None:
            self.open_rifle(rifle_id)
        out = []
        days = self.main.locator("#groups-card .day")
        for d in range(days.count()):
            day = days.nth(d)
            rows = day.locator("tbody tr[data-group-id]")
            for index in range(rows.count()):
                row = rows.nth(index)
                cells = row.locator("td").all_inner_texts()
                out.append(
                    {
                        "id": int(row.get_attribute("data-group-id")),
                        "label": " ".join(cells[0].split()),
                        "day": day.get_attribute("data-day"),
                        "time": cells[1].strip(),
                        "distance_yd": int(cells[2].split()[0]),
                        "position": cells[3].strip(),
                        "load_lot": " ".join(cells[4].split()),
                        "shots": cells[5].strip(),
                        "status": cells[8].strip().split("\n")[0].strip(),
                        "notes": cells[9].strip(),
                        "has_photo": row.get_attribute("data-photo-id") is not None,
                    }
                )
        return out

    def upload_photo(self, group_id: int, photo: RenderedTarget | Path, rifle_id: int) -> int:
        """Import a photo for an existing group from the rifle page; returns the photo id."""
        self.open_rifle(rifle_id)
        path = photo.path if isinstance(photo, RenderedTarget) else Path(photo)
        self.page.locator(f'.js-import[data-group-id="{group_id}"]').set_input_files(str(path))
        match = self.wait_path(r"/(?:photos/(\d+)/reference.*|groups/\d+/review.*)")
        return int(match[1]) if match[1] else self._photo_of_group(group_id)

    # ------------------------------------------------------------------- reference confirmation

    def _overlay_points(self, points: Sequence[tuple[float, float]]) -> None:
        """Click image-pixel points on the reference page's overlay."""
        for x, y in points:
            cx, cy = self._overlay_client(x, y)
            self.page.mouse.click(cx, cy)

    def _overlay_client(self, x: float, y: float) -> tuple[float, float]:
        box = self.page.locator("#overlay").bounding_box()
        width = float(self.page.locator("#reference-app").get_attribute("data-width"))
        height = float(self.page.locator("#reference-app").get_attribute("data-height"))
        return box["x"] + x / width * box["width"], box["y"] + y / height * box["height"]

    def find_references(self, kind: str) -> int:
        """Choose the printed reference kind (`grid`, `circle1`, `circle1.5`, `circle3` or a raw
        value such as `grid_1in`), press Find, and return how many proposals appeared."""
        value = REFERENCE_KINDS.get(kind, kind)
        self.page.locator("#kind").select_option(value)
        self.page.locator("#find").click()
        self.page.locator("#candidates-panel").wait_for()
        self.page.locator("#find:enabled").wait_for()
        return self.page.locator('#candidates input[name="candidate"]').count()

    def candidate_centres(self) -> list[tuple[float, float]]:
        """Centre, in image px, of each drawn proposal in index order (before one is selected)."""
        return self.page.evaluate(
            """() => [...document.querySelectorAll('#overlay > *')].map((node) => {
              if (node.tagName === 'ellipse') return [Number(node.getAttribute('cx')), Number(node.getAttribute('cy'))];
              if (node.tagName === 'polygon') {
                const pts = node.getAttribute('points').split(' ').map((p) => p.split(',').map(Number));
                return [pts.reduce((s, p) => s + p[0], 0) / pts.length, pts.reduce((s, p) => s + p[1], 0) / pts.length];
              }
              return null;
            }).filter((c) => c !== null)"""
        )

    @staticmethod
    def truth_reference_centre(truth: dict) -> tuple[float, float]:
        """Centre of the printed reference in the rendered image's pixels."""
        ref = truth["reference"]
        if ref["kind"] == "grid":
            xs, ys = zip(*ref["corners_px"], strict=True)
            return sum(xs) / len(xs), sum(ys) / len(ys)
        return tuple(ref["ellipse_center_px"])

    def confirm_reference(
        self,
        kind: str,
        candidate: int | str = 0,
        manual: Sequence[tuple[float, float]] | None = None,
        squares: tuple[int, int] | None = None,
        truth: dict | None = None,
        wait_for_review: bool = True,
    ) -> list[str]:
        """Confirm the reference on the reference page and return the warnings shown, if any.

        `candidate` is a proposal index, or "truth" to pick the proposal nearest the printed
        reference of `truth` (an aim dot can look like a 1 inch circle). With `manual` (image-px
        marks: 3+ circle edge points, or 4 grid corners TL, TR, BR, BL plus `squares=(across, down)`)
        the reference is marked by hand instead. When it confirms without warnings the app moves to
        the review page and, with `wait_for_review`, this waits for it.
        """
        if manual is None:
            count = self.find_references(kind)
            assert count > 0, "no reference proposals were found"
            index = candidate
            if candidate == "truth":
                assert truth is not None, "candidate='truth' needs the truth dict"
                target = self.truth_reference_centre(truth)
                centres = self.candidate_centres()
                index = min(
                    range(len(centres)), key=lambda i: np.hypot(*(np.array(centres[i]) - target))
                )
            self.page.locator('#candidates input[name="candidate"]').nth(int(index)).check()
            self.page.locator("#confirm-candidate").click()
        else:
            self.page.locator("#kind").select_option(REFERENCE_KINDS.get(kind, kind))
            self.page.locator("#manual-toggle").click()
            self._overlay_points(manual)
            if squares is not None:
                self.page.locator("#squares-x").fill(str(squares[0]))
                self.page.locator("#squares-y").fill(str(squares[1]))
            self.page.locator("#confirm-manual").click()
        return self._settle_reference(wait_for_review)

    def _settle_reference(self, wait_for_review: bool) -> list[str]:
        """Wait for what a confirm leads to and return the warnings shown, if any.

        The outcome is the review page (no warnings), or the reference page's result card; with
        `wait_for_review` a card that offers a review link is followed. Each look at the page is one
        atomic read, so a navigation between a look and the next step cannot fail it.
        """
        review = re.compile(rf"^{re.escape(self.base)}/groups/\d+/review")
        deadline = clock.monotonic() + 30
        while clock.monotonic() < deadline:
            state = self._read_page(
                """() => {
                  const result = document.getElementById("result");
                  const shown = result && !result.hidden && result.getClientRects().length > 0;
                  const next = document.getElementById("continue");
                  return {
                    url: location.href,
                    shown: !!shown,
                    warnings: shown
                      ? [...document.querySelectorAll("#result-warnings .notice")].map((e) => e.innerText)
                      : [],
                    next: next ? next.getAttribute("href") : null,
                  };
                }"""
            )
            if review.match(state["url"]):
                return []
            if state["shown"]:
                if wait_for_review and state["next"] and "/review" in state["next"]:
                    with suppress(PlaywrightError):
                        self.page.locator("#continue").click(timeout=3000)
                    self.page.wait_for_url(review)
                return state["warnings"]
            self.page.wait_for_timeout(100)
        raise AssertionError(
            f"neither the result card nor the review page appeared: {self.page.url}"
        )

    @staticmethod
    def circle_edge_points(truth: dict, count: int = 6) -> list[tuple[float, float]]:
        """`count` points on the printed circle's edge, in image px, for a manual reference."""
        ref = truth["reference"]
        cx, cy = ref["ellipse_center_px"]
        major, minor = ref["axes_px"][0] / 2, ref["axes_px"][1] / 2
        angle = np.radians(ref.get("angle_deg", 0.0))
        points = []
        for k in range(count):
            t = 2 * np.pi * k / count
            ex, ey = major * np.cos(t), minor * np.sin(t)
            points.append(
                (
                    cx + ex * np.cos(angle) - ey * np.sin(angle),
                    cy + ex * np.sin(angle) + ey * np.cos(angle),
                )
            )
        return [(float(x), float(y)) for x, y in points]

    # --------------------------------------------------------------------------- review editor

    def review_wait_ready(self) -> None:
        """Wait for the review editor to draw its photo and for the automatic detect to finish."""
        self.page.locator("#editor-svg image").wait_for()
        self.page.locator("#detect-button:enabled").wait_for()

    def review_counter(self) -> str:
        """The "N of M shots" counter text."""
        return self.page.locator("#shot-counter").inner_text().strip()

    def review_counts(self) -> tuple[int, int]:
        """(shots on the plot, expected) parsed from the counter."""
        match = re.match(r"(\d+) of (\d+) shots", self.review_counter())
        assert match, self.review_counter()
        return int(match[1]), int(match[2])

    def review_set_expected(self, count: int) -> None:
        """Type the expected shot count into the editor."""
        self.page.locator("#expected-input").fill(str(count))
        self.page.locator("#expected-input").dispatch_event("change")

    def review_detect(self) -> None:
        """Press Detect holes and wait for the detect response, then until the editor is idle."""
        with self.page.expect_response(re.compile(r"/api/groups/\d+/detect(?:\?.*)?$")):
            self.page.locator("#detect-button").click()
        self.page.locator("#detect-button:enabled").wait_for()

    def review_fill(self) -> None:
        """Press Fill shortfall (it saves first) and wait until the editor is idle again."""
        with self.page.expect_response(re.compile(r"/api/groups/\d+/fill-shortfall$")):
            self.page.locator("#fill-button").click()
        self.page.locator("#fill-button:enabled").wait_for()

    def review_set_region(self, x0: float, y0: float, x1: float, y1: float) -> None:
        """Press "Set region" and drag a rectangle over working-px (x0, y0)-(x1, y1) (editor at Fit)."""
        self.review_zoom("fit")
        button = self.page.locator("#region-button")
        if button.inner_text().strip() != "Drag a region":
            button.click()
        sx, sy = self.working_to_client(x0, y0)
        ex, ey = self.working_to_client(x1, y1)
        self.page.mouse.move(sx, sy)
        self.page.mouse.down()
        self.page.mouse.move(ex, ey, steps=8)
        self.page.mouse.up()

    def review_save(self) -> None:
        """Press Save and wait until it finishes."""
        with self.page.expect_response(re.compile(r"/api/groups/\d+/review$")):
            self.page.locator("#save-button").click()
        self.page.locator("#save-button:enabled").wait_for()

    def review_shots(self) -> list[dict]:
        """The drawn shots in order: class (origin), centre in working px, radius, selected."""
        return self.page.evaluate(
            """() => [...document.querySelectorAll('#editor-svg circle.shot')].map((c) => {
              const cls = c.getAttribute('class').split(' ');
              return {origin: cls[1], cx: Number(c.getAttribute('cx')), cy: Number(c.getAttribute('cy')),
                      r: Number(c.getAttribute('r')), selected: cls.includes('selected')};
            })"""
        )

    def review_view(self) -> dict:
        """The editor's viewBox (working px) and the SVG's on-screen rectangle."""
        return self.page.evaluate(
            """() => {
              const svg = document.getElementById('editor-svg');
              const [x, y, w, h] = svg.getAttribute('viewBox').split(' ').map(Number);
              const r = svg.getBoundingClientRect();
              return {x, y, w, h, left: r.left, top: r.top, width: r.width, height: r.height};
            }"""
        )

    def working_to_client(self, x: float, y: float) -> tuple[float, float]:
        """Screen position of working-px point (x, y), after revealing it if it is zoomed out of
        view (the editor is set to Fit when the point lies outside the current view)."""
        view = self.review_view()
        if not (
            view["x"] <= x <= view["x"] + view["w"] and view["y"] <= y <= view["y"] + view["h"]
        ):
            self.review_zoom("fit")
            view = self.review_view()
        return (
            view["left"] + (x - view["x"]) / view["w"] * view["width"],
            view["top"] + (y - view["y"]) / view["h"] * view["height"],
        )

    def review_zoom(self, how: str) -> float:
        """Zoom the editor: "in", "out", "fit" or "region"; returns the zoom level shown."""
        button = {
            "in": "#zoom-in-button",
            "out": "#zoom-out-button",
            "fit": "#zoom-fit-button",
            "region": "#zoom-region-button",
        }[how]
        self.page.locator(button).click()
        return float(self.page.locator("#zoom-level").inner_text().rstrip("×x").strip())

    def review_zoom_to(self, level: float) -> float:
        """Press the zoom buttons until the zoom is at least `level`; returns the level reached."""
        self.review_zoom("fit")
        current = 1.0
        while current < level:
            current = self.review_zoom("in")
        return current

    def review_click(self, x: float, y: float) -> None:
        """Click working-px point (x, y) on the editor."""
        self.page.mouse.click(*self.working_to_client(x, y))

    def review_drag_shot(self, index: int, dx_screen: float, dy_screen: float) -> None:
        """Drag shot `index` by a screen distance, in small steps, and release."""
        shot = self.review_shots()[index]
        sx, sy = self.working_to_client(shot["cx"], shot["cy"])
        self.page.mouse.move(sx, sy)
        self.page.mouse.down()
        self.page.mouse.move(sx + dx_screen, sy + dy_screen, steps=8)
        self.page.mouse.up()

    def review_select_shot(self, index: int) -> None:
        """Click shot `index` to select it."""
        shot = self.review_shots()[index]
        self.page.mouse.click(*self.working_to_client(shot["cx"], shot["cy"]))

    def review_delete_selected(self) -> None:
        """Press "Not a bullet hole" for the selected shot (the only delete control)."""
        self.page.locator("#delete-button").click()

    def review_aim_suggestion_visible(self) -> bool:
        """True while the editor offers possible aim marks (none is chosen for the user)."""
        return self.page.locator("#aim-suggestion").is_visible()

    def review_aim_status(self) -> str:
        """Text of the aim status line."""
        return self.page.locator("#aim-status").inner_text().strip()

    def review_use_suggested_aim(self, nexts: int = 0) -> None:
        """Pick a suggested aim mark: press Next `nexts` times (the first press selects the first
        mark; none is selected beforehand), then "Use this aim"."""
        self.page.locator("#aim-suggestion").wait_for()
        for _ in range(max(nexts, 1)):
            self.page.locator("#aim-next-button").click()
        self.page.locator("#aim-use-button").click()
        self.page.locator("#aim-status", has_text="Aim point is set").wait_for()

    def review_mark_aim(
        self,
        working_px: tuple[float, float] | None = None,
        *,
        truth_aim_px: tuple[float, float] | None = None,
        photo_id: int | None = None,
    ) -> str:
        """Mark the aim by hand: press "Mark aim" and click the mark; the page snaps to its centre.

        Give the working-px position, or `truth_aim_px` (the rendered photo's aim in image px) plus
        `photo_id` to convert it through the confirmed reference. Returns the aim status text
        ("Aim snapped to the mark." or "Aim set where you clicked.").
        """
        if working_px is None:
            assert truth_aim_px is not None and photo_id is not None
            working_px = self.truth_to_working([truth_aim_px], photo_id)[0]
        if self.review_aim_suggestion_visible():
            self.page.locator("#aim-manual-button").click()
        else:
            self.page.locator("#aim-button").click()
        self.review_click(*working_px)
        self.page.locator("#aim-status", has_text="Aim point is set").wait_for()
        return self.review_aim_status()

    def truth_to_working(
        self, points_px: Sequence[tuple[float, float]], photo_id: int
    ) -> list[tuple[float, float]]:
        """Map rendered-image pixels to the photo's working pixels using its confirmed reference.

        Reads the stored transform from the database (read-only); the working frame has its origin
        at the top-left of the warped photo and `working_ppi` pixels per inch.
        """
        with self.db() as conn:
            row = conn.execute(
                "SELECT h_px_to_plane_json, working_ppi, width_px, height_px FROM photos WHERE id = ?",
                (photo_id,),
            ).fetchone()
        assert row is not None and row["h_px_to_plane_json"], "the photo has no confirmed reference"
        h = np.array(json.loads(row["h_px_to_plane_json"]), dtype=float)
        w, ht = row["width_px"], row["height_px"]
        corners = apply_h(h, np.array([[0, 0], [w, 0], [w, ht], [0, ht]], dtype=float))
        origin = corners.min(axis=0)
        mapped = (apply_h(h, np.array(points_px, dtype=float)) - origin) * row["working_ppi"]
        return [(float(x), float(y)) for x, y in mapped]

    def review_finalize(self, expect_url: str | None = r"/(?:groups|rifles)/\d+(?:\?.*)?$") -> None:
        """Press Finalize, confirm it (it saves first) and wait for the page it moves to.

        A group made on the rifle page returns there after finalizing; this then opens the group's
        own page so a journey can read its statistics. Pass `expect_url=None` to stay on the page
        (for a refused finalize).
        """
        match = re.search(r"/groups/(\d+)/review", self.path())
        self.page.locator("#finalize-button").click()
        self.page.locator("#finalize-confirm-button").click()
        if expect_url is None:
            return
        self.wait_path(expect_url)
        if match and not re.search(rf"/groups/{match[1]}(?:\?.*)?$", self.path()):
            self.open_group(int(match[1]))

    def review_finalize_enabled(self) -> bool:
        """True when the Finalize button is enabled."""
        return self.page.locator("#finalize-button").is_enabled()

    def review_finalize_reason(self) -> str:
        """The reason text shown while Finalize is disabled (empty when it is enabled)."""
        return self.page.locator("#finalize-reason").inner_text().strip()

    def review_estimated_badge_visible(self) -> bool:
        """True when the "includes estimated shots" badge is shown in the editor."""
        return self.page.locator("#estimated-badge").is_visible()

    def review_spread_text(self) -> str:
        """The live extreme-spread line of the editor."""
        return self.page.locator("#spread-line").inner_text().strip()

    def open_review(self, group_id: int, return_to: str | None = None) -> None:
        """Open a group's review page and wait for it to be ready."""
        suffix = f"?return_to={return_to}" if return_to else ""
        self.goto(f"/groups/{group_id}/review{suffix}")
        self.review_wait_ready()

    def photo_group_to_review(
        self,
        ctx: GroupContext,
        scenario: str,
        *,
        lot: str = "LOT-1",
        shots: int | None = None,
        kind: str = "circle1",
        seed: int = 0,
        load_label: str | None = None,
        elevation_mil: float | None = None,
        weather: dict[str, float] | None = None,
    ) -> tuple[GroupRef, RenderedTarget]:
        """The common start of a journey: add a group with a photo and confirm its reference.

        Renders `scenario`, adds the group (shots default to the truth hole count), confirms the
        proposal that is the printed reference, and returns with the review editor ready.
        """
        target = self.render_target(scenario, seed)
        expected = shots if shots is not None else len(target.truth["groups"][0]["hole_indices"])
        ref = self.add_group(
            ctx,
            lot=lot,
            shots=expected,
            photo=target,
            load_label=load_label,
            elevation_mil=elevation_mil,
            weather=weather,
        )
        self.confirm_reference(kind, candidate="truth", truth=target.truth)
        self.wait_path(r"/groups/\d+/review.*")
        self.review_wait_ready()
        return ref, target

    # ------------------------------------------------------------------------------ group page

    def read_stats(self) -> Stats:
        """Read the statistics tiles of the group page the browser is on."""
        tiles: dict[str, tuple[str, str]] = {}
        for tile in self.main.locator(".stat").all():
            label = tile.locator(".label").inner_text().strip().lower()
            value = tile.locator(".value").inner_text().strip()
            sub = tile.locator(".sub").inner_text().strip() if tile.locator(".sub").count() else ""
            tiles[label] = (value, sub)
        es_value, es_sub = tiles["extreme spread"]
        mean = tiles.get("mean poi")
        dx = dy = None
        if mean is not None:
            match = re.match(r"(-?[\d.]+) in right, (-?[\d.]+) in up", mean[1])
            if match:
                dx, dy = float(match[1]), float(match[2])
        return Stats(
            n=int(tiles["shots"][0]),
            es_in=float(es_value.split()[0]),
            es_moa=float(es_sub.split()[0]),
            mean_dx_in=dx,
            mean_dy_in=dy,
            poi_text=None if mean is None else mean[0],
            includes_estimated=self.main.locator(
                ".badge", has_text="includes estimated shots"
            ).count()
            > 0,
        )

    def open_group(self, group_id: int) -> None:
        """Open the group results page."""
        self.goto(f"/groups/{group_id}")
        self.page.locator(".group-plot, #group-table, main h1").first.wait_for()

    def group_toggle(self, layer: str, on: bool | None = None) -> bool:
        """Read or set a group-page plot toggle: "photo", "circle", "poi" or "correction"."""
        box = self.page.locator(f"#toggle-{layer}")
        if on is True:
            box.check()
        elif on is False:
            box.uncheck()
        return box.is_checked()

    def group_toggle_available(self, layer: str) -> bool:
        """True when the toggle exists and is enabled."""
        box = self.page.locator(f"#toggle-{layer}")
        return box.count() > 0 and box.is_enabled()

    def group_layer_visible(self, layer: str) -> bool:
        """True when the plot layer (`photo`, `circle`, `poi`, `correction`) is displayed."""
        return self.page.evaluate(
            """(layer) => { const e = document.querySelector('.layer-' + layer);
              return !!e && getComputedStyle(e).display !== 'none'; }""",
            layer,
        )

    def group_correction_text(self) -> str | None:
        """The correction text when it is shown, else None."""
        text = self.visible_text("#correction-text")
        return text.strip() if text is not None else None

    def group_correction_reason(self) -> str | None:
        """Why the correction is withheld, or None when it is offered."""
        why = self.page.locator("#correction-why")
        return why.inner_text().strip() if why.count() else None

    def group_info_card(self, open_: bool | None = None) -> bool:
        """Open or close the correction "i" card (button toggles); returns whether it is open."""
        button, card = self.page.locator("#correction-info"), self.page.locator("#correction-card")
        if open_ is not None and card.is_visible() != open_:
            button.click()
        return card.is_visible()

    def group_shot_opacities(self) -> list[float]:
        """Computed opacity of every shot circle on the group plot."""
        return self.page.evaluate(
            "() => [...document.querySelectorAll('.group-plot .shot')].map((e) => Number(getComputedStyle(e).opacity))"
        )

    def group_notes(self, text: str) -> None:
        """Save group notes on the group page."""
        field_ = self.main.locator('[data-form="group-notes"] textarea[name="notes"]')
        field_.fill(text)
        with self.page.expect_navigation():
            self.main.get_by_role("button", name="Save notes").click()
        self.main.locator('[data-form="group-notes"]').wait_for()

    def earlier_notes(self) -> str | None:
        """The read-only "Earlier notes (carried over)" on the group page, or None when absent."""
        box = self.page.locator("#earlier-notes")
        return box.inner_text().strip() if box.count() else None

    def group_header(self) -> str:
        """The group page's header line (rifle, load, lot, date, distance, position, suppressor...)."""
        return " ".join(self.page.locator("#group-header").inner_text().split())

    def edit_group_context(
        self,
        *,
        date: str | None = None,
        time: str | None = None,
        distance_yd: int | None = None,
        position: str | None = None,
        suppressor: bool | None = None,
        suppressor_name: str | None = None,
        temperature_f: float | None = None,
        skip_weather: bool | None = None,
        save: bool = True,
    ) -> None:
        """Change fields of the group page's Context card and (by default) press Save."""
        if date is not None:
            self.page.locator("#c-date").fill(date)
        if time is not None:
            self.page.locator("#c-time").fill(time)
        if distance_yd is not None:
            self.page.locator("#c-distance").fill(str(distance_yd))
            self.page.locator("#c-distance").dispatch_event("input")
        if position is not None:
            self.page.locator("#c-position").select_option(position)
        if suppressor is not None:
            box = self.page.locator("#c-suppressor")
            box.check() if suppressor else box.uncheck()
        if suppressor_name is not None:
            self._pick_suppressor("c", suppressor_name)
        if temperature_f is not None or skip_weather is not None:
            panel = self.page.locator("#c-weather")
            if panel.get_attribute("open") is None:
                panel.locator("summary").click()
            if temperature_f is not None:
                self.page.locator("#c-temp").fill(str(temperature_f))
            if skip_weather is not None:
                skip = self.page.locator("#c-skip-weather")
                skip.check() if skip_weather else skip.uncheck()
        if save:
            with self.page.expect_navigation():
                self.page.locator("#c-save").click()
            self.page.locator("#context-card").wait_for()

    # -------------------------------------------------------------------------------- zeroing

    def create_zero(
        self,
        rifle_id: int,
        load_id: int,
        lot_code: str | None = "LOT-1",
        mode: str = "new_setup",
        *,
        via_api: bool = False,
    ) -> int:
        """Start a zero from the rifle page's "Start a zero" form and return its id.

        The page moves to /zeroing/<id>. `via_api=True` calls POST /api/zeros instead (set-up
        fallback); the id is then read from the API response and the zeroing page is opened.
        """
        if via_api:
            body = {"rifle_id": rifle_id, "load_id": load_id, "lot_code": lot_code, "mode": mode}
            zero = self.api("POST", "/api/zeros", body, status=None)
            assert isinstance(zero, dict) and "id" in zero, zero
            self.goto(f"/zeroing/{zero['id']}")
            zero_id = int(zero["id"])
        else:
            self.goto(f"/rifles/{rifle_id}")
            form = self.main.locator('form[data-form="new-zero"]')
            form.locator("#zero-start-load").select_option(str(load_id))
            form.locator("#zero-start-lot").fill(lot_code or "")
            form.locator("#zero-start-mode").select_option(mode)
            form.get_by_role("button", name="Start a zero").click()
            zero_id = int(self.wait_path(r"/zeroing/(\d+)$")[1])
        self.page.locator("#stepper li").first.wait_for()
        return zero_id

    def zero_rows(self, rifle_id: int) -> list[dict]:
        """Rows of the rifle page's zero list: id, state, and the cell texts."""
        self.goto(f"/rifles/{rifle_id}")
        rows = self.main.locator("tr[data-zero-id][data-state]")
        return [
            {
                "id": int(rows.nth(i).get_attribute("data-zero-id")),
                "state": rows.nth(i).get_attribute("data-state"),
                "cells": [c.strip() for c in rows.nth(i).locator("td").all_inner_texts()],
            }
            for i in range(rows.count())
        ]

    def zeroing_summary(self) -> str:
        """The zeroing page's one-line summary (zero id, mode and state)."""
        summary = self.page.locator("#zero-summary")
        expect(summary).not_to_have_text("Loading...")
        return summary.inner_text().strip()

    def zeroing_steps(self) -> list[tuple[str, str]]:
        """(label, state) of each stepper item; state is "current", "done" or "".."""
        items = self.page.locator("#stepper li")
        out = []
        for index in range(items.count()):
            item = items.nth(index)
            classes = (item.get_attribute("class") or "").split()
            out.append(
                (
                    item.inner_text().strip(),
                    "current" if "current" in classes else "done" if "done" in classes else "",
                )
            )
        return out

    def zeroing_add_group(
        self,
        shots: int | None = None,
        lot: str | None = None,
        on: str | None = None,
        time: str | None = None,
        position: str | None = None,
    ) -> int:
        """Create the next zeroing group (or the zero-check group once complete); returns its id.

        The group is at 100 yd; `on`, `time` and `position` replace the form's defaults (today,
        no time, bench).
        """
        card = self.page.locator("#add-group-card")
        card.wait_for()
        if on is not None:
            self.page.locator("#ag-date").fill(on)
        if time is not None:
            self.page.locator("#ag-time").fill(time)
        if position is not None:
            self.page.locator("#ag-position").select_option(position)
        if shots is not None:
            self.page.locator("#ag-shots").fill(str(shots))
        if lot is not None:
            self.page.locator("#ag-lot").fill(lot)
        note = self.page.locator("#ag-next")
        previous = self.visible_text("#ag-next") or ""
        self.page.locator("#ag-create").click()
        note.wait_for()
        if previous:
            expect(note).not_to_have_text(previous)
        match = re.search(r"Group #(\d+)", note.inner_text())
        assert match, note.inner_text()
        return int(match[1])

    def zeroing_advice(self) -> dict:
        """What the advice card shows: message, correction text (or None), and stats tiles text."""
        self.page.wait_for_load_state("networkidle")
        state = self._read_page(
            """() => {
              const shown = (e) => e && !e.hidden && e.getClientRects().length > 0;
              const card = document.getElementById("advice-card");
              if (!shown(card)) return null;
              const correction = document.getElementById("advice-correction");
              return {
                message: document.getElementById("advice-message").innerText,
                correction: shown(correction) ? correction.innerText : null,
                stats: [...document.querySelectorAll("#advice-stats .stat")].map((e) => e.innerText),
              };
            }"""
        )
        if state is None:
            return {"visible": False, "message": None, "correction": None, "stats": []}
        return {
            "visible": True,
            "message": state["message"].strip(),
            "correction": None if state["correction"] is None else state["correction"].strip(),
            "stats": state["stats"],
        }

    def zeroing_groups(self) -> list[dict]:
        """Rows of the zeroing group table: group id, step label, status, shots."""
        rows = self.page.locator("#groups-body tr")
        out = []
        for index in range(rows.count()):
            cells = rows.nth(index).locator("td").all_inner_texts()
            out.append(
                {
                    "group": int(cells[0].lstrip("#")),
                    "step": cells[1],
                    "status": cells[2],
                    "shots": int(cells[3]),
                }
            )
        return out

    def zeroing_record_dialed(self, up_mil: float, right_mil: float) -> None:
        """Record the correction actually dialed (whole 0.1 mil clicks)."""
        self.page.locator("#dl-up").fill(str(up_mil))
        self.page.locator("#dl-right").fill(str(right_mil))
        with self.page.expect_response(re.compile(r"/api/zeros/\d+/dialed$")) as sent:
            self.page.locator("#dl-save").click()
        assert sent.value.ok, sent.value.text()
        self.page.locator("#advice-card, #stepper").first.wait_for()

    def zeroing_dot(self) -> tuple[str, str]:
        """(suggestion text, dot size field value) of the dot card."""
        return (
            self.page.locator("#dot-suggestion").inner_text().strip(),
            self.page.locator("#dot-size").input_value(),
        )

    def zeroing_set_dot(self, size_in: float | None = None) -> None:
        """Set the dot (the suggested size unless `size_in` is given) and complete the zero."""
        if size_in is not None:
            self.page.locator("#dot-size").fill(str(size_in))
        with self.page.expect_response(re.compile(r"/api/zeros/\d+/dot$")) as sent:
            self.page.locator("#dot-save").click()
        assert sent.value.ok, sent.value.text()
        self.page.locator("#stepper li.done").last.wait_for()

    def zeroing_run_check(self, group_id: int | None = None) -> dict:
        """Run the zero check on a finalized check group; returns its message, route and result."""
        if group_id is not None:
            self.page.locator("#ck-group").select_option(str(group_id))
        history = self.page.locator("#ck-history li")
        before = history.count()
        with self.page.expect_response(re.compile(r"/api/zeros/\d+/checks$")):
            self.page.locator("#ck-run").click()
        box = self.page.locator("#ck-result")
        box.wait_for()
        expect(history).to_have_count(before + 1)
        return {
            "message": box.locator("p").first.inner_text().strip(),
            "route": box.locator("li").all_inner_texts(),
            "passed": "info" in (box.get_attribute("class") or ""),
        }

    def zeroing_check_history(self) -> list[str]:
        """Entries of the zero-check history list."""
        return self.page.locator("#ck-history li").all_inner_texts()

    # -------------------------------------------------------------------------------- analysis

    def analysis_open(self) -> None:
        """Open /analysis and wait for the group picker to fill."""
        self.goto("/analysis")
        self.page.locator("#picker-body").wait_for(state="attached")
        self.page.locator("#picker-body tr, #picker-empty:not([hidden])").first.wait_for()

    def analysis_pick(self, group_ids: Sequence[int]) -> None:
        """Tick exactly the given groups in the picker (disabled boxes are masked groups)."""
        for box in self.page.locator("#picker-body input[type=checkbox]").all():
            wanted = int(box.get_attribute("value")) in group_ids
            if box.is_enabled() and box.is_checked() != wanted:
                box.set_checked(wanted)

    def analysis_aggregate(self) -> dict | None:
        """Press "Aggregate selected groups"; returns the stat tiles, warnings and badges, or None
        when the page shows an error instead (read it with `flash()`).

        The previous result is hidden first, so what is returned answers this press.
        """
        result = self.page.locator("#aggregate-result")
        result.evaluate("e => { e.hidden = true; }")
        self.page.locator("#aggregate-legend").evaluate("e => e.replaceChildren()")
        self.page.locator("#aggregate-button").click()
        flash = self.page.locator("#flash:not([hidden])")
        result.or_(flash).first.wait_for()
        if not result.is_visible():
            return None
        tiles = {}
        for tile in self.page.locator("#aggregate-stats .stat").all():
            tiles[tile.locator(".label").inner_text().strip().lower()] = (
                tile.locator(".value").inner_text().strip(),
                tile.locator(".sub").inner_text().strip() if tile.locator(".sub").count() else "",
            )
        return {
            "tiles": tiles,
            "warnings": self.page.locator("#aggregate-warnings .notice").all_inner_texts(),
            "badges": self.page.locator("#aggregate-badges .badge").all_inner_texts(),
            "plot_shots": self.page.locator("#aggregate-plot circle.shot").count(),
        }

    def analysis_save_combination(self, name: str) -> None:
        """Name and save the current aggregate; waits for it to appear in the saved list."""
        self.page.locator("#aggregate-name").fill(name)
        self.page.locator('[data-form="save-aggregate"] button[type=submit]').click()
        self.page.locator("#saved-list li", has_text=name).first.wait_for()

    def analysis_saved(self) -> list[str]:
        """Text of each saved combination."""
        return self.page.locator("#saved-list li").all_inner_texts()

    def analysis_delete_saved(self, name: str) -> None:
        """Delete the saved combination called `name`."""
        item = self.page.locator("#saved-list li", has_text=name).first
        item.get_by_role("button").click()
        item.wait_for(state="detached")

    def analysis_search(self, query: str) -> list[str]:
        """Search notes; returns the result entries (empty list when none match)."""
        self.page.locator("#search-q").fill(query)
        self.page.locator("#search-results").evaluate("e => e.replaceChildren()")
        self.page.locator("#search-empty").evaluate("e => { e.hidden = true; }")
        self.page.locator('[data-form="search"] button[type=submit]').click()
        results = self.page.locator("#search-results li")
        empty = self.page.locator("#search-empty:not([hidden])")
        results.first.or_(empty).wait_for()
        return results.all_inner_texts()

    # -------------------------------------------------------------------------- compare, dope

    def compare_open(self, rifle_id: int) -> None:
        """Open the load comparison page of a rifle."""
        self.goto(f"/compare?rifle_id={rifle_id}")

    def compare_run(
        self,
        load_ids: Sequence[int] | None = None,
        position: str | None = None,
        suppressor: str | None = None,
    ) -> str:
        """Choose loads and filters, press Compare, and return the new results text.

        The previous results are detached first, so the text returned is the response to this
        press. A refused comparison leaves the results empty and shows the page's `#flash`; read
        it with `flash()`.
        """
        if load_ids is not None:
            for box in self.page.locator("#load-choices input[type=checkbox]").all():
                box.set_checked(int(box.get_attribute("data-load-id")) in load_ids)
        if position is not None:
            self.page.locator("#filter-position").select_option(position)
        if suppressor is not None:
            self.page.locator("#filter-suppressor").select_option(suppressor)
        results = self.page.locator("#comparison-results")
        results.evaluate("e => e.replaceChildren()")
        self.page.locator("#run-compare").click()
        results.locator("*").first.or_(self.page.locator("#flash:not([hidden])")).first.wait_for()
        return results.inner_text()

    def dope_open(self, load_id: int | None = None) -> None:
        """Open the dope page, for a load or (with no id) the load picker."""
        self.goto("/dope" if load_id is None else f"/dope?load_id={load_id}")

    def dope_rows(self) -> list[list[str]]:
        """Cell texts of each row of the dope table (empty when the page shows its empty note)."""
        self.page.locator("#dope-table *").first.wait_for()
        return [
            [c.strip() for c in row.locator("td").all_inner_texts()]
            for row in self.page.locator("#dope-table tbody tr").all()
        ]
