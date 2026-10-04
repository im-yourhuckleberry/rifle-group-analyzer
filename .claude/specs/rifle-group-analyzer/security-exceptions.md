# Security Exceptions: rifle-group-analyzer

| ID | Risk | Status | Compensating controls | Resolution |
|---|---|---|---|---|
| SE-1 | No authentication on the local API (design §11) | Accepted (D-09, P-14) | Loopback-only bind (config exits 2 otherwise), TrustedHost allowlist, OriginGuard + JSON/multipart-only writes, CSP/anti-framing headers | Permanent for v1 (single user, local only) |
| SE-2 | **G2-C1:** one crafted upload within the 40 MB / 60 MP caps can make Pillow's EXIF/IFD parser allocate gigabytes (≈ metadata bytes × IFD entry count) | **Resolved (Group 3 cycle 4, review-32, 2026-10-02):** whitelist rebuild + segment/chunk caps; no Pillow IFD parse; 40 MB floods ≤ 0.4 MB | Single-user, loopback-only app; the user uploads their own phone photos (real EXIF is tiny); fails closed (nothing stored) | Fixed by **T3.00** (256 KB metadata cap before parsing, with memory-bounded tests), verified in Group 3 review |
| SE-3 | Progressive JPEG with thousands of tiny extra scans pins a CPU during decode (162 KB → 36.8 s under load); pre-existing, Pillow-level | **Fix landed (T4.0b, cap 64 scans); open until the Group 4 review verifies it** | Single-user, loopback-only app; the user uploads their own phone photos | Fix in Group 4: cap the scan count (FF DA after the first SOS) in io.py |
| SE-4 | New bounded EXIF DateTimeOriginal reader (one sub-IFD hop) for per-group dates (B-66/B-67) | **Open, tracked; implemented by R3** | Same SE-2 class: our own bounded reader, Pillow never parses EXIF | Group 4 review verifies the bounds, tracemalloc and IFD spy |
