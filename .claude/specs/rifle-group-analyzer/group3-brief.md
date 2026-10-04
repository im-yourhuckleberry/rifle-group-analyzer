# Group 3 Brief: binding rules for every route and page slice

Read this together with your task in `tasks.md`, the "Build-time interface notes" at the end of `design.md`, and `decisions.md` B-01..B-31.

## Routes
- **Module shape:** each module in `src/rga/web/routes/<name>.py` exports `router = APIRouter()` with full `/api/...` paths and pages, and gets no prefix. Discovery is automatic.
- **Schemas:** use `rga.web.schemas.ROUTE_SCHEMAS[(METHOD, path)]` for request, query and response models. If a route or model you need is missing or wrong, message the lead; don't edit `schemas.py`.
- **Database access:** handlers take `conn: Annotated[sqlite3.Connection, Depends(get_db)]`. Services commit, and writes go through the services' `_write_transaction` pattern (see `services/groups.py`).
- **Errors:** raise `rga.web.app.ApiError(status, code, detail)`. The body is always `{detail, code}`. Map these:
  - `GroupError` codes: `group_not_found` → 404, `invalid_*` → 422, others → 409
  - `GroupLinkMismatch`, `GroupNotLinkable`, `ZeroTargetMismatch` and `LoadNotInRifle` → 409
  - `sqlite3.OperationalError` "database is locked" → 503 `busy` (B-24)
  - `ImageRejected` → 415/413
  - detector `ValueError` on tiny images → 422 `image_too_small`, after the `ImageRejected` check (B-27)
  - never a 500
- **Short-group masking (B-09/B-14/B-18):** for `services.groups.masks_poi(purpose, n)` groups (all `zero_check`, plus `zero_step2`/`zero_confirm` under 10 shots), responses carry `SizeOnlyStatsOut` and null per-shot offsets, with no mil, POI or direction text anywhere: JSON, HTML or JS. Aggregates and comparisons exclude masked groups (B-20).
- **User notes are shown verbatim (B-16).** There's no mean radius or best-N anywhere; the D-14 scan covers templates and JS.

## Pages
- **Layout:** Jinja2 templates extend `base.html` (blocks `title`, `head`, `content`, `scripts`; `active` is one of `home|rifles|analysis|compare|dope`). Autoescape is on.
- **CSP (enforced by a test):** no inline `<script>`/`<style>`, no `style=`, no `on*=`, no `javascript:`, no `setAttribute('style')`, no eval. Use external ES modules under `/static/js/` and classes from `app.css`. Add new CSS only via your own module-scoped CSS file if your task lists one; otherwise use the existing classes.
- **No native HTML form POSTs (B-13).** Every write goes through `api.js` (`apiGet`/`apiSend`/`apiUpload`; the upload field is `file`). Show errors in `#flash` with `textContent`, never `innerHTML`, for user strings.
- **No external URLs.** `/compare` and `/dope` must work with no query string (show a picker).
- Optional: invoke the `frontend-design:frontend-design` skill for layout and visual choices. Keep it a clean, readable tool UI with no external fonts or CDNs.

## Tests and verification
- Integration tests use `TestClient(create_app(settings))` with `tests/support/seed.py` (`seed_rifle`/`seed_load`/`seed_session`/`seed_group`) and a temp data dir, and use `raise_server_exceptions=False` where you assert a 500 can't happen.
- Test every error mapping you add, the masking rule wherever stats are returned, and a denial path for each write. Do mutation self-checks on a SCRATCH copy with `PYTHONDONTWRITEBYTECODE=1`.
- Append evidence to `test-evidence.md`. Don't edit `tasks.md`. Report to `main` by SendMessage.
- Format only your own files. Leave no stray files. Check processes by PID, or with `pgrep -f -- '-m rga'`.
