# Lead Runbook: rifle-group-analyzer

The main agent holds the lead role. This file carries the state that otherwise lives only in the conversation. **Read it first after any context compaction or resume**, then `decisions.md` (B-01 onward), `tasks.md`, and the "Build-time interface notes" at the end of `design.md`.

## Operating mode (decisions.md B-01, B-08)

**No shared task tools.** `TaskCreate`, `TaskUpdate`, `TaskList` and `TaskGet` don't exist in this session.
- `tasks.md` is the board.
- The lead assigns tasks by spawn prompt or `SendMessage`.
- Workers never edit `tasks.md`.
- The lead marks `_(in progress: <name>)_`, then `[x]` plus a `> Done.` note, from worker reports, and checks them against the disk.

**Verification.** Workers append their evidence to `test-evidence.md`. There are no verification sentinels and no hooks, so the lead checks the evidence by hand.

**B-32 standing rule:** decide technical details yourself and log them; only escalate product/scope, money/off-Mac, destructive actions, or a scope still blocked after one lead-authorized extra cycle.

**Running the groups.**
- The user authorized running all groups back to back without a per-group go-ahead (B-08). Advance as soon as the group's synthesizer returns PASS.
- Escalate to the user only in two cases: a scope reaches cycle 3 without PASS, or the Group 2 spike (T2.4) fails the P-11 synthetic thresholds. If the spike fails, hold T3.3/T3.4 and let the other Group 3 slices proceed.

**Ground rules.**
- The lead never writes code and never authors `review.md`.
- Workers format only their own files.
- New gaps become lead-added tasks, logged in `decisions.md` (as B-05 and B-06 were).
- Don't run a second lead session on this project in parallel; it causes duplicate work and file clobbering.

## Roster (named background agents, resumed via SendMessage to name)
| Name | Type | Group 1 work |
|---|---|---|
| devops-1 | devops-agent | T1.1, T1.8 |
| coding-1 | coding-agent | T1.2, T1.4 (+ sandbags fix) |
| coding-2 | coding-agent | T1.3, T1.6, T1.10 (+ sandbags fix) |
| coding-3 | coding-agent | T1.5 |
| coding-4 | coding-agent | T1.9, T1.7 (+ B-06 follow-up) |
| review-1 | review-agent | Group 1 cycle 1 synthesizer (Slice B + cross-module) |
| review-2 | review-agent | Group 1 cycle 1 analyst (Slice A: domain, transform, synth) |

coding-5 and coding-6 joined in Group 2 (pool: coding-1..6, devops-1). Reviewers used so far: review-1..19; next fresh names start at review-20. Reviewers are new per cycle; use fresh names (`review-3`, ...) to avoid misrouting.

## Group order and gates
1. **G1 Foundations: PASSED on cycle 3 (2026-10-01).** 10 tasks + 8 fix tasks.
   - Cycle 1 FAILED with 0 Critical and 13 Warnings.
   - The fix wave F1.1–F1.5 is done (B-09, B-10). The suite is 802 passed, 1 skipped.
   - Cycle 2 FAILED with 0 Critical and 4 Warnings (B-13).
   - Fix wave 2 is done (B-14). The suite is 901 passed, 1 skipped.
   - **Cycle 3 (TERMINAL) is in flight:** `review-5` (synthesizer) and `review-6` (analyst).
   - If cycle 3 isn't a PASS, report Group 1 blocked to the user and stop. On a PASS, open Group 2 at once (B-08).
   - Review sub-agent notices come to main, so relay them to the right reviewer (as was done for review-2).
   - If cycle 2 FAILs, run one more scoped fix wave, then cycle 3, which is terminal.
2. **G2 Core libraries + CV spike (IN PROGRESS):** 19 tasks (incl. lead-added T2.18 and T2.19). The spike gate is MET on synthetic (B-19); real photos are weak, and T2.19 (count-based tuning, user-approved) is in flight. Remaining: T2.16 (coding-2), T2.19 (coding-1), then T2.15 (needs T2.16).
   - **All 19 tasks are complete.** Cycle 1 FAILED (0 C / 23 W, B-23). Fix wave FX2.1–FX2.7 is DONE (B-24, B-25); the suite is 2910 passed, 2 skipped, 1 xfailed. Cycle 2 FAILED (0 C / 8 W, B-27). Fix wave 2 FY2.1–FY2.7 is DONE (suite 2998 passed, 2 skipped, 1 xfailed; real photos 68/78, 11 FP). Cycle 3 (terminal) FAILED with 1 W (G2-W17-R2), so Group 2 was BLOCKED. **The user authorized one extra fix + one extra review (B-29).** FZ2.1 (coding-5) is in flight. Cycle 4 resolved W17-R2 but found pre-existing G2-C1 → **Group 2 CLOSED with a tracked exception (B-30, SE-2)**, fixed by T3.00 in Group 3. If it isn't a PASS, report Group 2 blocked to the user and STOP.
   - Dependencies: T2.14 waits for T2.6, T2.10, T2.11. T2.16 waits for T2.1–T2.5. T2.15 waits for T2.16. T2.17 is devops.
   - **Spike gate:** T2.4 must meet spec §6 synthetic thresholds and write `spike-report.md`.
   - Planned first wave: coding-1 T2.4, coding-2 T2.2, coding-3 T2.18 (synth real-photo traits, B-12), coding-4 T2.3, coding-5 T2.1, coding-6 T2.5, devops-1 T2.17.
   - Queue after that: T2.6, T2.7, T2.8, T2.9, T2.10, T2.11, T2.12, T2.13, then T2.16, T2.14, T2.15.
   - Real photos are in data/real_photos and real-photo-notes.md. T2.2/T2.3/T2.4 carry B-12 criteria.
