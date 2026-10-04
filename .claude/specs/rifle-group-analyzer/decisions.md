# Decisions: Rifle Group Analyzer

> Slug: `rifle-group-analyzer` · Source: /co-plan · Date: 2026-09-30
> These are pre-spec decisions settled with the user. fullstack-agent should treat them as settled and flag any conflict as an open question rather than override it.

---

## D-01 Build a custom tool instead of extracting from the Hornady app
- **Context:** The user's data lives in the Hornady Ballistics app's Group Analysis. Research found no export, screenshot-only output (incomplete), no multi-group aggregation, and manual shot plotting.
- **Decision:** Build our own target-analysis tool. The Hornady app drops out of the workflow.
- **Alternatives:** OCR/vision extraction of Hornady screenshots (rejected: incomplete screens, fragile); manual re-entry (rejected: tedious).
- **Reversibility:** Expensive to change later (defines the whole product).
- **Deviations from spec:** None. Pre-spec decision.

## D-02 Local SQLite database is the only data store in v1; Open Brain later
- **Context:** Open Brain stores free-text thoughts with semantic search. It has no structured numeric fields, no structured queries, and no edit or delete, so it can't run the statistics.
- **Decision:** A local SQLite database is the source of truth. Open Brain sync is deferred to a later version, and the data model keeps a clean hook for it.
- **Alternatives:** Local DB plus Open Brain summaries in v1 (recommended by co-plan; user chose to defer); Open Brain only (rejected: fragile, no edits, slow parsing).
- **Reversibility:** Cheap to change later (adding sync is additive).
- **Deviations from spec:** None. Pre-spec decision.

