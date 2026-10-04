# Test Plan: Rifle Group Analyzer

> Slug: `rifle-group-analyzer` · Maps `rules/testing-standards.md` onto a **local** app. Evidence goes in `test-evidence.md` (command, exit code, key assertion lines).

## 1. Tier mapping for a local app

| Tier | Meaning here | Runs against | Mocks allowed | Command |
|---|---|---|---|---|
| **T1 Unit** | Pure functions and single modules: units, rounding, stats, zeroing rules, comparison, calibers, transforms, ragged placement on synthetic masks, image-io edge cases, config validation, template rendering | In-process | None needed (pure code). No network ever (`pytest-socket`) | `.venv/bin/pytest tests/unit -q` |
| **T2 Integration & contract** | Real components together: synthetic photo → reference → working image → detection → shortfall → stats vs ground truth; migrated SQLite vs every repo query; FastAPI app via `httpx` `TestClient` against a temp SQLite file; contracts listed in §3 | Real OpenCV, real SQLite file in `tmp_path`, real ASGI app in-process | Nothing. All dependencies are local and real | `.venv/bin/pytest tests/integration -q` |
| **T3 Local deployed-process verification** | The **running server process** started by `scripts/run.sh` with its real env config, real data dir, and real loopback socket. "Deploy" = start, "teardown" = stop + verify port freed and temp data dir removed | `uvicorn` subprocess on `127.0.0.1:<free port>` | **None** | `./scripts/smoke.sh` |
| **T4 End-to-end journeys** | One Playwright journey per acceptance criterion through the real browser UI, plus failure journeys | Live server (`tests/support/live_server.py`) + Chromium | None | `.venv/bin/pytest tests/e2e -q` |
| Accuracy (T2 sub-suite) | Detection metrics vs P-11 thresholds | Synthetic catalog | None | `.venv/bin/python tools/eval_detection.py --catalog tests/synth/catalog.json` and `.venv/bin/pytest tests/integration/test_detection_accuracy.py -q` |

**Declared not-applicable** (from the AWS-oriented rules; no silent skips):
- Cloud T3 mechanics: stack outputs, Lambda `FunctionError`, `iam simulate-principal-policy`, dev-account deploy/teardown, cloud resource leftovers query.
- IaC output contracts.
- All of `AWS-security-guidelines.md`: KMS, S3 TLS/BPA, logging, tags, DynamoDB/RDS/EBS/API GW/DSQL/Amplify, and `kms-key-usage.md`.

Their **local analogues are mandatory**: config-surface contract, start → smoke → stop with verified teardown, negative-path denial (non-loopback bind, foreign Host, cross-origin write, bad upload), and "no outbound network" checks.

**Real-photo gate (outstanding until the user supplies 10–20 photos):** `tools/eval_real_photos.py data/real_photos/` against the P-11 real-photo column. Until then, acceptance of Assumption 1 (CV reliability on real targets) is **synthetic-validated only**, and the lead must report it as outstanding.

## 2. Component coverage map