3. **G3 Feature slices (IN PROGRESS, B-31 wave 1):** coding-5 T3.00, coding-2 T3.0, devops-1 T3.0d, coding-4 T3.1, coding-3 T3.2, coding-1 T3.3, coding-6 T3.4; queue T3.5–T3.8, T3.10, then T3.9 smoke (T3 gate). (add lead tasks: truth.json EXPORT from a finalized group, B-21; (B-24 busy_timeout not needed: Python's default timeout is 5 s); plus B-20 masked-group exclusions in T3.5/T3.6) 8 coding + T3.9 devops (waits for T3.1–T3.4). **T3 smoke (`scripts/smoke.sh`) must pass before G3 is done.**
4. **G4 E2E journeys, contracts, scans:**
   - T4.1 (Playwright harness) goes first, then T4.2–T4.11 in parallel. T4.12 and T4.13 are independent.
   - Test authors only write tests. Any app defect found becomes a lead-created scoped fix task.
5. **End-to-end review scope:** its own 3 cycles, recorded in `review-e2e.md`, after all groups PASS. Run the T4 suite first and give the results to the reviewer.
6. **Phase 4:** T5.1 README via the `documentation` skill.
   - Mention `make bootstrap`, `make run`, the project-local `.playwright-browsers/`, and `SCAN_IGNORE_VULNS`.
7. **Cleanup:** confirm every task is `[x]`, and stop any running agents.

Reviewer counts:

| Scope | Reviewers | Slices |
|---|---|---|
| G1 | 2 | vision/synth/domain; db/web |
| G2 | 4 | vision; domain; db+services |
| G3 | 4 | split the 8 slices |
| G4 | 2 | — |
| E2E | 2–3 | — |

Review headings in `review.md` are `## Group N — Cycle M`. Derive the next cycle number from the disk, and log every synthesizer spawn in `decisions.md` B-07, since spawning one consumes a cycle.

## Worker prompt template (coding/devops)

Every worker prompt states:
- The instance identity, e.g. "You are `coding-N`, one of K parallel instances".
- The global rules are loaded.
- This is a LOCAL app, so spec §8 and test-plan §1 say which AWS rules don't apply.
- **DEGRADED MODE:** no task tools; don't edit `tasks.md`; report to `main` by SendMessage with the files, Run command, exit code, key output and downstream notes; append evidence (append only); no sentinel.
- Invoke `spec-workflow` first, then read `spec.md`, `design.md` (§1, §3, §4 binding, plus the interface notes at the end), `test-plan.md`, the `tasks.md` task, and `decisions.md`.
- Use absolute paths. Run commands as `cd /Users/calebschwarz/Documents/Hunting && .venv/bin/...`.
- Never install anything, and ask the lead for any new dependency.
- Write only the listed files. `conftest.py`, `pyproject.toml` and the `Makefile` are T1.1-owned.
- Report bugs in files you don't own; don't fix them.
- Use minimal docstrings and no why-prose. Invoke `documentation` at close-out if any docs are touched.
- Never imply a check ran when it didn't.
- **Mutation checks with `PYTHONDONTWRITEBYTECODE=1` / `-B`** (stale .pyc gives false kills).
- **Mutation checks only on a scratch copy** (e.g. `/tmp/<name>_scratch`), never by editing repo files in place. Parallel agents may read them mid-mutation (coding-2 flagged this from F1.1).

## Reviewer prompt template

State the scope ("Group N, cycle M of 3") and the role:
- **Analyst:** reviews its slice and sends findings to the synthesizer. It writes no files.
- **Synthesizer:** reviews its slice plus cross-module consistency, merges the analysts' findings, and is the sole author of `## Group N — Cycle M`, with exactly one PASS/FAIL.

Tell reviewers about degraded mode: the disk is ground truth, there are no commits, and missing sentinels are expected.

**FAIL triggers:**
- a missing test that would fail without a new permission, setting or schema element
- an assertion on a mock
- a completion note claiming verification its Run didn't perform
- verbose why-prose
- a missing public docstring
- any numeric correction from a 2–9 shot group outside Step 1
- any shot-removal reason other than `not_a_bullet_hole`
- any outbound network or CDN use
- any mean-radius or best-N metric

## Outstanding for the user
- **Real-photo gate:** 10–20 annotated photos, in the format in `tests/real/README.md` (written by T2.15). Report it as outstanding until they're supplied.
- **Optional setup:** authorize `context7` via `/mcp`, and install `uv` (`brew install uv`) for the aws-core and aws-data-analytics MCP servers. Neither matters for this app.
- **Suggestion:** a Starlette `TestClient` httpx deprecation warning appeared. The dependency decision is deferred.

## Current state (keep updated)
- **2026-10-03: SOLO MODE (B-83).** The user stopped all agent spawns. The lead does any further work directly, with no pool, reviewers or synthesizers.
- **Status:** all build tasks are [x], including B-82 (T4.0w/x/y), T4.0c timing and the T5.1 README. T4.0n stays superseded.
  - Final full suite: 5950 passed, EXIT=0.
  - No review-agent verdict exists for Group 4 or end-to-end; see B-83.
- **The user's app:** running on 127.0.0.1:8765 (schema v6), restarted after B-82 via scripts/run.sh. Find its PID with `lsof -nP -iTCP:8765 -sTCP:LISTEN`.
- **Cleanup done:** /tmp/rga* is cleared (only the final suite and timing logs remain), the sentinel dir is empty, and no stray processes remain.