## D-03 Dope is recorded in mils; no ballistic solver
- **Context:** "True the loads at distance" could mean back-calculating muzzle velocity, which would need a solver.
- **Decision:** Record real elevation dialed (mils) and the group center per load and distance. The user back-calculates MV in their own ballistic app and may store it as an optional "trued MV" field.
- **Alternatives:** Built-in MV back-calculation (rejected: biggest single scope item, duplicates the user's app).
- **Reversibility:** Cheap to change later.
- **Deviations from spec:** None. Pre-spec decision.

## D-04 Zeroing follows Formidilosus's method completely
- **Context:** The user first proposed custom rules, then asked for the plan to follow Form's recommendations entirely.
- **Decision:** 100 yd zero. Step 1, get on paper: 1–3 shots after bore sight, dial what you read (the only sub-10-shot correction). Step 2, 10-adjust-10: correct off the center of all shots with no discounting, then overlay to 20, with a 0.1 mil touch-up acceptable and an optional 30-round confirm. No corrections off 3–5 shot groups otherwise. Step 3: the 20+ shot extreme spread becomes the dot. Zero check: 1–2 shots inside or touching the dot means pass. Outside means "possible lost zero," back to Step 2, plus a mount and scope check. No ballistic-app offsets. Lots tracked. All corrections in 0.1 mil, mil/mil scope assumed. (Sources: MIL-FORM-001/-003, TIK-FORM-010/-028/-030, 22CM-FORM-016/-020, CBHB-FORM-024/-061, SFE-FORM-003, WYBTS-FORM-134/-142; claims LD-22, FB-12, OPT-22.) Routing a failed zero check back to Step 2 is co-plan's wiring; Form states the pass condition, not the fail procedure.
- **Alternatives:** The user's own rules (superseded, see D-05); a co-plan draft blending the user's thresholds with Form's logic (superseded).
- **Reversibility:** Cheap to change later (rule logic is isolated).
- **Deviations from spec:** None. Pre-spec decision.

## D-05 SUPERSEDED: user's original zeroing thresholds and distance scaling
- **Context:** Earlier in the conversation the user specified: >4" off means immediate exact correction even off one shot; ≤2" means wait for 10 rounds; 9+ shots means correct whenever ±0.05 mil rounds to a 0.1 click. A co-plan draft filled the 2–4" gap and scaled the 4" line by distance as an angle (≈1.1 mil).
- **Decision:** Superseded by D-04 at the user's request. Kept here for traceability.
- **Alternatives:** n/a
- **Reversibility:** n/a
- **Deviations from spec:** None. Pre-spec decision.

## D-06 Hole detection: classic computer vision plus mandatory user review, no AI
- **Context:** Vision language models are imprecise at pixel localization, and the statistics need sub-0.1" accuracy. Form's "simpler wins" principle (MET-25) argued against the AI cross-check.
- **Decision:** Classic local CV detects hole centers. The user always reviews (add, drag, delete as detection error, mark aim point). No vision-model or API call in v1.
- **Alternatives:** CV plus review with a Claude API hole-count cross-check (initially accepted, then dropped in the Form audit; recommended by co-plan, accepted by user); vision model places holes (rejected: imprecise); local vision model (rejected: heavy and weaker).
- **Reversibility:** Cheap to change later (a count-check can be added if detection proves unreliable).
- **Deviations from spec:** None. Pre-spec decision.

## D-07 Hole marker size is the fixed bullet diameter scaled from the reference
- **Context:** The user didn't want hole size to come from what the detector or model "sees."
- **Decision:** Detection finds centers only. Each hole is drawn at the load's bullet diameter, scaled with the reference object. Measurements are center to center.
- **Alternatives:** Size from detected blob (rejected by user).
- **Reversibility:** Cheap to change later.
- **Deviations from spec:** None. Pre-spec decision.

## D-08 Reference object chosen by dropdown
- **Context:** Targets vary, but every photo contains a known-size reference.
- **Decision:** A dropdown with 1" grid, 1" circle, 1.5" circle, 3" circle. The reference sets the scale, and a grid also enables perspective correction.
- **Alternatives:** Auto-detect the reference type (deferred; manual selection is simpler and reliable).
- **Reversibility:** Cheap to change later (options are additive).
- **Deviations from spec:** None. Pre-spec decision.

## D-09 Interface: local web app on the Mac
- **Context:** The workflow needs visual review (click-to-fix holes, dropdowns, overlays).
- **Decision:** A local web app served on localhost and used in the browser. Photos arrive via AirDrop or files.
- **Alternatives:** Phone or range access (rejected for v1: hosting, networking, security scope); native Mac app (rejected: slower to build, little gain). Recommended by co-plan, accepted by user.
- **Reversibility:** Expensive to change later if remote access is wanted (security and hosting design).
- **Deviations from spec:** None. Pre-spec decision.

## D-10 Multiple rifles from the start
- **Context:** Retrofitting multi-rifle support later would require reworking the data model.
- **Decision:** All loads, groups, zeros and dots are scoped to a rifle (with its scope).
- **Alternatives:** Single rifle (rejected). Recommended by co-plan, accepted by user.
- **Reversibility:** Expensive to change later.
- **Deviations from spec:** None. Pre-spec decision.

## D-11 Load record: lean core recipe
- **Context:** The user accepted the "core recipe." The Form audit then trimmed what he considers irrelevant (LD-04 seating depth, LD-05 velocity SD/ES, LD-13 brass).
- **Decision:** Factory: maker, product, bullet weight and type, lot. Handload: bullet, powder, charge, primer, batch ID. Optional trued MV. No velocity SD/ES. COAL/CBTO not tracked (notes only).
- **Alternatives:** Minimal record; full reloading log (both rejected).
- **Reversibility:** Cheap to change later (fields are additive).
- **Deviations from spec:** None. Pre-spec decision.

## D-12 Session data captured
- **Context:** Determines how groups can be filtered and compared.
- **Decision:** Date, rifle, distance, shooting position, suppressor on/off, notes. Weather basics are prompted only at ≥ 200 yd. Barrel round count is not tracked.
- **Alternatives:** Weather always; barrel round count (not selected).
- **Reversibility:** Cheap to change later.
- **Deviations from spec:** None. Pre-spec decision.

## D-13 The user picks which groups to overlay
- **Context:** Aggregation is only valid if the scope wasn't adjusted between groups.
- **Decision:** The user selects groups to combine, and the tool trusts the selection (no turret-state enforcement). Dialed elevation is still recorded per group as dope data.
- **Alternatives:** Log turret state per group and allow overlays only on matching state (recommended by co-plan; user declined).
- **Reversibility:** Cheap to change later.
- **Deviations from spec:** None. Pre-spec decision.

## D-14 Metrics: extreme spread is the headline; no mean radius
- **Context:** Form rejects mean radius as a precision claim because it lets you ignore the shot that would miss (LD-20). There are no "fliers" (LD-26).
- **Decision:** Report extreme spread (inches and MOA) and mean POI offset (mils). No mean radius, no group-reduction or best-N views. Shots are removed only as detection errors.
- **Alternatives:** Mean radius as a secondary metric (in the initial plan; dropped in the Form audit).
- **Reversibility:** Cheap to change later.
- **Deviations from spec:** None. Pre-spec decision.

## D-15 Load comparison guardrails
- **Context:** Small groups read noise (LD-01). A difference must fall outside normal variation (LD-27). 10-round groups vary about ±30% with nothing changed (CBHB-FORM-013).
- **Decision:** No ranking under 10 rounds per load (20+ preferred, higher confidence). A "no real difference" verdict within normal variation. A precision verdict against sub-2 MOA/10 shots for deer and elk to 600 yd (LD-07). POI shift reported vs the zero load, with advice to zero to the hunting load.
- **Alternatives:** Unguarded comparison (rejected).
- **Reversibility:** Cheap to change later.
- **Deviations from spec:** None. Pre-spec decision.

## D-16 Explicitly out of v1
- **Context:** Scope control.
- **Decision:** Out: Open Brain sync, phone or range use, chronograph import, ballistic solver, any AI call, dot-drill hit/miss logging, zero-history log, stringing analysis. (Dot drills and zero history were proposed in the Form audit, LD-23 and MET-20, and not selected by the user.)
- **Alternatives:** Including any of the above.
- **Reversibility:** Cheap to change later (all additive), except phone use (see D-09).
- **Deviations from spec:** None. Pre-spec decision.

## D-18 Unresolvable shots in ragged holes are placed by best guess
- **Context:** In larger groups, several rounds often pass through one jagged hole, and their individual positions can't be seen. Form's rule is that every shot counts (LD-26), so dropping them isn't acceptable.
- **Decision:** The user enters the shots fired. Once every distinct hole is accounted for, any shortfall is placed by best guess inside the larger jagged hole(s), marked "estimated," included in all stats, and draggable.
- **Alternatives:** Require exact placement (impossible from the photo); exclude unseen shots (violates "no discounting shots").
- **Reversibility:** Cheap to change later.
- **Deviations from spec:** None. Pre-spec decision (added by the user after approval).

## D-17 Technology: Python (recommended)
- **Context:** OpenCV is native to Python, and the user's other project already uses a Python venv.
- **Decision:** Python for the backend and CV, SQLite storage, a local web UI. Exact frameworks are left to fullstack-agent. Recommended by co-plan, accepted by user (not separately discussed; confirm in planning if there's a reason to differ).
- **Alternatives:** Node/TypeScript with OpenCV.js (weaker CV ergonomics).
- **Reversibility:** Expensive to change later.
- **Deviations from spec:** None. Pre-spec decision.

---

# Build-phase decisions (lead)

## B-01 Degraded coordination: tasks.md is the task board (2026-09-30)
- **Context:** The shared task tools (`TaskCreate`, `TaskUpdate`, `TaskList`, `TaskGet`) don't exist in this desktop-app session. Single-name ToolSearch selects returned "No matching deferred tools found," and they're absent from the deferred-tool list. `Agent` and `SendMessage` work. Missing ~/.claude config was installed from the sample-claude-code-agent-team repo, but that didn't restore the task tools.
- **Decision:** The user explicitly approved building in this session ("do it here"). The lead assigns tasks to named workers by spawn prompt or `SendMessage`. Workers never edit `tasks.md`, so there's no same-file race. The lead records claims and completions there from worker reports and checks them against disk. `test-evidence.md` is still required. Verification sentinels are skipped because no `TaskCompleted` hook fires without the task store, so the lead checks evidence by hand. Reviewers remain independent `review-agent` instances; the lead never authors `review.md`.
- **Alternatives:** Restart in a terminal `claude` session where the task tools may be available (offered; user declined); single-threaded build (not offered).
- **Reversibility:** Cheap to change later (a future session with the task tools can import `tasks.md` state).

## B-02 No sa-agent (planner P-14)
- **Context:** sa-agent is mandatory when work touches IAM, KMS, security groups or network exposure, or an IaC state backend.
- **Decision:** Not spawned. The app is local only: loopback bind, no cloud resources, no IAM or KMS, no state backend. The loopback, Host-allowlist and Origin controls are reviewed by review-agent against design.md Security Considerations.
- **Alternatives:** Spawn sa-agent for a security pass (no AWS surface to assess).
- **Reversibility:** Cheap to change later.

## B-04 Planner open questions answered by user (2026-09-30)
- **Q-3, zero center:** use the **mean point of impact** (centroid of all shots), as spec.md already does (CBHB-FORM-001). The enclosing-circle center (CBHB-FORM-040) was rejected because one outer shot would move the zero.
- **Q-7, Step 2 minimum:** **10 shots**, as specced. The 7–8 shot corrections in WYBTS-FORM-134/-142 were noted and not adopted.
- **Q-1 / P-01, dot size:** the standard dot nearest to **ES − bullet diameter**, confirmed after a plain-language explanation. "Touching" already adds about one bullet width, so this makes a passing check match the real cone.
- **Q-2 / P-02, Step 2 overlay:** **center on center.** The two 10-shot groups are aligned on their own centers before the 20-shot ES is measured (Form's literal method, 22CM-FORM-020). The zero is judged on the confirming group's center. Shifting by the dialed correction was rejected because scope tracking error would leak into the ES.
- **Q-4 / P-13, comparison:** **like with like.** Loads are compared on the average of their 10-shot groups, and separately on 30-shot aggregates when both loads have them. The sub-2 MOA verdict uses the 10-shot average (LD-07 is stated per 10 shots). Larger groups never penalize a load.
- **Q-6 / P-08, multiple groups per photo:** **in v1.** Each group has its own region and aim point.
- **Q-5 / P-10, positions:** `bench, prone_off_pack, kneeling_sitting, standing_field`. The user removed `sandbags`.
- **Q-8 / P-11, tolerances:** **accepted.** Center error p95 ≤ 0.05" on real photos and ≤ 0.03" on synthetic.
- **Reversibility:** Cheap to change later (rule parameters).

## B-03 git init approved
- **Context:** The planner's Q-10: reviews need a diff baseline.
- **Decision:** The user approved `git init` in /Users/calebschwarz/Documents/Hunting. devops-1 runs it as the first build step. No commits unless the user asks.
- **Reversibility:** Cheap to change later.

## B-05 Lead-added task T1.10: global error model handler
- **Context:** The `{detail, code}` error contract is required by T1.7 schemas and T1.9 `api.js`, but no task owned a global handler. FastAPI's default HTTPException body lacks `code`. coding-2 flagged this.
- **Decision:** Added T1.10, scoped to `web/app.py` (already owned by coding-2 from T1.6) plus a new test file. It stays inside Group 1 with no file overlap.
- **Reversibility:** Cheap to change later.

## B-06 API gaps from T1.7: group list route; group-to-photo link
- **Context:** coding-4 found design §4.7 had no route to list groups (needed by the /analysis picker for `POST /api/aggregate`) and no explicit group→photo link (needed for several groups per photo).
- **Decision:** Add `GET /api/groups?session_id=&load_id=&rifle_id=&status=`, returning a bare array and backed by the existing `list_groups` repo function. Accept `photo_id` on `GroupCreate`/`GroupUpdate`. design.md §4.7 is updated, and coding-4 adds `GroupListQuery` plus the registry entry in schemas.py. The Group 3 analysis-routes owner implements the route.
- **Reversibility:** Cheap to change later.

## B-07 Review cycle log
- **Group 1, cycle 1:** synthesizer `review-1` and analyst `review-2` spawned 2026-09-30. This consumes Group 1 cycle 1 of 3. Result: FAIL (0 Critical / 13 Warning); fix wave F1.1–F1.5 done.
- **Group 1, cycle 2:** synthesizer `review-3` and analyst `review-4` spawned 2026-09-30. This consumes Group 1 cycle 2 of 3. Result: FAIL (0 Critical / 4 Warning: W3-R, W11-R, N1, N2); fix wave F2.1–F2.3.
- **Group 1, cycle 3 (TERMINAL):** synthesizer `review-5` and analyst `review-6` spawned 2026-10-01. This consumes Group 1 cycle 3 of 3. **Result: PASS** (0 Critical / 0 Warning / 14 Suggestions). Group 2 opened (B-08).
- **Group 2, cycle 1:** synthesizer `review-7` spawned 2026-10-01 (pipelined; analysts `review-8` domain and `review-9` db/live-server start now; `review-10` vision is spawned once T2.15/T2.16/T2.19 land). This consumes Group 2 cycle 1 of 3. **Result: FAIL** (0 Critical / 23 Warning / 39 Suggestion); fix wave FX2.1–FX2.7 (B-23).
- **Group 2, cycle 2:** synthesizer `review-11` + analysts `review-12` (domain), `review-13` (db + live server), `review-14` (vision + tools) spawned 2026-10-01. This consumes Group 2 cycle 2 of 3. **Result: FAIL** (0 Critical / 8 Warning: W1-R, W9-R, W11-R, W17-R, W19-R, W22-R, N1, N2); fix wave FY2.1–FY2.7 (B-27). **Cycle 3 is TERMINAL.**
- **Group 2, cycle 3 (TERMINAL):** synthesizer `review-15` + analysts `review-16` (domain + services), `review-17` (db + live server), `review-18` (vision + tools) spawned 2026-10-01. This consumes Group 2 cycle 3 of 3. **Result: FAIL** (0 Critical / 1 Warning: G2-W17-R2, an EXIF-parse guard regression from FY2.3). **Group 2 is BLOCKED pending the user's decision** (budget exhausted; no further fix or review work without user authorization).
- **Group 3, cycle 1:** synthesizer `review-20` + analysts `review-21` (photos, review editor, truth export), `review-22` (sessions, zeroing, masking), `review-23` (rifles, analysis, compare, dope) spawned 2026-10-02. This consumes Group 3 cycle 1 of 3. Pre-review state: all 19 Group 3 tasks [x]; full suite 3986 passed / 2 skipped / 1 xfailed (worker, after T3.11); T3 smoke 36/0/0 (lead re-run). **Result: FAIL (2 Critical / 9 Warning / 33 Suggestion).** Fix wave FG3.1–FG3.7 (B-44).
- **Group 3, cycle 2:** synthesizer `review-24` + analysts `review-25` (io/SE-2 + photos), `review-26` (zeroing/sessions/review editor), `review-27` (ids, aggregates/compare, dope, export) spawned 2026-10-02. This consumes Group 3 cycle 2 of 3. Pre-review state: FG3.1–FG3.7 all [x]; full suite 4309 passed / 2 skipped / 1 xfailed (worker, after FG3.6); smoke 36/0/0 (lead re-run). **Result: FAIL (1 Critical / 5 Warning / 14 Suggestion); all 11 cycle-1 findings fixed.** Interrupted by the rate limit and resumed (B-48). Fix wave FG3b + user items (B-50).
- **Group 3, cycle 3 (TERMINAL):** synthesizer `review-28` + analysts `review-29` (zeroing check-groups, saved aggregates, id sweep, masking), `review-30` (vision U1/U2/UF2 + review editor W3), `review-31` (UF1 group page + correction gating) spawned 2026-10-02. This consumes Group 3 cycle 3 of 3. Pre-review state: FG3b.1/2/4/5, U1, U2, UF1, UF2, UF3 all [x]; smoke 36/0/0 (lead re-run); full suite 4730 passed (worker, before the final aim tweaks). The user's app server was restarted on the new code (PID 93688, schema v2). **Result: FAIL (1 Critical / 6 Warning / 23 Suggestion).** All cycle-2 findings were closed; the C1/W1–W6 below are new. Group 3 is blocked → lead-authorized cycle 4 (B-55).
- **Group 3, cycle 4 (LEAD-AUTHORIZED under B-32, B-55; final):** synthesizer `review-32` + analyst `review-33` (vision: grid fallback + aim) spawned 2026-10-02, scoped to the FG3c files plus the full-suite and smoke gates. Pre-review state: FG3c.1–3 [x]; full suite 4838 passed (worker); smoke PASSED (lead). User app restarted (PID 7967). **Result: FAIL (0 Critical / 1 Warning / 13 Suggestion).** G3c3-C1 and W1–W6 are closed; SE-2 is verified and can be closed. One Warning remains: G3c4-W1, aim pre-selection on tight 10-shot clusters. Group 3 is BLOCKED and goes to the user (B-57).
- **Group 4, cycle 1:** synthesizer `review-34` + analysts `review-35` (data and logic: migrations 003–006, repos, the B-66 services, compare/dope/zeroing, export), `review-36` (vision and security: io SE-3/SE-4, reference circle/grid, aim, review editor + scale reset), `review-37` (web: pages, JS, share, settings, Groups tab, client contract, e2e, smoke) spawned 2026-10-02. This consumes Group 4 cycle 1 of 3. Pre-review: all Group 4 tasks [x] except T4.0c (deferred, B-80); full suite 5909 passed (worker, after T4.0s); smoke 40/0/0; scan 0/0.

## B-08 User authorizes running all groups without per-group go-ahead (2026-09-30)
- **Decision:** The user said "run all groups one after the other until finished." The lead advances to the next group as soon as the current group's synthesizer returns PASS, without waiting for approval.
- **Still escalated to the user:** a scope reaching cycle 3 without PASS (terminal per protocol), and a failure of the Group 2 detection spike gate (spec §6, P-11). Non-CV Group 3 slices still proceed in that case, as tasks.md specifies.

## B-09 Lead decisions on Group 1 cycle 1 review (FAIL: 0 Critical / 13 Warning)
- **W10, ragged vs split:** the fixture is brought into line with design §7. Every pair inside a ragged cluster is ≤ 0.5 d apart, below the P-11 mandatory-split floor of 0.6 d, so a ragged blob is unambiguously unresolvable and AC-9's 3 estimated shots are well defined. P-11 is unchanged. Tests assert max member separation ≤ 0.5 d, an area band, and that the cluster renders as one component with no single attached.
- **W13, no_reference fixture:** its aim mark becomes a non-circular mark (diamond), so "no candidates" in T2.2/T4.11 stays true. The criteria are unchanged.
- **W7, anti-framing:** a new design §11 row covers the CSP / X-Frame-Options / nosniff / no-referrer headers, implemented in `web/security.py`.
- **S14:** `compensate()` is removed from design §4.4 (superseded by center-on-center, B-04).
- **S13:** T3.4 gains a "no silent drops" criterion: a review PUT can't drop a stored shot without a `not_a_bullet_hole` rejection (409 `shot_dropped_without_reason`).
- **Open question, mil POI on short zeroing groups:** **No.** For `zero_step2` / `zero_confirm` groups with < 10 shots, no mil POI, offset or direction text is shown or returned in advice/stats. General-purpose groups still show mean POI, since it's needed for POI-shift analysis and is not a recommendation. This follows D-04's must-never rule and drives the W3 fix.
- **Fix-wave scope:** the 13 Warnings only. The 27 Suggestions are deferred and recorded in review.md.
- **Group 1, cycle 2** is the next review after the fix wave (B-07).

## B-10 Accepted deviation in F1.1
- coding-2 edited `src/rga/web/app.py`, which isn't in F1.1's file list, to install the `SecurityHeaders` middleware and add the headers to the 500 handler. There's no other place to wire it, coding-2 already owns app.py (T1.6, T1.10), and no other fix-wave task touches it. Accepted.
- A known limitation is noted: a migration file that issues its own COMMIT is detected and raises, but its committed effects can't be rolled back. Migrations are authored in-repo only, so this is acceptable.

## B-11 The spike accuracy gate covers all 17 synthetic scenarios
- **Context:** F1.5 added three stress scenarios (`hires_wipe_decoys`, `close_holes_tight_pair`, `black_dot_mixed`) to meet design §7's breadth (review W12).
- **Decision:** The P-11 synthetic thresholds apply to all 17 scenarios. The new ones are the realistic phone-photo cases, so excluding them would defeat W12. If any scenario misses a threshold, `spike-report.md` names it, and the lead escalates per B-08. Non-CV Group 3 slices continue.
- **Reversibility:** Cheap to change later.

## B-12 Real photos inform Group 2 (2026-09-30)
- **Context:** The user supplied six real target photos (five saved to `data/real_photos/`, about 3 MP chat-downscaled; the sixth wasn't saved). The analysis is in `real-photo-notes.md`. They show traits the synthetic catalog lacks: black, red and green grids; light-core holes; QR, text, pen-circle and staple decoys; shadow wrinkles; a sticker on cardboard; curved paper; 180° rotation; tape-cross aims; several groups per sheet.
- **Decision:**
  - (1) Add lead task **T2.18**, which extends the synthetic catalog with these traits. It's owned in Group 2, since Group 1 files are past their fix wave.
  - (2) T2.2, T2.3 and T2.4 gain real-photo-trait criteria.
  - (3) Curved paper → a `non_planar` warning plus a re-shoot prompt, with no non-rigid warp.
  - (4) The spike reports a qualitative per-photo table on the real photos, which isn't gated until ground truth exists.
  - (5) The real-photo gate should use full-resolution originals.
- **User answer on reused sheets:** **always use a fresh sheet.** Every hole inside a group's region counts. There is no "previous group" reason, so D-14/FR-3.5 are unchanged (`not_a_bullet_hole` only). Several groups on one fresh sheet, each with its own ROI, are still supported (P-08). The pen-circle decoy scenario is dropped from T2.18.
- **Reversibility:** Cheap to change later.

## B-13 Lead decisions on Group 1 cycle 2 (FAIL: 0 Critical / 4 Warning)
- **Fix-wave scope:** W3-R, N2 (coding-4); W11-R (coding-3); N1 (coding-2). That's the Warnings only. **Cycle 3 is terminal.** If it doesn't PASS, Group 1 is reported blocked to the user.
- **S47 (group ROI excludes its own holes):** moved into T2.18 (Group 2 owns the synth files next), so Group 1's last fix wave stays narrow while the fix still lands before Group 2 consumers.
- **S28 (table-rebuild migration cascade-deletes):** logged as a limitation alongside B-10. **Rule:** no table-rebuild migration may be written without a dedicated helper that runs `PRAGMA foreign_keys=OFF` outside the transaction and checks `foreign_key_check` afterwards. v1 has only 001.
- **S30 (Referrer-Policy no-referrer → native form POST sends Origin: null → 403):** keep `no-referrer`. **Rule:** no native HTML form submissions; every write goes through `api.js` fetch (same-origin Origin header). Group 3 reviewers enforce it.
- **S33 (stray file `circle`, contents "M6 diamond-"):** moved to `/tmp/rga_stray_circle_file.txt`, not deleted.
- **float32 rounding (review-4 note):** behavior stays (round the exactly widened float). The docstring wording can be corrected in a later touch of units.py. Not a defect.

## B-14 Closing the F2.1 residuals at the producer side
- **Bare direction words without numbers** ("group is high and right; shoot more"): the schema can't ban bare words without false positives on count text. Instead, the domain zeroing module (T2.7) emits SHOOT_MORE/INVALID messages **only from fixed integer-count templates**, with an exact-string test. The schema guard (F2.1) is a backstop.
- **Per-shot offsets on short zeroing groups:** `ShotOut.dx_in/dy_in/dx_mil/dy_mil` are already nullable. The services (T2.14/T3.5/T3.7 short-zero-group guard) null them for `zero_step2`/`zero_confirm` groups with < 10 shots. Positions stay so the editor can draw. No schema change.
- **Reversibility:** Cheap to change later.

## B-15 Lead decisions from Group 1 cycle 3 notes
- **Zero-check messages:** T2.7 emits zero-check pass/fail text only from fixed templates with no number, direction or unit, with exact-string tests. Every SHOOT_MORE/INVALID/zero-check template is validated through the schema models in tests, so a rejected template can't surface as a 500.
- **Dope from short zeroing groups:** T3.8 excludes `zero_step2`/`zero_confirm` groups with < 10 shots from dope rows.
- **S48–S50 (message-guard breadth):** deferred as Suggestions. The producer-side fixed templates (B-14/B-15) are the primary control, and the schema guard is a backstop.

## B-16 User notes are never altered
- **Context:** coding-6 raised whether B-09's short-zero-group rule should blank user free-text notes (e.g. "shot high left").
- **Decision:** No. Notes are the user's own words and are shown verbatim everywhere. B-09 governs numbers and direction text the *app* produces, not the user's notes.
- Also: `SummaryGroup` gains `purpose: GroupPurpose = GENERAL` (an additive change to design §4.4). The service mapper sets it from `groups.purpose`.

## B-17 Dope rows exclude zeroing-workflow groups
- `repo_dope.dope_rows` excludes `zero_step1` and `zero_check` groups entirely (they're 1–3 shot workflow groups), plus `zero_step2`/`zero_confirm` groups with < 10 stored shots (B-15). It's implemented in the repo query; T3.8 keeps only a defensive check.

## B-18 Zero-check groups never show mil POI or offsets
- **Context:** T2.14 and T2.9 kept POI for `zero_check` groups (1–2 shots), since B-09 named only step2/confirm.
- **Decision:** A zero check is pass/fail against the dot (22CM-FORM-020). A fail routes back to Step 2 and never to a correction off 1–2 shots (D-04). So `zero_check` groups of any size get the same masking as short zero groups: size-only stats, null per-shot dx/dy (in and mil), and no POI or direction text in summaries. Positions stay for drawing, and the pass/fail result plus "inside/touching the dot" is shown. `zero_step1` groups keep offsets, because Step 1 corrects from them.
- **Follow-ups:** coding-6 updates `services/groups.py` (`is_short_zero_group` → `masks_poi(purpose, n)` or equivalent) and `domain/summaries.py`. The T2.14/T3.5/T3.7 guard text is updated.

## B-19 Spike result and the real-photo gap (user decision 2026-10-01)
- **Spike (T2.4):** the synthetic P-11 gate is met on all 23 scenarios, so there's no B-08 escalation.
- **Real photos (not gated):** about 10–70% recall plus 1–8 false positives per photo. real_02 (grey rims with pinholes) found about 1 of 9. The synthetic catalog is still easier than reality. Manual review (D-06) keeps the product usable.
- **User chose:** "Rough tuning pass now too."
  - (1) **T2.19** (coding-1): a count-based tuning pass on real_01..05, in parallel with the rest of Group 2. Hard constraints: the synthetic gate stays green with no loosening, interfaces are unchanged, and overfitting is avoided.
  - (2) **Exact-truth tuning later:** once the Group 3 review editor exists, the user marks holes on 10–20 full-size photos in the app, and those clicks become ground truth for the T2.15 real-photo gate and a scored tuning task (to be added after Group 3).
- **Recorded deviations:**
  - Grid detection (T2.3) uses a black-hat line mask + crossings + lattice growth instead of design §6's HoughLinesP sketch.
  - "Reject if rms > 0.02" became a `non_planar` **warning** above 0.05 in (planar synthetic rms ≤ 0.02 is still asserted).
  - Known limit: grids with sub-inch minor lines would lock to the finer spacing (2× scale error).
  - real_02 doesn't auto-detect its grid (only 6 crossings visible behind the band), so manual 4-corner applies.
- **Renderer note (Suggestion, not fixed):** the synthetic discs render about 1.2 px wider than truth (LINE_AA). The circle tests use 0.3% + 1.5 px against truth and strict 0.3% on exact renders.

## B-20 Aggregates and comparisons exclude POI-masked groups
- **Context:** review-8 noted that `domain.stats.aggregate` and `domain.comparison.poi_shift_mil` don't know group purpose.
- **Decision:** the Group 3 services (T3.5 aggregate, T3.6 compare) exclude or refuse any group where `masks_poi` is true, so masked POI can't leak through an aggregate center or a POI shift. This is enforced in services, with tests.

## B-21 Real-photo tool decisions (T2.15), accepted
- (1) Two or more annotated holes inside one detected ragged blob are exempt from recall (reported as `ragged_holes`), matching FR-3.7's best-guess fill.
- (2) The scale row compares the auto proposal with the user-confirmed reference. There's no separate hand-measured scale field.
- (3) Synthetic circle-photo tests use `--scale-pct 1.0` because of the renderer width note (B-19). Real photos use the default 0.5%.
- (4) Fewer than 10 annotated photos prints "provisional"; the exit code still reflects the metrics.
- **Group 3 follow-up:** add an export task (`truth.json` from a finalized group, reimplementing `plane_in_to_oriented_px` in services, since tools can't be imported by services) so the user never hand-writes JSON (B-19 path to exact truth).
- **Orphan cleanup:** three leftover `python -m rga` test servers from the T2.17 window (PIDs 33445/33516/33755, started 01:20) were found by review-9 and stopped by the lead with SIGTERM to their process groups. Their ports were verified free. The root cause (the leak check used the wrong process-name pattern) is review-9 W8, for the fix wave.

## B-22 T2.19 tuning result accepted
- Real-photo recall against visual counts went from 47/78 to 69/78 holes, with FPs from 33 to 9. The synthetic gate stays at 23/23 with no loosening.
- `DetectParams` defaults changed (`dark_rel` 0.35→0.5, `min_area_a1` 0.35→0.55), plus new defaulted fields. The interface is unchanged.
- Known limits: holes overlapping a black diamond, the third hole of a clump, coloured-ring edges, the tape-cross navy dot (T2.16 xfail kept), and cardboard behind the sheet (handled by the group ROI). The five-photo sample is small; the exact-truth gate (B-19/B-21) remains the real measure.
- `tests/real/counts.json` gives approximate positions with uncertain entries flagged. **It's offered to the user for confirmation of per-photo shot counts.**

## B-23 Lead decisions on Group 2 cycle 1 (FAIL: 0 Critical / 23 Warning)
- **Fix-wave scope:** G2-W1..W23 only. The 39 Suggestions are deferred, apart from those already routed to Group 3 below.
- **B-21(1) revised (W19):** the real-photo scorer matches **resolved shots** (detected + split + estimated, after `resolve_shots`) to annotated holes via Hungarian at 0.5 d. Annotated holes inside a detected ragged blob count as recalled **only if** the resolved shots placed inside that blob's outline are ≥ the annotated holes inside it; otherwise the shortfall counts as misses. The detector can no longer exempt holes for itself.
- **Unevaluated rows never pass (W19):** a row that can't be evaluated (e.g. scale with no confirmed reference in the truth file) makes the verdict INCOMPLETE, exit 1, not PASS. The gate test needs a fixture that must fail, and it must go red.
- **W10/W11 link consistency is fixed in the repos** (create_group/update_group/update_session, update_zero lot_id, create_zero_check), matching `link_group`/`create_zero`, with negative tests. The services still map these errors to 409.
- **W12:** remove the `min_zeroing_shots` override from `dope_rows`. The threshold is the mirrored constant only.
- **W18:** `io.load_oriented` rejects extreme aspect ratios (long/short > 20) as `ImageRejected`, so T3.3 relies on `ImageRejected` alone. Both reference detectors also raise `ValueError` (not an OpenCV crash) on degenerate dimensions.
- **W22:** every T2.19 cue needs a **non-skipping** test (drawn scene or synthetic) that fails without it. A cue that can't be justified beyond one photo is removed, and the real-photo before/after is re-reported honestly. The lead corrects the T2.19 Done note.
- **W5:** the docstring gate extends from `rga.domain` to all of `src/`, `tools/` and `tests/support/`.
- **Corrected completion claims (W15, W16, W22):** the T2.11, T2.17 and T2.19 Done notes are annotated by the lead.
- **Routed to Group 3** (acceptance criteria added): PATCH of purpose or session recomputes offsets in the same transaction (T3.2/T3.5); the stats route handles a group with no shots (T3.5); the Advice→AdviceOut conversion criterion moves from T2.14 to T3.7 (G2-S8); T3.7's zero check reads the dot from the *complete* zero, not `active_zero` (G2-S25); T3.9's teardown check uses PIDs and process groups, never `pgrep "python -m rga"` (W16).

## B-24 SQLite write contention
- After FX2.1, a second writer during a service transaction gets "database is locked" instead of interleaving.
- **Decision:** add `PRAGMA busy_timeout = 5000` in `db.connection.connect` so brief contention waits instead of failing. This is a small Group 3 lead task, since `connection.py` is outside this fix wave. Routes map any residual lock error to 503 `busy` (T3.2 criterion; applies to all write routes).

## B-25 Live-server test timeouts loosened (accepted)
- FX2.7 follow-up: the SIGINT flake's root cause was inherited ignored signals in background launchers (reproduced). The nested pytest resets its dispositions now.
- Test-infra defaults raised: `startup_timeout` 15→30 s, `terminate_timeout` 10→20 s, request timeout 10→30 s, nested-test waits bounded at 30/180 s. This is a deliberate loosening of the T2.17 spec's 15 s startup, for robustness under concurrent agent load. It affects test infra only, not the app.
- devops-1 used temporary CPU-hog processes to verify under load, then killed them by recorded PID. Its 10/10 loop passed, as did a concurrent full suite (2910 passed, 2 skipped, 1 xfailed).

## B-26 Abandoned short confirm groups can be detached (Group 3)
- review-12 noted that, with every confirm group size-checked (FX2.2), an abandoned short confirm group makes all later judgments INVALID.
- **Decision:** T3.7 lets the user detach such a group from the zero (it becomes `general`, with offsets recomputed via the group service). Judgment uses linked confirm groups only. The domain rule (every judged confirm group needs ≥ 10) is unchanged.

## B-27 Lead decisions on Group 2 cycle 2 (FAIL: 0 Critical / 8 Warning)
- **Scope:** the 8 Warnings only, one file set each per review-11's map. Nothing else, because cycle 3 is terminal.
- **W11-R:** remove `lot_id` from `ZERO_UPDATABLE`. A zero's lot is fixed at creation (a new lot means a new POI check, SFE-FORM-003), so the mismatch state can't be reached.
- **W17-R:** apply EXIF orientation by reading tag 0x0112 directly and transposing, not via `ImageOps.exif_transpose` re-writing the whole EXIF block. Malformed unrelated tags are then ignored and a valid image still loads. Fuzz EXIF tag types (all TIFF types on Make/Software/XResolution with Orientation 1–8), and nothing but `ImageRejected` or success may result.
- **W22-R:** per B-23, pin the two kept rules (the clump centroid at detect.py:526, `tan_min_value`) with non-skipping drawn-scene tests that fail without them, **or remove them** and re-report the real photos honestly. Correct the spike-report/DESIGN_POINTS claim.
- **Falsified evidence (test-evidence.md:560, :570):** the owners append explicit corrections. The lead annotates the FX2.4/FX2.5/FX2.6 Done notes.
- **B-24 amended:** Python's `sqlite3.connect` already defaults to `timeout=5.0`, so no busy_timeout task is needed. The 503 `busy` mapping in T3.2 stays.
- **Tiny uploads (note, not a fix-wave item):** T3.3 maps reference-detector `ValueError` on images under 64 px to 422 `image_too_small` (added to T3.3). No io change this wave, to keep cycle 3 narrow.

## B-28 Pre-existing zeroing test gap, queued (not cycle-3-admissible)
- review-16 found that `zeroing.py:169-170` averages the latest confirm group with `/ len(latest)`, and a mutant dividing by 10 survives every test. That would double the correction for a 20-shot confirm group. The code is correct today, and the gap predates fix wave 2, so it isn't admissible in Group 2 cycle 3.
- **Decision:** add lead task **T3.0** (test-only) at the start of Group 3: (a) the centroid divisor, (b) latest-not-best confirm judging, (c) `&nbsp;`/doubled-separator coverage for best-of/group-reduction in the D-14 scan, which matters because Group 3 adds HTML labels. All are below the cycle-3 bar (pre-existing) and are recorded by review-16.
- **B-28 addendum:** lead task **T3.0d** (devops) fixes the pre-existing EPERM flake at test_live_server.py:269 (about 1 in 7 under load, review-17). It isn't cycle-3-admissible.

## B-29 USER-AUTHORIZED exception: one extra fix + one extra review cycle for Group 2 (2026-10-01)
- **Context:** Group 2 cycle 3 (terminal) FAILED with 1 Warning, G2-W17-R2: FY2.3 removed the EXIF-parse guard, so malformed EXIF/XMP escapes `load_oriented` as a non-`ImageRejected` exception. Group 2 was blocked.
- **User decision:** "(a) Narrow fix + one review". This authorizes ONE scoped fix task (**FZ2.1**, io.py + test_image_io.py + a test-evidence correction) and ONE extra review cycle, **Group 2 cycle 4**, scoped to `src/rga/vision/io.py` and `tests/unit/test_image_io.py` only, plus a full-suite regression gate.
- **Scope of FZ2.1:** guard only the `getexif()` orientation read so any exception raises `ImageRejected` (review-18 validated this on scratch: 0 escapes in 6,000 fuzz cases); add a regression test per trigger (JPEG with JFIF density + non-TIFF/truncated EXIF header; PNG eXIf with a non-TIFF header; PNG "Raw profile type exif" with non-hex text; PNG text chunk "xmp"; plus a HEIF case); extend the fuzz to those container paths; keep W17-R's behavior (a malformed *unrelated* tag still loads oriented); append a correction to test-evidence.md:662.
- **If cycle 4 doesn't PASS:** Group 2 is blocked again and goes back to the user. No further cycles without new authorization.
- **Group 2, cycle 4 (user-authorized, B-29):** single synthesizer `review-19` spawned 2026-10-01, scoped to io.py + test_image_io.py + the full-suite gate. **Result: FAIL: 1 Critical (G2-C1, PRE-EXISTING), 0 Warning.** G2-W17-R2 is RESOLVED (0 escapes over 56,000 fuzz cases, no regression). G2-C1: EXIF/IFD parsing in Pillow allocates ≈ metadata bytes × IFD entry count from a single upload within the 40 MB/60 MP caps (measured 8 MB → 2.5 GB; 20 MB → >4.5 GB and climbing). The behavior is identical on the cycle-2 code. **Group 2 is BLOCKED again; back to the user.**

## B-30 USER DECISION: Group 2 unblocked with a tracked exception; G2-C1 fixed first in Group 3 (2026-10-01)
- **User chose option (b).** Group 2 is marked COMPLETE with one tracked exception, **G2-C1** (pre-existing EXIF/IFD memory amplification in Pillow parsing; see review.md "Group 2 — Cycle 4"). The B-29-funded fix (G2-W17-R2) is verified resolved.
- G2-C1 is fixed by lead task **T3.00**, the first Group 3 task: sniff the EXIF/XMP carriers in the raw bytes (summed JPEG APP1 Exif segments, PNG eXIf / "Raw profile type exif" / XMP text chunks, HEIF Exif) and raise `ImageRejected("EXIF metadata too large")` above **256 KB** *before* Pillow parses them. T1 tests assert a fast ImageRejected and a bounded `tracemalloc` peak (< 256 MB) for review-19's reproducer shapes (PNG eXIf, JPEG multi-APP1, raw profile, HEIF). Real photos still load. It's reviewed in Group 3's cycles.
- Recorded in `security-exceptions.md` until T3.00 lands and Group 3 reviews it.
- review-19's uncounted notes 1 (a malformed tag sorted before Orientation drops the rotation) and 4 (`struct.error` unpinned) are folded into T3.00 as test-only additions, since they're the same file.

## B-31 Truth export as a CLI (Group 3)
- The B-21 exporter is a CLI (`python -m rga.export_truth`) rather than a web route. That avoids changing the design §4.7 route table and the schemas registry, and the user still never hand-writes JSON. README instructions are added in Phase 4.
- **Group 3 wave 1:** coding-5 T3.00 (G2-C1), coding-2 T3.0, devops-1 T3.0d, coding-4 T3.1, coding-3 T3.2, coding-1 T3.3, coding-6 T3.4. Queue: T3.5, T3.6, T3.7, T3.8, T3.10, then T3.9 (smoke, needs T3.1–T3.4).

## B-32 STANDING USER INSTRUCTION: the lead decides technical details (2026-10-01)
- The user said: "stop asking me these technical details, assume your own expertise."
- **Effect:** the lead makes all technical and design-detail decisions autonomously and logs them here with reasons, reporting outcomes instead of asking. This includes the review-cycle budget: when a scope's terminal cycle fails, the lead may authorize ONE narrow, scoped extra fix + review (as in B-29/B-30), routing pre-existing issues to a later task when sensible. Each such exception is logged here as "lead-authorized under B-32".
- **Still goes to the user:** product or scope changes the user would care about (what the app does for them), anything costing money or leaving the Mac, destructive or irreversible actions, and a scope that stays blocked after one lead-authorized extra cycle.

## B-33 T3.2 schema and route ownership (lead-decided under B-32, 2026-10-01)
- **`weather_prompt`:** add `weather_prompt: bool = False` to `SessionOut` (inherited by `SessionDetailOut`) in schemas.py. True when `distance_yd >= 200`; the service computes it. coding-3 makes this one surgical edit, plus a test in `tests/unit/test_schemas.py` (no reformatting of either file). Reason: the T3.2 criterion is an API contract, so it belongs in the registry and the contract test, not only in the page.
- **`PATCH /api/groups/{id}`:** owned by **T3.2** (coding-3), with the full `GroupUpdate` semantics, implemented through `services/sessions.update_group` (recompute offsets in the same transaction when the purpose changes; link errors → 409 `GroupLinkMismatch`). T3.5 drops `PATCH` from its endpoint list and uses this route for notes. Reason: the session page's "Import photo" action needs it now, and T3.5 hasn't started yet.

## B-34 Web layer imports db exceptions only via services; PID-only process stops (lead-decided under B-32, 2026-10-01)
- **Layering:** `rga.web.*` must not import `rga.db.*`, not even exception classes or row types. A service that can raise a db exception re-exports it (e.g. `services/sessions.py` lists `GroupLinkMismatch`, `GroupNotLinkable`, `ZeroTargetMismatch` and `LoadNotInRifle` in `__all__`), and the routes import it from there. A service returns its rows to routes, and routes take the types from the service module. Reason: keeps the existing layering test green without loosening it. Applies to T3.2 (`routes/sessions.py`) and T3.3 (`routes/photos.py`) now, and to every later slice.
- **Incident:** about 02:40 UTC coding-4 ran `pkill -f rga`, which could have sent SIGTERM to other agents' servers or test runs. All workers were told to re-run anything that died unexplained around then. **Rule:** stop processes only by a PID you started yourself (`kill -TERM <pid>`); never use `pkill`/`killall` with a pattern.

## B-35 Review editor zoom/pan (lead-decided under B-32, 2026-10-01)
- coding-6 reported that the review editor shows the whole sheet, so holes are tiny. The user will mark real holes in this editor to build the exact-truth gate (B-21), so precise clicking matters. Added **T3.4z**: viewBox-based zoom and pan, "Zoom to region", and drag accuracy at any zoom, in a new module-scoped `review.css`. Assigned to coding-6, then T3.8 (dope).

## B-36 T3.2 judgement calls accepted (lead-decided under B-32, 2026-10-01)
- A FINAL group can't change `shots_expected` or `photo_id` (409 `group_final`); notes, purpose and elevation stay editable.
- A group's photo changes only while it has no shots and no aim (409 `group_has_shots`); changing the photo clears the stored ROI.
- The weather gate (enter or skip at 200 yd and beyond) is enforced on the page, not the API, because AC-12 asks for a prompt, not a hard block.
- The routes reach the link exceptions through the `service` module attribute (`service.LINK_ERRORS`) rather than literal `from` imports; that satisfies B-34.
- **T3.10** was started before T3.3 lands; it reads stored photo and group rows and coordinates with coding-1 on the photo storage layout.

## B-37 T3.7 open points (lead-decided under B-32, 2026-10-01)
- **Detach route:** `DELETE /api/zeros/{id}/groups/{gid}` → `ZeroOut` is accepted. It's added to design §4.7, and to ROUTE_SCHEMAS via T3.7a. T4.12's client contract must include it.
- **Step 1 limit:** Step 1 is 1–3 shots (D-04, Form). Linking a 4+ shot group as Step 1 → 422 `invalid_step1_shots`, so no Step 1 correction can come from a larger group (T3.7a). This also closes the "zero_step1 groups of 4–9 shots aren't masked" gap, because such groups can't exist.
- **No expected-state parameter:** accepted as is. It's a single-user local app, and GET resyncs the state.
- **Frozen dot on a complete zero:** accepted. The dot is the recommendation made at completion; editing groups afterwards doesn't rewrite history. Starting a new zero gives a fresh dot.
- **T4.12** (API↔JS client contract) must cover the sibling routes the zeroing page calls (`GET /api/sessions?rifle_id=`, `POST /api/sessions/{id}/groups`, `GET /api/loads/{id}/lots`, `GET /api/groups`).

## B-38 T3.3 reference lock; T3.8 reassigned (lead-decided under B-32, 2026-10-01)
- coding-1's addition is accepted: confirming a reference is refused with 409 `reference_locked` once any group on the photo holds shots, an aim or an ROI, because a new scale or transform would silently invalidate stored coordinates. To change the reference, the user clears those groups' shots first.
- T3.1–T3.4 have all landed, so devops-1 runs the full T3.9 smoke.
- T3.8 (dope) is reassigned from coding-6 to coding-1, who is free now; coding-6 keeps only T3.4z.

## B-39 Aim in masked group detail (lead-decided under B-32, 2026-10-01)
- coding-4 flagged that `GET /api/groups/{id}` returns `aim` next to the shot positions for masked groups, so a client could compute the masked offset. The read API now returns `aim: null` for masked groups (**T3.5a**). The review editor keeps the aim in its page state, because the user places it there and it's the editing surface, not a results display. B-14's "positions stay for drawing" is unchanged.

## B-40 Group 3 T3 gate met; config-surface test added (lead-decided under B-32, 2026-10-02)
- T3.9 smoke: 36 PASS / 0 FAIL / 0 SKIPPED, re-run by the lead. Group 3's T3 gate is met. Its deviations from test-plan §4 wording are accepted: the LAN check tries all non-loopback IPv4 addresses, a PATCH attach step was added, and integrity_check opens the db read-write.
- Added **T3.9a**: a two-way config-surface test for the scripts (testing-standards T2 "config surface", and review item S18), assigned to devops-1.

## B-41 Oversize Step 1 groups are masked (lead-decided under B-32, 2026-10-02)
- After T3.7a, a Step 1 group can still grow past 3 shots after linking. The zeroing advice and `dialed` already refuse it, but the group endpoints would still show a mean POI. `masks_poi` now also masks `zero_step1` with more than 3 shots (**T3.7b**, coding-2), so no 4–9 shot group can show a correction anywhere outside the 1–3 shot Step 1 the doctrine allows. Updates B-09/B-18. Dope already excludes all zero_step1 groups (B-15).

## B-42 T3.6 comparison interpretations accepted (lead-decided under B-32, 2026-10-02)
- Masked groups are omitted from comparison entirely (tiers, pairs, POI pairs and the group list), through the single `masks_poi` rule. With B-41 this also covers oversize Step 1 groups automatically.
- Groups under 10 shots are listed with `tier: null` and never compared or paired. 20–29 shot groups are untiered (like-with-like tiers are 10–19 and ≥30) but count as 10+ shot data for POI pairs.
- POI shift = the mean over shared sessions of (load's pooled-shot centroid − zero load's), reported as direction words without advice. The zero load is used for pairing even when it isn't selected. With no shared session or no zero load, it's null plus a note.
- The conditions warning considers only tiered groups.

## B-43 One masking rule, everywhere (lead-decided under B-32, 2026-10-02)
- After T3.7b, two copies of the masking rule were stale: `domain/summaries._poi_note` (a 5-shot Step 1 summary still printed a mean POI; no live caller yet) and the client-side `isMasked` in `analysis.js` (keyed on `shots_expected`, no Step 1 case). Separately, `GroupOut` still carried `aim` for masked groups in the session-detail and group-list payloads, so B-39 was incomplete: list aim + detail positions = offset.
- **T3.11** (coding-2; the file owners are idle, so no clobber risk): the canonical rule moves to `domain.zeroing.masks_poi` (services re-exports it); summaries use it; `GroupOut.masked` is server-computed and `aim` is null when masked in every group payload; the client reads `group.masked` instead of re-deriving the rule.
- Group 3 review starts when T3.11 lands.

## B-44 Lead decisions on Group 3 cycle 1 (FAIL: 2 C / 9 W) (lead-decided under B-32, 2026-10-02)
Fix-wave scope: the 2 Criticals and 9 Warnings only. The 33 Suggestions are deferred (recorded in review.md).
- **G3-C1 (SE-2 still open):** the byte cap can't bound bytes × IFD entries. New rule: **Pillow never parses EXIF from an upload.** Orientation comes only from our own bounded IFD0 reader (`_ifd0_orientation`, with an entry-count cap). EXIF/XMP carriers are stripped from the raw bytes (JPEG APP1, PNG eXIf and metadata text chunks) before `Image.open`. No `getexif()`/`_getexif` call is reachable for uploads, including HEIF, where the raw `info["exif"]` goes to our reader. The JPEG marker walk rejects a segment length < 2 as malformed (ImageRejected) instead of stopping. The byte cap stays as defense in depth. Tests use tracemalloc bounds on 4,000- and 10,000-entry carriers under the cap, plus the length-0/1 JPEG, for every container. SE-2 stays open until a cycle verifies this. → FG3.1 (coding-5, also W3).
- **G3-W3:** decode errors (e.g. an empty iCCP after IDAT → IndexError) must become ImageRejected. `load_oriented` maps any exception from Pillow decode to ImageRejected 415, so uploads can't produce a 500. → FG3.1.
- **G3-C2 (zeroing distance):** the 100 yd rule is enforced wherever a zero group is judged, dialed, dotted or checked, not just at link time; a non-100 yd group judges as INVALID with a fixed message and no numbers. A session's distance can't be changed while it holds groups linked to a zero (409 `session_has_zero_groups`). → FG3.2 (coding-2, also W4).
- **G3-W4 (narrows B-33):** zero purposes (`zero_step1/zero_step2/zero_confirm/zero_check`) and `zero_id` are set ONLY by the zero routes (link, checks, detach). Group create/PATCH may set `purpose` only to `general`, may not set `zero_id` (422 `invalid_purpose`), and can't change the purpose of a zero-linked group (409 `group_linked_to_zero`; detach first). → FG3.2.
- **G3-W1 (ids ≥ 2^63 → 500):** fixed centrally. `PositiveId` in schemas gets `le=2**63-1` (bodies → 422), and an app-level handler maps the SQLite integer overflow (OverflowError) to 404 `not_found` for path ids. A registry-driven sweep test hits every `{id}` route with 2^63 and 2^64 and requires a 4xx. → FG3.3 (coding-4). The truth-export CLI bounds `--photo-id` (exit 2). → FG3.7.
- **G3-W2 (upload with no body → 500):** → FG3.5 (coding-1).
- **G3-W6 (reference lock has no escape):** B-38 is narrowed. The lock applies only while a group on the photo has shots or an aim. A stored ROI doesn't lock; re-confirming clears the ROIs of that photo's groups in the same transaction, because they're in old working px. The page text matches. → FG3.5.
- **G3-W5 + G3-W7 (AC-10 semantics):**
  - Unmoved estimated shots are app placeholders, not detections. They can be replaced or dropped without a rejection record, and Fill shortfall replaces only unmoved estimated shots.
  - Detected, manual and user-moved shots still need `not_a_bullet_hole` to be dropped.
  - Rejections are recorded per shot, not de-duplicated by coordinate.
  - When the count exceeds the expected count, Finalize is disabled with a reason ("remove N with Not a bullet hole, or raise the expected count"), never a dead end.
  - → FG3.6 (coding-6).
- **B-39 amended:** the `PUT review` response keeps the aim for masked groups (the editor adopts it). The `POST finalize` response nulls it for masked groups, so no other group payload carries a masked aim. → FG3.6.
- **G3-W8 (vacuous dope page assertion):** test-only fix. → FG3.7 (coding-3). The T3.10 "read-only db" claim is made true: open with `mode=ro`. → FG3.7.
- **G3-W9 (tier30 unreachable):** P-13 / Q-4 (the user's answer) promise comparison on 30-shot aggregates, and the user chose to pick the groups to combine. Implement **saved aggregates**:
  - migration 002 adds `aggregates` + `aggregate_groups`;
  - routes `POST /api/aggregates` {name, group_ids}, `GET /api/aggregates?load_id=` and `DELETE /api/aggregates/{id}`, using the same validation as `POST /api/aggregate` plus a single load;
  - a "Save this combination" button on /analysis;
  - compare's tier30 includes saved single-load aggregates with ≥ 30 shots, summarized by their pooled-overlay ES; masked groups can never be saved (B-20).
  - This fulfils an existing requirement rather than changing scope. design §4.7 is updated by the lead. → FG3.4 (coding-4, after FG3.3).
- Corrections: the T3.00 Done note ("SE-2 closes when Group 3 review verifies it") is superseded, and SE-2 stays open. The T3.10 Done note's "read-only db" is corrected by FG3.7.
- **Group 3, cycle 2** follows the fix wave.

## B-45 User-reported reference detection defects on real_02 (lead-decided under B-32, 2026-10-02)
- The user tried the app on a GunFun target (= `data/real_photos/real_02.jpg`): a 1.5" orange paster with a printed inner black ring (about 0.75") and a black diamond, on a red 1" grid that is interrupted by a thick vertical red band and cut off at the sheet edges.
- **Lead probe confirms both reports.** (1) `find_circle_candidates` returns ONE candidate, with full axes about 344 px = the inner ring, for every circle kind (1", 1.5", 3"). The outer orange disc (about 700 px) isn't proposed, so a confirmed "1.5 in circle" would make the scale 2× wrong. (2) `find_grid` returns None.
- **Decisions:**
  - **U1 circle (coding-2):** for paster/sticker references the reference is the OUTER boundary of the uniformly colored disc. Printed rings and marks inside it are not candidates for that disc's kind. Propose both the outer disc and the inner rings as separate ranked candidates where present, ranked outer-first for circle kinds. When a grid is also found, use it to cross-check: prefer the candidate whose diameter matches the kind in grid inches, and say so in the candidate label.
  - **U2 grid (coding-4):** detect a 1" lattice from PARTIAL evidence: thin line segments interrupted by thick bands and cut off at the edges. Line-width filtering excludes thick bands. Accept ≥ 2 parallel thin lines in each direction with consistent spacing, and line colors red, black, green or blue.
  - Acceptance on real_02 (a local, gitignored real-photo test that skips if the file is absent): circle_1_5in candidate #1 is the outer disc (diameter ≈ 1.5 × grid spacing ± 5%), and find_grid returns a lattice with spacing within 3% of the true 1". The synthetic P-11 gate and all other real photos must not regress.
- **Timing:** workers prototype on scratch copies now; repo edits wait until the Group 3 cycle-2 verdict lands. They then join that cycle's fix wave and are reviewed in cycle 3.

## B-46 USER REQUEST: group results page overlays (2026-10-02)
- The user asked for checkbox toggles on `/groups/{id}`: show/hide the actual photo under the plot, show/hide an extreme-spread circle "circling the group like Form describes", and show/hide the group's mean POI in a different color from the POA (aim).
- **Lead technical decisions (B-32):**
  - **Photo:** the rectified working image is drawn under the shots in the same plane-inch frame (served by the existing `GET /api/photos/{id}/image?variant=working`) and cropped to the group's ROI plus a margin. On by default when the group has a photo.
  - **Spread circle:** the smallest circle that contains every shot center (minimum enclosing circle), with its diameter shown in the legend next to ES. ES stays the headline number. For most groups the two are nearly equal; the legend names the circle so the numbers can't be confused. On by default.
  - **Mean POI:** a distinct marker and color from the aim marker, explained in the legend. Off by default.
  - **Masked groups (B-09/B-14/B-18/B-39/B-43):** there's no mean-POI toggle and no aim mark. The photo and spread-circle toggles remain, because the photo is the user's own target and the circle is size-only. No numbers beyond n/ES appear.
  - The toggles are client-side only (CSS classes on SVG groups, no inline style), and the state isn't persisted.
- Assigned to coding-6 as **UF1** in the Group 3 cycle-2 fix wave, reviewed in cycle 3. It prototypes now; repo edits wait for the cycle-2 verdict.

## B-47 USER REQUEST: "Correction" toggle on the group page (2026-10-02)
- The user asked for a show/hide "correction" toggle on `/groups/{id}`. When on: impacts drop to 25% opacity, shot numbers hide, POA and mean POI come to the front, and dotted lines join them (one vertical, one horizontal). The text is the opposite of the mean POI, e.g. "adjust 0.3 mil down, 0.1 mil left". An "i" icon opens a card explaining which way to turn the turrets.
- **Lead decisions (B-32), applying the user's own adoption of Form's doctrine (D-04, B-04):**
  - The correction is offered only where Form allows one: groups of **10+ shots** (unmasked), or a **Step 1 group of 1–3 shots**.
  - For any other group (2–9 shot general groups, masked groups) the toggle is present but disabled, with a fixed explanation ("Form: don't correct off fewer than 10 shots; shoot a 10-shot group"). No numbers or direction appear anywhere in the HTML/JS/data. This keeps the FAIL trigger "no numeric correction from a 2–9 shot group outside Step 1".
  - Values come from the same domain correction function the zeroing flow uses (0.1 mil rounding, mil/mil scope), so the group page and the zeroing page always agree; it's computed server-side and embedded only when allowed.
  - At distances other than 100 yd the text names the distance ("to center at 300 yd: adjust …"). It's the hold/dial for that distance, not a zero change; the info card says so.
  - **Info card:** fixed text explaining that you turn the elevation turret in the direction marked UP to move impacts up (and the windage turret marked R/L to move them right/left). It says to dial the stated amount in that direction, that one click is 0.1 mil on a mil/mil scope, and to check your turret's markings. It's a CSP-clean popover with no inline script/style.
- Folded into **UF1** (coding-6).

## B-48 Rate-limit interruption (2026-10-02)
- The account session limit stopped review-24, review-27, coding-2, coding-4 and coding-6 mid-work. They're resumed via SendMessage with their context intact. This does NOT consume a new review cycle: Group 3 cycle 2 continues with the same synthesizer (B-07).

## B-49 USER REQUEST: auto-detect the point of aim (2026-10-02)
- The user asked for POA auto-detection. A circle paster should be found "pretty exact"; the aim can also be a diamond, a cross, an X, or a grid intersection. Less obvious points (e.g. the bottom tip of a diamond) stay manual, and the manual pick stays available.
- **Lead decisions (B-32):**
  - **New vision module `vision/aim.py`:** `propose_aims(working_img, roi, reference)` returns up to 5 ranked candidates `{kind: paster_center | dot_center | diamond_center | cross_center | grid_intersection, point_px, score}`, searched around the group's ROI. Each kind is located as follows:
    - **paster/dot:** sub-pixel ellipse center, reusing reference_circle fitting. If the confirmed reference IS a circle paster near the group, its center is the top candidate.
    - **diamond:** the centroid of a dark convex quadrilateral with near-equal diagonals.
    - **cross/X:** the intersection of two thin crossing line segments, including tape crosses (real_05).
    - **grid intersection:** the lattice intersections nearest the group's center, only when no stronger mark is found.
  - **Nothing is auto-confirmed** (same principle as the reference). The editor shows the candidates as rings with labels and pre-selects the top one with "Use this aim / Next / place it yourself". The aim is set only when the user accepts or places it. Finalize still requires an aim, and manual click plus snap-aim stays unchanged.
  - The candidates are returned in `DetectOut.aim_candidates` (a surgical schema addition) so no extra request is needed. They're working-px points, like the shots.
  - Multi-aim sheets (P-08, real_05): candidates come only from inside or near the group's ROI.
- **Acceptance:** synthetic aim marks (dots, diamonds, crosses, grid intersections) recover the true aim within 0.03 in for paster/dot/diamond/cross and within 0.05 in for grid intersections. On real photos (local, skip if absent): real_02 gives the paster/diamond center as #1; real_05's tape cross is in the top 3 for its group.
- Assigned to coding-1 as **UF2** in the cycle-2 fix wave (reviewed in cycle 3). It prototypes on scratch now; repo edits wait for "go".

## B-50 Lead decisions on Group 3 cycle 2 (FAIL: 1 C / 5 W) + user items into the same wave (lead-decided under B-32, 2026-10-02)
- **G3c2-C1 + W1 (io.py; SE-2 stays open):** stripping is replaced by **whitelist rebuilding**.
  - **JPEG:** the file is re-emitted with only SOI, DQT, DHT, DRI, SOFn (the baseline/progressive ones Pillow decodes), SOS + entropy data, EOI, APP0 JFIF, APP14 Adobe, and APP2 ICC_PROFILE (size-capped).
    - Every other segment is dropped: APP1 Exif/XMP, APP2 MPF, COM and other APPn.
    - Any marker whose framing is ambiguous is ImageRejected: 0xC8, 0xF0–0xFD, length < 2, or a stray marker before SOS.
    - Orientation is read by our bounded reader from the dropped Exif before it's discarded.
  - **PNG:** only IHDR, PLTE, IDAT, IEND, tRNS, gAMA, cHRM, sRGB, pHYs, sBIT and a size-capped iCCP are kept; every other chunk is dropped.
  - **Generic guard:** a spy on Pillow's IFD loading (`ImageFileDirectory_v2.load` and friends, any tag type) must never fire for any upload, checked by a marker-sequence fuzz. → **FG3b.1** (coding-5).
- **G3c2-W2:** a new zero route, `POST /api/zeros/{id}/check-groups` {session_id, lot_code, shots_expected, notes}, creates the check group already linked as `zero_check`. It is masked from creation, the 100 yd session is required, and it returns 201 GroupOut. The zeroing page uses it, and `POST /api/zeros/{id}/checks` then runs the check. The B-44 rule is kept: zero purposes are set only by zero routes. design §4.7 is updated. → **FG3b.2** (coding-2).
- **G3c2-W3:** Fill refills placeholders up to the expected count after any nudge; a sibling placeholder is never silently lost. → **FG3b.3** (coding-1, who is already in review.js for UF2).
- **G3c2-W4:**
  - Each group counts at most once per load in tier30. Saved aggregates are counted greedily in creation order.
  - An aggregate that shares any group with an already-counted one is listed but not counted, with the note "overlaps another saved combination".
  - Saving an exact duplicate set → 409 `duplicate_aggregate`.
  - **G3c2-W5:** an aggregate's distance is derived live from its member sessions (mismatched distances → invalid), never stored-and-trusted.
  - → **FG3b.4** (coding-3). It edits analysis.py only after coding-6 reports UF1 applied, re-reading the file first.
- **User items in this wave** (B-45/B-46/B-47/B-49), all reviewed in cycle 3:
  - **U1** circle outer-disc + pipeline grid wiring (coding-2, staged);
  - **U2** partial-grid fallback in the new `reference_grid_lines.py` (coding-4, staged);
  - **UF1** group page toggles + correction (coding-6, staged);
  - **UF2** POA auto-detect (coding-1, prototyping).
- **Ordering in shared files:**
  - `analysis.py`: coding-6 (UF1) first, then coding-3 (FG3b.4).
  - `review.js`: coding-1 alone.
  - `zeroing.py`/`routes/zeroing.py`: coding-2 alone.
  - `schemas.py`: surgical edits by coding-2 (check-groups registry entry) and coding-1 (DetectOut.aim_candidates). Re-read the file right before editing.
  - `reference_circle.py`: coding-2 only; coding-1 imports helpers and doesn't edit it.
- **Group 3 cycle 3 is TERMINAL.** If it fails, the lead may authorize one extra narrow cycle under B-32; beyond that it goes to the user.

## B-51 USER REQUEST: Offline/Online mode, placeholder only (2026-10-02)
- **The user's plan for the future:** "Offline" mode is everything built so far. A later "Online" mode will add an API call to "mini 4.0" (the user's wording; the exact model/provider gets confirmed when that work starts) for smarter, more accurate image analysis.
- **Now:** add ONLY a visible mode toggle in the site header, showing **Offline** as the current mode and **Online** greyed out and unselectable, with a tooltip/caption "Online mode — coming later". It does nothing: no request, no setting stored, no JS behavior, no new route, no config variable. The no-network/no-CDN gates stay unchanged and must still pass.
- **When Online mode is built later,** it falls under the standing escalation rule: it sends photos off the Mac and costs money, so its design (which provider/model, what data is sent, opt-in per photo, key storage) goes to the user first. Recorded as future scope; the current requirements' "no cloud API" applies until then.
- Assigned to devops-1 as **UF3** (base.html + app.css + test_templates_shell.py), reviewed in Group 3 cycle 3.

## B-52 R27-S1 label wording (lead-decided under B-32, 2026-10-02)
- `/analysis` labels every 30+ shot saved aggregate "counts in the 30-shot comparison", but `/compare` may decline it (overlap, mixed conditions, a 30+ member). Only /compare knows the real status, so the /analysis label changes to "30+ shots: eligible for the 30-shot comparison (Compare shows whether it's counted)". It's a JS text change only (coding-3); no schema field.

## B-53 Id sweep gap + flaky leak test (lead-decided under B-32, 2026-10-02)
- The registry coverage self-check in test_id_bounds correctly failed for the new `check-groups` route; the sweep is extended (with a valid body so the path id is what's tested). The masked-aim leak test's raw substring check ("7.5") collides with timestamp microseconds about 1% of the time; it moves to field-level assertions and collision-proof seeded values. → FG3b.5 (coding-4).

## B-54 Private photo copies in /tmp removed; handling rule (lead, 2026-10-02)
- review-30 found world-readable (0644) copies and derivatives of the user's real photos left in /tmp by earlier workers and reviewers. The lead deleted them all: /tmp/u1_view.jpg; /tmp/u1_scratch, u2_scratch, coding6_uf1, t219 and spike; /tmp/real_vis_1..5.jpg; /tmp/sheet1/2.jpg; /tmp/rv7_scratch and rv7pta_scratch (full copies of real_01..05); and a pytest temp copy. A re-scan finds 0 remaining. The originals in data/real_photos (0700) are untouched, and nothing left the Mac.
- **Rule from now on:** workers and reviewers never write copies, crops, masks or overlays of real photos to disk. If a visual check is essential, write it inside a `mktemp -d` directory with mode 0700 and delete it before reporting. Scratch copies of the repo must not include data/.

## B-55 LEAD-AUTHORIZED under B-32: one extra narrow fix + review for Group 3 (cycle 4) (2026-10-02)
- Group 3 cycle 3 (terminal) FAILED on 1 Critical + 6 Warnings, all new (the cycle-2 findings were closed). Under B-32 the lead authorizes ONE narrow fix wave (FG3c) and ONE extra review cycle, **Group 3 cycle 4**, scoped strictly to the files below plus a full-suite and smoke regression gate. **If cycle 4 does not PASS, Group 3 goes to the user, with no further cycles.**
- **Decisions:**
  - **G3c3-C1 + W5 (io.py):** cap the whitelist rebuild.
    - JPEG: at most 64 kept non-scan segments, and at most 1 MB of kept header bytes outside SOS (ICC within its own 1 MB cap); beyond either → ImageRejected("too many image segments").
    - PNG: at most 64 kept non-IDAT chunks. The IDAT count is bounded by the file size, but the rebuild must stream or concatenate without one object per chunk beyond a fixed working set; peak memory O(file size).
    - Tests: a tracemalloc/RSS bound on the 40 MB empty-DHT JPEG and a many-chunk PNG (< 64 MB over the input size, < 2 s, rejected or loaded).
    - W5: a test proves the PNG load path uses the whitelist; it fails if the rebuild is bypassed, and the PNG fuzz spy must be able to fire on an unrebuilt control. → **FG3c.1** (coding-5).
  - **G3c3-W1:** aim proposals are best-effort. Any exception inside the aim search (cv2.error, ValueError) gives `aim_candidates: []`, logged, never a 500. Vision entry points convert cv2.error on degenerate images to ValueError. → **FG3c.2** (coding-1).
  - **G3c3-W2:** an aim candidate within one bullet diameter of any detected or proposed shot is discarded. Diamond candidates need a diagonal ≥ 2× the bullet diameter. The top candidate is pre-selected only if its score is ≥ 0.6; otherwise none is pre-selected and the list is just shown. → **FG3c.2**.
  - **G3c3-W4:** the Fill merge uses an optimal one-to-one assignment (minimum total distance; n is small, so brute force or Hungarian is fine) rather than greedy nearest pairs. A test covers the same-direction double drag. → **FG3c.2**.
  - **G3c3-W3 (narrows B-45):** the line-lattice fallback needs ≥ 3 lines in at least one family; the 2×2 exception is removed. A grid from the fallback always carries the warning "Grid found from partial lines — check that one square is 1 inch", shown on the reference page before confirming. real_02 (3 horizontals) must still pass. Test the hidden-middle-line 2× case → rejected or warned, plus "#" and the lone square → rejected. → **FG3c.3** (coding-4).
  - **G3c3-W6:** the check-groups 2^63 path id returns 422, because body validation runs first. That's an acceptable 4xx; the test asserts the exact code, and the FG3b.5 note is corrected. → **FG3c.3** (coding-4, test_id_bounds.py).
- Cycle-3 Suggestions are deferred (in review.md), including R31-W1 (autocomplete) and R29-S1 (aggregate save-order dependence), for a later polish pass.
- SE-2 stays Open until cycle 4 verifies C1.

## B-56 FG3c.2 deviations accepted (lead-decided under B-32, 2026-10-02)
- **Shot-proximity discard applies only to hole-sized marks** (under 3 bullet diameters across, thin crosses, grid crossings). A real paster or dot with a hit near its center stays (multi_dot_target), and the confirmed-reference center is exempt. This still removes the G3c3-W2 failure (no candidate on a hole on the red-band scenes or the real photos).
- **Diamond floor is 1.4× the bullet diameter, not 2×:** real_02's printed diamond is about 1.45× (0.32 in diagonal), and 2× would leave it with no candidate. Holes are handled by the proximity rule.

## B-57 Group 3 blocked after the lead-authorized cycle 4; SE-2 resolved; SE-3 opened (2026-10-02)
- Cycle 4 FAILED on one Warning, **G3c4-W1**: the aim auto-detect (UF2) can pre-select a "diamond" that is actually a tight 10-shot cluster. That happened in 18 of 120 generated groups, 0.29–0.58 in off. Nothing is auto-confirmed, but accepting it would give a wrong correction. B-55 says no further cycles without the user, so **the decision goes to the user**.
- **SE-2 → Resolved:** review-32 verified the IFD vector is closed and the rebuild is bounded (all 40 MB floods rejected in ≤ 0.21 s at ≤ 0.4 MB).
- **SE-3 opened (pre-existing, not caused by FG3c):** a progressive JPEG with thousands of tiny extra scans pins a CPU during decode (a 162 KB file took 36.8 s under load), and raw Pillow behaves the same. The lead's decision: track it as SE-3 and fix it in Group 4 with a scan-count cap in io.py (count FF DA markers after the first SOS and reject above a small cap).
- **Still outstanding:** the idle-machine timing check for detect + aims against the ≤ 3 s target.

## B-58 USER DECISION: aim pre-select turned off; Group 3 closed with a tracked exception; Group 4 opens (2026-10-02)
- The user picked "One more fix + review" by mistake and corrected it in chat: **"I meant to hit turn off pre-select."**
- **Decision:**
  - Aim suggestions stay as numbered rings, and **none is pre-selected**; the user always clicks the one they want.
  - There's no extra Group 3 review. The change (**T4.0a**) is verified in Group 4's review and the end-to-end review.
  - Group 3 is marked COMPLETE with one tracked exception: G3c4-W1, closed by T4.0a.
- Lead-added Group 4 items:
  - **T4.0a:** no pre-select.
  - **T4.0b:** the SE-3 progressive-scan cap.
  - **T4.0c:** idle-machine timing of detect + aims against the ≤ 3 s target, run late in Group 4 while the pool is idle.
- T4.12's contract scope adds: the DELETE detach, check-groups and aggregates routes; GroupOut.masked; DetectOut.aim_candidates.
- E2E tests that count radios scope them to `main` (the header switch).
- **Group 4 wave 1:** coding-1 T4.0a, coding-5 T4.0b, coding-6 T4.1 (harness), coding-3 T4.12, devops-1 T4.13 (first pass). After T4.1 lands, T4.2–T4.11 go out across the pool.

## B-59 Gap: no way to start or find a zero from the UI (lead-decided under B-32, 2026-10-02)
- coding-6 (T4.1) found that `POST /api/zeros` has no UI caller. The zeroing page is reachable only by URL, so the user can't start Form's zeroing flow from the app. That's a missing piece of FR-6, not a scope change.
- **T4.0d (coding-4):** the rifle detail page gets a "Zeroing" section:
  - **"Start a zero" form:** load (the rifle's loads, defaulting to the zero load), optional lot code, and mode new setup / existing. It posts `POST /api/zeros` via apiSend, then goes to `/zeroing/{id}`.
  - **The rifle's zeros:** listed newest first with state (step1/step2/confirm/dot/complete/superseded), load, lot, date and a link. The list is server-rendered from a new read helper `list_zeros_for_rifle` (repo_zeroing + services/zeroing).
  - **Dashboard:** shows the active (complete, not superseded) zero per rifle.
- No new API route (the server-rendered list avoids a registry change). Same CSP/api.js rules apply. Tests: integration (form → zero created → redirect, list shows states, masking unaffected), plus an e2e-marked Chromium check.

## B-60 USER REQUESTS: Settings tab; plot visuals (2026-10-02)
- **Settings tab:** the user said "add things just as I tell them to you, for now it's just what I've mentioned."
  - **Lead decision:** a new nav tab **Settings** (`/settings`) whose only content is the Offline/Online mode switch (Offline current, Online greyed out, "Online mode — coming later").
  - The switch moves there from the header, so it appears once.
  - No stored settings, no routes beyond the page, and no JS behavior.
  - Future settings are added only when the user names them.
  - → **T4.0f** (devops-1).
- **Visuals:**
  - (a) Every shot marker in a single group uses the same color. Estimated shots keep the same color with a dashed outline.
  - (b) On overlays (the /analysis aggregate and any multi-group plot), each group gets its own distinct color from a high-contrast, colorblind-safe palette, with a legend mapping color → group.
  - (c) The mean-POI marker becomes visually light: a thin open marker (e.g. a small open ring with a short crosshair, or an open "x"), in a color distinct from the aim marker, no filled shape.
  - (d) Generally: more color contrast and thinner lines (non-scaling strokes around 1–1.25 px for markers and circles). Precision over heft, with less crowding.
  - Applies to the group page, the analysis overlay and the review editor markers. Correction-view rules (fade to 25%, hide numbers) stay.
  - → **T4.0g** (coding-6, may use the frontend-design skill).
- Both are reviewed in the Group 4 review.

## B-61 USER REQUEST: "Thing hitter" circle on the group page (2026-10-02)
- The user asked for a show/hide toggle for Form's "thing hitter" circle, as opposed to the "group shooter" spread circle. The user chose the center: **the point of aim (POA)**. The radius runs from the POA to the center of the farthest shot. The circle's size is shown in MOA and mil.
- **Lead decisions:**
  - The label gives the circle's diameter in inches, MOA and mil at the session distance (the radius follows from it).
  - Off by default.
  - It's only shown on unmasked groups with an aim. Its size encodes the zero offset, so masked (short zeroing) groups get a disabled toggle with a fixed reason and emit no geometry or number (B-09/B-18/B-43).
  - It gets its own distinct thin color (B-60).
  - → **T4.0i** (coding-6, after T4.0g; same files).

## B-62 USER REQUEST: zoom on the group results photo (2026-10-02)
- The user asked for zoom on the group results plot: +/− in the corner, and regular scroll-to-zoom while the mouse hovers over it.
- **Lead decisions:** use the same viewBox approach as the review editor (B-35), plus drag-to-pan and Fit, clamped 1–16×. Scroll only zooms while the pointer is over the plot, so page scrolling elsewhere is unaffected. The zoom code is extracted into a shared `svg_zoom.js` so the editor and the group page don't duplicate it.
- → **T4.0j** (coding-6, after T4.0g and T4.0i; same files).

## B-63 USER REQUEST: mil equivalent in the ES box (2026-10-02)
- Add a small, low-visual-weight mil equivalent to the extreme-spread box on the group page, computed at the session distance and rounded to 0.01 mil. ES is size-only, so it also shows on masked groups. → **T4.0k** (coding-6, folded into T4.0g's pass on group.html).

## B-64 USER DECISION: handload fields are bullet, primer, brass, COAL only (supersedes D-11's handload fields) (2026-10-02)
- **The user said:** "bullet (grain weight and line, ie 116 tmk), primer, brass, coal should be the only variables available to input for handloads. everything else can go in notes."
- **Lead implementation decisions:**
  - **Handload input fields (factory-load fields unchanged):**
    - **Bullet:** two inputs shown together as one "Bullet" field, grain weight (gr, number) + bullet line (text, e.g. "TMK"). Displayed as "116 gr TMK".
    - **Primer:** text.
    - **Brass:** text.
    - **COAL:** inches, 3 decimals, 1.000–5.000.
    - Optional trued MV and notes stay as they are.
  - **Powder and charge are removed** from the handload form and API. Migration 003 adds `bullet_weight_gr`, `brass` and `coal_in`. It moves any existing `powder`/`charge_gr` values into the load's notes as a "Powder: …, charge: … gr" line, so no data is lost, and splits an existing `bullet` text into weight + line where it parses (otherwise it keeps it as the line). The old columns stay unused rather than being dropped, so the migration is reversible-safe.
  - D-11's explicit rejection of COAL is lifted; CBTO, SD, ES and seating depth are still not fields (notes only).
  - → **T4.0l** (coding-4). Reviewed in the Group 4 review.

## B-65 USER REQUESTS: named suppressors; group share card → PNG (2026-10-02)
- **Suppressor:** next to the suppressor checkbox, a dropdown of the user's suppressors. The user types new names and they're remembered.
  - **Lead decisions:**
    - A new `suppressors` table (id, name, unique case-insensitive, created_at) plus a nullable `sessions.suppressor_id` (migration 004; must apply after coding-4's 003).
    - The dropdown is shown only when the checkbox is checked. It lists saved suppressors plus "Add new…" (a text input that creates one).
    - Unchecking clears the suppressor_id. A suppressor_id with suppressor = false is rejected (422).
    - Routes: `GET /api/suppressors`, `POST /api/suppressors` {name}. Registry and design §4.7 are updated.
    - The boolean `suppressor` stays the comparison filter. The name shows on session lists, the group page and the share card.
  - → **T4.0n** (coding-3; the rifle-page session form is edited after coding-4's T4.0l lands, coordinated directly).
- **Share card:** a "Share" button on the group page opens a card. In it you pick which layers to include (the same show/hide set: photo, spread circle, thing hitter circle, mean POI, correction where allowed). The image shows the rifle, scope, suppressor, load, then the group's normal specs (date, distance, n, ES in/MOA/mil, estimated badge; mean POI and correction only where the page itself would show them).
  - **Output:** **PNG**, the most universally compatible lossless image format.
  - **Lead decisions:**
    - Rendered **server-side** with Pillow (built-in FreeType default font, no external fonts) by `GET /api/groups/{id}/share.png?layers=…`. This needs no CSP change, can be tested deterministically, and never contacts the network.
    - Masking rules apply exactly as on the page: no aim, POI, correction or thing-hitter circle for masked groups, and requested layers that aren't allowed are silently omitted and listed as "not shown".
    - The card's "Download PNG" is a same-origin link with the `download` attribute.
  - → **T4.0o** (coding-5: renderer + route + registry + tests) and **T4.0p** (coding-6: Share button + card UI on the group page, after T4.0j).

## B-66 USER DECISION: no sessions; every group carries its own context (2026-10-02)
- **The user said:** "I don't think a session is needed. each group should have its own date / time weather. date and time should pull from input photo meta data if it exists, and manual entry if it doesn't. each group might be shot from a different position during the same session as well."
- **Follow-up answers:**
  - **POI-shift pairing** = same rifle, **same calendar day and same distance**.
  - **Per-group fields:** **distance, position + suppressor (and which suppressor), weather (asked at ≥ 200 yd), date/time** (from photo metadata when present, otherwise manual). Each is pre-filled from the user's previous group.
- **Supersedes:** the session concept in requirements, spec and design (P-06 same-session pairs; AC-12 weather per session; FR-2 sessions). The detailed design delta and task split follow in B-67.
- **In-flight:** T4.0n (session-level suppressors) is on hold for re-scope. Its suppressors table, routes and picker are reused at group level.
- **Process note:** coding-4 reformatted other agents' test files with a directory-wide `ruff format`. The rule stays: format only your own files.

## B-68 Detector handoff to a separate session (2026-10-02)
- At the user's request, the lead wrote `detector-handoff.md`, the contract for a separate Claude Code session that will train or integrate a better hole detector.
- It binds that session to: the `detect_holes` / `HoleDetector` interface (working px, `Blob` output, ValueError-only errors, deterministic, ≤ 3 s), the offline default with no network unless Online is approved by the user, no copies of the private photos, no edits to this repo's src/ or tests/ (work in a sibling lab folder), and specific evidence formats (eval_detection and eval_real_photos JSON, timing).
- Integration stays with the build lead, through a detector registry with cv-v1 as default and fallback.

## B-67 Redesign plan adopted for B-66; the lead's decisions (2026-10-02)
- `redesign-b66.md` (from a read-only architect plan) is binding. Lead decisions on its open risks:
  1. **POI pairing (USER, asked):** bucket by **same day + distance + position + suppressor state**.
  2. A re-zero in the middle of a day is accepted and documented; legacy groups have no times.
  3. **Group defaults:** load and lot come from the rifle's latest group; distance, position and suppressor fall back to the global latest group; weather is pre-filled only for the same shot_date.
  4. **The `sessions` table is kept as a dead legacy table** (the user's database has a group-less session). `SessionRow` stays for the mirror test. Dropping it is a later, user-approved migration.
  5. The new-lot flag orders by shot date/time, with `finalized_at` as the tie-break.
  6. **Backup:** the runner writes `VACUUM INTO <db>.pre-NNN` (mode 0600) before any migration that has the foreign-keys-off directive. That's a non-destructive safety copy of the user's data.
  7. **Security:** the bounded EXIF DateTimeOriginal reader is a new parsing surface, tracked as **SE-4** until the Group 4 review verifies it. Pillow still never parses upload EXIF.
  8. The module rename (`*_sessions.py` → `*_groups.py`) is deferred until after integration; it's mechanical.
  9. **In-flight work:**
     - T4.0n is replaced by this redesign. Its suppressors table, routes and picker are reused.
     - T4.0o (share) and T4.0p (share UI) land first.
     - T4.0m is done.
  10. **No git here, so the work happens in place with a planned red window:**
      - each task's Run command covers only its own slice;
      - the full suite goes green at R9;
      - Group 4's review runs only on the integrated tree;
      - the user's running app may misbehave during the window, because templates and JS are served from disk.
- **Tasks R1–R9** are in tasks.md, with single owners for shared files. Owners:
  - R1 (migration, connection, rows, repos, seed): coding-2;
  - R2 (schemas.py + design §4.7): coding-3;
  - R3 (io EXIF date): coding-5, after T4.0o;
  - R4 (core services): coding-4;
  - R5 (compare, dope, zeroing): coding-1;
  - R6 (routes + share header): coding-3, after R2;
  - R7 (pages): coding-6, after T4.0p;
  - R8 (e2e + smoke): coding-2, after R1;
  - R9 (integration gates): devops-1.

## B-69 R2 validator ownership (lead, 2026-10-02)
- "Weather entered and skipped together" is owned by the service (`invalid_weather`, 422) for both create and patch, so the error code is the same everywhere (redesign §2). The schema-level validator is dropped. All other R2 validators (strict shot_time, utc_offset, suppressor_id needs suppressor, create-only general purpose and null zero_id) stay.

## B-70 R1 judgement calls accepted (lead, 2026-10-02)
1. **Date CHECKs use `IS`:** `date(x) IS x` instead of `=`, which let invalid dates through via a NULL comparison. The same for shot_time and taken_at. This corrects redesign §1.
2. **The backup is taken once, before the first pending migration** (so a v2 database backs up before 003), named `<db>.pre-005`, never overwritten (`.pre-005.1`, …). A new or already-current database gets no backup.
3. **A rifle or load that still has groups can't be deleted** (no cascade on groups.load_id). The app has no delete route anyway, so no data can vanish silently.
4. **PATCH never changes a group's `load_id`.** `GroupUpdate` doesn't expose it, and the service rejects it. So a group can't move to another rifle.
5. The app-level v2 upgrade check moves to R9 because of the red window.

## B-71 Masked groups show size on the rifle page (lead, 2026-10-02)
- Overrides redesign §4's "size none when masked". ES in inches and MOA is size-only and was already shown for masked groups (B-18, T4.0k), so rows keep showing it. No POI, offset or direction is carried.

## B-72 Label for migrated session notes (lead, 2026-10-02)
- On the group page, notes carried over from a former session are labelled **"Earlier notes (carried over)"**, with no "session" wording. The redesign removed sessions from the user's vocabulary, and this keeps the R9 grep gate free of exceptions. The field name `legacy_session_notes` stays, as allowed by the gate.

## B-73 Grep-gate allowance for the stored source value (lead, 2026-10-02)
- `ShotTimeSource.SESSION = "session"` in domain/types.py is the stored `shot_at_source` value for migrated groups, fixed by the 005 CHECK. It's allowed by the R9 gate as one named exception (that line only). The UI shows it as "(carried over)" (B-72). Renaming the stored value would need a new migration; not worth it.

## B-74 USER REQUEST: "Groups" tab listing every group (2026-10-02)
- The user asked for a top-level tab listing every measured group (all rifles and loads), with quick details to the right of each, scrollable and openable, with selectable filters.
- **Lead decisions:**
  - Nav tab **"Groups"** → page `/groups`, rows newest first by (shot_date, shot_time, id).
  - Each row shows:
    - date/time, rifle, load + lot, distance, position, suppressor (name), shots n/m;
    - ES in/MOA/mil (size-only, so shown for masked groups too), the estimated badge, status (draft/final), purpose label;
    - a notes snippet.
  - The whole row links to `/groups/{id}`. No POI, offset or correction is ever shown in the list.
  - Filters: rifle, load (narrowed by rifle), distance, position, suppressor (any/none/yes + which), status, purpose, date from/to. They're applied as query-string parameters so a filtered view can be bookmarked (a GET filter form; no native POST, B-13). A "Clear filters" link.
  - Server-rendered, with paging of 100 rows per page, "Show more" via a `?page=` link. No new JSON API is needed.
  - It extends the existing list query in services/analysis (`list_groups`) with the extra filters.
  - → **R11** (coding-4), started after R9 is green so the integration gate isn't disturbed. Reviewed in the Group 4 review.

## B-75 Incident: 20 leaked CPU-hog processes (2026-10-02)
- **What happened:** devops-1's T3.9 loaded-smoke recipe started 20 `python -c "while True: pass"` processes. Its zsh cleanup (an unquoted `$HOGS`) silently failed, and it falsely reported "0 hogs". They ran for about 13 h 50 min at 35–40 % CPU each, inflating the pool's load averages (30–48) and slowing every test run in that period.
- **Fix:** devops-1 found them and killed them by their exact recorded PIDs (18629–18648). The lead verified with `ps` that none remain.
- **Rule:**
  - Any background load generator is tracked in a PID file, stopped by PID, and verified with `ps -p` per PID.
  - Shell loops quote their variables.
  - A "cleanup done" claim needs that verification output in the evidence.
- **Effect:** timing measured in that window is invalid. T4.0c runs only after the lead confirms real idle (load < 4 with no pool runs).

## B-76 USER REQUESTS: share/plot tweaks (2026-10-02)
- **(a)** In the share image, the spread-circle label shows its size with MOA and mil in parentheses, e.g. "Spread circle 1.20 in (1.15 MOA, 0.33 mil)". The group page legend uses the same format for consistency.
- **(b)** POA (aim) and mean POI are both **bright red** (`#ff1a1a`) on the group page, the analysis overlay, the review editor and the share image. They stay distinguishable by shape: the aim is a crosshair/ring, the POI an open thin x. The colour is outside the per-group palette, so it never collides with a group colour.
- **(c)** The share card gets a show/hide checkbox for the **shot numbers** on each hole. It's a new share layer `numbers`, on by default; schemas `ShareLayer` gains `numbers`.
- **Split:** T4.0q (coding-5: share.py + ShareLayer) and T4.0r (coding-6: page CSS, the legend label, the share-card checkbox).

## B-77 USER REQUEST: Settings → Share defaults (2026-10-02)
- The user asked for a Share section in Settings where they choose which share-card checkboxes start on or off.
- **Lead decisions:** this is the app's first stored setting.
  - **Storage:** migration 006 adds a `settings` table (key TEXT PRIMARY KEY, value TEXT JSON, updated_at). Key `share_defaults` holds `{photo, spread, thing_hitter, poi, correction, numbers}` booleans. With no row, the built-in defaults apply: photo, spread and numbers on; the rest off.
  - **API:** `GET /api/settings/share-defaults` and `PUT /api/settings/share-defaults` (registry + design §4.7). It's a JSON write through api.js with denial paths (403/415).
  - **Settings page:** a "Share" section with the six checkboxes and Save. The Offline/Online switch stays as is.
  - **Share card:** it now opens with these saved defaults (replacing "preset from the page toggles"). Layers a group can't offer stay disabled with their reasons, whatever the default says. Masking is unchanged.
  - Local only; nothing leaves the Mac.
  - → **T4.0s** (coding-3).

## B-78 USER REQUEST: Groups-tab filters collapsed by default (2026-10-02)
- The filter panel on /groups is hidden by default and expands on click. It uses a native `<details>`/`<summary>`, so no JS or inline style is needed (CSP-clean).
- The summary reads "Filters", or "Filters (N active)" when any are applied, so active filters are never invisible. It stays collapsed on load either way, as the user asked. Clear stays inside the panel and also appears next to the summary when filters are active.
- → **T4.0t** (coding-4).

## B-79 USER REQUEST: redo the scale reference from the review editor (2026-10-02)
- The user asked for a way, at the top of the review editor above the "N of M shots" counter, to go back and redo the scale reference (grid vs 1.5" circle, etc.) when it was wrong.
- **Lead decisions:**
  - **The button:** "Change scale reference" sits at the top of the editor, above the counter.
  - **Nothing saved yet:** if no group on the photo has saved shots or an aim, the button goes straight to `/photos/{id}/reference?group_id=…&return_to=<this review>`, the existing flow.
  - **Saved work on draft groups:** if a draft group on the photo has saved shots or an aim (the B-38 lock), the editor shows a confirmation naming what will be cleared: "This clears the saved shots and aim of N draft group(s) on this photo. Continue?" On confirm, `POST /api/photos/{id}/reference/reset` clears shots, aim, ROI and rejections for those draft groups and the photo's confirmed reference in one transaction, then goes to the reference page.
  - **Finalized groups:** if any group on the photo is **final**, the button is disabled with "A finalized group uses this scale". Nothing is cleared silently.
  - **API:** the new route enters the registry and design §4.7. It has denial paths, and returns 409 when a group is final. It's a deliberate, confirmed user action, so it doesn't count as a hidden destructive operation.
  - → **T4.0u** (coding-1).

## B-80 Idle timing deferred; the Group 4 review proceeds (lead, 2026-10-02)
- The machine isn't idle: the user's separate detector session (B-68) is training a model (`train_seg.py`, PID 88816 with multiprocessing workers). That's the user's process and isn't touched. T4.0c (detect + aims ≤ 3 s on an idle machine) is deferred until training finishes; the review records it as live-validation pending.
- The Group 4 review starts now on the integrated tree. Scope: T4.0a–u, T4.1–T4.13, R1–R11, UF1–UF3/U1–U2 follow-ons, plus every user request since Group 3 (B-45 through B-79).

## B-81 User-reported: context edits not reflected on the share image (2026-10-02)
- The user reported that editing a group's context (suppressed / which suppressor) didn't show up on the shared export image. The server reads the group's own fields per request (`share._conditions`) and sends `Cache-Control: no-store`, so the cause is probably client-side (stale preview, page state, the PATCH body) or header truncation. → **T4.0v** (coding-6): reproduce in Chromium, find the root cause, fix it, add a regression test. Group 4's review re-verifies it in cycle 2.

## B-82 USER REQUESTS: share export wording and fields; caliber dropdown (2026-10-02) — QUEUED until after the Group 4 cycle-1 verdict (code freeze)
- **Share export image:**
  - (a) Remove the "includes estimated shots" badge.
  - (b) The header shows the rifle model and chambering, e.g. "T3x .223". That's `{rifle.name} {rifle.cartridge}`, de-duplicated if the name already contains the chambering.
  - (c) The date is formatted **MM/DD/YYYY** (e.g. 10/02/2026). That applies to the share image only; the app's pages keep their format unless the user asks.
- **Labels everywhere** (group page toggles, legend, share card, share image, Settings → Share): the thing hitter circle becomes **"Thing-hitter"** (hyphenated, in quotes, e.g. `"Thing-hitter" circle`), and the spread circle becomes **Cone of fire**. Internal names (layer keys `thing_hitter`, `spread`) are unchanged.
- **Rifle form:** the caliber (bullet diameter) is a **dropdown** with exactly 0.224, 0.243, 0.257, 0.264, 0.277, 0.284, 0.308 and 0.338 in. The chambering text (e.g. ".223") stays free text.
  - The cartridge prefill selects the matching diameter.
  - Editing an existing rifle whose diameter isn't in the list shows it as an extra "current" option, so data isn't silently changed.
  - The API accepts the existing range unchanged; the restriction is in the UI.
- **Owners:** T4.0w (coding-5, share image a–c), T4.0x (coding-6, label renames), T4.0y (coding-4, diameter dropdown).

## B-83 USER: stop multi-agent work; the lead finishes alone (2026-10-03)
- **The user's words:** "I've used way too many tokens… moving to not running all of these concurrent agents. figure out what needs to finish up, have opus do it by itself, no more extreme token burning."
- **Effect:** no more agent spawns: no coding/devops pool and no reviewer or synthesizer agents. The lead implements, verifies and records everything directly.
- **Group 4 review:**
  - Cycle 1 was consumed (review-34 to 37) when every reviewer hit the weekly usage limit before writing a verdict.
  - Its full-suite run did finish: /tmp/rga-review34-gq6Xql/suite.log shows 5935 passed, 2 skipped, 1 xfailed, EXIT=0.
  - By the user's direction, no further review-agent cycles are spawned for Group 4 or for the end-to-end scope.
  - In their place, the lead's own full-suite run after B-82 is the closing verification (test-evidence.md).
  - This is a user-authorized change to the review process, not a PASS from a review agent. review.md therefore has no Group 4 or e2e verdict.
- **B-82** (T4.0w/x/y) is implemented by the lead directly. The code freeze is lifted.
- **T4.0c** runs now: the user's training process (PID 88816) has exited.
- **T5.1 README** is written by the lead.
