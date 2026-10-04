# Real target photos: first look (user-supplied, 2026-09-30)

These are five photos from the user, stored in `data/real_photos/real_01..05.jpg` (gitignored, 0700). They arrived **downscaled to about 3 MP (2000×1500)** through chat. Originals from the phone are likely about 12 MP, so the real-photo gate should use originals (AirDrop or Files), because resolution matters for the tolerance targets.

## What each photo shows
| File | Target | Reference | Aim | Holes | Hazards |
|---|---|---|---|---|---|
| real_01 | Grey paper with a **black** 1" grid, an orange bull and a black ring (byallen.com) | Black grid | Orange bull | 9, including a merged pair and a touching pair. **Light cores** (tan backing shows through) with dark torn rims | Heavy **shadow wrinkles**, a **QR code** (dense black squares), printed text, the bull's red edge |
| real_02 | White paper with a **red** grid, a thick red vertical band, and an orange disc with a black **diamond** (GunFun) | Red grid | Black diamond | About 9–10, a merged cluster of 2–3, grey rims with bright pinhole centers | Holes sit on both the red band and white paper; red-on-red contrast is low |
| real_03 | Green "Rigid Crosshair" 1" grid, photographed **upside down** | Green grid | Black diamond | About 20, spread widely | **The paper is curved/bent** (grid lines visibly bow); **red pen circles** mark earlier holes; staples; old holes in cardboard outside the sheet; a hanger hole; printed labels (1L, 2R, ...) |
| real_04 | The same green grid, two sheets overlapped | Green grid | Black diamond | About 15 high-left, several merged or touching | A second sheet edge; a hole in the upper sheet; staples; holes in cardboard |
| real_05 | The same green grid, small caliber | Green grid | Diamond **and a user-made blue tape cross with a dot** | Three-plus groups on one sheet (one at the diamond, one high-right, one around the tape cross) plus scattered shots | Multi-aim confirms P-08 matters. The aim point can be any user mark, not just a printed dot |
| real_06 (seen in chat; **not saved**, re-send as a file) | Orange stick-on disc with black diamond and tick marks, stuck on **bare corrugated cardboard**, no paper target | **Orange circle only** (likely the 3" circle) | Black diamond (mostly shot away) | About 7: one **large ragged hole of several shots** at center (the FR-3.7 case), others on the disc rim and on the cardboard | Corrugation stripes and **cracks/creases in the cardboard** that look like dark lines; strong low-angle shadows; a small dark dot (a decoy) low on the cardboard; holes on cardboard are uniformly dark, holes on the disc show light cores |

## Gaps between the synthetic catalog and these photos (for T2.18)
0. **Backgrounds aren't always paper:** a sticker on raw cardboard (real_06) gives a textured brown background with creases. The circle reference sits on cardboard, and holes appear in both materials.
1. **Grid line color varies:** black, red and green. The synthetic grid is blue only, so reference_grid must not key on one hue.
2. **Hole appearance:** the most common is a **light core** (backing or paper visible) with a dark, torn rim. Fully dark holes are rarer. The synthetic catalog mostly draws dark holes.
3. **Non-hole dark structure:**
   - QR codes, text and labels
   - grid intersections
   - aim diamonds
   - pen circles around old holes
   - staples
   - shadows and wrinkles
   - the second sheet's edge
4. **Non-planar paper** (real_03): a single homography can't flatten it. Decision: detect large homography residuals on the grid and **warn "flatten the target and re-shoot"** (as P-03 does for tilt), not attempt a non-rigid warp.
5. **Aim marks:** printed diamonds and user tape crosses. Aim snap must handle a diamond and a cross center, and the manual aim click is always available.
6. **Holes outside the sheet** (cardboard backer) must not be counted. ROI per group handles this.
7. **Orientation:** photos may be rotated 180°. Grid-based rectification handles this, but the UI must not assume "up".
8. **Pen-marked old holes:** the user decided to **always use a fresh sheet** (B-12). Reused sheets aren't supported, and every hole in a group's region counts.

## Ground truth still needed
To score detection, each photo needs the true hole centers. The plan (T2.15) defines an annotation file. The lighter path is for the user to mark the holes in the app's review editor once it exists, so their confirmed clicks become the truth file.
