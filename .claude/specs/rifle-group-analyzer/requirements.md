# Requirements: Rifle Group Analyzer

> Slug: `rifle-group-analyzer` · Source: /co-plan · Date: 2026-09-30

## Project Summary

A local web app on the user's Mac that turns photos of paper targets into a per-rifle statistical record. It detects bullet-hole centers automatically, lets the user review and correct them, computes group statistics, compares loads, aggregates groups, records real dope at distance, and runs a zeroing / zero-check process taken entirely from Formidilosus's (Rokslide) published method. All data lives in a local SQLite database.

## Intent & Success Criteria

**Who it's for:** the user (a single rifle shooter/hunter). They own multiple rifles and shoot both factory ammunition and handloads.

**What they do today:** they use the Hornady Ballistics app's Group Analysis ($4.99 in-app purchase). There, shots are plotted manually on a photo. The app has no export, the results can only be screenshotted (and not fully), and groups can't be combined across sessions.

**Why now:** they want to understand their rifle systems statistically over time. The Hornady app traps the data and can't aggregate it.

**Observable success criteria:**
1. The user drops in a target photo, selects the reference object, confirms the detected holes, marks the aim point, and gets extreme spread (inches and MOA) plus mean POI offset (mils).
2. Factory loads and handloads can be compared side by side by extreme spread. The comparison is guarded against small samples (see FR-5).
3. The POI shift of each load relative to the rifle's zero load is reported in mils, with the scope unadjusted.
4. Groups chosen by the user can be overlaid into one aggregate (e.g. 3×10 → 30), with its extreme spread and center.
5. Zeroing guidance follows Form's procedure, with corrections to the nearest 0.1 mil (mil/mil scope assumed).
6. A zero check (1–2 shots against the stored dot) returns pass or "possible lost zero."
7. A per-load dope table records real elevation (mils) by distance.
8. Session notes (e.g. "went high after switching ammo") are captured and shown with their groups.

**Smallest worthwhile version:** manual or automatic shot entry from a photo with correct scaling, extreme spread and POI statistics, load comparison with guardrails, and the zeroing flow.

## Functional Requirements

### FR-1 Rifles, scopes, loads
1.1 Support multiple rifles. Each rifle has a scope (mil/mil assumed; corrections in 0.1 mil). *Accept:* groups, loads, zeros and dots are always scoped to one rifle.
1.2 Loads come in two kinds: **factory** (maker, product, bullet weight/type, lot number) and **handload** (bullet, powder, charge, primer, batch ID). Seating depth (COAL/CBTO) is **not** a tracked variable and may go in notes. *Accept:* a load can be created and edited with these fields only.
1.3 Optional **trued MV** field per load, entered from the user's own ballistic app. No velocity SD/ES fields. *Accept:* the field exists and is optional.
1.4 The bullet diameter is derived from the caliber and used for hole rendering (FR-3.4).
1.5 Every group records the lot or batch. A new lot of an existing load is treated as needing its own POI check against the zero load (different lots can have different POI). *Accept:* the tool flags the first group of a new lot.

### FR-2 Sessions and groups
2.1 A session has a date, rifle, distance, shooting position (bench, prone off pack, kneeling/sitting, standing/field), suppressor on/off, and free-text notes.
2.2 Weather basics (temperature, optional density altitude and wind) are **prompted when distance ≥ 200 yd** and optional below that.
2.3 A group belongs to a session and a load. It stores each shot's X/Y offset from the aim point in inches and mils, and the elevation dialed (for dope at distance).
2.4 Session and group notes are free text, searchable within the app.