| Component | T1 | T2 | T3 | T4 |
|---|---|---|---|---|
| `domain/units` | conversions at 100/300/600 yd; `round_tenth` half-away (0.05→0.1, −0.05→−0.1, 0.149→0.1, 0.15→0.2); `offset_from_aim` y-flip | `high_right_group` fixture → correction text "DOWN … LEFT" end to end | — | AC-5 |
| `domain/calibers` | table lookups, unknown → None | rifle create pre-fills diameter via API | — | AC-1 setup |
| `domain/stats` | ES known sets (2 pts, collinear, square, 30 random vs brute force); mean POI; MOA/mil; `includes_estimated`; aggregate alignment on own aim; mixed-distance ValueError; **grep test: no `mean_radius`/`best_of` symbol anywhere in `src/`** | stats from synthetic photo vs truth (≤0.05 in ES) | smoke finalize → stats | AC-1, AC-4 |
| `domain/zeroing` | step1 1–3 → correction, ≥4 → SHOOT_MORE; step2 2–9 → SHOOT_MORE **for every n in 2..9 across 200 random groups (must-never)**; ≥10 → correction; P-02 center-on-center overlay (revised B-04: no compensation; zero judged on confirm-group centroid); touch-up vs confirm-again thresholds; 30-shot path; `suggest_dot` (1.2 in/.224 → 1.0; table of cases); `zero_check` inside/touching/outside boundary (dist == r_dot + r_bullet passes; +0.001 fails); 0 or 3 shots → ValueError | zeroing service state transitions on a real DB; supersede previous zero | — | AC-5, AC-6 |
| `domain/comparison` | like-with-like tiers (P-13 revised B-04): tier10 = 10–19-shot groups, tier30 = ≥30; 20–29 in no tier; missing tier → insufficient; ≥20 tier10 rounds → high confidence; 30-shot group never penalizes the verdict; 29 % diff → no real difference, 31 % → ranked; verdict bands 1.5/2.0; poi_shift None without shared sessions; conditions warning | comparison service over seeded DB: only same-session pairs used; only ≥10-shot groups eligible | — | AC-2, AC-3 |
| `domain/summaries` | deterministic text; contains ES in/MOA, mean POI mil, notes; no mean radius | — | — | — |
| `vision/transform` | similarity & homography round trips (≤1e-6); y-down conventions; `build_working` ppi cap/max side | synthetic skew → rectified truth error | — | — |
| `vision/io` | JPEG/PNG/HEIC load; EXIF orientation 6/8 rotate correctly; >40 MB → reject; >60 MP → reject; non-image bytes → reject; truncated JPEG → reject | upload route stores sha256 name, dedupes | oversize → 413, text file → 415 against live server | AC-10 failure journey |
| `vision/reference_circle` | edge-point fits (3 pts circle, ≥5 ellipse); axis-ratio warning at <0.98 | `clean_circle1/3`: scale err ≤0.3 %; `circle_tilt_warn` → warning; `no_reference` → zero candidates | smoke uses auto-proposed circle then confirm | AC-1, AC-10 (manual mark) |
| `vision/reference_grid` | manual 4-corner homography exact | `clean_grid`, `skew10_grid`, `skew15_grid`: rectified center err p95 ≤0.03 in | — | AC-1 variant (grid) |
| `vision/detect` | param validation; empty ROI → [] | accuracy suite vs P-11 on all catalog scenarios; runtime ≤3 s on 12 MP | smoke detect on fixture | AC-1 |
| `vision/ragged` | AC-9 mask: 7 single + 1 ragged(3) + expected 10 → 3 estimated, all `pointPolygonTest ≥ 0`, spacing ≥ 2r where the outline allows; no ragged + shortfall → stacked in largest hole; too_many; unassigned_ragged; determinism (two runs equal) | `ragged_10x7` rendered photo through the full pipeline | — | AC-9 |
| `vision/pipeline` | — | catalog round trip; propose never returns a confirmed reference | — | AC-1 |
| `db/connection` + migration | — | tables/columns/CHECKs exist; `foreign_keys=1`; `user_version=1`; idempotent migrate; CHECK rejects `reason='flier'`; final group without aim rejected | `PRAGMA integrity_check` on the live data dir | — |
| `db/repo_*` | — | every repo function round trip on a migrated temp DB; **data-schema contract**: each repo SQL statement runs `EXPLAIN` without error against the migrated schema, and access-pattern queries use their index per `EXPLAIN QUERY PLAN` (`ix_groups_load_status`, `ix_groups_lot`, `ix_sessions_rifle_date`, `ix_zeros_rifle_load`); `search_notes` escaping | — | AC-8 |
| `services/groups` | — | `recompute_offsets` invariant (stored dx/dy/mil equal fresh computation after aim move, distance change, shot drag); finalize guards (count ≠ expected, no aim, unconfirmed reference → error); new-lot flag | smoke finalize | AC-1, AC-10, AC-11 |
| `config.py` | loopback validation (accept 127.0.0.1/::1/localhost; reject 0.0.0.0, LAN IP, hostname); defaults | **config-surface contract**: env vars read by `config.py` == vars set by `scripts/run.sh` | `RGA_HOST=0.0.0.0 ./scripts/run.sh` exits non-zero | — |
| `web/security` | — | TestClient: bad Host → 400; cross-origin POST → 403; same-origin POST → passes; `text/plain` POST → 415; `return_to=//evil` rejected | live: same three denials over a real socket | AC-10 |
| `web/app` + routes | — | per-slice API tests (status codes, schemas, error codes); **API contract**: every `/api/...` path in `static/js/*.js` matches a registered route + method (task 4.11); **no-CDN**: no `http(s)://` in templates/static; XSS: `<script>` in notes rendered escaped | `/healthz` 200 with `schema_version` | all |
| Templates / JS | `base.html` renders nav, no external URLs | — | — | all journeys |
| Scripts | `bash -n` + `shellcheck` (if installed; declared if not) | — | `smoke.sh` itself is the T3 gate | — |

