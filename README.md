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

The tabs across the top are Home, Rifles, Groups, Analysis, Compare loads, Dope and Settings.

1. **Add a rifle** (Rifles). Give it a name and chambering, for example "T3x" and ".223". Picking a
   known cartridge fills in the bullet diameter. The diameter is a dropdown: 0.224, 0.243, 0.257,
   0.264, 0.277, 0.284, 0.308 or 0.338 in.
2. **Add a load.** Factory or handload, with a lot code.
3. **Record a group** from the rifle's page. Set the date and time (read from the photo when it has
   them), distance, position, suppressor and weather. The form pre-fills from your last group.
4. **Upload the photo**, then set the **scale reference**: the target grid, or a 1.5" circle. If the
   scale was wrong, use **Change scale reference** at the top of the review page to redo it.
5. **Review the shots.** Detected holes are proposed. Add, drag or remove them, and mark the aim
   point. Overlapping holes in a ragged group can be added as estimated shots.
6. **Read the results.** The group page shows the shot count, extreme spread (in, MOA, mil), the cone
   of fire, the "Thing-hitter" circle, the mean point of impact and the correction. Zero-check and
   zeroing groups show size only, so the point of impact can't bias the next group.
7. **Share.** Make a PNG card of the group and pick which overlays to include. The default checkboxes
   are set in Settings → Share.

Other flows:

- **Zeroing** (from a rifle's page): a guided, step-by-step 100-yard zero.
- **Zero check**: confirm an existing zero.
- **Groups**: every group you've measured, newest first. Collapsible filters cover rifle, load,
  distance, date range, position, purpose, status and suppressor.
- **Analysis**: combined groups across days.
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
