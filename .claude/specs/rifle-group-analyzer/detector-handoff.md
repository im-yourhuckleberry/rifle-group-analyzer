# Handoff: bullet-hole detector work (separate Claude Code session)

**From:** the rifle-group-analyzer build lead. **Date:** 2026-10-02.
**Project root:** `/Users/calebschwarz/Documents/Hunting`. The app is `src/rga/`, run with `./scripts/run.sh`.

## 1. Goal

Build a better bullet-hole detector, by training or via an API model. It must plug into the existing app without changing anything else. It needs higher recall and fewer false positives on real phone photos of paper and cardboard targets. Today's classic-CV detector scores:
- synthetic gate (P-11): 23 of 23 scenarios pass;
- real photos: 68 of 78 holes found, 11 false positives, on 5 photos. These are visual counts, not exact truth.

Real-photo targets (spec §6): **recall ≥ 90 %, ≤ 5 false positives per photo, hole-centre error p95 ≤ 0.05 in, total runtime ≤ 3 s per photo.**

## 2. Hard constraints (read before anything else)

1. **The app stays offline by default.** Offline mode is the product as built. Any network model belongs behind the future **Online** mode, which today is a greyed-out placeholder on the Settings page (decisions.md B-51).
   - In Offline mode, nothing you add may open a socket.
   - The app's test suite must pass with no network. A test enforces loopback-only.
