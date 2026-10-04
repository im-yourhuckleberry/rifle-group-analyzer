# Real-photo ground truth and evaluation

The synthetic catalog gates the detector, but only real photos can show how it behaves on a real
range. This folder documents the annotation format and the tool that scores the pipeline against it.

- Photos and their truth files live in `<root>/data/real_photos/` (gitignored, private). They are not
  checked in, so CI skips the real-photo test with the message `real-photo gate outstanding`.
- Run: `.venv/bin/python -m tools.eval_real_photos` (options below).
- Use full-size originals (AirDrop or Files), not chat-downscaled copies: pixel size affects the
  centre-error tolerance.
- `counts.json` (this folder) holds approximate hand-read hole counts for `real_01..05`. It is **not**
  ground truth and is only used by the optional `--coarse` mode.

## File layout

```
data/real_photos/
  range_day1.jpg                 the photo (jpg, jpeg, png or heic)
  range_day1.truth.json          its annotation: <photo stem> + ".truth.json", same folder
```

A photo without a truth file is listed as "not annotated" and not scored. A truth file without a photo
is ignored.

## `<photo>.truth.json` (format version 1)

All pixel coordinates are in the **oriented image**: the photo after EXIF rotation is applied, `x`
right, `y` down, OpenCV pixel coordinates (the first pixel's centre is `(0, 0)`). This is exactly the
image the app stores and shows for the photo (`GET /api/photos/{id}/image?variant=oriented`).

| Key | Type | Required | Meaning |
|---|---|---|---|
| `format_version` | `1` | no | Defaults to 1; any other value is rejected. |
| `reference_kind` | `grid_1in` \| `circle_1in` \| `circle_1_5in` \| `circle_3in` | yes | The reference printed on the target. |
| `distance_yd` | number > 0 | yes | Shooting distance. |
| `bullet_diameter_in` | number, 0.1 to 0.6 | yes | Bullet diameter used for detection. |
| `holes_px` | `[[x, y], ...]` | yes | Centre of **every** confirmed hole, oriented px. |
| `aim_px` | `[x, y]` or `null` | no | Aim point for single-group files (default `null`). |
| `hand_es_in` | number >= 0 or `null` | no | Hand-measured extreme spread (centre to centre, inches) of the single group (default `null`). |
| `reference` | object | no | The reference the user **confirmed** in the app (below). Enables the scale-error row. |
| `groups` | list | no | Several groups on one sheet (below). Without it, all holes form one group. |

`reference`:

| Key | Meaning |
|---|---|
| `method` | `"manual"` (default) or `"auto"`, as confirmed. |
| `h_px_to_plane` | 3x3 matrix, oriented px to plane inches (the photo's stored `h_px_to_plane`). Must be invertible. |

Each `groups[]` entry:

| Key | Required | Meaning |
|---|---|---|
| `name` | no | Label in the report (`group1`, `group2`, ... by default). |
| `hole_indices` | yes | Indices into `holes_px` of this group's holes; unique, and no hole may belong to two groups. Holes in no group are ignored (for example holes in the backing). |
| `shots_expected` | no | Expected shot count passed to the resolver; defaults to `len(hole_indices)`. |
| `roi_px` | no | `[x0, y0, x1, y1]` in oriented px, `x1 > x0`, `y1 > y0`. Detection is limited to it. Without it the whole sheet is searched. |
| `aim_px` | no | The group's aim point. |
| `hand_es_in` | no | Hand-measured ES of this group, or `null`. |

Minimal file (one group, everything else defaulted):

```json
{
  "reference_kind": "grid_1in",
  "distance_yd": 100,
  "bullet_diameter_in": 0.224,
  "holes_px": [[1210.5, 804.0], [1275.2, 790.8], [1190.0, 880.3]],
  "aim_px": [1240.0, 830.0],
  "hand_es_in": 1.12
}
```

Full file as the app exports it (two groups, confirmed reference):

```json
{
  "format_version": 1,
  "reference_kind": "circle_1in",
  "distance_yd": 100,
  "bullet_diameter_in": 0.224,
  "holes_px": [[610.2, 700.1], [655.0, 690.4], [980.8, 702.9], [1002.3, 745.0]],
  "aim_px": null,
  "hand_es_in": null,
  "reference": {"method": "manual", "h_px_to_plane": [[0.0021, 0.0, -1.4], [0.0, 0.0021, -0.9], [0.0, 0.0, 1.0]]},
  "groups": [
    {"name": "left dot", "roi_px": [520, 600, 760, 820], "aim_px": [640, 710], "shots_expected": 2,
     "hole_indices": [0, 1], "hand_es_in": null},
    {"name": "right dot", "roi_px": [900, 620, 1100, 820], "aim_px": [990, 720], "shots_expected": 2,
     "hole_indices": [2, 3], "hand_es_in": 0.55}
  ]
}
```

## How the app produces it (no hand-written JSON)

The user marks and finalizes each group in the review editor on the confirmed reference. An export of
a photo's finalized groups needs only data the app already stores:

| Truth field | From the app |
|---|---|
| `reference_kind`, `reference.h_px_to_plane`, `reference.method` | the photo's confirmed reference |
| `distance_yd` | the session |
| `bullet_diameter_in` | the rifle |
| `holes_px` | each finalized shot's plane position (`x_in`, `y_in`, working-plane inches), converted to oriented px |
| `groups[].hole_indices`, `shots_expected`, `name` | the group's shots (all shots of the group, in order) and its expected count |
| `groups[].roi_px` | the group's stored ROI (working px), mapped to oriented px and boxed |
| `groups[].aim_px` | the group's aim point (plane inches), converted like a shot |
| `hand_es_in` | typed by the user in an optional field, otherwise `null` |

Shots are kept as working-plane inches, so the conversion to oriented px is
`px = inv(h_px_to_working) @ (inches * ppi)`, where `ppi` and `h_px_to_working` come from
`build_working_image` for the confirmed reference. The reference implementation is
`tools.eval_real_photos.plane_in_to_oriented_px(points_in, ppi, h_px_to_working)`, and
`tools.eval_real_photos.truth_document(...)` builds the JSON document from those values. A test pins
the round trip.

## Running the evaluation

```
.venv/bin/python -m tools.eval_real_photos [--dir data/real_photos] [--json report.json]
                                           [--overlay-dir DIR] [--scale-pct 0.5] [--coarse]
```

For every annotated photo the tool runs the real pipeline: `propose_candidates` for the annotated
`reference_kind`, then `build_working_image`, then `analyze_group` per group (ROI mapped to working
px). It plays the user's confirm step by picking the proposal closest to the annotated reference
(rank 1 when the file has no `reference`). If nothing is proposed it falls back to the annotated
reference (`ref` column `annotated`); with neither, the photo is reported as `no_reference`.

### Scoring

The tool scores the **resolved shots**: the detected, split and estimated shots the app would show
after `resolve_shots` has brought the group to `shots_expected`. Matching is one to one (Hungarian,
gate 0.5 bullet diameters) between the shots and the annotated holes. The detector never decides
which holes are exempt.

Ragged blobs get a count-based credit. For each detected ragged outline, the annotated holes inside
it count as recalled only up to the number of resolved shots placed inside that outline. Holes beyond
that are misses, and shots beyond the number of holes are false positives. So a blob that hides four
annotated holes but receives one resolved shot costs three misses (a test pins this case), while a
blob that receives four estimated shots is credited all four (the user then drags them into place).
`ragged_holes` and `ragged_credited` report the holes inside ragged outlines and the credit given.

Reported per group and pooled:

| Metric | Gate (spec section 6, real photos) |
|---|---|
| Reference scale error (auto proposal vs the annotated reference, percent) | at most 0.5 % per photo |
| Hole-centre error p95 (inches; matched shots only, estimated credits have none) | at most 0.05 in |
| Recall (pooled) | at least 90 % |
| False positives per photo | at most 5 |
| Group ES error: app ES vs the ES of the annotated holes, and vs `hand_es_in` when given | at most 0.05 in (groups with estimated shots are reported, not gated) |
| Runtime per photo (proposal, working image, detection) | at most 3 s |

The JSON report (`--json`) also carries each group's mean-POI error. Ten to twenty annotated photos
make the gate final; with fewer the run prints `provisional`.

### Verdict: PASS, FAIL or INCOMPLETE

- **FAIL**: a row misses its limit, or a photo could not be evaluated (unreadable, holes outside the
  image, no reference at all).
- **INCOMPLETE**: a row had nothing to evaluate. It never counts as a pass. Rows that can be
  incomplete:
  - scale: a photo has an automatic proposal but its truth file has no confirmed `reference` (the
    minimal format above can therefore never pass), or no photo has both. A photo where nothing was
    proposed is reported as `manual_reference_needed` and falls back to its annotated reference;
  - centre error: no annotated hole was matched by a shot;
  - group ES: no group has two or more shots and none estimated.
- **PASS**: every row was evaluated and met. `hand_es_in` is optional and only gates when given.

Exit codes: `0` PASS; `1` FAIL or INCOMPLETE; `2` bad arguments or an invalid truth file (the file and
field are named); `3` real-photo gate outstanding (folder missing or empty, or no photo has a truth
file).

`--coarse` additionally compares photos that have no truth file with the approximate counts in
`counts.json` (found vs visual count). It is informational and never changes the exit code.