### FR-3 Photo pipeline and hole detection
3.1 The user imports a target photo (e.g. AirDropped from their phone) into the local web app.
3.2 A **dropdown** selects the reference object: 1" grid, 1" circle, 1.5" circle, 3" circle. Every photo contains one. The reference sets pixel-to-inch scale. A grid also allows perspective correction. *Accept:* stats on a test photo match hand-measured values within the agreed tolerance (see Assumptions).
3.3 **Classic computer vision** (local, no network) detects hole **centers**. No AI or vision-model call is used in v1.
3.4 Each hole is drawn as a **fixed circle at the bullet's true diameter**, scaled from the reference. Hole size is never inferred from the image. Known diameter helps split merged holes (a blob about twice the size is likely two shots).
3.5 **Mandatory review step:** the user can add a missed hole, drag a misplaced center, delete a non-hole, and mark the aim point. Deletion is allowed **only as "not a bullet hole" (detection error)**, never as a "flier."
3.6 All measurements are center to center.
3.7 **Expected shot count and ragged-hole best guess:** the user enters the number of shots fired for each group. If, after every distinct hole is accounted for, fewer holes are found than shots fired, the remaining shots are placed by **best guess inside the larger jagged/merged hole(s)**. Estimated positions stay within the ragged hole's outline, spaced by the bullet diameter where the outline allows. Estimated shots are visually marked as "estimated," count fully in all statistics (no shot is ever dropped), and can be dragged by the user. A group containing estimated shots is labeled as such in its stats. *Accept:* a 10-shot photo with 7 distinct holes and one ragged hole yields 10 shots, 3 marked estimated, all inside the ragged outline.

### FR-4 Group statistics
4.1 Headline metric: **extreme spread** in inches and MOA, framed as "the smallest target every shot hit."
4.2 **Mean POI offset** from the aim point, in mils (and inches).
4.3 **No mean radius** and no "best N of M" or group-reduction views.
4.4 **Aggregate overlay:** the user selects which groups to combine. Each group is aligned on its own aim point, and the tool trusts that the scope wasn't adjusted between them. Output: combined extreme spread, combined center, and shot count.

### FR-5 Load comparison guardrails
5.1 Loads are not ranked or compared until each has **≥ 10 rounds** in comparable conditions; 20+ is preferred and labeled as higher confidence.
5.2 When two loads differ by less than normal group-to-group variation (10-round groups can vary about ±30% with nothing changed), the tool says **"no real difference."**
5.3 A plain precision verdict per load against Form's bar: sub-2 MOA for 10 shots is sufficient for deer and elk to 600 yd (1.5 MOA to 800).
5.4 Each load's **POI shift relative to the rifle's zero load** is reported in mils. Advice is always to zero the rifle to the hunting load, never to enter a ballistic-app offset.