## 3. Contract tests (each reads both sides)

| Contract | Side A | Side B | Test (task) |
|---|---|---|---|
| Coordinate convention | `offset_from_aim` y-flip | synthetic `high_right_group` truth (high-right) | `tests/integration/test_coordinate_contract.py` (2.6) |
| Data schema vs access patterns | `001_initial.sql` | every SQL string in `repo_*.py` | `tests/integration/test_repo_*` (2.10–2.13) |
| API schema vs client | FastAPI routes + `schemas.py` | `static/js/*.js` calls through `api.js` | `tests/integration/test_api_client_contract.py` (4.11) |
| Config surface | `config.py` reads | `scripts/run.sh`, `live_server.py`, `smoke.sh` set | `tests/integration/test_config_surface.py` (1.8) |
| Detection output vs ragged input | `detect_holes -> list[Blob]` | `resolve_shots(blobs…)` | `tests/integration/test_pipeline.py` (2.16) |
| Rejection reason | `RejectionReason` enum (1 member) | DB CHECK + review API validator | `tests/integration/test_api_review.py` (3.4) |

## 4. T3 local smoke (`scripts/smoke.sh`, task 3.9): every check must assert, exit non-zero on failure, and record its result
1. Pick a free port. `RGA_DATA_DIR=$(mktemp -d)`. `trap` teardown on EXIT.
2. **Negative start:** `RGA_HOST=0.0.0.0 ./scripts/run.sh` exits non-zero within 5 s and never listens.
3. Start the real server via `./scripts/run.sh` and poll `/healthz` (≤15 s). Assert `schema_version == 1`.
4. **Bind check:** `lsof -nP -iTCP:$PORT -sTCP:LISTEN` lists only `127.0.0.1`. If a LAN IP exists (`ipconfig getifaddr en0`), `curl` to it must fail to connect. If none exists, print `LAN-CHECK: SKIPPED (no LAN IP)`. That is declared, not passed.
5. **Denials:** `Host: evil.example` → 400. `Origin: http://evil.example` POST → 403. `text/plain` POST → 415. Upload of 41 MB → 413. Upload of a text file → 415.
6. **Round trip:**
   1. Render the `clean_circle1` fixture to a temp file (`tests/synth/generate.py --scenario clean_circle1 --out`).
   2. Create rifle → load → session (100 yd) → group (expected = truth count).
   3. Upload, propose reference, confirm candidate 0, detect, fill shortfall, set aim (truth aim), `PUT review`, finalize.
   4. `GET stats`: assert ES within 0.05 in of truth and `n` == expected.
7. **On-disk:** the DB file exists under `RGA_DATA_DIR`. `sqlite3 … 'PRAGMA integrity_check'` = `ok`. The photo exists as `photos/<sha256>.*`.
8. **No egress:** `lsof -nP -a -p $PID -i` shows no ESTABLISHED connection to a non-loopback address.
9. **Teardown:** stop the server. Assert the port is free and the temp dir is removed. Exit with the aggregate status (`${PIPESTATUS[0]}`-safe, no swallowed errors).