2. **Sending photos off the Mac, or spending money, is the user's decision** (B-32 escalation rule). Before any upload or paid API call, confirm with the user:
   - which provider and model ("mini 4.0" was the user's wording; confirm what they mean);
   - what data is sent;
   - where the API key lives. Never put it in the repo or in `data/`; use an env var or the macOS keychain.
3. **The real photos are private** (`data/real_photos/`, mode 0700, gitignored; B-54).
   - Don't copy, crop or export them anywhere: not to /tmp, not to a dataset folder, not to the cloud.
   - Read them in place only. If you must write derived images, use a `mktemp -d` directory with mode 0700 and delete it before you finish.
4. **Don't edit this project's `src/` or `tests/`.**
   - The main build session is actively changing them right now: sessions are being removed, groups are gaining per-group context, and a share card is in progress. Parallel edits will clobber.
   - Work in a sibling folder, e.g. `/Users/calebschwarz/Documents/Hunting-detector-lab/`. Import the app read-only through its venv (`/Users/calebschwarz/Documents/Hunting/.venv/bin/python`), or copy the repo there.
   - Deliver an **adapter module plus evidence** (§6). The build lead integrates it.
5. **Don't install packages into this project's `.venv`.** Use your own venv in the lab folder. List any runtime dependency the adapter needs; the lead adds it to the hash-locked `requirements.lock`.

## 3. Where the detector sits in the pipeline

```
photo bytes → vision.io.load_oriented → oriented BGR image (EXIF rotation applied, metadata stripped)
            → reference confirmed by the user (circle or 1" grid) → h_px_to_plane (3×3)
            → vision.pipeline.build_working_image → WORKING image: rectified, ppi known
            → vision.detect.detect_holes(working, frame, bullet_diameter_in, roi_px)  ← YOU REPLACE THIS
            → vision.ragged.resolve_shots(blobs, shots_expected, bullet_d_px)        (unchanged)
            → the user reviews in the editor → finalize
```

You only replace `detect_holes`. Rectification, scale, the shot-count resolution for ragged holes, aim detection and the review editor all stay as they are.

## 4. The interface you must implement (binding)

```python
# src/rga/vision/detect.py, current signature; keep it exactly
def detect_holes(
    working: np.ndarray,             # H×W×3 uint8 BGR (OpenCV order), rectified; may be H×W gray
    frame: WorkingFrame,             # rga.domain.types.WorkingFrame(ppi: float, width_px: int, height_px: int)
    bullet_diameter_in: float,       # e.g. 0.224, 0.264, 0.308
    roi_px: tuple[int, int, int, int] | None = None,   # (x0, y0, x1, y1) working px; None = whole image
    params: DetectParams | None = None,                # yours may ignore it
) -> list[Blob]: ...
```

Deliver a class with the same call shape, so the lead can switch detectors with one line:

```python
class HoleDetector(Protocol):
    name: str                 # e.g. "cv-v1", "yolo-holes-0.3"
    version: str
    requires_network: bool    # must be False for anything used in Offline mode
    def detect(self, working, frame, bullet_diameter_in, roi_px=None) -> list[Blob]: ...
```

**Output: `rga.domain.types.Blob`**, a frozen dataclass. Return a `list[Blob]` sorted by `(y, x)` of `centers_px[0]`.

| Field | Type | Meaning |
|---|---|---|
| `kind` | `BlobKind` | `"single"` (one hole), `"split"` (a merged blob you resolved into ≥ 2 distinct holes), `"ragged"` (one torn outline hiding several holes whose centres you can't place) |
| `contour_px` | `tuple[tuple[int, int], ...]` | Outline polygon in **working px**, closed implicitly, ≥ 3 points. For `single` a circle-ish outline is fine. For `ragged` it **must be the real torn outline**, because the resolver places estimated shots inside it. |
| `area_px` | `float` | Area of the outline in px². |
| `centers_px` | `tuple[tuple[float, float], ...]` | Hole **centres** in working px, sub-pixel: 1 for single, ≥ 2 for split, 1 representative for ragged. |
| `confidence` | `float` in (0, 1] | Your score. The editor doesn't threshold on it today; keep it calibrated anyway. |

**Coordinates and units:**
- Working px: `x` right, `y` down, the first pixel's centre is `(0, 0)`.
- 1 inch = `frame.ppi` px. The bullet diameter in px is `bullet_diameter_in * frame.ppi`.
- The centre of a hole is the centre of the bullet's path, so ES is measured **centre to centre**. It is not the centroid of a torn, light-cored region.

**Rules:**
- With `roi_px`, return only blobs whose centre lies inside it. An empty ROI, or one entirely outside the image, returns `[]`.
- Bad input (non-image array, `bullet_diameter_in <= 0`, `ppi <= 0`) raises **`ValueError`** and nothing else. Never raise another exception type. The app maps ValueError to a 4xx; anything else becomes a 500.
- **Deterministic:** the same input gives the same output, so seed everything.
- **Budget:** ≤ 1.5 s for a typical group region and ≤ 3 s for a whole 12 MP sheet, on an Apple-silicon CPU. If you need the GPU/ANE through Core ML or MPS, say so.
- **Never invent holes to reach a count.** The resolver fills a shortfall with `estimated` shots inside `ragged` outlines. You report only what you see. Over-reporting splits is worse than reporting one `ragged` blob.
- **Ignore:** printed marks (grid lines, text, QR codes, aim diamonds and dots, pen marks), staples, wrinkles, shadows, cardboard corrugation and creases. These are the main real-photo false positives.

If the model runs as a **service** (Online mode), the wire format is the same data as JSON:

```json
{ "detector": {"name": "…", "version": "…"},
  "holes": [ {"kind": "single|split|ragged",
              "centers_px": [[x, y], …],
              "contour_px": [[x, y], …],
              "area_px": 123.4,
              "confidence": 0.93} ] }
```

The request sends the working image as PNG bytes plus `ppi`, `bullet_diameter_in` and `roi_px`.

## 5. Data you can use

**Synthetic (unlimited, fully labelled):**
- `tests/synth/generate.py` with `render_named(name, seed) -> (img_bgr, truth)`. The 23 scenarios are in `tests/synth/catalog.json`, and pen crosses and bare grids are in `tests/synth/aim_scenarios.py`.
- `truth` includes:
  - `holes_in` (hole centres in plane inches);
  - `h_plane_to_image` (3×3, plane in → image px);
  - `ppi`, `bullet_diameter_in`, `ragged_groups`, `ragged_outlines_in`, `merged_pairs`;
  - decoys: `decoys_in`, `print_decoys_in`, `staples_in`, `wrinkles_in`, `creases_in`;
  - `holes_light_core` and `holes_wipe` (hole appearance);
  - `background` (paper or cardboard), `non_planar`, `groups` and `reference`.
- Vary seeds freely. Don't edit `catalog.json`: it defines the 23-scenario gate.

**Real (few, private, ground truth in progress):**
- `data/real_photos/real_01..05.jpg`. These are about 3 MP chat-downscaled copies; full-size originals are still to come from the user.
- `tests/real/counts.json` has approximate hand counts only; it is **not** truth.
- Exact truth arrives as `<stem>.truth.json`, in the format in `tests/real/README.md`: oriented px, `holes_px`, optional `groups[]` with `roi_px` and `hole_indices`, and the confirmed `reference.h_px_to_plane`. The user creates these by correcting holes in the app's review editor and running `python -m rga.export_truth --photo-id N`.
- Treat the real photos as a **held-out evaluation set**, not training data, unless the user explicitly agrees otherwise.

## 6. What to hand back (deliverables and format)

Put all of this in the lab folder and send the build lead a short report.

1. **`hole_detector_<name>.py`:** the adapter implementing §4. It is importable with only the dependencies you list, and has no network access unless `requires_network = True`.
2. **Model artifact:** path, size, format (ONNX, Core ML, PyTorch…), license and SHA-256. Small enough to ship inside the app folder, or the download is a user-approved step.
3. **`requirements-detector.txt`:** exact pinned versions of any new runtime dependencies.
4. **Evidence, as JSON plus a one-paragraph summary:**
   - the **synthetic gate** run with your detector swapped in: `python -m tools.eval_detection --seeds 0,1,2 --report <file>.json`. All 23 scenarios must still pass the P-11 thresholds (spec §6);
   - the **real-photo gate**: `python -m tools.eval_real_photos --json <file>.json`, once truth files exist. Until then, use `--coarse` against `tests/real/counts.json` and report found / false positives per photo next to today's baseline of 68/78 with 11 FP;
   - **timing**: median and max per photo, and for a group ROI, with the hardware noted.
5. **Known failure modes** with an example scenario name for each: merged-pair splits, light-cored holes on cardboard, holes on grid lines, and so on.
6. **For an Online/API model:** the exact request and response, the cost per photo, the latency, and what leaves the Mac. This is so the lead can bring the Online-mode design to the user (B-51).

## 7. Integration plan (the build lead does this, not you)

- Add a detector registry: `cv-v1`, the current one and the default, plus yours. The pipeline calls the selected detector through the `HoleDetector` protocol.
- Gate on the synthetic P-11 suite and the real-photo gate. Keep `cv-v1` as the fallback.
- Online detectors appear only once the user approves the Online-mode design, behind the Settings toggle.

## 8. Useful files to read

- `src/rga/vision/detect.py`: today's detector and `DetectParams`.
- `src/rga/vision/ragged.py`: `resolve_shots`, which shows what the resolver needs from ragged blobs.
- `src/rga/vision/pipeline.py`: `analyze_group`, `build_working_image`, `roi_to_working`.
- `src/rga/domain/types.py`: `Blob`, `BlobKind`, `WorkingFrame`.
- `tools/eval_detection.py` and `tools/eval_real_photos.py`, plus `tests/real/README.md`.
- `.claude/specs/rifle-group-analyzer/spec.md` §6 (tolerances), `real-photo-notes.md` (what the real photos look like and their hazards), and decisions.md B-12, B-19, B-21, B-51 and B-54.
