## Group 1 — Cycle 1

Date: 2026-09-30 · Reviewing: Group 1, Foundations and contracts (T1.1–T1.10) · Scope/cycle: Group 1, cycle 1 of 3 (decisions.md B-07)
Synthesizer: review-1 (Slice B plus whole-group cross-module consistency). Analyst: review-2 (Slice A: T1.2, T1.4, T1.5).
Coordination: degraded mode (B-01). Ground truth is disk, tasks.md, test-evidence.md and decisions.md. No sentinels and no git baseline, as expected.
Method:
- Every Slice B file read in full.
- Every Run command re-run.
- Live `scripts/run.sh` process probe.
- Mutation testing on a scratch copy of the repo. Source was never edited; each harness was validated with a positive-control mutation that failed as expected.
- Plugin passes merged: `feature-dev:code-reviewer`, `pr-review-toolkit:silent-failure-hunter`, `pr-review-toolkit:pr-test-analyzer`.

### Spec Alignment
- **Implemented as specified:**
  - DDL matches design §5, with `sandbags` removed (B-04).
  - Row dataclasses mirror the columns 1:1.
  - Config reads exactly the four `RGA_*` vars and rejects non-loopback hosts with exit 2.
  - Guards per design §11: Host allowlist, OriginGuard and the content-type rule.
  - `safe_return_to` is present.
  - The `{detail, code}` error model works and the 500 body has no leaks (B-05).
  - Schemas exist for all 40 design §4.7 routes, including B-06 `GroupListQuery` and `photo_id`.
  - B-04 tier10/tier30 compare shape is present.
  - The rejection reason is `not_a_bullet_hole` only, in the enum, the DB CHECK and the schema, and each layer is tested.
  - The UI shell has no external assets.
  - Domain and transform signatures match design §4.1–4.3 and §4.5 (review-2).
- **B-04 reflected:**
  - No `sandbags` in `Position`, the DB CHECK or the schemas, and each has a negative test.
  - The comparison response carries `tier10`/`tier30` per load and per pair, and 20–29-shot groups get `tier: null`.
  - The center-on-center overlay is Group 2. The only schema surface is `ZeroDialed`, which is stored and displayed only, plus `AdviceOut.stats`; the latter has a must-never gap, see W3.
  - design §4.4 still lists `compensate()`; see S14.
- **AC-5 must-never (no numeric correction from 2–9 shots outside Step 1):** no producer exists yet. The schema guard is incomplete; see W3.
- **AC-9:** the fixture is not reliably "ragged" under design §6's split rule. It needs a lead decision; see W10.
- **AC-10 (Group 1 pieces):**
  - Loopback bind: verified live. `lsof` shows only `127.0.0.1:<port>`, and the `::1` bind also works.
  - Rejection CHECK: verified.
  - Final-needs-aim CHECK: verified.
  - No outbound network in `src/`: verified by grep. The no-CDN verifier itself has a gap; see W4.
- **AC-12:** the session weather fields and `weather_skipped` are present and nullable, per spec §4.2.

### Critical
- None.

### Warning

**W1. Schema elements with no test that fails without them; the enum↔CHECK contract is checked from one side only.**
- Where: `src/rga/db/migrations/001_initial.sql` and `tests/integration/test_schema.py:340-400`.
- Evidence (static-verified by mutation): each of the following was deleted one at a time and the full suite stayed at 540 passed:
  - `shots_expected >= 1` (:103)
  - the `groups.purpose` IN-list (:107)
  - the `groups.status` IN-list (:109)
  - `zeros.mode` (:82) and `zeros.state` (:83)
  - `zero_checks.result` (:150) and `zero_checks.group_id UNIQUE` (:149)
  - `photos.sha256 UNIQUE` (:61, the only DB backstop for FR-3.1 dedupe)
  - `photos.reference_kind` (:65) and `reference_confirmed IN (0,1)` (:72)
  - `trued_mv_fps > 0` (:25) and `dot_size_in > 0` (:88)
  - `suppressor IN (0,1)` (:47) and `moved_by_user IN (0,1)` (:132)
  - `groups.zero_id ON DELETE SET NULL` (:108)
  - `rifles.public_id UNIQUE` (:6)
  - the `loads`/`sessions` → `rifles` cascades (:20, :43)
  - `groups.lot_id NOT NULL` (:100) [via pr-test-analyzer]
- Also, the domain enums are pinned only to hard-coded lists (`tests/unit/test_types.py`), and the DB CHECK lists only to a few hand-picked negatives. No test reads both sides. Misspelling `'estimated'` in `shots.origin` or dropping `'superseded'` from `zeros.state` stays green [via pr-test-analyzer]. Values agree today [review-2 parsed both sides].
- Why: testing-standards rule 5 (every schema element needs a test that fails without it) and the "contract asserted from only one side" FAIL condition.
- Required fix: in `test_schema.py`:
  1. Add one contract test per (table.column, enum/Literal) pair: `LoadKind`, `Position`, `ReferenceKind`, `ZeroState`, `GroupPurpose`, `GroupStatus`, `ShotOrigin`, `RejectionReason`, `schemas.ZeroMode`, `schemas.ZeroCheckOutcome`. Each test inserts every member (must succeed) and one non-member (must raise CHECK).
  2. Add parametrized negative cases for each listed CHECK/UNIQUE/NOT NULL.
  3. Add assertions for the SET NULL and cascade behavior.

**W2. The migration runner breaks its "rolled back in full" contract on error paths.**
- Where: `src/rga/db/connection.py:72-83` and test `tests/integration/test_schema.py:217-227`.
- Evidence (empirically reproduced):
  - (a) A migration that forgets `PRAGMA user_version` is committed before `MigrationError` is raised. Table `t` persists, and every later `migrate()`, including app startup, fails with `table t already exists`.
  - (b) A trailing unterminated `/*` in a migration swallows the appended `COMMIT`. `migrate()` returns success with `in_transaction=True`, `close()` silently rolls everything back, and the reopened DB has `user_version 0` and no tables.
  - The existing test asserts only that an error is raised, not that anything was rolled back.
  - Sources: own reproduction; via silent-failure-hunter (HIGH) and code-reviewer (watch item).
- Why: data-integrity contract of the schema layer (design §5 "each in a transaction"). Latent today because `001` is correct.
- Required fix:
  1. Run `BEGIN IMMEDIATE;\n{sql}` without `COMMIT`.
  2. Raise if `not conn.in_transaction`.
  3. Check `user_version` while still inside the transaction, then `conn.commit()`; on any exception, `rollback()`.
  4. Extend the test so table `t` is absent after the error, a second `migrate()` raises the same `MigrationError`, and the unterminated-comment case is rejected.

**W3. The AC-5 must-never guard in `AdviceOut` is incomplete.**
- Where: `src/rga/web/schemas.py:907-921` and test `tests/unit/test_schemas.py:614-621`.
- Problem: the validator forbids `correction` on `shoot_more`/`invalid` but accepts `stats: GroupStatsOut`, which carries `mean_dx_mil`, `mean_dy_mil` and `poi_text` ("0.3 mil high, 0.2 mil right"). That is a numeric correction in all but name.
- Design: §4.4 says SHOOT_MORE is "never a number", and §8 says no correction number is rendered for SHOOT_MORE.
- The author (coding-4) confirmed this was unintentional; the UI should show at most n and ES for these kinds. Sources: own; via pr-test-analyzer I5.
- Why: a latent path from a 2–9-shot group to a mil number in the API contract that Group 2/3 code against.
- Required fix:
  - For `shoot_more`/`invalid`, either reject `stats`, or require that it carries no mean-POI fields (for example a separate `n`/`es_in`/`es_moa` model).
  - Fix the docstring claim to match.
  - Add a test that `shoot_more` with full stats is rejected.

**W4. The no-CDN verifier passes with a CDN present.**
- Where: `tests/unit/test_templates_shell.py:93-97`.
- Evidence (static-verified by mutation): the test only matches `https?://` and `//cdn`. Both of these stayed green:
  - injecting `<script src="//unpkg.com/htmx.org">` into `base.html`
  - appending `@import url(//fonts.example.com/x.css)` to `app.css`
- It will also false-positive on the SVG namespace `http://www.w3.org/2000/svg` that `svg_editor.js` will need, which invites weakening the test.
- Sources: own; via pr-test-analyzer I1.
- Why: this is the only guard for design §11 threat (d) on the browser side, since T3 `lsof` sees only server sockets. "Any outbound network or CDN use" is a must-never, and verify-the-verifier makes a check that can't fail a defect.
- Required fix:
  - Also reject protocol-relative references (`(src|href|action)\s*=\s*["']//`, `url\(\s*["']?//`, `import … ["']//`) and `wss?://`/`ftp://`.
  - Add an exact allowlist for XML namespace URIs.
  - Optional: enforce in the browser via the CSP in W7.

**W5. The security-scan gate can report clean when it did not scan what it claims.**
- Where:
  - `scripts/scan.sh:39-41` (`note_status`)
  - `scripts/scan.sh:104-114`
  - `pyproject.toml:44-45`
  - tests `tests/unit/test_scripts_syntax.py:83-122`
- Evidence (empirically verified):
  - (a) `note_status` ignores any code other than 1 or 3. A checker killed by a signal (137), not executable (126) or exiting 2 leaves `STATUS=0`, and the script prints "exit 0 (clean)".
  - (b) A header-only `requirements.lock` gives `PIP-AUDIT: audited 0 pinned packages` and exit 0.
  - (c) bandit `exclude_dirs = ["tests", …, "data", …]` matches by substring. `src/rga/services/metadata_probe.py` containing `shell=True` was silently unscanned while an identical `probe_plain.py` was flagged.
  - (d) The `SCAN_IGNORE_VULNS` env var, the pip-audit result parsing (`vulns` key, `skip_reason`, count mismatch) and the combined two-tool status path have no automated test. test-evidence.md:146 records them as manual runs. Mutating `"vulns"` to a wrong key stays green.
  - Sources: own; via silent-failure-hunter HIGH-1/MEDIUM-4, code-reviewer watch item, pr-test-analyzer I3.
- Why: the T4.13 gate fails open, and an untested env var breaks rule 5.
- Required fix:
  - Map any rc not in {0,1,3} to 3 (NOT RUN).
  - Treat `pins == []` as NOT RUN.
  - Drop `exclude_dirs`, which is redundant with `-r src`, or anchor it to top-level paths.
  - Add tests with a stub `.venv/bin/pip-audit` replaying a recorded clean output and a recorded vulnerable output. Assert the vulns → 1 path, the skipped/short → 3 path, `--ignore-vuln` being passed and logged, and the default combined-status path.

**W6. The data-directory 0700 permission has no test and is not applied to existing directories.**
- Where: `src/rga/web/deps.py:30-31` and tests `tests/integration/test_app_security.py:63-73`.
- Evidence (static-verified by mutation): removing `mode=0o700` stays green. `mkdir(exist_ok=True)` never tightens an existing `data/`, for example one restored with `cp -R` at 0755. The T1.6 Done note still records "init_database 0700 dirs".
- Sources: own; via pr-test-analyzer I4, silent-failure-hunter #13.
- Why: rule 5; this is a new permission with no failing test.
- Required fix:
  - Assert `stat.S_IMODE(...) == 0o700` for the data, photos and working dirs after `create_app` on a fresh tmp dir.
  - Either `chmod` existing dirs to 0700 (and test it), or warn when the mode is wider (and test that).

**W7. No anti-framing headers (clickjacking) in the loopback threat model.**
- Where: `src/rga/web/security.py:19-47` and `src/rga/web/app.py:126-127`.
- Evidence: verified that no `X-Frame-Options`, `Content-Security-Policy` or `X-Content-Type-Options` header is sent.
- Attack: a hostile page in the user's browser, which is threat (b) in design §11, can frame `http://127.0.0.1:8765/...`. Host and Origin checks pass because the framed app is same-origin, so the user can be tricked into clicking state-changing controls ("Not a bullet hole", Finalize) once the Group 3 pages exist.
- Sources: via code-reviewer (watch item, verified); pr-test-analyzer I1 recommends the same CSP.
- Why: Medium security. Threat class (b) has CSRF and DNS-rebinding controls but none for UI redress.
- Required fix:
  - Add a small middleware setting `X-Frame-Options: DENY`, `Content-Security-Policy: default-src 'self'; frame-ancestors 'none'` and `X-Content-Type-Options: nosniff` on every response.
  - Add a T2 test.
  - The CSP also enforces W4 in the browser.
  - The lead adds a row to the design §11 control table.

**W8. Seventeen public domain types have no docstring.** [via review-2 W1]
- Where: `src/rga/domain/types.py:8, 23, 30, 36, 41, 48, 56, 61, 76, 103, 111, 118, 127, 135, 144, 165, 173`. That covers `ReferenceKind`, `ShotOrigin`, `BlobKind`, `LoadKind`, `Position`, `GroupPurpose`, `GroupStatus`, `ZeroState`, `AdviceKind`, `Shot`, `WorkingFrame`, `Blob`, `CircleCandidate`, `ReferenceResult`, `GroupStats`, `Advice` and `ZeroCheckResult`.
- Confirmed by my AST check, which found no other missing public docstrings in `src/`.
- Why: tasks.md in-code docs rule; it is a handoff FAIL condition.
- Required fix: add one-line docstrings. Optional, T1.1 owner: enable ruff `D1` for `src/` so this is gated.

**W9. `round_tenth` crashes on numpy scalars in the correction path.** [via review-2 W2; reproduced by review-1]
- Where: `src/rga/domain/units.py:49`.
- Evidence: `round_tenth(np.float64(0.05))` and `round_tenth(np.float32(0.05))` raise `decimal.InvalidOperation`, because `repr` gives `np.float64(0.05)`. `in_to_mil(np.float64(...))` returns an `np.float64` that would feed straight into it.
- Why: latent crash for Group 2 stats/zeroing if any reduction uses numpy.
- Required fix: use `Decimal(repr(float(abs(x))))`, and add tests with `np.float64` and `np.float32` inputs.

**W10. The AC-9 `ragged_10x7` fixture is not reliably "ragged" under design §6's own split rule.** [via review-2 W3; needs a lead decision]
- Where: `tests/synth/generate.py:288` and `tests/unit/test_synth.py:98-122`.
- Evidence (review-2 measurements, seeds 0–5):
  - Members sit at `r·U(0.5,1.0)`, so pair separations reach 0.90 d.
  - The best 3-circle union IoU is 0.907–0.929, against the 0.8 split threshold.
  - The best 2-circle IoU is 0.805 on seeds 1 and 5.
  - Seed 2 has 3 distance-transform peaks.
- So "exactly 3 estimated" depends on the detector's peak count, not on the fixture. This is in tension with P-11, which requires merged pairs at 0.6 d or more to split. `test_synth` checks seed 0 only.
- Required fix:
  - Constrain cluster members so every pair is below 0.6 d and the best k=3 IoU is below 0.8, and assert both over several seeds.
  - The lead reconciles AC-9 with P-11 before Group 2 opens.

**W11. Several truth keys are never checked against the rendered pixels.** [via review-2 W4]
- Where: `tests/synth/generate.py:593-617` and `tests/unit/test_synth.py:157-162, 191-198`.
- Evidence (review-2 mutations):
  - Garbage values in `reference.center_px`, `ellipse_center_px`, `axes_px` and `angle_deg` leave 52/52 passing. The build-time notes tell Group 2 to pick candidates by `center_px`.
  - The grid `corners_px` test is tautological: it projects the same homography that generated the value.
  - `ragged_outlines_in` can be swapped for the halo polygon with no failure.
- Why: the verifier for the fixtures that gate the Group 2 spike cannot fail.
- Required fix: compare each key against a fit on the image (rendered ellipse, detected line intersections, dark-mask IoU).

**W12. The synthetic catalog is narrower than design §7.** [via review-2 W5]
- Where: `tests/synth/catalog.json` and `tests/synth/generate.py:66, 81, 254, 411`.
- Gaps against design §7:
  - ppi spans 140–220, not 120–350, so the 12 MP phone-photo downscale to 200 ppi is never exercised.
  - `wipe_prob` is 0 everywhere, so bullet wipe is never drawn.
  - Specks are always below the 0.35·A1 area floor, so the false-positive gate is untested.
  - There are no hole-sized printed marks, no 0.4 d pair, no black aim dot, and no hole straddling the bull edge.
- Why: unjustified deviation from design §7. The catalog is the only evidence for Assumption 1 and the P-11 spike.
- Required fix: add scenarios or ranges for each item above.

**W13. The `no_reference` fixture contains a circle that a finder can propose as a reference.** [via review-2 W6; requires live validation in Group 2]
- Where: `tests/synth/catalog.json:83-89`.
- Evidence: the fixture's 1.0 in green circle aim dot scores like a reference circle under Otsu + `fitEllipse`: mean axis about 1.02 in at 180 ppi, circularity 0.82, against 0.88–0.90 for real references.
- Conflict: T2.2 expects `no_reference → []`, and T4.11 expects "no candidates".
- Required fix: switch the aim mark to the supported diamond shape, or amend the T2.2/T4.11 acceptance criteria. This is a lead decision.

### Suggestion

Slice B (review-1 plus plugin passes):
- **S1** `src/rga/web/static/js/api.js:17-21`
  - `checkPath` accepts `/\evil.example` and `/\t/evil.example`, which `fetch` resolves as protocol-relative. `safe_return_to` rejects both.
  - Align the two: reject backslash and control characters. Not exploitable today because paths come from constants.
  - Sources: own; code-reviewer; pr-test-analyzer.
- **S2** The api.js↔error-model contract is checked by substring.
  - Where: `tests/integration/test_error_model.py:178-180`, `tests/unit/test_schemas.py:100-104`, `tests/unit/test_templates_shell.py:111-121`.
  - Mutating api.js to read `body.details` stays green.
  - Use word-boundary regexes now, or run api.js against real responses in T4. [via pr-test-analyzer I2]
- **S3** `src/rga/web/deps.py:19-25`
  - `get_db` closes without checking for an open transaction, so a Group 3 route that forgets `commit()` returns 2xx and loses the write.
  - Roll back and log at teardown, and add a test fixture that fails any request leaving a transaction open. [via silent-failure-hunter #3]
- **S4** `src/rga/web/app.py:129-131`
  - `/healthz` says `ok` when `schema_version` differs from `latest_version()`; for example it returned 0 after the DB file was deleted mid-run.
  - Compare against `latest_version()` and return 503 on mismatch.
  - Consider opening per-request connections with `mode=rw` so a missing DB isn't recreated. [via silent-failure-hunter #8]
- **S5** `src/rga/db/connection.py:31`
  - The result of `PRAGMA journal_mode = WAL` isn't checked; on unsupported filesystems the mode silently stays `delete`. Assert the returned mode. [via silent-failure-hunter #6]
  - The default data dir is under `~/Documents`, which may be iCloud-synced. T5.1's README should warn that a live SQLite/WAL file must not sit in a synced folder.
- **S6** Startup failure handling.
  - `src/rga/db/connection.py:43-46`: misnamed migration files (`002-x.sql`, `002_x.SQL`) are silently skipped, and numbering gaps aren't detected. [via silent-failure-hunter #7]
  - `src/rga/__main__.py:18-27`: `OSError`/`MigrationError`/`sqlite3.Error` at startup print a raw traceback. Exit 1 is still loud, but the output is unfriendly. [via silent-failure-hunter #9]
- **S7** Silent route and static failures.
  - `src/rga/web/app.py:103-106`: a public route module whose `router` isn't an `APIRouter` is silently skipped. Raise `TypeError` instead. [via silent-failure-hunter #11]
  - `src/rga/web/app.py:135`: `check_dir=False` hides a missing static directory. [via silent-failure-hunter #12]
- **S8** `scripts/scan.sh:30-35`: a missing tool exits 1 ("findings") rather than 3 ("NOT RUN"), and pip-audit is required even with `--only bandit`. This fails closed, so it is cosmetic. [via silent-failure-hunter #5]
- **S9** `tests/integration/test_app_security.py:210-219`
  - The "connection is closed" assertion passes for the wrong reason: `ProgrammingError` is raised by the thread check. Use `match="closed"`. [via pr-test-analyzer I6]
  - Minor in the same file: `:76-78` has no assertion. In `tests/integration/test_config_surface.py:208-211`, the port probe after process exit proves nothing; the rc is the real guard. [via pr-test-analyzer]
- **S10** `tests/unit/test_templates_shell.py:100-102`
  - The `innerHTML` scan covers only `api.js`. Loop over `static/js/*.js` so page scripts are covered as they land.
  - The T4.9 XSS payload should be `<img src=x onerror=…>`, because `<script>` stays inert even through `innerHTML`. [via pr-test-analyzer I9]
- **S11** `src/rga/web/schemas.py:1009`: `ROUTE_SCHEMAS` is not tied to the registered routes. When routes land (Group 3/4), add a test that each route's request/response models equal the registry entry, including `/healthz` ↔ `HealthOut`. [via pr-test-analyzer I10]
- **S12** Boundary tests and a missing CHECK.
  - The schema size limits are untested at their boundaries (`ReviewRequest.shots` 200, `rejections` 500, `Notes` 10 000, `Name` 200, aggregate 100, comparison 20).
  - `sessions.weather_skipped` (`001_initial.sql:52`) has no `IN (0,1)` CHECK, unlike `suppressor`. The design omits it too. [via pr-test-analyzer]
- **S13** `src/rga/web/schemas.py:685-691` (`ReviewRequest`, design §4.7)
  - The full-state PUT makes it possible to remove a shot without any recorded reason, by simply omitting it.
  - Carry this into T3.4's acceptance criteria: the review service must reject a PUT that drops a previously stored shot unless a matching `not_a_bullet_hole` rejection accompanies it, with a T2 test (AC-10, D-14).
- **S14** `.claude/specs/rifle-group-analyzer/design.md:141` still lists `compensate(offsets, dialed, …)`, which contradicts B-04 center-on-center and T2.7. The lead should remove it or mark it "not used by the overlay" so Group 2 doesn't implement compensation.
- **S15** Formatting is not gated. `ruff format --check` flags 4 files: `tests/integration/test_config_surface.py`, `tests/unit/test_scripts_syntax.py`, `tests/synth/generate.py` and `tests/unit/test_synth.py`. `make lint` runs only `ruff check`. Add `ruff format --check .` to `make lint` (T1.1 owner).
- **S16** `scripts/lock.sh:42-44` installs the pinned lock tooling without `--require-hashes`. This is a supply-chain gap in dev tooling only.
- **S17** Upgrade path for the TestClient warning. Every TestClient run emits Starlette's `httpx` deprecation warning (lead-accepted). Plan the `httpx2` move in a later lock update, before it becomes an error.
- **S18** Config-surface setters. When T2.17 (`live_server.py`) and T3.9 (`smoke.sh`) land, extend `test_config_surface.py` to assert set-equality with the config reads for those setters. A subset check isn't enough, because a forgotten `RGA_PORT` would fall back to 8765. [via pr-test-analyzer I8]
- **S19** tasks.md bookkeeping (lead):
  - T1.2 has no `> Done.` note. [own + review-2 S9]
  - T1.9's note sits after the `---` separator.
  - T1.4's note claims the DB sandbags follow-up, which test-evidence attributes to coding-2.

Slice A [via review-2]:
- **S20** `tests/unit/test_layering.py:10-18, 66-68`:
  - Modules outside the five layers (`config`, `__main__`, new top-level modules) aren't checked.
  - `ALLOWED["services"]` includes `rga.config`, which design §1 doesn't list.
  - Dynamic imports pass.
  - The checker's own allow-list isn't pinned.
- **S21** `tests/unit/test_transform.py:262`:
  - The 0.75 px tolerance lets a 0.5 px warp/h mismatch pass; the measured error is ≤ 0.01 px. Tighten to about 0.05 px.
  - Untested: the `_native_ppi` perspective term, the corner-sign infinity check, the RANSAC threshold value, and `BORDER_REPLICATE` (indistinguishable on a black test image).
- **S22** Circle-similarity truth frame. The similarity keeps the image rotation, while truth offsets are in the paper frame. That gives a 0.005–0.012 mil mean-POI bias against AC-1's 0.02 mil tolerance. Compare offsets in the image frame, or expose `rotation_deg` in the truth.
- **S23** `tests/synth/generate.py:551-567`: `groups[].roi_px` is in original-image px, while `RoiPx`/`detect_holes` use working px. At 200 ppi the two nearly coincide, which hides a mix-up. Rename it, document it, or add `roi_in`. (Cross-slice with T1.7 `RoiPx`.)
- **S24** Assertions that can miss regressions:
  - `tests/unit/test_calibers.py:16-30` asserts only 8 of the 17 diameters.
  - `tests/unit/test_units.py:98-115` lacks the low-right and high-left quadrants for `offset_from_aim`.
  - `tests/unit/test_types.py:37-40` should assert `str(m) == m.value`, so a `(str, Enum)` regression would fail.
- **S25** `tests/synth/generate.py:296, 303` draws light cores only inside the bull, so a hole on a black aim dot would render dark on black.
- **S26** Test-module hygiene:
  - `tests/unit/test_transform.py:313-314` has a dead assignment with a contradictory comment.
  - `test_types.py`, `test_units.py`, `test_calibers.py` and `test_transform.py` have no module docstrings; confirmed by review-1's AST check.
  - `test_layering.find_violations` has no docstring.
- **S27** `src/rga/vision/transform.py:128`: the bbox corners use (0,0)…(W,H) under the pixel-center convention, giving a ½ px canvas shift. The mapping stays consistent and accuracy isn't affected.

### Cross-Task Consistency
- **Enums ↔ DDL ↔ schemas:**
  - Values agree today. review-2 parsed every DDL IN-list against the enums, and review-1 read the schemas, which reuse the domain enums by identity (tested).
  - The DDL side isn't tied to the enums by any test; see W1.
  - `ZeroMode`/`ZeroCheckOutcome` Literals equal the `zeros.mode`/`zero_checks.result` CHECKs.
  - The numeric bounds agree or the pydantic side is stricter: diameter (0.1, 0.6), `shots_expected` ≥ 1, distance > 0, `wind_clock` 1–12, trued MV > 0, dot > 0. A bad value therefore gets a 422, never an `IntegrityError` 500.
- **Rows ↔ DDL:** each `*Row` field list equals `PRAGMA table_info`, checked by a two-sided test. Rows → response models are tested via `from_attributes` for rifle, load, session, shot and rejection. The `DopeRow` → `DopeRowOut` mapping (`session_date`→`date`, `shot_count`→`shots`) is a service concern, documented in the build-time notes.
- **Domain dataclasses ↔ response models:**
  - `GroupStats`, `Advice` and `Correction` validate via `from_attributes`.
  - `TierSummary` = `TierSummaryOut`.
  - `compare_pair` Literal = `PairVerdict`.
- **Config surface:** the vars `config.py` reads equal the vars `run.sh` sets, in both directions, measured at runtime, by AST and by stub run. `live_server.py` and `smoke.sh` are pending; see S18.
- **api.js ↔ error handlers:**
  - Both use the shape `{detail, code}`. The middleware refusals (403 `bad_origin`, 415 `unsupported_media_type`) match. The Host-guard 400 is plain text and api.js falls back to `http_400`.
  - The test is weak; see S2.
  - The upload field `file` is recorded for T3.3.
- **Layering:** the test passes on the current tree. `web → db` happens only via `web/deps.py → db.connection`. Checker gaps: S20.
- **Must-never sweep across the group:**
  - The rejection reason is single-valued in the enum, the DB and the schema, and each is tested.
  - No `mean_radius`/best-N symbols: grep finds only negative tests.
  - No HTTP client or URL in `src/`, and `/docs`/`/redoc` are disabled. The no-CDN verifier gap is W4.
  - The numeric-correction leak path is W3.
  - Reasonless shot removal through the full-state PUT is S13, for T3.4.
- **Open spec question for the lead (not counted):** `GET /api/groups/{id}/stats` will show mean POI in mil, with direction words, for any group, including a 5-shot `zero_step2` group. FR-4.2/AC-1 require mean POI. The must-never wording (AC-5) is scoped to "correction". The lead should confirm that the group page is acceptable, or decide that zeroing-purpose groups below 10 shots suppress it.
- **Fixture ↔ downstream acceptance criteria:** W10 (AC-9 vs P-11), W13 (`no_reference` vs T2.2/T4.11) and S23 (`roi_px` space vs `RoiPx`) need lead reconciliation before Group 2 opens.

### Tests
- [x] **All tests passing:**
  - `cd /Users/calebschwarz/Documents/Hunting && .venv/bin/pytest -q`: exit 0, 540 passed, 1 skipped. The skip is `SHELLCHECK: NOT INSTALLED`, declared. 1 warning: the Starlette httpx deprecation.
  - `make lint`: exit 0.
  - `bandit -c pyproject.toml -r src`: exit 0, no issues.
  - Each task's Run command re-run by review-1, with counts matching the completion notes:
    - T1.1: 15
    - T1.3: 43
    - T1.6: 95
    - T1.7: 114, which is 121 with layering as recorded in the follow-up
    - T1.8: 36 passed, 1 skipped; the negative start `RGA_HOST=0.0.0.0 ./scripts/run.sh` → exit 2
    - T1.9: 13
    - T1.10: 76
  - Slice A, per review-2: T1.2 108, T1.4 48, T1.5 52.
- [ ] **Test coverage adequate.** It is not adequate:
  - W1: 18 schema elements, plus the enum↔CHECK contract.
  - W5: the scan gate and the `SCAN_IGNORE_VULNS` env var.
  - W6: the 0700 permission.
  - W4 and W11: verifier gaps.
  - W12: the catalog against design §7.
- **Tier coverage:**
  - The Group 1 gate is T1 + T2 (test-plan §6), and both ran.
  - T3/T4 are not due for Group 1.
  - Informational only, not the T3 gate: review-1 started the real server via `scripts/run.sh` on a temp data dir and probed it:
    - `/healthz` 200 `{"status":"ok","schema_version":1}`
    - `Host: evil.example` → 400
    - cross-origin POST → 403 `bad_origin`
    - `Origin: null` multipart → 403
    - `text/plain` → 415
    - `--path-as-is /static/../config.py` → 404
    - `lsof` showed only `127.0.0.1:<port>` LISTEN
    - `RGA_HOST=::1` bind works, and a same-origin `[::1]` POST passes the guards
    - the port was free after stop, and the temp dir was removed
  - Declared and outstanding, not passed:
    - shellcheck is not installed.
    - api.js runtime behavior is covered only by an ad hoc node check, as declared in test-evidence.md.
    - pip-audit needs network; it ran in T1.8.
- **No assertions on mocks.** The `uvicorn.run` stub in `tests/unit/test_config.py:109-154` asserts what `main()` passes, plus a real DB side effect. That is acceptable.
- **Completion notes and test-evidence match what the Run commands executed.** Verification sentinels don't apply under B-01.

### Verdict
Verdict: FAIL — 0 Critical, 13 Warning (W1–W7 Slice B, W8–W13 via review-2), 27 Suggestion. Per the handoff, PASS requires zero Critical and zero Warning. The blockers are untested schema elements, a permission and an env var (testing-standards rule 5); verifier gaps on must-never guards (no-CDN, SHOOT_MORE mil numbers, the scan gate); the migration-runner rollback bug; missing public docstrings; and fixture-adequacy issues that gate the Group 2 spike.

## Group 1 — Cycle 2

Date: 2026-10-01 · Reviewing: Group 1, Foundations and contracts, after fix wave F1.1–F1.5 (W1–W13) · Scope/cycle: Group 1, cycle 2 of 3 (decisions.md B-07). Cycle 3 is terminal.
Synthesizer: review-3 (Slice B: W1–W7, plus whole-group cross-module consistency). Analyst: review-4 (Slice A: W8–W13).
Coordination: degraded mode (B-01). Ground truth is disk, tasks.md, test-evidence.md and decisions.md. No sentinels and no git baseline, as expected.
Method:
- Every Slice B file the fix wave changed was read in full on current disk.
- The other Slice B files have not changed since cycle 1: their mtimes are 22:10–22:29, before the cycle-1 review at 23:24. They were regression-checked through their tests and through their seams with the changed files.
- Every F1.x Run command and every original Group 1 Run command was re-run.
- Mutation testing ran on a scratch copy (`/tmp/rga-review3.*`, venv symlinked). The repo was never edited. Each harness had a positive control that failed as expected. Mutant counts:
  - 47 DDL
  - 17 runner, permission and header
  - 14 schema and no-CDN
  - 11 scan gate
  - 8 synth, as cross-checks of review-4
- Live probe (informational, not the T3 gate): the real server, started via `scripts/run.sh` on a temp data dir and driven with the project-local Playwright Chromium.
- Plugin passes merged:
  - `feature-dev:code-reviewer`: no findings at ≥ 80 % confidence; its watch items are merged below.
  - `pr-review-toolkit:silent-failure-hunter`: 3 findings, re-verified and re-rated below.
  - review-4 merged its own `feature-dev:code-reviewer` and `pr-test-analyzer` passes.
- Before rating the two F1.2 gaps, I asked coding-4 (the F1.2 author) about them. coding-4 confirmed both were oversights.

### W1–W13 resolution

| Item | Fix | Status | Evidence |
|---|---|---|---|
| W1: schema elements untested; enum↔CHECK checked from one side | F1.1 | **Resolved as raised** | My own run killed every cycle-1 item (the 18 listed plus `groups.lot_id`) and 4 enum mutants: the `'estimated'` typo, `superseded` dropped, an extra `loads.kind` member, `sandbags` re-added. The new contract test reads the DDL from `sqlite_master` and the enums, and fails if a new CHECK enum has no pair. Five schema elements that were not among the cycle-1 findings still survive; see N1. |
| W2: migration rollback | F1.1 | **Resolved** | Killed: the cycle-1 runner, which appended COMMIT and had no in-transaction check (4 failed); no `complete_statement` pre-check (3); no `in_transaction` check (1); no rollback (4); commit before the version check (3). For each of the 5 bad files, `sqlite_master`, `user_version` and the reopened file stay unchanged, and a second `migrate()` raises the same error. Two survivors are outside W2's contract: `BEGIN` vs `BEGIN IMMEDIATE`, and `except Exception` vs `BaseException`. |
| W3: AC-5 guard in AdviceOut | F1.2 | **Not resolved** (partly) | `stats` and `correction` are closed. Each of these mutants is killed: removing the `GroupStatsOut` isinstance check, the correction check, `extra="forbid"`, or the INVALID kind. A domain `GroupStats` dataclass is rejected, because pydantic picks `GroupStatsOut`. `message` still accepts numeric correction and direction text; see W3-R. |
| W4: no-CDN verifier | F1.2 | **Resolved** | Killed: the css-import pattern dropped; attribute-url with no single-quote form; the namespace allowlist matched by prefix; `wss`/`ftp` dropped; `<link href="//fonts.gstatic.com">` injected into the real `base.html`. A test enforces a negative example for each of the 6 patterns. SVG namespace URIs pass by exact allowlist. The backslash case is still open; see S34. |
| W5: scan gate fails open | F1.3 | **Resolved** | 11 mutants killed: unknown rc → 0; no `pins == []` check; no `-s requirements.lock`; a count-only name check; a `vulns` key typo; `--ignore-vuln` dropped; `skip_reason` ignored; status 1 overriding 3; HIGH not failing; `exclude_dirs = ["tests","data"]` re-added; the bandit `errors`/0-lines check dropped. The real `./scripts/scan.sh` exits 0. |
| W6: 0700 data dirs | F1.1 | **Resolved** | Killed: no chmod; chmod 0750; chmod of the data dir only. The tests cover a fresh dir, `umask(0)`, and pre-existing 0755 dirs. |
| W7: anti-framing headers | F1.1 | **Resolved** | Killed: no middleware; middleware not outermost; 500 without headers; no `Referrer-Policy`; `frame-ancestors 'self'`; `append` (duplicate headers); headers added to the wrong ASGI message. The constants equal the design §11 row (design.md:400, checked by hand), and the live server sends all four headers. The `no-referrer` value has a side effect on native form POSTs; see S30. The B-10 app.py deviation is limited to the wiring and the 500 headers, as accepted. |
| W8: domain docstrings | F1.4 | **Resolved** [via review-4] | All 17 classes are documented. The AST gate kills dropped-docstring mutants and catches a new undocumented domain module. My AST pass over `src/` finds no missing public docstring, only nested closures. |
| W9: `round_tenth` with numpy | F1.4 | **Resolved** [via review-4] | Fixed by `float(x)` coercion; reverting it is killed. The float32 nuance is S37. |
| W10: ragged fixture | F1.5 | **Resolved per B-09** [via review-4] | Over 30 seeds: max member separation 0.451–0.493 d (< 0.6 d), one dark component, no single attached, and 1 distance-transform peak at 0.5 d suppression. Raising the separation cap from 0.5 to 0.7 d is killed. The T2.4 caveat is in Cross-Task Consistency. |
| W11: truth keys vs the render | F1.5 | **Not resolved** (partly) [via review-4; reproduced by review-3] | The reference circle, ragged outline and merged-pair checks now hit the render (8 mutants killed). Grid corner identity is still unverified, and the new F1.5 truth keys and RNG paths have verifiers that cannot fail; see W11-R. |
| W12: catalog breadth | F1.5 | **Resolved in content** [via review-4] | The new content: `hires_wipe_decoys` (380 ppi, 17.8 MP, 200 ppi working image); wipe rings, verified on the render; decoys rendered at 0.36–0.81 A1; a 0.4 d pair; a black aim dot. Their verifiers are part of W11-R. The 4000 px cap claim is wrong; see S38. |
| W13: `no_reference` diamond | F1.5 | **Resolved** [via review-4] | On seeds 0 and 3, contour/fitEllipse and HOUGH_GRADIENT_ALT find no reference-like candidate. The cycle-1 circular dot, used as a control, still scores 1.000 in. The T2.2 caveat is in Cross-Task Consistency. |

Summary: 11 resolved; W3 and W11 are not resolved.

### Spec Alignment
- **Fix wave vs design.** The changes match design §5, §11 and the build-time notes for F1.1, F1.4 and F1.5:
  - §5: executing both the DDL file and the design block gives identical `sqlite_master` and `user_version`.
  - §11: the new Anti-framing row matches `SECURITY_HEADERS` exactly.
- **B-09 short-zero-group rule:**
  - Met for `AdviceOut.stats` and `AdviceOut.correction`.
  - Not met for `AdviceOut.message` (W3-R).
  - Not met for the `GET /api/groups/{id}/stats` contract (N2).
- **AC-5 must-never:** no producer exists yet, and the API-boundary backstop still has the two gaps above.
- **AC-9:** under design §6's 0.5 d peak rule, the fixture is now unambiguously ragged (B-09).
- **AC-10 pieces:**
  - The rejection CHECK, the final-needs-aim CHECK and the loopback bind are unchanged and still tested.
  - There is no outbound network and no CDN in `src/`, templates or static files; checked by grep and the new no-CDN check.
  - There are no mean-radius or best-N symbols.
  - The rejection reason is still single-valued in the enum, the DB CHECK and the schema.

### Critical
- None.

### Warning

**W3-R. `AdviceOut.message` still accepts numeric correction and direction text for `shoot_more`/`invalid`.** (W3 residual)
- Where: `src/rga/web/schemas.py:920` (`_MIL_OR_CLICK_TEXT`) and `:945`; tests at `tests/unit/test_schemas.py:664-676`.
- Evidence (static-verified): with kind `shoot_more` or `invalid`, these messages validate:
  - "DOWN 0.2, LEFT 0.3"
  - "Correct UP 0.2 RIGHT 0.1"
  - "Group is 0.3 high and 0.2 right; shoot more"
  - "Come up 0.2 mrad"
  - "0.4 MOA low"
  - "Dialed DOWN 0.2", because `\bdial\b` does not match "dialed" [via feature-dev:code-reviewer]
- coding-4 confirmed it skipped direction-word rules to avoid false positives such as "6 left to fire", and did not consider a decimal-number ban or `mrad`.
- Why: F1.2's acceptance criterion is "no mil POI, offset or direction text in any field (B-09)". This guard is the API backstop for the AC-5 must-never (no numeric correction from a 2–9 shot group outside Step 1), and a numeric correction gets through it.
- Fix:
  - For these kinds, reject any decimal number in `message`, plus the stems `dial\w*`, `mils?`, `mrads?`, `moa` and `clicks?`. Integer counts stay allowed.
  - Add a negative test for each example above, and keep "4 of 10 so far, 6 left to fire" as a positive.

**W11-R. Some fixture truth that gates the Group 2 spike still has verifiers that cannot fail.** (W11 residual) [via review-4 NEW-W1 and NEW-W2; reproduced by review-3]
- **(a) Grid corners.**
  - Where: `tests/unit/test_synth.py:341-373`; truth at `tests/synth/generate.py:727-737`.
  - Why the mutants survive:
    - The lattice is anchored only at `corners_in[0]`.
    - Off-line lattice points are skipped (`:360`) under an 81 % floor (`:366`).
    - Each corner only needs a line pixel within ±4 px (`:369-371`).
  - Surviving mutants:
    - TR one square inward (review-3)
    - BR and BL swapped (review-3)
    - TL slid 0.3 in along the top edge (review-3)
    - BL two squares in (review-4)
    - TR at an interior junction (review-4)
    - the rightmost vertical line, or the bottom horizontal line, not drawn (review-4)
  - `test_synth.py:295-300` still compares the corners through the same homography that produced them.
- **(b) New F1.5 truth keys.**
  - The decoy area floor is asserted from the generator's own `radius_in` (`test_synth.py:418`), and `:421` only checks that the center pixel is dark. Decoys drawn at 0.5× radius with truth unchanged (0.09–0.25 A1, below the floor) survive; reproduced by review-3.
  - `holes_light_core` and `holes_wipe` are not checked per hole, so flipping one flag survives [review-4].
- **(c) Determinism.**
  - `test_synth.py:125-140` covers 5 scenarios, none with decoys or wipe rings.
  - An unseeded RNG in `_decoys` (`generate.py:476-502`) survives (reproduced by review-3), as does one in the wipe branch (`:413-416`) [review-4].
  - Design §7 requires determinism, and B-11 runs the spike gate on all 17 scenarios.
- The fixtures are correct today; review-4 measured them on the render. The defect is that a regression would stay green. This is the same class and severity as cycle-1 W11 (testing-standards rule 11).
- Fix:
  - Build the expected lattice from all four `corners_px`. Require every lattice point not hidden by a hole or the aim mark, boundary points included, to sit on line pixels, with an L-junction check at each corner.
  - Assert that each decoy's rendered dark area is ≥ 0.35 A1 and within about 10 % of its truth area.
  - Check `holes_light_core` per hole: mean gray in a 0.4 r disc > 150 exactly when the flag is set. Check `holes_wipe` per hole as well.
  - Parametrize the determinism test over every catalog scenario.

**N1. Five schema elements still have no test that fails without them.** (new; same class as W1)
- Where, all in `src/rga/db/migrations/001_initial.sql`:
  - `:99`: `groups.load_id REFERENCES loads(id)`
  - `:101`: `groups.photo_id REFERENCES photos(id)`
  - `:81`: `zeros.lot_id REFERENCES load_lots(id)`
  - `:148`: `zero_checks.zero_id … ON DELETE CASCADE`
  - `:51`: the lower bound of `wind_clock BETWEEN 1 AND 12`; only 13 is tested (`tests/integration/test_schema.py:562-566`)
- Evidence (static-verified by mutation): removing any one of them, or changing the bound to `BETWEEN 0 AND 12`, leaves the full suite at 802 passed, 1 skipped.
- Each element is live. On the real schema:
  - `load_id=9999`, `photo_id=9999` and `lot_id=9999` each raise FOREIGN KEY.
  - `wind_clock=0` raises CHECK.
  - Deleting a zero removes its checks.
- Why: testing-standards rule 5, and the lead's FAIL trigger (a new schema element with no test that would fail without it).
- Fix: using the existing `Seed` helper in `test_schema.py`:
  - add FK-violation cases for the three FKs;
  - add `wind_clock=0` to `CHECK_VIOLATIONS`;
  - add a "deleting a zero removes its zero_checks" case.
- The `zeros.rifle_id` cascade needs no test: on consistent data its mutant is equivalent, because the loads → zeros cascade removes the rows first.

**N2. The registered `GET /api/groups/{id}/stats` contract cannot express B-09's size-only stats.** (new; cross-task consistency)
- Where: `src/rga/web/schemas.py:1070` (`RouteSchema(response=GroupStatsOut)`). `GroupStatsOut` (`:486-498`) requires `mean_dx_in`, `mean_dy_in`, `mean_dx_mil` and `mean_dy_mil`.
- Evidence (static-verified): `GroupStatsOut.model_validate({"n":5,"es_in":1.0,"es_moa":0.95,"includes_estimated":False})` fails with 4 errors.
- The conflict:
  - B-09 says short `zero_step2`/`zero_confirm` groups (< 10 shots) return no mil POI "in advice/stats".
  - T3.5 and T3.7 (tasks.md:317, :345) require that "the stats this layer returns have no mean POI".
  - The build-time notes make `ROUTE_SCHEMAS` binding for later groups, and neither T3.5 nor T3.7 owns `schemas.py`.
  - So a route built from the registry must either return mil POI for a 5-shot zeroing group (the AC-5/B-09 must-never) or fail response validation with a 500.
- coding-4 confirmed this was an oversight, not a decision.
- Fix: make the registry response `GroupStatsOut | SizeOnlyStatsOut`. Add a schema test that a size-only body validates for the stats route and a mixed body is rejected.
- Lead decision for the record: does B-09 also cover the per-shot `dx_mil`/`dy_mil` in `ShotOut`, inside `GroupDetailOut`, for those groups?

### Suggestion
Cycle-1 Suggestions S1–S27 stay deferred per B-09, and the fix wave made none of them worse. S15 still flags the same 4 files. S23 (the `roi_px` coordinate space) is still open; see S47.

New in cycle 2 (Slice B and cross-module; review-3 plus plugin passes):
- **S28: Table-rebuild migrations silently delete data.**
  - `src/rga/db/connection.py:82` runs every migration inside `BEGIN IMMEDIATE`. SQLite ignores `PRAGMA foreign_keys` inside a transaction, and `connect()` has already turned foreign keys on.
  - So SQLite's documented rebuild (FKs off, create the new table, copy, drop, rename) cascades on `DROP TABLE`.
  - Reproduced with a synthetic 002 that rebuilds `rifles` unchanged: `migrate()` returned 2, and `loads` went from 1 row to 0.
  - Rated Suggestion because it is:
    - pre-existing: the cycle-1 runner also wrapped migrations in `BEGIN…COMMIT`, as design §5 requires;
    - latent: no 002 is planned for v1;
    - outside the cycle-2 FAIL-trigger bar.
  - Before any 002 lands, do one of:
    - turn FKs off before `BEGIN`, run `PRAGMA foreign_key_check` inside the transaction and raise on any rows, and restore FKs in `finally`;
    - reject migration files that contain `foreign_keys`.
  - Either way, add a rebuild test. The lead may prefer to log this as a known limitation next to B-10.
  - [via silent-failure-hunter, which rated it HIGH and called it fix-wave-introduced; review-3 found it pre-existing]
- **S29: a `# nosec` comment hides a HIGH bandit finding.**
  - `scripts/scan.sh:98-104` ignores bandit's `_totals.nosec` and `skipped_tests`, so the gate prints clean with no trace of the suppression. Reproduced by the hunter. The repo has no `nosec` today (grep).
  - Fix: print both counts, and fail or report NOT RUN when they are above zero unless allowlisted like `SCAN_IGNORE_VULNS`; or run bandit with `--ignore-nosec`.
  - [via silent-failure-hunter]
- **S30: native same-origin form POSTs are refused.**
  - With `Referrer-Policy: no-referrer` (`src/rga/web/security.py:26`), Chromium sends `Origin: null` on a native same-origin HTML form POST, and `OriginGuard` (`:88`) refuses it with 403.
  - Verified live (real server via `run.sh`, project-local Chromium): a `<form method=post enctype=multipart/form-data>` submit got 403 with `Origin: null`, while JSON and FormData `fetch()` POSTs passed the guard.
  - Nothing breaks today, because every write goes through `fetch()` in `api.js`. Record in the build-time notes that pages must never use native form POSTs.
  - Safari and Firefox `fetch()` behaviour was not checked, because only Chromium is installed. This requires live validation. The Fetch spec keeps the real Origin for CORS-mode fetch.
  - `Referrer-Policy: same-origin` would settle both questions and still send no referrer cross-site. It changes a design §11 value, so it is a lead decision.
  - [own; watch item from feature-dev:code-reviewer]
- **S31: `migrate()` can report success with nothing saved.** On a caller-supplied `autocommit=True` connection, `commit()` is a no-op (`connection.py:88`), so the transaction stays open. `connect()` never creates such a connection. Raise if `conn.in_transaction` is still true after the commit. [via silent-failure-hunter]
- **S32: two simultaneous starts can both apply 001.** `migrate()` reads `user_version` (`connection.py:71`) before `BEGIN IMMEDIATE`, so the second start crashes on "table already exists". Nothing is corrupted. Re-read the version inside the transaction. [via feature-dev:code-reviewer watch item]
- **S33: a stray file.** `/Users/calebschwarz/Documents/Hunting/circle` (12 bytes, "M6 diamond-", mtime 23:32 during F1.5) is untracked, in no task's file list and not gitignored. The lead should delete it before any commit.
- **S34: the no-CDN detector misses backslash and tab URLs.** `tests/unit/test_templates_shell.py:97-106` does not flag `src="/\evil.example/x.js"`, `href="\\evil.example/a.css"` or `src="/<TAB>/evil.example/a.png"`, which browsers normalize to `//`. The CSP now blocks them in the browser. Same class as cycle-1 S1.
- **S35: defense in depth for AC-5.** `AdviceOut` accepts `kind="correction"` with `stats.n` = 5. Step 1 allows only 1–3 shots and Step 2 needs 10 or more, so a correction from 4–9 shots is never valid. Consider rejecting it in the validator.
- **S36: header-constant drift is untested.** `tests/integration/test_app_security.py:371-383` compares `SECURITY_HEADERS` with a hand-copied constant, not with design §11. Parse the design row, as the registry test does for §4.7.
- **For the record:** these surviving mutants are equivalent or outside the contract and need no action:
  - the `zeros.rifle_id` cascade
  - `BEGIN` vs `BEGIN IMMEDIATE`
  - `except Exception` vs `BaseException`
  - the `AdviceOut` union order (pydantic smart mode)

Slice A [via review-4]:
- **S37** (review-4 S1): float32 halves round down.
  - `units.py:48-51` widens np.float32 to float64 before `repr`, so float32 0.35, 0.45, 0.65 and 1.05 round down. `in_to_mil(np.float32(1.26),100)` gives 0.3, vs 0.4 for a Python float. `test_units.py:139-142` is tautological.
  - This matches cycle-1 W9's prescribed fix and only bites within one float32 ulp of a .x5 boundary. pr-test-analyzer's Critical rating was not adopted.
  - The lead decides the float32 semantics.
- **S38** (S2): `hires_wipe_decoys` does not exercise the 4000 px cap; its working image is 2213×2626.
  - `catalog.json:113` claims "4000 px" and "13.5 MP" (it is 17.8 MP), and design.md:494 claims the scenario exercises the cap.
  - The cap is unit-tested in `test_transform.py:213-234`. Correct the description and the note.
- **S39** (S3): design.md:493 gives the outline area as 1.6–1.8 A1. Over 30 seeds it is 1.58–1.90; the test band of 1.5–3.0 holds.
- **S40** (S4): the diamond test (`test_synth.py:376-386`) accepts an equal-area circle; a 0.798 in circle survived. Add a circularity or 4-vertex check.
- **S41** (S5): `generate.py:714-723` sorts `axes_px` to [major, minor] but keeps cv2's width-axis angle. For `circle_tilt_warn`, that angle belongs to the minor axis. Document the convention or normalize the angle.
- **S42** (S6): `close_holes_tight_pair` spacing varies by seed: 1.41 d on seed 0, 2.09 d on seed 8. This is fine under the fixed-seed convention.
- **S43** (S7): specks don't avoid printed circles, dots or decoys, and decoys don't avoid specks or a bull (`generate.py:462-502`).
- **S44** (S8): cluster members get `light=_in_black()` (`:302`), but `_cluster_shape` always draws dark (`:457`). Latent, because no cluster sits on black.
- **S45** (S9): there are short rationale clauses in new private docstrings (`generate.py:18-20, 315-316, 477-478`; `test_synth.py:223-224, 242`). They are brief, so they don't meet the verbose why-prose trigger. Trim them when the code is next touched.
- **S46** (S10): self-referential asserts remain at `test_synth.py:299-300, 232, 431, 444`.
- **S47** (review-4 NEW-W3, rated Warning by review-4): truth `groups[].roi_px` leaves out the group's own holes in most scenarios.
  - Where: `generate.py:645-657`; the ROI is aim ± 1.1 in.
  - Confirmed by review-3: 2 of 10 hole centers fall inside the ROI for `high_right_group` and 6 of 10 for `ragged_10x7`, against 5/5 per group for `multi_dot_target`.
  - Only `multi_dot_target` is tested (`test_synth.py:305-321`).
  - Rated Suggestion here because it is pre-existing T1.5 code, outside the cycle-2 new-finding bar, and the build-time notes scope `roi_px` to the multi-group ROIs, where it is correct.
  - It is still a trap: a Group 2 harness that feeds truth `roi_px` to `detect_holes`/`analyze_group` would drop shots and fail B-11 falsely.
  - Fix it together with S23 before Group 2 consumes it: size the ROI from the hole extent, and test containment for every scenario.

### Cross-Task Consistency
- **Enums ↔ DDL ↔ schemas:** now asserted from both sides (`test_domain_db_contract.py`), including a coverage test for new CHECK enums.
- **Design §5 ↔ DDL:** equivalent after normalization; both give identical `sqlite_master` and `user_version`.
- **Design §11 ↔ `SECURITY_HEADERS`:**
  - The values are identical today, but drift isn't tested (S36).
  - The CSP-compatibility asset scan passes for `base.html` and `api.js`, and it covers later templates and scripts automatically.
- **B-09 ↔ schemas ↔ Group 3 tasks:** the short-zero-group rule is enforceable in `AdviceOut` but not in the stats-route contract (N2), and `message` leaks (W3-R).
- **OriginGuard ↔ Referrer-Policy ↔ api.js:** compatible for `fetch()`; native form POSTs are refused (S30).
- **Fixtures ↔ Group 2** [via review-4]. These are notes for the lead before Group 2 opens; they are not counted.
  1. The ragged blob passes the split IoU ≥ 0.8 test on 30/30 seeds and stays ragged only because of design §6's 0.5 d peak suppression; at 0.3 d, 4/30 seeds give 2 peaks. T2.4 should pin min peak separation ≥ 0.5 d as a tested invariant.
  2. Sub-0.6 d pairs (the 0.4 d pair and the 0.5 d merged pair) render at 1.28–1.54 A1 with 1 peak, so they read as single on 5/6 seeds. The T2.4 harness must define how these pairs are scored under B-11.
  3. The decoys in `hires_wipe_decoys` (0.36–0.81 A1, solid, round) differ from holes mainly in gray level and the missing halo. The clean-target FP ≤ 1 gate may fail by construction, or the detector may overfit to that gray gap. B-11 already escalates.
  4. Accumulator-only HOUGH_GRADIENT still proposes the `no_reference` diamond (0.64–0.90 in). T2.2 must keep design §6's fill and circularity verification.
  5. `roi_px`: see S23 and S47.
- **Unchanged Slice B files.** These have not changed since cycle 1, and their tests pass: the config surface, run.sh, Makefile, conftest, rows.py, config.py, `__main__.py`, base.html, app.css, api.js, the bootstrap and lock scripts, and the requirements files. I checked their seams with the changed files:
  - the 0700 dirs under the conftest temp `RGA_DATA_DIR`
  - the CSP against `base.html` and `api.js`
  - the empty `[tool.bandit]` table with `bandit -c pyproject.toml`

### Tests
- [x] **All tests passing.**
  - `cd /Users/calebschwarz/Documents/Hunting && .venv/bin/pytest -q`: exit 0, 802 passed, 1 skipped (`SHELLCHECK: NOT INSTALLED`, declared), 1 warning (the Starlette httpx deprecation, lead-accepted).
  - `make lint`: exit 0.
  - `./scripts/scan.sh`: exit 0, with network available.
    - `BANDIT: scanned 1686 lines; high=0 medium=0 low=0`
    - `PIP-AUDIT: audited 21 pinned packages; vulnerable packages=0`
  - Fix-wave Runs re-run; the counts match the completion notes:
    - F1.1: 197
    - F1.2: 193
    - F1.3: 74 passed, 1 skipped, then `scan.sh --only bandit` exit 0
    - F1.4: 115 (also re-run by review-4)
    - F1.5: 110 (also re-run by review-4)
  - Original Group 1 Runs re-run:
    - T1.1 `test_env`: 15
    - T1.6: 106 (the security test file grew)
    - T1.10: 87
    - T1.4 plus calibers: 63
    - The T1.8 negative start, `RGA_HOST=0.0.0.0 ./scripts/run.sh`, exits non-zero and creates no data dir.
- [ ] **Test coverage adequate.** It is not adequate:
  - N1: five untested schema elements.
  - W11-R: fixture verifiers that cannot fail.
  - W3-R and N2: the AC-5 backstop.
- **Tier coverage:**
  - The Group 1 gate is T1 + T2 (test-plan §6), and both ran. T3/T4 are not due.
  - Informational only, not the T3 gate: review-3 started the real server via `scripts/run.sh` on a temp data dir and probed it.
    - `/healthz` 200 `{"status":"ok","schema_version":1}`
    - all four §11 headers present on the live response
    - Chromium form-POST and fetch results as described in S30
    - the port was free after stop, and the temp dir was removed
- **No assertions on mocks.** The scan.sh tests use stub tools only as recorded inputs, and they assert scan.sh's own exit codes and output. That is acceptable.
- **Completion notes and test-evidence match what was reproduced,** with two exceptions: the 4000 px claim (S38), and the decoy floor, which is asserted from generator parameters rather than the render (W11-R b).

### Verdict
Verdict: FAIL — 0 Critical, 4 Warning (W3-R, W11-R, N1, N2), 20 new Suggestion. 11 of the 13 cycle-1 Warnings are resolved; W3 and W11 are only partly resolved. The two new Warnings are five untested schema elements and a stats-route contract that cannot express B-09. Cycle 3 is the terminal cycle for Group 1.

## Group 1 — Cycle 3

Date: 2026-10-01 · Reviewing: Group 1, Foundations and contracts, after fix wave F2.1–F2.3 (W3-R, W11-R, N1, N2) · Scope/cycle: Group 1, cycle 3 of 3, TERMINAL (decisions.md B-07, B-13).
Synthesizer: review-5 (Slice B: W3-R and N2 via F2.1, N1 via F2.3, regression checks of the cycle-1 Slice B items, cross-module consistency). Analyst: review-6 (Slice A: W11-R via F2.2, regression checks of W8–W13).
Coordination: degraded mode (B-01). Ground truth is disk, tasks.md, test-evidence.md and decisions.md. There are no commits and no sentinels, as expected.

Method:
- **What changed.** The only files under `src/`, `tests/` and `scripts/` newer than the cycle-2 review (review.md, 00:19:59) are `src/rga/web/schemas.py`, `tests/unit/test_schemas.py`, `tests/integration/test_schema.py`, `tests/unit/test_synth.py` and `tests/synth/generate.py`.
  - `generate.py` has a newer mtime (00:36:19) because F2.2 mutated it in place and then restored it. Its content is byte-identical to `/tmp/gen.bak` (23:31, F1.5) and `/tmp/gen2.bak` (00:32, before the F2.2 mutation). Both reviewers checked this independently; review-6 compared sha256 and byte-compared renders.
  - Every other Group 1 file has the same content it had in cycle 2.
- **Mutation testing.** I ran 79 mutants on a scratch copy at `/tmp/rga-review5.DGKS`, with the venv symlinked. A test confirmed the scratch copy imports its own `rga` and `tests.synth.generate`. The repo was never edited, and every mutated scratch file was diffed back to identical with the repo.
- **Verify-the-verifier.** My first F2.1 mutation run killed every mutant, including ones that should survive. The failing test was `test_registry_covers_exactly_the_routes_in_design_4_7`, which parses design.md, and the scratch copy did not contain it. I copied the spec docs into the scratch copy, confirmed a green baseline (901 passed, 1 skipped), and then re-ran every mutant. The positive control C0 (message check removed) was killed.
- **Plugin passes:**
  - `feature-dev:code-reviewer` on F2.1 and F2.3. Static only: Bash was disabled in its session. One finding at ≥ 80 % confidence; see S49.
  - `pr-review-toolkit:pr-test-analyzer` on all three fix-wave test files. It ran 33 mutants on its own scratch copy. Nothing rated ≥ 8 and two gaps rated 6; see S50, plus S48 and S51.
  - `silent-failure-hunter` was not run, because the fix wave added no error handling.
  - review-6 merged its own `feature-dev:code-reviewer` and `pr-test-analyzer` passes.

### W3-R, W11-R, N1, N2 resolution

| Item | Fix | Status | Evidence |
|---|---|---|---|
| W3-R: `AdviceOut.message` accepted numeric correction text for `shoot_more`/`invalid` | F2.1 | **Resolved as raised** | All 6 cycle-2 examples are rejected for both kinds, both with and without `SizeOnlyStatsOut` stats (24/24, checked directly). The count text "Shoot more shots: 4 of 10 so far, 6 left to fire" is still accepted. The prescribed rules are in place: any decimal (`schemas.py:929`), the stems `dial\w*`, `click\w*`, `mils?`, `mrads?` and `moa` (`:930`), plus "come up/down" and a number next to a direction word in either order (`:931-933`). **Mutants:** 24 of the 30 W3-R mutants were killed. Killed: removing each pattern; dropping each of the 6 direction words; narrowing `\W{0,3}`; dropping either `re.I`; dropping either kind from `_NO_NUMBER_KINDS`; removing the correction check or the `GroupStatsOut` check; removing the benign strip; `search`→`match`. The survivors concern coupling coverage, not the prescribed rules, and are rated Suggestion (S50): M19 and M21 widen the benign strip; P8 and P13 skip the message check when stats are present; P7 loosens `ZeroOut.advice`; P31 removes the plural stems. |
| N2: the stats-route contract could not express B-09 size-only stats | F2.1 | **Resolved** | `ROUTE_SCHEMAS[("GET","/api/groups/{id}/stats")]` is `GroupStatsOut \| SizeOnlyStatsOut` (`schemas.py:1095`). A size-only body validates as `SizeOnlyStatsOut`. Mixed bodies are rejected. `GroupDetailOut` has no `stats` field, and a test asserts that. A domain `GroupStats` with full stats on `shoot_more` is rejected. **Mutants killed:** route reverted to `GroupStatsOut` (M24); `extra="forbid"` dropped from `SizeOnlyStatsOut` (M26); `AdviceOut.stats` without `SizeOnlyStatsOut` (M27); a POI field added to `SizeOnlyStatsOut` (M28). M25 survives (route made nullable); it is outside the contract. |
| N1: five schema elements had no test | F2.3 | **Resolved** | 10 of 11 DDL mutants killed: `groups.load_id`, `groups.photo_id` and `zeros.lot_id` FKs each removed; `zeros.lot_id → loads`; the `zero_checks.zero_id` cascade removed, and also changed to no-action; `wind_clock` as `BETWEEN 0 AND 12`, `<= 12`, `BETWEEN 1 AND 13`, and with the CHECK removed. The survivor (adding `ON DELETE CASCADE` to `photo_id`) and the coincident-id parent-table gap are S51. |
| W11-R: fixture verifiers could not fail | F2.2 | **Resolved** [via review-6; cross-checked by review-5] | review-6 ran 79 mutants with both controls behaving as expected, and every cycle-2 survivor is now killed: grid corner identity (7 of 7), decoys at 0.5× and 0.55×, per-hole `holes_light_core` and `holes_wipe` flips, and unseeded RNG in `_decoys` and the wipe branch. I independently ran 9 mutants and all 9 were killed: rightmost or bottom line not drawn; decoys drawn at 0.5× with truth unchanged; unseeded decoy-radius RNG; unseeded wipe RNG; last `holes_light_core` flag flipped; TR one square inward; BR/BL swapped; TL slid 0.3 in. |

**Regression check of the 11 cycle-1 Warnings resolved in cycle 2:** none has regressed.
- W1: 13 of 13 DDL spot mutants killed. They cover `shots_expected`, the `purpose` CHECK, `sha256` UNIQUE, `zero_checks.group_id` UNIQUE, `zero_id` SET NULL, `lot_id` NOT NULL, the `'flier'` reason, final-needs-aim, the `superseded` state, the `estimated` origin typo, the sessions cascade, `reference_confirmed` and `sandbags`.
- W2: 5 of 5 runner mutants killed.
- W4: 1 of 1 killed (`//fonts.gstatic.com` injected into `base.html`).
- W5: 2 of 2 killed (unknown rc treated as clean; `pins == []` check removed).
- W6: 1 of 1 killed (chmod removed).
- W7: 2 of 2 killed (`X-Frame-Options` dropped; `frame-ancestors 'self'`).
- W8, W9, W10, W12, W13 [via review-6]: 16 of 17 of the cycle-2 killed set are still killed. The one survivor, R6, is pre-existing; see S59.

### Spec Alignment
- **F2.1** matches its acceptance criteria and the cycle-2 prescription. The phrasing gaps that remain are outside what was prescribed; see S48 and S49.
- **N2** makes the registry able to express B-09's size-only stats. The T3.5 and T3.7 short-zero-group guards (tasks.md:252, :333, :361) can now be built against `ROUTE_SCHEMAS` without forcing a 500 or a leak.
- **F2.3** matches its acceptance criteria exactly.
- **F2.2** matches its acceptance criteria. Its stated "known limit" (a full mirror of the corner labels goes undetected) is wrong in the safe direction: the order test catches the mirror (S54).
- **AC-5 at the schema layer:**
  - `AdviceOut` rejects the app's own correction and POI vocabularies for `shoot_more`/`invalid`: the `Correction.text` form ("Dial DOWN 0.2, LEFT 0.3") and the `poi_text` form ("0.3 mil high, 0.2 mil right").
  - `stats` is size-only, and `correction` is null.
  - The authoritative control is T2.7's fixed integer-count templates with an exact-string test (B-14).
- **AC-10 pieces:**
  - The rejection reason is still single-valued in the enum, the DB CHECK and the schema; R7 was killed.
  - There is no outbound network or CDN use in `src/`, templates or static files (grep; W4 mutant killed). The only `http://` in `src/` is the Origin comparison at `security.py:88`.
  - There are no `mean_radius`, best-N or `group_reduction` symbols (grep).
- **Lead decisions not re-raised:** S28, S30, S33, S47 (moved to T2.18), float32 rounding, the B-14 residuals (bare direction words, per-shot offsets), the symmetric-grid mirror limitation, the ApiError placement in `web/app.py`, `check_same_thread=False`, the absence of shellcheck (declared), the B-10 `app.py` deviation, and `bandit -r src` only.

### Critical
- None.

### Warning
- None.

### Suggestion
Cycle-1 and cycle-2 Suggestions stay deferred per B-09 and B-13; this fix wave made none of them worse. S15 still flags the same 4 files under `ruff format --check`: `test_config_surface.py`, `test_scripts_syntax.py`, `generate.py` and `test_synth.py`. `make lint` does not gate formatting.

New in cycle 3 (Slice B and cross-module; review-5 plus plugin passes):
- **S48: the message guard still accepts some numeric phrasings outside the app's vocabulary.** (W3-R residual)
  - Where: `src/rga/web/schemas.py:924-934`.
  - Accepted today for `shoot_more`, verified:
    - "Group is 2 inches high and 1 inch right; shoot more"
    - "Group is 2 in high"
    - "2 milliradians high"
    - "Shoot more: 4 MOAs low so far"
    - "Move up by 2"
    - "elevation +2, windage -1"
    - spelled-out numbers such as "Hold two tenths high"
  - Why Suggestion and not Warning:
    - The cycle-2 prescription is met in full and mutation-proven: decimals, the stems `dial\w*`/`mils?`/`mrads?`/`moa`/`clicks?`, and a negative for each example.
    - None of these strings is in a vocabulary any planned producer emits.
    - B-14 makes T2.7's exact-string template test the authoritative control and this regex a backstop. A blocklist can never be complete; spelled-out numbers alone are an unbounded class.
  - Fix (whenever `schemas.py` is next touched):
    - add `inch(es)?`, `"`, `cm`, `mil\w*` and `moa\w*` to the unit pattern;
    - add a "spaced number" rule (`direction … by \d`);
    - add one negative for each.
  - [own; via pr-test-analyzer; considered by feature-dev:code-reviewer]
- **S49: the benign-count strip runs before the decimal check and can eat the fractional digits.**
  - Where: `schemas.py:925-927` and `:938-944`.
  - Example: "Hold 0.5 left to fire" becomes "Hold 0. " after the strip, so it is accepted. ".5 left to shoot" and "Hold 1.5 left to shoot" are accepted the same way.
  - This is a literal miss of F2.1's "rejects any decimal".
  - Why Suggestion: it only triggers on a decimal immediately before "left to fire/shoot". That phrase appears only in the integer count template, so there is no realistic producer path.
  - Fix:
    - run the decimal pattern on the raw message;
    - anchor the strip as `(?<![\d.])\d+\s+left\s+to\s+(?:fire|shoot)\b`;
    - add both strings as negatives.
  - [own; via feature-dev:code-reviewer, which rated it Important at 85; via pr-test-analyzer]
- **S50: coupling coverage of the message guard.**
  - Where: `tests/unit/test_schemas.py:705-720`.
  - Every message negative uses `stats=None`. Production `shoot_more` advice carries `SizeOnlyStatsOut` (the T3.5/T3.7 conversion rule).
  - Surviving mutants (260 passed each; reproduced by review-5, found by pr-test-analyzer):
    - P8: skip the message check when stats are present.
    - P13: early `return` when stats are size-only.
    - P7: `ZeroOut.advice` (`schemas.py:1019`, the only API carrier of `AdviceOut`) loosened to `dict[str, Any]`.
    - M19 and M21: the benign strip widened to `\d+\s+left\b`, or to "shots high/low…".
    - P31: `mils?`→`mil` and `mrads?`→`mrad`.
  - What is verified today: the guard behaves correctly with size-only stats (24/24 examples rejected), and `ZeroOut` rejects a nested bad advice.
  - Why Suggestion: the guarded element has tests that kill its removal and each of its rules. These survivors are configuration or shape variants, so none of the cycle-3 admissibility items applies.
  - Fix:
    - parametrize `CORRECTION_TEXT_NEGATIVES` and `REVIEW_NEGATIVES` over `stats ∈ {None, SIZE_ONLY_STATS}`;
    - add one `ZeroOut` negative;
    - add the negatives "Group is 2 left", "2 mils" and "2 mrads".
  - [own; via pr-test-analyzer, rated 6]
- **S51: the FK tests prove an FK exists, not which table it points at.**
  - Where: `tests/integration/test_schema.py:783-799`.
  - Photo id 1 coincides with load and session id 1, so `groups.photo_id REFERENCES loads(id)` or `sessions(id)` survive [via pr-test-analyzer D1/D2].
  - Adding `ON DELETE CASCADE` to `photo_id` also survives (own N1j). v1 has no photo-delete path, so it is unreachable.
  - F2.3's criterion ("fails if removed") is met.
  - Fix: assert `PRAGMA foreign_key_list` (target table and on-delete action) for every FK against design §5.
- **S52: one short rationale comment in a test.** `tests/unit/test_schemas.py:664` reads "Each negative is flagged by exactly one pattern, so dropping any pattern fails its own case." It is brief and gets the same treatment as S45.
- **S53: the `AdviceOut` docstring overclaims.** `schemas.py:950` says these kinds never carry "direction text in any field". Bare direction words are accepted by design (B-14). Reword it to "no number next to a direction word". [via feature-dev:code-reviewer]

Slice A [via review-6]:
- **S54 (S-a): the F2.2 notes misstate what is checked.** Where: `test-evidence.md:242` and `tasks.md:147`.
  - The "known limit" is wrong in the safe direction. A horizontal mirror of the corner labels is killed by the order test at `test_synth.py:296-304`; only the lattice test is blind to it. Correct the note.
  - `test-evidence.md:235` says "square centers show no line", but the code checks only the 4 corner squares (`:396-397`).
- **S55 (S-b): outer grid lines and corners are pinned only to about 2–6 px; interior junctions are sub-pixel.**
  - Shifting all `corners_px` by 1 px survives, and so do the right or bottom line shifted 2–4 px [via pr-test-analyzer].
  - Today the truth-vs-render offset on the outer lines is a median of 0.10–0.34 px and a max of 0.66 px.
  - Recommend that T2.18 adds a perpendicular sub-pixel check on the outer edges.
- **S56 (S-c): the decoy floor and area are checked on seed 0 only, and decoy centers only to within one radius.**
  - The smallest rendered decoy over seeds 0–7 is 0.36 A1, and seed 36 renders at 0.355 A1.
  - This is adequate under the fixed-seed-0 convention (design.md:468).
  - Recommend that T2.18 adds a seed sweep and a centroid ≤ 1 px assertion.
- **S57 (S-d): `holes_wipe` is verified on `hires_wipe_decoys` only.**
  - At 240 ppi in `close_holes_tight_pair`, the band test fails on 6 of 8 seeds. The flag means "drawn", not "visible".
  - Cross-slice note for Group 2.
- **S58 (S-e): `reference.spacing_in` and `reference.corners_in` have no test that would fail without them.** (`generate.py:732, 735`)
  - Nothing reads them today. This is pre-existing since T1.5 [also via pr-test-analyzer].
  - Fix: check that `project(h, corners_in)` ≈ `corners_px`, and that the corner span divided by the square count equals `spacing_in`.
- **S59 (S-f): removing `CLUSTER_MIN_AREA_RATIO` survives (R6).** Where: `generate.py:311`.
  - The tested seeds stay at ≥ 1.548 A1. Untested seeds fall to 1.42 A1 on 2 of 30.
  - Pre-existing since F1.5.
- **S60 (S-g): process.** F2.2 mutated the repo's `generate.py` in place while peers ran the suite. The content is identical to the backup. Use scratch copies in future waves.
- **S61 (S-h):** `ruff format --check` would reformat `generate.py` and `test_synth.py`. This is pre-existing (S15) and not gated.

### Cross-Task Consistency
- **Schemas ↔ B-09/B-14 ↔ Group 3 tasks.** The `shoot_more`/`invalid` advice contract and the stats-route contract are now both expressible and enforced at the schema layer. `GroupDetailOut` has no stats. `ShotOut` offsets stay nullable for the B-14 service guard.
- **Notes for the lead (not counted).** These are AC-5 paths outside B-09's "advice/stats" scope. Neither is a Group 1 defect, because B-14 assigns producer and service guards to Groups 2–3.
  1. **Zero-check messages.**
     - Where: `ZeroCheckOut.message` and `route` (`schemas.py:993-995`).
     - A 2-shot zero check is a "2–9 shot group outside Step 1" under AC-5's wording, but nothing guards these fields.
     - T2.7's zero-check criterion (tasks.md:211) checks only that mount (#1) is named before scope (#2).
     - Decide either:
       - T2.7 asserts the exact pass/fail strings, as B-14 does for SHOOT_MORE; or
       - the `AdviceOut` message rule is extended to `ZeroCheckOut`.
  2. **Dope rows.**
     - Where: T2.13 and T3.8 (tasks.md:237, :363).
     - `centered_elevation_mil = dialed − center_dy_mil` (`schemas.py:1035-1036`) taken from a finalized `zero_step2`/`zero_confirm` group of fewer than 10 shots, with elevation set, is in effect a numeric correction.
     - Neither task has a purpose filter or a short-zero-group guard.
     - Recommend that T3.8 excludes those groups, with a test.
  3. **T2.7 templates should also be checked through `AdviceOut`.**
     - T2.7's tests should validate every SHOOT_MORE/INVALID template through `AdviceOut`, not only by exact string.
     - The guard also rejects some plausible count wording ("6 left to go", "sample is low (4 of 10)"), and a template that trips it would surface as a 500.
     - [via feature-dev:code-reviewer]
  4. **For T2.18** [via review-6]:
     - `_line_mask` is specific to `GRID_LINE_BGR`.
     - The order test assumes rotation under 45°.
     - Corner labels are in the paper frame, so on a 180° sheet TL is not the image's top-left.
     - Reference seed-0 image sha256 values (first 16 hex chars) for checking "per-seed outputs unchanged":
       - `hires_wipe_decoys` 8eb73f0e46c93235
       - `close_holes_tight_pair` f8ed9a8124875263
       - `black_dot_mixed` 2025969f93cc7e83
       - `clean_grid` 05a27ad4ab653488
       - `ragged_10x7` 216ac1e802459f3a
  5. S47 (`roi_px` containment) is unchanged and owned by T2.18 (B-13).
- **File ownership.** Each F2.x task wrote only its declared files. F2.2 mutated `generate.py` in place but restored it byte-identical.

### Tests
- [x] **All tests passing.**
  - `cd /Users/calebschwarz/Documents/Hunting && .venv/bin/pytest -q`: exit 0, 901 passed, 1 skipped, 902 collected.
    - The skip is `tests/unit/test_scripts_syntax.py:50` `SHELLCHECK: NOT INSTALLED`, declared.
    - The 1 warning is the Starlette httpx deprecation, lead-accepted.
  - `make lint`: exit 0. `ruff check .` passes, `bash -n` passes on every script, and SHELLCHECK is NOT INSTALLED (declared).
  - `./scripts/scan.sh`: exit 0, with network available.
    - `BANDIT: scanned 1708 lines; high=0 medium=0 low=0`
    - `PIP-AUDIT: audited 21 pinned packages; vulnerable packages=0`
  - `bandit -q -c pyproject.toml -r src`: exit 0.
  - Fix-wave Runs re-run; the counts match the completion notes:
    - F2.1 (`tests/unit/test_schemas.py tests/unit/test_templates_shell.py`): 260 passed.
    - F2.3 (`tests/integration/test_schema.py tests/integration/test_domain_db_contract.py`): 120 passed.
    - F2.2 (`tests/unit/test_synth.py`): 132 passed (also re-run by review-6).
  - review-6 also ran the Slice A regression Run (types, units, calibers, layering, transform, synth): 310 passed.
- [x] **Test coverage adequate for the Group 1 gate (T1 + T2).**
  - Every cycle-2 Warning now has a test that fails on its regression.
  - The remaining gaps are configuration or shape variants: S50, S51 and S55–S59.
- **Tier coverage:**
  - The Group 1 gate is T1 + T2 (test-plan §6), and both ran. T3/T4 are not due for Group 1.
  - No live server probe this cycle. The fix wave changed no runtime wiring: `schemas.py` models are not bound to routes until Group 3. This is declared, not passed.
- **No assertions on mocks.** The new tests assert on the pydantic validators, a migrated SQLite DB opened through `connect()`, and rendered pixels.
- **Completion notes and test-evidence match what was reproduced,** with two exceptions in F2.2's notes (S54): the understated "known limit", and the imprecise "square centers" wording. The checks themselves ran.

### Verdict
Verdict: PASS — 0 Critical, 0 Warning, 14 new Suggestion (S48–S61; S54–S61 via review-6). W3-R, W11-R, N1 and N2 are resolved, and the 11 cycle-1 Warnings resolved in cycle 2 show no regression. The Suggestions and the five lead notes in Cross-Task Consistency are for the record and for the Group 2/3 owners; none blocks.

## Group 2 — Cycle 1

Date: 2026-10-01 · Reviewing: Group 2, Core libraries and the detection-accuracy spike (T2.1–T2.19, including lead-added T2.18 and T2.19) · Scope/cycle: Group 2, cycle 1 of 3. This is the first `## Group 2` section; the Group 1 cycles above do not count against it.

Synthesizer: review-7. It reviewed the services layer (T2.14 and the B-18 follow-up), whole-group cross-module consistency, and the B-09/B-14/B-15/B-17/B-18 rule end to end.

Analysts:
- review-8: domain, T2.6–T2.9.
- review-9: db and live server, T2.10–T2.13 and T2.17.
- review-10: vision, T2.1–T2.5, T2.15, T2.16, T2.18 and T2.19.

Coordination: degraded mode (B-01). Ground truth is the disk, the tasks.md Done notes, test-evidence.md and decisions.md. There are no commits and no sentinels, as expected. Lead decisions B-10..B-22 are treated as accepted. They are not re-raised unless the code contradicts them.

### Method

**What was read.** Every slice file was read in full on current disk. Every line cited below was re-read before writing.

**Mutation testing**, on scratch copies only:
- Setup: `/tmp/rv7_scratch`, with the venv symlinked, `PYTHONDONTWRITEBYTECODE=1` and `-p no:cacheprovider`. A probe test confirmed that the scratch copy imports its own `rga` and `tests.support.seed`.
- review-7 ran 54 mutants against `services/groups.py`, `tests/support/seed.py` and `domain/zeroing.py`. The zeroing mutants re-verify review-8's survivors. Result: 38 killed and 16 survived. Two survivors are equivalent on current data (M27 tolerance 0, M30 rifle taken via the load). The rest are cited in the findings.
- Every mutated file was `cmp`-identical to the repo afterwards. The repo was never edited.

**Empirical probes:**
- a deterministic two-connection interleaving on real SQLite (G2-W1);
- a summary rendered with a str purpose (G2-W4);
- the `GroupStatsOut | SizeOnlyStatsOut` union fed real service objects;
- every SHOOT_MORE/INVALID template validated through `AdviceOut` (step2 n=0..9, step1 n=4..12);
- the 937,500×64 PNG and the AV1-as-`heic` inputs (G2-W17, G2-W18);
- the `--only` typo run of the spike tool (G2-W20);
- one T2.19 cue mutant with the real photos hidden (G2-W22);
- an end-to-end vision → service chain on 6 synthetic photos with auto-proposed references (see Cross-Task Consistency).

**Plugin passes on the services slice:**
- `feature-dev:code-reviewer`: static only, because its session had no shell.
- `pr-review-toolkit:silent-failure-hunter`.
- `pr-review-toolkit:pr-test-analyzer`.
- `pr-review-toolkit:type-design-analyzer`.

Their findings were re-verified before merging. The silent-failure-hunter wrote experiment files into review-7's scratch copy, so the scratch copy was rebuilt from the repo before any further mutation run. Each analyst merged its own plugin passes.

**Implementer clarification:** coding-6 confirmed that G2-W3 and G2-W4 are unintended, and that nothing documents a coercion requirement for `SummaryGroup.purpose`. coding-6 has not edited anything.

### Spec Alignment
- **Implemented as specified (services):**
  - The T2.14 contract matches the design build notes: `load_group` returns `GroupBundle(group, session, load, rifle, shots, stats)`, and `recompute_offsets`, `recompute_session_offsets`, `save_review`, `finalize`, `group_stats` and `new_lot_flag` are all present.
  - Plane coordinates are the source of truth. Stored offsets come only from `offset_from_aim` and `in_to_mil` (`groups.py:109-128`). Stats are computed fresh from plane positions (mutant M33 killed).
  - Finalize guards run in the order `reference_unconfirmed` → `no_aim` → `count_mismatch` (`groups.py:285-301`).
  - Rejections are `not_a_bullet_hole` only, append-only, and de-duplicated. The count-based no-silent-drops rule matches the design build notes.
  - B-18: `masks_poi` covers every `zero_check` group and `zero_step2`/`zero_confirm` groups under 10 shots. `zero_step1` and `general` keep their offsets (`groups.py:81-87`).
  - B-16: notes stay byte-identical through save and finalize. `test_layering` is green.
- **Implemented as specified (other slices):**
  - Domain [via review-8]: doctrine checked against the corpus (22CM-FORM-020, MIL-FORM-001/003, LD-07/20/22/26, FB-12, OPT-22). Step 1 corrects from 1–3 shots. Step 2 needs at least 10 shots and uses the mean of all of them. The overlay is center on center and judged on the latest confirm centroid. The touch-up and confirm-again thresholds, the dot rule nearest to ES − d (1.2 → 1.0), the zero-check boundary and the fixed templates are all as decided.
  - DB [via review-9]: parameterized SQL only, EXPLAIN and index plans asserted, and no commit in any repo (grep). B-15/B-17 hold on the default path.
  - Vision [via review-10]:
    - The P-11 thresholds in `tools/eval_detection.py:38-52` equal spec §6.
    - The noisy/clean split loosens nothing.
    - The pipeline never auto-confirms.
    - D-07 holds.
    - AC-9 holds through the real pipeline (7 + 3 estimated, all inside the outline).
    - The T2.18 byte-identity hashes match review-6's references.
    - Ragged placement is deterministic.
    - Import → shots takes 0.94 s at 12 MP.
- **AC-5 must-never (no numeric correction from 2–9 shots outside Step 1):** on every current code path the domain emits SHOOT_MORE/INVALID with `correction=None` and `stats=None`, and every template validates through `AdviceOut`. The multi-confirm path has no test that would catch a regression (G2-W7).
- **B-09/B-14/B-15/B-17/B-18 end to end:** the four layers agree today on the default, enum-typed paths:
  - domain zeroing (fixed templates);
  - services (`SizeOnlyStats` and null per-shot offsets);
  - summaries (masked when the purpose is an enum);
  - `repo_dope` (excludes step1 and check groups, and short step2/confirm groups).

  Three latent leak paths remain:
  - a str purpose in summaries (G2-W4);
  - the `dope_rows(min_zeroing_shots=...)` override (G2-W12);
  - stale stored offsets after a purpose change, where the nulling is untested on both the write side and the read side (G2-W2).

  Aggregate and comparison take no purpose input. B-20 assigns that guard to T3.5 and T3.6.
- **T2.14 criterion "convert domain Advice with full GroupStats to SizeOnlyStatsOut ... Test it":** this is neither implemented nor declared in T2.14. It is vacuous for `groups.py`, which produces no `Advice`, and the domain never attaches stats to SHOOT_MORE/INVALID (verified for every n). The same criterion sits on T3.5 and T3.7 (G2-S8).
- **AC-10 (Group 2 pieces):** finalize requires a confirmed reference when a photo is linked, an aim, and `len(shots) == shots_expected`. Under concurrent requests these guards are not atomic (G2-W1).
- **AC-11:** the new-lot flag is correct on the tested data. Two filters in the query it depends on are untested (G2-W13).
- **D-14:** there is no mean radius or best-N anywhere in `src/` today (grep clean), but the verifier misses common spellings (G2-W9).
- **No outbound network or CDN:** `bandit` is clean and `src/` has no HTTP client. The live-server tests assert a loopback-only listener.
- **Real photos:** not a Group 2 gate (B-19, B-22). The T2.19 numbers (69 of 78 visually counted holes, 9 FP) reproduce exactly [via review-10]. Its justification claims do not (G2-W22).

### Critical
- None.

### Warning

**Services slice and cross-module seams (review-7)**

**G2-W1. The guard reads in `save_review`, `finalize` and `recompute_*` run outside the write transaction, so concurrent requests can bypass AC-10 and the no-silent-drops rule.**
- Where:
  - `src/rga/services/groups.py:243-258`; its write block is `:260-271`.
  - `:282-301`; its write is `:302-306`.
  - `:131-133`.
- Why: Python's sqlite3 runs in legacy isolation mode, so a transaction only opens at the first write statement. Every guard read (status, stored shot count, existing rejections, aim, count, distance) therefore runs in autocommit and can be stale by the time the write starts.
- Reproduced deterministically on real SQLite with two connections (one per request, as `get_db` provides):
  - A concurrent 4-shot save that lands between `finalize`'s count check and its UPDATE leaves the group `final` with **4 of 3 shots**.
  - A concurrent save that raises the stored count from 2 to 5, landing after `save_review`'s count read, lets a 2-shot PUT **drop 3 stored shots with 0 rejections**.
  - [via silent-failure-hunter F1/F2/F6] A group finalized in the gap still gets overwritten. A double-submitted PUT stores the rejection twice. A concurrent distance change leaves stale `dx_mil`.
- Scope: this needs two requests on the same group at once (double-click, or two tabs). It was reproduced by deterministic interleaving, not over HTTP, so the HTTP path is flagged for live validation.
- Fix:
  - Take the write lock before the guard reads: `if not conn.in_transaction: conn.execute("BEGIN IMMEDIATE")` inside the `with conn:`, and run the reads and writes in that one transaction.
  - Add a test that interleaves a second connection and asserts the refusal or serialization.
  - [own; via silent-failure-hunter, which prototyped the fix: 94/94 green, race serialized]

**G2-W2. The B-14/B-18 per-shot offset mask is unproven on both the write side and the read side.**
- Where:
  - Write side: `groups.py:113-119`.
  - Read side: `groups.py:172-175`.
  - Tests: `tests/integration/test_service_groups.py:348-365` and `:412-424`.
- Surviving mutants (94 passed each):
  - N1: skip the NULL write when the mask applies.
  - N3: `_masked` clears only `dx_in` and `dy_mil`.
- Why the tests can't catch them:
  - `replace_shots` already inserts NULLs, so no test moves a group from stored offsets to masked.
  - The "after an aim move" half of `:348-359` cannot fail.
  - The stale-value tests check only two of the four fields.
- Real path: `repo_sessions.update_group(purpose="zero_check")` (purpose is whitelisted) followed by `recompute_offsets`. The zeroing linkage and PATCH in T3.5/T3.7 will use exactly this. Mil offsets for a zero_check group would then stay in the stored columns, guarded only by a read-side mask whose test can't fail either.
- Fix:
  - Seed a general group with an aim and stored offsets, change its purpose to each masked purpose, call `recompute_offsets`, and assert all four columns are NULL from a second connection.
  - In both stale tests, assert all four fields.
  - [own; via pr-test-analyzer F3/F4; type-design M1]

**G2-W3. `SizeOnlyStats.includes_estimated` has no test, and no test reads `SizeOnlyStats` and `SizeOnlyStatsOut` together.**
- Where: `groups.py:59-66` and `:163-168`.
- Evidence: mutant M7 (forcing `includes_estimated=False`) survives all 94 tests. No test reads the field on a `SizeOnlyStats` at all.
- Why: this is a new contract element whose value FR-3.7 and AC-9 require ("includes estimated shots"), yet no test would fail without it (testing-standards rule 5).
- What already holds: the field sets match today. A `SizeOnlyStats` validates as `SizeOnlyStatsOut` through the stats-route union (probe).
- Fix:
  - Assert `includes_estimated is True` for a zero_check group, a short zero_step2 group and an aim-less draft, each containing one estimated shot.
  - Add a parity test, `fields(SizeOnlyStats) == SizeOnlyStatsOut.model_fields - {"es_label"}`, as T2.8 does for `TierSummary`.
  - Validate real `bundle.stats` through `ROUTE_SCHEMAS[("GET","/api/groups/{id}/stats")]`.
  - [own; confirmed by coding-6; via pr-test-analyzer F2; type-design L4]

**G2-W4. A summary prints mil POI with direction words for a zero_check group whose purpose is a str (a B-18 leak path).**
- Where: `src/rga/domain/summaries.py:85` (`group.purpose is GroupPurpose.ZERO_CHECK`). `GroupRow.purpose` is `str` (`src/rga/db/rows.py:128`), and B-16 says the mapper sets `SummaryGroup.purpose` from `groups.purpose`.
- Verified: `SummaryGroup(purpose="zero_check")` with 2 shots prints "Mean point of impact: 0.25 mil high (0.90 in), 0.14 mil right (0.50 in)". The enum masks correctly. A str `"zero_step2"` happens to work, because `:87` uses `in frozenset`.
- Tests: `tests/unit/test_summaries.py` passes enum members only.
- Fix:
  - Compare with `==`, or coerce in `SummaryGroup.__post_init__` (`GroupPurpose(self.purpose)`, which also rejects unknown values).
  - Add tests that pass `"zero_check"`, `"zero_step2"` and `"zero_confirm"` as plain str through `group_summary` and `session_summary`.
  - [own; via review-8 W1, independently verified; type-design H1; confirmed by coding-6]

**G2-W5. Public functions, classes or methods with no docstring.**
- Where:
  - `tools/eval_detection.py`: functions at `:152`, `:161`, `:176`, `:343`, `:412`, `:435`, `:443`, `:639`, `:678` and `:755`, plus the properties `ScenarioResult.passed` (`:339`) and `RealPhotoScore.recall` (`:495`).
  - `tools/eval_real_photos.py`: `main` (`:706`) and `PhotoReport.false_positives` (`:346`).
  - `tests/support/live_server.py`: `HttpResponse.text` and `HttpResponse.json` (`:74`, `:77`).
  - `tools/__init__.py` has no module docstring.
- Why: the tasks.md in-code docs rule, which is a handoff FAIL condition (Group 1 precedent W8). `src/rga` has no gaps (AST scan).
- Fix:
  - Add one-line docstrings.
  - Fix the check: the docstring gate in `tests/unit/test_types.py` covers only `rga.domain`. Extend it to all of `src/`, `tools/` and `tests/support/`.
  - [own; via review-9 W7; review-10 W9]

**Domain slice [via review-8]**

**G2-W6. `step2_confirm_advice` judges only the initial and latest groups.**
- Where: `src/rga/domain/zeroing.py:143-155`; the crash is in `_centered` at `:122-125`.
- Problems:
  - An empty middle confirm (`confirms=[[], <10 shots>]`) raises `ZeroDivisionError`. That is a 500, not INVALID.
  - A short middle confirm enters the overlay. Reproduced: `[3-shot, 10-shot]` → READY_FOR_DOT with n=23. Centering a 3-shot group shrinks the cone ES that `suggest_dot` uses.
  - Reproduced: a 15-shot initial group plus a 5-shot and a 10-shot confirm (10+5+15 in review-8's terms) prints "The 30-shot overlay gives very high confidence".
  - Spec §4.6 makes every confirm a group of 10 or more shots.
- Fix:
  - Require every confirm group to have at least 10 shots, returning INVALID from a fixed integer-count template.
  - Count only full groups toward the 30-shot label.
  - Test the empty and short middle cases.

**G2-W7. The AC-5 must-never guard on the multi-confirm path has no test.**
- Where: `tests/unit/test_zeroing.py:295-305`.
- Evidence: mutant Z33 checks `len(groups[1])` but still judges `groups[-1]`. It survives (278 passed, reproduced by review-7). With it, a 1–9-shot latest confirm yields a numeric correction.
- Fix: for `confirms=[full 10, k-shot]` with k in 1..9, assert SHOOT_MORE, the exact "k of 10" template, `correction is None` and `stats is None`.

**G2-W8. Advice kind and message are under-asserted in Step 1 and Step 2 initial.**
- Where: `zeroing.py:80-82` and `:178`; tests at `tests/unit/test_zeroing.py:196-205` and `:217`.
- Surviving mutants (Z03 and Z30 reproduced by review-7):
  - Z03: the no-adjust test uses `or` instead of `and`.
  - Z30: any correction of 0.1 mil or less is treated as NO_ADJUSTMENT.
  - Z37 and Z39: the correction text is dropped from the CORRECTION and TOUCH_UP messages.
- Effect: the user would read "No adjustment needed." for a 0.3 mil one-axis group, or for a 0.1 mil group. MIL-FORM-003 dials exactly 0.1.
- Fix:
  - Parametrize elevation-only, windage-only and 0.1-mil cases for step1 (1 and 3 shots) and step2 initial (10 shots), asserting the kind, the exact message and the `Correction`.
  - At `:217`, assert that the kind is CORRECTION exactly when either axis is non-zero.

**G2-W9. The D-14 verifier misses common spellings.**
- Where: `tests/unit/test_no_forbidden_metrics.py:9-13`.
- Evidence (reproduced): these are not flagged: `<th>Mean radius</th>`, `mean-radius`, `meanRadiusIn`, `Best-of-3`, `best 5 of 10`, `group reduction`.
- Why: Group 3 adds the templates and JS where such labels would appear, and a verifier that cannot catch them is a silent gap (the same class as Group 1 W4).
- Fix:
  - Use `mean[\s_-]?radius` with no suffix lookahead, `best[\s_-]?(?:of|n\b|\d)` and `group[\s_-]?reduc`.
  - Add each string above as a plant.

**DB and live-server slice [via review-9]**

**G2-W10. Group link consistency is enforced on only one of three write paths.**
- Where: `src/rga/db/repo_sessions.py`:
  - `:61-78`, the `_GROUP_FIELDS` whitelist;
  - `:286-329`, `create_group`;
  - `:369-383`, `update_group`;
  - `:46-60`, `_SESSION_FIELDS`, which includes `rifle_id`.
- What happens:
  - `repo_zeroing.link_group` checks rifle, load and lot.
  - `create_group(zero_id=...)` and `update_group(zero_id=...)` write the link unchecked. Probe: a zero of rifle 2 was attached to a rifle-1 group.
  - `create_group` also accepts a load from another rifle and a lot from another load.
  - `update_session(rifle_id=...)` moves a session out from under its groups.
- Reachability: through `GroupCreate.load_id`/`zero_id` and `GroupUpdate.zero_id`. The T3.2 acceptance criteria don't mention it. review-7's mutant M30 (taking the rifle from `load.rifle_id`) also survives the service suite.
- Fix: either use an EXISTS-guarded INSERT/UPDATE with a typed error mapped to 409, or remove `zero_id` from `create_group` and `_GROUP_FIELDS` so links go only through `link_group`. Drop `rifle_id` from the session whitelist. Add negative tests. If the lead prefers service-level enforcement, add explicit T3.2/T3.7 acceptance criteria instead and record that decision.

**G2-W11. `update_zero` and `create_zero_check` skip checks that `create_zero` and `link_group` make.**
- Where: `src/rga/db/repo_zeroing.py:18-27`, `:137-157` and `:201-217`.
- What happens:
  - `update_zero(lot_id=<a lot of another load>)` is accepted, while `create_zero` rejects the same input.
  - `create_zero_check` accepts a group that isn't linked to the zero, including one from another rifle, of any purpose, at 300 yd.
- Fix: guard or remove `lot_id` in `update_zero`, and EXISTS-guard `create_zero_check` (the group must be linked to this zero with purpose zero_check). Alternatively, add a T3.7 acceptance criterion and a test.

**G2-W12. The `dope_rows` override can switch off B-15.**
- Where: `src/rga/db/repo_dope.py:29-39`; test `tests/integration/test_repo_dope.py:219-223`.
- What happens: `min_zeroing_shots=0..9` brings back short step2/confirm groups, and T3.8 would derive `center_dy_mil` from them. That is a latent must-never path, and the test explicitly allows lowering the bar.
- Fix: remove the parameter, or raise `ValueError` below `MIN_ZEROING_SHOTS`. Replace the override test with one that shows the bar cannot be lowered.

**G2-W13. The `finalized_before` branch of `load_has_other_lot_groups` has untested filters, so AC-11 is unproven.**
- Where: `repo_sessions.py:411-416`; tests `tests/integration/test_repo_sessions.py:863-887`.
- Evidence:
  - Dropping `load_id = ?` (S22) or `lot_id <> ?` (S23) survives the repo, service, dope and zeroing suites.
  - With S22, the first group of a brand-new load is flagged "new lot" whenever another load has an earlier finalized group.
  - The service test data never puts an other-load group earlier (`test_service_groups.py:837`).
  - review-7's M16 shows the draft branch is similarly unpinned (G2-S1).
- Fix: in both suites, add an earlier other-load group and an earlier same-lot group (expect False), then a same-load other-lot group (expect True).

**G2-W14. `repo_sessions` has no never-commit guard.**
- Where: the whole of `repo_sessions.py`; `test_repo_sessions.py`.
- Evidence: adding `conn.commit()` to `create_group` (S24) or `update_group` (S25) survives. `repo_rifles` and `repo_zeroing` have an AST scan and rollback tests. `repo_sessions` tests only `replace_shots`.
- Why it matters: G2-W1's fix and every service transaction rely on this contract (design §4.6).
- Fix: add the same AST scan (commit/rollback/executescript; COMMIT/END/ROLLBACK except `ROLLBACK TO replace_shots`) and a behavioral rollback test over every write function.

**G2-W15. Written columns are compared DB-to-DB, so a column swap goes undetected, and the evidence claims full round trips.**
- Where: `repo_sessions.py:215-229`, `:244-278` and `:286-329`; tests `test_repo_sessions.py:258-317` and `:376-387`.
- Surviving swaps:
  - `create_photo`: width_px and height_px.
  - `update_photo_reference`: working_w and working_h, and the two JSON columns.
  - `create_group`: aim_x_in and aim_y_in.
- The evidence says "round trips ... for every public function" (test-evidence.md:332).
- Fix: assert against the input values, using a distinct value for every column.

**G2-W16. The T2.17 residue check was blind, and its Done note and evidence claim a verification it did not perform.**
- Where: `test-evidence.md:265` and `:281`; tasks.md T2.17 Done note.
- The claim: "0 `python -m rga` processes (pgrep)" after every run, and "the mutations left no processes".
- Why the check was blind: on this machine `pgrep -f "python -m rga"` matches nothing, because the framework binary is `.../MacOS/Python`. review-7 confirmed it returned 0 while a live `-m rga` server was running.
- What was missed: three orphan servers from the T2.17 window (PIDs 33445/33516/33755, PPID 1, started 01:20–01:21) were still listening on loopback at about 10:00. review-7 saw them in `ps` and `lsof`. The lead then stopped them (B-21).
- The declared limitation names only SIGKILL. SIGTERM to pytest also skips the fixture teardown.
- Fix: correct the evidence. T3.9's teardown check must use the recorded PIDs and process groups plus `lsof -iTCP -sTCP:LISTEN`, not that name pattern. Declare the SIGTERM case, or handle it.

**Vision slice [via review-10]**

**G2-W17. Two hostile inputs escape the `ImageRejected` contract, so they would return a 500 instead of a 415.**
- Where: `src/rga/vision/io.py:26-34` and `:92`.
- The two inputs:
  - (a) EXIF Orientation=6 with XResolution typed as ASCII: `exif_transpose` raises `TypeError`.
  - (b) An AV1-coded HEIF whose ftyp major brand is `heic`: it passes the brand sniff, then `pillow_heif` raises `RuntimeError`. review-7 reproduced (b).
- Both fail closed: nothing is stored, and the 500 body is generic.
- Fix: catch `TypeError`/`AttributeError`/`KeyError` around `exif_transpose` (or rotate with `img.transpose` by the tag), map the `pillow_heif` `RuntimeError` to `ImageRejected`, and add one in-test case per trigger.

**G2-W18. Extreme aspect ratios crash both reference detectors.**
- Where: `src/rga/vision/reference_circle.py:525-530` and `src/rga/vision/reference_grid.py:420-425`.
- Evidence: a 937,500×64 PNG (exactly 60 MP, 175 KB) passes every io cap. `propose_candidates` then raises `cv2.error` for both `circle_1in` and `grid_1in` (reproduced by review-7). The docstring promises `ValueError`.
- Fix: reject in `load_oriented` when the minimum side is below 64 or the aspect ratio exceeds about 20:1 (or clamp the resize to at least 1 px and raise `ValueError`), and add a test.

**G2-W19. The real-photo gate tool can pass vacuously, and one of its tests cannot fail.**
- Where:
  - `tools/eval_real_photos.py:383-404`, `:416-427`, `:569-574` and `:614`.
  - `tests/integration/test_eval_real_photos.py:152-154` and `:724`.
- Problems:
  - (b) When no truth file has a `reference`, which is the README's minimal format, the scale row is skipped silently and the run passes with `scale_error_photos=0`.
  - (c) `assert run(...) in (EXIT_PASS, EXIT_FAIL)` can never go red on a failing gate.
  - (W8) `s["holes"] == sum(... and 0 ...) + s["holes"]` reduces to `x == 0 + x`, so the pooled hole count is never checked.
- Not part of this Warning: (a), the detector-defined ragged exemption, implements B-21(1) as written and is a lead note (Cross-Task Consistency item 7).
- Fix:
  - Fail, or mark "not evaluated", any row with zero evaluated photos.
  - Assert EXIT_PASS once at least 10 photos are scored (non-provisional).
  - Assert the real expected pooled total.
  - [via review-10 W3/W8; silent-failure-hunter F4/F6/F7]

**G2-W20. The spike-gate tool exits 0 after evaluating nothing.**
- Where: `tools/eval_detection.py:343-383`, `:412-440` and `:768-786`.
- Evidence:
  - `--only clean_gird` prints "0/0 scenarios pass" and exits 0 (reproduced by review-7, reading the real exit code).
  - `--seeds 4-0` scores zero renders and reports "1/1 scenarios pass".
  - The official Run is unaffected: 23 scenarios × 5 seeds, verified.
- Fix: fail on an unknown `--only` name, an empty seed range or zero scores, and add a test of `main()`.

**G2-W21. `spike-report.md` states fixed conclusions regardless of the computed verdict.**
- Where: `tools/eval_detection.py:731-752` (`STATIC_FINDINGS`, `REAL_PHOTO_RECOMMENDATION`), emitted unconditionally at `:709` and `:726`.
- The fixed sentences: "seeds 5-9 pass 23/23", "The synthetic P-11 gate is unchanged and still passes on all 23 scenarios" and "No B-08 escalation: the synthetic gate is met".
- Why it matters: a FAIL run would print FAIL in its verdict line and "gate is met / no escalation" further down. B-08 escalation keys off this report. The constants are also rationale prose embedded in code.
- Fix: compute these sentences from the results, or move the narrative to a hand-written, dated section outside the generator. [via review-10 W5; own observation; verified on disk]

**G2-W22. Several T2.19 cues rest on skipping real-photo tests, and the report and Done note overstate how they were justified.**
- Where:
  - `src/rga/vision/detect.py:233-243` (structure removal), `:76` (`max_pair_axis_ratio`), `:86` (`tan_max_a1`), `:46` (`min_area_a1`), `:87`, `:38` and `:53`/`:548`.
  - `spike-report.md:131-132`.
  - The tasks.md T2.19 Done note.
- What survives: six cue mutants pass the full synthetic gate and every synthetic and drawn test. They are pinned only by `test_detection_real_photos`, which skips whenever `data/real_photos` is absent, and that directory is gitignored. review-7 reproduced `max_pair_axis_ratio` 2.6→99 with the photos hidden: 260 passed, 17 skipped.
- Single-photo cues: structure removal, the pair-axis cap and `tan_max_a1` each move one photo only. That breaks T2.19's hard constraint ("justified by more than one photo or by a synthetic trait").
- Claims not borne out:
  - The report cites synthetic justification for structure removal (black-dot/diamond scenarios), the pair-axis cap (`black_grid_light_holes_qr`) and the area floor (text/QR). The suite does not support any of these.
  - `test_hole_touching_a_large_black_diamond_separates_from_it` passes with structure removal off.
  - `cluster_core_r` at 9.0 does change real_04, although the evidence says "no observable effect".
- Why it matters: the Done note says "each justified across several photos plus synthetic", which is a completion claim that was not verified. B-22 accepted the result numbers, not this claim.
- Fix:
  - Add a drawn or synthetic case that fails without each cue: a hole overlapping a broad dark region, a 0.4–0.5 A1 round mark, a letter pair that must not split, a 1.5 A1 tan blob.
  - Drop the single-photo cues, or justify them.
  - Test or remove `cluster_core_r`.
  - Correct the report and Done-note wording.

**G2-W23. The AC-9 guarantee that every estimated shot is 100 % inside the outline rests on an untested line.**
- Where: `src/rga/vision/ragged.py:193`; tests `tests/unit/test_ragged.py:284-296`.
- Evidence:
  - Removing the initial `_project` of the kmeans seeds survives all 172 ragged and pipeline tests.
  - With that mutant, a C-shaped outline with count=1 places the shot 48 px outside the outline.
  - Loosening `_inside` (`:226-227`) to −6 px also survives.
  - The case can't be reached today: the detector emits only ragged blobs with solidity 0.8 or more.
- Fix: add a count=1 test on a ring or C-shaped outline asserting `pointPolygonTest ≥ 0`.

### Suggestion

Services slice (review-7 and plugin passes):
- **G2-S1. Smaller untested paths in `groups.py`, none on a must-never guard.**
  - M16: the draft new-lot "lot already finalized" check (`:326-327`) is unpinned.
  - M19: rejections are dropped from the finite check (`:249`), although the `invalid_coordinates` message names them. The API bounds block NaN.
  - M27: the dedupe tolerance boundary is untested.
  - N2: the dedupe `and`→`or` (`:206-208`) survives.
  - M36: the `recompute_offsets` commit (`:141`) survives, because the "committed" test also calls the session recompute.
  - `finalize`'s offset writes are not checked from a second connection [via pr-test-analyzer F6].
- **G2-S2. Seed-helper gaps** (`tests/support/seed.py`). Group 3 and 4 rely on these helpers.
  - These parameters are ignored without any test failing: `bullet_diameter_in`, `suppressor`, `kind`, a custom `aim` in the plane math (`:158`), `roi_json`, name, label, date, position, `sha256` and extra fields. The commits in `seed_rifle`, `seed_load`, `seed_session` and `seed_photo` can be removed the same way.
  - `seed_group` commits three times, so a `no_aim` failure leaves a committed draft.
  - `shots_expected or ...` turns an explicit 0 into `len(offsets)`.
  - `seed_photo` builds its default hash from a row count, so it can collide with an explicit hash.
  - One pass-through test covers most of this.
  - [own S12–S15; via pr-test-analyzer F7–F9; silent-failure-hunter F11–F13]
- **G2-S3. `group_stats` returns `None` for a group with no shots, but the stats route schema is `GroupStatsOut | SizeOnlyStatsOut` with no `None` member** (`groups.py:155-156`, `schemas.py:1095`). A route that follows the service literally would return a 500. The lead decides the mapping in T3.5. [via pr-test-analyzer F1]
- **G2-S4. Type-level hardening of the masking invariant:**
  - `masks_poi` returns False (fail-open) for unknown or mis-cased strings. Parse with `GroupPurpose(...)`.
  - `GroupBundle` has no `__post_init__` invariants (stats None exactly when shots are empty, `n == len(shots)`, no `GroupStats` when masked) and holds a mutable list.
  - `GroupError` codes are free-form strings.
  - The `is_short_zero_group` alias name is misleading after B-18.
  - The `ShotOut` docstring omits masking.
  - [via type-design M2, L1, L3, L5]
- **G2-S5. The masking rule exists in three copies:**
  - `groups.py:23-24` and `:81-87` use `STEP2_MIN_SHOTS`;
  - `summaries.py:13-14` and `:84-89` use their own `10`, which nothing ties to the others;
  - `repo_dope.py:22-24` uses SQL literals that are asserted.

  Put one `masks_poi` and one constant in `rga.domain` and reuse them everywhere. Add a two-sided test that `masks_poi(p, n)` agrees with whether a summary omits its POI, for every purpose as both enum and str. [own; via review-8 S1 and cross-slice item 3; type-design M4]
- **G2-S6. `SummaryGroup.stats: GroupStats` forces any mapper to compute a full POI for masked groups** (`summaries.py:39`). An aim-less general group with `SizeOnlyStats` raises `AttributeError` at `:71`. Move `SizeOnlyStats` to the domain and widen the annotation. [via type-design H2; review-8 cross-slice item 2]
- **G2-S7. Error-path details** [via silent-failure-hunter F3, F4, F5, F7, F8]:
  - With extreme coordinates (±1e308, blocked by the API's ±1000 bound), `save_review` and `finalize` raise after commit, which contradicts "On any error nothing is changed".
  - A missing parent row is reported as `group_not_found` or `reference_unconfirmed`.
  - `with conn:` commits or rolls back the caller's pending work.
  - Raw `OverflowError`/`TypeError` are possible for direct callers.
  - `recompute_session_offsets` does nothing for an unknown id.
- **G2-S8. T2.14's "convert Advice ... Test it" criterion is vacuous at T2.14** (see Spec Alignment). The lead should record that T3.7 owns it.
- **G2-S9. Short rationale prose** (Group 1 S45/S52 treatment):
  - `tests/integration/test_service_groups.py:155` ("a final group cannot be reviewed again, so ...");
  - `tests/support/seed.py:6` ("so every derived value comes from the same code the app uses");
  - `repo_dope.py:36-37`, `repo_rifles.py:6`/`:80` and `repo_zeroing.py:6`/`:88` [via review-9 S1];
  - `comparison.py:4-6`, `zeroing.py:138-140` and `summaries.py:1` [via review-8];
  - `reference_circle` docstrings, `detect._without_printed_structures` and the `tests/real/README.md` intro [via review-10 S13].

  [own; via feature-dev:code-reviewer]
- **G2-S10. Two-sided parity tests are missing for two more seams** that currently agree (verified): `ragged.Status` against `schemas.DetectStatus`, and `stats.AggregateStats` against `AggregateOut`. Add them the way T2.8 did for its literals.

Domain slice [via review-8]:
- **G2-S11.** `stats.py:105-107`: mutant S03 survives (aggregate center computed as the mean of group means). Every center test uses equal-size groups; add a 10+3 case.
- **G2-S12.** `zeroing.py:164` hard-codes "30-shot" when the overlay can hold 35 or more shots.
- **G2-S13.** `step1_advice` with 10 or more shots returns SHOOT_MORE ("use Step 2"). Consider INVALID, or document it for the Group 3 UI.
- **G2-S14.** `zero_check` fails closed on NaN offsets while `group_stats` raises. Align them.
- **G2-S15.** Note only: the verdict is inclusive at 2.0 MOA (spec §4.5) where LD-07 says "sub-2"; notes are whitespace-collapsed (asserted; within B-16).
- **G2-S16 (review-8 W6, re-rated Suggestion by the synthesizer).** test-plan.md:28 and design §3 call for a T2 test from the `high_right_group` render to the "DOWN … LEFT" correction text, and that chained test doesn't exist. Both halves are guarded: render to signs in `test_coordinate_contract.py:80-97`, and signs to DOWN/LEFT in `test_zeroing`. A flip on either side goes red, so no defect class escapes and no Done note claims the chain. Add the chained assertion or amend the plan.

DB and live-server slice [via review-9]:
- **G2-S17.** `live_server.py:264-278`: `mkdtemp` and `Popen` run before the cleanup `try`, so a missing or non-executable `run.sh` leaks the temp dir. This contradicts the evidence's "cleans up its own attempt".
- **G2-S18.** `live_server.py:198-223`: `stop()` doesn't report a server that exited early. Separately, early-exit detection is untested (`test_live_server.py:255-262`).
- **G2-S19.** `test_live_server.py:144-152`: the "passes the guards" test accepts a 404 or a 500. Assert `status < 500` and a non-guard code.
- **G2-S20.** `live_server.py:180`: a str body is latin-1-encoded, so a body containing "€" raises. Encode as UTF-8 before Group 3/4 post notes.
- **G2-S21.** `test_live_server.py:158-164`: the setter-side env contract reads the recorded env, not the child's real environment.
- **G2-S22.** Other `live_server.py` edge paths: the health check accepts any non-5xx; the port probe treats any `OSError` as closed; there is a `killpg` PermissionError case and a dropped retry cleanup note.
- **G2-S23.** `repo_sessions.py:485-488`: when SQLite itself aborts the transaction, `ROLLBACK TO replace_shots` raises "no such savepoint" and masks the real error.
- **G2-S24.** `repo_sessions.py:552-584`: `search_notes` is case-insensitive for ASCII only, its snippet offsets drift on `casefold`, and several of its mutants survive.
- **G2-S25.** `repo_zeroing.py`:
  - `active_zero` prefers an in-progress zero over the complete one (by design). T3.7 must read the dot from the complete zero.
  - Mutants Z5 and Z8 survive.
  - `supersede_zeros` doesn't check that `except_id` belongs to the same rifle and load.
- **G2-S26.** `repo_rifles.py:267-270`: lot codes aren't stripped, so "A1 " creates a second lot and a false new-lot flag. Strip at the schema.
- **G2-S27.** `repo_sessions.py:61-78`: nothing pins the immutability of group `created_at` and `id`. `update_group(status='final')` without `finalized_at` is allowed.

Vision slice [via review-10]:
- **G2-S28.** `detect.py:155-164`: the ROI filter tests only `centers_px[0]`, so a split blob straddling the ROI edge brings a center outside it.
- **G2-S29.** The `cv2.setRNGSeed` global side effect in `reference_circle.py:316` isn't recorded in the design build notes (the one in ragged is).
- **G2-S30.** `test_pipeline.py:48-54, 69`: the `tape_cross_multi_aim` xfail is `strict=False` and covers the whole scenario. Use `strict=True` and xfail only group 3.
- **G2-S31.** Nothing pins the render hashes of the 17 old synthetic scenarios; byte-identity was checked once. Add a hash-pin test gated on the cv2 version.
- **G2-S32.** `tests/real/README.md:11-12` says `counts.json` is used only by `--coarse`.
- **G2-S33.** `tests/real/counts.json` `meta.baseline.before` (47 found / 33 FP) can't be reproduced and differs from B-19's T2.4 figures. Say it was rescored with T2.19's lists.
- **G2-S34.** `test_detection_real_photos.py:25-31`: one missing photo skips all 17 tests, and the `name.*` glob can pick up `.aae` or `.heic` files.
- **G2-S35.** One bad photo aborts `eval_real_photos` with exit 1, and the `cv2.imwrite` return value is ignored.
- **G2-S36.** `io.py:85-87`: `warnings.catch_warnings` is process-global. This is benign, because the explicit pixel check is the real control.
- **G2-S37.** Two items for the record: `hires_wipe_decoys` sits exactly at its FP budget (3 of 3); and auto-grid rms up to 0.05 in gives no warning (B-19 deviation; real_05 is at 0.039 in).

Bookkeeping:
- **G2-S38.** In tasks.md the T2.4 Done note sits under T2.14, and the T2.15 and T2.16 notes sit away from their tasks. The T2.16 note still says 2 xfails (it is now 1; the follow-up evidence at test-evidence.md:484-486 is correct).
- **G2-S39.** B-18 deliberately keeps POI on `zero_step1` groups. A 4–9-shot Step 1 group therefore shows mil POI beside a SHOOT_MORE advice. That is lead-accepted, so this is a UI note for T3.7 only.

### Cross-Task Consistency
- **Vision → services chain (probe).**
  - Setup: for 6 synthetic photos (circle 1 in, circle 3 in, ragged_10x7, skew10_grid, high_right_group and merged_pairs), the chain ran: render → `propose_candidates` (auto) → truth-nearest candidate → `build_working_image` → `analyze_group` → `shots_from_resolve` → `services.groups.save_review` with the truth aim mapped through `h_px_to_working` → `finalize`.
  - Result: every group came back `ok` with the expected n. |ΔES| ≤ 0.017 in and |Δmean POI| ≤ 0.011 mil against truth, inside the AC-1 tolerances (0.05 in, 0.02 mil). ragged_10x7 gave 3 estimated shots. Confidence values are plain `float` (sqlite-safe).
  - Coordinate frames agree across the chain: working-plane inches for both shots and aim, y-down storage, and a single y-flip in `offset_from_aim`.
- **Domain/service types against the schemas.**
  - Field sets agree today:
    - `GroupStats` ⊂ `GroupStatsOut` (`es_label` and `poi_text` default);
    - `SizeOnlyStats` = `SizeOnlyStatsOut` minus `es_label`;
    - `TierSummary` = `TierSummaryOut` (asserted by T2.8);
    - `Correction` = `CorrectionOut`;
    - `AggregateStats` ⊂ `AggregateOut`.
  - `ragged.Status` equals `DetectStatus`.
  - Only T2.7 (templates through `AdviceOut`/`ZeroCheckOut`) and T2.8 assert both sides; see G2-W3 and G2-S10 for the rest.
  - `GroupStatsOut.poi_text` has no producer yet. The Group 3 route must build it. `summaries._poi_text` is private.
- **services ↔ db conventions:**
  - Repos never commit (grep). Services commit with `with conn:`.
  - `replace_shots` opens its own BEGIN when needed and uses a savepoint, so `save_review`'s writes are atomic. The guard reads are not (G2-W1).
  - `repo_dope`'s threshold is asserted equal to `STEP2_MIN_SHOTS`; the summaries threshold is not (G2-S5).
- **Notes for the lead and the Group 3 owners (not counted as findings):**
  1. B-20 already covers aggregate and compare. The services must use `masks_poi`. Note that a masked `ShotRow` still carries `x_in`/`y_in`, so a caller that re-derives offsets with `offset_from_aim` bypasses the per-shot mask [review-8].
  2. A PATCH that changes purpose or session (`GroupUpdate.purpose`, a session distance or rifle change) must go through a service that recomputes offsets in the same transaction. A `groups.set_purpose` would make that explicit (G2-W2) [type-design M1].
  3. The `GET /api/groups/{id}/stats` route must map `None` for a group with no shots (G2-S3).
  4. Build `GroupDetailOut.shots` from `bundle.shots` (masked), never from `repo_sessions.list_shots` [pr-test-analyzer F12].
  5. The stats on confirm-advice (TOUCH_UP_OK, CONFIRM_AGAIN, READY_FOR_DOT) describe the center-on-center overlay, whose mean is about 0. Don't present it as the zero's POI; the judged centroid is in `advice.correction` [review-8].
  6. T3.7's zero check reads the dot from the complete zero, not from `active_zero` (G2-S25). The T3.9 teardown check must use PIDs and process groups (G2-W16).
  7. B-21(1) as written ("inside one detected ragged blob") lets the detector decide which truth holes are exempt from recall. review-10 reproduced four distinct holes 1.2 d apart, read as ragged and fully exempt, giving PASS. Consider a truth-side exemption (annotated ragged indices, or members less than 0.6 d apart) and capping the exempt fraction. This is a lead decision, not a finding against T2.15.
  8. If link consistency (G2-W10, G2-W11) stays in services, T3.2 and T3.7 need explicit acceptance criteria and negative tests.
  9. The T3.3 routes should be able to rely on `ImageRejected` alone. G2-W17 and G2-W18 belong in `io.py` and the detectors, not in route catch-alls [review-10].
- **File ownership:** each Group 2 task wrote only its declared files. T2.19 also touched `tools/eval_detection.py` (lead-approved in tasks.md). No task edited `conftest.py`, `pyproject.toml` or the `Makefile`.

### Tests
- [x] **All tests passing.**
  - `cd /Users/calebschwarz/Documents/Hunting && .venv/bin/pytest -q`: exit 0. 2619 passed, 2 skipped and 1 xfailed in 249.7 s.
    - Skip 1, declared: `tests/unit/test_scripts_syntax.py:50` `SHELLCHECK: NOT INSTALLED`.
    - Skip 2, declared: `tests/integration/test_eval_real_photos.py:721` "real-photo gate outstanding: no annotated photos" (B-19/B-21).
    - The xfail is `tape_cross_multi_aim` group 3, with its reason recorded (G2-S30).
    - The single warning is the Starlette httpx deprecation, lead-accepted.
  - `make lint`: exit 0. `ruff check .` passes, `bash -n` passes on every script, and shellcheck is NOT INSTALLED (declared).
  - `./scripts/scan.sh`: exit 0. `BANDIT: scanned 5559 lines; high=0 medium=0 low=0` and `PIP-AUDIT: audited 21 pinned packages; vulnerable packages=0`.
  - The spike gate, T2.4's Run: `.venv/bin/python -m tools.eval_detection --catalog tests/synth/catalog.json` → exit 0, "23/23 scenarios pass; failing: none". Then `.venv/bin/pytest tests/integration/test_detection_accuracy.py -q` → exit 0, 79 passed.
    - Deviation: `--report` pointed at `/tmp/rv7_spike_report.md`, so the committed `spike-report.md` was not regenerated. It was cmp-verified untouched. Its metrics rows match the fresh run except for runtimes. review-10 ran the same gate independently with the same result.
  - Every task's Run was re-run by its slice reviewer and matches its Done-note count: T2.1 58, T2.2 275, T2.3 125, T2.4+T2.19 105, T2.5 48, T2.6 130, T2.7 103, T2.8 94, T2.9 57, T2.10 90, T2.11 74, T2.12 99, T2.13 62, T2.14 94, T2.15 71 + 1 skipped, T2.16 124 + 1 xfailed, T2.17 22, T2.18 219.
- [ ] **Test coverage adequate.** Not yet:
  - Several guards and contract elements have no test that fails without them: G2-W2, W3, W7, W8, W9, W13, W14, W15 and W23.
  - Three verifiers can pass vacuously: G2-W19, W20 and W21.
  - The T2.19 cues are pinned only by a suite that skips without the gitignored photos (G2-W22).
- **Tier coverage.**
  - The Group 2 gate is T1 + T2 + accuracy (test-plan §6), and all of it ran.
  - T3 (`smoke.sh`) is due in Group 3.
  - `tests/support/live_server.py` is the local T3 analogue (real `run.sh` process, loopback socket) and is green.
  - The real-photo gate is outstanding and declared (B-19, B-21), with no claim of a pass.
- **No assertions on mocks.** The only monkeypatches inject faults (the service rollback test, and the io pixel cap), and every assertion reads real SQLite state or real pixels.
- **Completion notes against what was reproduced.** Mismatches:
  - G2-W16: the T2.17 residue check.
  - G2-W22: the T2.19 justification claim.
  - G2-W15: the T2.11 round-trip claim.
  - G2-S17: the T2.17 "cleans up its own attempt" claim.
  - G2-S38: the T2.16 xfail count.

  The real-photo numbers in the T2.19 note (69 of 78, 9 FP) reproduce exactly [via review-10].

### Verdict
Verdict: FAIL — 0 Critical, 23 Warning (G2-W1–W23), 39 Suggestion (G2-S1–S39).
Reason: the 23 Warnings fall into five groups:
- guards that are not atomic, or that have no test failing without them (G2-W1–W3, W7–W9, W13–W15, W23);
- latent B-18/B-15 leak paths (G2-W4, W12);
- unchecked link consistency (G2-W10, W11);
- error-contract escapes on hostile images (G2-W17, W18);
- verifier and completion-claim gaps (G2-W5, W16, W19–W22).

There are no Critical findings. This is cycle 1 of 3, so one scoped fix wave and Group 2 cycle 2 follow.

## Group 2 — Cycle 2

Date: 2026-10-01 · Reviewing: Group 2 after fix wave FX2.1–FX2.7 (B-23), re-verifying G2-W1–W23 · Scope/cycle: Group 2, cycle 2 of 3 (B-07). Cycle 3 is terminal for this group.

Synthesizer: review-11. It reviewed W1–W4 (FX2.1: `services/groups.py`, `domain/summaries.py` and their tests), the cross-module consistency of the whole group, and a regression pass over the Group 2 seams.

Analysts (all three reported):
- review-12: domain, W6–W9.
- review-13: db and live server, W10–W16.
- review-14: vision and tools, W5 and W17–W23.

Coordination: degraded mode (B-01). Ground truth is the disk, the tasks.md Done notes and lead corrections, test-evidence.md and decisions.md. B-20..B-25 are treated as decided and are not re-raised. The cycle-1 Suggestions G2-S1–S39 stay deferred (B-23) and are not repeated here.

PASS bar (lead handoff): zero Critical and zero Warning.

### Method
- **What was read.** Every slice file was read on current disk, and every line cited below was re-read before writing. For W1–W4, FX2.1 was diffed against the cycle-1 snapshot in `/tmp/rv7_scratch`. The `src/` diff is exactly `_write_transaction`, its four call sites, and `is`→`==` at `summaries.py:85`.
- **Scratch copy.** All mutation runs used `/tmp/rv11_scratch`, with the venv symlinked, `PYTHONDONTWRITEBYTECODE=1` and `-p no:cacheprovider`. A probe confirmed the copy imports its own `rga`, `rga.services.groups` and `tests.support.seed`. After every mutant the harness restored the file and checked it with `cmp` against the repo. The repo was never edited. Artifacts are indexed in `/tmp/rv11_tools/README.txt`.
- **review-11 mutants (49 distinct, plus re-runs):**
  - The FX2.1 set (34): all killed. The 11 W1 transaction mutants were re-run against the interleaving tests alone, with the trace test deselected, and all were still killed.
  - The split-transaction set (6): 5 survive (G2-W1-R).
  - Re-checks of review-12's survivors (5) and of review-13's and review-14's survivors (4): every claimed survivor reproduced.
- **Empirical probes (review-11):**
  - A two-thread race on production `connect()` connections with the default 5 s busy timeout. A save that lands during finalize's count check waits, then is refused with `group_final`, and the group stays final with 3 of 3 shots. A double-submitted rejection is stored once. The probe goes red on the pre-fix behaviour (mutants W1-a and W1-b).
  - A rival write fired in the gap between the last guard read and the write (G2-W1-R).
  - An end-to-end chain on 13 synthetic groups × 3 purposes (39 rows): PNG bytes → `io.load_oriented` → `propose_candidates` → `build_working_image` → `analyze_group` → `save_review` → `finalize`.
  - Reproductions of W17-R (EXIF) and W11-R (update_zero).
- **Plugin passes on the W1–W4 slice:**
  - `feature-dev:code-reviewer`: static only, no findings at ≥ 80 %.
  - `pr-review-toolkit:silent-failure-hunter`: 12 probes in its own copy, no Critical or Warning.
  - `pr-review-toolkit:pr-test-analyzer`: one Warning, which review-11 reproduced independently (G2-W1-R).
- **Analyst totals:**
  - review-12: 53 mutants.
  - review-13: 89 mutants, plus the orphan, signal and load scenarios.
  - review-14: 78 mutants, plus about 35,000 structured EXIF fuzz cases and 15,000 byte-fuzz cases.
  - Each analyst also ran its own `feature-dev:code-reviewer` pass.

### Resolution of G2-W1–W23

| ID | Status | Evidence |
|---|---|---|
| W1 | **Partially resolved** → G2-W1-R | Code fixed: `_write_transaction` (`groups.py:132-154`) takes BEGIN IMMEDIATE before the guard reads in all four write paths, or a SAVEPOINT when the caller already has a transaction. 11 transaction mutants are killed by the interleaving tests alone, and the threaded race on production connections serializes correctly. However, the tests prove the lock is held only at the first guard read, not that the guards and the write share one transaction (below). [own; via pr-test-analyzer] |
| W2 | Resolved | Mutants killed: masked groups get offsets written (N1a); the NULL write skipped, leaving stale values (N1b); only the inch columns nulled on write (N1c); the read mask on 2 or 3 fields (N3, N3b); the read mask off (N4); the bundle built from raw shots (N5). The purpose change → recompute → second-connection check covers all four columns for `zero_check` (2 and 10 shots), `zero_step2` (9) and `zero_confirm` (3), through both recompute paths (`test_service_groups.py:950-977`). Every stale field is masked on read (`:1010-1020`). [own] |
| W3 | Resolved | Mutants killed: `includes_estimated` forced False, forced True, an extra `SizeOnlyStats` field, and a wrong `es_moa`. The field-set parity test is at `:1053-1056`. Real `bundle.stats` validate through `ROUTE_SCHEMAS[("GET","/api/groups/{id}/stats")]` to `SizeOnlyStatsOut` or `GroupStatsOut` as appropriate (`:1058-1082`). [own] |
| W4 | Resolved | `summaries.py:85` now uses `==`. Mutants killed: `is` restored; an enum-only step2/confirm check; thresholds 9 and 11; the zero-check note only below 10 shots. Plain-str purposes run through `group_summary` and a 4-group `session_summary` (`test_summaries.py:259-310`). [own] |
| W5 | Resolved | The gate in `test_types.py` now covers `src/`, `tools/` and `tests/support/`, including methods and properties. Mutants that remove docstrings (`tools/__init__`, `ScenarioResult.passed`, `HttpResponse.text`, `io.sha256_hex`) are killed. [via review-14; review-13] |
| W6 | Resolved | Every earlier confirm group is size-checked (`zeroing.py:160-162`). An empty or short middle group gives INVALID from a fixed template, with no crash. 15+5+10 no longer reaches the confidence label. Every W6-class mutant is killed. A residual on the initial group is in G2-N1. [via review-12] |
| W7 | Resolved | `[full, k]` and `[full, full, k]` for k = 1..9 return the exact "k of 10" template with `correction is None` and `stats is None`, validated through `AdviceOut`. The adapted Z33 is killed. [via review-12] |
| W8 | Resolved | Exact kind, Correction and message are asserted for one-axis and exactly-0.1-mil cases. Z03, Z30, Z37 and Z39 are killed. [via review-12] |
| W9 | **Not resolved (regressed)** → G2-W9-R | All six cycle-1 spellings are now caught, but FX2.3 lost PascalCase coverage that the cycle-1 regex had (below). [via review-12; reproduced by review-11] |
| W10 | Resolved | `_check_group_links` runs on `create_group` and `update_group` (on the merged links), and `update_session` checks the rifle. 16 link mutants are killed. [via review-13] |
| W11 | **Partially resolved** → G2-W11-R | The `update_zero` foreign-lot guard and the `create_zero_check` linked + purpose guard hold (Z1–Z11 killed; Z3 is equivalent). But `update_zero(lot_id=...)` can still re-point a zero against its linked groups (below). [via review-13; reproduced by review-11] |
| W12 | Resolved | The override is gone and the signature is asserted. D1–D6 are killed. [via review-13] |
| W13 | Resolved | review-9's S22 and S23 are killed by `TestOtherLotFilters`. [via review-13] |
| W14 | Resolved | AST, SQL-literal and behavioral rollback guards are in place. S24, S25 and obfuscated-commit mutants are killed. [via review-13] |
| W15 | Resolved | Every cycle-1 column swap and five more are killed. [via review-13] |
| W16 | Resolved | Residue is now checked by PID, PGID, a `ps -E` environment scan and `lsof`. SIGTERM, SIGHUP and SIGINT leave zero residue. The T2.17 orphan scenario was reconstructed and cleaned up, and 8 concurrent runs at load average 21 left nothing behind. A new test gap in the next-start orphan warning is in G2-N2. [via review-13] |
| W17 | **Partially resolved** → G2-W17-R | The AV1-as-`heic` input and the ASCII-typed XResolution input are rejected. A third hostile EXIF still escapes as `AttributeError` (below). [via review-14; reproduced by review-11] |
| W18 | Resolved | `io.py:73-77` caps the aspect ratio at 20:1 from the header. Both detectors raise ValueError. 26 shapes × 4 kinds × 4 layouts produced 0 non-ValueError escapes. [via review-14] |
| W19 | **Partially resolved** → G2-W19-R | B-23 scoring, INCOMPLETE and the must-fail fixture are fixed. The vacuous pooled-holes assertion (cycle-1 sub-item W8) is unchanged on disk (below). [via review-14; reproduced by review-11] |
| W20 | Resolved | A bad `--only`, empty or invalid `--seeds`, or a missing catalog exits 2 with "nothing was evaluated". [via review-14] |
| W21 | Resolved | The conclusions are computed from the verdict. A failing run prints FAIL and "B-08 escalation: **REQUIRED**". [via review-14] |
| W22 | **Partially resolved** → G2-W22-R | The removed cues are gone, and every listed cue's on/off mutant is killed with the photos hidden. Two kept sub-rules still rest on one photo each (below). [via review-14; reproduced by review-11] |
| W23 | Resolved | The C-shaped outline test kills the "no `_project`" mutant. The −6 px `_inside` mutant is equivalent. [via review-14] |

Totals: 17 resolved and 6 not fully resolved (W1, W9, W11, W17, W19, W22).

### Spec Alignment
- **AC-10 (Group 2 pieces).** The finalize guards, the no-silent-drops rule and the rejection de-dup now run under the write lock.
  - Deterministic interleavings: the rival is refused as "database is locked".
  - Threaded race on production connections: the rival waits, then is refused with `group_final`, and the group stays final with 3 of 3 shots.
  - Atomicity across the guard→write gap is correct in code, but no test pins it (G2-W1-R).
- **B-09/B-14/B-18 masking, end to end.** No leak path was found, and W2 and W4 are closed:
  - services: masked on write, on read, and after a purpose change;
  - summaries: str purposes are masked;
  - `repo_dope`: the override is removed (W12).
- **AC-5 must-never.** Every SHOOT_MORE and INVALID path returns `correction=None` and `stats=None`. Two `step2_confirm_advice` contract lines are unpinned (G2-N1).
- **D-14.** `src/` has 0 hits. The verifier regressed on PascalCase (G2-W9-R).
- **AC-1 and AC-9 through the services (chain probe).** 13 synthetic groups (clean circle 1 in and 3 in, ragged_10x7, skew10_grid, high_right_group, merged_pairs, multi_dot_target ×3, black_grid_light_holes_qr, cardboard_sticker_ragged, red_grid_band_light_diamond, green_grid_upside_down_staples) were run from PNG bytes, each as `general`, `zero_check` and `zero_step2`:
  - worst |ΔES| = 0.0036 in (tolerance 0.05) and worst |ΔPOI| = 0.0005 mil (tolerance 0.02);
  - masked purposes return `SizeOnlyStats` with all four stored columns NULL;
  - `includes_estimated` matches the estimated origins;
  - `io.load_oriented` returns the exact rendered pixels.
- **Upload handling.** The FLOAT-typed EXIF input still produces a 500 rather than a 415 (G2-W17-R). It fails closed: nothing is stored, and the body is generic.
- **No outbound network.** `src/` and `tools/` have no HTTP client or socket import (grep). Bandit is clean.
- **Layering.** `test_layering` is green. The new `reference_circle`/`reference_grid` → `vision.io` import stays within one layer. No `src/` module imports `tools` or `tests`.

### Critical
- None.

### Warning

**G2-W1-R. The W1 tests prove the lock is held at the first guard read, not that the guards and the write share one transaction.** [own; via pr-test-analyzer, independently reproduced]
- Where:
  - Code: `src/rga/services/groups.py:310-336` (`finalize`), `:162-168` (`recompute_offsets`) and `:171-175` (`recompute_session_offsets`).
  - Tests: `tests/integration/test_service_groups.py:1085-1098` (`interleave` fires right after the first guard read), `:1118-1128` (the trace test checks only `statements[0]`), `:1130-1147` and `:1205-1224`.
- Evidence: five mutants survive `test_service_groups.py`, `test_summaries.py` and `test_repo_sessions.py` (303 passed):
  - M2: `finalize` keeps its guards in `_write_transaction` and moves `update_group` + `_write_offsets` into a following `with conn:`.
  - M2b: the same, with a second `_write_transaction`.
  - M4 and M4b: the same split in `recompute_offsets`.
  - M5: `recompute_session_offsets` with one transaction per group.

  No other test file exercises the services.
- Reproduced effect, with a rival fired in the gap (a hook on `repo_rifles.now_iso`, or on the first `set_shot_offsets`):
  - Real code: the rival gets "database is locked" in both cases.
  - M2: the group is stored **final with 4 of 3 shots**, which is the cycle-1 W1 defect returning.
  - M4: the stored `dx_mil` is 0.1944 at 300 yd, where the fresh value is 0.0648.
- Why Warning: this is a new element (FX2.1's one-transaction guarantee), and no test fails without it (testing-standards rule 5).
- Fix, already validated on scratch (green on current code; red on all six split mutants, M3 included): extend the trace test so that, for each of the four write paths:
  - the first statement is `BEGIN IMMEDIATE`;
  - there is exactly one `COMMIT`;
  - no BEGIN, COMMIT, END or ROLLBACK appears between them;
  - no INSERT, UPDATE, DELETE or BEGIN appears after the COMMIT.

  Optionally, also fire a rival at the write (finalize: `repo_rifles.now_iso`; recompute: the first `set_shot_offsets`). File: `tests/integration/test_service_groups.py`.

**G2-W9-R. FX2.3 regressed the D-14 verifier on PascalCase best-of.** [via review-12 NEW-W2; reproduced by review-11]
- Where: `tests/unit/test_no_forbidden_metrics.py:9-16`.
- Cause: under IGNORECASE, `(?![a-z])` also rejects capitals, and the camel branches are case-sensitive, so a capital `B` escapes both.
- Evidence:
  - `BestOfN`, `BestOfThree` and `class BestOfGroups:` were flagged by the cycle-1 regex and now escape.
  - `BestNGroups`, `<th>Mean&nbsp;radius</th>`, `mean__radius` and `mean  radius` were never caught.
  - `src/` has 0 hits today. Group 3 adds the JS class names and table headers where these would appear.
- Fix (validated by review-12 against all 33 existing plants and 15 legitimate names; `BestOffset`, `BestNumber` and `BEST_OFFSET` stay unflagged):
  - Use `(?-i:[bB]estOf[A-Z0-9])`, `(?-i:[bB]estN[A-Z0-9])` and `mean(?:[\s_-]+|&nbsp;)?radius`.
  - Add each string above as a plant.

**G2-W11-R. `update_zero(lot_id=...)` can re-point a zero against its linked groups, creating a state that every link path refuses.** [via review-13 N1; reproduced by review-11, `/tmp/rv11_tools/n1_probe.py`]
- Where: `src/rga/db/repo_zeroing.py:18-27` (`ZERO_UPDATABLE`), `:59-64` (`_UPDATE_ZERO`) and `:139-165`.
- Reproduction:
  1. Create a zero with no lot, and link groups of lots L1 and L2 to it.
  2. `update_zero(lot_id=L1)` is accepted, and both groups stay linked.
  3. Afterwards `link_group(z, g_L2)` raises `GroupNotLinkable`, and `update_group(g_L2, zero_id=z)` and `create_group(lot L2, zero z)` raise `GroupLinkMismatch` ("tied to another lot").
- Why it matters: B-23 requires the repos to enforce link consistency "matching link_group". It is latent, because no planned route changes a zero's lot, but that is the same footing as cycle-1 W10's `update_session(rifle_id)`.
- Fix: remove `lot_id` from `ZERO_UPDATABLE` (no caller needs it), or guard it with `NOT EXISTS (SELECT 1 FROM groups WHERE groups.zero_id = zeros.id AND groups.lot_id <> ?)` and raise `ZeroTargetMismatch`. Add a negative test with a linked group of the other lot.

**G2-W17-R. A hostile EXIF still escapes `ImageRejected` as `AttributeError`, so an upload would return a 500 instead of a 415; FX2.4's evidence claim is falsified.** [via review-14; confirmed by feature-dev:code-reviewer; reproduced by review-11, `/tmp/rv11_tools/w17r.py`]
- Where: `src/rga/vision/io.py:38` (`_EXIF_ERRORS`) and `:111-114`.
- Trigger: EXIF Orientation=6 plus Make (0x010F) or Software (0x0131) typed FLOAT (type 11). `exif_transpose` re-serializes the EXIF and raises `AttributeError: 'float' object has no attribute 'encode'`. Reproduced in JPEG and PNG for both tags; HEIF is unaffected.
- The false claim: `test-evidence.md:560` says `AttributeError`/`KeyError` "could not be triggered by any crafted input", and they were removed from the catch on that basis.
- It fails closed: nothing is stored, and the body is generic.
- Fix: preferably, read the orientation tag defensively and apply `img.transpose(...)` directly, so EXIF is never re-serialized (the BGR output drops metadata anyway). The minimal alternative is to add `AttributeError` and `KeyError` to `_EXIF_ERRORS`. Either way:
  - add the FLOAT-typed Make/Software case for JPEG and PNG to `tests/unit/test_image_io.py`;
  - correct the evidence line.

**G2-W19-R. The vacuous pooled-holes assertion (cycle-1 sub-item W8) is still on disk, and FX2.5's evidence claims a check that does not exist.** [via review-14; confirmed by feature-dev:code-reviewer; reproduced by review-11]
- Where: `tests/integration/test_eval_real_photos.py:161-163`. The assertion is `s["holes"] == sum(... and 0 ...) + s["holes"]`, which reduces to `x == 0 + x`.
- Evidence: mutant `"holes": sum(g.holes for g in groups) + 7` (`tools/eval_real_photos.py:628`) survives (85 passed, 1 skipped). No test asserts the pooled hole count (grep).
- The false claim: `test-evidence.md:570` says "the pooled hole count is asserted as 10 + 10 + 15".
- Fix: assert the real total, computed from the exported scenarios' `hole_indices`, and correct the evidence line.

**G2-W22-R. Two kept T2.19 sub-rules are still pinned only by the skipping real-photo test, and the FX2.6 report and Done note claim more than holds.** [via review-14; reproduced by review-11]
- Where:
  - `src/rga/vision/detect.py:526`: the clump centre is the Voronoi-cell centroid.
  - `src/rga/vision/detect.py:79`: `tan_min_value = 70`.
  - The pin `tests/integration/test_detection_accuracy.py:467` (`test_clump_centres_follow_each_hole_when_sizes_differ`, atol 3 px).
  - `spike-report.md:131-132` ("Every cue below has a test that runs without the gitignored real photos and fails when the cue is removed") and the FX2.6 Done note.
- Evidence: with the photos hidden, both mutants survive the detection, pipeline, eval, coordinate and ragged suites (390 passed, 18 skipped, 1 xfailed). review-14 also ran the whole non-live suite (2852 passed). With the photos linked read-only, each is killed only by `test_detection_real_photos`:
  - the bare distance-peak centre is killed by real_04 (found 13 → 12);
  - `tan_min_value` 70→1 is killed by real_01 (found 6 → 5).
- On the pin's own drawn scene the peak-only centres are exact, while the current code is about 0.6 px off, so the cited pin passes either way.
- Why it matters: B-23 says to pin or remove every such cue.
- Fix:
  - (1) Use the peak positions directly, or add a drawn scene where the cell centroid and the peak differ by more than 1 px and assert within 1 px.
  - (2) Add a drawn dark-brown, tan-hued blob with V < 70 that must not be detected as a hole, or remove the floor.
  - Re-run the synthetic gate (design note: don't change `DetectParams` defaults without it), re-report the real photos, and correct the pin text and the Done note.

**G2-N1. Two `step2_confirm_advice` contract lines have no test that fails without them.** These were in the cycle-1 code and are newly found. They are the W6/W7 classes, a short Step 2 group entering the judgment and a wrong correction text. [via review-12 NEW-W1; reproduced by review-11, `/tmp/rv11_tools/zdemo.py`]
- **(a) The initial-group threshold** (`src/rga/domain/zeroing.py:155`). It is pinned only by a 6-shot case (`tests/unit/test_zeroing.py:484-497`).
  - Mutants `< 7` and `< STEP2_MIN_SHOTS - 1` survive all 608 consumer tests.
  - With `< 7`, an 8-shot initial plus a 10-shot confirm returns "Zero confirmed: ready for the dot." instead of INVALID "…8 of 10 so far". The 18-shot overlay then feeds the dot.
  - That contradicts spec §4.6 (20+ shots) and FX2.2's own docstring ("every group in the overlay is a full group").
- **(b) Judging on the latest group, elevation axis** (`zeroing.py:170`). The only 3-group judged test moves the first confirm in x only (`:499-511`, `ring(0.9, 0.0)`).
  - Mutant: `confirm_mean_dy` taken from `groups[1]`. It survives all 608 tests, while the windage twin is killed.
  - With the first confirm 0.3 mil high and the latest centred, the real code returns READY_FOR_DOT and the mutant returns "Dial DOWN 0.3, then shoot another 10-shot confirm group." That is a numeric correction from a superseded group, and nothing goes red.
- Fix:
  - Parametrize the initial-group test over 0..9 shots, asserting INVALID, the exact template, `assert_no_number` and `AdviceOut`.
  - Offset the first confirm on both axes in the 30-shot test (e.g. `ring(0.9, 0.9)`), or parametrize per axis.

**G2-N2. The next-start orphan warning, the only mitigation for the declared SIGKILL case, is untested where it is wired.** [via review-13 N2; reproduced by review-11]
- Where: `tests/support/live_server.py:495` (the `_warn_about_orphans()` call in `start_live_server`) and `tests/integration/test_live_server.py:632-660`.
- Evidence: the orphan test calls the private `_warn_about_orphans()` directly (`:656`). Deleting the call at `:495` survives 35 of 35.
- The claim: the FX2.7 Done note and the module docstring promise that the next `start_live_server()` warns. If a refactor drops that call, SIGKILL orphans go silent again, which is the cycle-1 W16 failure mode (orphans alive for about 9 h).
- Why Warning: this is a new element (FX2.7) with no test that fails without it.
- Fix: in that test, reset `_orphan_check_done`, then run `run_live_server()` (or `start_live_server()` + `stop()`) inside `pytest.warns(OrphanedServerWarning, match=str(orphan_pid))`.

### Suggestion
- None counted. The cycle-2 bar admits only Critical and Warning findings, and G2-S1–S39 stay deferred.
- Below-the-bar observations, recorded but not counted:
  - [via silent-failure-hunter] A failed COMMIT on the non-nested path of `_write_transaction` (`groups.py:152-154`) leaves the connection inside the transaction. It was reproduced only with injected faults; `get_db` closes the connection, which rolls back.
  - [via code-reviewer] `finalize` on an already-final group, called inside a caller's transaction, now commits that transaction.
  - [via code-reviewer] `invalid_shots_expected` and `invalid_coordinates` are now raised before `group_not_found` and `group_final`.
  - [via silent-failure-hunter] In nested mode the docstrings at `groups.py:136-138` overstate the guarantee. A deferred caller `BEGIN` is kept safe by WAL's SQLITE_BUSY_SNAPSHOT, not by the service lock.
  - [via review-12] TOUCH_UP_OK overlay stats are unasserted.
  - [via review-13] Surviving mutants are equivalent or backstopped. The W15 class docstring says "distinct" values, but the create_group test reuses id 1.
  - [via review-14] `real_photo_conclusions` is unpinned. The 0.3 d ragged-outline tolerance has no boundary test. Several `DetectParams` fields survive every non-skipping test. The `gray_dark_rel` 0.5–0.55 band carries 9 of the 69 found real holes with no non-skipping test; review-14 offered this for folding into W22-R, and the synthesizer did not fold it.
  - [via review-14] FX2.6 edited `tests/real/counts.json`, which is not in its file list. This is the honest re-baseline B-23 asked for, but undeclared.

### Cross-Task Consistency
- **Masking rule.** `masks_poi` (services), `summaries._poi_note`, `repo_dope.MIN_ZEROING_SHOTS` and `zeroing.STEP2_MIN_SHOTS` all use 10. `GroupPurpose` is a `StrEnum`, so `masks_poi(GroupPurpose.ZERO_CHECK, n)` and the str form agree. G2-S5 (three copies) stays deferred.
- **services ↔ db.**
  - `_write_transaction` relies on repos never committing, and W14 now pins that.
  - `replace_shots` nests its `SAVEPOINT replace_shots` inside the service transaction.
  - `save_review` and `finalize` call `update_group` only with non-link fields, so `GroupLinkMismatch` cannot arise from them.
  - `_check_group_links` is check-then-write, so it is atomic only inside the caller's BEGIN IMMEDIATE [review-13].
- **services ↔ schemas.** `SizeOnlyStats` and `SizeOnlyStatsOut` are pinned both ways (W3), and real stats pass the stats-route union.
- **vision → services.** The chain probe above found the frames, y-flip and AC-1 tolerances intact after the FX2.4 and FX2.6 changes.
- **Notes for the lead and the Group 3 owners (not counted as findings):**
  1. **B-24.** Python's `sqlite3.connect` already defaults to `timeout=5.0`, so production connections wait about 5 s. The threaded probe confirms the rival waits, then gets `group_final`. Only the tests' `busy_timeout = 0` connections fail fast. The planned PRAGMA is harmless but already the effective default.
  2. **T3.3.** `io` accepts 1×1 to 63×63 uploads, and `propose_candidates` then raises ValueError. "T3.3 relies on ImageRejected alone" therefore holds only for aspect ratio. Either reject a minimum side under 64 in `io`, or give T3.3 a criterion mapping the detectors' ValueError to a 4xx [review-14].
  3. **T3.7.** Under FX2.2, an abandoned short confirm group makes every later judgment INVALID. T3.7 needs a criterion for which confirm groups are passed in: full or finalized ones only, or a way to drop an abandoned group [review-12].
  4. **Group 3 link writes.** Services should call `create_group`/`update_group` inside `_write_transaction`, and map `GroupLinkMismatch`, `GroupNotLinkable` and `ZeroTargetMismatch` to 409, as tasks.md:347 already requires [review-13].
- **File ownership for the fix wave.** No two items share a file:

  | Finding | Files |
  |---|---|
  | W1-R | `tests/integration/test_service_groups.py` |
  | W9-R | `tests/unit/test_no_forbidden_metrics.py` |
  | W11-R | `src/rga/db/repo_zeroing.py`, `tests/integration/test_repo_zeroing.py` |
  | W17-R | `src/rga/vision/io.py`, `tests/unit/test_image_io.py` |
  | W19-R | `tests/integration/test_eval_real_photos.py` |
  | W22-R | `src/rga/vision/detect.py`, `tests/integration/test_detection_accuracy.py`, `spike-report.md`, and the DESIGN_POINTS text in `tools/eval_detection.py` |
  | N1 | `tests/unit/test_zeroing.py` |
  | N2 | `tests/integration/test_live_server.py` |

  The evidence corrections at `test-evidence.md:560` and `:570` go to the lead or the owners.

### Tests
- [x] **All tests passing.**
  - `cd /Users/calebschwarz/Documents/Hunting && .venv/bin/pytest -q`: exit 0. 2910 passed, 2 skipped and 1 xfailed in 213 s.
    - Skips (declared): `test_scripts_syntax.py:50` "SHELLCHECK: NOT INSTALLED" and `test_eval_real_photos.py:923` "real-photo gate outstanding".
    - The xfail is `tape_cross_multi_aim` (G2-S30).
    - The only warning is the lead-accepted Starlette httpx deprecation.
  - `make lint`: exit 0. `ruff check .` passes, `bash -n` passes on every script, and shellcheck is NOT INSTALLED (declared).
  - `./scripts/scan.sh`: exit 0. `BANDIT: scanned 5675 lines; high=0 medium=0 low=0` and `PIP-AUDIT: audited 21 pinned packages; vulnerable packages=0`.
  - Spike gate: `.venv/bin/python -m tools.eval_detection --catalog tests/synth/catalog.json --report /tmp/rv11_spike_report.md` → exit 0, "23/23 scenarios pass; failing: none", "P-11 synthetic gate: PASS". Then `.venv/bin/pytest tests/integration/test_detection_accuracy.py -q` → exit 0, 97 passed.
    - The committed `spike-report.md` and `tests/real/counts.json` are sha-identical before and after.
    - The fresh report differs from the committed one in the runtime columns only.
  - No residue after the gates: `pgrep -f -- '-m rga'` finds nothing, there are no `rga-live` environment processes, and no Python listener.
  - Every fix task's Run, re-run by its slice owner, matches its Done-note count: FX2.1 195, FX2.2 163, FX2.3 165, FX2.4 478, FX2.5 85 + 1 skipped, FX2.6 190 plus the eval exit 0, FX2.7 35.
- [ ] **Test coverage adequate.** Not yet:
  - Contract lines with no test that fails without them: G2-W1-R, W22-R, N1 and N2.
  - Verifier gaps: G2-W9-R (regressed) and W19-R (vacuous).
  - An escape found by fuzzing: G2-W17-R.
- **Tier coverage.**
  - The Group 2 gate is T1 + T2 + accuracy (test-plan §6), and all of it ran green.
  - T3 (`smoke.sh`) is due in Group 3. `live_server.py` remains the local T3 analogue.
  - The real-photo gate is outstanding and declared (B-19, B-21), with no claim of a pass.
- **No assertions on mocks.** Monkeypatches only provide timing hooks or inject faults (the `interleave` hooks, the failure injections, canned eval results). Every assertion reads real SQLite state, real pixels or real process state.
- **Completion claims against what was reproduced.** Mismatches:
  - `test-evidence.md:560` (FX2.4, the AttributeError claim): G2-W17-R.
  - `test-evidence.md:570` (FX2.5, "10 + 10 + 15"): G2-W19-R.
  - The FX2.6 Done note and `spike-report.md:131` ("every cue ... fails when the cue is removed"): G2-W22-R.

  The FX2.1 claims (195 passed, 18 mutants killed, interleavings refused) reproduce. Its interleavings are real, but they fire within the first transaction, so they do not reach the guard→write gap (G2-W1-R).

### Verdict
Verdict: FAIL — 0 Critical, 8 Warning (G2-W1-R, G2-W9-R, G2-W11-R, G2-W17-R, G2-W19-R, G2-W22-R, G2-N1, G2-N2), 0 Suggestion counted.
Reason: 17 of 23 cycle-1 Warnings are resolved and every gate is green. Six are only partly resolved:
- two verifiers are still blind or regressed (W9-R, W19-R);
- one hostile-EXIF escape remains (W17-R);
- one link-consistency hole remains (W11-R);
- two contract elements have no test that fails without them (W1-R, W22-R).

There are also two new untested contract lines (N1, N2). Two fix-wave evidence claims were falsified (FX2.4, FX2.5), and one is overstated (FX2.6).

This was cycle 2 of 3. Group 2's last review is cycle 3, which is terminal: if it does not PASS, the group is reported blocked for the user's decision.

## Group 2 — Cycle 3

Date: 2026-10-01 · Reviewing: Group 2 after fix wave 2, FY2.1–FY2.7 (B-27), re-verifying the 8 cycle-2 Warnings (G2-W1-R, W9-R, W11-R, W17-R, W19-R, W22-R, N1, N2) · Scope/cycle: Group 2, cycle 3 of 3 (B-07). **This cycle is TERMINAL.** There is no cycle 4 for this group.

Synthesizer: review-15. Its slice was W1-R (FY2.1, `tests/integration/test_service_groups.py`), a spot regression of the 17 cycle-1 Warnings resolved in cycle 2, and cross-module consistency.

Analysts (all three reported):
- review-16: domain and services, W9-R (FY2.2) and N1 (FY2.6).
- review-17: db and live server, W11-R (FY2.3, repo part) and N2 (FY2.7).
- review-18: vision and tools, W17-R (FY2.3, io part), W19-R (FY2.4) and W22-R (FY2.5).

Coordination: degraded mode (B-01). Ground truth is the disk, the tasks.md Done notes and lead corrections, test-evidence.md (including the appended corrections at `:634-641`, `:654-656` and `:666-667`), and decisions.md. B-20..B-27 are decided and are not re-raised.

Admissibility bar (lead handoff): a new finding counts only if it is at the Critical/Warning bar AND either was introduced or exposed by fix wave 2, or is a regression of an already-resolved item. PASS means zero Critical and zero Warning.

### Method
- **What was read.** Every cited line was re-read on current disk.
- **Snapshot diff.** The repo was diffed against the cycle-2 snapshot `/tmp/rv11_scratch`. The changed files are exactly the FY2 file lists:
  - source: `repo_zeroing.py`, `io.py` and `detect.py`;
  - tests: `test_service_groups.py`, `test_no_forbidden_metrics.py`, `test_repo_zeroing.py`, `test_image_io.py`, `test_eval_real_photos.py`, `test_detection_accuracy.py`, `test_zeroing.py` and `test_live_server.py`;
  - other: `tests/real/counts.json`, and the DESIGN_POINTS text in `tools/eval_detection.py`, which is a text-only change.

  `services/groups.py`, `domain/zeroing.py`, `domain/summaries.py`, `repo_sessions.py`, `repo_dope.py` and `tests/support/live_server.py` are unchanged. The repo was also compared against review-15's start snapshot after the gates, and nothing had changed during the review.
- **Scratch copy.** review-15's mutants ran on `/tmp/rv15_scratch` with the venv symlinked, `PYTHONDONTWRITEBYTECODE=1` and `-p no:cacheprovider`. A probe confirmed that pytest imports the scratch copy's own `rga` and `tests.support`. After every mutant the harness restored the file and checked it with `cmp` against the repo. The repo was never edited. Artifacts are in `/tmp/rv15_tools/`:
  - `mutate.py`;
  - `w1r_split.json` / `.out`;
  - `regress17.json` / `.out`;
  - `s_c.json`;
  - `chain_probe.py`;
  - `w17r2/`;
  - the gate logs.
- **review-15's mutants:**
  - 16 split-transaction and lock-placement mutants for W1-R;
  - 18 regression mutants;
  - one re-check of an analyst survivor (S-c).
- **Probes:**
  - A vision → services chain on 13 synthetic scenarios × 3 purposes (45 rows), with an added EXIF Orientation 6 PNG leg.
  - An independent rebuild of the G2-W17-R2 trigger files, run against both the current `io.py` and the cycle-2 `io.py`.
- **Plugin passes on the W1-R slice:**
  - `feature-dev:code-reviewer`: static, no findings at ≥ 80 %.
  - `pr-review-toolkit:pr-test-analyzer`: ran its own mutants on the scratch copy. It reported four Minor items, merged below the bar.

  Each analyst ran its own `feature-dev:code-reviewer` pass, and review-16 also ran `pr-test-analyzer`.

### Resolution of the 8 cycle-2 Warnings

| ID | Status | Evidence |
|---|---|---|
| W1-R | **Resolved** | Three new classes:<br>- `TestOneTransactionPerWritePath` (`test_service_groups.py:1344-1403`), whose checker `assert_one_transaction` (`:1330-1341`) is itself tested against five bad traces;<br>- `TestRivalAtTheWriteIsLockedOut` (`:1406-1496`);<br>- `TestRivalAtEveryStatementIsLockedOut` (`:1499-1572`), with a positive control at `:1557`.<br><br>review-15's 16 mutants are review-11's six (M2, M2b, M4, M4b, M5, M3) plus R1–R10: a following `with conn:` in save_review, a deferred BEGIN, guard reads hoisted before the lock in finalize, session recompute and save_review, a commit + BEGIN IMMEDIATE after every shot or between groups, an always-savepoint mode, and late writes after the block.<br><br>All 16 are killed by the file, by the trace class alone and by the every-statement class alone. The rival-at-the-write class alone kills only the variants whose gap sits at its fire point (M2, M4, R1, R2, R6), as the FY2.1 evidence declares (`test-evidence.md:649`). The pre-FY2.1 tests alone let M2, M2b, M4, M4b, M5, R7, R9 and R10 survive, which confirms the new tests carry the guarantee.<br><br>The Run gives 140 passed in each of 3 runs, exit 0, matching the Done note. [own; via feature-dev:code-reviewer, pr-test-analyzer] |
| W9-R | **Resolved** | The PascalCase branches `(?-i:[bB]estOf[A-Z0-9])` and `(?-i:[bB]estN[A-Z0-9])`, and `mean(?:[\s_-]\|&nbsp;\|&#160;)*radius`, are at `test_no_forbidden_metrics.py:10-12`. Every required plant is flagged, plus 50 extra spellings. None of the 37 legitimate names is flagged, and `src/` has 0 hits in 38 of 38 files. 19 of 19 mutants are killed, and the FY2.2 claims (78 passed, 9/9) reproduce. [via review-16] |
| W11-R | **Resolved** | `lot_id` is out of `ZERO_UPDATABLE` (`repo_zeroing.py:18-26`) and out of `_UPDATE_ZERO` (`:58-62`). `update_zero(lot_id=...)` raises ValueError before any read or write.<br><br>A 40-route probe into the mismatch state was refused on every route, with 0 violation rows. The same probe goes red on the cycle-2 code. 9 of 9 mutants are killed. No caller or route updates a zero's lot (`schemas.py:1102-1107`). [via review-17; review-15 grep-confirmed the callers and routes] |
| W17-R | **Cited trigger resolved; FY2.3 regressed the contract** → G2-W17-R2 | The FLOAT-typed Make/Software + Orientation 6 inputs now load oriented in JPEG and PNG. The tag-type fuzz (`test_image_io.py:369-398`) is real, and FY2.3's 227 passed reproduces.<br><br>However, removing the EXIF catch lets malformed EXIF headers escape `load_oriented` as SyntaxError or ValueError. The cycle-2 code rejected these inputs as ImageRejected (Warning below). [via review-18; reproduced independently by review-15] |
| W19-R | **Resolved** | `test_eval_real_photos.py:161-167` derives the pooled count from the rendered fixtures (10 + 10 + 15 = 35), asserts `s["holes"] == 35`, and checks each photo's group sums. The +7, −1, first-group-only and groups + 1 mutants are killed. An AST scan found no remaining tautology, and the correction is at `test-evidence.md:634-641`. [via review-18] |
| W22-R | **Resolved** | The clump cell-centroid was removed: `detect.py:512-523` now returns the distance peaks, and that is the whole `detect.py` diff. `tan_min_value` and the grey route are pinned by drawn scenes that run with the photos hidden, and every mutant in both directions of removal is killed.<br><br>The real photos give 68/78 found and 11 FP, with real_04 at 12 and deterministic across thread counts. This matches the spike report and the `counts.json` floors; the cycle-2 centroid code gives 69. The DESIGN_POINTS text equals the report text. [via review-18] |
| N1 | **Resolved** | (a) The initial threshold at `zeroing.py:155`: 10 of 10 mutants killed, including `< 7`, `< 8`, `< 9` and `< STEP2_MIN_SHOTS - 1`.<br><br>(b) Judging on the latest group at `:169-170`: 14 of 14 mutants killed. This includes the cycle-2 survivor (dy taken from `groups[1]`), which is killed by the both-axes tests at `test_zeroing.py:513` and `:527`. The FY2.6 claims (176 passed, 9/9) reproduce. [via review-16] |
| N2 | **Resolved** | Both orphan tests (`test_live_server.py:632-664` and `:667-693`) get `OrphanedServerWarning` only through `run_live_server()`, inside `pytest.warns(match=<pid>)`. Deleting the `live_server.py:495` call fails exactly those two tests, and 7 of 11 other wiring mutants are killed. The survivors are below the bar.<br><br>No residue was left in any snapshot. The orphan tests passed 7 of 7 under load. [via review-17] |

Totals: 7 of 8 are resolved. W17-R's cited trigger is fixed, but its fix introduced one new admissible Warning (G2-W17-R2).

**Regression check of the 17 cycle-1 Warnings resolved in cycle 2:** all 17 still hold.
- 18 representative mutants were run on the current tests, and all were killed: W2 (write side and read side), W3, W4, W5, W6, W7, W8, W10, W12, W13, W14, W15, W16, W18, W20, W21 and W23.
- No test that pinned them was removed. Every line removed from the seven changed test files is one that B-27 superseded:
  - the FX2.4 `update_zero` lot tests;
  - the ASCII-XResolution rejection test;
  - the vacuous pooled-holes assertion;
  - the 3 px clump tolerance (now 1.5 px);
  - the old D-14 regex;
  - the private-call orphan test.

### Spec Alignment
- **AC-10 / no silent drops (services).** The guard reads and writes of `save_review`, `finalize`, `recompute_offsets` and `recompute_session_offsets` are now pinned as one `BEGIN IMMEDIATE`…`COMMIT` transaction. A rival writer is refused at every statement inside it. `services/groups.py` is unchanged since cycle 2.
- **AC-5 must-never and B-09/B-14/B-18 masking.** `zeroing.py`, `summaries.py`, `services/groups.py` and `repo_dope.py` are unchanged. The N1 contract lines are now pinned, and the W2, W3, W4, W6, W7, W8 and W12 regression mutants are killed.
- **D-14.** `src/` has 0 hits. The verifier covers the cycle-2 PascalCase and `&nbsp;` escapes. Sibling-branch escapes that predate the fix wave are recorded under Suggestions.
- **AC-1 and AC-9 through the services (chain probe, current code).** 13 scenarios were run from PNG bytes as `general`, `zero_check` and `zero_step2`, giving 45 rows:
  - the scenarios are clean_circle1, clean_circle3, ragged_10x7, skew10_grid, high_right_group, merged_pairs, multi_dot_target ×3 groups, black_grid_light_holes_qr, cardboard_sticker_ragged, red_grid_band_light_diamond, green_grid_upside_down_staples, close_holes_tight_pair and black_dot_mixed;
  - worst |ΔES| = 0.0347 in, on close_holes_tight_pair, identical on the cycle-2 code (tolerance 0.05), and worst |ΔPOI| = 0.0005 mil (tolerance 0.02);
  - masked purposes return `SizeOnlyStats` with all four stored columns NULL;
  - `includes_estimated` matches the estimated origins.

  An EXIF Orientation 6 PNG of each render loads back to the exact rendered pixels through the new `_apply_orientation`.
- **Upload handling (spec §4.3, B-23 W18, B-27).** Hostile EXIF must give ImageRejected or a successful load. FY2.3 meets this for tag-type corruption but not for header-level corruption (G2-W17-R2). The defect fails closed: nothing is stored, and the 500 body is generic.
- **No outbound network.** No new import in the changed files. Bandit is clean.
- **Layering.** `test_layering` is green, and no `src/` module imports `tools` or `tests`.

### Critical
- None.

### Warning

**G2-W17-R2. FY2.3 removed the EXIF parse guard, so malformed EXIF headers escape `load_oriented` as SyntaxError or ValueError instead of `ImageRejected`. Cycle 2 rejected the same inputs; this is a regression.** [via review-18; independently reproduced by review-15 with its own inputs, `/tmp/rv15_tools/w17r2/`; feature-dev:code-reviewer, run by review-18, found it independently]
- Where:
  - `src/rga/vision/io.py:87`: `img.getexif().get(_ORIENTATION_TAG)` in `_apply_orientation`. It is called from `load_oriented` at `:131`, outside any `try`.
  - FY2.3 removed `_EXIF_ERRORS` and its `ImageRejected("invalid EXIF metadata")` catch.
  - The broken promise is the module docstring at `io.py:3`: "Every decoder or limit failure surfaces as `ImageRejected`."
- Cause: `getexif()` itself parses the EXIF TIFF header and IFD, the PNG "Raw profile type exif" text, and the XMP fallback. FX2.4 had wrapped that parse in a catch.
- review-15's reproduction: each input below was built independently and loaded with the current `io.py`, then with the cycle-2 `io.py` from `/tmp/rv11_scratch/src`.

  | Input | Current code | Cycle-2 code |
  |---|---|---|
  | JPEG saved with `dpi=(300,300)` and EXIF `Exif\0\0XX*\0…` | `SyntaxError: not a TIFF file` | `ImageRejected: invalid EXIF metadata: SyntaxError` |
  | The same JPEG with EXIF `Exif\0\0II*` (truncated) | `SyntaxError` | `ImageRejected` |
  | PNG with an `eXIf` chunk that has a non-TIFF header | `SyntaxError` | `ImageRejected` |
  | PNG with a `tEXt` "Raw profile type exif" chunk of non-hex text | plain `ValueError` (not `ImageRejected`) | `ImageRejected` |

  Controls: a JPEG without JFIF density and the same bad EXIF loads in both versions, because Pillow pre-parses it in `_read_dpi_from_exif` and swallows the error. A PNG with a `tEXt` XMP chunk also loads in both.
- review-18's fuzz: 1,040 escapes in 6,000 structured cases (seed 18) and 3,418 in 20,000 (seed 99), across JPEG with density, PNG and HEIF. The cycle-2 `io.py` had **0** escapes on the same 6,000 cases. review-18 also reports a `TypeError` from an XMP `tEXt` shape; review-15's simpler XMP input did not trigger it.
- Why the in-repo checks miss it:
  - `_raw_exif` (`test_image_io.py:327-333`) always writes a valid TIFF header and IFD.
  - Pillow-saved JPEGs carry no JFIF density, so they take the swallowing path.
  - The PNG byte-mutation fuzz (`:481-491`) trips the chunk CRC at open, before EXIF is parsed.
- False completion claim: the FY2.3 evidence says "30,000 random byte corruptions of an EXIF block (JPEG) … produced no exception … so no catch is kept there" (`test-evidence.md:662`), and the Done note says malformed tags "never crash". Both measured only the swallowed JPEG path, which is the same inference error the cycle-2 correction at `:656` describes.
- Consumers:
  - T3.3 relies on `ImageRejected` alone (B-23 W18), so such an upload would return 500, not 415.
  - B-27's T3.3 mapping of reference-detector ValueError to 422 `image_too_small` would mislabel the plain-ValueError case if it caught ValueError broadly.
  - `tools/eval_real_photos.py:501` catches only `(OSError, ImageRejected)`, so one such photo aborts the eval run.
- Why Warning and not Critical: this is the same class, and the same fail-closed effect, as G2-W17 (cycle 1) and G2-W17-R (cycle 2), both rated Warning. Nothing is stored, the body is generic, and the app is local. feature-dev:code-reviewer rated it Critical; the rating here stays consistent with the earlier cycles. It is admissible because FY2.3 introduced it, and because it breaks B-27's stated acceptance ("nothing but ImageRejected or success may result").
- Fix, validated by review-18 on scratch (test_image_io 112 passed, 0 escapes on the 6 probes; the unrotated variant also gave 0 escapes on the 6,000-case fuzz):
  - Wrap only the metadata read, `img.getexif().get(...)`, in `except (SyntaxError, ValueError, TypeError, struct.error, OSError, EOFError)`.
  - In that branch, raise `ImageRejected("invalid EXIF metadata: …")`. That restores FX2.4's outcome for these inputs, and a sideways target would otherwise silently swap the correction axes.
  - Add one regression test per trigger above, plus a HEIF with garbage EXIF.
  - Correct the `test-evidence.md:662` claim.

### Suggestion
Below the bar or pre-existing. These are recorded and not counted.

**W1-R slice** [own; via pr-test-analyzer, feature-dev:code-reviewer]:
- `test_service_groups.py:1516`: the `assert "database is locked" in str(error)` runs inside an sqlite3 trace callback. CPython swallows exceptions raised there (verified empirically), so it cannot fail. The per-path tests are therefore meaningful only alongside the positive control at `:1557`. A `-k` subset without that control could be vacuous. Fix: collect errors in a list and assert after `call()`.
- `:1327` and `:1340-1341`: writes are detected by their first word. A late write phrased as `WITH … UPDATE` after the COMMIT survives all three classes. pr-test-analyzer demonstrated this on scratch: 140 passed, with a stale `dx_mil` at 300 yd. No repo function uses that SQL shape. Fix: compare `conn.total_changes` at the traced COMMIT with its value on return.
- `:1407`: the class docstring ("so no gap exists") claims more than the rival-at-the-write class proves. It cannot see a split whose second transaction also starts with `BEGIN IMMEDIATE`. The evidence states this honestly.
- `rival.close()` is not in a `finally` (`:1425`, `:1448`, `:1468` and `:1496`). This matters only after a failure.

**Domain and D-14** [via review-16]:
- S-a: the separator-run and `&nbsp;` handling went to the mean-radius branch only. The branches at `test_no_forbidden_metrics.py:11-14` still allow a single optional separator and are byte-identical to cycle 2. These are never flagged: `<th>Best&nbsp;of&nbsp;3</th>`, `best  of 3`, `best__n`, `<th>Group&nbsp;reduction</th>` and `group  reduction`. For the Group 3 template and JS owners: use one shared separator pattern on all five branches, and add these strings as plants.
- S-b: a "judge the confirm closest to aim" selection mutant survives, because in every judged test the latest confirm is also the closest. It needs a multi-line rewrite.
- S-d: two check-order mutants survive (INVALID vs SHOOT_MORE wording). They produce no number, so AC-5 holds.
- S-e: other escape classes are unrelated to the fix wave: labels wrapped across lines, synonyms (`avg_radius`), spelled-out numbers (`bestThree`), `BestGroup`, the hex entity `&#xA0;`, and silently skipped non-UTF-8 files.

**DB and live server** [via review-17]:
- N2 wiring survivors L5 (PPID filter dropped), L7 (call moved into `run_live_server`), L8 (once-guard never set) and L11 (equivalent).
- `decisions.md:284` still lists "update_zero lot_id" as a repo guard; B-27 (`:307`) supersedes it.

**Vision and tools** [via review-18]:
- `test_image_io.py:421`: the RATIONAL entry passes as out of range, not as non-integer.
- `:440-456`: the LONG 6 rotation is unpinned.
- `test-evidence.md:662`: "as Pillow did" is wrong for FLOAT 6.0, which `exif_transpose` rotated.
- `eval_real_photos.py:628`: informational summary fields are unpinned.
- `detect.py:523`: a 1 px shift and the restored centroid sit within the 1.5 px tolerance, consistent with the rule having been removed. The `gray_dark_rel` and `tan_min_value` pins are one-directional.
- `spike-report.md:131`: "every cue and threshold pinned" holds in the removal direction only.
- `:139`: "about 0.52 of the paper" is 0.502 in the pin.

**Pre-existing, Warning-class, not admissible this cycle (for the user's decision; not counted):**
- **S-c, unpinned divisor** [via review-16; reproduced by review-15]. `zeroing.py:169-170`: `/ len(latest)` → `/ STEP2_MIN_SHOTS` survives every consumer suite (447 passed). Every judged confirm in the tests has exactly 10 shots.
  - Effect: a 20-shot latest confirm would read "Dial DOWN 0.4, LEFT 0.6" instead of "DOWN 0.2, LEFT 0.3".
  - The real code is correct, so this is a test gap of the cycle-2 N1 class on code and fixtures that predate fix wave 2.
  - Killing test: initial `ring(1,1)`, latest `ring(1.08, 0.72, n=20)`, expect `Correction(-0.2, -0.3, …)`.
- **Live-server load flake** [via review-17; re-read by review-15]. `test_live_server.py:266-274` catches only `ProcessLookupError` around `os.killpg(pid, 0)`. On this macOS that call raises EPERM while the group's only member is an unreaped zombie.
  - It failed 1 of 7 module runs under concurrent load and 0 of 20 when looped alone. It fails closed and left no residue.
  - The lines are unchanged since cycle 2.
  - Fix: treat `PermissionError` as alive, as `_group_alive` does (`live_server.py:401-408`). T3.9 reuses these checks.

### Cross-Task Consistency
- **Interface changes from fix wave 2:**
  - `update_zero` lost `lot_id`. No caller passes it, no route updates a zero's lot, and `ZeroTargetMismatch` is still raised by `create_zero`, so the T3.2 409 mapping holds.
  - `io` no longer raises "invalid EXIF metadata". The intended effect is that inputs with malformed unrelated tags now load; the unintended effect is G2-W17-R2.
  - `detect._cluster_centers` returns peak positions as Python floats, so the sqlite and JSON boundaries are unaffected.
  - `DetectParams` is unchanged.
- **services ↔ db.** `_write_transaction` still relies on repos never committing, which W14 pins (the regression mutant is killed). Its one-transaction guarantee is now pinned from the service side (W1-R).
- **vision → services.** The chain probe above found the frames, the y-flip, the masking and the AC-1 tolerances intact after the FY2.3 and FY2.5 changes. close_holes_tight_pair, merged_pairs and cardboard_sticker_ragged give identical results on the cycle-2 code.
- **Spec documents.**
  - design.md §4 (`load_oriented`, `repo_zeroing` row) and test-plan.md:35 are consistent with the code.
  - B-26 (detach) and B-27 (tiny uploads → 422 `image_too_small`) are in T3.7 and T3.3 (`tasks.md:438`, `:379`).
  - B-24 (busy → 503) is at `tasks.md:368`.
- **File ownership for fix wave 2.** Each FY2 task touched only its declared files. `counts.json` was declared in FY2.5. No fix task edited `conftest.py`, `pyproject.toml`, the `Makefile` or `scripts/`.
- **Notes for the lead and the Group 3 owners (not counted):**
  1. T3.3 must not catch ValueError broadly for `image_too_small`, because `ImageRejected` is a ValueError subclass. Map `ImageRejected` first, and once G2-W17-R2 is fixed, nothing else should arrive.
  2. Extend the D-14 separator handling to every branch before the Group 3 templates land (S-a).
  3. T3.9's PID/PGID teardown checks should treat EPERM as alive.

### Tests
- [x] **All tests passing.**
  - `cd /Users/calebschwarz/Documents/Hunting && .venv/bin/pytest -q`: exit 0. 2998 passed, 2 skipped and 1 xfailed in 216 s, on the first run with no re-run needed.
    - Skips (declared): `test_scripts_syntax.py:50` "SHELLCHECK: NOT INSTALLED" and `test_eval_real_photos.py:932` "real-photo gate outstanding".
    - The xfail is `tape_cross_multi_aim` (G2-S30).
    - The only warning is the lead-accepted Starlette httpx deprecation.
  - `make lint`: exit 0. `ruff check .` reports "All checks passed!", `bash -n` passes on every script, and shellcheck is NOT INSTALLED (declared).
  - `./scripts/scan.sh`: exit 0. `BANDIT: scanned 5673 lines; high=0 medium=0 low=0` and `PIP-AUDIT: audited 21 pinned packages; vulnerable packages=0`.
  - Spike gate: `.venv/bin/python -m tools.eval_detection --catalog tests/synth/catalog.json --report /tmp/rv15_spike_report.md` → exit 0, "23/23 scenarios pass; failing: none", "P-11 synthetic gate: PASS", "B-08 escalation: not triggered". Then `.venv/bin/pytest tests/integration/test_detection_accuracy.py -q` → exit 0, 100 passed.
    - The fresh report differs from the committed `spike-report.md` in runtime columns only.
    - `spike-report.md` and `tests/real/counts.json` are sha256-identical before and after.
  - No residue after the gates: there are no `RGA_DATA_DIR=*rga-live-*` processes, `pgrep -f -- '-m rga'` finds nothing, and there are no `rga-live-*` temp dirs.
  - Every FY2 Run, re-run by its slice owner, matches its Done-note count: FY2.1 140 (×3), FY2.2 78, FY2.3 227, FY2.4 85 + 1 skipped, FY2.5 126 (plus eval exit 0; 109 + 17 skipped with the photos hidden), FY2.6 176, FY2.7 36.
- [ ] **Test coverage adequate.** Not yet: no test exercises header-level EXIF corruption, so G2-W17-R2 passes the whole suite.
- **Tier coverage.**
  - The Group 2 gate is T1 + T2 + accuracy (test-plan §6), and all of it ran green.
  - T3 (`smoke.sh`) is due in Group 3. `live_server.py` remains the local T3 analogue.
  - The real-photo gate is outstanding and declared (B-19, B-21), with no claim of a pass.
- **No assertions on mocks.** Monkeypatches only provide timing hooks (`interleave_before`, the trace callbacks) or inject faults (the positive-control commit in `:1557`). Every assertion reads real SQLite state, real pixels or real process state.
- **Completion claims against what was reproduced.**
  - Mismatch: `test-evidence.md:662` and the FY2.3 Done note ("malformed … never crash"; "no catch is kept there"). See G2-W17-R2.
  - Every other FY2 claim reproduces, including FY2.1's 10/10 split kills and the "rival-at-the-write kills only V1/V3" caveat, FY2.4's 6/6, FY2.5's 17/17 with the photos hidden, and FY2.6's and FY2.7's mutant lists.

### Verdict
Verdict: FAIL — 0 Critical, 1 Warning (G2-W17-R2), 0 Suggestion counted.
Reason: 7 of the 8 cycle-2 Warnings are resolved, and all 17 earlier resolutions still hold. W17-R's cited trigger is fixed, but FY2.3 removed the EXIF parse guard, so malformed EXIF headers now escape `load_oriented` as SyntaxError or ValueError. The cycle-2 code mapped them to ImageRejected.

**Group 2 is BLOCKED for the user's decision.** This was cycle 3 of 3, the terminal cycle, and no further fix or review cycle opens in this scope without explicit user authorization logged in decisions.md. Options for the user:
- (a) Authorize a narrow fix: guard the `getexif()` read (about 4 lines in `io.py`), add the per-trigger regression tests, correct `test-evidence.md:662`, and verify it.
- (b) Accept G2-W17-R2 as a known limitation and let Group 3 proceed, with T3.3 required to map any non-ImageRejected decode exception to 415.

Separately, the user should decide whether the two pre-existing Warning-class gaps listed above (the S-c divisor test and the live-server EPERM flake) are routed into Group 3 or the end-to-end scope.

## Group 2 — Cycle 4 (user-authorized, B-29)

Date: 2026-10-01 · Reviewing: Group 2 after fix 3, FZ2.1 (`src/rga/vision/io.py`, `tests/unit/test_image_io.py`) · Scope/cycle: Group 2, cycle 4 of 3 + 1, the **one-time extra cycle the user authorized in B-29** after the terminal cycle 3 failed on G2-W17-R2.

Reviewer: review-19, sole synthesizer, no analysts.

Scope, per the lead's handoff and B-29: (1) verify G2-W17-R2 is resolved; (2) confirm FZ2.1 did not regress W17 / W17-R / W18 (a malformed *unrelated* tag still loads oriented, the aspect cap, the decode-error mapping); (3) the full-suite regression gate. New findings are admissible only at the Critical/Warning bar and only in those two files or caused by FZ2.1. **Lead clarification received mid-review:** a *pre-existing* issue in those files that FZ2.1 neither introduced nor exposed counts toward the verdict only if it is **Critical** on its merits (the lead's own example: "a practical denial of service or memory exhaustion from a single uploaded file within the app's 40 MB / 60 MP limits"); Warning-class and below are recorded as uncounted notes for the user to route. That rule is applied below.

Coordination: degraded mode (B-01), no task tools. Ground truth is the disk.

### Method
- **What changed.** The repo was diffed against review-15's cycle-3 snapshot `/tmp/rv15_scratch`. Exactly two files differ, both declared in FZ2.1: `src/rga/vision/io.py` (the 4-line guard plus two docstring lines) and `tests/unit/test_image_io.py` (`import zlib`, plus a new section at `:537-777`). `src/`, `tests/`, `tools/`, `scripts/`, `Makefile` and `pyproject.toml` are otherwise byte-identical. Both files were sha256-pinned before and after this review and are unchanged.
- **Every cited line was re-read on current disk.** No finding here is quoted from a message or a remembered snapshot.
- **Scratch discipline.** All mutants ran on `/tmp/rv19/scratch` (an rsync of `src`, `tests`, `tools`, `pyproject.toml` with the venv symlinked), `PYTHONDONTWRITEBYTECODE=1`, `-p no:cacheprovider`. A probe confirmed pytest there imports `/private/tmp/rv19/scratch/src/rga/...`. Each mutant was restored and `cmp`-checked against the repo file. The repo was never edited.
- **Re-run of the cycle-3 reproducers** (review-18's `exif_probe.py` / `exif_fuzz.py`, review-15's `/tmp/rv15_tools/w17r2/`).
- **My own independent fuzz**, `/tmp/rv19/fuzz_rv19.py` — built from a different direction than both review-18's and the in-repo fuzz: payloads start from **well-formed** EXIF written by Pillow's own writer (both endians, with Exif/GPS sub-IFDs, MakerNote, rationals) or from well-formed XMP packets, then take byte-level **and IFD-structure-aware** mutations (retype / recount / repoint a chosen entry, rewrite the entry count, damage the header, truncate, splice). 19 container paths with always-valid framing: JPEG with JFIF density, without density, with `units=0`, with EXIF before JFIF, split across **two APP1 segments**, with an XMP APP1; PNG `eXIf` before and after IDAT, twice, and via `Image.save`; PNG raw profile as `tEXt`/`zTXt`/`iTXt`; PNG XMP as `tEXt` under both the `xmp` and `XML:com.adobe.xmp` keys, `zTXt` and `iTXt`; HEIF with EXIF and with XMP. Two oracles beyond "no escape": every load is compared pixel-for-pixel against an independent Pillow decode transposed by the EXIF-spec table, and every rejection must match io.py's fixed message grammar (no payload echo).
- **Over-rejection matrix**, `/tmp/rv19/overbroad_rv19.py` — deterministic, not random: orientations 1-8 × both endians × 13 metadata profiles × 12 containers, plus 2 XMP forms × 8 orientations × 6 carriers. Expected corners come from the marker geometry, not from Pillow or io.py.
- **Mutation runs** on the scratch copy: 16 guard mutants and 10 W17/W17-R/W18 regression mutants, each scored by `tests/unit/test_image_io.py` and, independently, by my fuzz and matrix.
- **Plugin passes:** `feature-dev:code-reviewer`, `pr-review-toolkit:silent-failure-hunter`, `pr-review-toolkit:pr-test-analyzer`. Their findings are deduplicated and merged below at the severity the evidence supports, tagged with the source.

### Spec Alignment
- **B-29 / FZ2.1 acceptance, item by item:** the guard wraps only the orientation read (`io.py:88-91`); the per-trigger regression tests exist (JPEG with density, PNG `eXIf` before and after IDAT, raw profile non-hex and non-TIFF, `tEXt` "xmp", HEIF — `test_image_io.py:592-622`); the no-payload-echo test is at `:624-628`; the ≥ 6,000-case container fuzz is at `:752-777` with non-vacuity floors; W17-R's malformed-unrelated-tag behaviour is still covered (`:661-671` and the older `:352-415`); the `test-evidence.md:662` correction is appended at `test-evidence.md:679-689` and is accurate about what the old probe could not reach. All met.
- **spec §4.3 / design §11 upload hardening, threat (c).** The documented control is "size cap 40 MB, decoded-pixel cap 60 MP, Pillow `MAX_IMAGE_PIXELS` set, magic-byte sniffing". FZ2.1 restores the error *contract* for hostile metadata. The control's *resource* promise does not hold for metadata — see G2-C1.
- **No new import, no outbound network.** `io.py`'s imports are unchanged; bandit and pip-audit are clean.
- **Layering.** Unchanged; `test_layering` is green in the full run.

### Resolution of G2-W17-R2
**Resolved.**

| Check | Result |
|---|---|
| review-15's 6 cycle-3 trigger files (`/tmp/rv15_tools/w17r2/load.py`) | 4 now `ImageRejected: invalid EXIF metadata: SyntaxError`/`ValueError`; the 2 that loaded before still load (the no-density JPEG, where Pillow swallows the parse at open, and the PNG `tEXt` xmp built without the trailing payload). No escapes. |
| review-18's 10 probes (`/tmp/review18/exif_probe.py`) | current code matches the pre-FY2.3 (FX2.4) column on all 10: 6 `ImageRejected`, 4 loads. Includes the `TypeError` from the `tEXt` "xmp" shape. |
| review-18's fuzz, seed 18, 6,000 cases | **0 escapes** (was 1,040 on FY2.3). |
| review-18's fuzz, seed 99, 20,000 cases | **0 escapes** (was 3,418 on FY2.3). |
| My independent fuzz, 10 seeds × 3,000 = **30,000 cases**, 19 containers | **0 escapes, 0 oracle mismatches, 0 message-grammar violations.** 24,888 loads, 5,112 rejections. Every rejection coincides with the independent Pillow parse also raising, so nothing is rejected that Pillow could read. Re-running seed 1901 after the gates reproduced 0/0. |
| Power check: the same 600-case slice of my fuzz against the FY2.3 `io.py` (`/tmp/rv15_scratch/src`) | 107 escapes (SyntaxError, ValueError, TypeError, struct.error). The fuzz does detect the bug it is certifying as fixed. |
| `test-evidence.md:662` | corrected at `:679-689`; the correction states accurately why the original 30,000-case probe never reached the guarded call. |

The guard itself is strong under mutation. Of 16 guard mutants, 13 are killed by `tests/unit/test_image_io.py` alone: removed (19 tests fail), `SyntaxError`-only (6), `(SyntaxError, ValueError)` (5), no `struct.error`/`OSError` (4 — the `II+` BigTIFF header cases), `_DECODE_ERRORS`-only so the XMP `TypeError` escapes (2), fail-open `orientation = None` (19), fail-open `return img` (19), payload echoed via `{exc}` (3), message text changed (17), `getexif()` hoisted out of the `try` (19), bare `raise` (19), plain `ValueError` instead of `ImageRejected` (19), exception type dropped from the message (2). Three survivors are equivalent or near-equivalent, not gaps: `except BaseException`, dropping `from exc` (no test asserts `__cause__`), and narrowing the `try` to `getexif()` alone — in Pillow 12.3 `.get()` on the returned object cannot raise, which `pr-test-analyzer` found independently.

### Regression check: W17, W17-R, W18
No regression. 10 regression mutants, 9 killed by the file, and the survivor is a pre-existing test gap, not a behaviour change:

| Mutant | Result |
|---|---|
| `ImageOps.exif_transpose` restored (W17-R) | KILLED, 24 tests |
| orientation ignored | KILLED, 27 |
| transpose table entry for 6 wrong | KILLED, 20 |
| non-int orientation coerced with `int()` | KILLED, 1 |
| aspect-ratio check removed (W18) | KILLED, 7 |
| aspect `>` → `>=` | KILLED, 2 |
| `RuntimeError` dropped from `_PLUGIN_DECODE_ERRORS` (the AV1-as-heic input, W17b) | KILLED, 2 |
| `SyntaxError` dropped from `_DECODE_ERRORS` | KILLED, 1 |
| `OSError` dropped from `_DECODE_ERRORS` | KILLED, 11 |
| `struct.error` dropped from `_DECODE_ERRORS` | **SURVIVED** — pre-existing gap, see the notes below |

Behaviour, measured rather than argued:
- **Well-formed orientations still rotate correctly everywhere.** The over-rejection matrix is **2,400 / 2,400 correct** on current code: orientations 1-8 × both endians × 12 containers × 13 profiles (clean Pillow EXIF, rich EXIF with sub-IFDs, FLOAT Make before *and* after Orientation, ASCII XResolution, unknown TIFF types, an out-of-range `count` on a later tag, a bad Exif-IFD pointer, a GPS pointer loop, a 300-byte MakerNote, a bad next-IFD pointer, duplicate unrelated tags), plus 96 XMP cases across `iTXt`, compressed `iTXt`, `zTXt`, `tEXt` and JPEG APP1. The same matrix on the cycle-2 `io.py` scores 1,784 / 2,400 (616 failures — that is W17-R).
- **A malformed unrelated tag still loads oriented** (the W17-R contract), in every container and both endians, including the FLOAT Make and ASCII XResolution triggers. The guard is not over-broad: in 30,000 fuzz cases no input was rejected that an independent Pillow parse could read.
- **The aspect cap and the decode-error mapping are untouched** — unchanged on disk, and their mutants are killed.

### Critical

**G2-C1. A single upload inside the 40 MB / 60 MP caps makes EXIF parsing allocate gigabytes; peak memory is about (metadata bytes × IFD entry count) and nothing bounds it.** [own; independently flagged by feature-dev:code-reviewer at 85 % confidence]

*Pre-existing: this is NOT caused or exposed by FZ2.1 — byte-identical behaviour on the cycle-2 and FY2.3 `io.py`. It is counted this cycle only because it meets the Critical bar the lead set for pre-existing in-file issues.*

- Where: `src/rga/vision/io.py:89` (`img.getexif()` inside the new guard) and, for JPEGs without JFIF density, inside `Image.open` at `:123`, where Pillow does the same parse in `_read_dpi_from_exif` (`PIL/JpegImagePlugin.py:402`, `:502-529`) — i.e. **before** `_apply_orientation` and outside its guard.
- Cause: `ImageFileDirectory_v2.load` (`PIL/TiffImagePlugin.py:896-942`) materialises a separate `bytes` for **every** IFD entry whose `count × unit_size > 4`, reading from the entry's offset and storing it in `_tagdata[tag]`. Entries are retained simultaneously, there is no cap and no de-duplication, and the IFD count field allows 65,535 entries. A crafted file points every entry at the same in-bounds region, so one payload is copied once per entry. Because every read is **in bounds**, Pillow neither warns nor raises: the file simply loads.
- Reproduction: `/tmp/rv19/exif_amplification.py <io_src> <container> <payload_KB> <entries> [watchdog_MB]`. It runs the load in a child and kills it at the watchdog, so growth can be shown without exhausting the host. Measured on current code, every file far inside both caps:

  | Container | File size | Entries | Outcome | Peak RSS | Wall |
  |---|---|---|---|---|---|
  | PNG `eXIf` | 0.3 MB | 100 | loaded | 67 MB | 0.00 s |
  | PNG `eXIf` | 1.0 MB | 300 | loaded | 351 MB | 0.02 s |
  | PNG `eXIf` | 4.0 MB | 300 | loaded | 1,277 MB | 0.10 s |
  | PNG `eXIf` | 8.0 MB | 300 | loaded | 2,505 MB | 0.21 s |
  | PNG raw profile (`tEXt`) | 1.0 MB | 500 | loaded | 304 MB | 0.02 s |
  | HEIF | 1.0 MB | 500 | loaded | 570 MB | 0.03 s |
  | JPEG, EXIF split over APP1 segments | 8.0 MB | 300 | loaded | 2,610 MB | 0.22 s |
  | JPEG with JFIF density, same split | 8.0 MB | 300 | loaded | 2,610 MB | 0.23 s |
  | PNG `eXIf` | 20 MB | 1,000 | **killed by my watchdog at 4,502 MB, still allocating** | > 4.5 GB | 0.54 s |
  | PNG raw profile | 10 MB | 1,000 | **killed at 4,681 MB, still allocating** | > 4.6 GB | 0.53 s |

  Peak tracks `payload × entries` linearly across every point. Pillow's 64 KB limit on a single APP1 marker does **not** bound JPEG: Pillow concatenates consecutive APP1 `Exif` segments (`PIL/JpegImagePlugin.py:103-104`), which the table above exercises.
- Scope of the bound: with a ~39 MB metadata payload and the 65,535-entry ceiling the nominal demand is in the terabytes, so in practice the only bound is the machine. On this host (16 GB) `RLIMIT_AS`/`RLIMIT_DATA` cannot be lowered (verified: "current limit exceeds maximum limit"), so there is no OS backstop. **Stated honestly: I stopped every run at a watchdog rather than driving the host into swap, so "the process dies / the machine thrashes" at the top of the in-limit range is an inference from the measured linear growth plus the absent rlimits, not an executed observation.** The measured multi-gigabyte allocations are facts; the terminal state is not.
- Why the caps do not help: `MAX_UPLOAD_BYTES` bounds the file, `MAX_PIXELS` and `MAX_ASPECT_RATIO` bound the decoded raster. Nothing bounds metadata, and the amplification factor between the file and the allocation is unbounded. This is the control in design.md §11 ("Upload hardening", threat (c) "Malformed or hostile image files") not doing the job it is documented to do.
- Error contract: where the allocation happens under the new guard (PNG, HEIF, JPEG with density), a `MemoryError` would be converted to `ImageRejected` — fail-closed, after the memory is already gone. For a JPEG **without** JFIF density the parse is inside `Image.open`, whose catch list `_PLUGIN_DECODE_ERRORS` (`io.py:47`) does not include `MemoryError`, so it would escape `load_oriented` and break the contract again. (Analysis of the catch lists on disk; not executed, for the reason above.)
- Reachability, stated plainly so the user can weigh it: the server is loopback-only and `OriginGuard` rejects cross-origin POSTs, so the practical trigger is the user importing a crafted file, or a local process posting one — not a remote attacker. Nothing is written to disk and no data is exposed; the impact is availability. A camera or phone file will not trigger it by accident.
- Why Critical and not Warning: it is memory exhaustion from one file inside the documented limits — the lead's stated bar, and independently CWE-789 (allocation sized by untrusted input) with the documented hardening control bypassed. The earlier io findings (G2-W17, W17-R, W17-R2) were rated Warning because they produce a clean generic 500 with no resource cost; this one allocates 2.5 GB from an 8 MB file in a fifth of a second and keeps going. I am not stretching the earlier precedent to cover it, and I am not inflating it to force a FAIL: the numbers above are the whole basis.
- Fix (for the user's decision, not authorised by me): bound the metadata **before** it is parsed, on the raw bytes, since the JPEG path allocates inside `Image.open`:
  - sniff the EXIF/XMP carriers in the input bytes (JPEG APP1 `Exif\0\0` segments summed, PNG `eXIf` and `Raw profile type exif`/XMP text chunks, HEIF EXIF) and raise `ImageRejected("EXIF metadata too large")` above a small cap — 256 KB covers every real camera file, including MakerNotes;
  - and/or replace `getexif()` with a bounded orientation-only reader: validate the TIFF header, read IFD0's count, clamp it by `(len - offset - 2) // 12`, scan the 12-byte entries for tag `0x0112` with type 3 and count 1, and never materialise an out-of-line value. That also removes the dependence on Pillow's whole-IFD behaviour.
  - Tests that would fail without the fix: the table rows above as T1 cases, asserting a fast `ImageRejected` and a bounded `tracemalloc` peak (for example under 256 MB) for a 4 MB PNG `eXIf` with 300 entries, and the same for the JPEG multi-APP1 and raw-profile carriers.

### Warning
None. No Warning-class finding was introduced or exposed by FZ2.1.

### Suggestion
Below the bar, or pre-existing and recorded for routing. None of these is counted.

**Pre-existing, Warning-class, routed to the user (not caused or exposed by FZ2.1 — each verified identical on the cycle-2 and FY2.3 `io.py`):**
1. **A malformed unrelated tag that sorts *before* Orientation silently drops the rotation.** [via pr-test-analyzer (criticality 8) and silent-failure-hunter; reproduced by me, `/tmp/rv19/sorted_tag_probe.py`] `PIL/TiffImagePlugin.py:944-951` turns an out-of-range value offset into a `UserWarning` and **stops reading the rest of IFD0**. TIFF requires ascending tag order, so Make (0x010F) and Model (0x0110) precede Orientation (0x0112) in real files and are stored out of line. A Make entry with a bad offset followed by a valid Orientation 6 loads **unrotated**, with no error, in PNG `eXIf` and in JPEG with and without density; `orient_first` on the same bytes rotates correctly. Identical on all three `io.py` versions, so it is not an FZ2.1 regression — but it does mean the W17-R contract ("a malformed unrelated tag still loads oriented") holds only when the bad tag sorts after Orientation, which is the order every existing test uses. Rejecting here would be wrong (a mutant that does so breaks the tag-type fuzz's load floor at `test_image_io.py:399`); the bounded orientation-only reader proposed under G2-C1 fixes this case as a side effect. Effect: the user sees a sideways photo; no crash, no data loss.
2. **A JPEG without JFIF density swallows malformed EXIF entirely.** [via silent-failure-hunter; already recorded as a control in cycle 3] Pillow parses and discards the error at open (`JpegImagePlugin.py:502-529`) and caches an empty, already-loaded `Exif`, so the guard has nothing to catch and the image loads unrotated. Fails open to a successful load, not to an escape, so the `ImageRejected` contract is intact. A fresh `Image.Exif().load(img.info["exif"])` before the read would make this consistent with the density path; the suggester verified that variant keeps all 140 tests green.
3. **The fuzz at `test_image_io.py:752-777` never checks orientation** — the loaded branch (`:766-768`) asserts only `dtype` and `ndim`, so it cannot see an unrotated or wrongly rotated result. [via pr-test-analyzer] My fuzz does check it (against an independent decode) and finds no mismatch on current code, so this is a durability gap in the repo's own test, not a live defect.
4. **`struct.error` in `_DECODE_ERRORS` (`io.py:32`) is load-bearing but unpinned** — removing it is the one surviving regression mutant. A PNG with a short post-IDAT ancillary chunk (for example a 2-byte `gAMA`) then escapes as `struct.error`; today it is `ImageRejected("image could not be decoded: error")`. Verified on scratch, `/tmp/rv19/r08_probe.py`. One test would close it.

**Minor, FZ2.1-local:**
5. `test_image_io.py:651-654` (`test_heif`) does not exercise `_apply_orientation`: libheif applies the rotation itself and pillow_heif resets the tag to 1, so the assertion passes regardless of the transpose table. It is still a valid end-to-end contract check; the comment could say so.
6. The guard discards `__cause__` only in the message, which is right, but nothing asserts the chaining itself, so the `from exc` is unpinned (mutant G16 survives). Note for whoever writes the T3.3 handler: `__cause__` *does* contain payload bytes (for example ``not a TIFF file (header b'XY\x00*...')``), so the handler must render `str(exc)` only, never the chain or a traceback.
7. `io.py:83` ("other tags are ignored") is true of what this module *writes*, but not of Pillow's parse, which reads the whole IFD — items 1 and G2-C1 both follow from that. Worth a word in the docstring.

### Cross-Task Consistency
- **Interface.** `load_oriented`'s signature, `ImageRejected`, the caps and the transpose table are unchanged. The only behavioural change versus cycle 3 is that malformed EXIF/XMP now raises `ImageRejected` instead of escaping, which restores the cycle-2 contract. No other module imports `_apply_orientation`.
- **Consumers.** T3.3 can rely on `ImageRejected` alone for metadata corruption again (B-23 W18). The cycle-3 note still stands: map `ImageRejected` **before** any broad `ValueError` catch for `image_too_small`, because `ImageRejected` is a `ValueError` subclass (`io.py:50`). `tools/eval_real_photos.py:501` catches `(OSError, ImageRejected)`, which is now sufficient for these inputs. G2-C1 would not reach any of these mappings — the process is in trouble before a response is formed.
- **Docs.** design.md §4 and test-plan.md:35 still match the code. design.md §11's "Upload hardening" row overstates the protection in one respect (metadata is outside both caps) — that is G2-C1, not a separate finding.
- **File ownership.** FZ2.1 touched only its two declared files, plus the `test-evidence.md` correction B-29 asked for.

### Tests
- [x] **All tests passing.**
  - `cd /Users/calebschwarz/Documents/Hunting && .venv/bin/pytest -q` → exit **0**, **3025 passed, 2 skipped, 1 xfailed** in 214 s, first run, no re-run needed and no flake observed.
    - Skips (declared): `test_scripts_syntax.py` shellcheck-not-installed and the real-photo gate (B-19/B-21).
    - The xfail is `tape_cross_multi_aim` (G2-S30). The only warning is the lead-accepted Starlette httpx deprecation.
    - +27 tests versus cycle 3's 2998, which matches the FZ2.1 Done note.
  - FZ2.1's `Run:` re-run by me: `.venv/bin/pytest tests/unit/test_image_io.py -q` → exit 0, **140 passed**, matching the Done note.
  - `make lint` → exit 0 (`ruff check .` "All checks passed!", `bash -n` on every script, shellcheck NOT INSTALLED, declared). `ruff format --check` on both changed files: "2 files already formatted".
  - `./scripts/scan.sh` → exit 0: `BANDIT: scanned 5677 lines; high=0 medium=0 low=0`; `PIP-AUDIT: audited 21 pinned packages; vulnerable packages=0`.
  - No residue: no `-m rga` processes, no `RGA_DATA_DIR` processes, no `rga-live-*` temp dirs. Both reviewed files are sha256-identical to their pre-review state.
- [ ] **Test coverage adequate.** Not fully. The G2-W17-R2 guard is well covered and mutation-proven. Three gaps remain, all pre-existing: no test bounds metadata-driven memory (G2-C1), no test covers a malformed tag sorted before Orientation (note 1), and `struct.error` is unpinned (note 4).
- **Tier coverage.** The Group 2 gate is T1 + T2 + accuracy (test-plan §6); all of it ran green. FZ2.1 is T1, which is the right tier for it. T3 (`smoke.sh`) is still due in Group 3, and the real-photo gate is still outstanding and declared (B-19, B-21) — neither is claimed as passed here.
- **No assertions on mocks.** The new tests assert on real decoded pixels and real exception types from real byte inputs. No monkeypatching in the new section.
- **Completion claims versus what I reproduced.** Every number in the FZ2.1 Done note and in `test-evidence.md:691-698` reproduces: 140 passed, the full suite at 3025/2/1, review-18's fuzz 1,040 → 0, the per-trigger tests, and the 18/18 mutant kill (my own 16-mutant set agrees, with 3 equivalents I classify as such rather than as kills). The `test-evidence.md:662` correction is accurate. No claim in this fix is overstated.

### Verdict
Verdict: FAIL — 1 Critical (G2-C1), 0 Warning, 7 Suggestion (not counted).
Reason: G2-W17-R2 is fully resolved and nothing regressed, but `io.py` lets one upload inside the 40 MB / 60 MP caps drive EXIF parsing to multi-gigabyte, effectively unbounded allocation (measured 2.5 GB from an 8 MB file; > 4.5 GB from a 20 MB file and still climbing when stopped) — a pre-existing hole that meets the Critical bar the lead set for pre-existing in-file issues.

**Group 2 is BLOCKED and returns to the user.** B-29 authorised exactly one extra cycle and this was it; no further fix or review cycle opens in this scope without new authorization from the user, which only the user can give. The findings, reproducers (`/tmp/rv19/`) and evidence above are preserved for whatever the user decides. For the record, so the decision is not misread: **FZ2.1 itself did its job** — the work B-29 authorised is complete and correct, and the blocking finding is older than it and independent of it. What the user decides about G2-C1 (fix it in Group 2, route it to Group 3 with the T3.3 upload work, or accept it as a documented limitation of a loopback single-user app) is theirs to choose.

## Group 3 — Cycle 1

Date: 2026-10-02 · Reviewing: Group 3, all 19 tasks (T3.00, T3.0, T3.0d, T3.1–T3.11 and the a/b/z follow-ups) · Scope/cycle: **Group 3, cycle 1 of 3** (B-07). Cycles 2 and 3 remain in this scope.

Synthesizer: review-20. My own slice was T3.00/SE-2, T3.0d, T3.9/T3.9a, the cross-module surface (`web/app.py`, `security.py`, `schemas.py`, `static/js/api.js`, the route registry, error mapping, masking single-source, CSP, no-egress, layering, D-14, docstrings) and the full-suite and T3 gates.

Analysts (all three reported before this verdict was written):
- review-21: photos (T3.3), review editor (T3.4/T3.4z), truth export (T3.10).
- review-22: sessions (T3.2), zeroing (T3.7/7a), masking (T3.7b/T3.11), T3.0.
- review-23: rifles/dashboard (T3.1), analysis (T3.5/T3.5a), compare (T3.6), dope (T3.8).

Coordination: degraded mode (B-01) — no task tools, no sentinels. Ground truth is the disk.

### Method
- **Every cited line was re-read on current disk.** No finding below is quoted from a message or a remembered snapshot; each analyst Warning was re-verified here before being counted, and the two findings I could not reproduce are marked as such.
- **Scratch only.** Mutation and bypass probes ran under `/tmp/rga-review20-*` with `PYTHONDONTWRITEBYTECODE=1`; the repo was never edited, and no stray file was left in it.
- **Gates run by me:**
  - Full suite: `cd /Users/calebschwarz/Documents/Hunting && .venv/bin/pytest -q -p no:cacheprovider` → exit **0**, **3986 passed, 2 skipped, 1 xfailed** in 880.71 s. Matches the expected baseline exactly.
  - T3 smoke: `./scripts/smoke.sh` → exit **0**, **36 PASS / 0 FAIL / 0 SKIPPED**, including the round trip (ES 1.2240 in vs truth 1.2304), the bind/denial/egress checks and PID/PGID teardown with no residue.
  - **Verify the verifier:** on a scratch copy of the smoke tree I tightened `ES_TOLERANCE_IN` 0.05 → 0.001; the script reported `[6.15] FAIL` and exited **1**. The gate can fail.
- **Probes:** a masked-group leak sweep over every read payload and page; a 16,564-request malformed-input sweep over every registered route; EXIF amplification measured with `tracemalloc` and with a child-process RSS watchdog (review-19's `/tmp/rv19/exif_amplification.py` plus my own container builder driven from the repo's own test helpers).
  - The leak sweep initially returned 0 findings because `TestClient` sends `Host: testserver`, which the Host guard answers with 400 — a vacuous pass. I caught it with a power check (the same sweep over an *unmasked* group must produce hits), fixed the base URL, re-ran, and only then trusted the result. The power check then produced 52 hits on the unmasked control.
- **Plugin pass:** `feature-dev:code-reviewer` on my slice. Its two findings are merged below at the severity the evidence supports; one is upheld, one is partly upheld and re-rated.

### Spec Alignment
- **T3 gate (test-plan §6, spec §8).** Met: `smoke.sh` is green against the real `python -m rga` process over a real loopback socket, and the gate is mutation-proven. T3.9a's two-way config surface holds: `rga.config` reads exactly `RGA_DATA_DIR`, `RGA_HOST`, `RGA_PORT`, `RGA_LOG_LEVEL`, and those are exactly what `run.sh`, `smoke.sh`, the Makefile and `live_server.py` set.
- **Route registry vs reality (design §4.7).** Exact match: 40 `/api` routes plus `/healthz`; after normalising path-parameter names, `ROUTE_SCHEMAS` and the discovered routers agree with **no** route missing on either side, and every route's `response_model` is the registry's model. The 11 pages are the §4.7 list. Every `/api/...` path in `static/js/*.js` resolves to a registered route, and no page script calls `fetch` directly.
- **Masking has a single source (B-43).** `masks_poi` is defined once, at `domain/zeroing.py:45`, and re-exported at `services/groups.py:80`. No second copy of the rule exists in services, routes, templates or JS.
- **No POI leak for masked groups.** My sweep over `zero_check(2)`, `zero_step2(6)`, `zero_step1(5)`, `zero_confirm(9)` and a draft `zero_step2(6)` found **0** leaks across `GET /api/groups/{id}`, `/stats`, the group list under four filters, session detail, search, comparison, dope, aggregate, the zero payload, and the `/groups/{id}`, `/sessions/{id}`, `/analysis`, `/compare`, `/dope`, `/zeroing/{id}` pages: `masked: true`, `aim: null`, all four per-shot offset columns null, size-only stats, and no mil value, offset, direction word or aim-derived number anywhere. **But** that holds only while the group keeps its zeroing purpose — see G3-W4.
- **D-14.** No `mean radius`, `best of`/`best N` or `group reduction` identifier or label anywhere in `src/` (Python, JS, templates, CSS). The only `best` hits are an unrelated local in `review.js:113` and loop variables in `reference_circle.py`.
- **Shot removal.** `not_a_bullet_hole` is the only reason in the enum, the DB CHECK, the schema (`schemas.py:680-685`) and the UI button text (`review.html:44`). No other reason is reachable.
- **No numeric correction from a 2–9 shot group outside Step 1.** Upheld in the domain and the advice payloads. Not upheld with respect to **distance** — see G3-C2.
- **No outbound network, no CDN.** No HTTP-client import in `src/` (`urllib.parse.quote` is the only `urllib` reference, used for a header). No `http(s)://` in templates or static assets; smoke check 8 confirms the running process holds only loopback connections.
- **CSP.** No inline `<script>`/`<style>`, no `style=`, no `on*=`, no `javascript:`, no `eval`/`new Function`, no `setAttribute('style')`, no `innerHTML`/`insertAdjacentHTML`/`document.write` in any template or page script. All 10 scripts are external ES modules; CSS is two local files. No native form POST.
- **Layering (B-34).** `rga.web.*` imports no `rga.db` and no `rga.vision`; `web/deps.py` remains the single sanctioned exception. `domain` imports nothing from other layers.
- **Docstrings and prose.** No public name in the Group 3 files lacks a docstring (the gate covers `src/`, `tools/`, `tests/support`). Comment density is very low (0–12 comment lines per file) and I found no why-prose essays; the longest docstrings (`live_server.py` module, 32 lines) are usage contracts, which is the right content.
- **Upload hardening (design §11, threat (c)).** The error contract is intact — nothing escapes `load_oriented` as a non-`ImageRejected` exception on the EXIF paths. The **resource** promise is not met: see G3-C1.

### Critical

**G3-C1. SE-2 / G2-C1 is NOT closed. One upload inside the 40 MB / 60 MP caps still drives EXIF parsing to multi-gigabyte allocation, by two independent routes. SE-2 cannot be closed.** [own; the carrier-bypass half was also flagged by feature-dev:code-reviewer at 88%]

T3.00's 256 KB cap is real, well-tested and kills every shape review-19 recorded — I re-ran review-19's own reproducer against current `io.py` and all of them are now rejected in 0.00 s at 50–140 MB RSS (they previously reached 2.5 GB+). The defect is that the cap bounds **metadata bytes**, while the allocation is **metadata bytes × IFD entry count**, and the entry count is never bounded.

- **(a) Residual in-cap amplification — no bypass needed, the file is fully compliant.** A payload *under* the cap with many IFD entries loads successfully and allocates gigabytes. Measured with `tracemalloc` via the repo's own test helpers (`_amplifying_tiff`, `_png_with_chunks`, `_jpeg_with`, `_heif_with_exif`):

  | Container | Metadata | Entries | Outcome | Peak |
  |---|---|---|---|---|
  | PNG `eXIf` | 250 KB (cap 256 KB) | 300 | loaded | 74 MB |
  | PNG `eXIf` | 250 KB | 2,000 | loaded | 490 MB |
  | PNG `eXIf` | 250 KB | 4,000 | loaded | 978 MB |
  | JPEG multi-APP1 (no density) | 250 KB | 4,000 | loaded | 978 MB |
  | JPEG multi-APP1 (JFIF density) | 250 KB | 4,000 | loaded | 978 MB |
  | HEIF | 250 KB | 4,000 | loaded | 978 MB |
  | PNG `eXIf` (child + RSS watchdog) | 250 KB | 10,000 | **loaded** | **2,551 MB RSS** |

  The file in every row is about 0.2 MB. Peak tracks `payload × entries` linearly. IFD0's count field allows 65,535 entries, so a single cap-compliant 250 KB file extrapolates to roughly 16 GB of nominal demand; I stopped at a measured 2.5 GB rather than drive the host into swap, so the top of that range is an **inference from the measured linear growth**, not an executed observation. The 2.5 GB is a fact.
- **(b) The JPEG pre-scan can be bypassed outright.** `src/rga/vision/io.py:107-108` — `if length < 2: break` — aborts the whole marker walk at the first segment whose declared length is 0 or 1. Pillow tolerates such a segment and keeps parsing, then concatenates the later `Exif\0\0` APP1 segments. So the scanner returns **0 bytes** while Pillow parses megabytes. Measured, with a 4-byte `\xff\xe2\x00\x00` (APP2, declared length 0) inserted before the Exif segments:

  | Payload | Entries | File | Pre-scan counted | Pillow's `info["exif"]` | Outcome | Peak RSS |
  |---|---|---|---|---|---|---|
  | 1 MB | 300 | 1.0 MB | **0 B** | 1 MB | ImageRejected | 411 MB |
  | 4 MB | 300 | 4.0 MB | **0 B** | 4 MB | ImageRejected | 1,293 MB |
  | 8 MB | 300 | 8.0 MB | **0 B** | 8 MB | **killed by my watchdog at 2,592 MB, still allocating** | > 2.5 GB |

  `\xff\xe2\x00\x01` (length 1) and `\xff\xfe\x00\x00` (COM, length 0) bypass identically. These files are still *rejected* — the post-open check at `io.py:274` sees the carrier in `img.info` — but only **after** the allocation has happened, so the error contract holds and the resource promise does not. (The plugin also predicted a bypass via the length-less markers `0xC8`/`0xF0`-`0xFD`; I tested that construction and the scanner counted the full 4 MB correctly, so **that half did not reproduce** and I am not counting it.)
- Reachability, stated plainly: loopback-only, `OriginGuard` blocks cross-origin writes, so the trigger is the user importing a crafted file or a local process posting one. No data is exposed and nothing is written; the impact is availability. A phone photo cannot trigger it by accident.
- Why Critical: this is the same finding, at the same bar, that the lead and the user set in B-29/B-30 — "memory exhaustion from a single uploaded file within the app's 40 MB / 60 MP limits". T3.00 was the task created to close it, and `security-exceptions.md` SE-2 says it closes "when Group 3 review verifies it". It does not verify.
- Fix scope (`src/rga/vision/io.py`, `tests/unit/test_image_io.py`): bound the **work**, not just the bytes — the bounded orientation-only IFD reader that already exists as `_ifd0_orientation` (`io.py:187-208`) does exactly this and never materialises an out-of-line value, so the fix is to stop calling `img.getexif()` for orientation at all and read every container's carrier through `_ifd0_orientation` (plus a cap on the entry count). Separately, make `_jpeg_metadata_bytes` mirror Pillow's tolerance instead of `break`ing on a short length. Tests that would fail without it: the table rows above, asserting a bounded `tracemalloc` peak (the existing `PEAK_LIMIT_BYTES = 64 MB` is the right bar) for a cap-compliant 250 KB × 4,000-entry carrier in each container, and for the length-0-marker JPEG.
- **SE-2 cannot be closed. It should stay open in `security-exceptions.md`.**

**G3-C2. A zero can be judged, dialed and completed from a session that is not 100 yd, producing a numeric scope correction that is wrong by the distance ratio.** [via review-22; independently reproduced by me]

Spec §4.6 says "All zeroing sessions must be at 100 yd" and T3.7's acceptance criterion says "Only 100 yd sessions are allowed". The guard `_require_100_yd` (`services/zeroing.py:397-402`) is called only from `link_group` (`:436`) and `run_zero_check` (`:608`). Judging converts at `ZERO_DISTANCE_YD` unconditionally (`services/zeroing.py:168-172`) and never re-reads the session. Three paths reach it; I reproduced all three against the real app:

| Path | Result |
|---|---|
| (a) Link a 10-shot group at 100 yd (advice `Dial DOWN 0.3, LEFT 0.3`), then `PATCH /api/sessions/{id} {"distance_yd": 300}` — the exact body `session.js` sends | PATCH 200; advice still **`Dial DOWN 0.3, LEFT 0.3`**; `POST /dialed` **200**, state → `step2_confirm` |
| (b) `POST /api/sessions/{300 yd}/groups {"purpose":"zero_step2","zero_id":Z}` | **201**, then judged with a numeric correction |
| (c) The same 300 yd group that `POST /api/zeros/{id}/groups` refuses with **422 `invalid_distance`** is accepted by `PATCH /api/groups/{id} {"purpose":"zero_step2","zero_id":Z}` | PATCH **200**; advice **`Dial DOWN 0.3, LEFT 0.3`** |

In path (a) the app then shows two contradictory numbers for the *same* group: the zeroing page says `Dial DOWN 0.3, LEFT 0.3` while `GET /api/groups/{id}/stats` reports `mean_dy_mil 0.0958`, `mean_dx_mil 0.1042` — a 3× difference, because the stats route uses the session distance and the zero does not. review-22 also drove this through to a **completed** zero with a dot suggested from an ES recorded at 300 yd.

- Why Critical: it is reachable from the UI, it silently corrupts the one number the product exists to produce (what the user dials into their scope), and it breaks an explicit spec constraint and acceptance criterion. It is not an AC-5 shot-count violation — the group has 10 shots — so it is a distinct defect from the "must-never".
- Fix scope (`src/rga/services/zeroing.py`, `src/rga/services/sessions.py`, `tests/integration/test_api_zeroing.py`, `tests/integration/test_api_sessions.py`): re-check the session distance at judge time and refuse `dialed`/`dot` (a fixed-template INVALID advice, no number and no direction word), and close the create/PATCH linking route (shared with G3-W4). Tests for all three paths.

### Warning

**G3-W1. Any id ≥ 2**63 from a client returns HTTP 500 across most of the app.** [own; independently reported by all three analysts]
- Cause: `sqlite3` raises `OverflowError: Python int too large to convert to SQLite INTEGER`, which no handler maps. Confirmed by traceback at `repo_rifles.py:128`, `repo_sessions.py:223/292/397/428` and others.
- My sweep: **16,564 requests, 624 responses ≥ 500**, every one of them this class. Affected: `GET/PATCH /api/rifles/{id}`, `/api/rifles/{id}/loads`, `/api/loads/{id}/lots`, `/api/loads/{id}/dope`, `GET/PATCH /api/groups/{id}`, `/api/groups/{id}/stats`, `POST /api/groups/{id}/detect|fill-shortfall|finalize`, `PUT /api/groups/{id}/review`, `GET/PATCH /api/sessions/{id}`, `POST /api/sessions/{id}/groups`, `GET /api/photos/{id}/image`, `POST /api/photos/{id}/reference/confirm`, the queries `?session_id=`, `?rifle_id=`, `?load_id=`, body ids (`POST /api/aggregate {"group_ids":[2**63]}`, `PATCH /api/groups/{id} {"photo_id": 2**63}`, `PUT .../zero-load`), and the pages `/rifles/{id}`, `/sessions/{id}`, `/groups/{id}`, `/groups/{id}/review`, `/photos/{id}/reference`. `2**63-1` correctly gives 404.
- Not affected, and the model for the fix: `routes/zeroing.py:40-41` and `routes/compare.py:37` bound their path ids with `Path(ge=1, le=SQLITE_MAX_INTEGER)`.
- Root: `schemas.py:50` `PositiveId = Annotated[int, Field(ge=1)]` has no upper bound, and most route modules take a bare `int` path parameter.
- Breaks the group3-brief rule "never a 500". Precedent for the rating: G2-W17/W17-R2 (a 500 where a 4xx belongs) were Warnings.
- Fix scope (`src/rga/web/schemas.py` — lead-owned, all of `src/rga/web/routes/*.py`, plus a new registry-driven test): one bounded `PositiveId` and one shared path-id type, and a parametrised test that hits **every** `{id}` route with `2**63` and asserts 4xx. An `OverflowError` arm in the global handler is a reasonable backstop but should not be the only fix.

**G3-W2. `POST /api/photos` with no body and no `Content-Type` returns 500.** [own]
- `OriginGuard` deliberately lets a bodyless unsafe-method request through (`security.py:91-98`, correct on its own terms), and `_read_upload` then constructs `MultiPartParser(request.headers, ...)` at `routes/photos.py:136-140`, which raises `KeyError: 'Content-Type'` before any `MultiPartException` can be mapped.
- Reproduced: `POST /api/photos` with no body → `KeyError` → 500 `internal_error`. The same request *with* `Content-Type: application/json` correctly gives 422 `invalid_upload`.
- Fix scope (`src/rga/web/routes/photos.py`, `tests/integration/test_api_photos.py`): require a multipart content type up front and return 415, and add the no-body case to the photos denial tests.

**G3-W3. A PNG with an empty `iCCP` chunk after IDAT escapes `load_oriented` as `IndexError`, giving 500 on upload.** [via feature-dev:code-reviewer (85%); reproduced by me]
- `img.load()` (`io.py:271`) runs Pillow's `load_end`, which calls the PNG chunk handlers for post-IDAT chunks; `chunk_iCCP` indexes past the end of a zero-length payload. `IndexError` is in neither `_DECODE_ERRORS` nor `_PLUGIN_DECODE_ERRORS` (`io.py:29-37`, `:52`), and `routes/photos.py:55-65` does not catch it.
- Reproduced both ways: `load_oriented` → `ESCAPED IndexError: index out of range`; `POST /api/photos` → **500**. The identical chunk placed *before* IDAT is handled correctly (415). No CRC check protects the post-IDAT path.
- This is exactly the G2-W17-R2 class — the `io.py` docstring promise "Every decoder or limit failure surfaces as `ImageRejected`" (`io.py:3`) — and is rated Warning for the same reasons it was then: fails closed, nothing stored, generic body.
- Fix scope (`src/rga/vision/io.py`, `tests/unit/test_image_io.py`): add `IndexError` to the decode catch (or catch `Exception` around `Image.open`/`img.load()` excluding `MemoryError`), plus a regression test per post-IDAT chunk shape.

**G3-W4. `POST /api/sessions/{id}/groups` and `PATCH /api/groups/{id}` set `purpose` and `zero_id` freely, bypassing every zeroing guard — including the masking rule.** [via review-22; reproduced by me]
- `services/sessions.py` `GROUP_PATCHABLE` includes `purpose` and `zero_id`, so the state-machine guards in `link_group`/`detach_group`/`run_zero_check` (zero state, 100 yd, Step 1 limit, out-of-order, linked-elsewhere, zero-check immutability) are all optional.
- I reproduced the masking consequence end to end: a 2-shot group PATCHed to `zero_check` correctly reports `masked: true, aim: null`; PATCHing it **back** to `general` returns 200 and the same group then reports `masked: false`, `aim: {4.25, 5.5}` and full `mean_dx_mil`/`mean_dy_mil` — a POI for a 2-shot zero check, which B-18 exists to forbid. review-22 additionally reproduced (E) a complete zero being pushed back to `CONFIRM_AGAIN` with a correction, and (F) a zero wedged permanently in `step2_confirm` by orphaning its initial group with `zero_id: null`.
- The UI never sends these fields, so this is API-only — but it is the same surface the masking guarantee rests on, and B-33's "full `GroupUpdate` semantics" is what granted it.
- Fix scope (`src/rga/services/sessions.py`, `tests/integration/test_api_sessions.py`, and a lead note narrowing B-33): refuse `zero_id` changes and `purpose` changes to/from zeroing purposes on zero-linked or checked groups, or route them through `services.zeroing`.

**G3-W5. Two co-located estimated shots can never both be removed; the editor dead-ends on 409.** [via review-21; reproduced by me]
- With no ragged blob and a shortfall ≥ 2, `resolve_shots` stacks estimated shots at identical coordinates (reproduced on `clean_circle1` with `shots_expected = holes + 2`: both estimated shots at exactly `(5.096977, 7.949740)`).
- `_new_rejections` (`services/groups.py:228-236`) de-duplicates rejections **by coordinate**, so a second rejection at the same point is discarded; the count rule at `:274-277` then sees more dropped shots than fresh rejections and raises `shot_dropped_without_reason`. Reproduced: removing one of the pair → **409**; sending two rejections at the same point → **409** ("3 stored shot(s) are missing and only 1 new rejection(s) were given").
- Workaround is non-obvious (drag the shot first, then delete). Fix scope (`src/rga/services/groups.py`, `tests/integration/test_service_groups.py`): count rejection multiplicity — each request rejection consumes at most one unmatched stored rejection — keeping repeated PUTs idempotent.

**G3-W6. The B-38 reference lock has no escape from the UI.** [via review-21; reproduced by me]
- Reproduced: confirm the wrong circle candidate → run one detect with a region → re-confirming the correct candidate gives **409 `reference_locked`**; `PUT review` with `shots: []` and `aim: null` returns 200 but leaves `roi_json` set, so the re-confirm is **still 409**. The lock predicate (`services/photos.py:270-271`) tests `roi_json IS NOT NULL OR aim_x_in IS NOT NULL OR status = 'final'`, and only a `PATCH photo_id` clears the ROI — which no page calls.
- Picking the wrong candidate is a realistic first move: per T2.2 the true circle is at rank 0 or 1 and a 1 in aim dot looks like a 1 in reference circle. The reference page still shows "Confirming again replaces it", which is now false.
- Fix scope (`src/rga/services/photos.py` or `src/rga/services/review.py`, `reference.html`, `tests/integration/test_api_photos.py`): clear the ROI when a review PUT stores no shots and no aim, or add an explicit "reset this group's region and aim" action; show the lock state on the page.

**G3-W7. Lowering the expected count and re-running Fill shortfall dead-ends the editor on repeated 409s.** [via review-21, reproduced by them in real Chromium against a real server; mechanism verified here by file:line — my own API-level replay did not reproduce it, see below]
- Mechanism, read on disk: `fillShortfall()` (`review.js:257-260`) and `detect()` (`:247-255`) call `save()` first, then `mergeProposal()`. `mergeProposal` (`review.js:187-195`) keeps only shots that are `manual` or (`moved_by_user` and not `estimated`) and adds the proposal's shots, **without adding a rejection for any stored shot it drops**. The next `save()` or `finalize()` therefore omits stored shots with no matching rejection and the server's count rule (`groups.py:274-277`) refuses it with 409 `shot_dropped_without_reason`. Recovery needs a page reload.
- **My re-run did not reproduce it** and I say so plainly: I replayed the sequence through the API on `clean_circle1`, where fill returns all 5 detected shots regardless of the lowered expected count, so no shot is dropped and the save returned 200. review-21 used `ragged_10x7`, where fill honours `shots_expected` and returns fewer shots than are stored — which is the condition the mechanism needs. The fixture difference explains the divergence; it does not contradict their result. I count it on their browser reproduction plus the code path above, and flag it as the one finding here whose end-to-end trigger I did not execute myself.
- Fix scope (lead decision on AC-10 semantics, then `src/rga/web/static/js/review.js` and/or `src/rga/services/groups.py`, plus an API-sequence test on a ragged fixture).

**G3-W8. A dope test asserts something the page can never render (verify the verifier).** [via review-23; reproduced by me]
- `tests/integration/test_api_dope.py:289` asserts `f"group {group.id}" not in page.text`. `/dope` never renders a group id at all — `dope.js` draws rows from the API and `dope.html` has no group-id column. I confirmed it with a group that **is** in the dope rows: `group_id` 5 is in the API rows and `"group 5" in page.text` is `False`.
- So the "and the page" half of T3.8's criterion ("an end-to-end test that such groups never appear on the dope page") is carried entirely by the API half plus a manual browser check. The underlying property does hold.
- Fix scope (test-only, `tests/integration/test_api_dope.py`): drop the vacuous clause and rename the test, or add a real browser assertion (Playwright browsers are present), or declare the page half deferred to T4.8 in `test-evidence.md`.

**G3-W9. `tier30` is unreachable for the workflow the spec's own example describes.** [via review-23; verified here by reading]
- P-13/B-04 define tier30 as "groups **or saved aggregates** with ≥30 shots". No saved-aggregate entity exists in design §5 or §4.7, and `services/compare.py:199` tiers single groups only (`tier_for(stats.n)`). A user shooting Form's 3×10 (AC-4's own example) therefore never has tier30 data, and every tier30 pair is `insufficient_data` unless they record one ≥30-shot group.
- This is a planning gap between spec P-13 and design, not an implementer error — T3.6 implemented the design, and the UI text is honest. It is a deviation from a settled user decision (B-04, Q-4) that nothing in `decisions.md` justifies, which is why it is a Warning rather than a Suggestion.
- Fix scope (lead/user decision logged in `decisions.md`; then either an amendment to P-13 or pooling aim-aligned same-session tier10 groups in `src/rga/services/compare.py`).

### Suggestion
Recorded, not counted. The analysts' full lists are in their reports; these are the ones I consider worth keeping.

**Cross-module [own]:**
- S1. `tests/smoke/` is outside the docstring gate's `GATED_ROOTS` (`tests/unit/test_types.py:112`), although `smoke_roundtrip.py` is documented anyway.
- S2. The 503 `busy` detail text differs per route module ("try again", "try again in a moment", "wait a moment and try again") and only `analysis`, `compare` and `sessions` send `Retry-After`. `photos.py:63` matches `"locked"` case-sensitively while every other module lowercases first.

**Photos / review editor [via review-21]:** `PUT review` accepts shots and an aim on an unconfirmed reference, after which `/stats` reports mil values computed from unscaled inches (S1); the finalize response still carries the aim for a masked group (S2); a PUT carries no reference/frame identity, so a stale tab can save coordinates for a replaced reference (S3); manual reference marks outside the image and absurd ppi are accepted (S4); `export_truth._stage` opens a predictable `.part-<pid>` file without `O_EXCL|O_NOFOLLOW` (S5); the T3.10 "read-only db" claim is inaccurate — `connect()` opens read-write and sets WAL (S6); 415 denial is tested only on `PUT review` among the 8 photo/review writes (S7); no `Cross-Origin-Resource-Policy` header (S8); a late `propose` response can repopulate candidates for a superseded reference kind (S9); a sub-8 px region drag leaves a fractional ROI that detect rejects with 422 (S10).

**Sessions / zeroing [via review-22]:** nested `_write_transaction` commits the caller's outer transaction (S1); `poi_text` uses `round()` while corrections use `round_tenth`, so 0.15 mil reads "0.1 mil high" beside "Dial DOWN 0.2" (S2); the zero-check candidate list offers this zero's own Step 1 group, which then 409s (S3); success flashes are hidden immediately by `render()` (S4); `dialedTouched` is never reset (S5); `dialed` in step1 accepts an aim-less INVALID group (S6); `/dot` on a superseded zero gives a misleading message (S7); the `summaries` module docstring omits the Step 1 > 3 case (S8); zeroing writes have no 415 test and the 503 test monkeypatches instead of taking a real lock (S9); the D-14 scan still misses ` `, `&#x00A0;`, `&#160` without a semicolon, `&ensp;`/`&thinsp;` and labels split across tags (S10); `STEP1_MAX_SHOTS` is redefined rather than imported, and private helpers are imported across modules (S11).

**Rifles / analysis / compare / dope [via review-23]:** `analysis.js:79-86` re-implements the server's direction rounding with `Math.round` and disagrees on negative halves (S1); `GET /api/search?q=%00` matches everything because SQLite's LIKE truncates at NUL (S2); masking docstrings in `services/analysis.py:4-6` and `services/compare.py:4-6` are stale after B-41/B-43 (S3); a conditional skip in `test_api_analysis.py:672` could hide a routing regression (S4); repeated `load_ids` query parameters are silently dropped (S5); a rifle with >20 loads 422s on opening `/compare` (S6); signed inches render as "-0.30 in right" (S7); multi-line notes collapse without `white-space: pre-wrap`, weakening B-16's "verbatim" (S8); one dope test asserts only that the seed helper finalized (S9); aggregate overlay circles use a fixed radius rather than the bullet diameter (S10); no small-sample cue on dope rows (S11); route modules import mappers from sibling route modules (S12).

### Cross-Task Consistency
- **The "never a 500" rule is the one systemic gap.** Three independent causes (G3-W1 huge ids, G3-W2 bodyless upload, G3-W3 post-IDAT chunk) all land on the same brief rule. G3-W1 spans six slices and must be fixed once, centrally, with a registry-driven sweep test — not per slice. My 16,564-request sweep is a ready template for that test.
- **Masking is consistent where purposes are stable, and only there.** The single-source refactor (T3.11/B-43) holds: one definition, no stale copies, and no leak in any read payload or page. G3-W4 is the hole — the rule is enforced on *current* purpose, and purpose is freely writable through a non-zeroing route. Any fix should keep `masks_poi` as the only rule and close the write path instead of adding a second check.
- **The masked-aim rule has two accepted exemptions that B-39/B-43 do not name.** The `PUT review` response (by design — the editor adopts the aim, `review.js:224`) and the `POST finalize` response (not used by the editor) both carry `aim` for a masked group. Both analysts raised it independently. The behaviour is defensible; the decision text is incomplete. Suggest the lead amend B-39 to name them, and consider nulling it in the finalize response.
- **`repo_dope` keeps a SQL copy of the exclusion rule** (`repo_dope.py:22-24`) alongside `masks_poi`. It agrees today and is deliberately stricter per B-17, but it is a second place to edit. Noted against B-43's "one rule", not raised as a defect.
- **Interface contracts hold.** The route registry, the JS client surface, the config surface and the `api.js` upload field name all match their design-§4.7/§9 contracts exactly, in both directions.
- **Completion claims vs what I reproduced.** Every Group 3 `Run:` command I re-ran matches its Done note, and the full-suite and smoke numbers in the task notes reproduce exactly. Two wording corrections: T3.00's Done note says "SE-2 closes when Group 3 review verifies it" — it does not verify (G3-C1); and T3.10's "read-only db" is inaccurate (review-21 S6). The T3.9 Done note's declared deviations (LAN check over all non-loopback IPv4, the PATCH attach step, read-write `integrity_check`) are accurate and were accepted in B-40.

### Tests
- [x] **All tests passing.** `.venv/bin/pytest -q -p no:cacheprovider` → exit **0**, **3986 passed, 2 skipped, 1 xfailed** (880.71 s), matching the expected baseline. Both skips are declared and re-confirmed by me: `test_scripts_syntax.py:50` "SHELLCHECK: NOT INSTALLED" and `test_eval_real_photos.py:932` "real-photo gate outstanding". The xfail is `tape_cross_multi_aim` (G2-S30). The only warning is the accepted Starlette/httpx deprecation.
- [x] **T3 gate met.** `./scripts/smoke.sh` → exit 0, 36 PASS / 0 FAIL / 0 SKIPPED, and the gate itself fails when mutated.
- [ ] **Test coverage adequate.** No. Each of the nine findings above is a behaviour no test exercises:
  - No test bounds metadata-driven memory for a **cap-compliant** carrier, so G3-C1(a) passes the whole suite; no test covers a short-length JPEG marker, so G3-C1(b) does too. The existing `TestMetadataSizeCap` is strong on the shapes it covers and blind to entry count.
  - No test hits any route with an id ≥ 2**63 (G3-W1); the two slices that bound their ids do so without a test that would fail if the bound were removed.
  - No test covers a bodyless upload (G3-W2) or a post-IDAT chunk shape (G3-W3).
  - No test links a zeroing group through `POST groups`/`PATCH group`, or changes a session's distance after linking (G3-C2, G3-W4).
  - No test removes a co-located shot pair (G3-W5), clears a reference lock (G3-W6), or lowers the expected count before a refill (G3-W7).
  - One dope assertion is vacuous (G3-W8).
- **Tier coverage (testing-standards).** T1 and T2 are strong across the group. **T3 ran and passed.** T4 is Group 4's scope and is not claimed here; the page scripts (`session.js`, `zeroing.js`, `analysis.js`, `compare.js`, `dope.js`) are currently covered only by static source checks plus manual browser passes, which is declared in the task notes and must be closed by T4. The real-photo gate remains outstanding and declared (B-19/B-21) — review-21's W4 (the truth exporter writes unmoved app-proposed detections as truth, making parts of that gate self-referential) is a real "verify the verifier" concern for whoever owns the post-Group-3 tuning task; I record it here rather than counting it, because it affects a gate that has not run.
- **No assertions on mocks.** Spot-checked across the Group 3 test files: the monkeypatches inject faults (a locked database, a failing service call) or act as call spies that delegate to the real function; the assertions read real SQLite state, real decoded pixels, real HTTP responses or real process state.

### Verdict
Verdict: FAIL — 2 Critical (G3-C1, G3-C2), 9 Warning (G3-W1–W9), 33 Suggestion (not counted).
Reason: T3.00 did not close G2-C1 — a cap-compliant 250 KB carrier still allocates 2.5 GB, and a 4-byte marker bypasses the JPEG pre-scan entirely — so **SE-2 cannot be closed**; and a zero can be judged, dialed and completed from a non-100 yd session, producing a scope correction wrong by the distance ratio.

This is cycle 1 of 3 for Group 3. Cycles 2 and 3 remain, so the normal loop applies: one scoped fix wave covering the 2 Criticals and 9 Warnings, re-verified by cycle 2. Suggested fix-wave grouping, by file ownership: (1) `vision/io.py` + `test_image_io.py` — G3-C1, G3-W3; (2) `services/zeroing.py` + `services/sessions.py` — G3-C2, G3-W4; (3) `web/schemas.py` + all `web/routes/*.py` + a new sweep test — G3-W1, G3-W2; (4) `services/groups.py`, `services/photos.py`, `review.js` — G3-W5, G3-W6, G3-W7; (5) test-only — G3-W8; (6) a lead/user decision logged in `decisions.md` — G3-W9.


## Group 3 — Cycle 2

Date: 2026-10-02 · Reviewing: Group 3 fix wave FG3.1–FG3.7 (B-44) against cycle 1's 2 Criticals and 9 Warnings, plus regressions in the changed files · Scope/cycle: **Group 3, cycle 2 of 3**. Cycle 3 remains and is terminal. The B-48 rate-limit interruption did not consume a cycle.

Synthesizer: review-24. My own slice was SE-2 (G3-C1) and the cross-module surface: masking single source, the never-a-500 rule, CSP, egress, layering, D-14, docstrings, plus the full-suite and smoke gates.

Analysts (all three reported before this section was written):
- review-25: io.py (FG3.1) and photos (FG3.5).
- review-26: zeroing and sessions (FG3.2), review editor (FG3.6).
- review-27: id bounds (FG3.3), saved aggregates and compare (FG3.4), dope test and truth export (FG3.7).

Coordination: degraded mode (B-01). There are no task tools and no sentinels, and the disk is ground truth.

### Method
- **Every cited line was re-read on current disk.** I reproduced each analyst Warning myself before counting it. Where I relied on an analyst's measurement instead of my own, I say so.
- **Scratch only.** Probes ran under `/tmp/rga-review24-*`. Mutation runs used `PYTHONDONTWRITEBYTECODE=1` on a scratch copy of `src/` and `tests/`, which I diffed identical to the repo before using it. The repo was never edited.
- **Processes.** PID 58042 and port 8765 were never touched. TestClient only sets a `Host: 127.0.0.1:8765` header and binds nothing. Smoke picks its own free port. Afterwards `pgrep -f -- '-m rga'` showed only 58042.
- **Not vacuous.** With TestClient's default `testserver` host, every probe got 400 from the Host guard. My probes used `base_url="http://127.0.0.1:8765"`, and the same request then gave 200. The masked-leak sweep was also run against an **unmasked** control group, where it found 40 hits.
- **SE-2 method, stated plainly.** For G3-C1 I did not build new memory-amplifying files. I verified closure in three ways:
  - I ran the repo's own tracemalloc-bounded flood tests. These are the exact shapes cycle 1 measured at 978 MB to 2.5 GB, at 4,000 and 10,000 entries, in every container.
  - I mutated `io.py` on scratch to show those tests can fail.
  - I read `io.py` and Pillow's open path statically.
  - For the one residual carrier, I proved reachability with a zero-entry probe and a spy. Its memory figure (336 MB) is **review-25's measurement**, not mine.
  - The plugin's marker-divergence finding (G3c2-W6) rests on reading `io.py` and Pillow's source only. I did not construct or run a reproducer for it.
- **Plugin pass.** I ran `feature-dev:code-reviewer` over the cross-module files. Its two findings are merged below:
  - The MPF one duplicates G3c2-W1.
  - The marker-divergence one is G3c2-W6, unexecuted.

### Gates (run by me)
- **Full suite.** `cd /Users/calebschwarz/Documents/Hunting && .venv/bin/pytest -q -p no:cacheprovider` gave exit **0**: **4309 passed, 2 skipped, 1 xfailed** in 930.62 s. This matches the baseline exactly. The skips are the same two declared ones (no shellcheck; real-photo gate outstanding), and the xfail is `tape_cross_multi_aim`.
- **T3 smoke.** `./scripts/smoke.sh` gave exit **0**: **36 PASS / 0 FAIL / 0 SKIPPED**. That includes `[3.3] schema_version == 2` and the round trip (ES 1.2240 vs truth 1.2304).
  - **The verifier can fail.** On a scratch copy I tightened `ES_TOLERANCE_IN` from 0.05 to 0.001. The script then reported `[6.15] FAIL` and exited 1.
- **io slice.** `.venv/bin/pytest tests/unit/test_image_io.py -q` gave 349 passed in 18 s. These are the same flood shapes that allocated gigabytes in cycle 1.

### Cycle-1 findings: re-verification

| Finding | Status | Evidence |
|---|---|---|
| **G3-C1** (SE-2: in-cap IFD amplification; length-0/1 JPEG bypass) | **The shapes cycle 1 found and B-44 named are closed, but SE-2 is NOT closed: a new marker-walk divergence (G3c2-C1) reopens the cap bypass, and an MPF carrier (G3c2-W1) also reaches Pillow's IFD parser.** | See below. |
| **G3-C2** (zero judged from a non-100 yd session) | **Closed** | See below. |
| **G3-W1** (ids ≥ 2^63 → 500) | **Closed** | See below. |
| **G3-W2** (bodyless upload → 500) | **Closed** | See below. |
| **G3-W3** (empty iCCP after IDAT → 500) | **Closed** | See below. |
| **G3-W4** (purpose/zero_id freely writable) | **Closed** | See below. |
| **G3-W5** (co-located estimated pair unremovable) | **Closed** | See below. |
| **G3-W6** (reference lock has no escape) | **Closed** [via review-25] | See below. |
| **G3-W7** (lower expected count + Fill → 409 dead-end) | **Closed as a dead-end** [via review-26] | A related new defect is G3c2-W3. |
| **G3-W8** (vacuous dope page assertion) | **Closed** [via review-27] | See below. |
| **G3-W9** (tier30 unreachable) | **Closed** [via review-27; cross-checked by me] | Two new defects are G3c2-W4 and G3c2-W5. |

**G3-C1 (SE-2)**
- No `getexif`, `_getexif` or `exif_transpose` call exists anywhere in `src/`.
- `io.py:332-342` opens only the stripped bytes. Orientation comes from the bounded `_ifd0_orientation` (`io.py:250-273`, `MAX_IFD_ENTRIES = 256`).
- `_strip_jpeg` rejects a declared length under 2 (`io.py:125-126`).
- The repo's flood tests pass at a peak under 64 MB and under 2 s. They cover PNG eXIf (before and after IDAT), the raw profile, JPEG multi-APP1 with and without density, and HEIF, at 4,000 and 10,000 entries, under 256 KB. The length-0/1 APP2/COM/APP0 tests pass too.
- Scratch mutants, each killed:
  - entry cap 256 → 100000: 2 tests fail.
  - stripping disabled: the spy test fails.

**G3-C2**
I replayed cycle 1's three paths against current code:
- (a) `PATCH /api/sessions/{id} {"distance_yd":300}` under a linked group → **409** `session_has_zero_groups`.
- (b) `POST /api/sessions/{300 yd}/groups` with `zero_step2` + `zero_id` → **422** `invalid_purpose`.
- (c) `PATCH /api/groups/{id}` with purpose + zero_id → **422**, and purpose alone → **422**. Linking the 300 yd group through the zero route → **422** `invalid_distance`.

Judging a non-100 yd group returns a fixed INVALID advice with no numbers (`services/zeroing.py:209-262`). review-26 also closed detach → re-distance → re-link, a zero check at 300 yd, and SQL-tampered distances at every stage.

**G3-W1**
- My sweep covered all 37 parameterised route/method pairs from OpenAPI, including the 6 pages, at 2^63, 2^64, 2^63−1, 0 and `abc`: 185 requests, **0** responses of 500 or above.
- I also sent 1,611 malformed requests over every `ROUTE_SCHEMAS` route (bad ids × bad bodies): **0** responses of 500 or above.
- review-27 independently sent 432 requests over all 55 operations: 0 responses of 500 or above.

**G3-W2**
`POST /api/photos` gave:
- no body / no Content-Type → 415
- JSON → 415
- text/plain → 415
- multipart without a boundary → 422
- a plain PNG (positive control) → 201

**G3-W3**
A PNG with an empty iCCP after IDAT gave **415** `image could not be decoded: IndexError`. The control gave 201.

**G3-W4**
- Create and PATCH accept only `general` and never `zero_id` (`services/sessions.py:307-308`, `:358-367`).
- A linked group's purpose change → 409.
- Every writer of a zeroing purpose goes through `services/zeroing._link`, which sets `zero_id` first (`:421-427`).
- review-26 swept 144 purpose × zero_id combinations: 0 accepted.
- Note, not a finding: an *unlinked* zeroing-purpose group (which can only be made by a direct DB write) can still be PATCHed to general and unmasked. No API path creates one.

**G3-W5**
- On `clean_circle1` with expected = holes + 2, both estimated shots sit at exactly (5.096977, 7.949740).
- Dropping one → 200, dropping both → 200, with 0 rejections stored. I ran this myself on scratch.

**G3-W6** [via review-25]
- review-21's path passes: wrong confirm → detect with ROI → shots + aim → 409 → clear shots and aim → re-confirm 200, ROI cleared.
- The lock is re-checked inside `BEGIN IMMEDIATE` (`services/photos.py:340`).

**G3-W7** [via review-26]
- The ragged_10x7 flow ends in Save 200 and Finalize 200 with 0 rejections.
- Over-count disables Finalize with a reason (`review.js:125-137`).

**G3-W8** [via review-27]
- A real-Chromium test of the rendered `/dope` rows has an exact-match positive control.
- `export_truth` opens `mode=ro` and bounds `--photo-id` (exit 2).

**G3-W9** [via review-27; cross-checked by me]
- review-27 exercised save → `/compare` tier30 verdict in a real browser.
- My API probe got `tier30` verdicts from saved aggregates.
- A masked member → 409 `masked_group` on both `POST /api/aggregates` and `POST /api/aggregate`.

**SE-2 can NOT be closed. It stays Open.**
- Every carrier B-44 enumerated is closed, and the specific multi-gigabyte shapes cycle 1 measured now load bounded.
- **But the cap bypass is reopened by a different vector (G3c2-C1), executed here.** `_strip_jpeg` treats the 15 JPEG extension markers (`0xC8`, `0xF0`-`0xFD`) as length-bearing and skips the segment they declare; Pillow does not, and scans into it. A crafted `FF F0 <len>` whose length spans a following APP1 `Exif` segment is skipped whole by the stripper (so the Exif is neither cut nor charged) while Pillow re-scans, finds the APP1 and parses its IFD on open. I proved this: with that wrapper, `_strip_jpeg(attack).exif is None` and the IFD-load spy fires during `load_oriented`. That defeats the 256 KB cap entirely for this carrier, which is the exact defense B-44 built, and feeds the same reader cycle 1 measured at 978 MB to 2.5 GB.
- **And a bounded residual remains (G3c2-W1).** A JPEG APP2 `MPF` segment is never stripped and also reaches Pillow's IFD parser (review-25 measured 336 MB).
- Both falsify B-44's categorical rule and the FG3.1 Done note's "no Pillow EXIF/IFD parse is reachable (asserted by a spy)".
- **Recommendation.** SE-2 stays Open and names both vectors. It closes in cycle 3 once both are fixed and verified. Accepting a bounded residual as documented is a lead decision for MPF only; G3c2-C1 is not bounded and should be fixed.

### Spec Alignment
- **No masked leak.** The structured sweep covered:
  - masked `zero_check` (2), `zero_step2` (6), `zero_confirm` (9) and `zero_step1` (5) groups
  - every read payload: rifles, sessions, group list under three filters, group detail and stats, dope, lots, search, aggregates, healthz
  - 11 pages

  It found **0** leaks: no non-null offset, mil, POI or `aim`, and no mil-with-direction text. The unmasked control found 40 hits. Pushing a real zero to `complete` and running a 2-shot check gave 0 leaks across `GET /api/zeros/{id}`, group detail and stats. The check message is a fixed template.
- **B-39 as amended holds.** On a masked group, the `PUT review` response keeps the aim, and `POST finalize` returns `aim: null`. review-26 found one more carrier, the read-only editor page of a final masked group (R26-S1, a Suggestion).
- **The masking rule has one source.** There is one `def masks_poi` (`domain/zeroing.py:45`), re-exported at `services/groups.py:80`, and every service, route and summary calls it. The JS reads `group.masked`. `zeroing.js`'s purpose strings are titles only, and it POSTs `"general"` (`zeroing.js:224`).
- **No numeric correction from a 2–9 shot group outside Step 1.** Step 1 corrects only 1–3 shots (`domain/zeroing.py:116-128`). Step 2 and confirm need 10+ (`:139-142`, `:173-183`).
- **Shot removal.** `not_a_bullet_hole` is the only reason in the enum, the DB CHECK (`001_initial.sql:142`), the schema and `review.js:184`. Unmoved estimated placeholders drop without a reason, as B-44 allows (`services/groups.py:247-249`, `:288-296`).
- **CSP, egress, layering, D-14.**
  - No inline script/style, `style=`, `on*=`, `javascript:`, `innerHTML` or `eval` in templates or JS.
  - The only `http://` strings are SVG XML namespaces.
  - The only network client is `api.js`'s same-origin `fetch`. No HTTP-client import exists in `src/`; the one `urllib` use is `urllib.parse.quote`.
  - `rga.web` imports `rga.db` only in `deps.py`.
  - No mean-radius, best-N or group-reduction identifier or label.
- **Registry.** 44 `ROUTE_SCHEMAS` entries, matching design §4.7 with `/api/aggregates`. New writes have 403/415 denial tests (`test_api_analysis.py:455-471`).
- **Docstrings and prose.** Every module and public function in the 17 changed source files has a docstring. The only undocumented functions are closures: the exception handlers and `healthz` inside `create_app`, and `size` inside `_info_metadata_bytes`. Comment lines are section dividers only. There is no why-prose.

### Critical

**G3c2-C1. `_strip_jpeg` and Pillow disagree on the 15 JPEG extension markers (`0xC8`, `0xF0`-`0xFD`), so a crafted upload hides an EXIF carrier from the strip-and-charge step while Pillow parses its IFD on open. This defeats the 256 KB cap and reopens SE-2 / G2-C1.** [via feature-dev:code-reviewer at 88%; bypass and reachability reproduced by me; magnitude inferred from cycle 1's measurement of the same reader]
- **Where.** `src/rga/vision/io.py:117-143`. The `else` arm of `_strip_jpeg` reads a 2-byte length for any marker that is not `0xFF`/`0x00`/`0xD0`-`0xD9`/`0xDA`, and jumps `pos = segment_end` without looking inside for APP1. Pillow's JPEG open loop (`PIL/JpegImagePlugin.py:369-399`) looks up these markers (`MARKER` entries `0xC8`, `0xF0`-`0xFD`, `:276`, `:316-329`) with handler `None`, does nothing, reads **one** byte and keeps scanning, so it re-enters the bytes the stripper skipped and processes any `FF E1 Exif` there. At open, `_read_dpi_from_exif` (`:402`) calls `getexif()` → `ImageFileDirectory_v2.load`, which reads each out-of-line entry value with `_safe_read` (`TiffImagePlugin.py:913-936`) — the same bytes × entries reader behind G3-C1.
- **Reproduced by me (bypass and reachability, not magnitude).** Probe: a valid 16×16 JPEG, an APP1 `Exif` segment carrying a **zero-entry** IFD (no amplification), and an `FF F0 <len>` wrapper whose declared length spans that APP1.
  - Control (plain APP1 Exif): `_strip_jpeg(control).exif` is present, and `load_oriented` runs with **no** IFD-load call.
  - Attack (`FF F0`-wrapped): `_strip_jpeg(attack).exif is None` — the Exif was neither cut nor charged — and a spy on `ImageFileDirectory_v2.load` **fires** during `load_oriented`. The file is ultimately `ImageRejected` (libjpeg refuses the JPGn marker at `img.load()`), but the IFD parse already happened at open, before the rejection and before the dimension check. That is the G3-C1 "rejected only after allocation" pattern.
- **Magnitude is inferred, not executed by me.** I did not build an amplifying version. But the reader this vector reaches is the identical `ImageFileDirectory_v2.load` that cycle 1 measured directly at 978 MB (4,000 entries) and 2.5 GB (10,000 entries). IFD0 allows 65,535 entries, and the concatenated APP1 payload is not charged, so the ceiling is the original **unbounded** class, not the bounded MPF one. This one piece should pass the lead's live-validation gate to confirm the byte count — but the executed facts (cap bypass + IFD reach) already break SE-2's guarantee.
- **Resolves a disagreement with R25-S1.** review-25 reported this construction "does not reproduce (the stripper reads those as length-bearing and still charges/rejects)". That is the bug, not the absence of it: "reads those as length-bearing" is exactly what makes the stripper skip the following Exif. review-25 and the plugin tested different shapes; my executed probe shows the bypass.
- **Why Critical.** It meets the exact bar B-29/B-30 set for G2-C1: a single crafted upload within the 40 MB / 60 MP caps defeats the metadata cap and drives Pillow's IFD parser. It is the same reachability profile cycle 1 rated Critical (loopback, availability-only, crafted file). The FG3.1 spy inputs contain no JPGn shape, so `test_no_pillow_exif_or_ifd_parsing_is_reached` passes today over the hole.
- **Fix scope.**
  - `src/rga/vision/io.py`: in `_strip_jpeg`, treat `0xC8` and `0xF0`-`0xFD` as Pillow does — advance one byte and keep scanning, rather than skipping a declared length — so a hidden APP1 inside them is seen, cut and charged. (Rejecting these markers outright also works, since libjpeg refuses them at decode.)
  - `tests/unit/test_image_io.py`: add a JPGn-wrapped multi-APP1 carrier to the entry-flood and `exif_parse_spy` parametrisations, asserting a bounded tracemalloc peak and an empty spy.

### Warning

**G3c2-W1. A JPEG APP2 `MPF` segment still reaches Pillow's TIFF IFD loader on upload. It is not stripped, not charged against the 256 KB cap, and not tested.** [own; measured by review-25; also flagged by feature-dev:code-reviewer at 85%]
- **Where.** `src/rga/vision/io.py:132-137`: `_strip_jpeg` cuts only APP1 `Exif` and XMP. `io.py:336-338` then calls `Image.open(..., formats=["JPEG"])`. Pillow's JPEG factory (`PIL/JpegImagePlugin.py:168-173`) stores an APP2 `MPF\0` payload, and `jpeg_factory` (`:859`) → `_getmp` → `ImageFileDirectory_v2.load` (`:561`) parses it. That is the same bytes × entries reader behind G3-C1.
- **Reproduced by me.**
  - Probe: a 32×32 JPEG with one APP2 `MPF` segment whose IFD declares **zero** entries.
  - A spy on `ImageFileDirectory_v2.load` fires during `load_oriented`, and the image loads.
  - The control (the same JPEG without the segment) records no call.
- **Measured by review-25.** A 64 KB JPEG whose MPF IFD declares 5,400 entries is accepted (201) through `POST /api/photos` at a **336 MB** tracemalloc peak. With 300 entries the peak is 19 MB. The ceiling is bounded because Pillow keeps only the last MPF segment and a segment is at most 64 KB.
- **Why Warning, not Critical.** It is availability-only, loopback, and needs a crafted file, at roughly 336 MB rather than gigabytes. But it falsifies B-44's categorical rule and the FG3.1 spy claim. The spy inputs (`tests/unit/test_image_io.py:1267-1279`, `:1329-1350`) contain no MPF shape, and adding one would turn `test_no_pillow_exif_or_ifd_parsing_is_reached` red today. It is the reason SE-2 cannot close.
- **Fix scope.**
  - `src/rga/vision/io.py`: cut APP2 `MPF\0` segments in `_strip_jpeg` and charge them against the cap.
  - `tests/unit/test_image_io.py`: add an MPF carrier to the entry-flood and spy parametrisations (4,000 and 10,000 entries, peak under 64 MB).

**G3c2-W2. A zero-check group created from the zeroing page is a `general` group until the check runs, so it shows a mil POI from 2 shots. This is a regression from FG3.2 that reopens B-18 in the app's own flow.** [via review-26; reproduced by me]
- **Where.** `zeroing.js:200` computes `purpose = "zero_check"` but uses it only for titles. `createGroup` (`zeroing.js:212-234`) always POSTs `purpose: "general"` (`:224`), and for a check it does not link (`createGroup(!checking)`, `:234`). `services/sessions.py:307-308` now refuses any other purpose. Before FG3.2, zeroing.js created the group with a zeroing purpose, so it was masked from creation. The FG3.2 Done note records the change: "zeroing.js (send general)".
- **Reproduced by me.**
  - I POSTed the exact zeroing.js body (`shots_expected: 2`, `purpose: "general"`) → 201, `masked: false`.
  - PUT 2 shots + aim → finalize returned `masked: false` and the aim. `GET /api/groups/{id}/stats` then returned `mean_dx_mil` and `mean_dy_mil` for the 2-shot group.
  - With larger offsets, review-26 saw `/groups/{id}` render "0.1 mil high", "0.1 mil right".
  - After `POST /api/zeros/{id}/checks` the group becomes `zero_check` and masked.
- **Why Warning.** It is a POI readout, not a "Dial" correction, so it is not the 2–9-shot must-never. But it is exactly what B-18 exists to hide, for a group the user created *as* a zero check, through the app's own guided flow. Cycle 1 rated the API-only version of this exposure (G3-W4) a Warning, and this one is UI-reachable.
- **Fix scope.** A lead decision first, with two options:
  - (a) a zero route that creates the check group already linked as `zero_check`, in `services/zeroing.py` and `web/routes/zeroing.py`, with the registry updated; or
  - (b) amend B-44 so create may set `zero_check` without a `zero_id`, in `services/sessions.py`.

  Then `zeroing.js`, plus a test in `tests/integration/test_api_zeroing.py` that a group created by the zeroing-page flow is masked from creation.

**G3c2-W3. After the user nudges one estimated shot toward its sibling, Fill shortfall drops the sibling's placeholder and never puts it back. The count stays one short on every refill.** [via review-26; reproduced by me]
- **Where.** `review.js:194-204`: `mergeProposal` drops every proposal shot within `< reach` (one bullet diameter) of **any** kept shot (`:200`). The resolver spreads sibling estimated shots to exactly one diameter apart: `ragged.py:194` `_spread(..., separation=2.0 * r_px)` with `r_px = d/2` (`:76`). So once one sibling is moved, and therefore kept, any nudge toward the other filters out the other's re-proposal.
- **Reproduced by me.** I mirrored the page merge through the repo's own `_client_merge` on a scratch copy of `test_api_review.py`. The sibling gap measured **1.000 d**. After a 0.03 in nudge toward the sibling and a Fill:

  | Scene | After Fill | After a second Fill | Finalize |
  |---|---|---|---|
  | `merged_pairs` | 11 of 12 | 11 of 12 | 409 `count_mismatch` |
  | `close_holes_tight_pair` | 7 of 8 | 7 of 8 | 409 `count_mismatch` |

  The UI disables Finalize ("add 1 missing shot(s)") rather than dead-ending on a 409. The user can still add the shot by hand.
- **Why Warning.** FG3.6 introduced the duplicate filter. It contradicts B-44's "Fill replaces only unmoved estimated shots": here a placeholder is dropped with no replacement, in a core editor flow (AC-10). The existing test (`test_api_review.py:1432-1438`) moves the shot +1.0 in, too far to trigger it.
- **Fix scope.**
  - `src/rga/web/static/js/review.js`: match kept shots to proposal shots one to one, so each kept shot suppresses at most its nearest unmatched proposal shot within reach.
  - `tests/integration/test_api_review.py`: update `_client_merge` to match, and add a nudge-toward-sibling test on `merged_pairs` asserting count == expected after Fill.

**G3c2-W4. Duplicate or overlapping saved aggregates are each counted in a load's tier30, so the same shots count more than once and the tier30 verdict can flip.** [via review-27; reproduced by me]
- **Where.** `services/compare.py:365-370`: every counted saved aggregate is appended to `pooled` with no check for members it shares with another aggregate. `services/compare.py:263-267` guards only against a member that is itself a tier30 group. `services/analysis.py:302-324` and migration 002 have no guard on member sets.
- **Reproduced by me, through the API only.**
  - Load A saves "tight" (3×10, ES 0.6 in) and "wide" (3×10, 1.8 in). Load B saves "mid" (3×10, 1.2 in). The tier30 verdict is `no_real_difference`, with A at 2 entries / 60 rounds.
  - Re-saving "tight" five more times gives A 7 entries / **210 rounds from 60 distinct shots**, a mean of 0.737 vs B's 1.146, and the verdict becomes **`a_smaller`**.
  - review-27 reproduced the same flip with different numbers, plus an overlap case: {g1,g2,g3} + {g2,g3,g4} gives 90 rounds from 40 shots.
- **Why Warning.** A user action on `/analysis` (the Save form stays open) silently changes the `/compare` tier30 verdict, which is the output FG3.4 exists to produce. The FG3.4 Done note's "no double counting" covers only the group-overlap case. The tier10 verdict and status are unaffected.
- **Fix scope.**
  - `src/rga/services/compare.py`: count each group at most once per load's tier30. List a later overlapping aggregate as not counted, with a note.
  - Optionally `services/analysis.py` (or the route) refuses an identical member set with 409.
  - Tests for duplicate, overlap and the verdict in `tests/integration/test_api_compare.py`.

**G3c2-W5. A saved aggregate's stored distance goes stale after a session distance PATCH, and validity never compares the members against it.** [via review-27; reproduced by me]
- **Where.** `services/analysis.py:285`: `_evaluate` checks only that the usable members share one distance, never that it equals `row.distance_yd`. The stored value is what `web/routes/analysis.py:166` and `services/compare.py:273` (and the pages) display.
- **Reproduced by me.** I saved an aggregate at 100 yd, then sent `PATCH /api/sessions/{id} {"distance_yd":300}` → 200. That is allowed, because no zero groups exist. Afterwards:
  - `/api/aggregates` still says `distance_yd: 100`, `valid: true`.
  - `/compare` lists every aggregate at 100 yd while listing the same groups at 300 yd.
  - The stats are computed live at 300 yd (`_pool` uses `bundle.session.distance_yd`, `analysis.py:199`), so no verdict number is wrong.
- **Why Warning.** A persisted API field states something false about the record, on two pages, through the session page's ordinary distance edit. And the FG3.4 Done note says aggregates "become invalid ... when the distances no longer match", which the code only half implements.
- **Fix scope.** `src/rga/services/analysis.py` `_evaluate`: either mark invalid when the members' distance differs from `row.distance_yd`, or report the live distance. Plus a test in `tests/integration/test_api_analysis.py`.

**G3c2-W6. `_strip_jpeg` and Pillow walk 15 JPEG extension markers differently. Requires live validation, because I did not execute it.** [via feature-dev:code-reviewer (88%); verified by reading both sources]
- **What the static reading shows.**
  - `_strip_jpeg` (`src/rga/vision/io.py:114-143`) treats every marker other than 0xFF padding, 0x00, RST/SOI/EOI (0xD0-0xD9) and SOS (0xDA) as length-b
Recorded, not counted.

**Own:**
- G3c2-S1. `_strip_jpeg` walks junk byte by byte in Python (`io.py:110-113`). A 40 MB upload with the JPEG magic and no further 0xFF bytes takes **4.13 s** in the walk. That is bounded and per request. The structure predates the fix wave, since cycle 1's pre-scan walked the same way.

**Review editor / zeroing [via review-26]:**
- R26-S1. The read-only editor page of a final masked group still embeds the aim in `data-state` (`routes/review.py:81-83`, `:220-232`).
- R26-S2. The `session_has_zero_groups` text says "detach", which a complete or superseded zero refuses (409 `zero_state`).
- R26-S3. Contract drift in lead-owned files: `GroupCreate`/`GroupUpdate` still advertise every purpose and `zero_id` (`schemas.py:399-420`); also the `patch_group` docstring and design.md:215.
- R26-S4. The PUT drop guard is a count invariant, not a shot-identity one. B-44 could state the guarantee exactly.
- R26-S5. No test pins the zeroing.js/session.js POST body to `"general"`. This is for T4.12.
- R26-S6. `ZERO_ROUTES_ONLY_MESSAGE` is developer jargon in user-facing text.

**Aggregates / ids / export [via review-27]:**
- R27-S1. `/analysis` shows "counts in the 30-shot comparison" for aggregates that `/compare` refuses (mixed conditions, or overlapping a 30-shot group).
- R27-S2. A 10–19 shot saved aggregate reports `tier: "tier10"` but is never counted.
- R27-S3. A dangling member row (a group deleted with foreign keys OFF, out of band) makes `GET /api/aggregates` 404 and the comparison 500. This is not client input.
- R27-S4. `test_id_bounds.py` keeps a hand list of query and body ids. A schema walk would catch new ones.
- R27-S5. Coverage gaps: the name-escaping test never loads the page; there is no v1-with-data → v2 migration test (verified manually); save → compare has no browser or T4 test.
- R27-S6. Docstring drift in `services/analysis.py:4-6` and `services/compare.py:3-7`.

**Photos [via review-25]:**
- R25-S1 is a confirmation, not a defect: the predicted length-less-marker bypass (0xC8, 0xF0–0xFD) does not reproduce.

### Cross-Task Consistency
- **Interfaces hold across the fix wave.**
  - FG3.2 narrowed create/PATCH to `general`. Both page callers (`session.js:123`, `zeroing.js:224`) send `"general"`.
  - FG3.6 repaired the one test FG3.2 broke.
  - FG3.3's id bound and FG3.4's new routes agree. The id sweep covers `/api/aggregates/{id}`, and the registry count is 44 in both `ROUTE_SCHEMAS` and design §4.7.
- **The one cross-slice regression is G3c2-W2.** B-44's narrowing of create (FG3.2's mandate) removed the only way the zeroing page had to make a check group masked from birth, and no slice owned the gap. It needs a lead decision, because the two fixes amend different decisions (a new zero route, or B-44 itself).
- **The masking rule still has one source**, and the new aggregate path reuses it. Saved aggregates re-evaluate members through `masks_poi` live (`services/analysis.py:276-281`), so a member that later becomes a zero check drops out with `valid: false`. review-27 verified this; it also closes the pre-check window of G3c2-W2 for aggregates.
- **Completion claims vs disk.**
  - Every fix-wave `Run:` command the analysts and I re-ran matches its Done note, and the 4309/2/1 suite count reproduces.
  - Three Done-note claims overreach: FG3.1's "no Pillow EXIF/IFD parse is reachable" (G3c2-W1), and FG3.4's "no double counting" and "invalid when distances no longer match" (G3c2-W4, G3c2-W5).
  - The T3.00 and T3.10 corrections are recorded in `test-evidence.md` as B-44 required.

### Tests
- [x] **All tests passing.** Full suite exit **0**, 4309 passed, 2 skipped, 1 xfailed (930.62 s), matching the baseline. Both skips are declared. The only warning is the accepted Starlette/httpx deprecation.
- [x] **T3 gate met.** `smoke.sh` exit 0, 36/0/0, and the gate fails when mutated.
- [ ] **Test coverage adequate.** No. Each new finding is a behaviour no test exercises:
  - a JPGn-wrapped carrier in the flood and spy tests (G3c2-C1) — the spy test passes today over the hole
  - an MPF carrier in the flood and spy tests (G3c2-W1)
  - a zeroing-page-created check group being masked from creation (G3c2-W2)
  - a small nudge toward an estimated sibling before Fill (G3c2-W3)
  - duplicate or overlapping saved aggregates (G3c2-W4)
  - a session distance change after saving (G3c2-W5)
- **Mutation evidence I re-checked.**
  - io: entry cap and stripping mutants are killed.
  - review-26's 7 mutants and review-27's id-bound and handler mutants are killed.
  - The fix-wave Done notes report 44/44, 16/16, 10/10, 20/21 (1 equivalent), 11/11, 13/13 and 10/11 (1 equivalent).
- **No assertions on mocks.** The new fix-wave tests:
  - assert real SQLite state, real HTTP responses, real tracemalloc peaks and real rendered DOM;
  - use spies that delegate to the real functions;
  - inject faults only through monkeypatches (a locked database, an exception class into `Image.open`).
- **Tier coverage.** T1 and T2 are strong. T3 ran and passed. T4 is Group 4's scope; review-26 and review-27 each name a journey T4 should own (R26-S5, R27-S5).

### Verdict
This is cycle 2 of 3 for Group 3. Cycle 3 remains and is terminal. The normal loop applies: one scoped fix wave covering the 1 Critical and 5 Warnings, re-verified by cycle 3. Suggested fix-wave grouping by file ownership: (1) `vision/io.py` + `test_image_io.py` — G3c2-C1 and G3c2-W1 (same `_strip_jpeg` rewrite, strip and charge JPGn-hidden and MPF carriers; close SE-2); (2) `web/static/js/review.js` + `test_api_review.py` — G3c2-W3; (3) `services/compare.py` + `services/analysis.py` + `test_api_compare.py` — G3c2-W4 and G3c2-W5; (4) a lead decision on the zero-check creation path (B-44 or a new zero route), then `services/zeroing.py`/`routes/zeroing.py`/`zeroing.js` + `test_api_zeroing.py` — G3c2-W2. SE-2 stays Open in `security-exceptions.md`, naming both the JPGn (C1) and MPF (W1) vectors, until a cycle verifies the fix.

Verdict: FAIL — 1 Critical, 5 Warning, 14 Suggestion (not counted)

## Group 3 — Cycle 3

Date: 2026-10-02 · Reviewing: Group 3 Fix 2 (FG3b.1, FG3b.2, FG3b.4, FG3b.5, B-52) against cycle 2's 1 Critical and 5 Warnings, the user-requested work (U1, U2, UF1, UF2 + FG3b.3, UF3) as first-class scope, and regressions anywhere in Group 3 · Scope/cycle: **Group 3, cycle 3 of 3 (TERMINAL)**.

Synthesizer: review-28. My slice was SE-2 / FG3b.1 (`vision/io.py`, `tests/unit/test_image_io.py`), UF3, the cross-module surface, the full suite and smoke.

Analysts (all three reported before this section was written):
- review-29: zeroing check-groups (FG3b.2), saved aggregates (FG3b.4, B-52), the id sweep (FG3b.5), masking consistency.
- review-30: U1, U2, UF2 (`vision/aim.py` and the editor), the W3 fix in `review.js`.
- review-31: UF1, the group page, including correction gating against Form's doctrine.

Coordination: degraded mode (B-01). There are no task tools and no sentinels, and the disk is ground truth.

### Method
- **Every Critical and Warning below was re-checked by me on current disk.** I either reproduced it or cite the file:line I read. Where a figure is an analyst's or the plugin's, I say so. Findings that did not hold up are listed under "Dropped or re-rated".
- **Scratch only.** Probes ran under `/tmp/rga-review28-*`. The mutation run used a scratch copy of `src/` and `tests/` that I diffed identical to the repo first, with `PYTHONDONTWRITEBYTECODE=1`. The repo was never edited apart from this section.
- **Processes.** PID 93688 and port 8765 were never touched. TestClient only sends a `Host: 127.0.0.1:8765` header and binds nothing; smoke picks its own free port. Afterwards `pgrep -fl -- '-m rga'` showed only 93688.
- **Not vacuous.** With TestClient's default host, `/healthz` gave 400; with `base_url="http://127.0.0.1:8765"` it gave 200. The leak checks ran beside unmasked control groups, which did hit the needles.
- **SE-2 method.**
  - I re-ran review-24's and review-25's exact shapes and every JPGn marker against `load_oriented`. I used tracemalloc and a spy on `ImageFileDirectory_v2.load`, `Exif.load`, `Exif.load_from_fp`, `Image.getexif`, `_getexif` and `_getmp`.
  - The spy has a positive control: given the carrier directly, raw Pillow fires it.
  - I measured process RSS (`ru_maxrss`) in separate processes, and once through `POST /api/photos`.
  - I limited resource measurements to the shapes reported below. I did not construct further amplifying files. The plugin's extra amplification paths are therefore labelled as inferred.
- **Plugin pass.** `feature-dev:code-reviewer` read `io.py`, the whitelist tests and UF3 (read-only). Its findings are merged below, tagged `[via plugin]`, and marked as executed or inferred.

### Gates (run by me)
- **Full suite, run once.** `cd /Users/calebschwarz/Documents/Hunting && .venv/bin/pytest -q -p no:cacheprovider` → exit **0**: **4730 passed, 2 skipped, 1 xfailed** in 1240.53 s. It started at 03:55:32, after the last source change (03:24).
  - This matches the baseline.
  - The xfail is the existing `tape_cross_multi_aim`.
  - I did not capture skip reasons (no `-rs`). shellcheck is absent on this machine, consistent with the declared skip.
- **T3 smoke.** `./scripts/smoke.sh` → exit **0**, **36 PASS / 0 FAIL / 0 SKIPPED**.
- **Analyst slice gates.** These were re-run by the analysts, not by me:
  - review-29: 416 passed.
  - review-30: 652 passed, 1 xfailed; eval 23/23 P-11.
  - review-31: 204 passed; 8 Chromium tests ran.

### Cycle-2 findings: re-verification

| Finding | Status | Evidence |
|---|---|---|
| **G3c2-C1** (JPGn framing hides an APP1 from the strip) | **Closed** | Own probe. Each of 0xC8 and 0xF0-0xFD wrapping a 4,000-entry APP1 `Exif` → `ImageRejected("unexpected JPEG marker before the scan")`, at a traced peak under 0.1 MB, with the spy empty. `io.py:133-134` rejects any marker outside `_JPEG_LENGTH_MARKERS`. |
| **G3c2-W1** (APP2 MPF reaches the IFD loader) | **Closed** | Own probe. A 5,400-entry MPF loads at a 0.1 MB peak; `ImageFileDirectory_v2.load` never fires. An MPF prefix in each of APP0-APP15, five MPF segments, and MPF after EOI all gave the same result. Every non-whitelisted APPn is dropped (`io.py:145-163`). |
| **G3c2-W2** (zeroing-page check group born general) | **Closed** | Read: `services/zeroing.py:646-655` inserts `purpose=zero_check` with `zero_id` in one statement, and `zeroing.js:221` uses the route. review-29 also checked: a 5-stage leak sweep with 0 hits (the control had 48), every unmask path refused, the Chromium single-write test, and M4/M11 killed. |
| **G3c2-W3** (Fill loses a sibling placeholder) | **Closed for the reported case.** A new edge case is G3c3-W4. | `review.js:235-254` now matches one to one. review-30 checked the reported case with the page's own merge in node: 12 of 12 (merged_pairs) and 8 of 8 (close_holes_tight_pair). |
| **G3c2-W4** (duplicate/overlapping aggregates double-count) | **Closed** | Own API probe: an identical set → 409 `duplicate_aggregate`, and a reordered set → 409. review-29 covered overlap, greedy claim, deletion and masking a member. |
| **G3c2-W5** (stale stored distance) | **Closed** | Own API probe: all member sessions moved to 300 → `distance_yd: 300, valid: true`. One moved back → `valid: false`, reason "no longer at one distance (100, 300 yd)". |
| G3c2-W6 (marker-walk divergence, requires live validation) | **Closed** (same vector as C1, now executed) | See G3c2-C1. |

### SE-2

**The IFD-parser vector SE-2 tracks is closed. SE-2 itself should NOT be marked Resolved.**

- **Closed.**
  - Across every carrier I tried, Pillow's EXIF/MPF/IFD loaders never fired: JPGn wraps, MPF in every APPn, odd APP1 prefixes, Exif inside kept DHT/DQT/DRI/ICC/JFIF/Adobe payloads, FF padding, FF00, junk, data after EOI, PNG eXIf before and after IDAT and after IEND, and compressed XMP bombs.
  - The rebuilt pre-SOS stream is exactly framed. Pillow consumes each kept segment by its declared length (Skip/SOF/DQT/APP all read `n`), so nothing inside a kept payload is scanned. Pillow does not parse after SOS in Python. The plugin reached the same conclusion from Pillow's source.
  - SOF3/5-7/9-15, DAC, DNL, DHP and EXP are rejected.
- **Not closed.** SE-2's stated risk is "one crafted upload within the 40 MB / 60 MP caps can make [the process] allocate gigabytes". FG3b.1's own rebuild still has that risk, through a different mechanism (G3c3-C1, measured at +1.48 GB RSS, and the upload is accepted).
- **Recommendation.** Keep SE-2 Open naming G3c3-C1, or close its IFD vector and open a new exception for the rebuild. That is the lead's or the user's call; the code fix is small either way.

### Spec Alignment (new user-requested work)
- **U1 (B-45).**
  - The outer disc comes first and the grid cross-check is wired in (`pipeline.py` passes `grid=find_grid`).
  - real_02: the outer disc is #1 at 704 px and the inner ring #2 [via review-30, executed].
  - The candidate-label part of B-45 is not implemented (G3c3-S5).
- **U2 (B-45).**
  - real_02 gives 6 crossings at about 475 px/in; the other photos are unchanged [via review-30].
  - The two-lines-per-family acceptance can return a silently doubled unit (G3c3-W3).
- **UF1 (B-46/B-47).**
  - Correction gating matches the doctrine. review-31 rendered 200 pages, with 0 gate mismatches.
  - My own spot check:
    - Correction text appears only for unmasked 10-shot groups (100 and 300 yd) and a 2-shot Step 1 group.
    - None for general 5 and 9 shot groups or for any masked group.
    - The JSON APIs never carry correction text.
  - Values agree with the zeroing API in 17/17 cases [via review-31].
  - No numbers appear for masked groups. A disabled toggle carries a fixed reason.
- **UF2 (B-49).**
  - Nothing is auto-confirmed. Candidates are served only while the group has no aim; the manual pick and snap stay.
  - `DetectOut.aim_candidates` on a masked group is an editor surface, consistent with B-39 as amended, and is not counted as a leak.
  - The defects are G3c3-W1 (a 500) and G3c3-W2 (a bullet hole pre-selected as the aim).
- **UF3 (B-51).**
  - `base.html:20-24` is plain markup outside every block: no form, script, URL, `style=` or `on*=`.
  - Online is `disabled`, so it is not focusable, and it has its caption.
  - No JS selector reaches it. All radio and checkbox queries are scoped (`compare.js:186`, `analysis.js:229`, `group.js:10`), and `reference.js` uses `name="candidate"`.
  - No request, stored setting, route or config var. **No behaviour change.**
- **FG3b.5 / B-52.**
  - The sweep covers check-groups, and the leak test asserts on fields with collision-proof values [via review-29].
  - The B-52 label text is present (`analysis.js:177`, `analysis.html:41`).
  - The Done note misstates the status code (G3c3-W6).

### Cross-module checks (own)
- **One masking rule.** There is one `def masks_poi` (`domain/zeroing.py:45`). `group.purpose` comparisons elsewhere are flow logic, not re-derived masking. `services/analysis.py:450-455` is the correction gate (R31-S1 → G3c3-S7).
- **Masked leaks.**
  - Masked `zero_check`(2), `zero_step2`(6), `zero_confirm`(9) and `zero_step1`(5) groups: 0 aim digits, 0 "N mil dir" text, 0 offset keys across the page, detail and stats.
  - Unmasked controls carry the aim digits and offsets in JSON.
  - review-29 (20 surfaces × 5 stages) and review-31 (200 pages) agree.
- **No correction from 2-9 shots outside Step 1.** Confirmed, as above. General 1-9 shot groups show a mean-POI description, which B-09 allows; it is not a correction.
- **Shot removal.** `not_a_bullet_hole` remains the only reason. Placeholders drop without a reason, per B-44 [via review-30].
- **CSP and egress.**
  - No inline script/style, `style=`, `on*=`, `javascript:`, `innerHTML` or eval in templates or JS.
  - No `http(s)://` except SVG namespaces.
  - The only network call is `api.js`'s same-origin `fetch`.
  - No HTTP-client import in `src/`; the only `urllib` use is `urllib.parse.quote`.
- **Layering.**
  - `rga.web` imports `rga.db` only in `deps.py`.
  - vision and domain import no upper layer.
  - `domain/enclosing.py` uses only the stdlib.
- **D-14.** No mean-radius, best-N or group-reduction identifier or label. The spread circle is a size measure decided under B-46.
- **Docstrings.** Every module, public function and public class in the 15 changed source files has a docstring. The only comments are section dividers plus one descriptive line (`reference_circle.py:35`). There is no verbose why-prose. One stray line break is noted in G3c3-S16.
- **500s.** One new 500 (G3c3-W1). The id sweeps found none: review-29 sent 304 requests, and I probed check-groups at 2^63, 2^64 and 2^63−1.

### Critical

**G3c3-C1. The FG3b.1 whitelist rebuild allocates one Python object per kept segment or chunk with no count limit. A single 40 MB in-cap JPEG made of empty kept segments is accepted (201) while the server's RSS grows by about 1.5 GB and the request takes about 20 s. This reopens SE-2's stated risk through the fix itself.** [own; executed]
- **Where.**
  - `src/rga/vision/io.py:119`, `:143-154`, `:165`: `_rebuild_jpeg` appends one `bytes` slice per kept segment to `out`, then calls `b"".join(out)`. Nothing caps how many DQT/DHT/DRI/SOF/APP0/APP14 segments come before SOS.
  - `io.py:224`, `:239-243`, `:261`: `_rebuild_png` has the same structure per kept chunk.
- **Reproduced by me.**
  - A valid 96×48 JPEG padded to the 40 MB cap with about 10.5M empty DHT segments:
    - `_rebuild_jpeg` alone: RSS 159 → 1563 MB, 8.3 s; tracemalloc peak 1295 MB.
    - Full `load_oriented`: same RSS, 20.1 s, **loads**.
    - Through `POST /api/photos` (TestClient): **201**, RSS 231 → **1714 MB**, 19.9 s.
    - Control (the plain JPEG): 201, RSS +1 MB.
  - An APP0-JFIF flood gives +977 MB RSS, 14.7 s, loads.
  - On PNG, 1M sBIT or pHYs chunks (14-20 MB files) gave traced peaks of 144-156 MB in 18-25 s. I did not measure at the 40 MB cap.
- **Regression.**
  - The same inputs against the FG3.1 code (scratch copy `/tmp/mutfg32_29573058`): the DHT flood +0 MB; COM and APP0 floods about +620 MB, from Pillow's `applist`.
  - FG3b.1 removed the COM/APPn flood but raised the worst case from about 0.6 GB to about 1.5 GB.
  - The FG3b.1 fuzz (`test_image_io.py:1892-1922`, `:2020-2047`) never generates many kept segments, so its "peak < 64 MB" holds only for its own inputs.
- **Further unbounded kept parts [via plugin, inferred from Pillow source, not executed by me].**
  - SOF is kept at any length and count, and Pillow's `SOF` handler appends one `layer` tuple per 3 payload bytes (`JpegImagePlugin.py:233-236`).
  - JFIF/Adobe segments each append to `applist` (`:81`).
  - PNG `cHRM` is kept at any length, and Pillow unpacks `len // 4` values (`PngImagePlugin.py:537-538`).
- **Why Critical.** This is the bar B-29/B-30 and cycle 1 set for this class: one crafted upload within the 40 MB / 60 MP caps drives a GB-scale allocation. G3-C1 was rated Critical at 978 MB; G3c2-W1 was a Warning at 336 MB "rather than gigabytes". This one is accepted and stored, not rejected, and is a regression introduced by the SE-2 fix. It is availability-only on a loopback single-user app, with a bound of about 35× the upload. That bound is why the lead may weigh it, but under the project's own precedent it is Critical.
- **Fix scope** (`src/rga/vision/io.py`, `tests/unit/test_image_io.py`):
  - Cap the number of header segments before SOS and of PNG chunks (camera files have a few dozen), rejecting beyond the cap.
  - Allow one SOF of at most a small fixed length, one JFIF and one Adobe segment, and a DRI of exactly 4 bytes.
  - Check fixed PNG chunk sizes (IHDR 13, gAMA 4, cHRM 32, sRGB 1, pHYs 9, PLTE ≤ 768, tRNS ≤ 256).
  - Build the output in a `bytearray`.
  - Tests: kept and dropped segment floods, and a chunk flood, at the upload cap under tracemalloc (< 64 MB) and with a time bound.

### Warning

**G3c3-W1. Detect can return a 500.** [via review-30; reproduced by me through the public API]
- **Where.** `src/rga/services/review.py:246-247`: `_aim_candidates` catches only `ValueError`. `propose_aims` raises `cv2.error` from `cv2.resize` on a tiny working image (traceback: `review.py:246` → `aim.py:136` → `aim.py:256` → `aim.py:290`).
- **Reproduced.**
  - Steps: upload an 800×600 JPEG, then confirm `circle_1in` manually from 3 edge points on a flat arc (sagitta 6 px). That gives a 200 ppi working image of 11×8 px.
  - `POST /api/groups/{id}/detect` → **500 `internal_error`**, with or without `roi_px`.
  - Positive control: after `PUT review` sets an aim (so aims are skipped), the same detect → 200.
- **Why Warning.** "Any 500 reachable from client input" is a FAIL trigger. Cycle 1 rated the id-overflow and bodyless-upload 500s as Warnings. UF2 introduced this one; that path returned 200 before UF2.
- **Fix scope.** In `services/review.py` `_aim_candidates`, catch `cv2.error` too (or any exception, with a log line), so aim proposals can never fail detect. Optionally `propose_aims` raises `ValueError` for working images under about 1 in. Add a test in `tests/integration/test_api_review.py`.

**G3c3-W2. UF2 pre-selects a bullet hole as the aim (`diamond_center`) on the catalog's analogue of the user's GunFun target.** [via review-30; reproduced by me]
- **Where.** `src/rga/services/review.py:233-253`: `_aim_candidates` has the proposed `shots` but uses them only to place the search region (`:242`). Nothing excludes a candidate that sits on a shot. The editor pre-selects candidate #0 (`review.js:264`) behind one "Use this aim" click (`review.html:50`).
- **Reproduced.** `propose_aims` on `red_grid_band_light_diamond`, seeds 0/1/2:
  - #0 is `diamond_center`, scores 0.63/0.65/0.63, **0.041-0.045 in from a true hole centre**, 2.0-3.1 in from the true aim.
  - The real diamond is #1 at 0.30/0.21, or absent (seed 2).
  - review-30 also saw a hole at rank 1 in 4 more synthetic groups, and a hole at #2 on real_04.
- **Why Warning.** If the user accepts the pre-selected candidate, the aim sits inside their own group, so the POI offset is about 0. The UF1 correction (or the zeroing advice) is then computed from a wrong aim. The information needed to exclude it is already in scope. B-49's real-photo acceptance passed, but its test set omits this scene.
- **Fix scope.**
  - `services/review.py` (or `vision/aim.py`): drop candidates within about 0.75 bullet diameter of any proposed shot.
  - `tests/unit/test_aim.py`: add `red_grid_band_light_diamond` to the acceptance set.

**G3c3-W3. The U2 line-lattice fallback accepts exactly two lines per family as one 1-inch cell. When a band hides the middle line, it returns a doubled unit with no warning.** [via review-30, executed by them; the mechanism verified by me in code]
- **Where.** `src/rga/vision/reference_grid_lines.py:273-282`.
  - The `square_pair` rule accepts `v_count == h_count == 2`, with the unit taken as the gap between the two lines.
  - With 2 lines there is no spacing to check consistency against.
  - The 4 crossings that result land on real grid crossings, so the confirm overlay looks right.
- **review-30's measurements.**
  - A 210 px/in grid with the middle line of each family under a band → find_grid returns 4 crossings at **420 px/in**, rms 0, no warning.
  - Non-grid shapes (a lone square, a "#" mark, a 3×2 folded sheet) are also accepted.
- **Why Warning.** A confirmed reference at 2× scale silently halves every measurement on that photo. B-45 allowed "≥ 2 lines per direction with consistent spacing", but two lines cannot show consistent spacing. This is a correctness gap in the decided rule, not a coding slip.
- **Fix scope.** Lead decision first: refuse the two-line case, or attach a warning to it ("only one square found; check it spans exactly 1 in"). Then `reference_grid_lines.py`, plus a hidden-middle-line test in `tests/integration/test_reference_grid_synth.py`.

**G3c3-W4. The W3 fix's greedy nearest-pair matching is not a maximum matching, so Fill can over-count by one.** [via review-30, executed with the real JS; the mechanism verified by me in code]
- **Where.** `src/rga/web/static/js/review.js:235-254`. Pairs are taken shortest first. In this setup the greedy pass leaves C1 unmatched and re-adds it, even though K1–C1 and K2–C2 is a full matching:
  - siblings C1 and C2 are 1 d apart;
  - both are dragged about 0.6 d the same way, to K1 and K2;
  - so K1–C2 (0.4 d) is taken first, which blocks K2–C2 and K1–C1.
- **Measured by review-30.** 13 of 12 on merged_pairs and 9 of 8 on close_holes_tight_pair. The previous filter gave 12 and 8 here.
- **Why Warning.** This is the mirror of G3c2-W3, which cycle 2 rated a Warning, and the fix introduced it. B-50 says Fill refills "up to the expected count". Finalize is then disabled with "remove 1 with Not a bullet hole" (`review.js:145`). That text can lead the user to reject a real moved shot instead of the extra placeholder.
- **Fix scope.** A maximum bipartite matching (augmenting paths; n is tiny) in `review.js`, mirrored in `_client_merge` in `tests/integration/test_api_review.py`, plus this case as a test.

**G3c3-W5. Nothing tests that the PNG whitelist is applied on the load path, and the PNG fuzz's spy assertion cannot fail.** [via plugin, inferred; the mutant executed by me]
- **Where.**
  - `tests/unit/test_image_io.py:2020-2047` asserts `ifd_load_spy == []` for PNG. Pillow's PNG open/load path never calls the spied loaders on its own, so the assertion holds whether or not `_rebuild_png` is applied.
  - `test_a_dropped_chunk_with_a_bad_crc_does_not_reach_pillow_at_all` (`:1984-1988`) calls only `_rebuild_png`.
- **Mutant, executed on a scratch copy.**
  - Make `load_oriented` open the original bytes for PNG only (`io.py:369`): **all 470 tests in `test_image_io.py` still pass**.
  - The same mutant for JPEG is killed by 8 tests.
- **Why Warning.** B-50 makes the PNG whitelist a required security control. The FG3b.1 Done note cites the 1,500-case PNG fuzz as the guard ("spy never fires"), but the guard checks nothing for PNG. Per the testing standards, this is a verifier that cannot fail, on a new control with no test that would go red without it.
- **Fix scope** (`tests/unit/test_image_io.py` only):
  - Assert on the bytes handed to `Image.open` for PNG, for example by spying `Image.open`'s input, or by asserting that a dropped chunk is absent from what Pillow saw.
  - Make the bad-CRC test call `load_oriented`.

**G3c3-W6. The FG3b.5 acceptance and Done note say check-groups path ids 2^63/2^64 → 404. They actually give 422, and the test asserts only "some 4xx".** [via review-29; reproduced by me]
- **Where.** `tasks.md:529-530` (acceptance and Done note). `tests/integration/test_id_bounds.py:177-187` asserts `400 <= status < 500`. `src/rga/web/routes/zeroing.py:44` bounds `ZeroId` at the path, so validation answers first.
- **Reproduced.**
  - `POST /api/zeros/{2^63}/check-groups` → 422 `validation_error`, and `{2^64}` → 422.
  - `{2^63−1}` → 404 `zero_not_found`. `GET /api/zeros/{2^63}` → 422 as well.
- **Why Warning.** The security property holds: never a 500, always the error model. But the Done note states a verified outcome that its `Run:` did not check, and that outcome is false. The testing standards treat that as a FAIL condition.
- **Fix scope.** Either correct the acceptance and Done note to 422, which is consistent with every zero route's path bound, or assert the exact code in `test_id_bounds.py`. No product code changes.

### Requires live validation (not counted)
- **R30-W5, detect + aim time vs the ≤ 3 s NFR (spec §133).**
  - review-30 measured 0.59-3.78 s for aims alone on real_02-05, but at load average 34-60. The UF2 Done note says about 1.1 s typical.
  - The machine was still at load 39 when I checked. Per the rules I may not read `data/`, so I could not measure either way.
  - No test bounds detect plus aims (`test_aim_real_photos.py` allows 8 s).
  - This needs an idle-machine measurement in the lead's live-validation gate before the NFR can be claimed.

### Dropped or re-rated
- **R30-W6 (private-photo copies in /tmp): dropped.** `/tmp/u1_view.jpg`, `/tmp/u2_scratch/` (v02.jpg, mask02.png), `/tmp/real_vis_2.jpg`, `real_vis_3.jpg`, `sheet1.jpg` and `sheet2.jpg` do not exist on current disk. I also found no written "never copy" rule; `real-photo-notes.md` says the originals are stored 0700.
- **R30-W7 (B-45 "say so in the candidate label" not implemented) → G3c3-S5.** This is a planning gap: `reference.js` was not in U1's file list, and the Done note does not claim it. It has no effect on results.
- **R31-W1 (layer toggles desync after Back) → G3c3-S6.** The mechanism is verified in code: `group.js:10-27` applies each checkbox only at module run, and `group.html:30-33` lacks `autocomplete="off"`. But:
  - the effect is a display/control mismatch that one click resolves;
  - the plot never shows a correction the gate withholds;
  - no data changes;
  - the reproduction is headless Chromium, where back/forward-cache behaviour may differ from the user's browser (Safari/Chrome unverified).

### Suggestion
Recorded, not counted.

**Own / plugin:**
- G3c3-S1 [via plugin] The ICC caps are inconsistent:
  - JPEG: cumulative and silently dropped (`io.py:151-154`).
  - PNG: per chunk on compressed length, every iCCP kept (`io.py:241-243`).
  - The profile is never used downstream, so dropping it everywhere would remove the difference.
- G3c3-S2 [via plugin] The JPEG fuzz uses a dpi-bearing base (`test_image_io.py:1634`), so `_read_dpi_from_exif` returns early. Only MPF leaks can trip its spy, not Exif-only ones. Older tests cover Exif.
- G3c3-S3 [via plugin; read by me] Assertions that cannot fail or are redundant:
  - `test_image_io.py:1967` compares `load_oriented(data)` with itself;
  - `:1688-1690` and `:1697` repeat what `rebuilt == …` already asserts.
- G3c3-S4 [via plugin, not rendered] UF3 cascade and contrast:
  - the global `label` rule (`app.css:116`) overrides `.mode-switch`'s font size and margin;
  - the current-mode contrast is about 4.0:1;
  - the radio focus ring matches the header colour.

**UF1 [via review-31]:**
- G3c3-S5 (was R30-W7) The B-45 grid cross-check is not named in the reference candidate label.
- G3c3-S6 (was R31-W1) Add `autocomplete="off"` to the four toggles, and/or re-apply on `pageshow`. Add a back-navigation test.
- G3c3-S7 R31-S1. `correction_gate` allows any non-general unmasked purpose by default (`analysis.py:455`). This is unreachable today. Use an allow-list.
- G3c3-S8 R31-S2. Every group-plot test uses ppi 200, and two scale mutants survived. Add a non-200 case.
- G3c3-S9 R31-S3. The correction stays on the page after it has been dialed. Product wording.
- G3c3-S10 R31-S5. A missing working PNG hides the photo toggle, even though the image route can rebuild it. There is no size check under `preserveAspectRatio="none"`.
- G3c3-S11 R31-S6. Draft groups with 10+ shots offer a correction (consistent with zeroing). `_with_two` lacks a return annotation.

**Zeroing / aggregates [via review-29]:**
- G3c3-S12 R29-S1. The tier30 verdict depends on creation order (B-50's greedy rule as written). Consider an order-independent tie-break.
- G3c3-S13 R29-S3. PATCH `shots_expected` on a check group to 10 → 200. That leaves a permanent masked orphan. Bound it to 1-2.
- G3c3-S14 R29-S4/S5.
  - `zeroing.js:345` single `replace("_"," ")`.
  - The zero's group table is not refreshed after a check group is created.
  - The candidate list includes Step 1 groups that `/checks` refuses.
- G3c3-S15 R29-S6 / R31-S4. The `services/analysis.py:4-6` and `services/compare.py:4-6` docstrings misstate `masks_poi`.

**Vision / UF2 [via review-30]:**
- G3c3-S16 R30-S4.
  - Stray line break in the `find_circle_candidates` docstring (`reference_circle.py:541`).
  - `_grid_candidates`' `scale` argument receives ppi (`aim.py:225`).
  - Dead `or coarse` (`aim.py:229`).
- G3c3-S17 R30-S1. The U1 evidence's "real_01/03/04/05 unchanged" is stale for circle_3in ordering through the pipeline.
- G3c3-S18 R30-S2. The aim acceptance tests use seeds that pass. Add xfail known-miss cases.
- G3c3-S19 R30-S3. The real_05 tape cross is labelled `dot_center`, with a 0.12 in test tolerance.
- G3c3-S20 R30-S5. Hand-measured real_02 pitch disagrees: 470 vs 478 px/in.
- G3c3-S21 R30-S6. Pre-existing: the crossing path accepts a QR code or an anisotropic frame with no warning.
- G3c3-S22 R30-S7. `test_the_one_inch_kind_is_not_given_the_outer_disc` only restates the size filter.
- G3c3-S23 R29-S2's remainder. Lax coercion app-wide: `shots_expected: true` and `session_id: "1"` are accepted. This predates the wave.

### Cross-Task Consistency
- **Interfaces hold.**
  - check-groups is in the registry (45 routes in `ROUTE_SCHEMAS` and design §4.7) and in the id sweep.
  - `DetectOut.aim_candidates` is plane inches in the same frame as the proposed shots, and `review.js:345` consumes it.
  - UF1 reads `domain.zeroing.correction_for`, the function zeroing uses (17/17 agreement [via review-31]).
- **Seams with new defects.**
  - `services/review.py` ↔ `vision/aim.py`: G3c3-W1 (exception type) and G3c3-W2 (shots not passed).
  - U2 → U1 through `pipeline.py`: G3c3-W3 can also reorder circle candidates, although review-30 could not change #1 in practice.
- **Completion claims vs disk.**
  - Every fix-wave `Run:` re-run by the analysts or me matches its pass count.
  - Overreaching claims:
    - FG3b.1's PNG fuzz as a guard (G3c3-W5);
    - its "peak < 64 MB", which holds only for its own inputs (G3c3-C1);
    - FG3b.5's "→ 404" (G3c3-W6);
    - UF2's "about 1.1 s typical" (unverified either way).

### Tests
- [x] **All tests passing.** Full suite exit 0, 4730 passed, 2 skipped, 1 xfailed. Smoke exit 0, 36/0/0.
- [ ] **Test coverage adequate.** No. Missing:
  - a segment/chunk-count flood at the upload cap (G3c3-C1);
  - a tiny-working-image detect (G3c3-W1);
  - the hole-as-aim scene (G3c3-W2);
  - a hidden-middle-line grid (G3c3-W3);
  - a same-direction double drag before Fill (G3c3-W4);
  - a PNG load-path whitelist check (G3c3-W5);
  - an exact-code assertion, or a corrected note, for check-groups (G3c3-W6).
- **No assertions on mocks.** The spies delegate to the real functions. Analysts found none in their slices.
- **Tier coverage.** T1 and T2 are strong, and the Chromium e2e tests run. T3 smoke passed. T4 is Group 4's scope. The NFR timing is outstanding (live validation).

### Verdict
This is cycle 3 of 3 for Group 3, and it is **terminal**. There is 1 Critical and there are 6 Warnings, so the bar of zero Criticals and zero Warnings is not met.

Group 3 is **blocked**, and the lead decides what happens next. The findings and the exact evidence are recorded above. Fix scope by file, for reference:
- `vision/io.py` + `tests/unit/test_image_io.py`: C1, W5.
- `services/review.py` (+ `vision/aim.py`) + `test_api_review.py` / `test_aim.py`: W1, W2.
- `vision/reference_grid_lines.py` + `test_reference_grid_synth.py`, after a lead decision on B-45's two-line minimum: W3.
- `web/static/js/review.js` + `test_api_review.py`: W4.
- `tasks.md` FG3b.5 note or `test_id_bounds.py`: W6.

Also: SE-2 must not be marked Resolved while C1 stands, and the detect + aims timing needs an idle-machine measurement.

Verdict: FAIL — 1 Critical, 6 Warning, 23 Suggestion (not counted)

## Group 3 — Cycle 4

Date: 2026-10-02 · Reviewing: Group 3 Fix 3 (FG3c.1, FG3c.2, FG3c.3) against cycle 3's 1 Critical and 6 Warnings, regressions inside the FG3c files, and the full-suite and smoke gates · Scope/cycle: **Group 3, cycle 4: one extra cycle authorized by the lead under B-32/B-55, and FINAL.**

Synthesizer: review-32. My slice was C1 and W5 (`vision/io.py`, `test_image_io.py`), W1, W6, SE-2, the cross-module surface and the gates.

Analyst: review-33 covered W2, W3, W4 and real-photo behaviour (`aim.py`, the aim call in `review.py`, `review.js`, `reference_grid_lines.py`, `reference_grid.py` and their tests). It reported before this section was written.

Coordination: degraded mode (B-01). The disk is ground truth.

### Method
- **Re-checked by me.** I re-checked every Critical and Warning below on current disk, by reproduction or by the file:line I read. Analyst figures are tagged `[via review-33]`.
- **Scratch only.**
  - Probes ran in `mktemp -d /tmp/rga-review32-*` (mode 0700), against temp data dirs. Mutants ran on a copy of `src/`, `tests/`, `tools/` and `scripts/` without `data/`, with `PYTHONDONTWRITEBYTECODE=1`. I diffed the copy identical to the repo first and diffed it again after restoring.
  - No real photo was read, copied or written by me. The repo was not edited apart from this section.
- **Processes.** PID 7967 on 127.0.0.1:8765 was never touched, and afterwards it was still the only `-m rga` process listening there. TestClient binds nothing, and smoke picks its own free port.
- **Edit window.** Since 03:25, the only files changed under `src/`, `tests/`, `tools/` and `scripts/` are B-55's list plus the two test-support files accepted in the FG3c.2 note. Nothing outside scope was edited.
- **Plugin.** `feature-dev:code-reviewer` read the two rebuild functions, `load_oriented` and `_aim_candidates` (read-only), and also read Pillow 12.3.0's JPEG and PNG parsers. It made no finding at or above 80% confidence. Its notes on the docstrings are in G3c4-S1.

### Gates (run by me)
- **Full suite, run once.** `.venv/bin/pytest -q -p no:cacheprovider -rs` (`-rs` only adds skip reasons). Started 05:37:53, after the last source change at 05:07. Exit **0**: **4838 passed, 2 skipped, 1 xfailed**, in 1230 s. This matches the baseline.
  - Skip 1: shellcheck is not installed.
  - Skip 2: `test_eval_real_photos.py:932`, because there are no annotated photos.
  - The real-photo aim tests were not skipped.
- **T3 smoke.** `./scripts/smoke.sh` → exit **0**, **36 PASS / 0 FAIL / 0 SKIPPED**.
- **Lint and syntax.** `ruff check` and `ruff format --check` (no cache) on all 13 FG3c Python files: clean. `node --check review.js`: ok.
- **Analyst gates** [via review-33]:
  - The slice pytest: exit 0, 412 passed, 0 skipped. The real-photo and Chromium tests ran.
  - `tools.eval_detection`: 23/23.
  - Real-photo counts 68/78 with 11 FP, unchanged.

### Cycle-3 findings: re-verification

| Finding | Status | Evidence |
|---|---|---|
| **G3c3-C1** (unbounded rebuild) | **Closed** | Own repro through `POST /api/photos` (TestClient, temp data dir), the 40 MB empty-DHT JPEG: **415 `too many image segments`** in 0.06 s. Traced peak 120.2 MB and RSS +119 MB, which is the same as a 40 MB unsupported-type control (415, 120.2 MB, +160 MB), so the rebuild adds about nothing. Cycle 3 measured 201 at +1.5 GB and 20 s. See the attack table below. |
| **G3c3-W1** (detect 500 on a tiny working image) | **Closed** | Own probe: 32 photos with degenerate manual `circle_1in`/`circle_3in` arcs (working images 10×7 up to 560×420). Each called detect (no ROI, the full ROI, 1-px and 2-px ROIs) and fill-shortfall, and snap-aim was called on each photo, with and without an aim. **0 5xx** in about 490 requests; every aim-less detect returned 200 with `aim_candidates: []`. The cause is now a `ValueError` (`aim.py:143-144`), and the whole search sits inside `except Exception` with a WARNING log line (`review.py:243-259`). |
| **G3c3-W2** (hole pre-selected as the aim) | **Open**: new instance, G3c4-W1 | The original scene is fixed. On `red_grid_band_light_diamond` seeds 0-2, no candidate is within 1 d of a hole and nothing is pre-selected [via review-33]. My sweep of the catalog's 23 scenes, seeds 0-2, gave 0 wrong pre-selections. But tight 10-shot groups still produce one; see G3c4-W1. |
| **G3c3-W3** (2-line lattice doubles the unit) | **Closed** | Read: `reference_grid_lines.py:273` requires `max(v_count, h_count) >= 3`. `test_reference_grid.py:764-793` asserts `find_grid(...) is None` for the hidden-middle case, `#`, the lone square and 2+2. The partial-grid warning is set at `reference_grid.py:583-584` and rendered before confirm (`reference.js:133`). review-33 re-ran the original 210 px/in hidden-middle scene → None; real_02 gives 478 px/in with exactly the warning. |
| **G3c3-W4** (greedy Fill over-count) | **Closed** | Read: `review.js:240-309` is a min-cost one-to-one assignment (Hungarian), with out-of-reach pairs priced at `NO_PAIR_COST`. review-33 ran the page's own functions in node: 0/3000 mismatches against brute force; double and triple drags kept the expected count. The Chromium double-drag tests ran in my full suite. |
| **G3c3-W5** (PNG whitelist untested on the load path) | **Closed** | Own mutants on scratch. **PNG bypass** (`load_oriented` opens the original bytes for PNG) is now **killed by 5 tests**: `test_png_pillow_is_handed_the_rebuilt_bytes`, the bad-CRC test, and acTL/fcTL/fdAT; cycle 3 had 0. The JPEG bypass is killed. Removing the kept-segment cap is killed, and so is removing the PNG kept-chunk cap. With `Image.getexif` removed from the spy's targets, the unrebuilt controls still fire 60/60 through `Exif.load`/IFD loads. So the control is substantive, not self-satisfied. |
| **G3c3-W6** (check-groups "→ 404") | **Closed** | Own API repro: path ids 2^63 and 2^64 → **422 `validation_error`** (`path.zero_id` ≤ 2^63−1), and 2^63−1 → 404 `zero_not_found`. `test_id_bounds.py:191-204` asserts exactly 422 + `validation_error`, and 404 + `zero_not_found`. The FG3b.5 Done note is corrected (`tasks.md:530`). |

### C1 attack table (own, `load_oriented` in a fresh process per case; tracemalloc peak and `ru_maxrss` growth)
All 40 MB floods are **rejected `too many image segments`** in ≤ 0.21 s, at ≤ 0.4 MB traced and ≤ 0.4 MB RSS. The flood kinds were:
- JPEG: empty DHT, empty DQT, DRI, APP0 JFIF, APP14 Adobe, tiny ICC, COM, empty Exif and APP15; 0x00 junk, 0xFF fill, and alternating junk+COM.
- PNG: sBIT, pHYs, tEXt, a private chunk, tiny iCCP, and empty IDAT.

Boundaries and shapes:
- Junk of 4,096 bytes loads; 4,097 is rejected.
- 1,015 dropped 39 KB COM segments (39.6 MB) **load** at a 0.27 MB peak in 0.02 s, with no per-segment copy kept. 1,023 + the base's 8 segments is over the walk cap and is rejected.
- 50 valid DQT segments of 15 KB (11,550 tables) load in 0.22 s. The plugin noted that Pillow's DQT handler is quadratic within one segment. Its worst case at the 1 MB kept-byte cap (15 segments of 65.5 KB) loads in 0.15 s, and 16 are rejected.
- SOF with 19 components → 415 (Pillow refuses). A second SOF → `malformed JPEG frame header`. DRI of 6 bytes → `malformed JPEG restart interval`. Progressive SOF2 loads.
- An ICC budget filled by 17 × 62 KB, then 990 dropped ICC segments, loads at 2 MB.
- PNG with about 19,000 IDATs loads in 0.28 s at 0.7 MB. About 9,700 IDATs alternating with dropped tEXt (about 9,700 slices, the worst case for slices) loads in 0.29 s at 3.0 MB. Just over 20,000 chunks is rejected.
- 58 iCCP bombs, each just under Pillow's 1 MB inflate limit, load at 3 MB in 0.08 s.
- A 1 MB iCCP then 19,000 dropped iCCPs loads at 0.08 MB.
- A 39 MB zTXt XMP made of empty stored deflate blocks (0 bytes out) loads in 0.11 s.
- 154 × 260 KB iTXt XMP chunks with no NUL load in 0.03 s.

Nothing in either walk is super-linear, and no Python object count grows with the input beyond fixed caps (about 2,050 JPEG slices and about 10,000 PNG slices). The plugin agrees, from reading the code.

### SE-2
**SE-2 can now be CLOSED (Resolved).** The lead owns `security-exceptions.md`. This verdict does not depend on G3c4-W1, which is unrelated.
- **The IFD vector is closed.**
  - Cycle 3 closed it.
  - The whitelist is now proven to be on the load path for JPEG and PNG: the bypass mutants are killed.
  - HEIF metadata is capped before any Pillow EXIF parse (`test_heif_metadata_is_rejected_before_pillow_parses_the_exif`, in my full-suite run).
- **The rebuild amplification is closed.** That was SE-2's stated risk reopened by G3c3-C1, and the C1 evidence above covers it.
- **What remains is not SE-2's risk.**
  - Starlette's multipart handling holds about 3 copies of any 40 MB upload (120 MB traced). That is framework overhead and bounded.
  - The CPU issue under "Outside B-55 scope" below is a different class (decode time, not metadata memory). It should be tracked on its own.

### Spec Alignment (FG3c vs B-55/B-56)
- **FG3c.1** matches B-55. The caps are:
  - JPEG: 1,024 walked, 64 kept (ICC included), 1 MB of kept header bytes (ICC has its own 1 MB), 4 KB of junk, one SOF of at most 64 B, DRI exactly 4.
  - PNG: 20,000 walked, 64 kept non-IDAT (IEND counts), fixed sizes, a shared 1 MB iCCP budget.
  - The output is one join of memoryview slices.
- **FG3c.2** matches B-55 for W1 and W4, and B-56 for W2. The pre-select threshold is 0.6 (`review.js:13`, `:319`). The 0.59/0.60 Chromium tests ran [via review-33]. The B-56 size-aware rule is where G3c4-W1 gets in.
- **FG3c.3** matches B-55: at least 3 lines in one family; the warning is on every fallback result and shown before confirm; exact 422.

### Critical
None.

### Warning

**G3c4-W1. The W2 fix still pre-selects a mark sitting on a tight 10-shot group as the aim.** The B-56 size-aware exemption (`aim.py:190`, marks at least 3 bullet diameters across are never filtered) lets two kinds of hole-cluster shape through, both scored ≥ 0.6:
- solid crosses, which always get `size_px = math.inf` (`aim.py:497`), so the shot filter never applies to them;
- diamonds fitted to a cluster (longer diagonal, `aim.py:523`).

This breaks B-55's W2 rule and B-56's own claim ("no candidate on a hole"). [via review-33; reproduced and extended by me]
- **Reproduced (review-33's exact setup).** `paper(1000,1000)`, review-33's 10 hole centres (ES 0.38 in) drawn as 0.224 in dark discs, ROI = hole box ± 20 px, `propose_aims(..., 100.0, bullet_diameter_in=0.224, shots_px=holes)`.
  - #0 is `diamond_center` at **score 0.89**, 0.019 in from the centroid and 0.090 in from a hole: pre-selected.
  - The real 0.8 in diamond 1.7 in away is #1 at 0.34.
  - With the suite's ring-style `draw_holes`, the cluster is not proposed. So the rate depends on how the holes render.
- **Extended, production-like (own).** I rendered generator scenes (`tests/synth` holes) with 10 single shots at `spread_in` 0.35/0.45 and `min_sep_d` 0.4, on the catalog's 1 in green aim dot (`clean_circle1`, `clean_grid`, `noisy_lighting`), seeds 0-19. Shots came from `analyze_group` in the ROI and went into `propose_aims`, as `review.py:247-256` does.
  - **18/120 groups pre-select a wrong aim**, 0.29-0.58 in from the true aim (mostly `cross_center` at 0.75).
  - **12 of those 18 had all 10 shots detected and passed.** In **9 of those 12 the candidate sat within one bullet diameter of a true hole** (0.02-0.13 in).
  - With truth holes as `shots_px` (the input most favourable to the filter): 24/180.
- **Rate on drawn clusters** [via review-33]: 3-7/60 for 10-shot groups at σ 0.10-0.15 in (.224 and .308). 0/60 for 5 shots. A plus, X or T of plain holes → `cross_center` at 0.75-0.99, pre-selected.
- **Why Warning.** This is cycle 3's G3c3-W2 rating, and the same consequence:
  - These are the 10-shot groups B-47 gates the correction on. One "Use this aim" click puts the aim 0.3-0.6 in off, often inside the group, so the POI offset and the correction are wrong.
  - It is not auto-confirmed, and the ring is drawn where the user can see it. That keeps it a Warning, not a Critical.
  - The scene is the app's canonical use case: a tight group shot at a dot.
  - B-49's acceptance set has no group tighter than `spread_in` 0.7, so its tests could not see this.
- **Fix scope** (`src/rga/vision/aim.py`, `tests/unit/test_aim.py`, optionally `tests/synth/aim_scenarios.py`):
  - Give solid crosses their real extent instead of `inf`.
  - Tell a hole cluster from a real mark by how much of the mark's filled region the shot discs cover (for example ≥ 50% inside the union of discs of radius about 0.55 d), not by size alone. The multi_dot_target paster must stay.
  - Tests: seeded tight 10-shot groups on a 1 in dot (generator) and as drawn discs, .224 and .308 → no candidate pre-selected within 1 d of a hole. multi_dot_target and real_02-05 must keep B-49.

### Outside B-55 scope (pre-existing, not counted; escalated for a decision)
- **Progressive-JPEG scan repetition pins a CPU.**
  - Where: `_rebuild_jpeg` passes everything after the first SOS through unexamined (`io.py:167-169`), and libjpeg decodes every scan. It only warns on bogus progression.
  - Measured by me (load average about 45):
    - A flat 2000×1500 progressive JPEG with 1,000 extra 15-byte scans (27 KB) took 3.6 s.
    - With 10,000 extra scans (162 KB) it took **36.8 s**. Both loaded.
    - Raw `Image.open().load()` on the same bytes takes the same time (3.4 s for 1,000), so this is inherent in Pillow/libjpeg and **not caused by FG3c**. It has been present since the first load path.
  - Extrapolated, not executed: a 40 MB file holds about 2.7M such scans, which is hours of one core per upload. Memory stays bounded by the 60 MP cap.
  - On its own merits this is at least a Warning-class availability issue. It is outside B-55's regression scope, so it is not counted.
  - Suggested control: entropy-coded data stuffs every 0xFF, so `data.count(b"\xff\xda", sos)` is an exact scan count. Reject above a small cap (legit progressive files have about 10 scans). Track it as a new security exception or a Group 4 / e2e item; that is the lead's call.

### Requires live validation (not counted)
- **R30-W5, detect + aims vs the ≤ 3 s NFR (spec §133).** This is still outstanding. review-33 measured aims at 0.85-3.74 s and detect at 0.05-0.46 s on real_02-05; real_03 totals about 4.2 s. That was at load average about 34, so it is inconclusive. It needs an idle-machine measurement.

### Suggestion
Recorded, not counted.

**Own / plugin:**
- G3c4-S1 [plugin; verified by reading] The `io.py` docstrings understate the bounds.
  - `_rebuild_jpeg` says "a handful of slices" (`:128`); the bound is about 2,050.
  - `_rebuild_png` says "never one object per chunk" (`:278-279`); the alternating IDAT/dropped case gives one slice per kept run, up to about 10,000. Its line 279 is 114 chars.
  - "64 kept non-IDAT" includes IEND.
- G3c4-S2 The FG3b.5 acceptance text still says "a path id of 2^63 → 404" (`tasks.md:529`); only the Done note was corrected. B-55's W6 rationale says "body validation runs first", but the 422 actually comes from the path bound (`path.zero_id`). Docs only.
- G3c4-S3 Docstring nits from FG3c:
  - stray line breaks at `aim.py:114-115` and `reference_grid_lines.py:238-239`;
  - short why-clauses in `reference_grid_lines.py:237-238` and `aim.py:111-112`. These are brief, not verbose prose.
- G3c4-S4 FG3c.3's file list says `reference_grid.py` "(warning text only)". The change also adds the `GridCandidate.from_partial_lines` field (`:68`) and sets it (`:496`). The Done note describes this, and the mutant that never sets it is killed. Only the scope note was inaccurate.
- G3c4-S5 Both fuzz controls and `TestTheFuzzSpyCanFire` call `img.getexif()`, which is itself a spied target, so `bool(spy)` is satisfied by the call. I verified that the loaders fire anyway (mutant above). Asserting on `Exif.load` or `ImageFileDirectory_v2.load` specifically would make the test say so.

**Vision / editor [via review-33]:**
- G3c4-S6 (R33-S1) real_02's diamond is still #1 (0.006 in) but scores 0.48 < 0.6, so nothing is pre-selected on the photo that motivated B-49. This is consistent with B-55's threshold. The cause is the small-diamond factor (`aim.py:516`). No test pins pre-selection on real photos.
- G3c4-S7 (R33-S2) Grid candidates are appended unsorted after the marks (`aim.py:211-213`), against "sorted by score" (`:113`). This has no effect on pre-selection; the grid scores 0.3.
- G3c4-S8 (R33-S3) For thin crosses the shot filter uses the point from before refinement (`aim.py:189-201`). On pen_plus_circle_sheet seed 2 that leaves the true aim 0.198 in from a hole.
- G3c4-S9 (R33-S4) Neighbouring groups' holes are not in `shots_px` (`review.py:247-248`). A 0.12 cross_center sat 0.145 in from a neighbour's hole; it was never pre-selected.
- G3c4-S10 (R33-S5) W3 residual: hiding alternate lines in both families still gives 2× (420 px/in), but with the partial warning. B-55 accepts "rejected or warned". `_MAX_INDEX_GAP = 2` plus the coarsest-unit tie-break (`reference_grid_lines.py:207`, `:275`).
- G3c4-S11 (R33-S6) Pre-existing: the crossing search gives a silent 2× when lines 1 and 3 of a 5×5 grid are hidden in both directions. Same class as G3c3-S21.
- G3c4-S12 (R33-S7) W4 residual: a placeholder dragged ≥ 1 d cannot pair within reach (`review.js:245`), so Fill over-counts by 1. The extra is an unmoved placeholder, so removing it records no rejection, and B-50 speaks of nudges. Optionally cap fresh shots at max(0, expected − kept).
- G3c4-S13 (cross-slice) `pipeline.py:89` feeds `find_grid` results, partial ones included, into the U1 circle cross-check with no warning on the circle candidate. A warned 2× grid could reorder circle candidates. real_02 is the only partial case today, and it is correct. FG3c.3 made this stricter, not looser.

### Cross-Task Consistency
- **Interfaces hold.**
  - `review.py` passes `bullet_diameter_in` and every proposed and stored shot to `propose_aims` (`:247-256`), and a test asserts the wiring (`test_api_review.py:2018-2042`).
  - `DetectOut.aim_candidates` is unchanged in shape.
  - `PARTIAL_GRID_WARNING` flows unchanged through the proposal and confirm warnings to `reference.js:133` and `:216-224`.
  - `_client_merge` in the tests mirrors the page's assignment [via review-33].
- **Masking, removal reasons, egress and metrics are unchanged by FG3c.**
  - The only rejection reason in `review.js` is `not_a_bullet_hole` (`:221`).
  - No `innerHTML`, eval, inline style, `fetch` or URL in `review.js`.
  - No HTTP client or URL in the FG3c Python files.
  - No mean-radius or best-N identifier.
  - Aim candidates on masked groups are the editor surface accepted under B-39.
- **Docstrings.** Every public module, function and class in the five FG3c source files has a docstring. The only undocumented names are nested closures and the properties of the private `_Line`.
- **Completion claims vs disk.**
  - The FG3c.1 claims match my reproductions and mutants: floods under 0.2 s and 0.2 MB, bypass killed, 4838 passed.
  - FG3c.2's "no candidate on a hole" holds for its stated set (red-band scenes, catalog, real photos), not in general (G3c4-W1).
  - The FG3c.3 claims match the tests.

### Tests
- [x] **All tests passing.** Full suite exit 0, 4838 passed, 2 skipped, 1 xfailed. Smoke exit 0, 36/0/0.
- [ ] **Test coverage adequate.** No: there are no tight-group (σ ≤ 0.15 in) or hole-cluster scenes in the aim acceptance set (G3c4-W1).
- **No assertions on mocks.**
  - The `Image.open` and IFD spies delegate to the real functions, and their assertions are on what real Pillow received.
  - The W1 fault-injection test asserts the real route's 200 and its log line.
  - The `propose_aims` spy asserts the wiring of the seam.
- **Tier coverage.** T1 and T2 are strong. T3 smoke passed. T4 is Group 4's scope. The NFR timing is outstanding (live validation).

### Verdict
This is cycle 4 of Group 3, the one extra cycle the lead authorized under B-55, and it is **final**. There are 0 Criticals and 1 Warning (G3c4-W1), so the bar of zero Criticals and zero Warnings is not met.

**Group 3 is blocked for the user's decision.** C1, W1, W3, W4, W5 and W6 are closed, and SE-2 can be closed. The only blocker is the residual of W2.
- Fix scope, for reference: `src/rga/vision/aim.py` + `tests/unit/test_aim.py` (optionally `tests/synth/aim_scenarios.py`).
- The user's options include:
  - authorizing that fix;
  - accepting G3c4-W1 as a known limitation, since nothing is auto-confirmed and the ring is visible;
  - turning pre-selection off for 10-shot groups.

Separately, the progressive-JPEG CPU issue above needs a tracking decision, and the detect + aims timing still needs an idle-machine measurement.

Verdict: FAIL — 0 Critical, 1 Warning, 13 Suggestion (not counted)