### FR-6 Zeroing (Form's method, complete)
6.1 Zeroing distance is 100 yd. All corrections are in mils, rounded to the nearest 0.1, with no unit conversions.
6.2 **Step 1, get on paper** (new scope or rifle, or after a scope swap): after bore sighting, fire 1–3 shots, read the offset, and dial it. This is the **only** situation where the tool recommends a correction off fewer than 10 shots.
6.3 **Step 2, 10, adjust, 10:** a 10-shot group on one aim point. Correct off the center of **all** shots (no discounting). Then 10 more to confirm, and overlay both into a 20-shot group. If the 20-shot center is off by about a click, a 0.1 mil correction is acceptable. Optional 30-round confirmation for very high confidence.
6.4 Outside Step 1, the tool **never recommends a correction off a 3–5 shot group**.
6.5 **Step 3, the dot:** the extreme spread of the 20+ shot zero group becomes the rifle-and-load "dot" size (e.g. a 1.2" 20-shot group → use a 1" dot).
6.6 **Zero check:** 1–2 shots. Inside or touching the dot means pass, with no change. Outside the dot means "possible lost zero," route back to Step 2, and prompt a mount and scope check (the knowledge base ranks incorrect mounting as the #1 cause and scope failure as #2).
6.7 No zero offsets are entered in the ballistic app. Zero POA to POI.

### FR-7 Dope at distance
7.1 Per load, record the real elevation dialed (mils) and the group center at each distance, building a dope table. The tool does **not** compute or back-calculate muzzle velocity. The user does that in their own ballistic app and may store the result as trued MV (FR-1.3).

## Non-Functional Requirements

- **Scale:** one user, multiple rifles, hundreds to low thousands of groups over years. SQLite is sufficient.
- **Performance:** detection and statistics for one photo in a few seconds on a Mac.
- **Privacy/security:** fully local. No network calls in v1, no API keys, no accounts. The web server binds to localhost only.
- **Availability:** runs when the user starts it. No uptime requirement.
- **Cost:** $0 to run (no API usage in v1).
- **Maintainability:** the user maintains it. Prefer simple, well-known tools. Python is recommended because OpenCV is native there (see decisions.md). Dependencies are isolated in a venv and pinned per the user's execution-hygiene rules.
- **Extensibility:** the data model must leave a clean hook for later Open Brain sync (posting readable session and group summaries), without restructuring.
- **Units:** inches and MOA for group size, mils for offsets and corrections. Mil/mil scope is always assumed.

## Edge Cases & Failure Behavior

- **Merged or overlapping holes:** the tool uses the known bullet diameter to suggest a split. The user confirms in review.
- **Reference object not found or ambiguous:** the tool asks the user to mark the reference manually and never guesses a scale.
- **Photo taken at an angle:** with a grid, correct perspective. With a circle reference only, warn if the circle appears noticeably elliptical.
- **Hole count uncertainty:** the user's review is the final authority. No external cross-check in v1.
- **Too few shots for a correction:** show "shoot more" guidance per FR-6 instead of a number.
- **Too few rounds to compare loads:** show "insufficient data" per FR-5.1.
- **Must never happen:** a zeroing correction off a 3–5 shot group outside Step 1; a shot silently excluded as a "flier"; a scale applied without a confirmed reference; data leaving the Mac.

## Out of Scope

- Open Brain sync (planned next; the hook is kept in the data model)
- Phone or range use, remote access, or hosting
- Chronograph import (Garmin, LabRadar, etc.)
- Ballistic solver or muzzle-velocity back-calculation
- Any AI or vision-model call (including the hole-count cross-check), revisited only if detection proves unreliable on real targets
- Dot-drill hit/miss logging and hit-rate rollups
- Per-rifle zero-history log (correction events, failure causes)
- Shot-order or "stringing" analysis (Form treats stringing as noise)
- Mean radius and group-reduction statistics
- Full reloading log (brass prep, annealing, component lots beyond the batch)
- Barrel round-count tracking

## Assumptions to Validate

1. **Classic CV can find hole centers reliably on the user's real, mixed targets** (varied target types, lighting, angles). *Least certain.* Validate cheaply by running a detection prototype on 10–20 of the user's existing target photos before building the rest of the UI.
2. **The reference object (grid or circle) can be located and scaled accurately enough** for 0.1 mil decisions at 100 yd (0.1 mil ≈ 0.36"). Validate against hand-measured targets. A tolerance well under 0.1" is the goal.
3. **Center-only detection plus the fixed bullet diameter is enough to resolve most merged holes**, with the user's review covering the rest.

## Open Questions

- Measurement tolerance target for the photo pipeline (proposed: < 0.05" error on center positions at 100 yd). → fullstack-agent to propose and the user to confirm.
- Perspective correction with circle-only references: warn only, or attempt ellipse correction? → fullstack-agent.
- Testing standards: the user's global rules are AWS-oriented (T3 deployed-resource checks). This is a local app, so fullstack-agent should map the tiers to unit, integration (photo → stats against hand-measured fixtures), and an end-to-end journey through the local web UI, and state explicitly which AWS-specific rules don't apply.
- Exact shooting-position list for the dropdown. → user, during planning Answered: bench, prone off pack, kneeling/sitting, standing/field (sandbags removed).

## Notes

- **Method source:** the zeroing and statistics doctrine comes from the user's Rokslide knowledge base at `/Users/calebschwarz/Rokslide Scrape/` (Formidilosus corpus). Relevant claims: LD-01, LD-04, LD-05, LD-07, LD-08, LD-10, LD-13, LD-20–LD-23, LD-26, LD-27, FB-12, MET-17, MET-25, OPT-22. Key posts read during planning: MIL-FORM-001, MIL-FORM-003, TIK-FORM-010, TIK-FORM-028, TIK-FORM-030, 22CM-FORM-016, 22CM-FORM-019, 22CM-FORM-020, CBHB-FORM-013, CBHB-FORM-024, CBHB-FORM-061, SFE-FORM-003, WYBTS-FORM-134, WYBTS-FORM-142. That corpus is read-only reference. Do not modify it.
- **Hornady Group Analysis research** (why the tool is self-built): manual shot plotting, outputs ES, mean POI, mean radius and per-shot X/Y, no export, no multi-group aggregation, reported bugs. Sources: hornady.com/4dofapp, Hornady Podcast Ep. 067, App Store listing, Outdoor Life "What is Mean Radius."
- **Suggested build order:** (1) data model and manual shot entry with stats, (2) photo import, reference scaling and perspective, (3) automatic center detection and the review UI, (4) zeroing flow, zero check and overlays, (5) load comparison guardrails, (6) dope table.