## 5. T4 journeys (Group 4; one file per journey)

| File | AC | Journey (real UI, synthetic photos uploaded through the file input) |
|---|---|---|
| `test_ac1_photo_to_stats.py` | AC-1, AC-12 | Create rifle (caliber pre-fills diameter) → load → 300 yd session (weather prompt shown; enter temp) and a 100 yd session (no prompt) → add group → upload `clean_circle1` → choose "1\" circle", confirm → review shows N holes → mark aim → finalize → ES in/MOA + mean POI mil displayed within tolerance of truth |
| `test_ac1_grid_skew.py` | AC-1 | Same with `skew10_grid` and "1\" grid" reference |
| `test_ac2_ac3_compare.py` | AC-2, AC-3, AC-11 | Seed via UI/API: zero load + load B, same sessions, 2×10 each → comparison page shows side by side, verdict, POI shift in mil, "zero to your hunting load". Load C with 5 rounds shows "insufficient data". Adding a group with a new lot code shows the new-lot flag |
| `test_ac4_aggregate.py` | AC-4 | Select 3 ten-shot groups → aggregate 30 shots, combined ES + center. Selecting a group at another distance → error message |
| `test_ac5_zeroing.py` | AC-5 | New-setup zero: step 1 with 3 shots → correction text. Step 2 with a 5-shot group → "shoot more" and **no number on the page**. A 10-shot group → correction; confirm dialed; 10 confirm → 20-shot overlay → touch-up/ready → dot suggested (P-01) → save dot |
| `test_ac6_zero_check.py` | AC-6 | Zero check with 2 shots inside → PASS. With 1 shot outside → "possible lost zero", link to Step 2, mount-then-scope message. 3 shots → rejected |
| `test_ac7_dope.py` | AC-7 | Groups at 100/300/500 with elevation dialed → dope table rows with centered elevation |
| `test_ac8_notes.py` | AC-8 | Session + group notes shown with the group. Search finds them. `<script>` in notes renders inert |
| `test_ac9_ragged.py` | AC-9 | Upload `ragged_10x7`, expected 10 → 10 shots, 3 dashed "est" markers inside the ragged outline, stats badge "includes estimated shots". Drag one estimated shot → stats update |
| `test_ac10_guards.py` | AC-10 | `no_reference` photo → no candidates → manual 3-click reference required before detect. Delete offers only "Not a bullet hole". Finalize disabled at 9 of 10. Multi-dot target: two groups on one photo via ROI (P-08) |

## 6. Gates by group

| Group | Gate before review | Tiers |
|---|---|---|
| 1 Foundations | each task's `Run:` green; env import test; schema test | T1, T2 |
| 2 Core libs + spike | T1/T2 suites green; **accuracy suite meets P-11 synthetic thresholds** (spike gate, `spike-report.md`) | T1, T2, accuracy |
| 3 Vertical slices | per-slice API T2 green; **T3 `scripts/smoke.sh` green** (this group "deploys" the running server) | T2, T3 |
| 4 Journeys + contracts + scans | all T4 journeys green; API↔JS contract green; `scripts/scan.sh` zero High/Critical (or logged exceptions); T3 re-run on the integrated tree | T2, T3, T4 |
| End-to-end scope | T1–T4 all green on the integrated tree, evidence in `test-evidence.md`; real-photo gate declared outstanding unless photos are supplied | all |

## 7. Environment feasibility (stated up front)
- T1/T2/accuracy: runnable here (Python 3.14 + wheels verified available).
- T3: runnable here (local process, `lsof`, `curl`, `sqlite3` present). The LAN-IP negative check has no en0 address in the planning environment, so it will print SKIPPED (declared).
- T4: runnable after `playwright install chromium` (one-time network download in `scripts/bootstrap.sh`). If the download is blocked, T4 is outstanding and must be escalated, not passed.
- Real-photo gate: **not runnable** until the user supplies 10–20 annotated photos.
