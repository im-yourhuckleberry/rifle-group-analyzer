# Rifle Group Analyzer

A local web app for measuring rifle groups from target photos. It finds the bullet holes, works out
group size and point of impact, and follows Formidilosus's zeroing doctrine from Rokslide: a 100-yard
zero confirmed with groups of enough shots, then zero checks and load comparisons built on that zero.

Everything runs on your Mac. The server listens on loopback only (127.0.0.1), makes no network calls,
and keeps your photos and records in `data/`.

## Prerequisites

- macOS on Apple silicon.
- Python 3.14 from Homebrew (`brew install python@3.14`). Bootstrap uses `/opt/homebrew/bin/python3.14`;
  to use a different interpreter, set `BOOTSTRAP_PYTHON`.
- FileVault turned on is recommended, since `data/` holds your target photos.

## Setup and run

```bash
make bootstrap
```

This creates `.venv`, installs the hash-locked dependencies from `requirements-dev.lock`, and installs
the Chromium build the browser tests use, into `.playwright-browsers/`.

```bash
make run
```

Then open <http://127.0.0.1:8765>. Stop it with Ctrl-C.

Optional environment variables for `make run` / `scripts/run.sh`:

| Variable | Default | Meaning |
|---|---|---|
| `RGA_DATA_DIR` | `<repo>/data` | Where the database and photos live |
| `RGA_HOST` | `127.0.0.1` | Bind address. Must be `127.0.0.1`, `::1` or `localhost` |
| `RGA_PORT` | `8765` | Port |
| `RGA_LOG_LEVEL` | `info` | Uvicorn log level |

The server refuses to start, exiting with code 2, if the host isn't a loopback address.

## Using it

The tabs across the top are Groups (where the app opens), Rifles, Analysis, Compare loads, Dope and
Settings.

1. **Add a rifle** (Rifles). Give it a name and chambering, for example "T3x" and ".223". Picking a
   known cartridge fills in the bullet diameter. The diameter is a dropdown: 0.224, 0.243, 0.257,
   0.264, 0.277, 0.284, 0.308 or 0.338 in.
2. **Add a load.** Factory or handload, with a lot code.
3. **Add a new group.** Press **Add new group** at the top right of the Groups tab, pick the rifle
   and the target photo, and continue to the New group page: set the date and time (read from the
   photo when it has them), distance, position, suppressor and weather. The form pre-fills from your
   last group. You can also record a group straight from the rifle's page.
4. **Set the scale reference**: the target grid, or a printed 1, 1.5, 2, 2.5 or 3 inch circle. The
   outlines found on the photo are numbered, and the biggest circle starts selected; pick the one on
   the printed edge, then **Next**.
5. **Set the aim point.** Only the aim is shown at this step. With a printed circle it starts on the
   circle's exact center (**Use the circle's center**). Switch to **Mark it by hand**, or just click
   the photo, to put it exactly where you aimed (it never snaps to the printed mark, which holes
   often cover); drag it to fine-tune. Then **Confirm aim point**. **Go back** returns to the scale
   reference.
6. **Review the shots.** Detected holes are proposed only now. Add, drag or remove them; a shot is
   never added past the number fired. Overlapping holes in a ragged group get estimated ("est")
   shots, which you can drag into place. **Detect holes** looks at the whole photo (or the group's
   region, if it has one); when other groups share the photo, remove their holes with **Not a bullet
   hole**. **Done** saves; when the group is complete it offers **Lock and finish**, which
   opens the group's share card (a guided zeroing step returns to the zeroing flow instead).
   Leaving a group before it is finished (Exit, Cancel or the top bar) asks first; unsaved work is
   lost.
7. **Read the results.** The group page shows the shot count, extreme spread (in, MOA, mil), the cone
   of fire, the "Thing-hitter" circle, the mean point of impact and the correction. Zero-check and
   zeroing groups show size only, so the point of impact can't bias the next group.
8. **Share.** Make a PNG card of the group and pick which overlays to include. The default checkboxes
   are set in Settings → Share.

Other flows:

- **Zeroing** (from a rifle's page): a guided, step-by-step 100-yard zero.
- **Zero check**: confirm an existing zero.
- **Groups**: every group you've measured, newest first. Hover a row to see its photo. Collapsible
  filters cover rifle, load, distance, date range, position, purpose, status and suppressor.
  **Edit** shows a checkbox on each row; **Delete** removes the chosen groups for good (after a
  confirmation where Cancel is the default), along with any photo no other group uses.
- **Analysis**: combined groups across days. Only combine groups shot on the same zero; otherwise
  the combined size and point of impact are not accurate.
- **Compare loads**: loads side by side.
- **Dope**: per load, a table of finalized groups that have an elevation recorded, by distance.

## Backup

Stop the server, then copy the whole `data/` folder somewhere safe. To restore, copy it back while the
server is stopped. Before each schema upgrade the app writes its own `rga.sqlite3.pre-NNN` copy, but
that copy is not a substitute for a real backup.

## Tests

| Tier | What it covers | Command |
|---|---|---|
| Unit and integration | Domain math, detection, database, every route and page | `make test` (or `make test-fast` to skip the accuracy runs) |
| Detection accuracy | Synthetic target catalog scored against ground truth | `make accuracy` |
| End to end | Chromium journeys through the real server | `make e2e` |
| Smoke | Starts the real server and checks it over HTTP, including refused cross-origin writes | `make smoke` |
| Security scan | Bandit over `src/`, and pip-audit over `requirements.lock` (pip-audit needs a network connection) | `make scan` |
| Lint | Ruff and shell syntax | `make lint` |

To run everything at once: `.venv/bin/pytest -q`.

**Real-photo gate.** Your own target photos and their truth files live in `data/real_photos/`, which
is gitignored and stays on this Mac. When those photos are missing, the real-photo tests skip with
`real-photo gate outstanding`. See [tests/real/README.md](tests/real/README.md) for the annotation format
and the scorer:

```bash
.venv/bin/python -m tools.eval_real_photos
```
