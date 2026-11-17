# plugin-sponsorblock

OmniDesk v2 media extension plugin (`player.timeline.v1`).

## Features
- **K-Anonymity Query (`fetch`)**: Hashes `video_id` with SHA-256 and transmits only the 4-hex-char prefix to Bilibili (`bsbsb.top`) or YouTube (`sponsor.ajay.app`), filtering response buckets strictly by exact `video_id`.
- **Chapter Merge & Deduplication (`merge`)**: Merges external content chapters with SponsorBlock `sponsor`/`ad` segments into sorted `ChapterSegment` payloads.
- **F9 Skip Lookup (`next_segment`)**: Finds the next active or upcoming `sponsor`/`ad` segment relative to `current_sec` and returns `skip_to_sec`.
- **Atomic TTL Cache**: Persists responses atomically via temp file + `fsync` + `os.replace` with configurable `cache_ttl_sec`.
